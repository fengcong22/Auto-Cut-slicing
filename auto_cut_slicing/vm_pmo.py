"""Offline VM/PMO plan and render adapters for the maintained slicing engine."""

import argparse
import contextlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

from .alignment import load_alignment, parse_time_us
from .errors import SlicingError, VisualSpeechSyncError
from .instructions import parse_instruction
from .io import file_sha256, read_json, write_json, write_new_json
from .media import describe_source
from .planner import compile_job
from .render import render_output
from .upstream import transcribe_source

PROTOCOL = 'vm_pmo_review_video'
HASH = re.compile(r'[0-9a-f]{64}\Z')


V3_CONTEXT_PATHS = ('evidence_dir', 'intent_context_path', 'intent_path', 'analyze_result_path',
                    'qa_path', 'qa_review_path')
PLANNING_MODES = ('rule', 'ai_intent')


class VmError(SlicingError):
    def __init__(self, code, message, blockers=None):
        super().__init__(message)
        self.code = code
        self.blockers = blockers or []


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise VmError('INVALID_INPUT', f'{label} must contain exactly {sorted(fields)}')
    return value


def _id(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise VmError('INVALID_INPUT', f'{label} must be a nonempty identifier')
    return value


def _hash(value, label):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise VmError('INVALID_INPUT', f'{label} must be a lowercase SHA-256 digest')
    return value


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise VmError('INVALID_INPUT', f'{label} must be a positive integer')
    return value


def _path(value, label):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise VmError('INVALID_CONTEXT', f'{label} must be an absolute path')
    path = Path(value)
    if path != path.resolve():
        raise VmError('INVALID_CONTEXT', f'{label} must be a canonical path without links')
    return path


def _regular_file(path, code):
    try:
        info = path.lstat()
    except OSError as error:
        raise VmError(code, 'Required local file is unavailable') from error
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1 or
            path != path.resolve()):
        raise VmError(code, 'Required file must be ordinary and have one link')


def _identity(input_data):
    return {key: input_data[key] for key in
            ('package', 'binding', 'record', 'review_round', 'delivery_target')}


def _instruction_results(input_data):
    return [{
        'instruction_id': item.get('instruction_id'),
        'source_field_id': item.get('source_field_id'),
        'raw_text': item.get('raw_text'),
        'status': 'blocked', 'segments': [],
    } for item in input_data.get('instructions', []) if isinstance(item, dict)]


def _receipt_base(input_data, context):
    return {
        'protocol': PROTOCOL, 'schema_version': input_data.get('schema_version', 1),
        **_identity(input_data),
        'input_sha256': context['input_sha256'],
        'source_sha256': input_data['source']['sha256'],
    }


def _ready_receipt(base, plan, plan_sha256):
    receipt = {**base, 'status': 'ready', 'plan_sha256': plan_sha256,
               'instruction_results': plan.get('instruction_results'), 'error': None}
    if base.get('schema_version') == 3:
        receipt.update(planning_mode=plan.get('planning_mode'),
                       intent_sha256=plan.get('intent_sha256'),
                       coverage=plan.get('coverage'), blockers=[])
    return receipt


def _blocked_receipt(base, input_data, paths, error):
    receipt = {**base, 'status': 'blocked', 'plan_sha256': None,
               'instruction_results': _instruction_results(input_data),
               'error': _error(error)}
    if base.get('schema_version') == 3:
        intent_path = paths.get('intent_path')
        intent_hash = None
        if intent_path is not None and intent_path.is_file() and not intent_path.is_symlink():
            intent_hash = file_sha256(intent_path)
        blockers = getattr(error, 'blockers', None) or [receipt['error']]
        receipt.update(planning_mode=input_data.get('planning_mode'), intent_sha256=intent_hash,
                       coverage=None, blockers=blockers)
    return receipt


def _error(error):
    code = getattr(error, 'code', 'EXECUTION_FAILED')
    messages = {
        'INVALID_INPUT': 'VM/PMO input does not match its supported schema.',
        'INVALID_CONTEXT': 'Trusted local context is invalid.',
        'INPUT_CHANGED': 'Input bytes differ from the trusted context.',
        'PACKAGE_MISMATCH': 'Installed package identity differs from the request.',
        'UNSUPPORTED_ROUND': 'This review stage or parent is not supported yet.',
        'UNSUPPORTED_PROFILE': 'The requested output profile is not supported.',
        'UNSUPPORTED_INSTRUCTION': 'One or more editing instructions need review or additional evidence.',
        'WORD_ALIGNMENT_UNRESOLVED': 'Required source-bound word timing could not be verified.',
        'SOURCE_INVALID': 'The source media cannot be used for this profile.',
        'SOURCE_CHANGED': 'Source media differs from the bound input.',
        'OUTPUT_EXISTS': 'A planned output already exists; this run will not overwrite it.',
        'PLAN_CHANGED': 'The plan or ready receipt differs from this run.',
        'ANALYSIS_REQUIRED': 'AI planning requires a ready analyze result for this run.',
        'INTENT_MISSING': 'AI planning did not produce an edit intent file.',
        'INTENT_INVALID': 'The edit intent failed validation.',
        'INTENT_BLOCKED': 'The edit intent is blocked; see blockers.',
        'INTENT_RULE_MISMATCH': 'The edit intent and the fixed-pattern parser compile to different edits.',
        'RENDER_REQUIRED': 'QA requires a rendered MP4 for this run.',
        'REVIEW_MISSING': 'The AI picture review was not written for this run.',
        'REVIEW_INVALID': 'The AI picture review does not match the QA report.',
        'EXECUTION_FAILED': 'The local media operation failed.',
    }
    return {'code': code, 'message': messages.get(code, messages['EXECUTION_FAILED'])}


def _validate_input(input_data):
    required = {'protocol', 'schema_version', 'package', 'binding', 'record',
                'review_round', 'source', 'instructions', 'output_profile',
                'delivery_target'}
    version = input_data.get('schema_version') if isinstance(input_data, dict) else None
    optional = {'manual_revision'}
    if version == 3:
        required = required | {'planning_mode'}
        optional = optional | {'prior_rounds', 'ai_session', 'time_maps'}
    if not isinstance(input_data, dict) or not required <= set(input_data) or (
        set(input_data) - required - optional
    ):
        raise VmError('INVALID_INPUT', 'Input contains missing or unsupported fields')
    if input_data['protocol'] != PROTOCOL or type(version) is not int or version not in (1, 2, 3):
        raise VmError('INVALID_INPUT', 'Unsupported VM/PMO protocol or schema version')
    if 'manual_revision' in input_data and version not in (2, 3):
        raise VmError('INVALID_INPUT', 'Manual revisions require schema v2 or v3')
    if version == 3:
        _validate_v3_fields(input_data)
    package = _object(input_data['package'], ('alias', 'name', 'version', 'manifest_sha256'), 'package')
    _id(package['alias'], 'package.alias')
    manifest = Path(__file__).resolve().parents[1] / 'PACKAGE-MANIFEST.json'
    if (package['name'], package['version']) != ('auto-cut-slicing', '0.3.0') or _hash(package['manifest_sha256'], 'package.manifest_sha256') != file_sha256(manifest):
        raise VmError('PACKAGE_MISMATCH', 'Installed package identity or manifest bytes differ')
    binding = _object(input_data['binding'], ('task_id', 'run_id', 'subject_key',
                                              'config_version', 'stage_id', 'event_id'), 'binding')
    for key, value in binding.items():
        _id(value, f'binding.{key}')
    if version == 1 and binding['stage_id'] != 'initial':
        raise VmError('UNSUPPORTED_ROUND', 'Schema v1 supports only the initial stage')
    if version >= 2 and binding['stage_id'] not in ('initial', 'review'):
        raise VmError('UNSUPPORTED_ROUND', 'Schema v2/v3 supports only initial and review stages')
    record = _object(input_data['record'], ('base_token', 'table_id', 'record_id'), 'record')
    for key, value in record.items():
        _id(value, f'record.{key}')
    review_keys = ('round_id', 'input_snapshot_id', 'parent')
    raw_review = input_data['review_round']
    if version == 3 and isinstance(raw_review, dict) and 'source_basis' in raw_review:
        review_keys = review_keys + ('source_basis',)
    review = _object(raw_review, review_keys, 'review_round')
    # A rebased review re-plans the whole round on the original VM source (see the v3 contract).
    rebased = review.get('source_basis') == 'vm_original'
    if 'source_basis' in review and not rebased:
        raise VmError('INVALID_INPUT', 'review_round.source_basis must be vm_original')
    _id(review['round_id'], 'review_round.round_id')
    _id(review['input_snapshot_id'], 'review_round.input_snapshot_id')
    source = _object(input_data['source'], ('source_id', 'timeline_basis', 'vmid',
                                            'sha256', 'bytes', 'duration_ms'), 'source')
    _id(source['source_id'], 'source.source_id')
    _id(source['vmid'], 'source.vmid')
    _hash(source['sha256'], 'source.sha256')
    _positive_int(source['bytes'], 'source.bytes')
    _positive_int(source['duration_ms'], 'source.duration_ms')
    expected_timeline = 'vm_original'
    if version == 1:
        if review['parent'] is not None:
            raise VmError('UNSUPPORTED_ROUND', 'Schema v1 supports only parentless initial rounds')
    elif binding['stage_id'] == 'review':
        expected_timeline = 'previous_review_video'
        parent = _object(review['parent'], ('kind', 'run_id', 'artifact_id', 'delivery_id',
                                             'sha256', 'bytes', 'duration_ms'), 'review parent')
        if parent['kind'] != 'managed_run':
            raise VmError('UNSUPPORTED_ROUND', 'Review requires a Taskboard-managed delivered parent')
        for key in ('run_id', 'artifact_id', 'delivery_id'):
            _id(parent[key], f'review parent.{key}')
        _hash(parent['sha256'], 'review parent.sha256')
        _positive_int(parent['bytes'], 'review parent.bytes')
        _positive_int(parent['duration_ms'], 'review parent.duration_ms')
        if parent['run_id'] == binding['run_id'] or not rebased and any(
            parent[key] != source[source_key]
            for key, source_key in (('sha256', 'sha256'), ('bytes', 'bytes'),
                                    ('duration_ms', 'duration_ms'))
        ):
            raise VmError('UNSUPPORTED_ROUND', 'Review source does not match its frozen parent')
        if rebased:
            if input_data.get('planning_mode') != 'ai_intent':
                raise VmError('UNSUPPORTED_ROUND', 'A review on the original source requires ai_intent planning')
            expected_timeline = 'vm_original'
    elif rebased:
        raise VmError('UNSUPPORTED_ROUND', 'Only review rounds may be rebased on the original source')
    elif review['parent'] is not None:
        raise VmError('UNSUPPORTED_ROUND', 'Initial rounds must not have a parent')
    if source['timeline_basis'] != expected_timeline:
        raise VmError('UNSUPPORTED_ROUND', 'Source timeline does not match the bound workflow stage')
    profile = input_data['output_profile']
    if (not isinstance(profile, dict) or
            profile != {'id': 'review_mp4_v1', 'width': 1920, 'height': 1080,
                        'fps': 25, 'audio': 'preserve'} or
            any(type(profile[key]) is not int for key in ('width', 'height', 'fps'))):
        raise VmError('UNSUPPORTED_PROFILE', 'Output profile must be review_mp4_v1, 1080p25 with preserved audio')
    target = _object(input_data['delivery_target'], ('attachment_field_id',), 'delivery_target')
    _id(target['attachment_field_id'], 'delivery_target.attachment_field_id')
    time_maps = _time_maps(input_data, source) if rebased else {}
    if not rebased and 'time_maps' in input_data:
        raise VmError('INVALID_INPUT', 'time_maps are only used by reviews on the original source')
    instructions = input_data['instructions']
    if not isinstance(instructions, list) or not instructions:
        raise VmError('INVALID_INPUT', 'instructions must be a nonempty list')
    seen = set()
    for item in instructions:
        allowed_instruction_fields = ['instruction_id', 'source_field_id', 'raw_text',
                                      'timeline_basis', 'applicability']
        if version >= 2:
            allowed_instruction_fields.extend(['visual_evidence', 'must_keep'])
        if rebased:
            allowed_instruction_fields.append('time_map_id')
        required_instruction_fields = ['instruction_id', 'source_field_id', 'raw_text',
                                       'timeline_basis', 'applicability']
        if not isinstance(item, dict) or set(item) - set(allowed_instruction_fields) or any(
            key not in item for key in required_instruction_fields
        ):
            raise VmError('INVALID_INPUT', 'instruction does not match the selected schema')
        instruction_id = _id(item['instruction_id'], 'instruction.instruction_id')
        if instruction_id in seen:
            raise VmError('INVALID_INPUT', 'Instruction IDs must be unique')
        seen.add(instruction_id)
        _id(item['source_field_id'], 'instruction.source_field_id')
        if not isinstance(item['raw_text'], str) or not item['raw_text'].strip():
            raise VmError('INVALID_INPUT', 'instruction.raw_text must be nonempty')
        if rebased:
            # Earlier rounds' feedback is re-applied; review feedback quotes times on the cut it reviewed.
            if item['applicability'] not in ('current_round', 'carried_over') or (
                item['timeline_basis'] == 'vm_original' and 'time_map_id' in item
            ) or (item['timeline_basis'] == 'previous_review_video'
                  and item.get('time_map_id') not in time_maps) or item['timeline_basis'] not in (
                'vm_original', 'previous_review_video'):
                raise VmError('UNSUPPORTED_INSTRUCTION', 'Instruction timeline or time map is invalid')
        elif item['timeline_basis'] != expected_timeline or item['applicability'] != 'current_round':
            raise VmError('UNSUPPORTED_INSTRUCTION', 'Instruction timeline must match the current workflow stage')
        if 'visual_evidence' in item:
            evidence = _object(item['visual_evidence'], ('source_sha256', 'frame_time', 'end'),
                               'visual evidence')
            if _hash(evidence['source_sha256'], 'visual evidence.source_sha256') != source['sha256']:
                raise VmError('UNSUPPORTED_INSTRUCTION', 'Visual evidence must match the selected source')
        if 'must_keep' in item and (not isinstance(item['must_keep'], list)
            or any(not isinstance(phrase, str) or not phrase.strip() or '\0' in phrase
                   for phrase in item['must_keep'])):
            raise VmError('INVALID_INPUT', 'must_keep must be a list of nonempty phrases')
    if 'manual_revision' in input_data:
        _manual_revision_clips(input_data)


def _time_maps(input_data, source):
    """Segment tables of the cuts reviewers watched: target (cut) time -> original source time."""
    rows = input_data.get('time_maps')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise VmError('INVALID_INPUT', 'A review on the original source requires 1-20 time_maps')
    maps = {}
    limit = source['duration_ms'] * 1000
    for row in rows:
        row = _object(row, ('time_map_id', 'plan_sha256', 'segments'), 'time map')
        map_id = _id(row['time_map_id'], 'time_map.time_map_id')
        _hash(row['plan_sha256'], 'time_map.plan_sha256')
        segments = row['segments']
        if map_id in maps or not isinstance(segments, list) or not 1 <= len(segments) <= 500:
            raise VmError('INVALID_INPUT', 'time map ids must be unique with 1-500 segments')
        offset = 0
        for segment in segments:
            segment = _object(segment, ('source_start_us', 'duration_us', 'target_start_us'), 'time map segment')
            if (type(segment['source_start_us']) is not int or segment['source_start_us'] < 0
                    or type(segment['duration_us']) is not int or segment['duration_us'] < 1
                    or segment['source_start_us'] + segment['duration_us'] > limit
                    or segment['target_start_us'] != offset):
                raise VmError('INVALID_INPUT', 'time map segments must be contiguous and inside the source')
            offset += segment['duration_us']
        maps[map_id] = segments
    return maps


def _validate_v3_fields(input_data):
    mode = input_data['planning_mode']
    if mode not in PLANNING_MODES:
        raise VmError('INVALID_INPUT', 'planning_mode must be rule or ai_intent')
    if mode == 'ai_intent':
        if 'manual_revision' in input_data:
            raise VmError('INVALID_INPUT', 'manual_revision cannot be combined with ai_intent planning')
        session = _object(input_data.get('ai_session'), ('agent', 'model', 'session_id'), 'ai_session')
        for key, value in session.items():
            _id(value, f'ai_session.{key}')
    elif 'ai_session' in input_data:
        raise VmError('INVALID_INPUT', 'ai_session is only used with ai_intent planning')
    rounds = input_data.get('prior_rounds', [])
    if not isinstance(rounds, list):
        raise VmError('INVALID_INPUT', 'prior_rounds must be a list')
    for row in rounds:
        _object(row, ('round_id', 'instructions'), 'prior round')
        _id(row['round_id'], 'prior round.round_id')
        if not isinstance(row['instructions'], list):
            raise VmError('INVALID_INPUT', 'prior round instructions must be a list')
        for item in row['instructions']:
            _object(item, ('instruction_id', 'raw_text'), 'prior round instruction')
            _id(item['instruction_id'], 'prior round instruction_id')
            if not isinstance(item['raw_text'], str):
                raise VmError('INVALID_INPUT', 'prior round raw_text must be text')


def _manual_revision_clips(input_data):
    if len(input_data['instructions']) != 1:
        raise VmError('UNSUPPORTED_INSTRUCTION', 'A run must contain exactly one instruction snapshot')
    revision = _object(input_data['manual_revision'],
                       ('source_sha256', 'note', 'clips'), 'manual revision')
    if _hash(revision['source_sha256'], 'manual revision source') != input_data['source']['sha256']:
        raise VmError('INVALID_INPUT', 'Manual revision source differs from the approved source')
    note = revision['note']
    if not isinstance(note, str) or not note.strip() or len(note) > 1024 or '\0' in note:
        raise VmError('INVALID_INPUT', 'Manual revision note is invalid')
    rows = revision['clips']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise VmError('INVALID_INPUT', 'Manual revision requires 1-8 clips')
    duration_ms = input_data['source']['duration_ms']
    clips = []
    previous_end = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) not in (
            {'start_ms', 'end_ms'}, {'start_ms', 'end_ms', 'freeze'}
        ):
            raise VmError('INVALID_INPUT', 'Manual revision clip has unsupported fields')
        start, end = row['start_ms'], row['end_ms']
        if any(type(value) is not int for value in (start, end)) or not previous_end <= start < end <= duration_ms:
            raise VmError('INVALID_INPUT', 'Manual revision clip is outside the approved source')
        clip = {'id': f'manual-{index}', 'source': input_data['source']['source_id'],
                'start': start / 1000, 'end': end / 1000, 'boundary_policy': 'exact'}
        if 'freeze' in row:
            freeze = _object(row['freeze'], ('start_ms', 'end_ms', 'frame_ms'), 'manual freeze')
            first, last, frame = freeze['start_ms'], freeze['end_ms'], freeze['frame_ms']
            if any(type(value) is not int for value in (first, last, frame)) or (
                not start <= first < last <= end or not 0 <= frame < duration_ms
            ):
                raise VmError('INVALID_INPUT', 'Manual freeze is outside the approved source or clip')
            clip['edits'] = [{'id': f'freeze-{index}', 'kind': 'freeze_frame',
                              'start': first / 1000, 'end': last / 1000,
                              'frame_time': frame / 1000, 'source_text': note}]
        clips.append(clip)
        previous_end = end
    return clips


def _validate_context(context, input_path, context_path):
    if not isinstance(context, dict):
        raise VmError('INVALID_CONTEXT', 'Context must be an object')
    version = context.get('schema_version')
    if context.get('protocol') != PROTOCOL or type(version) is not int or version not in (1, 2, 3):
        raise VmError('INVALID_CONTEXT', 'Unsupported VM/PMO context version')
    keys = ('run_dir', 'source_path', 'plan_path', 'plan_result_path', 'result_path', 'output_path')
    if version == 3:
        keys = keys + V3_CONTEXT_PATHS
    _object(context, ('protocol', 'schema_version', 'input_sha256') + keys, 'context')
    if _hash(context['input_sha256'], 'context.input_sha256') != file_sha256(input_path):
        raise VmError('INPUT_CHANGED', 'Input bytes differ from the trusted context')
    paths = {key: _path(context[key], f'context.{key}') for key in keys}
    run_dir = paths['run_dir']
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise VmError('INVALID_CONTEXT', 'run_dir must be an existing regular directory')
    if input_path != run_dir / 'input.json' or context_path != run_dir / 'execution-context.json':
        raise VmError('INVALID_CONTEXT', 'Input and context must use fixed names inside run_dir')
    for key in ('plan_path', 'plan_result_path', 'result_path', 'output_path') + (
        V3_CONTEXT_PATHS if version == 3 else ()
    ):
        if not paths[key].is_relative_to(run_dir) or paths[key] == run_dir:
            raise VmError('INVALID_CONTEXT', f'{key} must be inside run_dir')
    if version == 3:
        fixed = {run_dir / 'alignment.words.json', run_dir / 'output.words.json',
                 run_dir / 'qa-result.json', run_dir / 'qa-review-result.json',
                 run_dir / 'qa.before-review.json', run_dir / '.plan.lock'}
        if fixed & set(paths.values()):
            raise VmError('INVALID_CONTEXT', 'Context paths must not reuse fixed run files')
        for key in ('intent_context_path', 'intent_path', 'analyze_result_path', 'qa_path',
                    'qa_review_path'):
            if paths[key].suffix.lower() != '.json':
                raise VmError('INVALID_CONTEXT', f'{key} must end in .json')
            if paths[key].is_relative_to(paths['evidence_dir']):
                raise VmError('INVALID_CONTEXT', f'{key} must not be inside evidence_dir')
    if len(set(paths.values())) != len(paths) or input_path in paths.values():
        raise VmError('INVALID_CONTEXT', 'Context paths must be distinct from each other and input')
    if paths['output_path'].suffix.lower() != '.mp4':
        raise VmError('INVALID_CONTEXT', 'output_path must end in .mp4')
    for key in ('plan_path', 'plan_result_path', 'result_path'):
        if paths[key].suffix.lower() != '.json':
            raise VmError('INVALID_CONTEXT', f'{key} must end in .json')
    return paths


@contextlib.contextmanager
def _plan_lock(run_dir):
    lock_path = run_dir / '.plan.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise VmError('INVALID_CONTEXT', 'Plan lock must be an ordinary local file')
        with os.fdopen(descriptor, 'r+b') as stream:
            descriptor = None
            if info.st_size == 0:
                stream.write(b'\0')
                stream.flush()
            if os.name == 'nt':
                import msvcrt

                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _existing_plan(input_data, paths, base):
    if not paths['plan_path'].exists() and not paths['plan_result_path'].exists():
        return None
    if not paths['plan_result_path'].exists():
        raise VmError('OUTPUT_EXISTS', 'A partial plan already exists; this run will not overwrite it')
    _regular_file(paths['plan_result_path'], 'PLAN_CHANGED')
    receipt = read_json(paths['plan_result_path'])
    if not isinstance(receipt, dict) or {key: receipt.get(key) for key in base} != base:
        raise VmError('OUTPUT_EXISTS', 'Existing plan result belongs to a different request')
    if receipt.get('status') == 'ready':
        _regular_file(paths['plan_path'], 'PLAN_CHANGED')
        saved_plan = read_json(paths['plan_path'])
        if (not isinstance(saved_plan, dict)
                or receipt.get('plan_sha256') != file_sha256(paths['plan_path'])
                or any(saved_plan.get(key) != value for key, value in base.items())):
            raise VmError('PLAN_CHANGED', 'Existing plan differs from its ready receipt')
        descriptor = _source_descriptor(input_data, paths['source_path'])
        alignment_sha256 = saved_plan.get('alignment_sha256')
        if alignment_sha256 is not None:
            alignment_path = paths['run_dir'] / 'alignment.words.json'
            _regular_file(alignment_path, 'PLAN_CHANGED')
            if file_sha256(alignment_path) != alignment_sha256:
                raise VmError('PLAN_CHANGED', 'Existing alignment differs from the saved plan')
            try:
                alignment = load_alignment(alignment_path, descriptor)
            except SlicingError as error:
                raise VmError('PLAN_CHANGED', 'Existing alignment no longer matches the source') from error
            if alignment['sha256'] != alignment_sha256:
                raise VmError('PLAN_CHANGED', 'Validated alignment differs from the saved plan')
        intent_sha256 = saved_plan.get('intent_sha256')
        if intent_sha256 is not None:
            _regular_file(paths['intent_path'], 'PLAN_CHANGED')
            if file_sha256(paths['intent_path']) != intent_sha256:
                raise VmError('PLAN_CHANGED', 'Existing edit intent differs from the saved plan')
        expected_receipt = _ready_receipt(base, saved_plan, file_sha256(paths['plan_path']))
        if receipt != expected_receipt:
            raise VmError('PLAN_CHANGED', 'Existing ready receipt does not match its plan')
    elif receipt.get('status') == 'blocked':
        if paths['plan_path'].exists():
            raise VmError('PLAN_CHANGED', 'A blocked plan result cannot have a plan file')
    else:
        raise VmError('PLAN_CHANGED', 'Existing plan result has an unsupported status')
    return receipt


def _source_descriptor(input_data, source_path):
    source = input_data['source']
    _regular_file(source_path, 'SOURCE_INVALID')
    try:
        descriptor = describe_source(source_path, input_demuxer='mov')
    except SlicingError as error:
        raise VmError('SOURCE_INVALID', 'Source must be a self-contained MOV/MP4') from error
    if (descriptor['sha256'] != source['sha256'] or
            source_path.stat().st_size != source['bytes'] or
            round(descriptor['duration_us'] / 1000) != source['duration_ms']):
        raise VmError('SOURCE_CHANGED', 'Source bytes, size, or duration differ from input')
    if not descriptor['has_audio']:
        raise VmError('SOURCE_INVALID', 'review_mp4_v1 requires a source audio stream')
    return descriptor


def _segments(input_data, descriptor):
    instructions = input_data['instructions']
    if len(instructions) != 1:
        raise VmError('UNSUPPORTED_INSTRUCTION', 'This version supports exactly one instruction per run')
    item = instructions[0]
    try:
        clips = parse_instruction(item['raw_text'], input_data['source']['source_id'],
                                  descriptor['duration_us'])
    except SlicingError as error:
        raise VmError('UNSUPPORTED_INSTRUCTION', str(error)) from error
    segments = []
    for clip in clips:
        if set(clip) != {'id', 'source', 'start', 'end'}:
            raise VmError('UNSUPPORTED_INSTRUCTION', 'Speech, visual, deletion, or other semantic operations require evidence and a later protocol')
        start_us = parse_time_us(clip['start'])
        end_us = parse_time_us(clip['end'])
        if start_us % 1000 or end_us % 1000:
            raise VmError('UNSUPPORTED_INSTRUCTION', 'This protocol requires millisecond-aligned ranges')
        segments.append({'segment_id': clip['id'], 'source_id': clip['source'],
                         'start_ms': start_us // 1000, 'end_ms': end_us // 1000})
    return segments


def _parse_review_clips(input_data, descriptor):
    if len(input_data['instructions']) != 1:
        raise VmError('UNSUPPORTED_INSTRUCTION', 'A run must contain exactly one instruction snapshot')
    item = input_data['instructions'][0]
    try:
        clips = parse_instruction(item['raw_text'], input_data['source']['source_id'],
                                  descriptor['duration_us'],
                                  visual_evidence=item.get('visual_evidence'))
        protected = item.get('must_keep', [])
        for clip in clips:
            for operation in [*clip.get('deletions', []), *clip.get('edits', [])]:
                if operation.get('kind') in ('word', 'mute_word'):
                    operation['must_keep'] = list(protected)
        return clips
    except SlicingError as error:
        raise VmError('UNSUPPORTED_INSTRUCTION', 'The instruction needs supported, source-bound evidence') from error


def _requires_alignment(clips):
    return any(
        'start_cue' in clip
        or any(row.get('kind') in ('word', 'mute_word') for key in ('deletions', 'edits')
               for row in clip.get(key, []))
        for clip in clips
    )


def _alignment_path(input_data, source_path, descriptor, run_dir):
    target = run_dir / 'alignment.words.json'
    if not target.exists():
        try:
            transcribe_source({'path': str(source_path), 'sha256': descriptor['sha256']}, target)
        except Exception as error:
            raise VmError('WORD_ALIGNMENT_UNRESOLVED', 'Required source-bound word timing is unavailable') from error
    _regular_file(target, 'WORD_ALIGNMENT_UNRESOLVED')
    try:
        load_alignment(target, descriptor)
    except SlicingError as error:
        raise VmError('WORD_ALIGNMENT_UNRESOLVED', 'Word timing does not match the selected source') from error
    return target


def _engine_plan(input_data, source_path, descriptor, segments, run_dir):
    source_id = input_data['source']['source_id']
    job = {'schema_version': 1, 'name': 'VM PMO review',
           'sources': [{'id': source_id, 'path': str(source_path)}],
           'outputs': [{'id': 'review', 'name': 'review', 'clips': [
               {'id': row['segment_id'], 'source': source_id, 'boundary_policy': 'exact',
                'start': row['start_ms'] / 1000, 'end': row['end_ms'] / 1000}
               for row in segments]}]}
    with tempfile.TemporaryDirectory(prefix='.vm-plan-', dir=run_dir) as temporary:
        job_path = write_json(Path(temporary) / 'job.json', job)
        return compile_job(job_path, describe=lambda _: descriptor)


def _review_engine_plan(input_data, source_path, descriptor, clips, run_dir, alignment_path=None):
    source_id = input_data['source']['source_id']
    source = {'id': source_id, 'path': str(source_path)}
    if alignment_path is not None:
        source['asr'] = str(alignment_path)
    job = {'schema_version': 1, 'name': 'VM PMO review', 'sources': [source],
           'outputs': [{'id': 'review', 'name': 'review', 'clips': clips}]}
    with tempfile.TemporaryDirectory(prefix='.vm-plan-', dir=run_dir) as temporary:
        job_path = write_json(Path(temporary) / 'job.json', job)
        try:
            return compile_job(job_path, describe=lambda _: descriptor)
        except VisualSpeechSyncError as error:
            raise VmError(
                'UNSUPPORTED_INSTRUCTION', 'Speech cue does not align with the visual anchor.'
            ) from error
        except SlicingError as error:
            code = 'WORD_ALIGNMENT_UNRESOLVED' if alignment_path is not None else 'UNSUPPORTED_INSTRUCTION'
            message = ('Required source-bound word timing is ambiguous or invalid.'
                       if alignment_path is not None else
                       'The instruction needs supported, source-bound evidence.')
            raise VmError(code, message) from error


def _operation_rows(output, with_intent=False):
    rows = _operation_rows_base(output)
    if with_intent:
        for row in rows:
            clip_id, _, item = row['operation_id'].partition('/')
            row['intent_id'] = clip_id if item == 'start_cue' else item
    return rows


def _operation_rows_base(output):
    rows = []
    for cue in output.get('start_cues', []):
        row = {
            'operation_id': f"{cue['clip_id']}/start_cue", 'kind': 'start_cue',
            'source_start_us': cue['source_start_us'],
            'source_end_us': cue['source_end_us'],
            'requested_start_us': cue['requested_start_us'],
            'search_end_us': cue['search_end_us'],
            'search_radius_us': cue['search_radius_us'],
            'matched_text': cue['matched_text'],
            'match_strategy': cue['match_strategy'],
            'alignment_sha256': cue['alignment_sha256'],
            'provider': cue['provider'], 'model': cue['model'], 'adapter': cue['adapter'],
        }
        for field in ('visual_anchor_us', 'visual_sync_tolerance_us', 'visual_sync_delta_us'):
            if field in cue:
                row[field] = cue[field]
        rows.append(row)
    for edit in output.get('edits', []):
        rows.append({
            'operation_id': edit['item_id'], 'kind': edit['kind'],
            'source_start_us': edit['source_start_us'],
            'source_end_us': edit['source_end_us'],
            'target_join_us': edit['target_join_us'],
            'matched_text': edit.get('matched_text'),
            'match_strategy': edit.get('match_strategy'),
            'alignment_sha256': edit.get('alignment_sha256'),
            'provider': edit.get('provider'), 'model': edit.get('model'),
            'adapter': edit.get('adapter'),
            'must_keep_evidence': edit.get('must_keep_evidence', []),
        })
    for effect in output.get('effects', []):
        if effect['kind'] == 'mute':
            rows.append({
                'operation_id': effect['item_id'], 'kind': 'mute_word',
                'source_start_us': effect['source_start_us'],
                'source_end_us': effect['source_end_us'],
                'target_join_us': effect['target_start_us'],
                'matched_text': effect.get('requested_text'),
                'match_strategy': effect.get('match_strategy'),
                'alignment_sha256': effect.get('alignment_sha256'),
                'provider': effect.get('provider'), 'model': effect.get('model'),
                'adapter': effect.get('adapter'),
                'must_keep_evidence': effect.get('must_keep_evidence', []),
            })
            continue
        rows.append({
            'operation_id': effect['item_id'], 'kind': effect['kind'],
            'source_start_us': effect.get('source_start_us'),
            'source_end_us': effect.get('source_end_us'),
            'target_start_us': effect.get('target_start_us'),
            'frame_time_us': effect.get('frame_time_us'),
            'alignment_sha256': effect.get('alignment_sha256'),
        })
    return rows


def _intent_context_data(input_data, paths, alignment_path):
    instructions = []
    for item in input_data['instructions']:
        row = {'instruction_id': item['instruction_id'], 'raw_text': item['raw_text']}
        if 'must_keep' in item:
            row['must_keep'] = list(item['must_keep'])
        if 'time_map_id' in item:
            maps = {entry['time_map_id']: entry['segments'] for entry in input_data['time_maps']}
            row['time_map'] = [dict(segment) for segment in maps[item['time_map_id']]]
        instructions.append(row)
    return {
        'schema': 'auto-cut-slicing/intent-context', 'schema_version': 1,
        'timeline_basis': input_data['source']['timeline_basis'],
        'sources': [{'source_id': input_data['source']['source_id'],
                     'path': str(paths['source_path']), 'asr': str(alignment_path)}],
        'instructions': instructions,
        'evidence_dir': os.path.relpath(paths['evidence_dir'],
                                        paths['intent_context_path'].parent),
    }


def _edit_context(paths, descriptor):
    from .context import EditContext

    try:
        return EditContext(paths['intent_context_path'], describe=lambda _: descriptor)
    except SlicingError as error:
        raise VmError('INVALID_INPUT', 'Instruction or source IDs cannot form an intent context') from error


def _analysis(input_data, context, paths, descriptor):
    """Return the alignment bound by a ready analyze result after rechecking its files."""
    try:
        _regular_file(paths['analyze_result_path'], 'ANALYSIS_REQUIRED')
        analyzed = read_json(paths['analyze_result_path'])
    except (VmError, OSError, ValueError) as error:
        raise VmError('ANALYSIS_REQUIRED', 'Run analyze before AI planning') from error
    if (not isinstance(analyzed, dict) or analyzed.get('status') != 'ready'
            or analyzed.get('input_sha256') != context['input_sha256']):
        raise VmError('ANALYSIS_REQUIRED', 'Analyze result is not ready for this input')
    alignment_path = paths['run_dir'] / 'alignment.words.json'
    _regular_file(alignment_path, 'WORD_ALIGNMENT_UNRESOLVED')
    if file_sha256(alignment_path) != analyzed.get('alignment_sha256'):
        raise VmError('PLAN_CHANGED', 'Word alignment differs from the analyze result')
    try:
        load_alignment(alignment_path, descriptor)
    except SlicingError as error:
        raise VmError('WORD_ALIGNMENT_UNRESOLVED', 'Word timing does not match the selected source') from error
    _regular_file(paths['intent_context_path'], 'PLAN_CHANGED')
    if (read_json(paths['intent_context_path']) != _intent_context_data(input_data, paths, alignment_path)
            or file_sha256(paths['intent_context_path']) != analyzed.get('intent_context_sha256')):
        raise VmError('PLAN_CHANGED', 'Intent context differs from the analyze result')
    return alignment_path


def _intent_engine(input_data, context, paths, descriptor):
    from .intent import IntentError, compile_intent, validate

    alignment_path = _analysis(input_data, context, paths, descriptor)
    intent_path = paths['intent_path']
    if not intent_path.exists():
        raise VmError('INTENT_MISSING', 'No edit intent was written for this run')
    _regular_file(intent_path, 'INTENT_INVALID')
    edit_context = _edit_context(paths, descriptor)
    checked = validate(intent_path, edit_context)
    if checked['problems']:
        raise VmError('INTENT_INVALID', 'Edit intent failed validation', blockers=[
            {'code': row['code'], 'rule': row['rule'], 'message': row['message'], 'path': row['path']}
            for row in checked['problems']])
    intent = read_json(intent_path)
    if checked['status'] != 'proposed':
        raise VmError('INTENT_BLOCKED', 'Edit intent is blocked', blockers=intent.get('blockers', []))
    if len(intent['outputs']) != 1:
        raise VmError('INTENT_INVALID', 'A VM/PMO run renders exactly one output', blockers=[
            {'code': 'INTENT_INVALID', 'rule': 'VM', 'path': '$.outputs',
             'message': 'A VM/PMO run renders exactly one output'}])
    with tempfile.TemporaryDirectory(prefix='.vm-intent-', dir=paths['run_dir']) as temporary:
        work = Path(temporary)
        try:
            compile_intent(intent_path, edit_context, work / 'job.json', work / 'receipt.json',
                           work / 'plan.json')
        except IntentError as error:
            raise VmError('INTENT_INVALID', 'Edit intent failed compilation', blockers=[
                {'code': row['code'], 'rule': row['rule'], 'message': row['message'],
                 'path': row['path']} for row in error.problems]) from error
        engine = read_json(work / 'plan.json')
    if file_sha256(intent_path) != checked['intent_sha256']:
        raise VmError('PLAN_CHANGED', 'Edit intent changed during planning')
    return engine, intent, checked['intent_sha256'], alignment_path


def _edit_signature(output):
    return {
        'duration_us': output['duration_us'],
        'segments': [(row['source_id'], row['source_start_us'], row['duration_us'])
                     for row in output['segments']],
        'effects': sorted((row['kind'], row.get('source_start_us'), row.get('source_end_us'),
                           row.get('frame_time_us')) for row in output.get('effects', [])),
    }


def _covered_words(alignment, start_us, end_us):
    """Words the fixed patterns and the AI would each cut through, ignoring silence between them."""
    return [row for row in alignment['words']
            if row['start_us'] < end_us and row['end_us'] > start_us]


def _same_cut(left, right, alignment):
    """The two readings cut the same material. A boundary may sit anywhere in the silence around the
    words it covers: the fixed patterns place a written time on the clock, while the AI places it on
    word timing, so one end can cover a few hundred milliseconds of the same pause. A word starting
    between the two boundaries means they are different cuts, however small the difference."""
    if left['duration_us'] == right['duration_us']:
        return True
    if not alignment or len(left['segments']) != 1 or len(right['segments']) != 1:
        return False
    first, second = left['segments'][0], right['segments'][0]
    if first['source_id'] != second['source_id'] or first['source_start_us'] != second['source_start_us']:
        return False
    left_end = first['source_start_us'] + first['duration_us']
    right_end = second['source_start_us'] + second['duration_us']
    lower, upper = min(left_end, right_end), max(left_end, right_end)
    return not any(lower <= row['start_us'] < upper
                   for row in alignment.get('words', [])
                   if isinstance(row.get('start_us'), int))


def _cross_check_rule(input_data, paths, descriptor, alignment_path, output):
    """When the fixed-pattern parser also understands the feedback, both must cut the same material."""
    if len(input_data['instructions']) != 1:
        # The fixed patterns read one instruction; several (e.g. a rebased review) cannot be cross-checked.
        return 'not_applicable'
    try:
        clips = _parse_review_clips(input_data, descriptor)
        rule = _review_engine_plan(input_data, paths['source_path'], descriptor, clips,
                                   paths['run_dir'],
                                   alignment_path if _requires_alignment(clips) else None)
    except VmError:
        return 'not_applicable'
    rule_output, output = rule['outputs'][0], output
    alignment = load_alignment(alignment_path, descriptor) if alignment_path else None
    if (_edit_signature(rule_output) != _edit_signature(output)
            and not (_same_cut(rule_output, output, alignment)
                     and _edit_signature(rule_output)['effects'] == _edit_signature(output)['effects'])):
        raise VmError('INTENT_RULE_MISMATCH', 'Intent and fixed-pattern edits differ', blockers=[{
            'code': 'INTENT_RULE_MISMATCH',
            'message': 'The fixed-pattern parser also understands this instruction but compiles '
                       'to different segments or effects; review both readings.',
        }])
    return 'agreed'


def _instruction_segments(input_data, intent, output):
    owner = {}
    for clip in intent['outputs'][0]['clips']:
        owner[clip['intent_id']] = clip['intent_id']
        for operation in clip.get('operations', []):
            owner[operation['intent_id']] = clip['intent_id']
    results = []
    for item in input_data['instructions']:
        clips = {owner[intent_id] for row in intent['coverage']
                 if row['span']['instruction_id'] == item['instruction_id']
                 and row['disposition'] == 'operation'
                 for intent_id in row.get('intent_ids', []) if intent_id in owner}
        results.append({
            'instruction_id': item['instruction_id'], 'source_field_id': item['source_field_id'],
            'raw_text': item['raw_text'], 'status': 'resolved',
            'segments': [row['id'] for row in output['segments'] if row['clip_id'] in clips],
        })
    return results


def _build_plan(input_data, context, paths):
    _validate_input(input_data)
    descriptor = _source_descriptor(input_data, paths['source_path'])
    row = input_data['instructions'][0]
    version = input_data['schema_version']
    mode = input_data.get('planning_mode', 'rule')
    intent = intent_sha256 = cross_check = None
    output_id = 'review'
    if mode == 'ai_intent':
        engine, intent, intent_sha256, alignment = _intent_engine(input_data, context, paths, descriptor)
        output = engine['outputs'][0]
        output_id = output['id']
        cross_check = _cross_check_rule(input_data, paths, descriptor, alignment, output)
        plan_segments = [{
            'segment_id': segment['id'], 'source_id': segment['source_id'],
            'source_start_us': segment['source_start_us'],
            'duration_us': segment['duration_us'], 'target_start_us': segment['target_start_us'],
        } for segment in output['segments']]
        operations = _operation_rows(output, with_intent=True)
        expected_duration_ms = round(output['duration_us'] / 1000)
        alignment_sha256 = file_sha256(alignment)
    elif version == 1:
        segments = _segments(input_data, descriptor)
        engine = _engine_plan(input_data, paths['source_path'], descriptor, segments,
                              paths['run_dir'])
        plan_segments = segments
        operations = []
        expected_duration_ms = sum(segment['end_ms'] - segment['start_ms'] for segment in segments)
        instruction_segments = [segment['segment_id'] for segment in segments]
        alignment_sha256 = None
    else:
        clips = (_manual_revision_clips(input_data) if 'manual_revision' in input_data
                 else _parse_review_clips(input_data, descriptor))
        alignment = (_alignment_path(input_data, paths['source_path'], descriptor, paths['run_dir'])
                     if _requires_alignment(clips) else None)
        engine = _review_engine_plan(input_data, paths['source_path'], descriptor, clips,
                                     paths['run_dir'], alignment)
        output = engine['outputs'][0]
        plan_segments = [{
            'segment_id': segment['id'], 'source_id': segment['source_id'],
            'source_start_us': segment['source_start_us'],
            'duration_us': segment['duration_us'], 'target_start_us': segment['target_start_us'],
        } for segment in output['segments']]
        operations = _operation_rows(output)
        expected_duration_ms = round(output['duration_us'] / 1000)
        instruction_segments = [segment['segment_id'] for segment in plan_segments]
        alignment_sha256 = file_sha256(alignment) if alignment is not None else None
    if mode == 'ai_intent':
        instruction_results = _instruction_segments(input_data, intent, output)
    else:
        instruction_results = [{
            'instruction_id': row['instruction_id'], 'source_field_id': row['source_field_id'],
            'raw_text': row['raw_text'], 'status': 'resolved',
            'segments': instruction_segments,
        }]
    plan = {
        'protocol': PROTOCOL, 'schema_version': input_data['schema_version'], **_identity(input_data),
        'source': input_data['source'],
        'input_sha256': context['input_sha256'],
        'source_sha256': descriptor['sha256'],
        'instruction_results': instruction_results,
        'segments': plan_segments, 'operations': operations,
        'expected_duration_ms': expected_duration_ms,
        'engine_plan_sha256': engine['plan_sha256'],
    }
    if version >= 2:
        plan['alignment_sha256'] = alignment_sha256
    if version == 3:
        plan.update({
            'planning_mode': mode, 'output_id': output_id,
            'ai_session': input_data.get('ai_session'),
            'intent_sha256': intent_sha256,
            'evidence_manifest_sha256': (intent['bindings']['evidence_manifest_sha256']
                                         if intent else None),
            'coverage': intent['coverage'] if intent else None,
            'rule_cross_check': cross_check,
        })
    return plan, engine


def plan_command(input_path, context_path):
    _regular_file(input_path, 'INVALID_CONTEXT')
    _regular_file(context_path, 'INVALID_CONTEXT')
    input_data = read_json(input_path)
    context = read_json(context_path)
    paths = _validate_context(context, input_path, context_path)
    if context['schema_version'] != input_data.get('schema_version'):
        raise VmError('INVALID_CONTEXT', 'Context and input protocol versions differ')
    base = _receipt_base(input_data, context)
    with _plan_lock(paths['run_dir']):
        existing = _existing_plan(input_data, paths, base)
        if existing is not None:
            return existing
        try:
            plan, _ = _build_plan(input_data, context, paths)
        except (VmError, SlicingError, OSError, ValueError) as error:
            receipt = _blocked_receipt(base, input_data, paths, error)
            write_new_json(paths['plan_result_path'], receipt)
            return receipt
        write_new_json(paths['plan_path'], plan)
        receipt = _ready_receipt(base, plan, file_sha256(paths['plan_path']))
        write_new_json(paths['plan_result_path'], receipt)
        return receipt


def render_command(input_path, context_path, plan_path):
    _regular_file(input_path, 'INVALID_CONTEXT')
    _regular_file(context_path, 'INVALID_CONTEXT')
    input_data = read_json(input_path)
    context = read_json(context_path)
    paths = _validate_context(context, input_path, context_path)
    _validate_input(input_data)
    if context['schema_version'] != input_data['schema_version']:
        raise VmError('INVALID_CONTEXT', 'Context and input protocol versions differ')
    base = _receipt_base(input_data, context)
    if plan_path != paths['plan_path']:
        raise VmError('PLAN_CHANGED', '--plan must match context.plan_path')
    plan_hash = None
    try:
        if paths['result_path'].exists() or paths['output_path'].exists():
            raise VmError('OUTPUT_EXISTS', 'Result or MP4 already exists')
        _regular_file(paths['plan_result_path'], 'PLAN_CHANGED')
        _regular_file(plan_path, 'PLAN_CHANGED')
        receipt = read_json(paths['plan_result_path'])
        saved_plan = read_json(plan_path)
        plan_hash = file_sha256(plan_path)
        if receipt.get('status') != 'ready' or receipt.get('plan_sha256') != plan_hash:
            raise VmError('PLAN_CHANGED', 'Plan bytes differ from the ready receipt')
        expected_plan, engine = _build_plan(input_data, context, paths)
        if saved_plan != expected_plan or receipt != _ready_receipt(base, expected_plan, plan_hash):
            raise VmError('PLAN_CHANGED', 'Plan or ready receipt differs from current inputs')
        media = render_output(engine, engine['outputs'][0]['id'], paths['output_path'], width=1920,
                              height=1080, fps=25, input_demuxer='mov')
        artifact = {
            'kind': 'review_mp4', 'path': str(paths['output_path']),
            'sha256': media['sha256'], 'bytes': paths['output_path'].stat().st_size,
            'duration_ms': round(media['duration_us'] / 1000),
            'width': media['width'], 'height': media['height'], 'fps': media['fps'],
            'has_audio': media['has_audio'],
            'full_decode_passed': media['full_decode_passed'],
        }
        result = {**base, 'status': 'rendered', 'plan_sha256': plan_hash,
                  'instruction_results': expected_plan['instruction_results'],
                  'artifacts': [artifact], 'error': None}
    except (VmError, SlicingError, OSError, ValueError, KeyError, TypeError) as error:
        result = {**base, 'status': 'blocked' if isinstance(error, VmError) else 'failed',
                  'plan_sha256': plan_hash,
                  'instruction_results': _instruction_results(input_data),
                  'artifacts': [], 'error': _error(error)}
    write_new_json(paths['result_path'], result)
    return result


def _v3_request(input_path, context_path, *, ai_only=True):
    _regular_file(input_path, 'INVALID_CONTEXT')
    _regular_file(context_path, 'INVALID_CONTEXT')
    input_data = read_json(input_path)
    context = read_json(context_path)
    paths = _validate_context(context, input_path, context_path)
    if context['schema_version'] != 3 or input_data.get('schema_version') != 3:
        raise VmError('INVALID_CONTEXT', 'analyze and qa require schema v3 input and context')
    _validate_input(input_data)
    if ai_only and input_data['planning_mode'] != 'ai_intent':
        raise VmError('INVALID_INPUT', 'This step is only used with ai_intent planning')
    return input_data, context, paths, _receipt_base(input_data, context)


def _existing_analysis(input_data, context, paths, base):
    target = paths['analyze_result_path']
    if not target.exists():
        return None
    _regular_file(target, 'PLAN_CHANGED')
    saved = read_json(target)
    if not isinstance(saved, dict) or {key: saved.get(key) for key in base} != base:
        raise VmError('OUTPUT_EXISTS', 'Existing analyze result belongs to a different request')
    if saved.get('status') == 'ready':
        descriptor = _source_descriptor(input_data, paths['source_path'])
        _analysis(input_data, context, paths, descriptor)
    elif saved.get('status') != 'blocked':
        raise VmError('PLAN_CHANGED', 'Existing analyze result has an unsupported status')
    return saved


def analyze_command(input_path, context_path):
    """Prepare source-bound ASR, the intent context and the first evidence for AI planning."""
    from .evidence import load_manifest, manifest_path, transcript_window

    input_data, context, paths, base = _v3_request(input_path, context_path)
    with _plan_lock(paths['run_dir']):
        existing = _existing_analysis(input_data, context, paths, base)
        if existing is not None:
            return existing
        try:
            descriptor = _source_descriptor(input_data, paths['source_path'])
            alignment = _alignment_path(input_data, paths['source_path'], descriptor, paths['run_dir'])
            data = _intent_context_data(input_data, paths, alignment)
            if paths['intent_context_path'].exists():
                _regular_file(paths['intent_context_path'], 'PLAN_CHANGED')
                if read_json(paths['intent_context_path']) != data:
                    raise VmError('PLAN_CHANGED', 'Existing intent context differs from this input')
            else:
                write_new_json(paths['intent_context_path'], data)
            paths['evidence_dir'].mkdir(exist_ok=True)
            edit_context = _edit_context(paths, descriptor)
            source_id = input_data['source']['source_id']
            entries = {row['evidence_id'] for row in load_manifest(edit_context)['entries']}
            if 'transcript-full' not in entries:
                transcript_window(edit_context, source_id, 'transcript-full', 0,
                                  descriptor['duration_us'])
            result = {
                **base, 'status': 'ready', 'planning_mode': 'ai_intent',
                'alignment_sha256': file_sha256(alignment),
                'intent_context_sha256': file_sha256(paths['intent_context_path']),
                'evidence_manifest_sha256': file_sha256(manifest_path(edit_context)),
                'evidence_ids': ['transcript-full'], 'error': None,
            }
        except (VmError, SlicingError, OSError, ValueError) as error:
            result = {**base, 'status': 'blocked', 'planning_mode': 'ai_intent',
                      'alignment_sha256': None, 'intent_context_sha256': None,
                      'evidence_manifest_sha256': None, 'evidence_ids': [],
                      'error': _error(error)}
        write_new_json(paths['analyze_result_path'], result)
        return result


def qa_command(input_path, context_path, *, reverse_asr=False):
    """Run content QA on the rendered MP4 and publish its outcome as ``qa-result.json``.

    The receipt is written once for checked, blocked and failed outcomes alike; a repeated
    call returns the saved receipt instead of running QA again.
    """
    input_data, context, paths, base = _v3_request(input_path, context_path)
    receipt_path = paths['run_dir'] / 'qa-result.json'
    with _plan_lock(paths['run_dir']):
        if receipt_path.exists():
            _regular_file(receipt_path, 'PLAN_CHANGED')
            saved = read_json(receipt_path)
            if not isinstance(saved, dict) or {key: saved.get(key) for key in base} != base:
                raise VmError('OUTPUT_EXISTS', 'Existing QA result belongs to a different request')
            return saved
        result = _qa_outcome(input_data, context, paths, base, reverse_asr)
        write_new_json(receipt_path, result)
        return result


def _qa_outcome(input_data, context, paths, base, reverse_asr):
    from .qa import run_qa

    plan_hash = None
    try:
        if paths['qa_path'].exists():
            raise VmError('OUTPUT_EXISTS', 'QA report already exists')
        try:
            _regular_file(paths['result_path'], 'RENDER_REQUIRED')
            rendered = read_json(paths['result_path'])
        except (OSError, ValueError) as error:
            raise VmError('RENDER_REQUIRED', 'Render result is unavailable') from error
        if not isinstance(rendered, dict) or rendered.get('status') != 'rendered' or (
            {key: rendered.get(key) for key in base} != base
        ):
            raise VmError('RENDER_REQUIRED', 'QA requires a rendered result for this run')
        _regular_file(paths['plan_path'], 'PLAN_CHANGED')
        plan_hash = file_sha256(paths['plan_path'])
        artifact = rendered['artifacts'][0]
        _regular_file(paths['output_path'], 'PLAN_CHANGED')
        if (rendered.get('plan_sha256') != plan_hash or artifact['path'] != str(paths['output_path'])
                or file_sha256(paths['output_path']) != artifact['sha256']):
            raise VmError('PLAN_CHANGED', 'Rendered MP4 or plan differs from the render result')
        expected_plan, engine = _build_plan(input_data, context, paths)
        if read_json(paths['plan_path']) != expected_plan:
            raise VmError('PLAN_CHANGED', 'Plan differs from current inputs')
        descriptor = _source_descriptor(input_data, paths['source_path'])
        video_asr = None
        if reverse_asr:
            video_asr = paths['run_dir'] / 'output.words.json'
            if not video_asr.exists():
                try:
                    transcribe_source({'path': str(paths['output_path']), 'sha256': artifact['sha256']},
                                      video_asr)
                except Exception as error:
                    raise VmError('WORD_ALIGNMENT_UNRESOLVED', 'Output transcription is unavailable') from error
            _regular_file(video_asr, 'WORD_ALIGNMENT_UNRESOLVED')
        with tempfile.TemporaryDirectory(prefix='.vm-qa-', dir=paths['run_dir']) as temporary:
            engine_path = write_json(Path(temporary) / 'plan.json', engine)
            report = run_qa(_edit_context(paths, descriptor), paths['intent_path'], engine_path,
                            engine['outputs'][0]['id'], paths['output_path'], video_asr,
                            paths['qa_path'], plan_binding_path=paths['plan_path'])
        return {**base, 'status': 'checked', 'plan_sha256': plan_hash,
                'qa_sha256': file_sha256(paths['qa_path']), 'overall': report['overall'],
                'counts': report['counts'], 'reverse_asr': reverse_asr, 'error': None}
    except (VmError, SlicingError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
        return {**base, 'status': 'blocked' if isinstance(error, VmError) else 'failed',
                'plan_sha256': plan_hash, 'qa_sha256': None, 'overall': None,
                'counts': None, 'reverse_asr': reverse_asr, 'error': _error(error)}


def qa_review_command(input_path, context_path):
    """Merge the AI picture review at ``qa_review_path`` into ``qa.json`` and publish a receipt.

    ``qa-result.json`` keeps proving what QA produced; ``qa-review-result.json`` binds the reviewed
    report, the review file and the report as it was before the merge, so both states stay
    verifiable. Written once; a repeated call returns the saved receipt.
    """
    from .qa import apply_review

    input_data, context, paths, base = _v3_request(input_path, context_path)
    run_dir = paths['run_dir']
    receipt_path = run_dir / 'qa-review-result.json'
    before_path = run_dir / 'qa.before-review.json'
    with _plan_lock(run_dir):
        if receipt_path.exists():
            _regular_file(receipt_path, 'PLAN_CHANGED')
            saved = read_json(receipt_path)
            if not isinstance(saved, dict) or {key: saved.get(key) for key in base} != base:
                raise VmError('OUTPUT_EXISTS', 'Existing QA review result belongs to a different request')
            return saved
        result = _qa_review_outcome(paths, base, before_path, apply_review)
        write_new_json(receipt_path, result)
        return result


def _qa_review_outcome(paths, base, before_path, apply_review):
    qa_result = None
    try:
        try:
            _regular_file(paths['run_dir'] / 'qa-result.json', 'RENDER_REQUIRED')
            qa_result = read_json(paths['run_dir'] / 'qa-result.json')
        except (OSError, ValueError) as error:
            raise VmError('RENDER_REQUIRED', 'QA result is unavailable') from error
        if (not isinstance(qa_result, dict) or qa_result.get('status') != 'checked'
                or {key: qa_result.get(key) for key in base} != base):
            raise VmError('RENDER_REQUIRED', 'AI review requires a checked QA result for this run')
        _regular_file(paths['qa_path'], 'PLAN_CHANGED')
        if file_sha256(paths['qa_path']) != qa_result['qa_sha256']:
            raise VmError('PLAN_CHANGED', 'QA report differs from the QA result')
        if not paths['qa_review_path'].exists():
            raise VmError('REVIEW_MISSING', 'No AI picture review was written for this run')
        _regular_file(paths['qa_review_path'], 'REVIEW_INVALID')
        review_sha256 = file_sha256(paths['qa_review_path'])
        # Keep the report exactly as QA wrote it, then merge in place.
        before = paths['qa_path'].read_bytes()
        if before_path.exists():
            raise VmError('OUTPUT_EXISTS', 'A pre-review QA report already exists')
        before_path.write_bytes(before)
        try:
            merged = apply_review(paths['qa_path'], paths['qa_path'].with_name(
                paths['qa_path'].stem + '-frames'), paths['qa_review_path'])
        except SlicingError as error:
            paths['qa_path'].write_bytes(before)
            before_path.unlink()
            raise VmError('REVIEW_INVALID', 'AI picture review does not apply') from error
        if file_sha256(paths['qa_review_path']) != review_sha256:
            raise VmError('PLAN_CHANGED', 'AI review changed during the merge')
        return {**base, 'status': 'reviewed', 'plan_sha256': qa_result['plan_sha256'],
                'qa_sha256_before': qa_result['qa_sha256'],
                'qa_sha256': file_sha256(paths['qa_path']),
                'review_sha256': review_sha256,
                'overall': merged['overall'], 'counts': merged['counts'], 'error': None}
    except (VmError, SlicingError, OSError, ValueError, KeyError, TypeError) as error:
        return {**base, 'status': 'blocked' if isinstance(error, VmError) else 'failed',
                'plan_sha256': qa_result.get('plan_sha256') if isinstance(qa_result, dict) else None,
                'qa_sha256_before': qa_result.get('qa_sha256') if isinstance(qa_result, dict) else None,
                'qa_sha256': None, 'review_sha256': None, 'overall': None, 'counts': None,
                'error': _error(error)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m auto_cut_slicing.vm_pmo')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('analyze', 'plan', 'render', 'qa', 'qa-review'):
        command = commands.add_parser(name)
        command.add_argument('--input', required=True, type=Path)
        command.add_argument('--context', required=True, type=Path)
        if name == 'render':
            command.add_argument('--plan', required=True, type=Path)
        if name == 'qa':
            command.add_argument('--reverse-asr', action='store_true',
                                 help='Transcribe the rendered MP4 for spoken-content checks')
    args = parser.parse_args(argv)
    try:
        if not args.input.is_absolute() or not args.context.is_absolute():
            raise VmError('INVALID_CONTEXT', 'Input and context arguments must be absolute paths')
        if args.command == 'analyze':
            result = analyze_command(args.input, args.context)
        elif args.command == 'qa':
            result = qa_command(args.input, args.context, reverse_asr=args.reverse_asr)
        elif args.command == 'qa-review':
            result = qa_review_command(args.input, args.context)
        elif args.command == 'plan':
            result = plan_command(args.input, args.context)
        else:
            if not args.plan.is_absolute():
                raise VmError('INVALID_CONTEXT', 'Plan argument must be an absolute path')
            result = render_command(args.input, args.context, args.plan)
    except (VmError, SlicingError, OSError, ValueError, KeyError, TypeError) as error:
        result = {'protocol': PROTOCOL, 'schema_version': 1, 'status': 'failed',
                  'error': _error(error)}
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['status'] in ('ready', 'rendered', 'blocked', 'checked', 'reviewed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
