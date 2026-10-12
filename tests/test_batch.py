import array
import copy
import json
import shutil
import subprocess

import pytest

from auto_cut_slicing.io import file_sha256, read_json, write_json


@pytest.fixture
def batch_request(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required for actual batch delivery tests')
    source = tmp_path / '原始视频.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:size=320x180:rate=24:duration=1',
        '-f', 'lavfi', '-i', 'sine=frequency=700:sample_rate=48000:duration=1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(source),
    ], check=True, capture_output=True)
    request = {
        'contract_version': 1, 'request_id': 'batch-001',
        'boundary_policy': 'exact',
        'source': {'id': 'source', 'path': source.name},
        'outputs': [{
            'id': 'good', 'instruction': '0:00-0:00.32',
            'output_path': '成片/good.mp4',
        }],
    }
    path = write_json(tmp_path / 'request.json', request)
    return path, request, source


def test_batch_invalid_output_continues_and_delivers_fixed_profile(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    request['outputs'].insert(0, {
        'id': 'bad', 'instruction': '0:02-0:01', 'output_path': '成片/bad.mp4',
    })
    write_json(path, request)
    receipt_path = tmp_path / 'result.json'
    result = run_request(path, receipt_path)
    assert result['ok'] is False and result['status'] == 'partial_failure'
    assert [row['status'] for row in result['outputs']] == ['failed', 'complete']
    good = result['outputs'][1]
    assert (good['width'], good['height'], good['fps']) == (1920, 1080, 25)
    assert good['expected_duration_us'] == 320_000
    assert good['full_decode_passed'] is True
    assert good['spoken_final_acceptance'] is False
    assert file_sha256(good['path']) == good['sha256']
    assert read_json(receipt_path) == result
    assert not (tmp_path / '成片/bad.mp4').exists()


def test_batch_preserves_raw_instruction_and_needs_real_alignment(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    raw = '0:00（首先）-0:00.32'
    request['outputs'].insert(0, {'id': 'cue', 'instruction': raw, 'output_path': 'cue.mp4'})
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert result['outputs'][0]['source_text'] == raw
    assert result['outputs'][0]['error']['code'] == 'ASR_REQUIRED'
    assert result['outputs'][1]['status'] == 'complete'


def test_batch_resume_verifies_receipt_and_output_bytes(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, _, _ = batch_request
    receipt_path = tmp_path / 'result.json'
    first = run_request(path, receipt_path)
    output_path = tmp_path / '成片/good.mp4'
    original_mtime = output_path.stat().st_mtime_ns
    second = run_request(path, receipt_path, resume=True)
    assert second['ok'] and second['outputs'][0]['sha256'] == first['outputs'][0]['sha256']
    assert output_path.stat().st_mtime_ns == original_mtime
    with output_path.open('ab') as stream:
        stream.write(b'corruption')
    third = run_request(path, receipt_path, resume=True)
    assert third['ok'] is False
    assert third['outputs'][0]['error']['code'] == 'OUTPUT_CHANGED'


def test_batch_rejects_receipt_as_source_without_overwrite(batch_request):
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.errors import SlicingError

    path, _, source = batch_request
    original = file_sha256(source)
    with pytest.raises(SlicingError):
        run_request(path, source)
    assert file_sha256(source) == original


def test_conflicting_outputs_fail_before_writing_while_independent_output_runs(
    batch_request, tmp_path
):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    collision = copy.deepcopy(request['outputs'][0])
    collision.update(id='collision')
    request['outputs'].insert(0, collision)
    request['outputs'].append({
        'id': 'independent', 'instruction': '0:00-0:00.32', 'output_path': 'independent.mp4',
    })
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert [row['status'] for row in result['outputs']] == ['failed', 'failed', 'complete']
    assert not (tmp_path / '成片/good.mp4').exists()
    assert (tmp_path / 'independent.mp4').is_file()


def test_batch_does_not_overwrite_unrelated_existing_output(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, _, _ = batch_request
    output = tmp_path / '成片/good.mp4'
    output.parent.mkdir()
    output.write_bytes(b'existing unrelated video')
    result = run_request(path, tmp_path / 'result.json')
    assert result['outputs'][0]['error']['code'] == 'OUTPUT_EXISTS'
    assert output.read_bytes() == b'existing unrelated video'


def test_changed_request_cannot_resume_receipt(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.errors import SlicingError

    path, request, _ = batch_request
    receipt = tmp_path / 'result.json'
    run_request(path, receipt)
    request['outputs'][0]['instruction'] = '0:00-0:00.64'
    write_json(path, request)
    before = receipt.read_bytes()
    with pytest.raises(SlicingError, match='request'):
        run_request(path, receipt, resume=True)
    assert receipt.read_bytes() == before


def test_batch_runtime_failure_does_not_stop_later_outputs(batch_request, tmp_path, monkeypatch):
    from auto_cut_slicing import render
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.errors import SlicingError

    path, request, _ = batch_request
    request['outputs'].insert(0, {
        'id': 'broken', 'instruction': '0:00-0:00.32', 'output_path': 'broken.mp4',
    })
    write_json(path, request)
    real_render = render.render_output

    def render_with_failure(plan, output_id, destination, **profile):
        if output_id == 'broken':
            raise SlicingError('FFmpeg render failed fixture')
        return real_render(plan, output_id, destination, **profile)

    monkeypatch.setattr(render, 'render_output', render_with_failure)
    result = run_request(path, tmp_path / 'result.json')
    assert [row['status'] for row in result['outputs']] == ['failed', 'complete']
    assert result['status'] == 'partial_failure'


def test_run_cli_returns_json_receipt_and_nonzero_on_partial_failure(batch_request, tmp_path, capsys):
    from auto_cut_slicing.cli import main

    path, request, _ = batch_request
    request['outputs'].insert(0, {
        'id': 'bad', 'instruction': 'not an edit', 'output_path': 'bad.mp4',
    })
    write_json(path, request)
    receipt = tmp_path / 'result.json'
    assert main(['run', str(path), '--result', str(receipt)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == read_json(receipt)
    assert result['outputs'][1]['status'] == 'complete'


def test_resume_preserves_completed_evidence_across_transient_compile_failure(
    batch_request, tmp_path, monkeypatch
):
    from auto_cut_slicing import batch
    from auto_cut_slicing.errors import SlicingError

    path, _, _ = batch_request
    receipt = tmp_path / 'result.json'
    first = batch.run_request(path, receipt)
    original_compile = batch._compile_output

    def unavailable(*args, **kwargs):
        raise SlicingError('Temporary alignment file unavailable')

    monkeypatch.setattr(batch, '_compile_output', unavailable)
    failed = batch.run_request(path, receipt, resume=True)
    assert failed['ok'] is False
    monkeypatch.setattr(batch, '_compile_output', original_compile)
    recovered = batch.run_request(path, receipt, resume=True)
    assert recovered['ok'] is True
    assert recovered['outputs'][0]['sha256'] == first['outputs'][0]['sha256']


def test_resume_source_temporarily_missing_does_not_destroy_receipt_identity(
    batch_request, tmp_path
):
    from auto_cut_slicing.batch import run_request

    path, _, source = batch_request
    receipt = tmp_path / 'result.json'
    first = run_request(path, receipt)
    moved = source.with_suffix('.missing')
    source.rename(moved)
    try:
        failed = run_request(path, receipt, resume=True)
        assert failed['outputs'][0]['error']['code'] == 'SOURCE_INVALID'
        assert failed['source_sha256'] == first['source_sha256']
    finally:
        moved.rename(source)
    assert run_request(path, receipt, resume=True)['ok'] is True


def test_malformed_saved_receipt_fails_before_replacing_it(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.errors import SlicingError
    from auto_cut_slicing.io import json_sha256

    path, request, _ = batch_request
    receipt = write_json(tmp_path / 'result.json', {
        'contract_version': 1, 'request_id': request['request_id'],
        'request_sha256': json_sha256(request), 'outputs': [None],
    })
    original = receipt.read_bytes()
    with pytest.raises(SlicingError, match='Saved result'):
        run_request(path, receipt, resume=True)
    assert receipt.read_bytes() == original


def test_initial_source_failure_can_resume_when_source_is_supplied(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, _, source = batch_request
    moved = source.with_suffix('.missing')
    source.rename(moved)
    receipt = tmp_path / 'result.json'
    try:
        assert run_request(path, receipt)['status'] == 'failed'
    finally:
        moved.rename(source)
    assert run_request(path, receipt, resume=True)['ok'] is True


def test_resume_refreshes_measurements_and_preserves_request_labels(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, _, _ = batch_request
    receipt = tmp_path / 'result.json'
    first = run_request(path, receipt)
    saved = read_json(receipt)
    for row in (saved['outputs'][0], saved['outputs'][0]['completion_receipt']):
        row.update(source_text='corrupted', name='corrupted', width=1, height=1,
                   duration_us=999999999, fps=1)
    write_json(receipt, saved)
    resumed = run_request(path, receipt, resume=True)
    assert resumed['ok'] is True
    row = resumed['outputs'][0]
    assert row['source_text'] == first['outputs'][0]['source_text']
    assert row['name'] == 'good'
    assert (row['width'], row['height'], row['fps']) == (1920, 1080, 25)
    assert row['duration_us'] == first['outputs'][0]['duration_us']


def test_source_bound_cue_ripple_mute_and_freeze_deliver_one_verified_mp4(tmp_path):
    from auto_cut_slicing.batch import run_request

    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required for actual batch delivery tests')
    source = tmp_path / 'animation.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=160x90:rate=25:duration=3', '-f', 'lavfi', '-i',
        'sine=frequency=700:sample_rate=48000:duration=3', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source),
    ], check=True, capture_output=True)
    write_json(tmp_path / 'fixture.words.json', {
        'schema_version': 1, 'source_sha256': file_sha256(source),
        'provider': 'synthetic-test-fixture', 'model': 'synthetic', 'adapter': 'fixture-v1',
        'words': [
            {'text': '首先', 'start': .12, 'end': .24},
            {'text': '上一章', 'start': .4, 'end': .6},
            {'text': '声音', 'start': 1.04, 'end': 1.28},
        ],
    })
    clip = {
        'id': 'c1', 'start': .1, 'end': 2.8, 'start_cue': {'text': '首先'},
        'deletions': [{'id': 'delete', 'kind': 'word', 'text': '上一章', 'source_text': '剪掉“上一章”'}],
        'edits': [
            {'id': 'mute', 'kind': 'mute_word', 'text': '声音', 'source_text': '只删除声音'},
            {'id': 'freeze', 'kind': 'freeze_frame', 'start': .1, 'end': 1,
             'frame_time': .8, 'source_text': '使用后续选定帧直到1秒'},
        ],
    }
    request = write_json(tmp_path / 'request.json', {
        'contract_version': 1, 'request_id': 'effects-fixture',
        'boundary_policy': 'exact',
        'source': {'id': 's1', 'vmid': 'same-vmid', 'path': source.name, 'asr': 'fixture.words.json'},
        'outputs': [{'id': 'effects', 'name': '效果测试', 'clips': [clip], 'output_path': 'effects.mp4'}],
    })
    result = run_request(request, tmp_path / 'result.json')
    assert result['ok'], result
    output = result['outputs'][0]
    assert output['clips'] == [clip]
    assert output['start_cues'][0]['source_start_us'] == 120_000
    assert output['start_cues'][0]['provider'] == 'synthetic-test-fixture'
    assert output['expected_duration_us'] == 2_480_000
    assert output['full_decode_passed'] is True
    assert output['spoken_final_acceptance'] is False
    assert len(output['segments']) == 2
    assert [(effect['target_start_us'], effect['duration_us']) for effect in output['effects']
            if effect['kind'] == 'freeze_frame'] == [(0, 280_000), (280_000, 400_000)]
    destination = tmp_path / 'effects.mp4'

    def frame(path, time):
        # Compare with the same delivery resampling, avoiding interpolation differences.
        scale = 'scale=1920:1080,scale=160:90' if path == source else 'scale=160:90'
        return subprocess.run([
            'ffmpeg', '-v', 'error', '-ss', str(time), '-i', str(path),
            '-vf', scale, '-frames:v', '1', '-an', '-pix_fmt', 'rgb24',
            '-f', 'rawvideo', '-',
        ], check=True, capture_output=True).stdout

    expected = frame(source, .8)
    for time in (.08, .48):
        actual = frame(destination, time)
        assert len(actual) == len(expected)
        assert sum(abs(a - b) for a, b in zip(actual, expected)) / len(actual) < 5
    resumed = frame(destination, 1.08)
    expected_resumed = frame(source, 1.4)
    assert sum(abs(a - b) for a, b in zip(resumed, expected_resumed)) / len(resumed) < 5

    def samples(time, duration):
        decoded = subprocess.run([
            'ffmpeg', '-v', 'error', '-i', str(destination), '-ss', str(time),
            '-t', str(duration), '-vn', '-ac', '1', '-ar', '48000', '-f', 's16le', '-',
        ], check=True, capture_output=True).stdout
        return array.array('h', decoded)

    assert max(abs(sample) for sample in samples(.78, .1)) <= 2
    assert max(abs(sample) for sample in samples(.04, .12)) > 1000
    assert max(abs(sample) for sample in samples(1.05, .12)) > 1000


def test_structured_missing_clip_id_and_wrong_visual_hash_fail_independently(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    request['outputs'].insert(0, {
        'id': 'missing-id', 'clips': [{'start': 0, 'end': .32}], 'output_path': 'missing-id.mp4',
    })
    request['outputs'].insert(1, {
        'id': 'wrong-source', 'instruction': '0:00-0:00.32', 'output_path': 'wrong-source.mp4',
        'visual_evidence': {'source_sha256': '0' * 64, 'frame_time': .12, 'end': .24},
    })
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert [row['status'] for row in result['outputs']] == ['failed', 'failed', 'complete']
    assert result['outputs'][1]['error']['code'] == 'VISUAL_EVIDENCE_INVALID'


def test_resume_rejects_output_changed_between_hash_and_media_probe(tmp_path, monkeypatch):
    from auto_cut_slicing import batch, media

    destination = tmp_path / 'race.mp4'
    destination.write_bytes(b'original')
    original_hash, changed_hash = 'a' * 64, 'b' * 64
    hashes = iter([original_hash, changed_hash])
    monkeypatch.setattr(batch, 'file_sha256', lambda path: next(hashes))
    monkeypatch.setattr(media, 'describe_source', lambda path: {
        'sha256': changed_hash, 'width': 1920, 'height': 1080, 'fps': 25,
        'has_audio': True, 'duration_us': 320_000,
    })
    monkeypatch.setattr(media, '_run', lambda *args, **kwargs: None)
    monkeypatch.setattr(media, 'find_tool', lambda name: name)
    plan = {'plan_sha256': 'plan', 'outputs': [{'id': 'out', 'duration_us': 320_000, 'segments': [{}]}]}
    saved = {'path': str(destination), 'plan_sha256': 'plan', 'sha256': original_hash}
    with pytest.raises(batch.ContractError, match='changed'):
        batch._verify_completed(saved, plan, destination)


def test_resume_rechecks_source_after_full_output_decode(batch_request, tmp_path, monkeypatch):
    from auto_cut_slicing import media
    from auto_cut_slicing.batch import run_request

    path, _, source = batch_request
    receipt = tmp_path / 'result.json'
    assert run_request(path, receipt)['ok']
    real_run = media._run

    def change_source_during_decode(arguments, *, purpose):
        result = real_run(arguments, purpose=purpose)
        if purpose == 'Resume full decode':
            with source.open('ab') as stream:
                stream.write(b'changed during reuse validation')
        return result

    monkeypatch.setattr(media, '_run', change_source_during_decode)
    result = run_request(path, receipt, resume=True)
    assert not result['ok']
    assert result['outputs'][0]['error']['code'] == 'SOURCE_CHANGED'


def _sentence_alignment(source, path):
    # Explicitly synthetic word and utterance evidence, bound to actual source bytes.
    words = [
        {'text': '完整', 'start': .12, 'end': .24},
        {'text': '句子', 'start': .32, 'end': .48},
    ]
    return write_json(path, {
        'schema_version': 1, 'source_sha256': file_sha256(source),
        'provider': 'synthetic-test-fixture', 'model': 'synthetic', 'adapter': 'fixture-v1',
        'words': words,
        'utterances': [{'text': '完整句子。', 'start': .12, 'end': .48, 'words': words}],
    })


def test_default_sentence_policy_renders_complete_utterance(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, request, source = batch_request
    request.pop('boundary_policy')
    request['source']['asr'] = 'sentence.words.json'
    request['outputs'][0]['instruction'] = '0:00.2-0:00.3'
    _sentence_alignment(source, tmp_path / 'sentence.words.json')
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert result['ok'], result
    output = result['outputs'][0]
    assert output['boundary_policy'] == 'sentence'
    assert output['expected_duration_us'] == 360_000
    assert [(row['source_start_us'], row['duration_us']) for row in output['segments']] == [
        (120_000, 360_000),
    ]
    adjustment = output['boundary_adjustments'][0]
    assert (adjustment['requested_start_us'], adjustment['requested_end_us'],
            adjustment['resolved_start_us'], adjustment['resolved_end_us']) == (
        200_000, 300_000, 120_000, 480_000,
    )
    assert [(row['side'], row['requested_us'], row['resolved_us'])
            for row in adjustment['evidence']] == [
        ('start', 200_000, 120_000), ('end', 300_000, 480_000),
    ]
    assert output['full_decode_passed'] and not output['spoken_final_acceptance']


def test_default_sentence_policy_is_saved_before_failed_compile_and_resume(
    batch_request, tmp_path, monkeypatch
):
    from auto_cut_slicing import batch

    path, request, source = batch_request
    request.pop('boundary_policy')
    request['source']['asr'] = 'sentence.words.json'
    request['outputs'][0]['instruction'] = '0:00.2-0:00.3'
    write_json(path, request)
    receipt = tmp_path / 'result.json'
    compile_output = batch._compile_output

    def compile_with_pending_receipt(*args, **kwargs):
        pending = read_json(receipt)['outputs'][0]
        assert pending['status'] == 'pending'
        assert pending['boundary_policy'] == 'sentence'
        return compile_output(*args, **kwargs)

    monkeypatch.setattr(batch, '_compile_output', compile_with_pending_receipt)
    failed = batch.run_request(path, receipt)
    assert failed['status'] == 'failed'
    assert failed['outputs'][0]['boundary_policy'] == 'sentence'
    assert failed['outputs'][0]['error']['code'] in {'ASR_REQUIRED', 'ASR_INVALID'}
    _sentence_alignment(source, tmp_path / 'sentence.words.json')
    recovered = batch.run_request(path, receipt, resume=True)
    assert recovered['ok'], recovered
    assert recovered['outputs'][0]['boundary_policy'] == 'sentence'
    assert recovered['outputs'][0]['expected_duration_us'] == 360_000


def test_default_sentence_policy_fails_without_asr_before_output_publication(
    batch_request, tmp_path
):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    request.pop('boundary_policy')
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert not result['ok']
    assert result['outputs'][0]['boundary_policy'] == 'sentence'
    assert result['outputs'][0]['error']['code'] == 'ASR_REQUIRED'
    assert not (tmp_path / '成片/good.mp4').exists()


def test_default_sentence_policy_allows_complete_container_without_asr(batch_request, tmp_path):
    from auto_cut_slicing.batch import run_request

    path, request, _ = batch_request
    request.pop('boundary_policy')
    request['outputs'][0]['instruction'] = '0:00-0:01'
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert result['ok'], result
    assert result['outputs'][0]['boundary_policy'] == 'sentence'
    assert result['outputs'][0]['expected_duration_us'] == 1_000_000
    adjustment = result['outputs'][0]['boundary_adjustments'][0]
    assert (adjustment['requested_start_us'], adjustment['resolved_start_us'],
            adjustment['requested_end_us'], adjustment['resolved_end_us']) == (
        0, 0, 1_000_000, 1_000_000,
    )
    assert adjustment['evidence'] == []


@pytest.mark.parametrize('request_policy, clip_policy', [
    ('sentence', 'exact'), ('exact', 'sentence'),
])
def test_clip_boundary_policy_overrides_request_default(
    batch_request, tmp_path, request_policy, clip_policy
):
    from auto_cut_slicing.batch import run_request

    path, request, source = batch_request
    request['boundary_policy'] = request_policy
    request['source']['asr'] = 'sentence.words.json'
    request['outputs'][0].pop('instruction')
    request['outputs'][0]['clips'] = [{
        'id': 'c1', 'start': .2, 'end': .3, 'boundary_policy': clip_policy,
    }]
    _sentence_alignment(source, tmp_path / 'sentence.words.json')
    write_json(path, request)
    result = run_request(path, tmp_path / 'result.json')
    assert result['ok'], result
    expected = 100_000 if clip_policy == 'exact' else 360_000
    assert result['outputs'][0]['expected_duration_us'] == expected


@pytest.mark.parametrize('policy', [None, True, 1, [], {}, 'Sentence', 'unknown'])
@pytest.mark.parametrize('scope', ['request', 'clip'])
def test_invalid_boundary_policy_rejected_before_result_write(
    batch_request, tmp_path, policy, scope
):
    from auto_cut_slicing.batch import ContractError, run_request

    path, request, _ = batch_request
    if scope == 'request':
        request['boundary_policy'] = policy
    else:
        request['outputs'][0].pop('instruction')
        request['outputs'][0]['clips'] = [{
            'id': 'c1', 'start': 0, 'end': .32, 'boundary_policy': policy,
        }]
    write_json(path, request)
    receipt = tmp_path / 'result.json'
    with pytest.raises(ContractError) as captured:
        run_request(path, receipt)
    assert captured.value.code == 'INVALID_REQUEST'
    assert not receipt.exists()
    assert not (tmp_path / '成片/good.mp4').exists()


@pytest.mark.parametrize('legacy_status', ['pending', 'failed'])
def test_legacy_incomplete_receipt_resumes_with_exact_policy(
    batch_request, tmp_path, legacy_status
):
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.io import json_sha256

    path, request, _ = batch_request
    request.pop('boundary_policy')
    write_json(path, request)
    receipt = write_json(tmp_path / 'result.json', {
        'contract_version': 1, 'request_id': request['request_id'],
        'request_sha256': json_sha256(request), 'source_sha256': None,
        'ok': False, 'status': 'failed',
        'outputs': [{'output_id': 'good', 'status': legacy_status}],
    })
    result = run_request(path, receipt, resume=True)
    assert result['ok'], result
    assert result['outputs'][0]['boundary_policy'] == 'exact'
    assert result['outputs'][0]['expected_duration_us'] == 320_000


def test_legacy_complete_receipt_reuses_same_plan_hash_without_sentence_default(
    batch_request, tmp_path
):
    from auto_cut_slicing.batch import run_request
    from auto_cut_slicing.io import json_sha256

    path, request, _ = batch_request
    receipt = tmp_path / 'result.json'
    first = run_request(path, receipt)
    assert first['ok'], first
    output_path = tmp_path / '成片/good.mp4'
    original_mtime = output_path.stat().st_mtime_ns
    request.pop('boundary_policy')
    write_json(path, request)
    legacy = read_json(receipt)
    legacy['request_sha256'] = json_sha256(request)
    legacy['outputs'][0].pop('boundary_policy', None)
    write_json(receipt, legacy)
    result = run_request(path, receipt, resume=True)
    assert result['ok'], result
    assert result['outputs'][0]['boundary_policy'] == 'exact'
    assert result['outputs'][0]['plan_sha256'] == first['outputs'][0]['plan_sha256']
    assert output_path.stat().st_mtime_ns == original_mtime
