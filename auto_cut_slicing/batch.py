"""Versioned local Taskboard contract with independent MP4 output receipts."""

import copy
import os
import tempfile
from collections import Counter
from pathlib import Path

from .errors import SlicingError
from .io import file_sha256, json_sha256, read_json, write_json, write_new_json


class ContractError(SlicingError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def error_receipt(error):
    message = str(error)
    code = getattr(error, 'code', None)
    if code is None:
        lower = message.lower()
        if 'asr' in lower or 'alignment' in lower:
            code = 'ASR_REQUIRED' if 'requires' in lower else 'ASR_INVALID'
        elif 'visual' in lower or 'picture' in lower or 'frame evidence' in lower:
            code = 'VISUAL_EVIDENCE_REQUIRED'
        elif 'source bytes changed' in lower or 'source metadata changed' in lower:
            code = 'SOURCE_CHANGED'
        elif 'overwrite' in lower or 'already exists' in lower:
            code = 'OUTPUT_EXISTS'
        elif 'range' in lower or 'timestamp' in lower or 'time' in lower:
            code = 'INVALID_RANGE'
        elif 'ffmpeg' in lower or 'render' in lower:
            code = 'RENDER_FAILED'
        else:
            code = 'INVALID_OUTPUT'
    return {'code': code, 'message': message, 'retryable': code in {'RENDER_FAILED', 'TIMEOUT'}}


def _keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ContractError('INVALID_REQUEST', f'{label} must be an object')
    extra = set(value) - allowed
    if extra:
        raise ContractError('INVALID_REQUEST', f'Unsupported {label} fields: {sorted(extra)}')


def _identity(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ContractError('INVALID_REQUEST', f'{label} must be a nonempty stable ID')
    return value


def _path(value, base, label):
    if not isinstance(value, str) or not value.strip():
        raise ContractError('INVALID_REQUEST', f'{label} must be a nonempty local path')
    return (base / Path(value).expanduser()).resolve()


def _boundary_policy(value, label, *, code='INVALID_REQUEST'):
    if not isinstance(value, str) or value not in ('sentence', 'exact'):
        raise ContractError(code, f'{label} must be sentence or exact')
    return value


def _needs_alignment(clips, duration_us):
    from .alignment import parse_time_us

    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if clip.get('start_cue'):
            return True
        if clip.get('boundary_policy') == 'sentence' and (
            parse_time_us(clip.get('start'), field='clip start') != 0
            or parse_time_us(clip.get('end'), field='clip end') != duration_us
        ):
            return True
        for operation in clip.get('deletions', []) + clip.get('edits', []):
            if isinstance(operation, dict) and (
                operation.get('kind') in {'word', 'mute_word'} or operation.get('must_keep')
            ):
                return True
    return False


def _compile_output(row, source, descriptor, base, temporary, *, default_boundary_policy='exact'):
    from .instructions import parse_instruction
    from .planner import compile_job

    _keys(row, {'id', 'name', 'instruction', 'source_text', 'clips', 'output_path', 'visual_evidence'}, 'output')
    if ('instruction' in row) == ('clips' in row):
        raise ContractError('INVALID_OUTPUT', 'Declare exactly one of instruction or clips')
    evidence = row.get('visual_evidence')
    if evidence is not None:
        _keys(evidence, {'source_sha256', 'frame_time', 'end'}, 'visual_evidence')
        if evidence.get('source_sha256') != descriptor['sha256']:
            raise ContractError('VISUAL_EVIDENCE_INVALID', 'Visual evidence does not match source SHA-256')
    if 'instruction' in row:
        clips = parse_instruction(
            row['instruction'], source['id'], descriptor['duration_us'], visual_evidence=evidence
        )
    else:
        clips = copy.deepcopy(row['clips'])
    if not isinstance(clips, list) or not clips:
        raise ContractError('INVALID_OUTPUT', 'Output requires nonempty clips')
    for clip in clips:
        if not isinstance(clip, dict):
            raise ContractError('INVALID_OUTPUT', 'Each clip must be an object')
        clip.setdefault('source', source['id'])
        if 'boundary_policy' not in clip and default_boundary_policy == 'sentence':
            clip['boundary_policy'] = 'sentence'
    source_row = {'id': source['id'], 'path': str(_path(source['path'], base, 'source path'))}
    if _needs_alignment(clips, descriptor['duration_us']):
        if not source.get('asr'):
            raise ContractError(
                'ASR_REQUIRED', 'Sentence boundaries, speech cues and word edits require source-bound ASR'
            )
        source_row['asr'] = str(_path(source['asr'], base, 'ASR path'))
    output = {'id': row['id'], 'name': row['id'], 'clips': clips}
    job = {'schema_version': 1, 'name': 'Taskboard MP4', 'sources': [source_row], 'outputs': [output]}
    path = write_json(temporary / 'job.json', job)
    return compile_job(path, describe=lambda path: descriptor)


def _verify_completed(saved, plan, destination):
    from .media import _run, describe_source, find_tool, verify_sources

    if saved.get('path') != str(destination) or saved.get('plan_sha256') != plan['plan_sha256']:
        raise ContractError('OUTPUT_CHANGED', 'Saved output mapping or edit evidence changed')
    if not destination.is_file() or destination.is_symlink() or file_sha256(destination) != saved.get('sha256'):
        raise ContractError('OUTPUT_CHANGED', 'Saved MP4 is missing, linked, or its bytes changed')
    actual = describe_source(destination)
    if actual['sha256'] != saved.get('sha256'):
        raise ContractError('OUTPUT_CHANGED', 'Saved MP4 changed during resume media inspection')
    expected = plan['outputs'][0]['duration_us']
    if (actual['width'], actual['height'], actual['fps']) != (1920, 1080, 25):
        raise ContractError('OUTPUT_CHANGED', 'Saved output delivery profile changed')
    if not actual['has_audio'] or abs(actual['duration_us'] - expected) > 80_000:
        raise ContractError('OUTPUT_CHANGED', 'Saved output duration/audio does not match its plan')
    _run([find_tool('ffmpeg'), '-v', 'error', '-nostdin', '-xerror', '-i', str(destination),
          '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], purpose='Resume full decode')
    if file_sha256(destination) != saved.get('sha256'):
        raise ContractError('OUTPUT_CHANGED', 'Saved MP4 changed during resume verification')
    verify_sources(plan)
    return {
        'output_id': plan['outputs'][0]['id'], 'path': str(destination),
        'plan_sha256': plan['plan_sha256'], 'sha256': actual['sha256'],
        'duration_us': actual['duration_us'], 'expected_duration_us': expected,
        'duration_tolerance_us': 80_000, 'width': actual['width'], 'height': actual['height'],
        'fps': actual['fps'], 'has_audio': True,
        'segment_count': len(plan['outputs'][0]['segments']),
        'full_decode_passed': True, 'spoken_final_acceptance': False,
    }


def _resume_rows(previous, request_id, request_hash, output_ids):
    if (
        not isinstance(previous, dict) or previous.get('contract_version') != 1
        or previous.get('request_sha256') != request_hash or previous.get('request_id') != request_id
    ):
        raise ContractError('RESUME_MISMATCH', 'Saved result does not belong to this request')
    rows = previous.get('outputs')
    if (
        not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        or len(rows) != len(output_ids)
    ):
        raise ContractError('RESUME_MISMATCH', 'Saved result output inventory is invalid')
    ids = [row.get('output_id') for row in rows]
    if any(not isinstance(value, str) for value in ids) or set(ids) != set(output_ids):
        raise ContractError('RESUME_MISMATCH', 'Saved result output identities are invalid')
    for row in rows:
        if row.get('status') not in {'pending', 'ready', 'complete', 'failed'}:
            raise ContractError('RESUME_MISMATCH', 'Saved result output status is invalid')
        if 'boundary_policy' in row:
            _boundary_policy(row['boundary_policy'], 'Saved result boundary_policy', code='RESUME_MISMATCH')
        completion = row.get('completion_receipt')
        if completion is not None and not isinstance(completion, dict):
            raise ContractError('RESUME_MISMATCH', 'Saved result completion receipt is invalid')
    digest = previous.get('source_sha256')
    if digest is None and any(row.get('completion_receipt') or row['status'] == 'complete' for row in rows):
        raise ContractError('RESUME_MISMATCH', 'Saved result completion has no source identity')
    return {row['output_id']: row for row in rows}


def run_request(request_path, result_path, *, resume=False):
    """Render each valid output; preserve failures and successes in an atomic result file."""
    from .media import describe_source
    from .render import render_output

    request_path = Path(request_path).resolve()
    result_path = Path(result_path).resolve()
    request = read_json(request_path)
    _keys(request, {'contract_version', 'request_id', 'source', 'outputs', 'boundary_policy'}, 'request')
    if type(request.get('contract_version')) is not int or request['contract_version'] != 1:
        raise ContractError('INVALID_REQUEST', 'contract_version must be 1')
    request_id = _identity(request.get('request_id'), 'request_id')
    default_boundary_policy = _boundary_policy(request.get('boundary_policy', 'sentence'), 'boundary_policy')
    source = request.get('source')
    _keys(source, {'id', 'path', 'asr', 'vmid'}, 'source')
    _identity(source.get('id'), 'source.id')
    base = request_path.parent
    source_path = _path(source.get('path'), base, 'source.path')
    protected = {source_path, request_path}
    if source.get('asr'):
        protected.add(_path(source['asr'], base, 'source.asr'))
    outputs = request.get('outputs')
    if not isinstance(outputs, list) or not outputs:
        raise ContractError('INVALID_REQUEST', 'outputs must be a nonempty list')
    ids = [_identity(row.get('id'), 'output.id') for row in outputs if isinstance(row, dict)]
    if len(ids) != len(outputs) or len(set(ids)) != len(ids):
        raise ContractError('INVALID_REQUEST', 'Outputs must be objects with unique stable IDs')
    for row in outputs:
        if isinstance(row.get('clips'), list):
            for clip in row['clips']:
                if isinstance(clip, dict) and 'boundary_policy' in clip:
                    _boundary_policy(clip['boundary_policy'], 'clip boundary_policy')
    destinations = []
    path_errors = {}
    for index, row in enumerate(outputs):
        try:
            destinations.append(_path(row.get('output_path'), base, 'output_path'))
        except (SlicingError, OSError, ValueError) as error:
            destinations.append(None)
            path_errors[index] = error
    if result_path in protected or result_path in destinations:
        raise ContractError('PATH_CONFLICT', 'Result path must be separate from inputs and MP4 outputs')
    request_hash = json_sha256(request)
    previous = None
    previous_rows = {}
    if os.path.lexists(result_path):
        if not resume:
            raise ContractError('RESULT_EXISTS', 'Result already exists; use --resume for the same request')
        previous = read_json(result_path)
        previous_rows = _resume_rows(previous, request_id, request_hash, ids)
    elif resume:
        raise ContractError('RESUME_MISMATCH', '--resume requires an existing result file')
    descriptor_error = None
    try:
        descriptor = describe_source(source_path)
    except (SlicingError, OSError, ValueError) as error:
        descriptor = None
        descriptor_error = ContractError('SOURCE_INVALID', str(error))
    if previous and descriptor and previous.get('source_sha256') is not None and previous['source_sha256'] != descriptor['sha256']:
        raise ContractError('SOURCE_CHANGED', 'Source changed since the saved request result')
    result = {
        'contract_version': 1, 'request_id': request_id, 'request_sha256': request_hash,
        'source_sha256': descriptor['sha256'] if descriptor else previous.get('source_sha256') if previous else None,
        'ok': False, 'status': 'in_progress', 'outputs': [],
    }
    for row, destination in zip(outputs, destinations):
        old = previous_rows.get(row['id'], {})
        saved = {
            'output_id': row['id'], 'name': row.get('name', row['id']), 'status': 'pending',
            'source_text': row.get('instruction', row.get('source_text', '')),
            'path': str(destination) if destination else None,
            # Persist the effective default before compilation, including failed first runs.
            # Receipts predating this field retain their historical exact-range semantics.
            'boundary_policy': old.get('boundary_policy', 'exact') if previous else default_boundary_policy,
        }
        if 'clips' in row:
            saved['clips'] = copy.deepcopy(row['clips'])
        completion = old.get('completion_receipt')
        if completion is None and old.get('status') == 'complete':
            completion = {key: value for key, value in old.items() if key != 'completion_receipt'}
        if completion is not None:
            saved['completion_receipt'] = copy.deepcopy(completion)
        result['outputs'].append(saved)
    if previous:
        write_json(result_path, result)
    else:
        write_new_json(result_path, result)
    counts = Counter(destinations)
    prepared = {}
    # Compile every candidate before the first MP4 publication; faults remain attributable.
    with tempfile.TemporaryDirectory(prefix='.taskboard-', dir=result_path.parent) as work:
        for index, (row, destination) in enumerate(zip(outputs, destinations)):
            saved = result['outputs'][index]
            try:
                if index in path_errors:
                    raise path_errors[index]
                if destination in protected or counts[destination] > 1:
                    raise ContractError('PATH_CONFLICT', 'MP4 path conflicts with an input or another output')
                if destination.suffix.lower() != '.mp4':
                    raise ContractError('INVALID_OUTPUT', 'Output destination must end in .mp4')
                if descriptor_error:
                    raise descriptor_error
                plan = _compile_output(
                    row, source, descriptor, base, Path(work),
                    default_boundary_policy=saved['boundary_policy'],
                )
                saved['plan_sha256'] = plan['plan_sha256']
                saved['edits'] = plan['outputs'][0]['edits']
                saved['effects'] = plan['outputs'][0].get('effects', [])
                saved['segments'] = plan['outputs'][0]['segments']
                saved['start_cues'] = plan['outputs'][0].get('start_cues', [])
                saved['boundary_adjustments'] = plan['outputs'][0].get('boundary_adjustments', [])
                saved['expected_duration_us'] = plan['outputs'][0]['duration_us']
                prepared[index] = plan
                saved['status'] = 'ready'
            except (SlicingError, OSError, ValueError, KeyError, TypeError) as error:
                saved.update(status='failed', error=error_receipt(error))
        write_json(result_path, result)
        for index, plan in prepared.items():
            row = outputs[index]
            saved = result['outputs'][index]
            destination = destinations[index]
            try:
                completion = saved.get('completion_receipt')
                if completion is not None:
                    completed = _verify_completed(completion, plan, destination)
                else:
                    completed = render_output(plan, row['id'], destination, width=1920, height=1080, fps=25)
                    completed['plan_sha256'] = plan['plan_sha256']
                saved['completion_receipt'] = completed
                saved.update(completed, status='complete', path=str(destination))
            except (SlicingError, OSError, ValueError, KeyError, TypeError) as error:
                saved.update(status='failed', error=error_receipt(error))
            write_json(result_path, result)
    successes = sum(row['status'] == 'complete' for row in result['outputs'])
    result.update(ok=successes == len(outputs), status=(
        'complete' if successes == len(outputs) else 'partial_failure' if successes else 'failed'
    ))
    write_json(result_path, result)
    return result
