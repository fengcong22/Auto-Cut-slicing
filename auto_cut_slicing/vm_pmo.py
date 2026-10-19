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
from .errors import SlicingError
from .instructions import parse_instruction
from .io import file_sha256, read_json, write_json, write_new_json
from .media import describe_source
from .planner import compile_job
from .render import render_output
from .upstream import transcribe_source

PROTOCOL = 'vm_pmo_review_video'
HASH = re.compile(r'[0-9a-f]{64}\Z')


class VmError(SlicingError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
        'EXECUTION_FAILED': 'The local media operation failed.',
    }
    return {'code': code, 'message': messages.get(code, messages['EXECUTION_FAILED'])}


def _validate_input(input_data):
    _object(input_data, ('protocol', 'schema_version', 'package', 'binding', 'record',
                         'review_round', 'source', 'instructions', 'output_profile',
                         'delivery_target'), 'input')
    version = input_data['schema_version']
    if input_data['protocol'] != PROTOCOL or type(version) is not int or version not in (1, 2):
        raise VmError('INVALID_INPUT', 'Unsupported VM/PMO protocol or schema version')
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
    if version == 2 and binding['stage_id'] not in ('initial', 'review'):
        raise VmError('UNSUPPORTED_ROUND', 'Schema v2 supports only initial and review stages')
    record = _object(input_data['record'], ('base_token', 'table_id', 'record_id'), 'record')
    for key, value in record.items():
        _id(value, f'record.{key}')
    review = _object(input_data['review_round'], ('round_id', 'input_snapshot_id', 'parent'), 'review_round')
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
        if parent['run_id'] == binding['run_id'] or any(
            parent[key] != source[source_key]
            for key, source_key in (('sha256', 'sha256'), ('bytes', 'bytes'),
                                    ('duration_ms', 'duration_ms'))
        ):
            raise VmError('UNSUPPORTED_ROUND', 'Review source does not match its frozen parent')
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
    instructions = input_data['instructions']
    if not isinstance(instructions, list) or not instructions:
        raise VmError('INVALID_INPUT', 'instructions must be a nonempty list')
    seen = set()
    for item in instructions:
        allowed_instruction_fields = ['instruction_id', 'source_field_id', 'raw_text',
                                      'timeline_basis', 'applicability']
        if version == 2:
            allowed_instruction_fields.extend(['visual_evidence', 'must_keep'])
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
        if item['timeline_basis'] != expected_timeline or item['applicability'] != 'current_round':
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


def _validate_context(context, input_path, context_path):
    _object(context, ('protocol', 'schema_version', 'input_sha256', 'run_dir', 'source_path',
                      'plan_path', 'plan_result_path', 'result_path', 'output_path'), 'context')
    if context['protocol'] != PROTOCOL or type(context['schema_version']) is not int or context['schema_version'] not in (1, 2):
        raise VmError('INVALID_CONTEXT', 'Unsupported VM/PMO context version')
    if _hash(context['input_sha256'], 'context.input_sha256') != file_sha256(input_path):
        raise VmError('INPUT_CHANGED', 'Input bytes differ from the trusted context')
    paths = {key: _path(context[key], f'context.{key}') for key in
             ('run_dir', 'source_path', 'plan_path', 'plan_result_path', 'result_path', 'output_path')}
    run_dir = paths['run_dir']
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise VmError('INVALID_CONTEXT', 'run_dir must be an existing regular directory')
    if input_path != run_dir / 'input.json' or context_path != run_dir / 'execution-context.json':
        raise VmError('INVALID_CONTEXT', 'Input and context must use fixed names inside run_dir')
    for key in ('plan_path', 'plan_result_path', 'result_path', 'output_path'):
        if not paths[key].is_relative_to(run_dir) or paths[key] == run_dir:
            raise VmError('INVALID_CONTEXT', f'{key} must be inside run_dir')
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
        expected_receipt = {
            **base, 'status': 'ready', 'plan_sha256': file_sha256(paths['plan_path']),
            'instruction_results': saved_plan.get('instruction_results'), 'error': None,
        }
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
        except SlicingError as error:
            code = 'WORD_ALIGNMENT_UNRESOLVED' if alignment_path is not None else 'UNSUPPORTED_INSTRUCTION'
            message = ('Required source-bound word timing is ambiguous or invalid.'
                       if alignment_path is not None else
                       'The instruction needs supported, source-bound evidence.')
            raise VmError(code, message) from error


def _operation_rows(output):
    rows = []
    for cue in output.get('start_cues', []):
        rows.append({
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
        })
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


def _build_plan(input_data, context, paths):
    _validate_input(input_data)
    descriptor = _source_descriptor(input_data, paths['source_path'])
    row = input_data['instructions'][0]
    if input_data['schema_version'] == 1:
        segments = _segments(input_data, descriptor)
        engine = _engine_plan(input_data, paths['source_path'], descriptor, segments,
                              paths['run_dir'])
        plan_segments = segments
        operations = []
        expected_duration_ms = sum(segment['end_ms'] - segment['start_ms'] for segment in segments)
        instruction_segments = [segment['segment_id'] for segment in segments]
        alignment_sha256 = None
    else:
        clips = _parse_review_clips(input_data, descriptor)
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
    plan = {
        'protocol': PROTOCOL, 'schema_version': input_data['schema_version'], **_identity(input_data),
        'source': input_data['source'],
        'input_sha256': context['input_sha256'],
        'source_sha256': descriptor['sha256'],
        'instruction_results': [{
            'instruction_id': row['instruction_id'], 'source_field_id': row['source_field_id'],
            'raw_text': row['raw_text'], 'status': 'resolved',
            'segments': instruction_segments,
        }],
        'segments': plan_segments, 'operations': operations,
        'expected_duration_ms': expected_duration_ms,
        'engine_plan_sha256': engine['plan_sha256'],
    }
    if input_data['schema_version'] == 2:
        plan['alignment_sha256'] = alignment_sha256
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
            receipt = {**base, 'status': 'blocked', 'plan_sha256': None,
                       'instruction_results': _instruction_results(input_data),
                       'error': _error(error)}
            write_new_json(paths['plan_result_path'], receipt)
            return receipt
        write_new_json(paths['plan_path'], plan)
        receipt = {**base, 'status': 'ready', 'plan_sha256': file_sha256(paths['plan_path']),
                   'instruction_results': plan['instruction_results'], 'error': None}
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
        if saved_plan != expected_plan or receipt != {
            **base, 'status': 'ready', 'plan_sha256': plan_hash,
            'instruction_results': expected_plan['instruction_results'], 'error': None,
        }:
            raise VmError('PLAN_CHANGED', 'Plan or ready receipt differs from current inputs')
        media = render_output(engine, 'review', paths['output_path'], width=1920,
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m auto_cut_slicing.vm_pmo')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'render'):
        command = commands.add_parser(name)
        command.add_argument('--input', required=True, type=Path)
        command.add_argument('--context', required=True, type=Path)
        if name == 'render':
            command.add_argument('--plan', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.input.is_absolute() or not args.context.is_absolute():
            raise VmError('INVALID_CONTEXT', 'Input and context arguments must be absolute paths')
        if args.command == 'plan':
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
    return 0 if result['status'] in ('ready', 'rendered', 'blocked') else 1


if __name__ == '__main__':
    raise SystemExit(main())
