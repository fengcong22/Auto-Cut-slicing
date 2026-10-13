"""Explicit optional MP4 export; editable drafts remain the default delivery."""

import math
import os
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import SlicingError
from .io import file_sha256
from .media import (
    _input_options,
    _positive_number,
    _probe,
    _run,
    describe_source,
    find_tool,
    verify_sources,
)


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise SlicingError(
            f"Invalid render plan {field}: expected a {'positive' if positive else 'nonnegative'} integer."
        )
    return value


def _validate_output(plan: dict[str, Any], output_id: str) -> dict[str, Any]:
    outputs = plan.get("outputs", [])
    if not isinstance(outputs, list):
        raise SlicingError("Invalid render outputs inventory.")
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
        if not isinstance(source_id, str) or source_id not in plan["sources"]:
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
    _validate_effects(plan, output)
    return output


def _validate_effects(plan: dict[str, Any], output: dict[str, Any]) -> None:
    effects = output.get("effects", [])
    if not isinstance(effects, list):
        raise SlicingError("Render effects must be a list.")
    for effect in effects:
        if not isinstance(effect, dict) or effect.get("kind") not in ("mute", "freeze_frame"):
            raise SlicingError("Unknown or invalid render effect kind.")
        for field in ("item_id", "clip_id", "source_id"):
            if not isinstance(effect.get(field), str) or not effect[field].strip():
                raise SlicingError(f"Invalid render effect {field}.")
        if not isinstance(effect.get("source_text"), str):
            raise SlicingError("Invalid render effect source_text.")
        source_id = effect["source_id"]
        if source_id not in plan["sources"]:
            raise SlicingError(f"Unknown render effect source: {source_id}")
        source_start = _integer(effect.get("source_start_us"), "effect source_start_us")
        source_end = _integer(effect.get("source_end_us"), "effect source_end_us")
        duration = _integer(effect.get("duration_us"), "effect duration_us", positive=True)
        target_start = _integer(effect.get("target_start_us"), "effect target_start_us")
        target_end = target_start + duration
        if source_end != source_start + duration:
            raise SlicingError("Render effect source range does not match its duration.")
        if source_end > plan["sources"][source_id]["duration_us"]:
            raise SlicingError("Render effect exceeds source duration.")
        if target_end > output["duration_us"]:
            raise SlicingError("Render effect exceeds output duration.")
        if effect["kind"] == "freeze_frame":
            frame = _integer(effect.get("frame_time_us"), "effect frame_time_us")
            if frame >= plan["sources"][source_id]["duration_us"]:
                raise SlicingError("Render freeze frame lies outside the source duration.")
        source_cursor, target_cursor = source_start, target_start
        for segment in output["segments"]:
            segment_start = segment["target_start_us"]
            start = max(target_start, segment_start)
            end = min(target_end, segment_start + segment["duration_us"])
            if end <= start:
                continue
            if (
                start != target_cursor
                or segment["source_id"] != source_id
                or segment.get("clip_id") != effect["clip_id"]
                or segment["source_start_us"] + start - segment_start != source_cursor
            ):
                raise SlicingError("Render effect does not match its retained source and clip range.")
            source_cursor += end - start
            target_cursor = end
        if source_cursor != source_end or target_cursor != target_end:
            raise SlicingError("Render effect is not fully covered by retained segments.")
    for kind in ("mute", "freeze_frame"):
        previous_end = 0
        for effect in sorted(
            (row for row in effects if row["kind"] == kind), key=lambda row: row["target_start_us"]
        ):
            if effect["target_start_us"] < previous_end:
                raise SlicingError(f"Render {kind} effects overlap on the output timeline.")
            previous_end = effect["target_start_us"] + effect["duration_us"]


def _render_pieces(output: dict[str, Any]) -> list[dict[str, Any]]:
    pieces = []
    for segment in output["segments"]:
        segment_start = segment["target_start_us"]
        segment_end = segment_start + segment["duration_us"]
        effects = [
            effect for effect in output.get("effects", [])
            if effect["target_start_us"] < segment_end
            and effect["target_start_us"] + effect["duration_us"] > segment_start
        ]
        boundaries = {segment_start, segment_end}
        for effect in effects:
            boundaries.add(max(segment_start, effect["target_start_us"]))
            boundaries.add(min(segment_end, effect["target_start_us"] + effect["duration_us"]))
        ordered = sorted(boundaries)
        for start, end in zip(ordered, ordered[1:]):
            piece = {
                "source_id": segment["source_id"],
                "source_start_us": segment["source_start_us"] + start - segment_start,
                "retained_source_start_us": segment["source_start_us"],
                "retained_target_start_us": segment_start,
                "target_start_us": start,
                "duration_us": end - start,
                "muted": False,
            }
            for effect in effects:
                if effect["target_start_us"] <= start < effect["target_start_us"] + effect["duration_us"]:
                    if effect["kind"] == "mute":
                        piece["muted"] = True
                    else:
                        piece["frame_time_us"] = effect["frame_time_us"]
            pieces.append(piece)
    return pieces


def _profile(
    first: dict[str, Any], width: Any, height: Any, fps: Any,
) -> tuple[int, int, Fraction]:
    dimensions = []
    for field, value in (("width", width), ("height", height)):
        if value is None:
            value = first[field] + first[field] % 2
        value = _integer(value, f"profile {field}", positive=True)
        if value % 2:
            raise SlicingError(f"Render profile {field} must be even for MP4 export.")
        dimensions.append(value)
    if fps is None:
        fps = first["fps"]
    if (
        isinstance(fps, bool)
        or not isinstance(fps, (int, float, Fraction))
        or _positive_number(fps) is None
    ):
        raise SlicingError("Render profile fps must be a finite positive number.")
    rate = Fraction(str(fps)).limit_denominator(100_000)
    if rate <= 0:
        raise SlicingError("Render profile fps is too small to represent.")
    return dimensions[0], dimensions[1], rate


def _concat_list(path: Path, files: list[Path]) -> None:
    # Relative generated names avoid user-provided path escaping in the concat demuxer.
    path.write_text("".join(f"file '{file.name}'\n" for file in files), encoding="utf-8")


def render_output(
    plan: dict[str, Any], output_id: str, destination: str | Path,
    *, width: int | None = None, height: int | None = None, fps: float | None = None,
    input_demuxer: str | None = None,
) -> dict[str, Any]:
    """Render one selected output and publish only after decoding and duration checks."""
    target = Path(destination).expanduser().absolute()
    if os.path.lexists(target):
        raise SlicingError(f"Refusing to overwrite existing output: {target}")
    if target.suffix.lower() != ".mp4":
        raise SlicingError("The optional video export destination must end in .mp4.")
    if input_demuxer is None:
        verify_sources(plan)
    else:
        verify_sources(plan, input_demuxer=input_demuxer)
    output = _validate_output(plan, output_id)
    ffmpeg = find_tool("ffmpeg")
    first = plan["sources"][output["segments"][0]["source_id"]]
    width, height, rate = _profile(first, width, height, fps)
    if round(Fraction(output["duration_us"], 1_000_000) * rate) <= 0:
        raise SlicingError("The output is too short to render at the output frame rate.")
    fps = rate
    fps_text = f"{fps.numerator}/{fps.denominator}"
    sample_rate = 48_000
    target.parent.mkdir(parents=True, exist_ok=True)
    # Same-volume hardlink publication atomically refuses a concurrent destination creation.
    with tempfile.TemporaryDirectory(prefix=".slicing-render-", dir=target.parent) as work:
        directory = Path(work)
        video_files, audio_files = [], []
        cursor_us = previous_frames = previous_samples = 0
        for index, segment in enumerate(_render_pieces(output)):
            source = plan["sources"][segment["source_id"]]
            cursor_us += segment["duration_us"]
            frames_end = round(Fraction(cursor_us, 1_000_000) * fps)
            samples_end = round(Fraction(cursor_us * sample_rate, 1_000_000))
            frames, samples = frames_end - previous_frames, samples_end - previous_samples
            frames_start = previous_frames
            previous_frames, previous_samples = frames_end, samples_end
            # Effects may split less than one frame/sample; only cumulative rounding controls
            # which stream receives a unit at that boundary, so splits never add duration.
            if frames <= 0 and samples <= 0:
                continue
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
                *_input_options(input_demuxer),
                "-i",
                source["path"],
            ]
            video_input = "0:v:0"
            input_count = 1
            video_start_us = segment["source_start_us"]
            video_offset_frames = 0
            if "frame_time_us" in segment:
                video_start_us = segment["frame_time_us"]
            elif segment["target_start_us"] != segment["retained_target_start_us"]:
                # Preserve the original fps-conversion phase even for differing or variable
                # source frame rates: normalize from the retained origin, then skip frames.
                # Audio independently seeks the exact effect boundary without that prefix.
                video_start_us = segment["retained_source_start_us"]
                video_offset_frames = frames_start - round(
                    Fraction(segment["retained_target_start_us"], 1_000_000) * fps
                )
            if frames > 0 and (
                "frame_time_us" in segment or video_start_us != segment["source_start_us"]
            ):
                if "frame_time_us" in segment:
                    # Preserve decoded frames before the seek timestamp so the selected
                    # picture is the frame displaying that instant, including at source end.
                    arguments += ["-noaccurate_seek"]
                arguments += [
                    "-ss", f"{float(video_start_us / 1_000_000):.6f}",
                    *_input_options(input_demuxer),
                    "-i", source["path"],
                ]
                video_input = "1:v:0"
                input_count += 1
            if source["has_audio"]:
                audio_input = "0:a:0"
            else:
                arguments += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
                audio_input = f"{input_count}:a:0"
            fill_seconds = segment["duration_us"] / 1_000_000 + float((video_offset_frames + 1) / fps)
            filters = []
            if frames > 0:
                selected_frame = (
                    "trim=end_pts=1,reverse,trim=end_frame=1,"
                    if "frame_time_us" in segment else ""
                )
                # Extend the one selected source frame before fps conversion; audio always
                # comes from the independently sought original retained source interval.
                frozen_fill = (
                    f"tpad=stop_mode=clone:stop_duration={fill_seconds:.6f},"
                    if selected_frame else ""
                )
                filters.append(
                    f"[{video_input}]{selected_frame}setpts=PTS-STARTPTS,"
                    f"{frozen_fill}fps={fps_text},"
                    f"tpad=stop_mode=clone:stop_duration={fill_seconds:.6f},"
                    f"trim=start_frame={video_offset_frames}:end_frame={video_offset_frames + frames},"
                    f"setpts=N/({fps_text}*TB),"
                    # dar includes non-square source pixels. Fit that display ratio into
                    # even square-pixel dimensions before padding the delivery canvas.
                    f"scale=w='max(2,trunc(min({width},{height}*dar)/2)*2)':"
                    f"h='max(2,trunc(min({height},{width}/dar)/2)*2)',setsar=1,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[v]"
                )
            if samples > 0:
                mute = "volume=0," if segment["muted"] else ""
                filters.append(
                    f"[{audio_input}]aresample={sample_rate}:async=1:first_pts=0,"
                    f"aformat=sample_fmts=s16:channel_layouts=stereo,{mute}apad=whole_len={samples},"
                    f"atrim=end_sample={samples},asetpts=N/SR/TB[a]"
                )
            arguments += ["-filter_complex", ";".join(filters)]
            if frames > 0:
                arguments += [
                    "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-video_track_timescale",
                    str(fps.numerator), str(video_path),
                ]
                video_files.append(video_path)
            if samples > 0:
                arguments += ["-map", "[a]", "-vn", "-c:a", "pcm_s16le", str(audio_path)]
                audio_files.append(audio_path)
            _run(arguments, purpose=f"Render piece {index + 1}")
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
        if input_demuxer is None:
            verify_sources(plan)
        else:
            verify_sources(plan, input_demuxer=input_demuxer)
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
