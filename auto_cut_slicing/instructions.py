"""Conservative compilation of local slicing instructions into planner clips."""

import re
from decimal import Decimal
from typing import Any

from .alignment import normalized_text, parse_time_us
from .errors import SlicingError

_STRUCTURAL = str.maketrans("０１２３４５６７８９：（）＋－；，", "0123456789:()+-;,")
_TIME = r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?"
_QUOTED = r'(?P<quote>["“‘「])(?P<text>[^"“”‘’「」]+)(?P<close>["”’」])'
_QUOTE_CLOSE = {'"': '"', "“": "”", "‘": "’", "「": "」"}
_RANGE = re.compile(
    rf"(?P<start>{_TIME})\s*(?:\((?P<start_note>[^()]*)\)\s*)?-\s*"
    rf"(?:\((?P<edit_note>[^()]*)\)\s*-\s*)?(?P<end>{_TIME}|结束|结尾|源结束)\s*\Z"
)
_DELETE = re.compile(
    rf"(?:(?P<anchor>{_TIME})\s*)?(?:剪掉|删除)\s*{_QUOTED}\s*"
    r"(?:这几个字|这几字|[一二三四五六七八九十百两\d]+个?字)?\s*"
    r"(?P<audio>(?:的)?(?:声音|语音|音频))?\s*"
    r"(?:,\s*(?P<silence>(?:可以用)?(?:停顿|静音)占位))?\s*"
    r"(?:,\s*(?:其他|其它)不变)?\s*\Z"
)
_VISUAL = re.compile(
    rf"语音是\s*{_QUOTED}\s*配后面\s*\d+(?:\.\d+)?\s*(?:s|秒)"
    r"\s*只有\s*NaOH\s*的画面\s*\Z"
)
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")


def _time(value: str | int | float, field: str) -> int:
    if isinstance(value, str):
        value = value.translate(_STRUCTURAL).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            value = Decimal(value)
    return parse_time_us(value, field=field)


def _seconds(value_us: int) -> int | float:
    return value_us // 1_000_000 if value_us % 1_000_000 == 0 else value_us / 1_000_000


def _normalize_structure(text: str) -> str:
    result = []
    closing_quote = None
    for char in text:
        if closing_quote is not None:
            result.append(char)
            if char == closing_quote:
                closing_quote = None
        else:
            result.append(char.translate(_STRUCTURAL))
            if char in _QUOTE_CLOSE:
                closing_quote = _QUOTE_CLOSE[char]
    return "".join(result)


def _parts(text: str) -> list[str]:
    parts = []
    start = depth = 0
    closing_quote = None
    for index, char in enumerate(text):
        if closing_quote is not None:
            if char == closing_quote:
                closing_quote = None
            continue
        if char in _QUOTE_CLOSE:
            closing_quote = _QUOTE_CLOSE[char]
        elif char == "(":
            depth += 1
            if depth > 1:
                raise SlicingError("Unsupported nested instruction annotations")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise SlicingError("Unbalanced instruction annotations")
        elif char in "+;" and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    if closing_quote is not None or depth:
        raise SlicingError("Unbalanced instruction quotes or annotations")
    parts.append(text[start:].strip())
    if not all(parts):
        raise SlicingError("Instruction contains an empty retained range")
    return parts


def _phrase(match: re.Match) -> str:
    if _QUOTE_CLOSE[match["quote"]] != match["close"] or not normalized_text(match["text"]):
        raise SlicingError("Instruction requires matching quotes around nonempty spoken text")
    return match["text"]


def _delete(note: str, raw: str, start_us: int, end_us: int) -> tuple[str, dict] | None:
    match = _DELETE.fullmatch(note)
    if match is None:
        return None
    item = {"text": _phrase(match), "source_text": raw}
    if match["anchor"] is not None:
        anchor_us = _time(match["anchor"], "word search anchor")
        if not start_us <= anchor_us < end_us:
            raise SlicingError("Word search anchor must be inside the selected clip")
        item["rough_time"] = _seconds(anchor_us)
    if match["audio"] is not None or match["silence"] is not None:
        return "edits", {"id": "edit1", "kind": "mute_word", **item}
    return "deletions", {"id": "delete1", "kind": "word", **item}


def _freeze(evidence: Any, raw: str, start_us: int, end_us: int, duration_us: int) -> dict:
    if not isinstance(evidence, dict) or any(
        field not in evidence for field in ("source_sha256", "frame_time", "end")
    ):
        raise SlicingError(
            "Picture replacement requires visual_evidence with source_sha256, frame_time and end; "
            "inspect the selected source frame and supply its replacement boundary"
        )
    digest = evidence["source_sha256"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SlicingError("visual_evidence source_sha256 must be a SHA-256 digest")
    frame_us = _time(evidence["frame_time"], "visual_evidence frame_time")
    boundary_us = _time(evidence["end"], "visual_evidence end")
    if frame_us >= duration_us or not start_us < boundary_us <= end_us:
        raise SlicingError("visual_evidence frame or replacement range is outside the selected source/clip")
    return {
        "id": "edit1", "kind": "freeze_frame", "start": _seconds(start_us),
        "end": _seconds(boundary_us), "frame_time": _seconds(frame_us), "source_text": raw,
    }


def parse_instruction(
    text: str, source_id: str, duration_us: int, *, visual_evidence: dict | None = None
) -> list[dict[str, Any]]:
    """Compile supported instructions, preserving the original string in every edit label.

    Word times are search anchors only. The planner resolves exact source-bound ASR intervals.
    Visual source identity is validated against source bytes by the batch request adapter.
    """
    if not isinstance(text, str) or not text.strip():
        raise SlicingError("Instruction must be a nonempty string")
    if type(duration_us) is not int or duration_us <= 0:
        raise SlicingError("Instruction source duration_us must be a positive integer")
    if (
        not isinstance(source_id, str) or not source_id.strip()
        or any(char in source_id for char in "/\\")
        or any(ord(char) < 32 for char in source_id)
    ):
        raise SlicingError("Instruction source_id must be a nonempty ID without path separators")
    normalized = _normalize_structure(text).strip()
    clips = []
    for number, part in enumerate(_parts(normalized), start=1):
        match = _RANGE.fullmatch(part)
        clip: dict[str, Any] = {"id": f"part{number}", "source": source_id}
        if match is None:
            deletion = _delete(part, text, 0, duration_us)
            if deletion is None:
                raise SlicingError(f"Unsupported slicing instruction: {part!r}")
            clip.update(start=0, end=_seconds(duration_us))
            group, item = deletion
            clip[group] = [item]
            clips.append(clip)
            continue
        start_us = _time(match["start"], "clip start")
        end_us = duration_us if match["end"] in {"结束", "结尾", "源结束"} else _time(
            match["end"], "clip end"
        )
        if not 0 <= start_us < end_us <= duration_us:
            raise SlicingError("Instruction range requires 0 <= start < end <= source duration")
        clip.update(start=_seconds(start_us), end=_seconds(end_us))
        if match["start_note"] is not None:
            note = match["start_note"].strip()
            visual = _VISUAL.fullmatch(note)
            if visual is not None:
                clip["start_cue"] = {"text": _phrase(visual)}
                clip["edits"] = [_freeze(visual_evidence, text, start_us, end_us, duration_us)]
            elif note in {"首先", "接下来"}:
                clip["start_cue"] = {"text": note}
            else:
                if "画面" in note and visual_evidence is None:
                    raise SlicingError("Picture instruction requires source-bound visual_evidence")
                raise SlicingError(f"Unsupported clip start annotation: {note!r}")
        if match["edit_note"] is not None:
            deletion = _delete(match["edit_note"].strip(), text, start_us, end_us)
            if deletion is None:
                raise SlicingError(f"Unsupported edit annotation: {match['edit_note']!r}")
            group, item = deletion
            if group in clip:
                item["id"] = "edit2"
                clip[group].append(item)
            else:
                clip[group] = [item]
        clips.append(clip)
    return clips
