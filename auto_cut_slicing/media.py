"""Local media inspection with source-byte identity and explicit tool discovery."""

import json
import math
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import SlicingError
from .io import file_sha256


def find_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SlicingError(
            f"Required tool {name!r} was not found on PATH. Install FFmpeg including ffprobe."
        )
    return executable


def _run(arguments: list[str], *, purpose: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise SlicingError(f"{purpose} could not start: {exc}") from exc
    if result.returncode:
        diagnostic = result.stderr.strip()[-4000:]
        raise SlicingError(f"{purpose} failed (exit {result.returncode}): {diagnostic}")
    return result


def _input_options(input_demuxer: str | None = None) -> list[str]:
    if input_demuxer is None:
        return []
    if input_demuxer != 'mov':
        raise SlicingError('Unsupported restricted input demuxer.')
    return ['-protocol_whitelist', 'file', '-f', 'mov', '-enable_drefs', '0']


def _probe(path: Path, *, input_demuxer: str | None = None) -> dict[str, Any]:
    result = _run(
        [
            find_tool("ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            *_input_options(input_demuxer),
            str(path),
        ],
        purpose="Media probe",
    )
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise SlicingError("ffprobe did not return valid media metadata.") from exc
    if not isinstance(payload, dict):
        raise SlicingError("ffprobe did not return a media metadata object.")
    return payload


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _start_time(value: Any, fallback: float = 0.0) -> float:
    if value in (None, "N/A"):
        return fallback
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SlicingError("Source timeline start metadata is invalid.") from exc
    if not math.isfinite(number):
        raise SlicingError("Source timeline start metadata must be finite.")
    return number


def describe_source(path: str | Path, *, input_demuxer: str | None = None) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SlicingError(f"Source video does not exist or is not a file: {source}")
    try:
        metadata = _probe(source) if input_demuxer is None else _probe(source, input_demuxer=input_demuxer)
        streams = metadata.get("streams", [])
        videos = [
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic", 0)
        ]
        if not videos:
            raise SlicingError(f"Source has no readable video stream: {source}")
        video = videos[0]
        origin = _start_time(metadata.get("format", {}).get("start_time"))
        video_start = _start_time(video.get("start_time"), origin)
        if round((video_start - origin) * 1_000_000) > 1_000:
            raise SlicingError(
                "Source video starts later than the container timeline; "
                "delayed-video sources are unsupported in this version."
            )
        duration = _positive_number(metadata.get("format", {}).get("duration"))
        if duration is None:
            duration = _positive_number(video.get("duration"))
        if duration is None:
            raise SlicingError(f"Source video must have a finite positive duration: {source}")
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if width <= 0 or height <= 0:
            raise SlicingError(f"Source video dimensions are invalid: {source}")
        rotation = video.get("tags", {}).get("rotate", 0)
        for side_data in video.get("side_data_list", []):
            if "rotation" in side_data:
                rotation = side_data["rotation"]
        try:
            rotation = float(rotation)
        except (ValueError, TypeError, OverflowError):
            rotation = 0
        if math.isfinite(rotation) and round(rotation) % 180 == 90:
            width, height = height, width
        fps = None
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                fps = _positive_number(Fraction(str(video.get(key, "0/0"))))
            except (ValueError, ZeroDivisionError):
                continue
            if fps is not None:
                break
        if fps is None:
            raise SlicingError(f"Source video frame rate is invalid: {source}")
        return {
            "path": str(source),
            "sha256": file_sha256(source),
            "duration_us": round(duration * 1_000_000),
            "width": width,
            "height": height,
            "fps": fps,
            "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        }
    except SlicingError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SlicingError(f"Could not inspect source video {source}: {exc}") from exc


def verify_sources(plan: dict[str, Any], *, input_demuxer: str | None = None) -> None:
    sources = plan.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise SlicingError("The edit plan has no source inventory.")
    for source_id, expected in sources.items():
        if not isinstance(expected, dict) or not expected.get("path"):
            raise SlicingError(f"Invalid source inventory entry: {source_id}")
        actual = (describe_source(expected["path"]) if input_demuxer is None else
                  describe_source(expected["path"], input_demuxer=input_demuxer))
        if actual["sha256"] != expected.get("sha256"):
            raise SlicingError(f"Source bytes changed since planning: {source_id}")
        for field in ("duration_us", "width", "height", "has_audio"):
            if actual[field] != expected.get(field):
                raise SlicingError(f"Source metadata changed since planning ({field}): {source_id}")
        expected_fps = _positive_number(expected.get("fps"))
        if expected_fps is None or not math.isclose(actual["fps"], expected_fps, rel_tol=1e-9):
            raise SlicingError(f"Source metadata changed since planning (fps): {source_id}")


def extract_source_audio(path: str | Path, output: str | Path) -> Path:
    """Extract complete source audio as PCM; callers skip sources without audio."""
    source = Path(path).expanduser().resolve()
    descriptor = describe_source(source)
    if not descriptor["has_audio"]:
        raise SlicingError(f"Source has no audio stream to extract: {source}")
    destination = Path(output).expanduser().absolute()
    if os.path.lexists(destination):
        raise SlicingError(f"Refusing to overwrite existing source audio: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".slicing-audio-", dir=destination.parent) as work:
        temporary = Path(work) / "audio.wav"
        _run(
            [
                find_tool("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                f"aresample=async=1:first_pts=0,apad,atrim=duration={descriptor['duration_us'] / 1_000_000:.6f}",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(temporary),
            ],
            purpose="Source audio extraction",
        )
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise SlicingError(
                f"Refusing to overwrite concurrently created source audio: {destination}"
            ) from exc
        except OSError as exc:
            raise SlicingError(f"Could not atomically publish source audio: {exc}") from exc
    return destination
