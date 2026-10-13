import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from auto_cut_slicing.io import file_sha256, read_json, write_json
from auto_cut_slicing.media import describe_source


@pytest.fixture
def vm_case(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required for VM/PMO media contract')
    source = tmp_path / 'source.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:size=320x180:rate=25:duration=1',
        '-f', 'lavfi', '-i', 'sine=frequency=700:sample_rate=48000:duration=1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(source),
    ], check=True, capture_output=True)
    descriptor = describe_source(source)
    manifest = Path(__file__).resolve().parents[1] / 'PACKAGE-MANIFEST.json'
    input_data = {
        'protocol': 'vm_pmo_review_video', 'schema_version': 1,
        'package': {'alias': 'slicing', 'name': 'auto-cut-slicing', 'version': '0.2.0',
                    'manifest_sha256': file_sha256(manifest)},
        'binding': {'task_id': 'task-1', 'run_id': 'run-1', 'subject_key': 'subject-1',
                    'config_version': 'config-1', 'stage_id': 'initial', 'event_id': 'event-1'},
        'record': {'base_token': 'base-1', 'table_id': 'table-1', 'record_id': 'record-1'},
        'review_round': {'round_id': 'round-1', 'input_snapshot_id': 'snapshot-1', 'parent': None},
        'source': {'source_id': 'source-1', 'timeline_basis': 'vm_original', 'vmid': 'vm-1',
                   'sha256': descriptor['sha256'], 'bytes': source.stat().st_size,
                   'duration_ms': round(descriptor['duration_us'] / 1000)},
        'instructions': [{'instruction_id': 'instruction-1', 'source_field_id': 'field-1',
                          'raw_text': '0:00-0:00.20+0:00.40-0:00.60',
                          'timeline_basis': 'vm_original', 'applicability': 'current_round'}],
        'output_profile': {'id': 'review_mp4_v1', 'width': 1920, 'height': 1080,
                           'fps': 25, 'audio': 'preserve'},
        'delivery_target': {'attachment_field_id': 'attachment-1'},
    }
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    input_path = run_dir / 'input.json'
    write_json(input_path, input_data)
    context = {
        'protocol': 'vm_pmo_review_video', 'schema_version': 1,
        'input_sha256': file_sha256(input_path),
        'run_dir': str(run_dir), 'source_path': str(source),
        'plan_path': str(run_dir / 'plan.json'),
        'plan_result_path': str(run_dir / 'plan-result.json'),
        'result_path': str(run_dir / 'result.json'),
        'output_path': str(run_dir / 'review.mp4'),
    }
    context_path = run_dir / 'execution-context.json'
    write_json(context_path, context)
    return input_path, context_path, input_data, context


def invoke(*args):
    return subprocess.run([sys.executable, '-m', 'auto_cut_slicing.vm_pmo', *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8')


def test_vm_plan_and_render_bind_single_mp4_to_request(vm_case):
    input_path, context_path, input_data, context = vm_case
    planned = invoke('plan', '--input', input_path, '--context', context_path)
    assert planned.returncode == 0, planned.stderr
    plan = read_json(context['plan_path'])
    plan_result = read_json(context['plan_result_path'])
    assert plan_result['status'] == 'ready'
    assert plan_result['plan_sha256'] == file_sha256(context['plan_path'])
    assert plan['binding'] == input_data['binding']
    assert plan['record'] == input_data['record']
    assert plan['segments'] == [
        {'segment_id': 'part1', 'source_id': 'source-1', 'start_ms': 0, 'end_ms': 200},
        {'segment_id': 'part2', 'source_id': 'source-1', 'start_ms': 400, 'end_ms': 600},
    ]
    assert plan['operations'] == [] and plan['expected_duration_ms'] == 400
    rendered = invoke('render', '--input', input_path, '--context', context_path,
                      '--plan', context['plan_path'])
    assert rendered.returncode == 0, rendered.stderr
    result = read_json(context['result_path'])
    assert result['status'] == 'rendered' and result['plan_sha256'] == plan_result['plan_sha256']
    assert len(result['artifacts']) == 1
    artifact = result['artifacts'][0]
    assert artifact['kind'] == 'review_mp4' and artifact['path'] == context['output_path']
    assert artifact['sha256'] == file_sha256(context['output_path'])
    assert artifact['bytes'] == Path(context['output_path']).stat().st_size
    assert (artifact['width'], artifact['height'], artifact['fps']) == (1920, 1080, 25)
    assert artifact['has_audio'] and artifact['full_decode_passed']


def test_vm_semantic_instruction_blocks_without_output(vm_case):
    input_path, context_path, input_data, context = vm_case
    input_data['instructions'][0]['raw_text'] = '0:00-0:00.20，把开头字幕去掉'
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked'
    assert result['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']
    assert input_data['instructions'][0]['raw_text'] not in result['error']['message']
    assert not Path(context['plan_path']).exists()
    assert not Path(context['output_path']).exists()


def test_vm_render_rejects_changed_plan_before_output(vm_case):
    input_path, context_path, _, context = vm_case
    assert invoke('plan', '--input', input_path, '--context', context_path).returncode == 0
    plan = read_json(context['plan_path'])
    plan['segments'][0]['end_ms'] = 300
    write_json(context['plan_path'], plan)
    outcome = invoke('render', '--input', input_path, '--context', context_path,
                     '--plan', context['plan_path'])
    assert outcome.returncode == 0
    result = read_json(context['result_path'])
    assert result['status'] == 'blocked'
    assert not Path(context['output_path']).exists()


def test_vm_plan_blocks_changed_source_and_preserves_instruction(vm_case):
    input_path, context_path, input_data, context = vm_case
    source = Path(context['source_path'])
    with source.open('ab') as stream:
        stream.write(b'changed')
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked' and result['error']['code'] == 'SOURCE_CHANGED'
    assert result['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']
    assert not Path(context['plan_path']).exists()


def test_vm_multiple_instructions_block_without_dropping_feedback(vm_case):
    input_path, context_path, input_data, context = vm_case
    second = dict(input_data['instructions'][0], instruction_id='instruction-2',
                  raw_text='0:00.60-0:00.80')
    input_data['instructions'].append(second)
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked'
    assert [row['raw_text'] for row in result['instruction_results']] == [
        row['raw_text'] for row in input_data['instructions']]
    assert not Path(context['plan_path']).exists()


def test_vm_unsupported_review_round_produces_blocked_receipt(vm_case):
    input_path, context_path, input_data, context = vm_case
    input_data['review_round']['parent'] = {'run_id': 'prior-run'}
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked'
    assert result['error']['code'] == 'UNSUPPORTED_ROUND'
    assert result['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']


def test_vm_non_initial_stage_blocks_without_leaking_feedback(vm_case):
    input_path, context_path, input_data, context = vm_case
    input_data['binding']['stage_id'] = 'first_review'
    input_data['instructions'][0]['raw_text'] = '这个学生姓名需要打码'
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked' and result['error']['code'] == 'UNSUPPORTED_ROUND'
    assert result['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']
    assert input_data['instructions'][0]['raw_text'] not in result['error']['message']


def test_vm_rejects_context_outside_run_directory(vm_case):
    input_path, context_path, _, context = vm_case
    outside = context_path.parent.parent / 'context.json'
    outside.write_bytes(context_path.read_bytes())
    outcome = invoke('plan', '--input', input_path, '--context', outside)
    assert outcome.returncode != 0
    assert not Path(context['plan_result_path']).exists()


@pytest.mark.parametrize('change', ['unknown_field', 'boolean_version', 'float_profile'])
def test_vm_rejects_noncanonical_input_schema(vm_case, change):
    input_path, context_path, input_data, context = vm_case
    if change == 'unknown_field':
        input_data['extra'] = 'ignored?'
    elif change == 'boolean_version':
        input_data['schema_version'] = True
    else:
        input_data['output_profile']['fps'] = 25.0
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    result = read_json(context['plan_result_path'])
    assert outcome.returncode == 0 and result['status'] == 'blocked'
    assert not Path(context['plan_path']).exists()


def test_vm_rejects_output_outside_run_dir(vm_case):
    input_path, context_path, _, context = vm_case
    settings = read_json(context_path)
    outside = context_path.parent.parent / 'outside.mp4'
    settings['output_path'] = str(outside)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode != 0
    assert not Path(context['plan_result_path']).exists()
    assert not outside.exists()


def test_vm_refuses_existing_result_without_changing_bytes(vm_case):
    input_path, context_path, _, context = vm_case
    assert invoke('plan', '--input', input_path, '--context', context_path).returncode == 0
    existing = Path(context['result_path'])
    existing.write_bytes(b'original receipt')
    outcome = invoke('render', '--input', input_path, '--context', context_path,
                     '--plan', context['plan_path'])
    assert outcome.returncode != 0
    assert existing.read_bytes() == b'original receipt'
    assert not Path(context['output_path']).exists()


@pytest.mark.parametrize('name', ['input', 'context'])
def test_vm_rejects_hardlinked_input_or_context(vm_case, name):
    input_path, context_path, _, context = vm_case
    path = input_path if name == 'input' else context_path
    os.link(path, path.parent.parent / f'{name}-alias.json')
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode != 0
    assert not Path(context['plan_path']).exists()


def test_vm_blocks_hardlinked_source(vm_case):
    input_path, context_path, _, context = vm_case
    source = Path(context['source_path'])
    os.link(source, source.parent / 'source-alias.mp4')
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked' and result['error']['code'] == 'SOURCE_INVALID'
    assert not Path(context['plan_path']).exists()


@pytest.mark.parametrize('name', ['plan_path', 'plan_result_path'])
def test_vm_blocks_hardlinked_plan_or_ready_receipt(vm_case, name):
    input_path, context_path, _, context = vm_case
    assert invoke('plan', '--input', input_path, '--context', context_path).returncode == 0
    original = Path(context[name])
    os.link(original, original.parent / f'{name}-alias.json')
    outcome = invoke('render', '--input', input_path, '--context', context_path,
                     '--plan', context['plan_path'])
    assert outcome.returncode == 0
    result = read_json(context['result_path'])
    assert result['status'] == 'blocked'
    assert not Path(context['output_path']).exists()


def test_vm_blocks_manifest_source_instead_of_hashing_only_external_list(vm_case):
    input_path, context_path, input_data, context = vm_case
    source = Path(context['source_path'])
    external = source.with_name('segment.mov')
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:size=320x180:rate=25:duration=1',
        '-f', 'lavfi', '-i', 'sine=frequency=700:sample_rate=48000:duration=1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'pcm_s16le', '-shortest', str(external),
    ], check=True, capture_output=True)
    source.write_text("ffconcat version 1.0\nfile 'segment.mov'\nduration 1\n", encoding='utf-8')
    descriptor = describe_source(source)
    input_data['source'].update(sha256=file_sha256(source), bytes=source.stat().st_size,
                                duration_ms=round(descriptor['duration_us'] / 1000))
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    outcome = invoke('plan', '--input', input_path, '--context', context_path)
    assert outcome.returncode == 0
    result = read_json(context['plan_result_path'])
    assert result['status'] == 'blocked'
    assert result['error']['code'] == 'SOURCE_INVALID'
    assert not Path(context['plan_path']).exists()


def test_vm_render_blocks_source_replaced_by_external_manifest(vm_case):
    input_path, context_path, _, context = vm_case
    assert invoke('plan', '--input', input_path, '--context', context_path).returncode == 0
    source = Path(context['source_path'])
    source.rename(source.with_name('segment.mp4'))
    source.write_text("ffconcat version 1.0\nfile 'segment.mp4'\nduration 1\n", encoding='utf-8')
    outcome = invoke('render', '--input', input_path, '--context', context_path,
                     '--plan', context['plan_path'])
    assert outcome.returncode == 0
    result = read_json(context['result_path'])
    assert result['status'] == 'blocked'
    assert not Path(context['output_path']).exists()
