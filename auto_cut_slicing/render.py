"""Explicit optional MP4 export; editable drafts remain the default delivery."""

import math
import os
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import SlicingError
from .io import file_sha256
from .media import _positive_number, _probe, _run, describe_source, find_tool, verify_sources


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise SlicingError(
            f"Invalid render plan {field}: expected a {'positive' if positive else 'nonnegative'} integer."
        )
    return value


def _validate_output(plan: dict[str, Any], output_id: str) -> dict[str, Any]:
    outputs = plan.get("outputs", [])
    matches = [row for row in outputs if isinstance(row, dict) and row.get("id") == output_id]
    if len(matches) != 1:
        raise SlicingError(f"Expected exactly one output with id {output_id!r}.")
    output = matches[0]
    segments = output.get("segments")
    if not isinstance(segments, list) or not segments:
        raise SlicingError("Cannot render an empty segment plan.")
    cursor = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise SlicingError("Invalid render segment.")
        source_id = segment.get("source_id")
        if source_id not in plan["sources"]:
            raise SlicingError(f"Unknown render source: {source_id}")
        start = _integer(segment.get("source_start_us"), "source_start_us")
        duration = _integer(segment.get("duration_us"), "segment duration_us", positive=True)
        if _integer(segment.get("target_start_us"), "target_start_us") != cursor:
            raise SlicingError("Render segments must form one contiguous ordered output timeline.")
        if start + duration > plan["sources"][source_id]["duration_us"]:
            raise SlicingError(f"Render segment exceeds source duration: {source_id}")
        cursor += duration
    if cursor != _integer(output.get("duration_us"), "output duration_us", positive=True):
        raise SlicingError("Output duration does not match its retained segments.")
    return output


def _concat_list(path: Path, files: list[Path]) -> None:
    # Relative generated names avoid user-provided path escaping in the concat demuxer.
    path.write_text("".join(f"file '{file.name}'\n" for file in files), encoding="utf-8")


def render_output(plan: dict[str, Any], output_id: str, destination: str | Path) -> dict[str, Any]:
    """Render one selected output and publish only after decoding and duration checks."""
    target = Path(destination).expanduser().absolute()
    if os.path.lexists(target):
        raise SlicingError(f"Refusing to overwrite existing output: {target}")
    if target.suffix.lower() != ".mp4":
        raise SlicingError("The optional video export destination must end in .mp4.")
    verify_sources(plan)
    output = _validate_output(plan, output_id)
    ffmpeg = find_tool("ffmpeg")
    first = plan["sources"][output["segments"][0]["source_id"]]
    width = first["width"] + first["width"] % 2
    height = first["height"] + first["height"] % 2
    fps = Fraction(str(first["fps"])).limit_denominator(100_000)
    fps_text = f"{fps.numerator}/{fps.denominator}"
    sample_rate = 48_000
    target.parent.mkdir(parents=True, exist_ok=True)
    # Same-volume hardlink publication atomically refuses a concurrent destination creation.
    with tempfile.TemporaryDirectory(prefix=".slicing-render-", dir=target.parent) as work:
        directory = Path(work)
        video_files, audio_files = [], []
        cursor_us = previous_frames = previous_samples = 0
        for index, segment in enumerate(output["segments"]):
            source = plan["sources"][segment["source_id"]]
            cursor_us += segment["duration_us"]
            frames_end = round(Fraction(cursor_us, 1_000_000) * fps)
            samples_end = round(Fraction(cursor_us * sample_rate, 1_000_000))
            frames, samples = frames_end - previous_frames, samples_end - previous_samples
            previous_frames, previous_samples = frames_end, samples_end
            if frames <= 0 or samples <= 0:
                raise SlicingError(
                    "A retained segment is too short to render at the output frame rate."
                )
            video_path = directory / f"video-{index:08d}.mp4"
            audio_path = directory / f"audio-{index:08d}.wav"
            arguments = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-ss",
                f"{segment['source_start_us'] / 1_000_000:.6f}",
                "-i",
                source["path"],
            ]
            if source["has_audio"]:
                audio_input = "0:a:0"
            else:
                arguments += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
                audio_input = "1:a:0"
            fill_seconds = segment["duration_us"] / 1_000_000 + float(1 / fps)
            filters = (
                f"[0:v:0]setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps_text},tpad=stop_mode=clone:stop_duration={fill_seconds:.6f},"
                f"trim=end_frame={frames},setpts=N/({fps_text}*TB)[v];"
                f"[{audio_input}]aresample={sample_rate}:async=1:first_pts=0,"
                f"aformat=sample_fmts=s16:channel_layouts=stereo,apad=whole_len={samples},"
                f"atrim=end_sample={samples},asetpts=N/SR/TB[a]"
            )
            arguments += [
                "-filter_complex",
                filters,
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                str(fps.numerator),
                str(video_path),
                "-map",
                "[a]",
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ]
            _run(arguments, purpose=f"Render segment {index + 1}")
            video_files.append(video_path)
            audio_files.append(audio_path)
        video_list, audio_list = directory / "video.txt", directory / "audio.txt"
        _concat_list(video_list, video_files)
        _concat_list(audio_list, audio_files)
        candidate = directory / "candidate.mp4"
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(video_list),
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(audio_list),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                f"{output['duration_us'] / 1_000_000:.6f}",
                "-movflags",
                "+faststart",
                str(candidate),
            ],
            purpose="Concatenate rendered segments",
        )
        actual = describe_source(candidate)
        tolerance_us = max(50_000, math.ceil(2_000_000 / float(fps)))
        if abs(actual["duration_us"] - output["duration_us"]) > tolerance_us:
            raise SlicingError(
                f"Rendered duration failed acceptance: expected {output['duration_us']} us, "
                f"received {actual['duration_us']} us (tolerance {tolerance_us} us)."
            )
        if (actual["width"], actual["height"]) != (width, height) or not actual["has_audio"]:
            raise SlicingError("Rendered streams failed dimension/audio acceptance.")
        if not math.isclose(actual["fps"], float(fps), rel_tol=1e-9):
            raise SlicingError("Rendered video did not preserve the normalized frame rate.")
        for stream in _probe(candidate).get("streams", []):
            if stream.get("codec_type") not in ("video", "audio"):
                continue
            duration = _positive_number(stream.get("duration"))
            if (
                duration is None
                or abs(round(duration * 1_000_000) - output["duration_us"]) > tolerance_us
            ):
                raise SlicingError(f"Rendered {stream['codec_type']} duration failed acceptance.")
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-xerror",
                "-i",
                str(candidate),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            purpose="Full output decode acceptance",
        )
        # Recheck immediately before publishing so changed inputs cannot receive a success receipt.
        verify_sources(plan)
        try:
            os.link(candidate, target)
        except FileExistsError as exc:
            raise SlicingError(
                f"Refusing to overwrite concurrently created output: {target}"
            ) from exc
        except OSError as exc:
            raise SlicingError(f"Could not atomically publish export: {exc}") from exc
    return {
        "output_id": output_id,
        "path": str(target.resolve()),
        "sha256": file_sha256(target),
        "duration_us": actual["duration_us"],
        "expected_duration_us": output["duration_us"],
        "duration_tolerance_us": tolerance_us,
        "width": width,
        "height": height,
        "fps": actual["fps"],
        "has_audio": True,
        "segment_count": len(output["segments"]),
        "full_decode_passed": True,
        "spoken_final_acceptance": False,
    }
