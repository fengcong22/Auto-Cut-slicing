"""Strict, source-bound word alignment for destructive slicing decisions."""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import SlicingError
from .io import file_sha256, read_json

MICROSECONDS = 1_000_000
MAX_WORD_GAP_US = 1_500_000
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_CLOCK = re.compile(r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?\Z")


def parse_time_us(value: Any, *, field: str = "time") -> int:
    """Parse seconds or a clock timestamp without silently rounding boundaries."""
    if isinstance(value, bool):
        raise SlicingError(f"{field} must be seconds or a clock timestamp, not a boolean")
    try:
        if isinstance(value, (int, float, Decimal)):
            seconds = Decimal(str(value))
        elif isinstance(value, str) and _CLOCK.fullmatch(value):
            parts = value.split(":")
            tail = Decimal(parts[-1])
            if tail >= 60 or (len(parts) == 3 and int(parts[1]) >= 60):
                raise SlicingError(f"{field} has an invalid clock component")
            seconds = tail + Decimal(parts[-2]) * 60
            if len(parts) == 3:
                seconds += Decimal(parts[0]) * 3600
        else:
            raise SlicingError(f"{field} must be numeric seconds or MM:SS / HH:MM:SS")
        micros = seconds * MICROSECONDS
        if not micros.is_finite() or micros < 0 or micros != micros.to_integral_value():
            raise SlicingError(f"{field} must be finite, nonnegative and exact to microseconds")
        return int(micros)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        if isinstance(exc, SlicingError):
            raise
        raise SlicingError(f"{field} is not a valid timestamp") from exc


def normalized_text(text: str) -> str:
    """Ignore punctuation and whitespace; retain every actual spoken character."""
    return "".join(
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _phrase(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not normalized_text(value):
        raise SlicingError(f"{field} must contain spoken text")
    return value


def load_alignment(path: Path, source: dict[str, Any]) -> dict[str, Any]:
    """Validate a word manifest against the currently selected complete source."""
    path = Path(path).resolve()
    try:
        alignment_hash = file_sha256(path)
        raw = read_json(path)
        if file_sha256(path) != alignment_hash:
            raise SlicingError("ASR alignment changed while reading source-bound evidence")
    except (OSError, ValueError) as exc:
        if isinstance(exc, SlicingError):
            raise
        raise SlicingError(f"Cannot read ASR alignment: {path}") from exc
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 1
    ):
        raise SlicingError("ASR alignment schema_version must be 1")
    digest = raw.get("source_sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SlicingError("ASR alignment requires a source_sha256")
    if digest.lower() != source["sha256"].lower():
        raise SlicingError("ASR alignment source_sha256 does not match the current source")
    identities = {}
    for key in ("provider", "model", "adapter"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise SlicingError(f"ASR alignment requires a nonempty {key} identity")
        identities[key] = raw[key]
    raw_words = raw.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        raise SlicingError("ASR alignment requires a nonempty words list")
    words = []
    previous_end = 0
    for index, row in enumerate(raw_words):
        if not isinstance(row, dict):
            raise SlicingError(f"ASR word {index} must be an object")
        word_text = _phrase(row.get("text"), field=f"ASR word {index} text")
        start = parse_time_us(row.get("start"), field=f"ASR word {index} start")
        end = parse_time_us(row.get("end"), field=f"ASR word {index} end")
        if start < previous_end or start >= end or end > source["duration_us"]:
            raise SlicingError(f"ASR word {index} is overlapping, unordered or outside the source")
        words.append({"text": word_text, "start_us": start, "end_us": end})
        previous_end = end
    result = {
        "path": str(path),
        "sha256": alignment_hash,
        "source_sha256": digest.lower(),
        **identities,
        "words": words,
    }
    if "alignment_audio_sha256" in raw:
        audio_hash = raw["alignment_audio_sha256"]
        if not isinstance(audio_hash, str) or not _HASH.fullmatch(audio_hash):
            raise SlicingError("alignment_audio_sha256 must be a SHA-256 digest")
        result["alignment_audio_sha256"] = audio_hash.lower()
    return result


def find_phrase_matches(
    alignment: dict[str, Any], text: str, *, clip_start_us: int, clip_end_us: int
) -> list[dict[str, Any]]:
    """Find whole-token contiguous matches; a long silence splits an utterance."""
    wanted = normalized_text(_phrase(text, field="phrase"))
    words = alignment["words"]
    matches = []
    for start_index, first in enumerate(words):
        if first["start_us"] < clip_start_us or first["end_us"] > clip_end_us:
            continue
        current = ""
        previous_end = first["start_us"]
        for end_index in range(start_index, len(words)):
            word = words[end_index]
            if word["end_us"] > clip_end_us or word["start_us"] - previous_end > MAX_WORD_GAP_US:
                break
            current += normalized_text(word["text"])
            if not wanted.startswith(current):
                break
            if current == wanted:
                matches.append(
                    {
                        "source_start_us": first["start_us"],
                        "source_end_us": word["end_us"],
                        "word_indices": list(range(start_index, end_index + 1)),
                        "matched_text": "".join(
                            row["text"] for row in words[start_index : end_index + 1]
                        ),
                    }
                )
                break
            previous_end = word["end_us"]
    return matches


def protect_must_keep(
    alignment: dict[str, Any] | None,
    phrases: Any,
    *,
    delete_start_us: int,
    delete_end_us: int,
    clip_start_us: int,
    clip_end_us: int,
) -> list[dict[str, Any]]:
    if not isinstance(phrases, list):
        raise SlicingError("must_keep must be a list of phrases")
    if phrases and alignment is None:
        raise SlicingError("must_keep protection requires source-bound ASR word alignment")
    evidence = []
    for phrase in phrases:
        phrase = _phrase(phrase, field="must_keep phrase")
        matches = find_phrase_matches(
            alignment, phrase, clip_start_us=clip_start_us, clip_end_us=clip_end_us
        )
        if not matches:
            raise SlicingError(
                f"must_keep phrase could not be verified in the selected clip: {phrase}"
            )
        for match in matches:
            if (
                match["source_start_us"] < delete_end_us
                and match["source_end_us"] > delete_start_us
            ):
                raise SlicingError(f"Deletion would overlap must_keep phrase: {phrase}")
        evidence.append({"text": phrase, "matches": matches})
    return evidence


def resolve_word_deletion(
    alignment: dict[str, Any],
    text: str,
    *,
    clip_start_us: int,
    clip_end_us: int,
    rough_time_us: int | None = None,
    search_radius_us: int = 30_000_000,
    must_keep: Any = None,
) -> dict[str, Any]:
    matches = find_phrase_matches(
        alignment, text, clip_start_us=clip_start_us, clip_end_us=clip_end_us
    )
    if rough_time_us is not None:
        matches = [
            row
            for row in matches
            if abs(row["source_start_us"] - rough_time_us) <= search_radius_us
        ]
    if len(matches) != 1:
        raise SlicingError(
            f"Word deletion requires one unique exact ASR match; found {len(matches)}: {text}"
        )
    match = matches[0]
    protection = protect_must_keep(
        alignment,
        [] if must_keep is None else must_keep,
        delete_start_us=match["source_start_us"],
        delete_end_us=match["source_end_us"],
        clip_start_us=clip_start_us,
        clip_end_us=clip_end_us,
    )
    return {
        **match,
        "match_strategy": "unique_exact_whole_token",
        "must_keep_evidence": protection,
        "alignment_sha256": alignment["sha256"],
        "provider": alignment["provider"],
        "model": alignment["model"],
        "adapter": alignment["adapter"],
    }
