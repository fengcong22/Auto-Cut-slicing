import copy
import json

import pytest

from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.io import file_sha256
from auto_cut_slicing.planner import compile_job


@pytest.fixture
def sentence_job(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'sentence-source')
    words = [
        {'text': '上一段', 'start': 1, 'end': 2},
        {'text': '首先', 'start': 2.5, 'end': 3},
        {'text': '内容', 'start': 4, 'end': 5},
        {'text': '尾句', 'start': 7, 'end': 8},
    ]
    manifest = {
        'schema_version': 1, 'source_sha256': file_sha256(source),
        'provider': 'fixture', 'model': 'fixture', 'adapter': 'fixture', 'words': words,
        'utterances': [
            {'text': '上一段，首先内容。', 'start': 1, 'end': 5, 'words': words[:3]},
            {'text': '尾句。', 'start': 7, 'end': 8, 'words': words[3:]},
        ],
    }
    (tmp_path / 'words.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    job = {
        'schema_version': 1, 'name': 'sentence-test',
        'sources': [{'id': 's', 'path': str(source), 'asr': 'words.json'}],
        'outputs': [{'id': 'out', 'clips': [
            {'id': 'c', 'source': 's', 'start': 2.5, 'end': 4.2, 'boundary_policy': 'sentence'},
        ]}],
    }

    def compile(value=None):
        path = tmp_path / 'job.json'
        path.write_text(json.dumps(value or job, ensure_ascii=False), encoding='utf-8')
        return compile_job(path, describe=lambda p: {
            'sha256': file_sha256(p), 'duration_us': 10_000_000,
            'width': 1920, 'height': 1080, 'fps': 25, 'has_audio': True,
        })['outputs'][0]

    return job, compile


def test_sentence_policy_expands_both_edges_with_source_bound_receipt(sentence_job):
    _, compile = sentence_job
    output = compile()
    assert output['segments'][0]['source_start_us'] == 1_000_000
    assert output['duration_us'] == 4_000_000
    receipt = output['boundary_adjustments'][0]
    assert receipt['requested_start_us'] == 2_500_000
    assert receipt['requested_end_us'] == 4_200_000
    assert receipt['resolved_start_us'] == 1_000_000
    assert receipt['resolved_end_us'] == 5_000_000
    assert receipt['clip_id'] == 'c' and receipt['source_id'] == 's'
    assert receipt['policy'] == 'sentence'
    assert {row['side'] for row in receipt['evidence']} == {'start', 'end'}
    assert all(row['sentence_text'] == '上一段，首先内容。' for row in receipt['evidence'])
    assert all(row['alignment_sha256'] for row in receipt['evidence'])


@pytest.mark.parametrize('end', [3, 3.5, 4.2])
def test_internal_word_end_or_pause_still_completes_sentence(sentence_job, end):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0].update(start=1, end=end)
    assert compile()['duration_us'] == 4_000_000


@pytest.mark.parametrize('start,end', [(1, 5), (5, 7), (5.5, 7), (7, 8)])
def test_sentence_edges_and_between_sentence_gaps_stay_exact(sentence_job, start, end):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0].update(start=start, end=end)
    output = compile()
    assert output['segments'][0]['source_start_us'] == start * 1_000_000
    assert output['duration_us'] == (end - start) * 1_000_000


def test_explicit_cue_has_priority_and_end_resolves_before_cue(sentence_job):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0].update(start=2.6, end=2.8, start_cue={'text': '首先'})
    output = compile()
    assert output['segments'][0]['source_start_us'] == 2_500_000
    assert output['duration_us'] == 2_500_000
    assert output['start_cues'][0]['requested_start_us'] == 2_600_000
    assert output['boundary_adjustments'][0]['resolved_start_us'] == 2_500_000


def test_range_effect_coordinates_do_not_move_when_sentence_start_expands(sentence_job):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0]['edits'] = [{
        'id': 'freeze', 'kind': 'freeze_frame', 'start': 2.5, 'end': 4,
        'frame_time': 6, 'source_text': '画面替换原文。',
    }]
    output = compile()
    assert output['effects'][0]['source_start_us'] == 2_500_000
    assert output['effects'][0]['target_start_us'] == 1_500_000


def test_missing_asr_rejects_interior_semantic_boundary_but_allows_full_source(sentence_job):
    job, compile = sentence_job
    del job['sources'][0]['asr']
    with pytest.raises(SlicingError, match='ASR'):
        compile()
    job['outputs'][0]['clips'][0].update(start=0, end=10)
    assert compile()['duration_us'] == 10_000_000


def test_exact_and_legacy_clip_shapes_stay_compatible(sentence_job):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0]['boundary_policy'] = 'exact'
    explicit = compile()
    assert explicit['duration_us'] == 1_700_000
    assert 'boundary_adjustments' not in explicit
    del job['outputs'][0]['clips'][0]['boundary_policy']
    assert compile() == explicit


def test_boundary_expansion_rejects_new_overlap_but_allows_intentional_reuse(sentence_job):
    job, compile = sentence_job
    first = job['outputs'][0]['clips'][0]
    first.update(start=1, end=2)
    second = copy.deepcopy(first)
    second.update(id='second', start=4, end=5)
    job['outputs'][0]['clips'].append(second)
    with pytest.raises(SlicingError, match='overlap'):
        compile()
    second.update(start=1.5, end=2.5)
    assert compile()['duration_us'] == 8_000_000


def test_preserved_cue_reuse_is_not_misidentified_as_sentence_expansion(sentence_job):
    job, compile = sentence_job
    first = job['outputs'][0]['clips'][0]
    first.update(start=2.5, end=3, boundary_policy='exact')
    second = copy.deepcopy(first)
    second.update(id='second', start=3, end=5, boundary_policy='sentence',
                  start_cue={'text': '首先'})
    job['outputs'][0]['clips'].append(second)
    assert compile()['duration_us'] == 3_000_000


@pytest.mark.parametrize('policy', [None, True, 'word', {}])
def test_unknown_boundary_policy_fails(sentence_job, policy):
    job, compile = sentence_job
    job['outputs'][0]['clips'][0]['boundary_policy'] = policy
    with pytest.raises(SlicingError, match='boundary_policy'):
        compile()
