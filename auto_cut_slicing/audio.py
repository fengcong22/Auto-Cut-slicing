"""Measure the decay and pause between two ASR words from the source audio.

The measurement is energy based: it finds where the previous word's sound falls below a
threshold derived from that word's level and the pause's noise floor, and where sound rises
again into the next word. Low energy is not proof of silence, and breaths or background
sound can move either edge; the result is evidence for a reviewed choice, not a listening test.
"""

import array
import math
import subprocess
from pathlib import Path
from typing import Any

from .errors import SlicingError

SAMPLE_RATE = 16_000
HOP_US = 10_000
QUIET_RUN = 5          # 50 ms below threshold ends the decay
GUARD_US = 40_000      # one 25 fps frame kept clear of the next word's onset
CONTEXT_US = 300_000   # audio decoded after the next word's ASR start


def _decode(path: Path, start_us: int, end_us: int) -> array.array:
    from .media import find_tool

    raw = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{start_us / 1_000_000:.6f}", "-t", f"{(end_us - start_us) / 1_000_000:.6f}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return array.array("h", raw[: len(raw) // 2 * 2])


def envelope(path: str | Path, start_us: int, end_us: int) -> list[tuple[int, float]]:
    """10 ms RMS levels in dBFS, as (frame start µs, level); digital silence is -120 dBFS."""
    if not 0 <= start_us < end_us:
        raise SlicingError("Audio envelope requires 0 <= start < end")
    samples = _decode(Path(path), start_us, end_us)
    hop = SAMPLE_RATE * HOP_US // 1_000_000
    rows = []
    for index in range(len(samples) // hop):
        chunk = samples[index * hop:(index + 1) * hop]
        rms = math.sqrt(sum(value * value for value in chunk) / hop)
        rows.append((start_us + index * HOP_US, 20 * math.log10(rms / 32768) if rms > 0 else -120.0))
    if not rows:
        raise SlicingError("No audio samples could be decoded for the requested range")
    return rows


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))]


def word_gap_index(words: list[dict[str, Any]], at_us: int) -> int:
    """Index of the last word that ends at or before at_us (the gap after it contains at_us)."""
    candidates = [i for i, word in enumerate(words) if word["end_us"] <= at_us]
    if not candidates:
        raise SlicingError("No ASR word ends before the requested time")
    return candidates[-1]


def analyze_gap(path: str | Path, words: list[dict[str, Any]], index: int, duration_us: int) -> dict[str, Any]:
    """Measure decay end, next onset and safe cut windows in the gap after words[index]."""
    if not 0 <= index < len(words):
        raise SlicingError("Word index is outside the alignment")
    previous = words[index]
    following = words[index + 1] if index + 1 < len(words) else None
    next_start = following["start_us"] if following else duration_us
    start = previous["start_us"]
    end = min(duration_us, next_start + CONTEXT_US)
    levels = envelope(path, start, end)
    word_levels = [db for t, db in levels if previous["start_us"] <= t < previous["end_us"]] or [levels[0][1]]
    gap_levels = [db for t, db in levels if previous["end_us"] <= t < next_start]
    speech_ref = _percentile(word_levels, 0.9)
    floor = _percentile(gap_levels, 0.1) if len(gap_levels) >= 3 else min(db for _, db in levels)
    threshold = min(max(floor + 6.0, speech_ref - 30.0), speech_ref - 10.0)

    times = [t for t, _ in levels]
    quiet = [db <= threshold for _, db in levels]
    decay_end = None
    first = next((i for i, t in enumerate(times) if t >= previous["end_us"]), len(times))
    for i in range(first, len(times) - QUIET_RUN + 1):
        if times[i] >= next_start:
            break
        if all(quiet[i:i + QUIET_RUN]):
            decay_end = times[i]
            break
    onset = next_start
    if decay_end is not None:
        limit = next((i for i, t in enumerate(times) if t >= next_start), len(times))
        last_quiet = max((i for i in range(first, limit) if quiet[i]), default=None)
        if last_quiet is not None:
            onset = min(next_start, times[last_quiet] + HOP_US)
        onset = max(onset, decay_end)

    def window(lower, upper):
        return {"lower_us": lower, "upper_us": upper} if lower is not None and lower <= upper else None

    out_window = window(decay_end, onset - GUARD_US) if decay_end is not None else None
    in_window = window(decay_end + GUARD_US, onset) if decay_end is not None else None
    return {
        "previous_word": {"index": index, "text": previous["text"], "start_us": previous["start_us"],
                          "end_us": previous["end_us"]},
        "next_word": None if following is None else {
            "index": index + 1, "text": following["text"], "start_us": following["start_us"],
            "end_us": following["end_us"]},
        "speech_ref_dbfs": round(speech_ref, 1), "floor_dbfs": round(floor, 1),
        "threshold_dbfs": round(threshold, 1),
        "decay_end": decay_end, "low_energy_start": decay_end,
        "low_energy_end": onset if decay_end is not None else None,
        "next_word_start": next_start,
        "tail_after_word_us": None if decay_end is None else decay_end - previous["end_us"],
        "quiet_us": None if decay_end is None else onset - decay_end,
        "out_window": out_window, "in_window": in_window,
        "guard_us": GUARD_US, "hop_us": HOP_US,
        "envelope": [[t, round(db, 1)] for t, db in levels],
    }
