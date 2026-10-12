"""Compile independent slices and ordered concatenation into one editable plan."""

import re
from pathlib import Path
from typing import Any, Callable

from .alignment import load_alignment, parse_time_us, protect_must_keep, resolve_word_deletion
from .boundaries import resolve_sentence_range
from .errors import SlicingError
from .io import json_sha256, read_json

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SlicingError(f"{field} must be an object")
    return value


def _rows(value: Any, field: str, *, allow_empty: bool = False) -> list:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise SlicingError(
            f"{field} must be a {'possibly empty' if allow_empty else 'nonempty'} list"
        )
    return value


def _known_keys(row: dict, allowed: set[str], field: str) -> None:
    unknown = set(row) - allowed
    if unknown:
        raise SlicingError(f"Unsupported {field} fields: {', '.join(sorted(unknown))}")


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise SlicingError(f"{field} requires a nonempty ID without path separators")
    if any(ord(char) < 32 for char in value):
        raise SlicingError(f"{field} cannot contain control characters")
    return value


def _unique_id(value: Any, field: str, seen: set[str]) -> str:
    result = _identity(value, field)
    if result in seen:
        raise SlicingError(f"Duplicate {field}: {result}")
    seen.add(result)
    return result


def _safe_name(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in (".", "..")
        or value[-1:] in (".", " ")
        or _UNSAFE_NAME.search(value)
        or _DEVICE_NAME.fullmatch(value)
    ):
        raise SlicingError(f"{field} must be a safe local filename")
    return value


def _path(value: Any, base: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SlicingError(f"{field} must be a file path")
    result = Path(value)
    result = (base / result).resolve() if not result.is_absolute() else result.resolve()
    if not result.is_file():
        raise SlicingError(f"{field} does not exist: {result}")
    return result


def _range(row: dict, duration_us: int, field: str) -> tuple[int, int]:
    start = parse_time_us(row.get("start"), field=f"{field} start")
    end = parse_time_us(row.get("end"), field=f"{field} end")
    if start >= end or end > duration_us:
        raise SlicingError(f"{field} requires 0 <= start < end <= source duration")
    return start, end


def _descriptor(value: Any, path: Path) -> dict[str, Any]:
    result = dict(_object(value, "source descriptor"))
    if type(result.get("duration_us")) is not int or result["duration_us"] <= 0:
        raise SlicingError("Source descriptor requires a positive integer duration_us")
    digest = result.get("sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SlicingError("Source descriptor requires a SHA-256 digest")
    result["sha256"] = digest.lower()
    result["path"] = str(path)
    return result


def _resolve_word_request(row, source, clip_start, clip_end, item_id):
    if "alignment" not in source:
        raise SlicingError(f"Speech operation {item_id} requires source-bound ASR")
    rough = None
    if "rough_time" in row:
        rough = parse_time_us(row["rough_time"], field="rough_time")
    radius = parse_time_us(row.get("search_radius", 30), field="search_radius")
    return resolve_word_deletion(
        source["alignment"],
        row.get("text"),
        clip_start_us=clip_start,
        clip_end_us=clip_end,
        rough_time_us=rough,
        search_radius_us=radius,
        must_keep=row.get("must_keep", []),
    )


def _clip_effects(clip, source, clip_id, source_id, nominal_start, clip_start, clip_end, seen):
    effects = []
    for value in _rows(clip.get("edits", []), "edits", allow_empty=True):
        row = _object(value, "edit")
        edit_id = _unique_id(row.get("id"), "edit ID", seen)
        item_id = f"{clip_id}/{edit_id}"
        source_text = row.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            raise SlicingError(f"Edit {item_id} requires exact nonempty source_text")
        kind = row.get("kind")
        if kind == "mute_word":
            _known_keys(
                row,
                {"id", "kind", "text", "rough_time", "search_radius", "must_keep", "source_text"},
                "mute_word edit",
            )
            resolved = _resolve_word_request(row, source, clip_start, clip_end, item_id)
            start, end = resolved["source_start_us"], resolved["source_end_us"]
            detail = {
                **resolved,
                "requested_text": row["text"],
                "resolved_source_start_us": start,
                "resolved_source_end_us": end,
            }
            effect_kind = "mute"
        elif kind == "freeze_frame":
            _known_keys(
                row,
                {"id", "kind", "start", "end", "frame_time", "source_text"},
                "freeze_frame edit",
            )
            start, end = _range(row, source["duration_us"], f"edit {item_id}")
            detail = {}
            if "start_cue" in clip and start == nominal_start:
                detail = {"requested_start_us": start, "resolved_start_us": clip_start}
                start = clip_start
            if start >= end or start < clip_start or end > clip_end:
                raise SlicingError(f"Edit {item_id} is outside its selected clip or empty")
            frame = parse_time_us(row.get("frame_time"), field=f"edit {item_id} frame_time")
            if frame >= source["duration_us"]:
                raise SlicingError(f"Edit {item_id} frame_time must be inside the complete source")
            detail["frame_time_us"] = frame
            effect_kind = "freeze_frame"
        else:
            raise SlicingError(f"Unknown edit kind for {item_id}: {kind}")
        effects.append({
            **detail,
            "kind": effect_kind,
            "item_id": item_id,
            "clip_id": clip_id,
            "source_id": source_id,
            "source_text": source_text,
            "source_start_us": start,
            "source_end_us": end,
        })
    effects.sort(key=lambda row: (row["source_start_us"], row["source_end_us"], row["item_id"]))
    previous_by_kind = {}
    for effect in effects:
        if effect["source_start_us"] < previous_by_kind.get(effect["kind"], clip_start):
            raise SlicingError(f"Overlapping {effect['kind']} effects in clip {clip_id}")
        previous_by_kind[effect["kind"]] = effect["source_end_us"]
    return effects


def _protect_audio_operations(deletions, effects):
    operations = [*deletions, *(row for row in effects if row["kind"] == "mute")]
    for operation in operations:
        for owner in operations:
            for protected in owner["must_keep_evidence"]:
                for match in protected["matches"]:
                    if (
                        match["source_start_us"] < operation["source_end_us"]
                        and match["source_end_us"] > operation["source_start_us"]
                    ):
                        raise SlicingError(
                            f"Edit {operation['item_id']} overlaps must_keep protected by {owner['item_id']}"
                        )


def _map_effects(output, effects, segments):
    for effect in effects:
        fragments = []
        for segment in segments:
            start = max(effect["source_start_us"], segment["source_start_us"])
            end = min(
                effect["source_end_us"], segment["source_start_us"] + segment["duration_us"]
            )
            if start < end:
                fragments.append({
                    **effect,
                    "source_start_us": start,
                    "source_end_us": end,
                    "target_start_us": segment["target_start_us"] + start - segment["source_start_us"],
                    "duration_us": end - start,
                })
        if not fragments:
            raise SlicingError(f"Effect {effect['item_id']} has no retained source interval")
        output.setdefault("effects", []).extend(fragments)
        output["markers"].append({
            "item_id": effect["item_id"],
            "source_text": effect["source_text"],
            "source_id": effect["source_id"],
            "source_start_us": effect["source_start_us"],
            "source_end_us": effect["source_end_us"],
            "target_start_us": fragments[0]["target_start_us"],
            "execution_status": "executed",
        })


def compile_job(job_path: Path, *, describe: Callable | None = None) -> dict[str, Any]:
    """Validate the complete job before producing any cut or draft artifact."""
    job_path = Path(job_path).resolve()
    try:
        job = _object(read_json(job_path), "job")
    except (OSError, ValueError) as exc:
        if isinstance(exc, SlicingError):
            raise
        raise SlicingError(f"Cannot read slicing job: {job_path}") from exc
    if type(job.get("schema_version")) is not int or job["schema_version"] != 1:
        raise SlicingError("Job schema_version must be 1")
    _known_keys(job, {"schema_version", "name", "sources", "outputs"}, "job")
    name = _safe_name(job.get("name"), "job name")
    if describe is None:
        from .media import describe_source

        describe = describe_source
    sources = {}
    seen_sources: set[str] = set()
    for source_value in _rows(job.get("sources"), "sources"):
        source_row = _object(source_value, "source")
        _known_keys(source_row, {"id", "path", "asr"}, "source")
        source_id = _unique_id(source_row.get("id"), "source ID", seen_sources)
        path = _path(source_row.get("path"), job_path.parent, f"source {source_id} path")
        source = _descriptor(describe(path), path)
        if "asr" in source_row:
            alignment_path = _path(source_row["asr"], job_path.parent, f"source {source_id} ASR")
            source["alignment"] = load_alignment(alignment_path, source)
        sources[source_id] = source
    outputs = []
    seen_outputs: set[str] = set()
    seen_names: set[str] = set()
    seen_clips: set[str] = set()
    for output_value in _rows(job.get("outputs"), "outputs"):
        output_row = _object(output_value, "output")
        _known_keys(output_row, {"id", "name", "clips"}, "output")
        output_id = _unique_id(output_row.get("id"), "output ID", seen_outputs)
        output_name = _safe_name(output_row.get("name", output_id), "output name")
        if output_name.casefold() in seen_names:
            raise SlicingError(f"Output names must be unique on Windows: {output_name}")
        seen_names.add(output_name.casefold())
        output = {
            "id": output_id,
            "name": output_name,
            "duration_us": 0,
            "segments": [],
            "markers": [],
            "edits": [],
        }
        selected_ranges = []
        for clip_value in _rows(output_row.get("clips"), f"output {output_id} clips"):
            clip = _object(clip_value, "clip")
            _known_keys(
                clip, {"id", "source", "start", "end", "deletions", "start_cue", "edits", "boundary_policy"}, "clip"
            )
            clip_id = _unique_id(clip.get("id"), "clip ID", seen_clips)
            source_id = _identity(clip.get("source"), "clip source")
            if source_id not in sources:
                raise SlicingError(f"Clip {clip_id} references unknown source: {source_id}")
            source = sources[source_id]
            clip_start, clip_end = _range(clip, source["duration_us"], f"clip {clip_id}")
            nominal_start, nominal_end = clip_start, clip_end
            policy = clip.get("boundary_policy", "exact")
            if not isinstance(policy, str) or policy not in {"exact", "sentence"}:
                raise SlicingError(f"Clip {clip_id} boundary_policy must be exact or sentence")
            boundary = None
            if policy == "sentence":
                boundary = resolve_sentence_range(
                    source.get("alignment"), start_us=clip_start, end_us=clip_end,
                    duration_us=source["duration_us"], preserve_start="start_cue" in clip,
                )
                clip_start, clip_end = boundary["start_us"], boundary["end_us"]
            if "start_cue" in clip:
                cue = _object(clip["start_cue"], "start_cue")
                _known_keys(cue, {"text", "search_radius"}, "start_cue")
                cue_resolved = _resolve_word_request(
                    {"text": cue.get("text"), "rough_time": clip["start"],
                     "search_radius": cue.get("search_radius", 3)},
                    source, 0, clip_end, f"{clip_id}/start_cue",
                )
                clip_start = cue_resolved["source_start_us"]
                output.setdefault("start_cues", []).append({
                    **cue_resolved,
                    "clip_id": clip_id,
                    "source_id": source_id,
                    "requested_text": cue["text"],
                    "requested_start_us": nominal_start,
                })
            if clip_start >= clip_end:
                raise SlicingError(f"Clip {clip_id} is empty after resolving its boundaries")
            sentence_expanded = boundary is not None and (
                boundary["start_us"] < nominal_start or boundary["end_us"] > nominal_end
            )
            selected_start = clip_start if "start_cue" in clip else nominal_start
            if boundary is not None:
                output.setdefault("boundary_adjustments", []).append({
                    "clip_id": clip_id, "source_id": source_id, "policy": policy,
                    "requested_start_us": nominal_start, "requested_end_us": nominal_end,
                    "resolved_start_us": clip_start, "resolved_end_us": clip_end,
                    "evidence": boundary["evidence"],
                })
            # Only reject duplicate material introduced by semantic expansion.
            # Explicitly overlapping source selections still represent intentional reuse.
            for previous in selected_ranges:
                if previous["source_id"] != source_id:
                    continue
                original_overlap = (
                    selected_start < previous["selected_end"]
                    and nominal_end > previous["selected_start"]
                )
                resolved_overlap = (
                    clip_start < previous["end"] and clip_end > previous["start"]
                )
                expanded = sentence_expanded or previous["expanded"]
                if resolved_overlap and not original_overlap and expanded:
                    raise SlicingError(
                        f"Sentence boundary expansion introduces overlap between clips "
                        f"{previous['clip_id']} and {clip_id}; review the source selections"
                    )
            selected_ranges.append({
                "source_id": source_id, "clip_id": clip_id,
                "selected_start": selected_start, "selected_end": nominal_end,
                "start": clip_start, "end": clip_end,
                "expanded": sentence_expanded,
            })
            deletions = []
            seen_deletions: set[str] = set()
            for deletion_value in _rows(clip.get("deletions", []), "deletions", allow_empty=True):
                deletion = _object(deletion_value, "deletion")
                deletion_id = _unique_id(deletion.get("id"), "deletion ID", seen_deletions)
                item_id = f"{clip_id}/{deletion_id}"
                source_text = deletion.get("source_text")
                if not isinstance(source_text, str) or not source_text.strip():
                    raise SlicingError(f"Deletion {item_id} requires exact nonempty source_text")
                kind = deletion.get("kind")
                if kind == "word":
                    _known_keys(
                        deletion,
                        {
                            "id",
                            "kind",
                            "text",
                            "rough_time",
                            "search_radius",
                            "must_keep",
                            "source_text",
                        },
                        "word deletion",
                    )
                    if "alignment" not in source:
                        raise SlicingError(f"Word deletion {item_id} requires source-bound ASR")
                    rough = None
                    if "rough_time" in deletion:
                        rough = parse_time_us(deletion["rough_time"], field="rough_time")
                    radius = parse_time_us(deletion.get("search_radius", 30), field="search_radius")
                    resolved = resolve_word_deletion(
                        source["alignment"],
                        deletion.get("text"),
                        clip_start_us=clip_start,
                        clip_end_us=clip_end,
                        rough_time_us=rough,
                        search_radius_us=radius,
                        must_keep=deletion.get("must_keep", []),
                    )
                    start, end = resolved["source_start_us"], resolved["source_end_us"]
                    detail = {**resolved, "requested_text": deletion["text"]}
                elif kind == "range":
                    _known_keys(
                        deletion,
                        {"id", "kind", "start", "end", "must_keep", "source_text"},
                        "range deletion",
                    )
                    start, end = _range(deletion, source["duration_us"], f"deletion {item_id}")
                    if start < clip_start or end > clip_end:
                        raise SlicingError(f"Deletion {item_id} is outside its selected clip")
                    detail = {
                        "must_keep_evidence": protect_must_keep(
                            source.get("alignment"),
                            deletion.get("must_keep", []),
                            delete_start_us=start,
                            delete_end_us=end,
                            clip_start_us=clip_start,
                            clip_end_us=clip_end,
                        )
                    }
                else:
                    raise SlicingError(f"Unknown deletion kind for {item_id}: {kind}")
                deletions.append(
                    {
                        **detail,
                        "clip_id": clip_id,
                        "item_id": item_id,
                        "kind": kind,
                        "source_id": source_id,
                        "source_start_us": start,
                        "source_end_us": end,
                        "source_text": source_text,
                    }
                )
            deletions.sort(
                key=lambda row: (row["source_start_us"], row["source_end_us"], row["item_id"])
            )
            effects = _clip_effects(
                clip, source, clip_id, source_id, nominal_start, clip_start, clip_end, seen_deletions
            )
            previous_end = clip_start
            for deletion in deletions:
                if deletion["source_start_us"] < previous_end:
                    raise SlicingError(f"Overlapping deletion requests in clip {clip_id}")
                previous_end = deletion["source_end_us"]
            _protect_audio_operations(deletions, effects)
            cursor = clip_start
            segment_number = 0
            first_segment = len(output["segments"])
            for deletion in [*deletions, None]:
                keep_end = deletion["source_start_us"] if deletion is not None else clip_end
                if keep_end > cursor:
                    segment_number += 1
                    output["segments"].append(
                        {
                            "id": f"{output_id}/{clip_id}/keep-{segment_number}",
                            "clip_id": clip_id,
                            "source_id": source_id,
                            "source_start_us": cursor,
                            "duration_us": keep_end - cursor,
                            "target_start_us": output["duration_us"],
                        }
                    )
                    output["duration_us"] += keep_end - cursor
                if deletion is not None:
                    deletion["target_join_us"] = output["duration_us"]
                    output["edits"].append(deletion)
                    cursor = deletion["source_end_us"]
            _map_effects(output, effects, output["segments"][first_segment:])
            # A wholly removed clip intentionally has no retained segment.
        if output["duration_us"] <= 0:
            raise SlicingError(f"Output {output_id} is empty after deletion")
        for edit in output["edits"]:
            output["markers"].append(
                {
                    "item_id": edit["item_id"],
                    "source_text": edit["source_text"],
                    "source_id": edit["source_id"],
                    "source_start_us": edit["source_start_us"],
                    "source_end_us": edit["source_end_us"],
                    "target_start_us": min(edit["target_join_us"], output["duration_us"] - 1),
                    "execution_status": "executed",
                }
            )
        if "effects" in output:
            output["effects"].sort(
                key=lambda row: (row["target_start_us"], row["kind"], row["item_id"])
            )
        outputs.append(output)
    plan = {
        "schema_version": 1,
        "workflow_mode": "slicing",
        "name": name,
        "job_sha256": json_sha256(job),
        "sources": sources,
        "outputs": outputs,
    }
    plan["plan_sha256"] = json_sha256(plan)
    return plan
