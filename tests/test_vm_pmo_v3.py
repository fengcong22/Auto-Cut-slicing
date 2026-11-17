"""VM/PMO v3: AI edit intent planning on synthetic media and synthetic ASR.

The synthetic ASR and the hand-written intent stand in for the live ASR service and the AI
step. They verify deterministic mechanics only, not recognition accuracy or AI judgement.
"""

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from auto_cut_slicing import vm_pmo
from auto_cut_slicing.io import file_sha256, read_json, write_json
from auto_cut_slicing.media import describe_source

ROOT = Path(__file__).resolve().parents[1]
RAW = "0:00-0:05.60，删掉第二遍的“我们再看一下”"
RANGE = "0:00-0:05.60"
UTTERANCES = [
    ("我们开始。", [("我们", 0.2, 0.5), ("开始", 0.5, 0.9)]),
    ("我们再看一下这个实验。", [("我们", 1.2, 1.4), ("再看一下", 1.4, 1.9), ("这个", 1.9, 2.1),
                                ("实验", 2.1, 2.5)]),
    ("然后我们再看一下结果。", [("然后", 3.0, 3.3), ("我们", 3.3, 3.5), ("再看一下", 3.5, 4.0),
                                ("结果", 4.0, 4.4)]),
    ("最后总结。", [("最后", 4.9, 5.2), ("总结", 5.2, 5.6)]),
]
_TONE = "0.5*sin(2*PI*500*t)*(" + "+".join(
    f"between(t,{a},{b})" for _, rows in UTTERANCES for _t, a, b in rows) + ")"


def _alignment(sha):
    return {
        "schema_version": 1, "source_sha256": sha, "provider": "synthetic-fixture",
        "model": "synthetic-fixture", "adapter": "synthetic-fixture",
        "words": [{"text": t, "start": a, "end": b} for _, rows in UTTERANCES for t, a, b in rows],
        "utterances": [{"text": text, "words": [{"text": t, "start": a, "end": b} for t, a, b in rows]}
                       for text, rows in UTTERANCES],
    }


@pytest.fixture
def v3(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required for VM/PMO media contract")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=6",
        "-f", "lavfi", "-i", f"aevalsrc='{_TONE}':s=48000:d=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(source)], check=True, capture_output=True)
    descriptor = describe_source(source)
    manifest = ROOT / "PACKAGE-MANIFEST.json"
    calls = []

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        calls.append(source_descriptor["path"])
        assert source_descriptor["sha256"] == descriptor["sha256"]
        return write_json(output, _alignment(descriptor["sha256"]))

    monkeypatch.setattr(vm_pmo, "transcribe_source", transcribe)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    input_data = {
        "protocol": "vm_pmo_review_video", "schema_version": 3, "planning_mode": "ai_intent",
        "ai_session": {"agent": "codex", "model": "test-model", "session_id": "session-1"},
        "package": {"alias": "slicing", "name": "auto-cut-slicing",
                    "version": json.loads(manifest.read_text(encoding="utf-8"))["version"],
                    "manifest_sha256": file_sha256(manifest)},
        "binding": {"task_id": "task-1", "run_id": "run-1", "subject_key": "subject-1",
                    "config_version": "config-1", "stage_id": "initial", "event_id": "event-1"},
        "record": {"base_token": "base-1", "table_id": "table-1", "record_id": "record-1"},
        "review_round": {"round_id": "round-1", "input_snapshot_id": "snapshot-1", "parent": None},
        "source": {"source_id": "source-1", "timeline_basis": "vm_original", "vmid": "vm-1",
                   "sha256": descriptor["sha256"], "bytes": source.stat().st_size,
                   "duration_ms": round(descriptor["duration_us"] / 1000)},
        "instructions": [{"instruction_id": "instruction-1", "source_field_id": "field-1",
                          "raw_text": RAW, "timeline_basis": "vm_original",
                          "applicability": "current_round"}],
        "output_profile": {"id": "review_mp4_v1", "width": 1920, "height": 1080, "fps": 25,
                           "audio": "preserve"},
        "delivery_target": {"attachment_field_id": "attachment-1"},
    }
    context = {
        "protocol": "vm_pmo_review_video", "schema_version": 3, "input_sha256": "0" * 64,
        "run_dir": str(run_dir), "source_path": str(source),
        "plan_path": str(run_dir / "plan.json"),
        "plan_result_path": str(run_dir / "plan-result.json"),
        "result_path": str(run_dir / "result.json"), "output_path": str(run_dir / "review.mp4"),
        "evidence_dir": str(run_dir / "evidence"),
        "intent_context_path": str(run_dir / "intent-context.json"),
        "intent_path": str(run_dir / "edit-intent.json"),
        "analyze_result_path": str(run_dir / "analyze-result.json"),
        "qa_path": str(run_dir / "qa.json"), "qa_review_path": str(run_dir / "qa-review.json"),
    }
    case = {"run_dir": run_dir, "input": input_data, "context": context, "calls": calls,
            "descriptor": descriptor, "input_path": run_dir / "input.json",
            "context_path": run_dir / "execution-context.json"}
    save(case)
    return case


def save(case):
    write_json(case["input_path"], case["input"])
    case["context"]["input_sha256"] = file_sha256(case["input_path"])
    write_json(case["context_path"], case["context"])


def plan(case):
    return vm_pmo.plan_command(case["input_path"], case["context_path"])


def analyze(case):
    return vm_pmo.analyze_command(case["input_path"], case["context_path"])


def _span(raw, text, start=0):
    index = raw.index(text, start)
    return {"instruction_id": "instruction-1", "start": index, "end": index + len(text)}


def _anchor(raw, text, us):
    return {"ref": "user_anchor", "value_us": us, "span": _span(raw, text)}


def intent(case, raw=RAW, *, delete=True, end=None):
    """The intent an AI would write for RAW: keep 0-5.6 s, drop the second repetition."""
    alignment_sha = file_sha256(case["run_dir"] / "alignment.words.json")
    clip_span = _span(raw, RANGE)
    coverage = [{"span": clip_span, "quote": RANGE, "disposition": "operation",
                 "intent_ids": ["clip-1"], "reason": "保留范围"}]
    operations = []
    if delete:
        delete_span = _span(raw, "删掉第二遍的“我们再看一下”")
        coverage.append({"span": delete_span, "quote": "删掉第二遍的“我们再看一下”",
                         "disposition": "operation", "intent_ids": ["del-1"],
                         "reason": "第二次出现，音画同删"})
        operations.append({
            "intent_id": "del-1", "kind": "delete_phrase", "covers": [delete_span],
            "rationale": "第二遍",
            "phrase": {
                "source_id": "source-1", "alignment_sha256": alignment_sha,
                "first_index": 7, "last_index": 8, "text": "我们再看一下",
                "selection": {
                    "method": "user_ordinal", "scope_start_us": 0, "scope_end_us": 5_600_000,
                    "candidates": [
                        {"first_index": 2, "last_index": 3, "start_us": 1_200_000, "end_us": 1_900_000},
                        {"first_index": 7, "last_index": 8, "start_us": 3_300_000, "end_us": 4_000_000},
                    ],
                    "evidence_id": None, "ordinal": 2, "ordinal_span": _span(raw, "第二遍"),
                },
            }})
    return {
        "schema": "auto-cut-slicing/edit-intent", "schema_version": 1, "status": "proposed",
        "author": {"kind": "ai", "agent": "codex", "model": "test-model", "session_id": "session-1"},
        "bindings": {
            "timeline_basis": "vm_original",
            "sources": [{"source_id": "source-1", "sha256": case["descriptor"]["sha256"],
                         "duration_us": case["descriptor"]["duration_us"],
                         "alignment_sha256": alignment_sha}],
            "evidence_manifest_sha256": file_sha256(case["run_dir"] / "evidence" / "manifest.json"),
        },
        "instructions": [{"instruction_id": "instruction-1", "raw_text": raw,
                          "raw_text_sha256": hashlib.sha256(raw.encode()).hexdigest()}],
        "coverage": coverage,
        "outputs": [{"output_id": "review", "clips": [{
            "intent_id": "clip-1", "source_id": "source-1", "covers": [clip_span],
            "start": {"mode": "time", "at": _anchor(raw, "0:00", 0)},
            "end": {"mode": "time", "at": end or _anchor(raw, "0:05.60", 5_600_000)},
            "operations": operations, "must_keep": [], "rationale": "test"}]}],
        "blockers": [],
    }


def write_intent(case, value):
    return write_json(case["context"]["intent_path"], value)


def test_analyze_binds_asr_context_and_full_transcript_once(v3):
    result = analyze(v3)
    assert result["status"] == "ready", result["error"]
    assert result["planning_mode"] == "ai_intent"
    context = read_json(v3["context"]["intent_context_path"])
    assert context["instructions"] == [{"instruction_id": "instruction-1", "raw_text": RAW}]
    assert context["timeline_basis"] == "vm_original"
    assert context["evidence_dir"] == "evidence"
    manifest = read_json(v3["run_dir"] / "evidence" / "manifest.json")
    assert [row["evidence_id"] for row in manifest["entries"]] == ["transcript-full"]
    assert result["evidence_manifest_sha256"] == file_sha256(v3["run_dir"] / "evidence" / "manifest.json")
    assert analyze(v3) == result
    assert len(v3["calls"]) == 1


def test_ai_intent_plans_renders_and_runs_qa(v3):
    assert analyze(v3)["status"] == "ready"
    write_intent(v3, intent(v3))
    receipt = plan(v3)
    assert receipt["status"] == "ready", receipt
    assert receipt["planning_mode"] == "ai_intent"
    assert receipt["intent_sha256"] == file_sha256(v3["context"]["intent_path"])
    assert receipt["blockers"] == []
    assert [row["disposition"] for row in receipt["coverage"]] == ["operation", "operation"]
    saved = read_json(v3["context"]["plan_path"])
    assert saved["rule_cross_check"] == "not_applicable"
    assert saved["ai_session"]["session_id"] == "session-1"
    deletion = next(row for row in saved["operations"] if row["kind"] == "word")
    assert (deletion["intent_id"], deletion["source_start_us"], deletion["source_end_us"]) == (
        "del-1", 3_300_000, 4_000_000)
    assert saved["expected_duration_ms"] == 4_900
    assert saved["instruction_results"][0]["segments"] == [row["segment_id"] for row in saved["segments"]]
    assert plan(v3) == receipt

    rendered = vm_pmo.render_command(v3["input_path"], v3["context_path"], Path(v3["context"]["plan_path"]))
    assert rendered["status"] == "rendered", rendered["error"]
    assert rendered["artifacts"][0]["duration_ms"] == 4_900

    checked = vm_pmo.qa_command(v3["input_path"], v3["context_path"])
    assert checked["status"] == "checked", checked["error"]
    report = read_json(v3["context"]["qa_path"])
    assert checked["qa_sha256"] == file_sha256(v3["context"]["qa_path"])
    statuses = {row["kind"]: row["status"] for row in report["checks"]}
    assert statuses["coverage_complete"] == statuses["operation_compiled"] == "passed"
    assert statuses["unlisted_change"] == statuses["duration_match"] == "passed"
    assert report["bindings"]["plan_sha256"] == file_sha256(v3["context"]["plan_path"])
    assert report["bindings"]["output_sha256"] == rendered["artifacts"][0]["sha256"]
    assert read_json(v3["run_dir"] / "qa-result.json") == checked
    assert vm_pmo.qa_command(v3["input_path"], v3["context_path"]) == checked


def test_plan_requires_analysis_then_intent(v3):
    first = plan(v3)
    assert (first["status"], first["error"]["code"]) == ("blocked", "ANALYSIS_REQUIRED")
    assert first["instruction_results"][0]["raw_text"] == RAW
    assert not Path(v3["context"]["plan_path"]).exists()


def test_missing_intent_blocks_without_plan(v3):
    analyze(v3)
    receipt = plan(v3)
    assert (receipt["status"], receipt["error"]["code"]) == ("blocked", "INTENT_MISSING")
    assert receipt["intent_sha256"] is None
    assert receipt["blockers"] == [receipt["error"]]
    assert not Path(v3["context"]["plan_path"]).exists()


def test_blocked_intent_keeps_ai_blockers(v3):
    analyze(v3)
    value = intent(v3)
    value["status"] = "blocked"
    value["coverage"][1]["disposition"] = "ambiguous"
    value["coverage"][1]["intent_ids"] = []
    value["outputs"][0]["clips"][0]["operations"] = []
    value["blockers"] = [{"code": "INTENT_NEEDS_USER_DECISION", "spans": [value["coverage"][1]["span"]],
                          "message": "两处都可能是要删的那一遍", "missing_evidence": [], "candidates": []}]
    write_intent(v3, value)
    receipt = plan(v3)
    assert (receipt["status"], receipt["error"]["code"]) == ("blocked", "INTENT_BLOCKED"), receipt
    assert receipt["blockers"][0]["code"] == "INTENT_NEEDS_USER_DECISION"
    assert receipt["intent_sha256"] == file_sha256(v3["context"]["intent_path"])


def test_invalid_intent_reports_validator_rules(v3):
    analyze(v3)
    value = intent(v3)
    value["instructions"][0]["raw_text"] = RAW + "。"
    write_intent(v3, value)
    receipt = plan(v3)
    assert (receipt["status"], receipt["error"]["code"]) == ("blocked", "INTENT_INVALID")
    assert "V02" in {row["rule"] for row in receipt["blockers"]}


def test_rule_parser_cross_check_agrees(v3):
    v3["input"]["instructions"][0]["raw_text"] = RANGE
    save(v3)
    analyze(v3)
    write_intent(v3, intent(v3, RANGE, delete=False))
    assert plan(v3)["status"] == "ready"
    assert read_json(v3["context"]["plan_path"])["rule_cross_check"] == "agreed"


def test_rule_parser_disagreement_blocks(v3):
    v3["input"]["instructions"][0]["raw_text"] = RANGE
    save(v3)
    analyze(v3)
    alignment_sha = file_sha256(v3["run_dir"] / "alignment.words.json")
    end = {"ref": "word_edge", "value_us": 4_400_000, "source_id": "source-1",
           "alignment_sha256": alignment_sha, "index": 9, "edge": "end", "text": "结果"}
    write_intent(v3, intent(v3, RANGE, delete=False, end=end))
    receipt = plan(v3)
    assert (receipt["status"], receipt["error"]["code"]) == ("blocked", "INTENT_RULE_MISMATCH"), receipt
    assert not Path(v3["context"]["plan_path"]).exists()


def test_render_rejects_intent_changed_after_planning(v3):
    analyze(v3)
    write_intent(v3, intent(v3))
    assert plan(v3)["status"] == "ready"
    changed = intent(v3)
    changed["outputs"][0]["clips"][0]["rationale"] = "edited later"
    write_intent(v3, changed)
    rendered = vm_pmo.render_command(v3["input_path"], v3["context_path"], Path(v3["context"]["plan_path"]))
    assert (rendered["status"], rendered["error"]["code"]) == ("blocked", "PLAN_CHANGED")
    assert not Path(v3["context"]["output_path"]).exists()
    with pytest.raises(vm_pmo.VmError, match="intent"):
        plan(v3)


def test_qa_requires_rendered_output(v3):
    analyze(v3)
    write_intent(v3, intent(v3))
    plan(v3)
    checked = vm_pmo.qa_command(v3["input_path"], v3["context_path"])
    assert (checked["status"], checked["error"]["code"]) == ("blocked", "RENDER_REQUIRED")
    assert not Path(v3["context"]["qa_path"]).exists()
    assert read_json(v3["run_dir"] / "qa-result.json") == checked
    assert vm_pmo.qa_command(v3["input_path"], v3["context_path"]) == checked


def test_rule_mode_matches_v2_range_planning_without_analysis(v3):
    v3["input"]["planning_mode"] = "rule"
    del v3["input"]["ai_session"]
    v3["input"]["instructions"][0]["raw_text"] = RANGE
    save(v3)
    receipt = plan(v3)
    assert receipt["status"] == "ready", receipt
    assert (receipt["planning_mode"], receipt["intent_sha256"], receipt["coverage"]) == ("rule", None, None)
    saved = read_json(v3["context"]["plan_path"])
    assert saved["expected_duration_ms"] == 5_600
    assert v3["calls"] == []
    with pytest.raises(vm_pmo.VmError, match="ai_intent"):
        analyze(v3)


@pytest.mark.parametrize("change", ["no_session", "manual_with_ai", "bad_mode", "session_in_rule",
                                    "context_v2_paths", "intent_outside_run", "bad_prior_round",
                                    "qa_reuses_fixed_receipt"])
def test_v3_rejects_invalid_requests(v3, change):
    value = copy.deepcopy(v3["input"])
    context = v3["context"]
    if change == "no_session":
        del value["ai_session"]
    elif change == "manual_with_ai":
        value["manual_revision"] = {"source_sha256": value["source"]["sha256"], "note": "x",
                                    "clips": [{"start_ms": 0, "end_ms": 100}]}
    elif change == "bad_mode":
        value["planning_mode"] = "guess"
    elif change == "session_in_rule":
        value["planning_mode"] = "rule"
    elif change == "context_v2_paths":
        for key in vm_pmo.V3_CONTEXT_PATHS:
            del context[key]
    elif change == "qa_reuses_fixed_receipt":
        context["qa_path"] = str(v3["run_dir"] / "qa-result.json")
    elif change == "intent_outside_run":
        context["intent_path"] = str(v3["run_dir"].parent / "edit-intent.json")
    elif change == "bad_prior_round":
        value["prior_rounds"] = [{"round_id": "round-0"}]
    v3["input"] = value
    save(v3)
    outcome = subprocess.run(
        [__import__("sys").executable, "-m", "auto_cut_slicing.vm_pmo", "plan",
         "--input", str(v3["input_path"]), "--context", str(v3["context_path"])],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    result = json.loads(outcome.stdout)
    if change in ("context_v2_paths", "intent_outside_run", "qa_reuses_fixed_receipt"):
        # Untrusted context never yields a receipt path; the run fails before planning.
        assert outcome.returncode == 1 and result["status"] == "failed"
        assert result["error"]["code"] in ("INVALID_CONTEXT", "INVALID_INPUT")
    else:
        assert result["status"] == "blocked" and result["error"]["code"] == "INVALID_INPUT", result
    assert not Path(context["plan_path"]).exists()


def test_v3_cli_analyze_reports_blocked_asr(v3, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("service down")

    monkeypatch.setattr(vm_pmo, "transcribe_source", broken)
    result = analyze(v3)
    assert (result["status"], result["error"]["code"]) == ("blocked", "WORD_ALIGNMENT_UNRESOLVED")
    assert read_json(v3["context"]["analyze_result_path"]) == result
    assert plan(v3)["error"]["code"] == "ANALYSIS_REQUIRED"


def test_qa_reverse_asr_transcribes_the_rendered_mp4(v3, monkeypatch):
    analyze(v3)
    write_intent(v3, intent(v3))
    plan(v3)
    rendered = vm_pmo.render_command(v3["input_path"], v3["context_path"], Path(v3["context"]["plan_path"]))
    output_sha = rendered["artifacts"][0]["sha256"]
    seen = []

    def transcribe(source_descriptor, output, *, config=None, env_path=None):
        seen.append(source_descriptor)
        kept = [("我们", 0.2, 0.5), ("开始", 0.5, 0.9), ("我们", 1.2, 1.4), ("再看一下", 1.4, 1.9),
                ("这个", 1.9, 2.1), ("实验", 2.1, 2.5), ("然后", 3.0, 3.3), ("结果", 3.3, 3.7),
                ("最后", 4.2, 4.5), ("总结", 4.5, 4.9)]
        return write_json(output, {
            "schema_version": 1, "source_sha256": output_sha, "provider": "synthetic-fixture",
            "model": "synthetic-fixture", "adapter": "synthetic-fixture",
            "words": [{"text": t, "start": a, "end": b} for t, a, b in kept]})

    monkeypatch.setattr(vm_pmo, "transcribe_source", transcribe)
    checked = vm_pmo.qa_command(v3["input_path"], v3["context_path"], reverse_asr=True)
    assert checked["status"] == "checked", checked["error"]
    assert checked["reverse_asr"] is True
    assert [row["sha256"] for row in seen] == [output_sha]
    assert seen[0]["path"] == v3["context"]["output_path"]
    assert (v3["run_dir"] / "output.words.json").is_file()
