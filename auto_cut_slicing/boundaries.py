"""Resolve numeric slice edges using hash-bound, complete-token sentence evidence."""

import re
import unicodedata
from pathlib import Path
from typing import Any

from .alignment import normalized_text, parse_time_us
from .errors import SlicingError
from .io import file_sha256, read_json

_TERMINALS = frozenset("。！？.!?")
_CLOSERS = frozenset("\"'”’」』）》】〕〉»")
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "a.m",
        "p.m",
        "fig",
        "eq",
        "no",
        "nos",
        "vol",
        "inc",
        "ltd",
        "co",
        "corp",
        "dept",
        "approx",
        "cf",
        "al",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "capt", "gen", "sgt", "gov", "rev", "lt", "col", "sen", "rep",
        "cmdr", "adm", "cpl", "pvt", "maj", "brig", "hon", "supt",
    }
)
_DOT_WORD = re.compile(r"[A-Za-z]+(?:\.[A-Za-z]+)*$")


def _spoken(char: str) -> bool:
    return not char.isspace() and not unicodedata.category(char).startswith("P")


def _terminal(text: str, index: int) -> bool:
    """Recognize a period conservatively; lexical dots do not prove sentence closure."""
    char = text[index]
    if char not in _TERMINALS:
        return False
    if char != ".":
        return True
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous == "." or following == ".":
        # Ellipses communicate hesitation/omission, not a known closed sentence.
        return False
    if previous.isdigit() and following.isdigit():
        return False
    match = _DOT_WORD.search(text[:index])
    if match:
        word = match.group().lower()
        parts = word.split(".")
        if (
            word in _ABBREVIATIONS
            or parts[-1] in _ABBREVIATIONS
            or len(parts[-1]) == 1
            or all(len(part) == 1 for part in parts)
        ):
            return False
    return True


def _load_sentence_manifest(alignment: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(alignment["path"])
        expected = alignment["sha256"]
        if file_sha256(path) != expected:
            raise SlicingError("Sentence alignment SHA-256 changed before reading evidence")
        raw = read_json(path)
        if file_sha256(path) != expected:
            raise SlicingError("Sentence alignment SHA-256 changed while reading evidence")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, SlicingError):
            raise
        raise SlicingError("Cannot read hash-bound sentence alignment evidence") from exc
    if not isinstance(raw, dict):
        raise SlicingError("Sentence alignment manifest must be an object")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise SlicingError("Sentence alignment schema identity does not match validated alignment")
    digest = raw.get("source_sha256")
    if not isinstance(digest, str) or digest.lower() != alignment["source_sha256"]:
        raise SlicingError("Sentence alignment source_sha256 identity does not match validated alignment")
    for key in ("provider", "model", "adapter"):
        if raw.get(key) != alignment[key]:
            raise SlicingError(f"Sentence alignment {key} identity does not match validated alignment")
    return raw


def _sentences(alignment: dict[str, Any], duration_us: int) -> list[dict[str, Any]]:
    raw = _load_sentence_manifest(alignment)
    utterances = raw.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise SlicingError("Sentence boundaries require a nonempty utterances list")
    texts = []
    flattened = []
    char_word_indices = []
    for index, row in enumerate(utterances):
        if not isinstance(row, dict):
            raise SlicingError(f"Sentence utterance {index} must be an object")
        text = row.get("text")
        words = row.get("words")
        if not isinstance(text, str) or not normalized_text(text):
            raise SlicingError(f"Sentence utterance {index} requires spoken text")
        if not isinstance(words, list) or not words:
            raise SlicingError(f"Sentence utterance {index} requires word evidence")
        utterance_words = []
        for word_index, word in enumerate(words):
            if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                raise SlicingError(f"Sentence utterance {index} word {word_index} is invalid")
            word_text = word["text"]
            spoken = normalized_text(word_text)
            if not spoken:
                raise SlicingError(f"Sentence utterance {index} word {word_index} has no speech")
            start = parse_time_us(word.get("start"), field="Sentence word start")
            end = parse_time_us(word.get("end"), field="Sentence word end")
            if not 0 <= start < end <= duration_us:
                raise SlicingError("Sentence word range is outside the source duration")
            global_index = len(flattened)
            flattened.append({"text": word_text, "start_us": start, "end_us": end})
            utterance_words.append(word_text)
            char_word_indices.extend([global_index] * len(spoken))
        if normalized_text(text) != normalized_text("".join(utterance_words)):
            raise SlicingError(f"Sentence utterance {index} text does not match its words")
        texts.append(text)
    if flattened != alignment["words"]:
        raise SlicingError(
            "Sentence utterance words do not exactly match validated alignment words"
        )

    # Each spoken character maps to one validated global token. Punctuation has no timing.
    text = "".join(texts)
    sentences = []
    spoken_count = 0
    sentence_word_start = 0
    sentence_text_start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if _spoken(char):
            spoken_count += 1
            index += 1
            continue
        if not _terminal(text, index):
            index += 1
            continue
        if not spoken_count or sentence_word_start >= len(flattened):
            raise SlicingError("Sentence terminal punctuation has no preceding speech")
        word_end_index = char_word_indices[spoken_count - 1]
        if (
            spoken_count < len(char_word_indices)
            and char_word_indices[spoken_count] == word_end_index
        ):
            raise SlicingError("Sentence terminal punctuation falls inside a word token")
        if word_end_index < sentence_word_start:
            raise SlicingError("Sentence terminal punctuation has no new word evidence")
        text_end = index + 1
        while text_end < len(text) and (text[text_end] in _CLOSERS or _terminal(text, text_end)):
            text_end += 1
        sentences.append(
            {
                "sentence_text": text[sentence_text_start:text_end],
                "sentence_start_us": flattened[sentence_word_start]["start_us"],
                "sentence_end_us": flattened[word_end_index]["end_us"],
                "word_indices": list(range(sentence_word_start, word_end_index + 1)),
            }
        )
        sentence_word_start = word_end_index + 1
        sentence_text_start = text_end
        index = text_end
    if sentence_word_start != len(flattened):
        raise SlicingError("Sentence evidence has an unknown or unclosed final sentence ending")
    # Preserve trailing whitespace/punctuation verbatim in the final raw sentence text.
    if sentences:
        sentences[-1]["sentence_text"] += text[sentence_text_start:]
    return sentences


def resolve_sentence_range(
    alignment: dict[str, Any] | None,
    *,
    start_us: int,
    end_us: int,
    duration_us: int,
    preserve_start: bool = False,
) -> dict[str, Any]:
    """Expand interior sentence edges, preserving verified silence and explicit cue starts."""
    if (
        type(start_us) is not int
        or type(end_us) is not int
        or type(duration_us) is not int
        or not 0 <= start_us < end_us <= duration_us
    ):
        raise SlicingError("Sentence range must use ordered integer microseconds within duration")
    result = {"start_us": start_us, "end_us": end_us, "evidence": []}
    sides = []
    if start_us != 0 and not preserve_start:
        sides.append(("start", start_us))
    if end_us != duration_us:
        sides.append(("end", end_us))
    if not sides:
        return result
    if alignment is None:
        raise SlicingError("Sentence boundaries require source-bound ASR alignment evidence")
    sentences = _sentences(alignment, duration_us)
    for side, requested in sides:
        for sentence in sentences:
            if not sentence["sentence_start_us"] < requested < sentence["sentence_end_us"]:
                continue
            resolved = sentence[f"sentence_{side}_us"]
            result[f"{side}_us"] = resolved
            result["evidence"].append(
                {
                    "side": side,
                    "requested_us": requested,
                    "resolved_us": resolved,
                    **sentence,
                    "source_sha256": alignment["source_sha256"],
                    "alignment_sha256": alignment["sha256"],
                    "provider": alignment["provider"],
                    "model": alignment["model"],
                    "adapter": alignment["adapter"],
                    "strategy": "sentence_terminal_whole_token",
                }
            )
            break
    return result
