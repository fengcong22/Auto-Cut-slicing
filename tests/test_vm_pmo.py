import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from auto_cut_slicing import vm_pmo
from auto_cut_slicing.io import file_sha256, read_json, write_json, write_new_json
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
    manifest_data = json.loads(manifest.read_text(encoding='utf-8'))
    input_data = {
        'protocol': 'vm_pmo_review_video', 'schema_version': 1,
        'package': {'alias': 'slicing', 'name': 'auto-cut-slicing', 'version': manifest_data['version'],
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


def configure_v2_review(input_path, context_path, input_data, context, raw_text):
    source = Path(context['source_path'])
    digest = file_sha256(source)
    input_data['schema_version'] = 2
    input_data['binding']['stage_id'] = 'review'
    input_data['review_round'] = {
        'round_id': 'round-2', 'input_snapshot_id': 'snapshot-2',
        'parent': {'kind': 'managed_run', 'run_id': 'run-0', 'artifact_id': 'artifact-1',
                   'delivery_id': 'delivery-1', 'sha256': digest,
                   'bytes': source.stat().st_size,
                   'duration_ms': input_data['source']['duration_ms']},
    }
    input_data['source']['timeline_basis'] = 'previous_review_video'
    input_data['instructions'][0].update(
        raw_text=raw_text, timeline_basis='previous_review_video')
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['schema_version'] = 2
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    return digest


def configure_v2_initial(input_path, context_path, input_data, context, raw_text):
    digest = file_sha256(context['source_path'])
    input_data['schema_version'] = 2
    input_data['binding']['stage_id'] = 'initial'
    input_data['review_round'] = {
        'round_id': 'round-initial-v2', 'input_snapshot_id': 'snapshot-initial-v2',
        'parent': None,
    }
    input_data['source']['timeline_basis'] = 'vm_original'
    input_data['instructions'][0].update(raw_text=raw_text, timeline_basis='vm_original')
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['schema_version'] = 2
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)
    return digest


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


def test_vm_v2_initial_mutes_exact_source_bound_word_and_renders(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_initial(input_path, context_path, input_data, context,
                                  '0:00 剪掉“上一章”三个字的声音')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        assert source_descriptor['sha256'] == digest
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'ready', planned.get('error')
    plan = read_json(context['plan_path'])
    assert plan['binding']['stage_id'] == 'initial'
    assert plan['review_round']['parent'] is None
    assert plan['source']['timeline_basis'] == 'vm_original'
    operation = plan['operations'][0]
    assert operation['kind'] == 'mute_word'
    assert (operation['source_start_us'], operation['source_end_us']) == (200_000, 400_000)
    assert operation['matched_text'] == '上一章'
    assert operation['alignment_sha256'] == plan['alignment_sha256']

    rendered = vm_pmo.render_command(input_path, context_path, Path(context['plan_path']))
    assert rendered['status'] == 'rendered'
    assert rendered['artifacts'][0]['sha256'] == file_sha256(context['output_path'])
    assert rendered['artifacts'][0]['full_decode_passed']
    assert rendered['artifacts'][0]['duration_ms'] == 1000


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


def test_vm_v2_review_deletes_exact_parent_word_and_renders(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00 剪掉“上一章”三个字，其他不变')
    transcription_calls = []

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        transcription_calls.append(source_descriptor['sha256'])
        assert source_descriptor['sha256'] == digest
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'ready', planned.get('error')
    plan = read_json(context['plan_path'])
    assert plan['binding']['stage_id'] == 'review'
    assert plan['review_round'] == input_data['review_round']
    assert plan['operations'][0]['kind'] == 'word'
    assert (plan['operations'][0]['source_start_us'],
            plan['operations'][0]['source_end_us']) == (200_000, 400_000)
    assert plan['operations'][0]['matched_text'] == '上一章'
    assert plan['operations'][0]['match_strategy'] == 'unique_exact_whole_token'
    assert plan['operations'][0]['alignment_sha256'] == plan['alignment_sha256']
    assert plan['operations'][0]['provider'] == 'synthetic-fixture'
    assert plan['operations'][0]['must_keep_evidence'] == []
    assert plan['instruction_results'][0]['segments'] == [
        segment['segment_id'] for segment in plan['segments']
    ]
    assert plan['expected_duration_ms'] == 800
    assert len(transcription_calls) == 1


def test_vm_v2_concurrent_plan_calls_share_one_ready_result(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00 剪掉“上一章”三个字，其他不变')
    transcribe_started = threading.Event()
    second_transcribe_started = threading.Event()
    release_transcribe = threading.Event()
    second_lock_opened = threading.Event()
    transcription_calls = []
    calls_lock = threading.Lock()
    lock_opens = 0
    original_os_open = os.open

    def track_lock_open(path, *args, **kwargs):
        nonlocal lock_opens
        if Path(path).name == '.plan.lock':
            with calls_lock:
                lock_opens += 1
                if lock_opens == 2:
                    second_lock_opened.set()
        return original_os_open(path, *args, **kwargs)

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        with calls_lock:
            transcription_calls.append(source_descriptor['sha256'])
            if len(transcription_calls) > 1:
                second_transcribe_started.set()
        assert source_descriptor['sha256'] == digest
        transcribe_started.set()
        assert release_transcribe.wait(timeout=5)
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_new_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    monkeypatch.setattr(vm_pmo.os, 'open', track_lock_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(vm_pmo.plan_command, input_path, context_path)
        assert transcribe_started.wait(timeout=5)
        second = executor.submit(vm_pmo.plan_command, input_path, context_path)
        assert second_lock_opened.wait(timeout=5)
        try:
            assert not second_transcribe_started.wait(timeout=0.1)
        finally:
            release_transcribe.set()
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    assert first_result['status'] == 'ready', first_result.get('error')
    assert second_result['status'] == 'ready', second_result.get('error')
    assert first_result == second_result
    assert transcription_calls == [digest]
    assert read_json(context['plan_result_path']) == first_result
    assert first_result['plan_sha256'] == file_sha256(context['plan_path'])


@pytest.mark.parametrize(('changed_file', 'expected_code'), [
    ('source', 'SOURCE_CHANGED'),
    ('alignment', 'PLAN_CHANGED'),
])
def test_vm_v2_repeated_plan_revalidates_bound_files(vm_case, monkeypatch,
                                                     changed_file, expected_code):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00 剪掉“上一章”三个字，其他不变')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        assert source_descriptor['sha256'] == digest
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    receipt = vm_pmo.plan_command(input_path, context_path)
    assert receipt['status'] == 'ready'
    changed_path = (Path(context['source_path']) if changed_file == 'source'
                    else Path(context['run_dir']) / 'alignment.words.json')
    changed_path.write_bytes(changed_path.read_bytes() + b' ')

    with pytest.raises(vm_pmo.VmError) as error:
        vm_pmo.plan_command(input_path, context_path)

    assert error.value.code == expected_code
    assert read_json(context['plan_result_path']) == receipt


def test_vm_v2_repeated_plan_rejects_alignment_swap_during_validation(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00 剪掉“上一章”三个字，其他不变')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    receipt = vm_pmo.plan_command(input_path, context_path)
    assert receipt['status'] == 'ready'
    original_load_alignment = vm_pmo.load_alignment
    alignment_path = Path(context['run_dir']) / 'alignment.words.json'

    def swap_then_load(path, descriptor):
        alignment = read_json(path)
        alignment['words'][1]['text'] = '其他词'
        write_json(path, alignment)
        return original_load_alignment(path, descriptor)

    monkeypatch.setattr(vm_pmo, 'load_alignment', swap_then_load)
    with pytest.raises(vm_pmo.VmError) as error:
        vm_pmo.plan_command(input_path, context_path)

    assert error.value.code == 'PLAN_CHANGED'
    assert alignment_path.exists()
    assert read_json(context['plan_result_path']) == receipt


def test_vm_v2_review_mute_receipt_matches_taskboard_contract(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00 剪掉“上一章”三个字的声音')
    transcription_calls = []

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        transcription_calls.append(source_descriptor['sha256'])
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'ready', planned.get('error')
    plan = read_json(context['plan_path'])
    operation = plan['operations'][0]
    assert operation['operation_id'] == 'part1/edit1'
    assert operation['kind'] == 'mute_word'
    assert (operation['source_start_us'], operation['source_end_us']) == (200_000, 400_000)
    assert operation['target_join_us'] == 200_000
    assert operation['matched_text'] == '上一章'
    assert operation['match_strategy'] == 'unique_exact_whole_token'
    assert operation['alignment_sha256'] == plan['alignment_sha256']
    assert operation['provider'] == 'synthetic-fixture'
    assert operation['must_keep_evidence'] == []
    assert plan['expected_duration_ms'] == 1000

    rendered = vm_pmo.render_command(input_path, context_path, Path(context['plan_path']))
    assert rendered['status'] == 'rendered'
    assert rendered['artifacts'][0]['sha256'] == file_sha256(context['output_path'])
    assert rendered['artifacts'][0]['full_decode_passed']
    assert len(transcription_calls) == 1


def test_vm_v2_review_receipts_source_bound_start_cue_word_evidence(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00.20（接下来）-0:00.90')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '前文', 'start': 0, 'end': 0.2},
                {'text': '接下来', 'start': 0.2, 'end': 0.4},
                {'text': '后文', 'start': 0.4, 'end': 0.9},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'ready', planned.get('error')
    plan = read_json(context['plan_path'])
    assert len(plan['operations']) == 1
    cue = plan['operations'][0]
    assert cue['kind'] == 'start_cue'
    assert (cue['source_start_us'], cue['source_end_us']) == (200_000, 400_000)
    assert cue['requested_start_us'] == 200_000
    assert cue['matched_text'] == '接下来'
    assert cue['match_strategy'] == 'unique_exact_whole_token'
    assert cue['alignment_sha256'] == plan['alignment_sha256']
    assert (cue['provider'], cue['model'], cue['adapter']) == (
        'synthetic-fixture', 'synthetic-fixture', 'synthetic-fixture')


def test_vm_v2_review_blocks_ambiguous_start_cue_word_evidence(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '0:00.20（接下来）-0:00.90')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '接下来', 'start': 0.2, 'end': 0.4},
                {'text': '正文', 'start': 0.4, 'end': 0.5},
                {'text': '接下来', 'start': 0.6, 'end': 0.8},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'blocked'
    assert planned['error']['code'] == 'WORD_ALIGNMENT_UNRESOLVED'
    assert planned['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']
    assert not Path(context['plan_path']).exists()
    assert not Path(context['output_path']).exists()


def test_vm_v2_review_blocks_ambiguous_word_match_without_output(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '剪掉“上一章”三个字，其他不变')

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '上一章', 'start': 0.1, 'end': 0.3},
                {'text': '正文', 'start': 0.3, 'end': 0.5},
                {'text': '上一章', 'start': 0.6, 'end': 0.8},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'blocked'
    assert planned['error']['code'] == 'WORD_ALIGNMENT_UNRESOLVED'
    assert planned['instruction_results'][0]['raw_text'] == input_data['instructions'][0]['raw_text']
    assert not Path(context['plan_path']).exists()
    assert not Path(context['output_path']).exists()


def test_vm_v2_review_protects_explicit_keep_words(vm_case, monkeypatch):
    input_path, context_path, input_data, context = vm_case
    digest = configure_v2_review(input_path, context_path, input_data, context,
                                 '剪掉“上一章”三个字，其他不变')
    input_data['instructions'][0]['must_keep'] = ['上一章']
    write_json(input_path, input_data)
    settings = read_json(context_path)
    settings['input_sha256'] = file_sha256(input_path)
    write_json(context_path, settings)

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        alignment = {
            'schema_version': 1, 'source_sha256': digest,
            'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
            'adapter': 'synthetic-fixture',
            'words': [
                {'text': '上一章', 'start': 0.2, 'end': 0.4},
                {'text': '正文', 'start': 0.4, 'end': 0.8},
            ],
        }
        write_json(output, alignment)
        return alignment

    monkeypatch.setattr(vm_pmo, 'transcribe_source', transcribe, raising=False)
    planned = vm_pmo.plan_command(input_path, context_path)
    assert planned['status'] == 'blocked'
    assert planned['error']['code'] == 'WORD_ALIGNMENT_UNRESOLVED'
    assert not Path(context['plan_path']).exists()
    assert not Path(context['output_path']).exists()
