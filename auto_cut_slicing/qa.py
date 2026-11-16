"""Check whether a rendered output carries out its edit intent (schemas/qa-report.v1).

Automatic checks cover coverage, compiled operations, duration, word-level cut positions and,
when the output has its own source-bound reverse ASR, phrase absence/presence. Pictures,
listening and pacing are exported or listed for review and never marked as passed here.
"""

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .alignment import find_phrase_matches, load_alignment, normalized_text
from .boundaries import _sentences
from .context import EditContext
from .errors import SlicingError
from .intent import check_intent
from .io import file_sha256, read_json, write_new_json
from .schema import require_schema

_PHRASE_WINDOW_US = 2_000_000
_SILENCE_DBFS = -60.0


def _check(rows, check_id, kind, status, method, detail, *, intent_id=None, start=None, end=None):
    rows.append({"check_id": check_id, "intent_id": intent_id, "kind": kind, "status": status,
                 "method": method, "evidence_ids": [], "target_start_us": start,
                 "target_end_us": end, "detail": detail})


def _export_frame(video: Path, target_us: int, lead_us: int, image: Path) -> None:
    from .media import find_tool

    subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{max(0, target_us - lead_us) / 1_000_000:.6f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=960:-2", str(image)],
        check=True, capture_output=True,
    )


def _rms_dbfs(video: Path, start_us: int, duration_us: int) -> float | None:
    """RMS level in dBFS; -inf for digital silence, None when no audio samples decode."""
    import array
    import math

    from .media import find_tool

    raw = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{start_us / 1_000_000:.6f}", "-t", f"{duration_us / 1_000_000:.6f}",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    samples = array.array("h", raw[: len(raw) // 2 * 2])
    if not samples:
        return None
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else float("-inf")


def _seam_status(context, source_id, words, side, cut, inside, before, after):
    """Compare a cut with the measured decay of the previous word and onset of the next."""
    from .audio import analyze_gap

    if inside:
        return "needs_human", "automatic", "Cut splits a word; see cut_not_inside_word."
    if before is None:
        return "needs_human", "automatic", f"{side} cut at {cut}us precedes the first ASR word; listen."
    source = context.source(source_id)
    index = words.index(before)
    gap = analyze_gap(source["path"], words, index, source["duration_us"])
    decay, onset = gap["decay_end"], gap["low_energy_end"]
    label = f"{side} cut at source {cut}us after {before['text']!r}"
    if decay is None:
        return "needs_human", "automatic", f"{label}: no measurable pause before {after['text'] if after else 'the end'!r}."
    numbers = f"decay ends {decay}us, next onset {onset}us (threshold {gap['threshold_dbfs']} dBFS)"
    if cut < decay:
        return "needs_human", "automatic", f"{label} cuts {decay - cut}us of the word's decay; {numbers}."
    if cut > onset:
        return "needs_human", "automatic", f"{label} includes {cut - onset}us of the next word's onset; {numbers}."
    return "passed", "automatic", f"{label} lies in the measured pause; {numbers}. Pacing still needs listening."


def _cuts(output: dict[str, Any], durations: dict[str, int]):
    """Yield (segment, side, source_us, target_us) for every cut inside a source."""
    for segment in output["segments"]:
        start = segment["source_start_us"]
        end = start + segment["duration_us"]
        if start > 0:
            yield segment, "in", start, segment["target_start_us"]
        if end < durations[segment["source_id"]]:
            yield segment, "out", end, segment["target_start_us"] + segment["duration_us"]


def run_qa(context: EditContext, intent_path, plan_path, output_id, video, video_asr, report_path):
    from .media import describe_source

    report_path = Path(report_path)
    if report_path.exists():
        raise SlicingError(f"Refusing to overwrite: {report_path}")
    plan = read_json(plan_path)
    output = next((row for row in plan.get("outputs", []) if row["id"] == output_id), None)
    if output is None:
        raise SlicingError(f"Plan has no output {output_id}")
    rows: list[dict[str, Any]] = []
    checked = check_intent(intent_path, context)
    intent = checked["intent"]
    if checked["problems"] or checked["resolved"] is None:
        _check(rows, "coverage", "coverage_complete", "failed", "automatic",
               "; ".join(f"{p['rule']} {p['message']}" for p in checked["problems"][:5]) or "intent invalid")
        resolved = []
    else:
        _check(rows, "coverage", "coverage_complete", "passed", "automatic",
               "Every feedback span has a disposition; V01-V13 passed.")
        resolved = next((o["clips"] for o in checked["resolved"] if o["output_id"] == output_id), [])

    # Compiled operations and unlisted changes -------------------------------
    plan_items = {row["item_id"] for row in output.get("edits", [])} | {
        row["item_id"] for row in output.get("effects", [])}
    plan_clips = {row["clip_id"] for row in output["segments"]}
    wanted_items = {f"{clip['intent_id']}/{op['intent_id']}" for clip in resolved for op in clip["operations"]}
    wanted_clips = {clip["intent_id"] for clip in resolved}
    missing = sorted((wanted_items - plan_items) | (wanted_clips - plan_clips))
    _check(rows, "compiled", "operation_compiled", "failed" if missing or not resolved else "passed",
           "automatic", f"missing from plan: {missing}" if missing else
           f"{len(wanted_clips)} clips and {len(wanted_items)} operations are in the plan")
    extra = sorted((plan_items - wanted_items) | (plan_clips - wanted_clips))
    _check(rows, "unlisted", "unlisted_change", "failed" if extra else "passed", "automatic",
           f"plan items without an intent: {extra}" if extra else "No plan operation lacks an intent.")

    # Media ------------------------------------------------------------------
    descriptor = describe_source(video)
    tolerance = round(1_000_000 / Fraction(descriptor["fps"])) + 1_000
    delta = abs(descriptor["duration_us"] - output["duration_us"])
    _check(rows, "duration", "duration_match", "passed" if delta <= tolerance else "failed", "automatic",
           f"video {descriptor['duration_us']}us vs plan {output['duration_us']}us (delta {delta}us)",
           start=0, end=descriptor["duration_us"])

    # Word-level cut positions ---------------------------------------------------
    durations = {sid: context.source(sid)["duration_us"] for sid in {s["source_id"] for s in output["segments"]}}
    cue_starts = {row["clip_id"]: row["source_start_us"] for row in output.get("start_cues", [])}
    deletion_edges = {(row["source_id"], edge) for row in output.get("edits", [])
                      for edge in (row["source_start_us"], row["source_end_us"])}
    for number, (segment, side, cut, target) in enumerate(_cuts(output, durations), start=1):
        alignment = context.source(segment["source_id"])["alignment"]
        prefix = f"cut{number}"
        if alignment is None:
            _check(rows, prefix, "cut_not_inside_word", "not_run", "automatic",
                   f"{segment['source_id']} has no word alignment", start=target, end=target)
            continue
        words = alignment["words"]
        inside = [w for w in words if w["start_us"] < cut < w["end_us"]]
        before = max((w for w in words if w["end_us"] <= cut), key=lambda w: w["end_us"], default=None)
        after = min((w for w in words if w["start_us"] >= cut), key=lambda w: w["start_us"], default=None)
        where = f"{side} cut at source {cut}us"
        _check(rows, prefix, "cut_not_inside_word", "failed" if inside else "passed", "automatic",
               f"{where} splits {inside[0]['text']!r}" if inside else f"{where} is between words",
               intent_id=segment["clip_id"], start=target, end=target)
        _check(rows, f"{prefix}-gap", "seam_gap", *_seam_status(context, segment["source_id"], words, side,
                                                                cut, inside, before, after),
               intent_id=segment["clip_id"], start=target, end=target)
        if (segment["source_id"], cut) in deletion_edges:
            _check(rows, f"{prefix}-sentence", "sentence_complete", "passed", "automatic",
                   f"{where} is a reviewed word/range deletion join, not a clip edge",
                   intent_id=segment["clip_id"], start=target, end=target)
            continue
        try:
            sentences = _sentences(alignment, durations[segment["source_id"]])
        except SlicingError as error:
            _check(rows, f"{prefix}-sentence", "sentence_complete", "not_run", "automatic", str(error),
                   intent_id=segment["clip_id"], start=target, end=target)
            continue
        interior = next((s for s in sentences if s["sentence_start_us"] < cut < s["sentence_end_us"]), None)
        cue = side == "in" and cue_starts.get(segment["clip_id"]) == cut
        if interior is None:
            status, detail = "passed", f"{where} is at a sentence boundary or pause"
        elif side == "out":
            status, detail = "failed", f"{where} ends inside sentence: {interior['sentence_text']}"
        elif cue:
            status, detail = "passed", f"{where} starts at the reviewed start_cue word"
        else:
            status, detail = "needs_human", f"{where} starts mid-sentence: {interior['sentence_text']}"
        _check(rows, f"{prefix}-sentence", "sentence_complete", status, "automatic", detail,
               intent_id=segment["clip_id"], start=target, end=target)

    # Reverse ASR of the rendered output -----------------------------------------
    out_alignment = None
    if video_asr is not None:
        out_alignment = load_alignment(video_asr, descriptor)
    clip_targets = {}
    for segment in output["segments"]:
        a, b = clip_targets.get(segment["clip_id"], (segment["target_start_us"], 0))
        clip_targets[segment["clip_id"]] = (min(a, segment["target_start_us"]),
                                            max(b, segment["target_start_us"] + segment["duration_us"]))
    edits = {row["item_id"]: row for row in output.get("edits", [])}
    effects = {}
    for row in output.get("effects", []):
        effects.setdefault(row["item_id"], []).append(row)
    for clip in resolved:
        cid = clip["intent_id"]
        target = clip_targets.get(cid)
        checks = []
        for op in clip["operations"]:
            item = f"{cid}/{op['intent_id']}"
            if op["kind"] == "delete_phrase" and item in edits:
                join = edits[item]["target_join_us"]
                checks.append((op["intent_id"], "phrase_absent", op["phrase"]["text"],
                               max(0, join - _PHRASE_WINDOW_US), join + _PHRASE_WINDOW_US, False))
            elif op["kind"] == "mute_phrase" and item in effects:
                first = effects[item][0]
                checks.append((op["intent_id"], "phrase_absent", op["phrase"]["text"],
                               max(0, first["target_start_us"] - 500_000),
                               first["target_start_us"] + first["duration_us"] + 500_000, False))
        if target:
            for keep in clip["must_keep"]:
                checks.append((cid, "must_keep_present", keep, target[0], target[1], True))
        for intent_id, kind, text, a, b, want in checks:
            if out_alignment is None:
                _check(rows, f"{intent_id}-{kind}", kind, "not_run", "automatic",
                       "No reverse ASR of the rendered output was supplied (--video-asr).",
                       intent_id=intent_id, start=a, end=b)
                continue
            b = min(b, descriptor["duration_us"])
            hits = find_phrase_matches(out_alignment, text, clip_start_us=a, clip_end_us=b)
            ok = bool(hits) == want
            _check(rows, f"{intent_id}-{kind}", kind, "passed" if ok else "failed", "automatic",
                   f"{text!r} {'found' if hits else 'not found'} in output {a}-{b}us; reverse ASR can "
                   "misrecognize, so listen before acting on a failure.", intent_id=intent_id, start=a, end=b)
        if "cue" in clip and target:
            if out_alignment is None:
                _check(rows, f"{cid}-cue", "cue_first_word", "not_run", "automatic",
                       "No reverse ASR of the rendered output was supplied (--video-asr).",
                       intent_id=cid, start=target[0], end=target[0])
            else:
                following = [w for w in out_alignment["words"] if w["start_us"] >= target[0] - 100_000][:12]
                spoken = normalized_text("".join(w["text"] for w in following))
                ok = spoken.startswith(normalized_text(clip["cue"]["text"]))
                _check(rows, f"{cid}-cue", "cue_first_word", "passed" if ok else "failed", "automatic",
                       f"output speech after the clip start begins {spoken[:16]!r}",
                       intent_id=cid, start=target[0], end=target[0])

    # Pictures for review ----------------------------------------------------------------
    frame_dir = report_path.with_name(report_path.stem + "-frames")
    frame_dir.mkdir(parents=True, exist_ok=False)
    frame_us = round(1_000_000 / Fraction(descriptor["fps"]))
    lead = min(10_000, frame_us * 2 // 5)
    joins = sorted({segment["target_start_us"] for segment in output["segments"]})
    exported = []
    for join in joins:
        names = []
        for offset, label in ((-frame_us, "before"), (0, "after")):
            t = join + offset
            if not 0 <= t < descriptor["duration_us"]:
                continue
            image = frame_dir / f"t{t}us_{label}.png"
            _export_frame(Path(descriptor["path"]), t, lead, image)
            names.append(image.name)
        exported.append((join, names))
    for join, names in exported:
        kind = "opening_frame" if join == 0 else "seam_frames"
        _check(rows, f"frames-{join}", kind, "needs_human", "automatic",
               f"Review {frame_dir.name}/{', '.join(names)} for previous-topic residue or flashes.",
               start=join, end=join)
    for effect in output.get("effects", []):
        a = effect["target_start_us"]
        b = a + effect["duration_us"]
        op_id = effect["item_id"].split("/")[-1]
        if effect["kind"] == "freeze_frame":
            names = []
            for t, label in ((b - frame_us, "freeze-last"), (b, "resume")):
                if 0 <= t < descriptor["duration_us"]:
                    image = frame_dir / f"t{t}us_{label}.png"
                    _export_frame(Path(descriptor["path"]), t, lead, image)
                    names.append(image.name)
            _check(rows, f"freeze-{effect['item_id']}", "freeze_frames", "needs_human", "automatic",
                   f"Review the held frame and the hand-back to playback: {frame_dir.name}/{', '.join(names)}; "
                   "check for flashes of old content or a frozen new animation.",
                   intent_id=op_id, start=a, end=b)
        elif effect["kind"] == "mute":
            inner = max(0, effect["duration_us"] - 40_000)
            level = _rms_dbfs(Path(descriptor["path"]), a + 20_000, inner) if inner else None
            if level is None:
                _check(rows, f"mute-{effect['item_id']}-{a}", "mute_silence", "not_run", "automatic",
                       "No audio samples could be measured in the muted range.",
                       intent_id=op_id, start=a, end=b)
            else:
                shown = "digital silence" if level == float("-inf") else f"{level:.1f} dBFS"
                _check(rows, f"mute-{effect['item_id']}-{a}", "mute_silence",
                       "passed" if level <= _SILENCE_DBFS else "failed", "automatic",
                       f"Output audio in the muted range measures {shown} (threshold {_SILENCE_DBFS} dBFS).",
                       intent_id=op_id, start=a, end=b)
    _check(rows, "listening", "listening", "needs_human", "human",
           "Listen to every cut, the opening and the ending.")
    _check(rows, "pacing", "pacing", "needs_human", "human", "Judge pacing at semantic joins.")

    report = {
        "schema": "auto-cut-slicing/qa-report", "schema_version": 1,
        "bindings": {
            "intent_sha256": file_sha256(intent_path), "plan_sha256": file_sha256(plan_path),
            "output_id": output_id, "output_sha256": descriptor["sha256"],
            "source_sha256s": sorted({context.source(sid)["sha256"] for sid in durations}),
            "output_alignment_sha256": out_alignment["sha256"] if out_alignment else None,
        },
        "overall": "failed" if any(row["status"] == "failed" for row in rows) else "ready_for_review",
        "checks": rows,
        "limitations": [
            "Automatic checks rely on provider ASR word edges; they do not replace listening.",
            "Frame exports are for review; picture cleanliness is not judged automatically.",
            "ready_for_review is not acceptance.",
        ],
    }
    if isinstance(intent, dict) and intent.get("status") == "blocked":
        report["limitations"].append("The intent is blocked; this output does not implement it.")
    require_schema(report, "qa-report.v1.schema.json", "QA report")
    write_new_json(report_path, report)
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"ok": report["overall"] != "failed", "overall": report["overall"], "counts": counts,
            "report_path": str(report_path.resolve()), "frames_dir": str(frame_dir.resolve())}
