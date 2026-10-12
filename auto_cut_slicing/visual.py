"""Source-bound decoded-frame change proposals, without semantic picture assertions."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .alignment import parse_time_us
from .errors import SlicingError
from .io import file_sha256
from .media import _run, describe_source, find_tool

_WIDTH, _HEIGHT = 96, 54
_CHANGE_THRESHOLD = 3.0


def _frame_times(path: Path) -> list[int]:
    result = _run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_format", "-show_entries", "frame=best_effort_timestamp_time:format=start_time",
         "-of", "json", str(path)],
        purpose="Decoded frame timestamp inspection",
    )
    try:
        data = json.loads(result.stdout)
        origin_us = round(float(data.get("format", {}).get("start_time", 0)) * 1_000_000)
        times = [
            round(float(frame["best_effort_timestamp_time"]) * 1_000_000) - origin_us
            for frame in data["frames"]
        ]
        if not times or any(right <= left for left, right in zip(times, times[1:])):
            raise ValueError("non-increasing frame timestamps")
        return times
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SlicingError("Source has no reliable ordered decoded frame timestamps") from exc


def _read_frame(stream: BinaryIO) -> bytes:
    length = _WIDTH * _HEIGHT
    chunks = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if chunks:
                raise SlicingError("Visual inspection returned an incomplete decoded frame")
            return b""
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _scan(
    path: Path, selected_us: int, selected_index: int, times: list[int], limit_us: int
) -> dict[str, Any]:
    args = [
        find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
        "-map", "0:v:0", "-an", "-vf",
        f"select=gte(n\\,{selected_index}),scale={_WIDTH}:{_HEIGHT}:flags=area,format=gray",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-",
    ]
    with tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=diagnostics
            )
        except OSError as exc:
            raise SlicingError(f"Visual inspection could not start: {exc}") from exc
        try:
            previous = _read_frame(process.stdout)
            if not previous:
                raise SlicingError("Visual inspection could not decode the selected source frame")
            evidence: dict[str, Any] = {
                "selected_frame_time": selected_us / 1_000_000,
                "selected_frame_proxy_sha256": hashlib.sha256(previous).hexdigest(),
                "proxy_width": _WIDTH, "proxy_height": _HEIGHT,
                "pixel_format": "gray", "threshold_mean_absolute_difference": _CHANGE_THRESHOLD,
                "frames_scanned": 1, "change_detected": False,
            }
            previous_us = selected_us
            for timestamp_us in times[1:]:
                if timestamp_us >= limit_us:
                    break
                frame = _read_frame(process.stdout)
                if not frame:
                    process.wait()
                    diagnostics.seek(0)
                    detail = diagnostics.read().decode("utf-8", errors="replace")[-2000:]
                    raise SlicingError(f"Visual inspection ended before expected source frames: {detail}")
                evidence["frames_scanned"] += 1
                difference = sum(abs(a - b) for a, b in zip(previous, frame)) / len(frame)
                if difference >= _CHANGE_THRESHOLD:
                    evidence.update(
                        change_detected=True, candidate_time=timestamp_us / 1_000_000,
                        previous_frame_time=previous_us / 1_000_000,
                        mean_absolute_difference=difference,
                    )
                    break
                previous, previous_us = frame, timestamp_us
            return evidence
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def analyze_freeze(
    source_path: str | Path, frame_time: Any, *, end: Any = None
) -> dict[str, Any]:
    """Propose a next pixel-change boundary or preserve an explicitly supplied boundary.

    The selected time must identify an actual decoded source-frame timestamp. Boundary
    proposals are based on adjacent grayscale proxies and need human semantic review.
    """
    selected_us = parse_time_us(frame_time, field="freeze frame_time")
    source = describe_source(source_path)
    if selected_us >= source["duration_us"]:
        raise SlicingError("Freeze frame_time must be before the source duration")
    end_us = None if end is None else parse_time_us(end, field="freeze end")
    if end_us is not None and not selected_us < end_us <= source["duration_us"]:
        raise SlicingError("Freeze range requires frame_time < end <= source duration")
    path = Path(source["path"])
    times = _frame_times(path)
    if selected_us not in times:
        raise SlicingError(
            "frame_time must be an exact decoded frame timestamp; inspect source frame times "
            "instead of silently seeking to a different frame"
        )
    selected_index = times.index(selected_us)
    evidence = _scan(
        path, selected_us, selected_index, times[selected_index:], end_us or source["duration_us"]
    )
    if end_us is not None:
        boundary = end_us / 1_000_000
        evidence["end_selection"] = "explicit"
    elif evidence["change_detected"]:
        boundary = evidence["candidate_time"]
        evidence["end_selection"] = "pixel_change_candidate"
    else:
        boundary = source["duration_us"] / 1_000_000
        evidence["end_selection"] = "source_end"
    if file_sha256(path) != source["sha256"]:
        raise SlicingError("Source bytes changed during visual inspection")
    return {
        "source_sha256": source["sha256"], "frame_time": selected_us / 1_000_000,
        "end": boundary, "method": "decoded_frame_pixel_difference",
        "boundary_evidence": evidence, "semantic_verified": False,
        "limitations": [
            "Pixel differences propose visual-change boundaries and do not identify NaOH or other content.",
            "Grayscale frame proxies can miss small, gradual or equal-luminance visual changes.",
            "A semantic animation boundary and the selected picture require human review.",
        ],
    }
