import copy
import json

import pytest

from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.io import file_sha256, json_sha256
from auto_cut_slicing.planner import compile_job


@pytest.fixture
def job_factory(tmp_path):
    for filename in ("one.mp4", "two.mp4"):
        (tmp_path / filename).write_bytes(filename.encode())

    def describe(path):
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "duration_us": 10000000,
            "width": 1920,
            "height": 1080,
            "fps": 25,
            "has_audio": True,
        }

    def make(job=None, words=None):
        if job is None:
            job = {
                "schema_version": 1,
                "name": "切片测试",
                "sources": [{"id": "s1", "path": "one.mp4"}],
                "outputs": [
                    {
                        "id": "out",
                        "clips": [
                            {"id": "c1", "source": "s1", "start": 0, "end": 10, "deletions": []}
                        ],
                    }
                ],
            }
        if words is not None:
            asr = {
                "schema_version": 1,
                "source_sha256": file_sha256(tmp_path / "one.mp4"),
                "provider": "test-fixture",
                "model": "test-fixture",
                "adapter": "test-fixture-v1",
                "words": [{"text": text, "start": start, "end": end} for text, start, end in words],
            }
            (tmp_path / "asr.json").write_text(
                json.dumps(asr, ensure_ascii=False), encoding="utf-8"
            )
            job["sources"][0]["asr"] = "asr.json"
        path = tmp_path / "job.json"
        path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return job, path, describe

    return make


def compile_fixture(job_factory, job=None, words=None):
    job, path, describe = job_factory(job, words)
    return compile_job(path, describe=describe)


def test_range_deletion_ripples_duration_and_preserves_verbatim_trace(job_factory):
    job, _, _ = job_factory()
    text = "00:02 删除这里？\n 保持原文。"
    job["outputs"][0]["clips"][0]["deletions"] = [
        {"id": "d1", "kind": "range", "start": 2, "end": 4, "source_text": text}
    ]
    plan = compile_fixture(job_factory, job)
    output = plan["outputs"][0]
    assert output["duration_us"] == 8000000
    assert [
        (row["source_start_us"], row["duration_us"], row["target_start_us"])
        for row in output["segments"]
    ] == [(0, 2000000, 0), (4000000, 6000000, 2000000)]
    assert output["markers"] == [
        {
            "item_id": "c1/d1",
            "source_text": text,
            "source_id": "s1",
            "source_start_us": 2000000,
            "source_end_us": 4000000,
            "target_start_us": 2000000,
            "execution_status": "executed",
        }
    ]
    assert output["edits"][0]["target_join_us"] == 2000000
    assert plan["sources"]["s1"]["duration_us"] == 10000000


def test_word_and_range_requests_use_asr_bounds_then_compress(job_factory):
    job, _, _ = job_factory()
    job["outputs"][0]["clips"][0]["deletions"] = [
        {
            "id": "word",
            "kind": "word",
            "text": "删除这句",
            "rough_time": "00:02",
            "must_keep": ["保留"],
            "source_text": "02秒，删除这句",
        },
        {"id": "paragraph", "kind": "range", "start": 6, "end": 8, "source_text": "删除段落"},
    ]
    plan = compile_fixture(job_factory, job, [("删除", 2.1, 2.5), ("这句", 2.5, 3), ("保留", 3, 4)])
    output = plan["outputs"][0]
    assert output["duration_us"] == 7100000
    assert [
        (edit["source_start_us"], edit["source_end_us"], edit["target_join_us"])
        for edit in output["edits"]
    ] == [(2100000, 3000000, 2100000), (6000000, 8000000, 5100000)]
    assert output["edits"][0]["match_strategy"] == "unique_exact_whole_token"
    assert output["edits"][0]["word_indices"] == [0, 1]


def test_reorder_multisource_concat_and_independent_slice_share_plan(job_factory):
    job, _, _ = job_factory()
    job["sources"].append({"id": "s2", "path": "two.mp4"})
    job["outputs"] = [
        {
            "id": "independent",
            "name": "独立切片",
            "clips": [{"id": "slice", "source": "s1", "start": 1, "end": 3}],
        },
        {
            "id": "concat",
            "clips": [
                {"id": "first", "source": "s2", "start": 5, "end": 8},
                {"id": "second", "source": "s1", "start": 0, "end": 2},
            ],
        },
    ]
    plan = compile_fixture(job_factory, job)
    assert [row["duration_us"] for row in plan["outputs"]] == [2000000, 5000000]
    assert [
        (row["source_id"], row["source_start_us"], row["target_start_us"])
        for row in plan["outputs"][1]["segments"]
    ] == [("s2", 5000000, 0), ("s1", 0, 3000000)]


def test_adjacent_deletions_remain_independent_at_start_and_end(job_factory):
    job, _, _ = job_factory()
    job["outputs"][0]["clips"][0]["deletions"] = [
        {"id": "start", "kind": "range", "start": 0, "end": 1, "source_text": "起始删除"},
        {"id": "next", "kind": "range", "start": 1, "end": 2, "source_text": "相邻删除"},
        {"id": "end", "kind": "range", "start": 8, "end": 10, "source_text": "末尾删除"},
    ]
    output = compile_fixture(job_factory, job)["outputs"][0]
    assert output["duration_us"] == 6000000
    assert len(output["segments"]) == 1
    assert len(output["markers"]) == 3
    assert [row["target_start_us"] for row in output["markers"]] == [0, 0, 5999999]
    assert output["edits"][-1]["target_join_us"] == 6000000


def test_whole_clip_can_be_removed_but_whole_output_cannot(job_factory):
    job, _, _ = job_factory()
    clip = job["outputs"][0]["clips"][0]
    clip["deletions"] = [
        {"id": "all", "kind": "range", "start": 0, "end": 10, "source_text": "整段删除"}
    ]
    with pytest.raises(SlicingError, match="empty after deletion"):
        compile_fixture(job_factory, job)
    job["outputs"][0]["clips"].append({"id": "other", "source": "s1", "start": 0, "end": 1})
    output = compile_fixture(job_factory, job)["outputs"][0]
    assert output["duration_us"] == 1000000
    assert len(output["segments"]) == len(output["markers"]) == 1


def test_overlapping_ranges_fail_before_any_output(job_factory):
    job, _, _ = job_factory()
    job["outputs"][0]["clips"][0]["deletions"] = [
        {"id": "a", "kind": "range", "start": 1, "end": 4, "source_text": "a"},
        {"id": "b", "kind": "range", "start": 3, "end": 5, "source_text": "b"},
    ]
    with pytest.raises(SlicingError, match="Overlapping deletion"):
        compile_fixture(job_factory, job)


def test_must_keep_protection_applies_to_other_range_requests(job_factory):
    job, _, _ = job_factory()
    job["outputs"][0]["clips"][0]["deletions"] = [
        {
            "id": "word",
            "kind": "word",
            "text": "删除",
            "must_keep": ["保留"],
            "source_text": "删词",
        },
        {"id": "range", "kind": "range", "start": 2, "end": 4, "source_text": "删段落"},
    ]
    with pytest.raises(SlicingError, match="overlaps must_keep protected"):
        compile_fixture(job_factory, job, [("删除", 0, 1), ("保留", 2, 3)])


@pytest.mark.parametrize("bad_name", ["../escape", "CON", "x\\y", "x.", "x ", "a:b", "?"])
def test_unsafe_output_names_fail(job_factory, bad_name):
    job, _, _ = job_factory()
    job["outputs"][0]["name"] = bad_name
    with pytest.raises(SlicingError, match="safe local filename"):
        compile_fixture(job_factory, job)


def test_unknown_operations_duplicate_ids_and_invalid_ranges_fail(job_factory):
    original, _, _ = job_factory()
    mutations = []
    bad = copy.deepcopy(original)
    bad["outputs"][0]["clips"][0]["deletions"] = [
        {"id": "x", "kind": "speed", "source_text": "未知"}
    ]
    mutations.append((bad, "Unknown deletion kind"))
    bad = copy.deepcopy(original)
    bad["sources"].append(copy.deepcopy(bad["sources"][0]))
    mutations.append((bad, "Duplicate source ID"))
    bad = copy.deepcopy(original)
    bad["outputs"].append(copy.deepcopy(bad["outputs"][0]))
    mutations.append((bad, "Duplicate output ID"))
    bad = copy.deepcopy(original)
    bad["outputs"][0]["clips"].append(copy.deepcopy(bad["outputs"][0]["clips"][0]))
    mutations.append((bad, "Duplicate clip ID"))
    bad = copy.deepcopy(original)
    bad["outputs"][0]["clips"][0]["source"] = "missing"
    mutations.append((bad, "unknown source"))
    for start, end in ((True, 2), (3, 2), (0, 11), (0.0000001, 2)):
        bad = copy.deepcopy(original)
        bad["outputs"][0]["clips"][0].update(start=start, end=end)
        mutations.append((bad, None))
    for job, message in mutations:
        with pytest.raises(SlicingError, match=message):
            compile_fixture(job_factory, job)


def test_word_deletion_rejects_missing_or_stale_alignment(job_factory):
    job, _, _ = job_factory()
    job["outputs"][0]["clips"][0]["deletions"] = [
        {"id": "word", "kind": "word", "text": "删除", "source_text": "删除这个词"}
    ]
    with pytest.raises(SlicingError, match="requires source-bound ASR"):
        compile_fixture(job_factory, job)
    job, path, describe = job_factory(job, [("删除", 1, 2)])
    asr_path = path.parent / "asr.json"
    asr = json.loads(asr_path.read_text(encoding="utf-8"))
    asr["source_sha256"] = "0" * 64
    asr_path.write_text(json.dumps(asr), encoding="utf-8")
    with pytest.raises(SlicingError, match="does not match"):
        compile_job(path, describe=describe)


@pytest.mark.parametrize("scope", ["job", "source", "output", "clip", "word", "range"])
def test_unsupported_fields_never_silently_drop_instructions(job_factory, scope):
    job, _, _ = job_factory()
    if scope == "job":
        target = job
    elif scope == "source":
        target = job["sources"][0]
    elif scope == "output":
        target = job["outputs"][0]
    elif scope == "clip":
        target = job["outputs"][0]["clips"][0]
    else:
        target = {"id": "delete", "kind": scope, "source_text": "需求"}
        if scope == "word":
            target.update(text="删除", start=1)
        else:
            target.update(start=1, end=2, text="删除")
        job["outputs"][0]["clips"][0]["deletions"] = [target]
    if scope not in ("word", "range"):
        target["speed"] = 2
    with pytest.raises(SlicingError, match="Unsupported"):
        compile_fixture(job_factory, job)


def test_canonical_job_and_plan_digests_are_deterministic(job_factory):
    job, path, describe = job_factory()
    first = compile_job(path, describe=describe)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=4, sort_keys=True), encoding="utf-8")
    second = compile_job(path, describe=describe)
    assert first == second
    digest = first.pop("plan_sha256")
    assert digest == json_sha256(first)
    assert first["job_sha256"] == json_sha256(job)
