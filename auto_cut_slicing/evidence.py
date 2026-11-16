"""Source-bound evidence files that an AI editor reads and cites in an edit intent.

Every command writes one evidence file into the context's evidence directory and registers
it in ``manifest.json`` (``schemas/evidence-manifest.v1.schema.json``). Existing evidence IDs
and files are never replaced.
"""

import subprocess
from pathlib import Path
from typing import Any

from .alignment import find_phrase_matches, normalized_text
from .boundaries import _sentences
from .context import EditContext
from .errors import SlicingError
from .io import file_sha256, read_json, write_json, write_new_json
from .schema import require_schema

TOOL_VERSION = "1"
MANIFEST = "manifest.json"
_CONTEXT_WORDS = 10


def manifest_path(context: EditContext) -> Path:
    return context.evidence_dir / MANIFEST


def _source_binding(context: EditContext, source_id: str) -> dict[str, Any]:
    source = context.source(source_id)
    alignment = source["alignment"]
    return {
        "source_id": source_id,
        "sha256": source["sha256"],
        "duration_us": source["duration_us"],
        "alignment_sha256": alignment["sha256"] if alignment else None,
    }


def load_manifest(context: EditContext) -> dict[str, Any]:
    path = manifest_path(context)
    if not path.exists():
        return {
            "schema": "auto-cut-slicing/evidence-manifest", "schema_version": 1,
            "sources": [_source_binding(context, sid) for sid in context.source_ids()],
            "entries": [],
        }
    manifest = read_json(path)
    require_schema(manifest, "evidence-manifest.v1.schema.json", "Evidence manifest")
    current = {row["source_id"]: _source_binding(context, row["source_id"])
               for row in manifest["sources"] if row["source_id"] in context.source_ids()}
    for row in manifest["sources"]:
        if row["source_id"] in current and row != current[row["source_id"]]:
            raise SlicingError(
                f"Evidence manifest is stale for source {row['source_id']}; "
                "use a new evidence directory for changed media or ASR"
            )
    for sid in context.source_ids():
        if sid not in current:
            manifest["sources"].append(_source_binding(context, sid))
    return manifest


def entry(manifest: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    for row in manifest["entries"]:
        if row["evidence_id"] == evidence_id:
            return row
    raise SlicingError(f"Evidence ID is not registered: {evidence_id}")


def _ensure_new(context: EditContext, evidence_id: str) -> dict[str, Any]:
    manifest = load_manifest(context)
    if any(item["evidence_id"] == evidence_id for item in manifest["entries"]):
        raise SlicingError(f"Evidence ID already exists: {evidence_id}")
    if (context.evidence_dir / f"{evidence_id}.json").exists():
        raise SlicingError(f"Evidence file already exists: {evidence_id}.json")
    return manifest


def _register(context: EditContext, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _ensure_new(context, row["evidence_id"])
    target = context.evidence_dir / row["path"]
    write_new_json(target, payload)
    row["sha256"] = file_sha256(target)
    manifest["entries"].append(row)
    require_schema(manifest, "evidence-manifest.v1.schema.json", "Evidence manifest")
    write_json(manifest_path(context), manifest)
    return {"ok": True, "evidence": row, "path": str(target)}


def _row(context, evidence_id, kind, source_id, params, limitations, **extra):
    source = context.source(source_id)
    alignment = source["alignment"]
    return {
        "evidence_id": evidence_id, "kind": kind, "path": f"{evidence_id}.json",
        "sha256": "0" * 64, "source_id": source_id, "source_sha256": source["sha256"],
        "alignment_sha256": alignment["sha256"] if alignment else None,
        "tool": f"auto_cut_slicing.evidence.{kind}", "tool_version": TOOL_VERSION,
        "params": params, **extra, "limitations": limitations,
    }


def _words(alignment, first, last):
    return [
        {"index": index, "text": word["text"], "start_us": word["start_us"], "end_us": word["end_us"]}
        for index, word in enumerate(alignment["words"][first:last], start=first)
    ]


def transcript_window(context, source_id, evidence_id, start_us, end_us):
    _ensure_new(context, evidence_id)
    source = context.source(source_id)
    alignment = context.alignment(source_id)
    if not 0 <= start_us < end_us <= source["duration_us"]:
        raise SlicingError("Transcript window must satisfy 0 <= start < end <= source duration")
    indices = [i for i, w in enumerate(alignment["words"]) if w["end_us"] > start_us and w["start_us"] < end_us]
    words = _words(alignment, indices[0], indices[-1] + 1) if indices else []
    try:
        sentences = [
            {"text": row["sentence_text"], "start_us": row["sentence_start_us"],
             "end_us": row["sentence_end_us"], "first_index": row["word_indices"][0],
             "last_index": row["word_indices"][-1]}
            for row in _sentences(alignment, source["duration_us"])
            if row["sentence_end_us"] > start_us and row["sentence_start_us"] < end_us
        ]
        sentence_note = None
    except SlicingError as error:
        sentences, sentence_note = [], f"Sentence evidence unavailable: {error}"
    gaps = [
        {"after_index": a["index"], "start_us": a["end_us"], "end_us": b["start_us"],
         "duration_us": b["start_us"] - a["end_us"]}
        for a, b in zip(words, words[1:]) if b["start_us"] - a["end_us"] >= 200_000
    ]
    payload = {"source_sha256": source["sha256"], "alignment_sha256": alignment["sha256"],
               "start_us": start_us, "end_us": end_us, "words": words,
               "sentences": sentences, "gaps_ge_200ms": gaps, "sentence_note": sentence_note}
    limitations = ["ASR word edges are the provider's estimates; listen before relying on tight cuts."]
    if sentence_note:
        limitations.append(sentence_note)
    return _register(context, _row(context, evidence_id, "transcript_window", source_id,
                                   {"start_us": start_us, "end_us": end_us}, limitations), payload)


def find_phrase(context, source_id, evidence_id, text, *, scope_start_us=0, scope_end_us=None, anchor_us=None):
    _ensure_new(context, evidence_id)
    source = context.source(source_id)
    alignment = context.alignment(source_id)
    scope_end_us = source["duration_us"] if scope_end_us is None else scope_end_us
    if not 0 <= scope_start_us < scope_end_us <= source["duration_us"]:
        raise SlicingError("Phrase scope must satisfy 0 <= start < end <= source duration")
    if not normalized_text(text):
        raise SlicingError("Phrase must contain spoken text")
    words = alignment["words"]
    candidates = []
    for match in find_phrase_matches(alignment, text, clip_start_us=scope_start_us, clip_end_us=scope_end_us):
        first, last = match["word_indices"][0], match["word_indices"][-1]
        candidates.append({
            "first_index": first, "last_index": last,
            "start_us": match["source_start_us"], "end_us": match["source_end_us"],
            "matched_text": match["matched_text"],
            "before": "".join(w["text"] for w in words[max(0, first - _CONTEXT_WORDS):first]),
            "after": "".join(w["text"] for w in words[last + 1:last + 1 + _CONTEXT_WORDS]),
            "anchor_distance_us": None if anchor_us is None else abs(match["source_start_us"] - anchor_us),
        })
    payload = {"source_sha256": source["sha256"], "alignment_sha256": alignment["sha256"],
               "text": text, "scope_start_us": scope_start_us, "scope_end_us": scope_end_us,
               "anchor_us": anchor_us, "candidate_count": len(candidates), "candidates": candidates}
    params = {"text": text, "scope_start_us": scope_start_us, "scope_end_us": scope_end_us, "anchor_us": anchor_us}
    limitations = ["Matches are whole-token contiguous ASR matches; a phrase split inside one ASR token is not found."]
    return _register(context, _row(context, evidence_id, "phrase_search", source_id, params, limitations), payload)


def _frame_times_cached(context, source_id) -> list[int]:
    from .visual import _frame_times

    source = context.source(source_id)
    cache = context.evidence_dir / f".frametimes-{source['sha256'][:16]}.json"
    if cache.exists():
        data = read_json(cache)
        if data.get("source_sha256") == source["sha256"]:
            return data["frame_times_us"]
    times = _frame_times(Path(source["path"]))
    write_json(cache, {"source_sha256": source["sha256"], "frame_times_us": times})
    return times


def frames(context, source_id, evidence_id, at_us: list[int], *, neighbors=0, width=960):
    from .media import find_tool

    if not at_us:
        raise SlicingError("Frame evidence requires at least one time")
    if not 0 <= neighbors <= 12:
        raise SlicingError("Frame neighbors must be between 0 and 12")
    _ensure_new(context, evidence_id)
    source = context.source(source_id)
    times = _frame_times_cached(context, source_id)
    selected: list[tuple[int, int]] = []
    for requested in at_us:
        if not 0 <= requested < source["duration_us"]:
            raise SlicingError("Frame time is outside the source")
        nearest = min(range(len(times)), key=lambda i: (abs(times[i] - requested), times[i]))
        for index in range(max(0, nearest - neighbors), min(len(times), nearest + neighbors + 1)):
            if index not in [item[0] for item in selected]:
                selected.append((index, requested))
    selected.sort()
    folder = context.evidence_dir / evidence_id
    if folder.exists():
        raise SlicingError(f"Frame evidence folder already exists: {folder}")
    folder.mkdir(parents=True)
    rows = []
    for index, requested in selected:
        frame_us = times[index]
        gaps = [abs(times[j] - frame_us) for j in (index - 1, index + 1) if 0 <= j < len(times)]
        lead_us = min([10_000, *[int(gap * 0.4) for gap in gaps]])
        image = folder / f"f{index:06d}_{frame_us}us.png"
        subprocess.run(
            [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
             "-ss", f"{max(0, frame_us - lead_us) / 1_000_000:.6f}", "-i", source["path"],
             "-frames:v", "1", "-vf", f"scale={width}:-2", str(image)],
            check=True, capture_output=True,
        )
        rows.append({"index": index, "frame_time_us": frame_us, "requested_us": requested,
                     "file": f"{evidence_id}/{image.name}", "sha256": file_sha256(image)})
    payload = {"source_sha256": source["sha256"], "frames": rows}
    params = {"at_us": at_us, "neighbors": neighbors, "width": width}
    limitations = ["Images show decoded frames only; picture content and cleanliness need visual review."]
    return _register(context, _row(context, evidence_id, "frames", source_id, params, limitations,
                                   frame_times_us=[row["frame_time_us"] for row in rows]), payload)


def audio_gap(context, source_id, evidence_id, *, after_word=None, at_us=None):
    """Measure the previous word's decay and the next word's onset in one word gap."""
    from .audio import analyze_gap, word_gap_index

    if (after_word is None) == (at_us is None):
        raise SlicingError("Choose exactly one of after_word or at")
    _ensure_new(context, evidence_id)
    source = context.source(source_id)
    alignment = context.alignment(source_id)
    words = alignment["words"]
    index = after_word if after_word is not None else word_gap_index(words, at_us)
    payload = {"source_sha256": source["sha256"], "alignment_sha256": alignment["sha256"],
               **analyze_gap(source["path"], words, index, source["duration_us"])}
    params = {"after_word": index, "at_us": at_us}
    limitations = [
        "Energy-based: low energy is not proof of silence; breaths or background sound move both edges.",
        "out_window is for an outgoing cut (keeps the decay, stops before the next onset); "
        "in_window is for an incoming cut. Listen to the chosen point.",
    ]
    if payload["decay_end"] is None:
        limitations.append("The previous word does not decay below the threshold before the next word.")
    return _register(context, _row(context, evidence_id, "audio_gap", source_id, params, limitations), payload)
