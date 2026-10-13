"""Offline VM/PMO v1 plan and render adapter for the maintained slicing engine."""

import argparse
import json
import re
import stat
import sys
import tempfile
from pathlib import Path

from .alignment import parse_time_us
from .errors import SlicingError
from .instructions import parse_instruction
from .io import file_sha256, read_json, write_json, write_new_json
from .media import describe_source
from .planner import compile_job
from .render import render_output

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
        'protocol': PROTOCOL, 'schema_version': 1,
        **_identity(input_data),
        'input_sha256': context['input_sha256'],
        'source_sha256': input_data['source']['sha256'],
    }


def _error(error):
    code = getattr(error, 'code', 'EXECUTION_FAILED')
    messages = {
        'INVALID_INPUT': 'VM/PMO input does not match the v1 schema.',
        'INVALID_CONTEXT': 'Trusted local context is invalid.',
        'INPUT_CHANGED': 'Input bytes differ from the trusted context.',
        'PACKAGE_MISMATCH': 'Installed package identity differs from the request.',
        'UNSUPPORTED_ROUND': 'This review stage or parent is not supported yet.',
        'UNSUPPORTED_PROFILE': 'The requested output profile is not supported.',
        'UNSUPPORTED_INSTRUCTION': 'One or more editing instructions need review or additional evidence.',
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
    if input_data['protocol'] != PROTOCOL or type(input_data['schema_version']) is not int or input_data['schema_version'] != 1:
        raise VmError('INVALID_INPUT', 'Unsupported VM/PMO protocol or schema version')
    package = _object(input_data['package'], ('alias', 'name', 'version', 'manifest_sha256'), 'package')
    _id(package['alias'], 'package.alias')
    manifest = Path(__file__).resolve().parents[1] / 'PACKAGE-MANIFEST.json'
    if (package['name'], package['version']) != ('auto-cut-slicing', '0.2.0') or _hash(package['manifest_sha256'], 'package.manifest_sha256') != file_sha256(manifest):
        raise VmError('PACKAGE_MISMATCH', 'Installed package identity or manifest bytes differ')
    binding = _object(input_data['binding'], ('task_id', 'run_id', 'subject_key',
                                              'config_version', 'stage_id', 'event_id'), 'binding')
    for key, value in binding.items():
        _id(value, f'binding.{key}')
    if binding['stage_id'] != 'initial':
        raise VmError('UNSUPPORTED_ROUND', 'Only the initial stage is supported')
    record = _object(input_data['record'], ('base_token', 'table_id', 'record_id'), 'record')
    for key, value in record.items():
        _id(value, f'record.{key}')
    review = _object(input_data['review_round'], ('round_id', 'input_snapshot_id', 'parent'), 'review_round')
    _id(review['round_id'], 'review_round.round_id')
    _id(review['input_snapshot_id'], 'review_round.input_snapshot_id')
    if review['parent'] is not None:
        raise VmError('UNSUPPORTED_ROUND', 'Only initial vm_original rounds without a parent are supported')
    source = _object(input_data['source'], ('source_id', 'timeline_basis', 'vmid',
                                            'sha256', 'bytes', 'duration_ms'), 'source')
    _id(source['source_id'], 'source.source_id')
    _id(source['vmid'], 'source.vmid')
    _hash(source['sha256'], 'source.sha256')
    _positive_int(source['bytes'], 'source.bytes')
    _positive_int(source['duration_ms'], 'source.duration_ms')
    if source['timeline_basis'] != 'vm_original':
        raise VmError('UNSUPPORTED_ROUND', 'Only the vm_original timeline is supported')
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
        _object(item, ('instruction_id', 'source_field_id', 'raw_text', 'timeline_basis',
                       'applicability'), 'instruction')
        instruction_id = _id(item['instruction_id'], 'instruction.instruction_id')
        if instruction_id in seen:
            raise VmError('INVALID_INPUT', 'Instruction IDs must be unique')
        seen.add(instruction_id)
        _id(item['source_field_id'], 'instruction.source_field_id')
        if not isinstance(item['raw_text'], str) or not item['raw_text'].strip():
            raise VmError('INVALID_INPUT', 'instruction.raw_text must be nonempty')
        if item['timeline_basis'] != 'vm_original' or item['applicability'] != 'current_round':
            raise VmError('UNSUPPORTED_INSTRUCTION', 'Only current_round vm_original instructions are supported')


def _validate_context(context, input_path, context_path):
    _object(context, ('protocol', 'schema_version', 'input_sha256', 'run_dir', 'source_path',
                      'plan_path', 'plan_result_path', 'result_path', 'output_path'), 'context')
    if context['protocol'] != PROTOCOL or type(context['schema_version']) is not int or context['schema_version'] != 1:
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


def _build_plan(input_data, context, paths):
    _validate_input(input_data)
    descriptor = _source_descriptor(input_data, paths['source_path'])
    segments = _segments(input_data, descriptor)
    engine = _engine_plan(input_data, paths['source_path'], descriptor, segments,
                          paths['run_dir'])
    row = input_data['instructions'][0]
    plan = {
        'protocol': PROTOCOL, 'schema_version': 1, **_identity(input_data),
        'source': input_data['source'],
        'input_sha256': context['input_sha256'],
        'source_sha256': descriptor['sha256'],
        'instruction_results': [{
            'instruction_id': row['instruction_id'], 'source_field_id': row['source_field_id'],
            'raw_text': row['raw_text'], 'status': 'resolved',
            'segments': [segment['segment_id'] for segment in segments],
        }],
        'segments': segments, 'operations': [],
        'expected_duration_ms': sum(row['end_ms'] - row['start_ms'] for row in segments),
        'engine_plan_sha256': engine['plan_sha256'],
    }
    return plan, engine


def plan_command(input_path, context_path):
    _regular_file(input_path, 'INVALID_CONTEXT')
    _regular_file(context_path, 'INVALID_CONTEXT')
    input_data = read_json(input_path)
    context = read_json(context_path)
    paths = _validate_context(context, input_path, context_path)
    base = _receipt_base(input_data, context)
    try:
        if paths['plan_path'].exists() or paths['plan_result_path'].exists():
            raise VmError('OUTPUT_EXISTS', 'Plan or plan result already exists')
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
