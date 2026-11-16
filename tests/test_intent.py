"""Edit intent validation/compilation, evidence tools and QA on synthetic media and ASR.

The synthetic ASR checks deterministic mechanics only; it does not certify recognition accuracy.
"""

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from auto_cut_slicing import evidence
from auto_cut_slicing.context import EditContext
from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.intent import (
    IntentError,
    compile_intent,
    parse_ordinal,
    parse_user_time,
    validate,
)
from auto_cut_slicing.io import file_sha256, read_json, write_json
from auto_cut_slicing.render import render_output
from auto_cut_slicing.schema import load_schema, schema_errors

ROOT = Path(__file__).resolve().parents[1]
RAW = "0:00-0:05.60，删掉第二遍的“我们再看一下”"
UTTERANCES = [
    ("我们开始。", [("我们", 0.2, 0.5), ("开始", 0.5, 0.9)]),
    ("我们再看一下这个实验。", [("我们", 1.2, 1.4), ("再看一下", 1.4, 1.9), ("这个", 1.9, 2.1), ("实验", 2.1, 2.5)]),
    ("然后我们再看一下结果。", [("然后", 3.0, 3.3), ("我们", 3.3, 3.5), ("再看一下", 3.5, 4.0), ("结果", 4.0, 4.4)]),
    ("最后总结。", [("最后", 4.9, 5.2), ("总结", 5.2, 5.6)]),
]


def _words(utterances):
    return [{"text": t, "start": a, "end": b} for _, rows in utterances for t, a, b in rows]


def _asr(sha, utterances):
    return {
        "schema_version": 1, "source_sha256": sha, "provider": "synthetic-fixture",
        "model": "synthetic-fixture", "adapter": "synthetic-fixture", "words": _words(utterances),
        "utterances": [{"text": text, "words": [{"text": t, "start": a, "end": b} for t, a, b in rows]}
                       for text, rows in utterances],
    }


@pytest.fixture
def case(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required for intent media fixtures")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(source)], check=True, capture_output=True)
    sha = file_sha256(source)
    asr = write_json(tmp_path / "source.words.json", _asr(sha, UTTERANCES))
    context_path = write_json(tmp_path / "context.json", {
        "schema": "auto-cut-slicing/intent-context", "schema_version": 1,
        "timeline_basis": "local_source",
        "sources": [{"source_id": "talk", "path": "source.mp4", "asr": "source.words.json"}],
        "instructions": [{"instruction_id": "i1", "raw_text": RAW}],
        "evidence_dir": "evidence",
    })
    context = EditContext(context_path)
    return {"dir": tmp_path, "context": context, "context_path": context_path, "sha": sha,
            "alignment_sha": file_sha256(asr), "duration": context.source("talk")["duration_us"]}


def _span(text, start=0):
    index = RAW.index(text, start)
    return {"instruction_id": "i1", "start": index, "end": index + len(text)}


def _anchor(text, us):
    return {"ref": "user_anchor", "value_us": us, "span": _span(text)}


def _intent(case, **change):
    clip_span, delete_span = _span("0:00-0:05.60"), _span("删掉第二遍的“我们再看一下”")
    phrase = {
        "source_id": "talk", "alignment_sha256": case["alignment_sha"],
        "first_index": 7, "last_index": 8, "text": "我们再看一下",
        "selection": {
            "method": "user_ordinal", "scope_start_us": 0, "scope_end_us": 5_600_000,
            "candidates": [
                {"first_index": 2, "last_index": 3, "start_us": 1_200_000, "end_us": 1_900_000},
                {"first_index": 7, "last_index": 8, "start_us": 3_300_000, "end_us": 4_000_000},
            ],
            "evidence_id": None, "ordinal": 2, "ordinal_span": _span("第二遍"),
        },
    }
    intent = {
        "schema": "auto-cut-slicing/edit-intent", "schema_version": 1, "status": "proposed",
        "author": {"kind": "ai", "agent": "test", "model": None, "session_id": None},
        "bindings": {"timeline_basis": "local_source", "sources": [{
            "source_id": "talk", "sha256": case["sha"], "duration_us": case["duration"],
            "alignment_sha256": case["alignment_sha"]}], "evidence_manifest_sha256": None},
        "instructions": [{"instruction_id": "i1", "raw_text": RAW,
                          "raw_text_sha256": hashlib.sha256(RAW.encode()).hexdigest()}],
        "coverage": [
            {"span": clip_span, "quote": "0:00-0:05.60", "disposition": "operation",
             "intent_ids": ["clip-1"], "reason": "保留范围"},
            {"span": delete_span, "quote": "删掉第二遍的“我们再看一下”", "disposition": "operation",
             "intent_ids": ["del-1"], "reason": "第二次出现，音画同删"},
        ],
        "outputs": [{"output_id": "out", "clips": [{
            "intent_id": "clip-1", "source_id": "talk", "covers": [clip_span],
            "start": {"mode": "time", "at": _anchor("0:00", 0)},
            "end": {"mode": "time", "at": _anchor("0:05.60", 5_600_000)},
            "operations": [{"intent_id": "del-1", "kind": "delete_phrase", "covers": [delete_span],
                            "phrase": phrase, "rationale": "第二遍"}],
            "must_keep": [], "rationale": "test"}]}],
        "blockers": [],
    }
    for key, value in change.items():
        intent[key] = value
    return intent


def _write(case, intent, name="intent.json"):
    return write_json(case["dir"] / name, intent)


def _codes(result):
    return {(row["rule"], row["code"]) for row in result["problems"]}


def test_repository_example_and_schema_keywords_validate():
    assert schema_errors(read_json(ROOT / "examples" / "edit-intent.json"), "edit-intent.v1.schema.json") == []
    for name in ("edit-intent.v1.schema.json", "evidence-manifest.v1.schema.json",
                 "qa-report.v1.schema.json", "intent-context.v1.schema.json"):
        assert load_schema(name)["$schema"].endswith("2020-12/schema")


def test_user_times_and_ordinals_parse_without_guessing():
    assert parse_user_time("4：56") == 296_000_000
    assert parse_user_time("第六秒") == 6_000_000
    assert parse_user_time("4分34") == 274_000_000
    assert parse_user_time("0:05.60") == 5_600_000
    assert parse_ordinal("第二遍") == (2, False)
    assert parse_ordinal("最后一次") == (1, True)
    assert parse_ordinal("倒数第二") == (2, True)
    with pytest.raises(SlicingError):
        parse_user_time("大概五秒左右")


def test_ordinal_selection_compiles_to_the_selected_repeat(case):
    path = _write(case, _intent(case))
    assert validate(path, case["context"])["compilable"] is True
    result = compile_intent(path, case["context"], case["dir"] / "job.json", case["dir"] / "receipt.json",
                            case["dir"] / "plan.json")
    deletion = read_json(case["dir"] / "job.json")["outputs"][0]["clips"][0]["deletions"][0]
    assert deletion["text"] == "我们再看一下" and deletion["rough_time"] == 3.3
    assert deletion["search_radius"] < 2.1 and deletion["source_text"] == RAW
    edit = read_json(case["dir"] / "plan.json")["outputs"][0]["edits"][0]
    assert (edit["source_start_us"], edit["source_end_us"]) == (3_300_000, 4_000_000)
    assert result["receipt"]["outputs"][0]["duration_us"] == 4_900_000


def test_choosing_among_repeats_without_user_evidence_is_rejected(case):
    intent = _intent(case)
    selection = intent["outputs"][0]["clips"][0]["operations"][0]["phrase"]["selection"]
    selection["method"] = "unique_in_scope"
    del selection["ordinal"], selection["ordinal_span"]
    result = validate(_write(case, intent), case["context"])
    assert ("V08", "INTENT_AMBIGUOUS_MATCH") in _codes(result)
    with pytest.raises(IntentError):
        compile_intent(case["dir"] / "intent.json", case["context"], case["dir"] / "j.json", case["dir"] / "r.json")
    assert not (case["dir"] / "j.json").exists()


def test_candidates_must_equal_the_alignment(case):
    intent = _intent(case)
    intent["outputs"][0]["clips"][0]["operations"][0]["phrase"]["selection"]["candidates"].pop(0)
    assert ("V08", "INTENT_AMBIGUOUS_MATCH") in _codes(validate(_write(case, intent), case["context"]))


def test_time_without_a_matching_source_is_rejected(case):
    intent = _intent(case)
    intent["outputs"][0]["clips"][0]["end"]["at"]["value_us"] = 5_500_000
    assert ("V07", "INTENT_TIME_UNSOURCED") in _codes(validate(_write(case, intent), case["context"]))


def test_uncovered_feedback_is_reported(case):
    intent = _intent(case)
    intent["coverage"].pop(1)
    codes = _codes(validate(_write(case, intent), case["context"]))
    assert ("V05", "INTENT_UNCOVERED_TEXT") in codes and ("V05", "INTENT_CONFLICT") in codes


def test_changed_raw_feedback_is_rejected(case):
    intent = _intent(case)
    intent["instructions"][0]["raw_text"] = RAW + "。"
    assert ("V02", "INTENT_CONFLICT") in _codes(validate(_write(case, intent), case["context"]))


def test_blocked_intent_is_valid_but_not_compilable(case):
    intent = _intent(case, status="blocked", outputs=[], blockers=[{
        "code": "INTENT_AMBIGUOUS_MATCH", "message": "两处候选", "spans": [_span("第二遍")],
        "missing_evidence": [], "candidates": []}])
    intent["coverage"][1]["disposition"] = "ambiguous"
    intent["coverage"][0]["disposition"] = "context"
    intent["coverage"][0]["intent_ids"] = intent["coverage"][1]["intent_ids"] = []
    path = _write(case, intent)
    result = validate(path, case["context"])
    assert result["ok"] and not result["compilable"]
    with pytest.raises(SlicingError, match="blocked"):
        compile_intent(path, case["context"], case["dir"] / "j.json", case["dir"] / "r.json")


def test_evidence_lists_every_candidate_and_never_overwrites(case):
    result = evidence.find_phrase(case["context"], "talk", "phrase-1", "我们再看一下", anchor_us=3_000_000)
    payload = read_json(result["path"])
    assert [row["first_index"] for row in payload["candidates"]] == [2, 7]
    assert payload["candidates"][1]["before"].endswith("然后")
    with pytest.raises(SlicingError, match="already exists"):
        evidence.find_phrase(case["context"], "talk", "phrase-1", "我们")
    window = read_json(evidence.transcript_window(case["context"], "talk", "tw", 1_000_000, 4_500_000)["path"])
    assert [row["text"] for row in window["sentences"]] == ["我们再看一下这个实验。", "然后我们再看一下结果。"]
    frames = evidence.frames(case["context"], "talk", "fr", [1_000_000], neighbors=1)
    assert frames["evidence"]["frame_times_us"] == [960_000, 1_000_000, 1_040_000]
    manifest = read_json(case["dir"] / "evidence" / "manifest.json")
    assert [row["evidence_id"] for row in manifest["entries"]] == ["phrase-1", "tw", "fr"]


def test_cited_evidence_must_exist_and_bind_the_manifest(case):
    evidence.find_phrase(case["context"], "talk", "phrase-1", "我们再看一下")
    intent = _intent(case)
    intent["outputs"][0]["clips"][0]["operations"][0]["phrase"]["selection"]["evidence_id"] = "phrase-1"
    assert ("V04", "INTENT_EVIDENCE_MISSING") in _codes(validate(_write(case, intent), case["context"]))
    intent["bindings"]["evidence_manifest_sha256"] = file_sha256(case["dir"] / "evidence" / "manifest.json")
    assert validate(_write(case, intent), case["context"])["ok"]


def _render(case, intent, tag):
    path = _write(case, intent, f"{tag}.json")
    compile_intent(path, case["context"], case["dir"] / f"{tag}-job.json", case["dir"] / f"{tag}-r.json",
                   case["dir"] / f"{tag}-plan.json")
    plan = read_json(case["dir"] / f"{tag}-plan.json")
    video = case["dir"] / f"{tag}.mp4"
    render_output(plan, "out", video)
    return path, case["dir"] / f"{tag}-plan.json", video


def test_qa_confirms_deletion_with_output_asr_and_flags_review_items(case):
    from auto_cut_slicing.qa import run_qa

    path, plan, video = _render(case, _intent(case), "good")
    shifted = copy.deepcopy(UTTERANCES)
    shifted[2] = ("然后结果。", [("然后", 3.0, 3.3), ("结果", 3.3, 3.7)])
    shifted[3] = ("最后总结。", [("最后", 4.2, 4.5), ("总结", 4.5, 4.9)])
    video_asr = write_json(case["dir"] / "good.words.json", _asr(file_sha256(video), shifted))
    result = run_qa(case["context"], path, plan, "out", video, video_asr, case["dir"] / "qa.json")
    report = read_json(case["dir"] / "qa.json")
    status = {row["kind"]: row["status"] for row in report["checks"]}
    assert result["overall"] == "ready_for_review"
    assert status["phrase_absent"] == "passed" and status["duration_match"] == "passed"
    assert status["cut_not_inside_word"] == "passed" and status["listening"] == "needs_human"
    assert schema_errors(report, "qa-report.v1.schema.json") == []


def test_qa_fails_an_ending_cut_inside_the_last_word(case):
    from auto_cut_slicing.qa import run_qa

    raw = "0:00-0:05.30"
    context_data = read_json(case["context_path"])
    context_data["instructions"][0]["raw_text"] = raw
    write_json(case["context_path"], context_data)
    context = EditContext(case["context_path"])
    span = {"instruction_id": "i1", "start": 0, "end": len(raw)}
    intent = _intent(case)
    intent["instructions"][0].update(raw_text=raw, raw_text_sha256=hashlib.sha256(raw.encode()).hexdigest())
    intent["coverage"] = [{"span": span, "quote": raw, "disposition": "operation",
                           "intent_ids": ["clip-1"], "reason": "range"}]
    clip = intent["outputs"][0]["clips"][0]
    clip.update(covers=[span], operations=[])
    clip["start"]["at"]["span"] = {"instruction_id": "i1", "start": 0, "end": 4}
    clip["end"]["at"] = {"ref": "user_anchor", "value_us": 5_300_000,
                         "span": {"instruction_id": "i1", "start": 5, "end": len(raw)}}
    case["context"] = context
    path, plan, video = _render(case, intent, "cut")
    result = run_qa(context, path, plan, "out", video, None, case["dir"] / "qa-cut.json")
    checks = read_json(case["dir"] / "qa-cut.json")["checks"]
    failed = {row["kind"] for row in checks if row["status"] == "failed"}
    assert result["overall"] == "failed"
    assert {"cut_not_inside_word", "sentence_complete"} <= failed
    assert json.dumps(checks, ensure_ascii=False).count("总结") >= 1


def test_cli_validates_and_compiles_intent(case, capsys):
    from auto_cut_slicing.cli import main

    path = _write(case, _intent(case))
    assert main(["intent", "validate", str(path), "--context", str(case["context_path"])]) == 0
    assert json.loads(capsys.readouterr().out)["compilable"] is True
    assert main(["evidence", "find-phrase", "--context", str(case["context_path"]), "--source", "talk",
                 "--id", "p", "--text", "我们再看一下", "--anchor", "3.3"]) == 0
    assert json.loads(capsys.readouterr().out)["evidence"]["kind"] == "phrase_search"
    bad = _intent(case)
    bad["outputs"][0]["clips"][0]["end"]["at"]["value_us"] = 1
    assert main(["intent", "compile", str(_write(case, bad, "bad.json")), "--context",
                 str(case["context_path"]), "--job", str(case["dir"] / "j.json"),
                 "--receipt", str(case["dir"] / "r.json")]) == 1
    assert json.loads(capsys.readouterr().out)["problems"][0]["rule"] == "V07"
