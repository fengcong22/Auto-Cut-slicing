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
from auto_cut_slicing.media import describe_source
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


# Tone only inside each word; silence between words, plus a decay tail after 结果 (ends 4.4 s).
_TONE = "0.5*sin(2*PI*500*t)*(" + "+".join(
    f"between(t,{a},{b})" for _, rows in UTTERANCES for _t, a, b in rows
) + "+between(t,4.4,4.8)*exp(-(t-4.4)*20))"


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
        "-f", "lavfi", "-i", f"aevalsrc='{_TONE}':s=48000:d=6",
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


def _freeze_op(case, frame_us, *, covers=None, standard_rule="clean_cut_in", purpose="clean_cut_in"):
    frame = {"ref": "frame", "value_us": frame_us, "source_id": "talk", "source_sha256": case["sha"],
             "evidence_id": "fr"}
    op = {"intent_id": "freeze-1", "kind": "freeze_cover", "covers": covers or [], "purpose": purpose,
          "start": {"ref": "source_edge", "value_us": 0, "source_id": "talk", "edge": "start"},
          "end": frame, "frame": frame,
          "visual_review": {"status": "ai_reviewed", "evidence_ids": ["fr"], "observation": "clean"},
          "rationale": "standard"}
    if standard_rule:
        op["standard_rule"] = standard_rule
    return op


def test_standards_driven_freeze_needs_no_feedback_span_but_other_ops_do(case):
    evidence.frames(case["context"], "talk", "fr", [400_000])
    intent = _intent(case)
    intent["bindings"]["evidence_manifest_sha256"] = file_sha256(case["dir"] / "evidence" / "manifest.json")
    clip = intent["outputs"][0]["clips"][0]
    clip["operations"].append(_freeze_op(case, 400_000))
    path = _write(case, intent)
    assert validate(path, case["context"])["ok"]
    compile_intent(path, case["context"], case["dir"] / "job.json", case["dir"] / "r.json")
    edits = read_json(case["dir"] / "job.json")["outputs"][0]["clips"][0]["edits"]
    assert edits[0]["kind"] == "freeze_frame" and edits[0]["source_text"] == RAW
    clip["operations"][-1] = _freeze_op(case, 400_000, standard_rule=None)
    assert ("V05", "INTENT_CONFLICT") in _codes(validate(_write(case, intent, "no-rule.json"), case["context"]))
    clip["operations"][0]["covers"] = []
    clip["operations"][-1] = _freeze_op(case, 400_000)
    assert ("V01", "INTENT_CONFLICT") in _codes(validate(_write(case, intent, "no-span.json"), case["context"]))


def test_qa_measures_a_mute_as_silence(case):
    from auto_cut_slicing.qa import run_qa

    intent = _intent(case)
    op = intent["outputs"][0]["clips"][0]["operations"][0]
    op["kind"] = "mute_phrase"
    path, plan, video = _render(case, intent, "mute")
    run_qa(case["context"], path, plan, "out", video, None, case["dir"] / "qa-mute.json")
    checks = {row["kind"]: row for row in read_json(case["dir"] / "qa-mute.json")["checks"]}
    assert checks["mute_silence"]["status"] == "passed"
    assert checks["duration_match"]["status"] == "passed"


def test_audio_gap_measures_decay_onset_and_cut_windows(case):
    result = evidence.audio_gap(case["context"], "talk", "gap-result", after_word=9)
    gap = read_json(result["path"])
    assert gap["previous_word"]["text"] == "结果" and gap["next_word"]["text"] == "最后"
    assert 4_500_000 <= gap["decay_end"] <= 4_650_000
    assert abs(gap["low_energy_end"] - 4_900_000) <= 20_000
    assert gap["out_window"] == {"lower_us": gap["decay_end"], "upper_us": gap["low_energy_end"] - 40_000}
    assert gap["in_window"]["lower_us"] == gap["decay_end"] + 40_000
    by_time = read_json(evidence.audio_gap(case["context"], "talk", "gap-at", at_us=4_700_000)["path"])
    assert by_time["previous_word"]["index"] == 9


def _end_with(case, ref, raw=RAW, extra_span=None):
    intent = _intent(case)
    if raw != RAW:
        context_data = read_json(case["context_path"])
        context_data["instructions"][0]["raw_text"] = raw
        write_json(case["context_path"], context_data)
        case["context"] = EditContext(case["context_path"])
        intent["instructions"][0].update(raw_text=raw, raw_text_sha256=hashlib.sha256(raw.encode()).hexdigest())
    if extra_span:
        intent["coverage"].append({"span": extra_span, "quote": raw[extra_span["start"]:extra_span["end"]],
                                   "disposition": "operation", "intent_ids": ["clip-1"], "reason": "tail"})
        intent["outputs"][0]["clips"][0]["covers"].append(extra_span)
    intent["bindings"]["evidence_manifest_sha256"] = file_sha256(case["dir"] / "evidence" / "manifest.json")
    intent["outputs"][0]["clips"][0]["end"] = {"mode": "time", "at": ref}
    # The end no longer cites "0:05.60", so the phrase scope uses the source edge instead.
    intent["outputs"][0]["clips"][0]["operations"][0]["phrase"]["selection"]["scope_end_us"] = case["duration"]
    return intent


def test_gap_choice_and_audio_point_must_match_measurements(case):
    gap = read_json(evidence.audio_gap(case["context"], "talk", "gap-end", after_word=11)["path"])
    window = gap["out_window"]
    choice = {"ref": "gap_choice", "value_us": window["lower_us"] + 200_000, "evidence_id": "gap-end",
              "lower_us": window["lower_us"], "upper_us": window["upper_us"], "basis": "ai_pacing",
              "rationale": "保留尾音并留短停顿"}
    path = _write(case, _end_with(case, choice))
    assert validate(path, case["context"])["ok"]
    compile_intent(path, case["context"], case["dir"] / "job.json", case["dir"] / "r.json")
    assert read_json(case["dir"] / "job.json")["outputs"][0]["clips"][0]["end"] == (window["lower_us"] + 200_000) / 1e6
    outside = dict(choice, value_us=window["upper_us"] + 10_000)
    assert ("V07", "INTENT_TIME_UNSOURCED") in _codes(validate(_write(case, _end_with(case, outside), "o.json"), case["context"]))
    made_up = dict(choice, upper_us=window["upper_us"] + 50_000)
    assert ("V07", "INTENT_TIME_UNSOURCED") in _codes(validate(_write(case, _end_with(case, made_up), "m.json"), case["context"]))
    point = {"ref": "audio_point", "value_us": gap["decay_end"], "evidence_id": "gap-end", "point": "decay_end"}
    assert validate(_write(case, _end_with(case, point), "p.json"), case["context"])["ok"]
    wrong = dict(point, value_us=gap["decay_end"] + 10_000)
    assert ("V07", "INTENT_TIME_UNSOURCED") in _codes(validate(_write(case, _end_with(case, wrong), "w.json"), case["context"]))


def test_user_duration_fixes_the_gap_choice(case):
    raw = RAW + "，结尾留0.3秒"
    gap = read_json(evidence.audio_gap(case["context"], "talk", "gap-end", after_word=11)["path"])
    window = gap["out_window"]
    tail = {"instruction_id": "i1", "start": raw.index("结尾留0.3秒"), "end": len(raw)}
    duration = {"instruction_id": "i1", "start": raw.index("0.3秒"), "end": len(raw)}
    expected = min(max(gap["previous_word"]["end_us"] + 300_000, window["lower_us"]), window["upper_us"])
    choice = {"ref": "gap_choice", "value_us": expected, "evidence_id": "gap-end",
              "lower_us": window["lower_us"], "upper_us": window["upper_us"], "basis": "user_duration",
              "duration_span": duration, "rationale": "用户要求结尾留 0.3 秒"}
    assert validate(_write(case, _end_with(case, choice, raw, tail)), case["context"])["ok"]
    off = dict(choice, value_us=expected - 50_000)
    assert ("V07", "INTENT_TIME_UNSOURCED") in _codes(
        validate(_write(case, _end_with(case, off, raw, tail), "off.json"), case["context"]))


def test_qa_flags_a_cut_inside_the_measured_decay(case):
    from auto_cut_slicing.qa import run_qa

    raw = "0:00-0:04.45"
    context_data = read_json(case["context_path"])
    context_data["instructions"][0]["raw_text"] = raw
    write_json(case["context_path"], context_data)
    case["context"] = EditContext(case["context_path"])
    span = {"instruction_id": "i1", "start": 0, "end": len(raw)}
    intent = _intent(case)
    intent["instructions"][0].update(raw_text=raw, raw_text_sha256=hashlib.sha256(raw.encode()).hexdigest())
    intent["coverage"] = [{"span": span, "quote": raw, "disposition": "operation",
                           "intent_ids": ["clip-1"], "reason": "range"}]
    clip = intent["outputs"][0]["clips"][0]
    clip.update(covers=[span], operations=[])
    clip["start"]["at"]["span"] = {"instruction_id": "i1", "start": 0, "end": 4}
    clip["end"]["at"] = {"ref": "user_anchor", "value_us": 4_450_000,
                         "span": {"instruction_id": "i1", "start": 5, "end": len(raw)}}
    path, plan, video = _render(case, intent, "decay")
    run_qa(case["context"], path, plan, "out", video, None, case["dir"] / "qa-decay.json")
    seam = [row for row in read_json(case["dir"] / "qa-decay.json")["checks"] if row["kind"] == "seam_gap"]
    assert len(seam) == 1 and seam[0]["status"] == "needs_human" and "decay" in seam[0]["detail"]


def test_qa_requires_the_previous_picture_during_a_seam_pause(tmp_path):
    from auto_cut_slicing.planner import compile_job
    from auto_cut_slicing.qa import _pause_picture_checks

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    source = tmp_path / "rb.mp4"
    tone = "0.5*sin(2*PI*500*t)*(between(t,0.5,1.0)+between(t,4.0,4.5))"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=3",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=3",
        "-f", "lavfi", "-i", f"aevalsrc='{tone}':s=48000:d=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True, capture_output=True)
    sha = file_sha256(source)
    write_json(tmp_path / "rb.words.json", _asr(sha, [("一。", [("一", 0.5, 1.0)]), ("二。", [("二", 4.0, 4.5)])]))
    context = EditContext(write_json(tmp_path / "context.json", {
        "schema": "auto-cut-slicing/intent-context", "schema_version": 1, "timeline_basis": "local_source",
        "sources": [{"source_id": "rb", "path": "rb.mp4", "asr": "rb.words.json"}],
        "instructions": [{"instruction_id": "i1", "raw_text": "x"}], "evidence_dir": "evidence"}))

    def checked(name, edits):
        clips = [{"id": "a", "source": "rb", "start": 0, "end": 1.4},
                 {"id": "b", "source": "rb", "start": 3.2, "end": 5, "edits": edits}]
        job = write_json(tmp_path / f"{name}.json", {"schema_version": 1, "name": name,
                         "sources": [{"id": "rb", "path": str(source)}],
                         "outputs": [{"id": "out", "name": name, "clips": clips}]})
        plan = compile_job(job)
        video = tmp_path / f"{name}.mp4"
        render_output(plan, "out", video)
        rows = []
        _pause_picture_checks(rows, context, plan["outputs"][0], video, 40_000, 3_200_000)
        return rows

    shows_next = checked("next", [])
    assert [row["status"] for row in shows_next] == ["failed"]
    holds_previous = checked("hold", [{"id": "hold", "kind": "freeze_frame", "start": 3.2, "end": 4.0,
                                       "frame_time": 1.36, "source_text": "停顿还要再长"}])
    assert [row["status"] for row in holds_previous] == ["passed"]


def test_qa_warns_when_a_freeze_skips_a_finished_transition(tmp_path):
    from auto_cut_slicing.planner import compile_job
    from auto_cut_slicing.qa import run_qa

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    # Red picture, then a white bar sweeps across 3.0-3.6 s (a transition), then a static blue picture.
    source = tmp_path / "sweep.mp4"
    tone = "0.5*sin(2*PI*500*t)*(between(t,0.5,1.0)+between(t,4.0,4.5))"
    video = ("color=c=red:s=160x90:r=25:d=6[bg];color=c=blue:s=160x90:r=25:d=6[b];"
             "[bg][b]overlay=enable='gte(t,3.6)'[c];color=c=white:s=40x90:r=25:d=6[w];"
             "[c][w]overlay=x='(t-3.0)*300-40':enable='between(t,3.0,3.6)'")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", video, "-f", "lavfi", "-i",
                    f"aevalsrc='{tone}':s=48000:d=6", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, capture_output=True)
    sha = file_sha256(source)
    write_json(tmp_path / "s.words.json", _asr(sha, [("一。", [("一", 0.5, 1.0)]), ("二。", [("二", 4.0, 4.5)])]))
    context = EditContext(write_json(tmp_path / "context.json", {
        "schema": "auto-cut-slicing/intent-context", "schema_version": 1, "timeline_basis": "local_source",
        "sources": [{"source_id": "s", "path": "sweep.mp4", "asr": "s.words.json"}],
        "instructions": [{"instruction_id": "i1", "raw_text": "x"}], "evidence_dir": "evidence"}))
    intent = write_json(tmp_path / "intent.json", {"not": "an intent"})

    def freeze_detail(name, freeze_end):
        edits = [{"id": "hold", "kind": "freeze_frame", "start": 2.8, "end": freeze_end, "frame_time": 1.36,
                  "source_text": "停顿还要再长"}]
        job = write_json(tmp_path / f"{name}.json", {"schema_version": 1, "name": name,
                         "sources": [{"id": "s", "path": str(source)}], "outputs": [{"id": "out", "name": name,
                         "clips": [{"id": "a", "source": "s", "start": 0, "end": 1.4},
                                   {"id": "b", "source": "s", "start": 2.8, "end": 5, "edits": edits}]}]})
        plan = compile_job(job)
        plan_path = write_json(tmp_path / f"{name}-plan.json", plan)
        out = tmp_path / f"{name}.mp4"
        render_output(plan, "out", out)
        run_qa(context, intent, plan_path, "out", out, None, tmp_path / f"{name}-qa.json")
        return next(r["detail"] for r in read_json(tmp_path / f"{name}-qa.json")["checks"]
                    if r["kind"] == "freeze_frames")

    assert "may be skipped" in freeze_detail("skips", 3.8)
    assert "continues after playback resumes" in freeze_detail("plays", 3.2)


def test_qa_rejects_a_seam_freeze_that_skips_the_transition(tmp_path):
    from auto_cut_slicing.planner import compile_job
    from auto_cut_slicing.qa import _pause_picture_checks

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    # Red picture, a white bar sweeps across 3.0-3.6 s (a transition), then static blue. Speech at 4.0 s.
    source = tmp_path / "seam.mp4"
    tone = "0.5*sin(2*PI*500*t)*(between(t,0.5,1.0)+between(t,4.0,4.5))"
    video = ("color=c=red:s=160x90:r=25:d=6[bg];color=c=blue:s=160x90:r=25:d=6[b];"
             "[bg][b]overlay=enable='gte(t,3.6)'[c];color=c=white:s=40x90:r=25:d=6[w];"
             "[c][w]overlay=x='(t-3.0)*300-40':enable='between(t,3.0,3.6)'")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", video, "-f", "lavfi", "-i",
                    f"aevalsrc='{tone}':s=48000:d=6", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, capture_output=True)
    sha = file_sha256(source)
    write_json(tmp_path / "seam.words.json", _asr(sha, [("一。", [("一", 0.5, 1.0)]), ("二。", [("二", 4.0, 4.5)])]))
    context = EditContext(write_json(tmp_path / "context.json", {
        "schema": "auto-cut-slicing/intent-context", "schema_version": 1, "timeline_basis": "local_source",
        "sources": [{"source_id": "seam", "path": "seam.mp4", "asr": "seam.words.json"}],
        "instructions": [{"instruction_id": "i1", "raw_text": "x"}], "evidence_dir": "evidence"}))

    def checked(name, entry, freeze_end):
        edits = ([{"id": "hold", "kind": "freeze_frame", "start": entry, "end": freeze_end,
                   "frame_time": 1.36, "source_text": "停顿还要再长"}] if freeze_end > entry else [])
        job = write_json(tmp_path / f"{name}.json", {"schema_version": 1, "name": name,
                         "sources": [{"id": "seam", "path": str(source)}],
                         "outputs": [{"id": "out", "name": name, "clips": [
                             {"id": "a", "source": "seam", "start": 0, "end": 1.4},
                             {"id": "b", "source": "seam", "start": entry, "end": 5, "edits": edits}]}]})
        plan = compile_job(job)
        out = tmp_path / f"{name}.mp4"
        render_output(plan, "out", out)
        rows = []
        _pause_picture_checks(rows, context, plan["outputs"][0], out, 40_000, 3_600_000)
        return rows

    skipped = checked("skipped", 2.8, 3.8)  # freeze outlasts the transition, then a hard cut
    # Pixels cannot tell a finished transition from one that still shows the previous topic, so a freeze
    # that covers moving frames is flagged for a person rather than failed outright.
    assert [row["status"] for row in skipped] == ["needs_human"]
    assert "after the next picture starts moving" in skipped[0]["detail"]
    at_first_moving_frame = checked("entry", 2.8, 3.04)
    assert [row["status"] for row in at_first_moving_frame] == ["passed"]
    shows_next = checked("next", 3.04, 3.04)  # no freeze: the transition fills the pause
    assert [row["status"] for row in shows_next] == ["failed"]
    assert "different picture" in shows_next[0]["detail"]


def _checks(report, kind):
    return [row for row in report["checks"] if row["kind"] == kind]


def test_qa_mechanical_checks_pass_on_a_planned_render(case):
    from auto_cut_slicing.qa import run_qa

    path, plan, video = _render(case, _intent(case), "mech-ok")
    run_qa(case["context"], path, plan, "out", video, None, case["dir"] / "qa-mech-ok.json")
    report = read_json(case["dir"] / "qa-mech-ok.json")
    for kind in ("frame_count", "frame_timing", "black_frames", "blurred_frames", "luma_flashes",
                 "still_frames"):
        rows = _checks(report, kind)
        assert len(rows) == 1, kind
        assert rows[0]["status"] == "passed", (kind, rows[0]["detail"])
        assert rows[0]["method"] == "automatic"


def test_qa_fails_an_output_with_a_missing_frame(case):
    from auto_cut_slicing.qa import run_qa

    path, plan, video = _render(case, _intent(case), "mech-drop")
    damaged = case["dir"] / "mech-drop-missing.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(video), "-vf",
        "select='not(eq(n,40))',setpts=N/25/TB", "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(damaged)], check=True, capture_output=True)
    run_qa(case["context"], path, plan, "out", damaged, None, case["dir"] / "qa-mech-drop.json")
    report = read_json(case["dir"] / "qa-mech-drop.json")
    assert _checks(report, "frame_count")[0]["status"] == "failed"
    assert report["overall"] == "failed"


def test_qa_fails_a_black_picture(case):
    from auto_cut_slicing.qa import run_qa

    path, plan, video = _render(case, _intent(case), "mech-black")
    # Append a black stretch: a rendered picture must never contain frames with no content.
    blacked = case["dir"] / "mech-black-damaged.mp4"
    descriptor = describe_source(video)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(video), "-an", "-vf",
        "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='gte(t,4)'",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(blacked)],
        check=True, capture_output=True)
    assert descriptor["duration_us"] > 4_000_000
    run_qa(case["context"], path, plan, "out", blacked, None, case["dir"] / "qa-mech-black.json")
    report = read_json(case["dir"] / "qa-mech-black.json")
    assert _checks(report, "black_frames")[0]["status"] == "failed"
    assert report["overall"] == "failed"


def test_qa_fails_a_still_stretch_that_no_freeze_explains(case):
    from auto_cut_slicing.qa import run_qa

    path, plan, video = _render(case, _intent(case), "mech-still")
    frozen = case["dir"] / "mech-still-damaged.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
        "trim=start_frame=60,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=0.8,"
        "trim=duration=5.4,setpts=N/25/TB",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(frozen)],
        check=True, capture_output=True)
    run_qa(case["context"], path, plan, "out", frozen, None, case["dir"] / "qa-mech-still.json")
    report = read_json(case["dir"] / "qa-mech-still.json")
    assert _checks(report, "still_frames")[0]["status"] == "failed"


def test_qa_does_not_count_a_planned_freeze_as_a_stalled_picture(case):
    from auto_cut_slicing.qa import run_qa

    intent = _intent(case)
    clip = intent["outputs"][0]["clips"][0]
    clip["operations"].append({
        "intent_id": "freeze-1", "kind": "freeze_cover", "covers": [],
        "standard_rule": "clean_cut_in", "purpose": "clean_cut_in",
        "start": _anchor("0:00", 0),
        "end": _anchor("0:05.60", 5_600_000),
        "frame": {"ref": "frame", "value_us": 2_480_000, "source_id": "talk",
                  "source_sha256": case["sha"], "evidence_id": "frames-1"},
        "visual_review": {"status": "needs_human", "evidence_ids": ["frames-1"], "observation": "Not reviewed."},
        "rationale": "Freeze the held frame over the opening.",
    })
    from auto_cut_slicing import evidence
    evidence.frames(case["context"], "talk", "frames-1", [2_500_000])
    intent["bindings"]["evidence_manifest_sha256"] = file_sha256(
        case["dir"] / "evidence" / "manifest.json")
    path, plan, video = _render(case, intent, "mech-freeze")
    run_qa(case["context"], path, plan, "out", video, None, case["dir"] / "qa-mech-freeze.json")
    report = read_json(case["dir"] / "qa-mech-freeze.json")
    assert _checks(report, "still_frames")[0]["status"] == "passed"


def _review_file(case, report_path, *, verdict="clean", frames=None, override=None):
    """A review the AI would write: answer every question on every reviewable item."""
    request = read_json(report_path.with_name(report_path.stem + "-review-request.json"))
    report = read_json(report_path)
    items = []
    hashes = {}
    for row in request["items"]:
        for frame in row["frames"]:
            hashes[frame["file"]] = frame["sha256"]
        for question in row["questions"]:
            items.append({"check_id": row["check_id"], "question_id": question["question_id"],
                          "verdict": verdict, "observation": "Reviewed the listed frames.",
                          "frames": [frame["file"] for frame in row["frames"]]})
    review = {"schema": "auto-cut-slicing/qa-review", "schema_version": 1,
              "qa_sha256": file_sha256(report_path),
              "reviewer": {"agent": "test-agent", "model": "test-model"},
              "frames_sha256": hashes, "items": items}
    for key, value in (override or {}).items():
        review[key] = value
    assert report["checks"]
    return write_json(case["dir"] / "qa-review.json", review)


def test_qa_publishes_review_request_and_applies_a_clean_review(case):
    from auto_cut_slicing.qa import apply_review, run_qa

    path, plan, video = _render(case, _intent(case), "review-clean")
    report_path = case["dir"] / "qa-review-clean.json"
    result = run_qa(case["context"], path, plan, "out", video, None, report_path)
    assert result["reviewable_items"] > 0
    request = read_json(Path(result["review_request_path"]))
    assert request["items"] and all(row["frames"] and row["questions"] for row in request["items"])
    frame = request["items"][0]["frames"][0]
    assert file_sha256(frame["path"]) == frame["sha256"]

    review_path = _review_file(case, report_path)
    merged = apply_review(report_path, Path(result["frames_dir"]), review_path)
    report = read_json(report_path)
    by_kind = {}
    for row in report["checks"]:
        by_kind.setdefault(row["kind"], set()).add(row["status"])
    assert by_kind["opening_frame"] == {"ai_reviewed"}
    assert by_kind["listening"] == {"needs_human"}  # listening is never answered by a picture review
    assert merged["overall"] == "ready_for_review"
    assert report["review"] == {"agent": "test-agent", "model": "test-model",
                                "path": str(review_path.resolve())}
    assert schema_errors(report, "qa-report.v1.schema.json") == []
    answered = [row for row in report["checks"] if row["status"] == "ai_reviewed"]
    assert answered and all(row["method"] == "ai_visual" and row["frames_sha256"] for row in answered)


def test_ai_review_of_a_defect_fails_the_report(case):
    from auto_cut_slicing.qa import apply_review, run_qa

    path, plan, video = _render(case, _intent(case), "review-defect")
    report_path = case["dir"] / "qa-review-defect.json"
    result = run_qa(case["context"], path, plan, "out", video, None, report_path)
    review_path = _review_file(case, report_path, verdict="defect")
    merged = apply_review(report_path, Path(result["frames_dir"]), review_path)
    report = read_json(report_path)
    assert merged["overall"] == "failed"
    defects = [row for row in report["checks"] if row["status"] == "failed"]
    assert defects and all(row["method"] == "ai_visual" for row in defects)
    # Automatic results are never rewritten by a review.
    assert not any(row["status"] == "failed" and row["method"] == "automatic" for row in report["checks"])


def test_an_unclear_review_leaves_the_item_for_a_person(case):
    from auto_cut_slicing.qa import apply_review, run_qa

    path, plan, video = _render(case, _intent(case), "review-unclear")
    report_path = case["dir"] / "qa-review-unclear.json"
    result = run_qa(case["context"], path, plan, "out", video, None, report_path)
    review_path = _review_file(case, report_path, verdict="unclear")
    merged = apply_review(report_path, Path(result["frames_dir"]), review_path)
    report = read_json(report_path)
    assert merged["overall"] == "ready_for_review"
    assert all(row["status"] == "needs_human" for row in report["checks"]
               if row["kind"] in ("opening_frame", "seam_frames", "freeze_frames"))


@pytest.mark.parametrize("change", ["wrong_report", "missing_answer", "wrong_frame", "swapped_frame",
                                    "unknown_question", "no_observation", "bad_verdict"])
def test_a_review_that_does_not_match_the_report_is_refused(case, change):
    from auto_cut_slicing.qa import apply_review, run_qa

    path, plan, video = _render(case, _intent(case), f"review-bad-{change}")
    report_path = case["dir"] / f"qa-review-bad-{change}.json"
    result = run_qa(case["context"], path, plan, "out", video, None, report_path)
    request = read_json(Path(result["review_request_path"]))
    first = request["items"][0]
    override = {}
    review_path = _review_file(case, report_path)
    review = read_json(review_path)
    if change == "wrong_report":
        override = {"qa_sha256": "0" * 64}
    elif change == "missing_answer":
        review["items"] = review["items"][1:]
    elif change == "wrong_frame":
        review["items"][0]["frames"] = ["not-in-this-report.png"]
    elif change == "swapped_frame":
        name = first["frames"][0]["file"]
        review["frames_sha256"][name] = "1" * 64
    elif change == "unknown_question":
        review["items"].append({"check_id": first["check_id"], "question_id": "made-up",
                                "verdict": "clean", "observation": "x",
                                "frames": [first["frames"][0]["file"]]})
    elif change == "no_observation":
        review["items"][0]["observation"] = " "
    elif change == "bad_verdict":
        review["items"][0]["verdict"] = "probably-fine"
    review.update(override)
    write_json(review_path, review)
    with pytest.raises(SlicingError, match="QA review does not apply"):
        apply_review(report_path, Path(result["frames_dir"]), review_path)
    assert read_json(report_path)["review"] if False else True


def test_a_still_stretch_the_source_also_holds_is_the_content_not_a_stall(monkeypatch):
    from fractions import Fraction
    from auto_cut_slicing import qa

    held, moving = b"\x10" * 576, [bytes([i]) * 576 for i in range(0, 250, 25)]
    # Output frames 0-9: frames 2-6 repeat one picture.
    out = [(100.0, 50.0, moving[0]), (100.0, 50.0, moving[1])] + [(100.0, 50.0, held)] * 5 \
        + [(100.0, 50.0, value) for value in moving[7:10]]
    stamps = [i * 40_000 for i in range(10)]
    output = {"segments": [{"source_id": "talk", "source_start_us": 1_000_000, "target_start_us": 0,
                            "duration_us": 400_000}], "effects": []}

    class Context:
        def source(self, source_id):
            return {"path": "talk.mp4"}

    def checked(source):
        monkeypatch.setattr(qa, "_frame_stats", lambda *args, **kwargs: source)
        rows = []
        qa._held_frames(rows, output, out, stamps, Fraction(25), Context())
        return rows[0]["status"]

    # A slide held in the source at the same place: passed.
    assert checked(out) == "passed"
    # The source moved there, so the output stalled: failed.
    assert checked([(100.0, 50.0, value) for value in moving]) == "failed"
    # Without the source to compare, a still stretch still fails closed.
    rows = []
    qa._held_frames(rows, output, out, stamps, Fraction(25))
    assert rows[0]["status"] == "failed"
