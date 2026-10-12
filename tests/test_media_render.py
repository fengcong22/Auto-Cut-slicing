import array
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.media import describe_source, extract_source_audio, verify_sources
from auto_cut_slicing.render import render_output


@pytest.fixture
def videos(tmp_path: Path) -> dict:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    first, second = tmp_path / "有声音 source.mp4", tmp_path / "silent source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(first),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=100x100:rate=30:duration=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(second),
        ],
        check=True,
        capture_output=True,
    )
    return {"first": describe_source(first), "second": describe_source(second)}


def make_plan(sources: dict, segments: list[tuple[str, int, int]]) -> dict:
    cursor = 0
    rows = []
    for index, (source_id, start, duration) in enumerate(segments):
        rows.append(
            {
                "id": f"segment-{index}",
                "clip_id": f"clip-{index}",
                "source_id": source_id,
                "source_start_us": start,
                "duration_us": duration,
                "target_start_us": cursor,
            }
        )
        cursor += duration
    return {
        "schema_version": 1,
        "workflow_mode": "slicing",
        "name": "real-media-test",
        "job_sha256": "fixture",
        "sources": sources,
        "outputs": [
            {
                "id": "joined",
                "name": "Joined",
                "duration_us": cursor,
                "segments": rows,
                "markers": [],
                "edits": [],
            }
        ],
    }


def rgb_frame(path: str | Path, seconds: float) -> bytes:
    return subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seconds),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-an",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout


def test_probe_and_full_source_audio(videos: dict, tmp_path: Path) -> None:
    first = videos["first"]
    assert first["duration_us"] == 3_000_000
    assert (first["width"], first["height"], first["fps"]) == (160, 90, 25)
    assert first["has_audio"] is True
    assert videos["second"]["has_audio"] is False
    plan = make_plan(videos, [("first", 0, 1_000_000)])
    verify_sources(plan)
    output = extract_source_audio(first["path"], tmp_path / "source.wav")
    with wave.open(str(output), "rb") as audio:
        assert audio.getsampwidth() == 2
        assert abs(audio.getnframes() / audio.getframerate() - 3.0) < 0.05
    with pytest.raises(SlicingError, match="overwrite"):
        extract_source_audio(first["path"], output)
    with pytest.raises(SlicingError, match="no audio"):
        extract_source_audio(videos["second"]["path"], tmp_path / "missing.wav")


def test_real_delete_and_concat_normalize_silent_source(videos: dict, tmp_path: Path) -> None:
    # The removed middle second is absent; the last segment is another resolution/fps with no audio.
    plan = make_plan(
        videos,
        [("first", 0, 1_000_000), ("first", 2_000_000, 1_000_000), ("second", 500_000, 1_000_000)],
    )
    destination = tmp_path / "result.mp4"
    receipt = render_output(plan, "joined", destination)
    assert receipt["full_decode_passed"] is True
    assert receipt["spoken_final_acceptance"] is False
    assert receipt["segment_count"] == 3
    assert abs(receipt["duration_us"] - 3_000_000) <= receipt["duration_tolerance_us"]
    assert (receipt["width"], receipt["height"], receipt["fps"]) == (160, 90, 25)
    assert describe_source(destination)["has_audio"] is True
    # A frame after the deletion must come from the selected final source second.
    selected_frame = rgb_frame(videos["first"]["path"], 2.4)
    actual_frame = rgb_frame(destination, 1.4)
    assert len(selected_frame) == len(actual_frame) == 160 * 90 * 3
    assert sum(abs(a - b) for a, b in zip(selected_frame, actual_frame)) / len(actual_frame) < 4
    # Check the synthesized final second is silent instead of reusing the preceding audio.
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(destination),
            "-ss",
            "2.2",
            "-t",
            "0.5",
            "-vn",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    samples = array.array("h", decoded)
    assert samples and max(abs(sample) for sample in samples) <= 2
    assert not list(tmp_path.glob(".slicing-render-*"))


def test_render_cumulative_frame_rounding(videos: dict, tmp_path: Path) -> None:
    # 70-ms clips at 25fps individually round to 80ms; only cumulative rounding avoids drift.
    plan = make_plan(videos, [("first", index * 100_000, 70_000) for index in range(12)])
    receipt = render_output(plan, "joined", tmp_path / "short-clips.mp4")
    assert abs(receipt["duration_us"] - 840_000) <= receipt["duration_tolerance_us"]


def test_rotation_is_normalized_in_probe_and_export(videos: dict, tmp_path: Path) -> None:
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-display_rotation",
            "90",
            "-i",
            videos["first"]["path"],
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
        capture_output=True,
    )
    source = describe_source(rotated)
    assert (source["width"], source["height"]) == (90, 160)
    receipt = render_output(
        make_plan({"rotated": source}, [("rotated", 0, 1_000_000)]),
        "joined",
        tmp_path / "normalized.mp4",
    )
    assert (receipt["width"], receipt["height"]) == (90, 160)


def test_delayed_audio_preserves_source_timeline_in_wav_and_export(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    source = tmp_path / "delayed.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=160x90:rate=25:duration=3",
            "-itsoffset",
            "1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    descriptor = describe_source(source)
    extracted = extract_source_audio(source, tmp_path / "delayed.wav")
    with wave.open(str(extracted), "rb") as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
        rate = audio.getframerate()
        assert audio.getnchannels() == 1
        assert abs(len(samples) / rate - descriptor["duration_us"] / 1_000_000) <= 1 / rate
    assert max(abs(sample) for sample in samples[: int(0.9 * rate)]) <= 2
    onset = next(index for index, sample in enumerate(samples) if abs(sample) > 1000) / rate
    assert abs(onset - 1.0) < 0.03
    destination = tmp_path / "delayed-render.mp4"
    render_output(
        make_plan({"delayed": descriptor}, [("delayed", 500_000, 2_000_000)]), "joined", destination
    )
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(destination),
            "-vn",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    rendered_samples = array.array("h", decoded)
    assert max(abs(sample) for sample in rendered_samples[: int(0.4 * rate)]) <= 2
    rendered_onset = (
        next(index for index, sample in enumerate(rendered_samples) if abs(sample) > 1000) / rate
    )
    assert abs(rendered_onset - 0.5) < 0.03


def test_delayed_video_is_rejected_before_any_output(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    source = tmp_path / "delayed-video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-itsoffset",
            "1",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=160x90:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:sample_rate=48000:duration=3",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(SlicingError, match="video starts later than the container timeline"):
        describe_source(source)


def test_equal_nonzero_container_and_video_origin_is_supported(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    source = tmp_path / "nonzero-origin.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=160x90:rate=25:duration=2",
            "-an",
            "-c:v",
            "mpeg2video",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    metadata = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert float(metadata["format"]["start_time"]) > 1
    assert metadata["streams"][0]["start_time"] == metadata["format"]["start_time"]
    descriptor = describe_source(source)
    assert descriptor["duration_us"] == 2_000_000


def test_render_refuses_overwrite_and_changed_source(videos: dict, tmp_path: Path) -> None:
    plan = make_plan(videos, [("first", 0, 1_000_000)])
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"do not replace")
    with pytest.raises(SlicingError, match="overwrite"):
        render_output(plan, "joined", destination)
    assert destination.read_bytes() == b"do not replace"
    source = Path(videos["first"]["path"])
    with source.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(SlicingError, match="bytes changed"):
        render_output(plan, "joined", tmp_path / "changed.mp4")
    assert not (tmp_path / "changed.mp4").exists()


def test_render_rejects_invalid_ranges(videos: dict, tmp_path: Path) -> None:
    plan = make_plan(videos, [("first", 2_000_000, 2_000_000)])
    with pytest.raises(SlicingError, match="exceeds source duration"):
        render_output(plan, "joined", tmp_path / "invalid.mp4")


def test_probe_rejects_audio_only_and_bogus_input(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-t",
            "0.1",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(SlicingError, match="no readable video"):
        describe_source(audio)
    bogus = tmp_path / "not-video.mp4"
    bogus.write_text(json.dumps({"pretend": "video"}), encoding="utf-8")
    with pytest.raises(SlicingError, match="Media probe failed"):
        describe_source(bogus)
    with pytest.raises(SlicingError, match="does not exist"):
        describe_source(tmp_path / "missing.mp4")
