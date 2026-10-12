import array
import copy
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


def test_sentence_completed_range_renders_full_picture_and_audio(videos, tmp_path):
    from auto_cut_slicing.planner import compile_job

    source = videos['first']
    words = [
        {'text': '完整', 'start': 0.2, 'end': 0.6},
        {'text': '句子', 'start': 1, 'end': 1.8},
    ]
    asr = tmp_path / 'sentences.json'
    asr.write_text(json.dumps({
        'schema_version': 1, 'source_sha256': source['sha256'],
        'provider': 'synthetic-fixture', 'model': 'synthetic-fixture',
        'adapter': 'synthetic-fixture', 'words': words,
        'utterances': [{'text': '完整句子。', 'start': 0.2, 'end': 1.8, 'words': words}],
    }, ensure_ascii=False), encoding='utf-8')
    job = tmp_path / 'sentence-job.json'
    job.write_text(json.dumps({
        'schema_version': 1, 'name': 'sentence-render',
        'sources': [{'id': 's', 'path': source['path'], 'asr': str(asr)}],
        'outputs': [{'id': 'out', 'clips': [{
            'id': 'c', 'source': 's', 'start': 0.4, 'end': 1.2, 'boundary_policy': 'sentence',
        }]}],
    }), encoding='utf-8')
    plan = compile_job(job)
    assert plan['outputs'][0]['duration_us'] == 1_600_000
    destination = tmp_path / 'sentence-completed.mp4'
    receipt = render_output(plan, 'out', destination, width=160, height=90, fps=25)
    assert abs(receipt['duration_us'] - 1_600_000) <= 40_000
    assert receipt['has_audio']
    actual = describe_source(destination)
    assert actual['has_audio'] and abs(actual['duration_us'] - 1_600_000) <= 40_000
    # Compare the new opening/ending pictures against the expanded source interval.
    for target, original in [(0, 0.2), (1.56, 1.76)]:
        left, right = rgb_frame(destination, target), rgb_frame(source['path'], original)
        assert len(left) == len(right)
        assert sum(abs(a - b) for a, b in zip(left, right)) / len(left) < 5


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


def audio_samples(path: str | Path, start: float, duration: float) -> array.array:
    decoded = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-ss", str(start), "-t", str(duration), "-vn", "-f", "s16le",
            "-ac", "1", "-ar", "48000", "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return array.array("h", decoded)


def tone_frequency(samples: array.array) -> float:
    crossings = sum(left <= 0 < right for left, right in zip(samples, samples[1:]))
    return crossings / (len(samples) / 48_000)


def effect(kind: str, *, start: int, end: int, target: int, frame: int = 0) -> dict:
    row = {
        "kind": kind,
        "item_id": "effect-1",
        "clip_id": "clip-0",
        "source_id": "first",
        "source_text": "原始操作文本（保留）",
        "source_start_us": start,
        "source_end_us": end,
        "target_start_us": target,
        "duration_us": end - start,
    }
    if kind == "freeze_frame":
        row["frame_time_us"] = frame
    return row


@pytest.fixture
def changing_media(tmp_path: Path, request: pytest.FixtureRequest) -> dict:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    source = tmp_path / "animation and changing tones.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={getattr(request, 'param', 25)}:duration=3",
            "-f", "lavfi", "-i",
            "aevalsrc=0.125*sin(2*PI*if(lt(t\\,1)\\,440\\,if(lt(t\\,2)\\,880\\,1320))*t):s=48000:d=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
        capture_output=True,
    )
    return {"first": describe_source(source)}


def test_delivery_profile_preserves_rectangle_aspect_with_padding(videos: dict, tmp_path: Path) -> None:
    destination = tmp_path / "rectangle.mp4"
    receipt = render_output(
        make_plan(videos, [("first", 0, 400_000)]), "joined", destination,
        width=320, height=240, fps=20,
    )
    assert (receipt["width"], receipt["height"], receipt["fps"]) == (320, 240, 20)
    image = rgb_frame(destination, 0.2)
    assert len(image) == 320 * 240 * 3
    # A 16:9 rectangle occupies 320x180, centered in a 320x240 frame.
    assert max(image[10 * 320 * 3 : 20 * 320 * 3]) <= 2
    assert max(image[220 * 320 * 3 : 230 * 320 * 3]) <= 2
    assert max(image[40 * 320 * 3 : 50 * 320 * 3]) > 100
    assert max(image[190 * 320 * 3 : 200 * 320 * 3]) > 100


def test_delivery_profile_1920_by_1080_at_25fps(videos: dict, tmp_path: Path) -> None:
    receipt = render_output(
        make_plan(videos, [("second", 0, 200_000)]), "joined", tmp_path / "delivery.mp4",
        width=1920, height=1080, fps=25,
    )
    assert (receipt["width"], receipt["height"], receipt["fps"]) == (1920, 1080, 25)
    assert receipt["full_decode_passed"] is True


def test_delivery_profile_preserves_non_square_pixel_display_aspect(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media integration requires FFmpeg and ffprobe")
    source, destination = tmp_path / "wide pixels.mp4", tmp_path / "display-aspect.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", "color=c=red:size=100x100:rate=25:duration=0.4",
            "-vf", "setsar=2", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(source),
        ],
        check=True, capture_output=True,
    )
    metadata = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
        check=True, capture_output=True, text=True,
    ).stdout)
    assert metadata["streams"][0]["sample_aspect_ratio"] == "2:1"
    assert metadata["streams"][0]["display_aspect_ratio"] == "2:1"
    receipt = render_output(
        make_plan({"first": describe_source(source)}, [("first", 0, 400_000)]),
        "joined", destination, width=320, height=240, fps=25,
    )
    assert (receipt["width"], receipt["height"]) == (320, 240)
    image = rgb_frame(destination, 0.2)
    # Display aspect 2:1 needs 320x160 square pixels, with 40 black rows on each side.
    red_rows = [row for row in range(240) if image[(row * 320 + 160) * 3] > 150]
    assert red_rows == list(range(40, 200))
    assert all(image[(120 * 320 + column) * 3] > 150 for column in range(320))
    output_metadata = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(destination)],
        check=True, capture_output=True, text=True,
    ).stdout)
    output_video = next(row for row in output_metadata["streams"] if row["codec_type"] == "video")
    assert output_video["sample_aspect_ratio"] == "1:1"


def test_freeze_uses_selected_source_frame_and_preserves_source_audio(
    changing_media: dict, tmp_path: Path,
) -> None:
    plan = make_plan(changing_media, [("first", 1_000_000, 2_000_000)])
    plan["outputs"][0]["effects"] = [
        effect("freeze_frame", start=1_250_000, end=2_250_000, target=250_000, frame=400_000)
    ]
    destination = tmp_path / "frozen.mp4"
    receipt = render_output(plan, "joined", destination)
    assert abs(receipt["duration_us"] - 2_000_000) <= receipt["duration_tolerance_us"]
    selected = rgb_frame(changing_media["first"]["path"], 0.4)
    for seconds in (0.6, 1.0):
        frozen = rgb_frame(destination, seconds)
        assert sum(abs(a - b) for a, b in zip(selected, frozen)) / len(frozen) < 4
    resumed = rgb_frame(destination, 1.6)
    expected_resumed = rgb_frame(changing_media["first"]["path"], 2.6)
    assert sum(abs(a - b) for a, b in zip(expected_resumed, resumed)) / len(resumed) < 4
    assert sum(abs(a - b) for a, b in zip(selected, resumed)) / len(resumed) > 10
    during_freeze = audio_samples(destination, 0.4, 0.3)
    after_freeze = audio_samples(destination, 1.5, 0.3)
    assert max(abs(sample) for sample in during_freeze) > 1000
    assert abs(tone_frequency(during_freeze) - 880) < 15
    assert abs(tone_frequency(after_freeze) - 1320) < 15


def test_mute_preserves_duration_and_audio_content_on_both_sides(
    changing_media: dict, tmp_path: Path,
) -> None:
    plan = make_plan(changing_media, [("first", 500_000, 2_000_000)])
    plan["outputs"][0]["effects"] = [
        effect("mute", start=900_000, end=1_400_000, target=400_000)
    ]
    destination = tmp_path / "muted.mp4"
    receipt = render_output(plan, "joined", destination)
    assert abs(receipt["duration_us"] - 2_000_000) <= receipt["duration_tolerance_us"]
    muted = audio_samples(destination, 0.45, 0.35)
    before = audio_samples(destination, 0.05, 0.25)
    after = audio_samples(destination, 0.95, 0.25)
    assert muted and max(abs(sample) for sample in muted) <= 2
    assert abs(tone_frequency(before) - 440) < 15
    assert abs(tone_frequency(after) - 880) < 15
    # Check onset near the requested end, independently of interior silence/content checks.
    boundary = audio_samples(destination, 0.85, 0.15)
    onset = next(index for index, sample in enumerate(boundary) if abs(sample) > 1000) / 48_000
    assert abs(onset - 0.05) < 0.015
    actual_picture = rgb_frame(destination, 1.2)
    expected_picture = rgb_frame(changing_media["first"]["path"], 1.7)
    assert sum(abs(a - b) for a, b in zip(expected_picture, actual_picture)) / len(actual_picture) < 4


def test_subframe_effect_boundaries_keep_cumulative_frame_and_sample_counts(
    changing_media: dict, tmp_path: Path,
) -> None:
    plan = make_plan(changing_media, [("first", 0, 1_000_000)])
    plan["outputs"][0]["effects"] = [
        effect("mute", start=10_000, end=510_000, target=10_000)
    ]
    destination = tmp_path / "subframe-boundaries.mp4"
    render_output(plan, "joined", destination)
    metadata = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(destination)],
        check=True, capture_output=True, text=True,
    ).stdout)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
    assert int(video["nb_frames"]) == 25
    assert audio["time_base"] == "1/48000"
    assert int(audio["duration_ts"]) == 48_000
    muted = audio_samples(destination, 0.06, 0.35)
    assert muted and max(abs(sample) for sample in muted) <= 2
    assert max(abs(sample) for sample in audio_samples(destination, 0.6, 0.2)) > 1000


def test_freeze_and_mute_can_overlap_without_affecting_other_ranges(
    changing_media: dict, tmp_path: Path,
) -> None:
    plan = make_plan(changing_media, [("first", 0, 1_000_000)])
    mute = effect("mute", start=400_000, end=600_000, target=400_000)
    mute["item_id"] = "mute-1"
    plan["outputs"][0]["effects"] = [
        effect("freeze_frame", start=200_000, end=800_000, target=200_000, frame=1_600_000),
        mute,
    ]
    destination = tmp_path / "frozen-and-muted.mp4"
    render_output(plan, "joined", destination)
    expected_picture = rgb_frame(changing_media["first"]["path"], 1.6)
    for seconds in (0.3, 0.5, 0.7):
        actual_picture = rgb_frame(destination, seconds)
        assert sum(abs(a - b) for a, b in zip(expected_picture, actual_picture)) / len(actual_picture) < 4
    assert max(abs(sample) for sample in audio_samples(destination, 0.45, 0.1)) <= 2
    assert abs(tone_frequency(audio_samples(destination, 0.23, 0.1)) - 440) < 15
    assert abs(tone_frequency(audio_samples(destination, 0.65, 0.1)) - 440) < 15


def test_effect_fragments_render_after_ripple_deletion(changing_media: dict, tmp_path: Path) -> None:
    plan = make_plan(changing_media, [("first", 0, 500_000), ("first", 1_500_000, 1_000_000)])
    # Both retained spans belong to one original clip; the missing source second ripples out.
    plan["outputs"][0]["segments"][1]["clip_id"] = "clip-0"
    plan["outputs"][0]["effects"] = [
        effect("freeze_frame", start=300_000, end=500_000, target=300_000, frame=800_000),
        effect("freeze_frame", start=1_500_000, end=1_700_000, target=500_000, frame=800_000),
    ]
    destination = tmp_path / "ripple-effect.mp4"
    receipt = render_output(plan, "joined", destination)
    assert abs(receipt["duration_us"] - 1_500_000) <= receipt["duration_tolerance_us"]
    selected = rgb_frame(changing_media["first"]["path"], 0.8)
    actual = rgb_frame(destination, 0.6)
    assert sum(abs(a - b) for a, b in zip(selected, actual)) / len(actual) < 4
    assert abs(tone_frequency(audio_samples(destination, 0.53, 0.12)) - 880) < 15


@pytest.mark.parametrize("frame_time, displayed_frame", [(415_000, 0.4), (2_999_999, 2.96)])
def test_freeze_selects_containing_frame_at_non_aligned_and_tail_times(
    changing_media: dict, tmp_path: Path, frame_time: int, displayed_frame: float,
) -> None:
    plan = make_plan(changing_media, [("first", 0, 400_000)])
    plan["outputs"][0]["effects"] = [
        effect("freeze_frame", start=0, end=400_000, target=0, frame=frame_time)
    ]
    destination = tmp_path / "containing-frame.mp4"
    render_output(plan, "joined", destination)
    expected = rgb_frame(changing_media["first"]["path"], displayed_frame)
    actual = rgb_frame(destination, 0.2)
    assert sum(abs(a - b) for a, b in zip(expected, actual)) / len(actual) < 4


@pytest.mark.parametrize("changing_media", [30], indirect=True)
def test_mute_does_not_change_animation_when_delivery_fps_differs(
    changing_media: dict, tmp_path: Path,
) -> None:
    plan = make_plan(changing_media, [("first", 0, 1_000_000)])
    control, muted = tmp_path / "control.mp4", tmp_path / "mute-at-different-fps.mp4"
    render_output(plan, "joined", control, fps=25)
    plan["outputs"][0]["effects"] = [
        effect("mute", start=250_000, end=650_000, target=250_000)
    ]
    render_output(plan, "joined", muted, fps=25)
    for seconds in (0.2, 0.28, 0.4, 0.68, 0.84):
        expected, actual = rgb_frame(control, seconds), rgb_frame(muted, seconds)
        assert sum(abs(a - b) for a, b in zip(expected, actual)) / len(actual) < 4


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"}, {"source_id": "second"}, {"clip_id": "different-clip"},
        {"source_start_us": 1}, {"source_end_us": 900_000}, {"duration_us": 0},
        {"source_start_us": 300_000, "source_end_us": 900_000},
        {"target_start_us": -1}, {"target_start_us": True}, {"source_text": None},
        {"frame_time_us": 3_000_000}, {"frame_time_us": -1},
    ],
)
def test_invalid_effect_fails_without_publishing(
    videos: dict, tmp_path: Path, changes: dict,
) -> None:
    plan = make_plan(videos, [("first", 0, 1_000_000)])
    invalid = effect("freeze_frame", start=200_000, end=800_000, target=200_000, frame=400_000)
    invalid.update(changes)
    plan["outputs"][0]["effects"] = [invalid]
    destination = tmp_path / "invalid.mp4"
    with pytest.raises(SlicingError):
        render_output(plan, "joined", destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".slicing-render-*"))


def test_overlapping_same_kind_effects_fail_without_publishing(videos: dict, tmp_path: Path) -> None:
    plan = make_plan(videos, [("first", 0, 1_000_000)])
    first = effect("mute", start=200_000, end=600_000, target=200_000)
    second = copy.deepcopy(first)
    second.update(item_id="effect-2", source_start_us=500_000, source_end_us=900_000,
                  target_start_us=500_000)
    plan["outputs"][0]["effects"] = [first, second]
    destination = tmp_path / "overlap.mp4"
    with pytest.raises(SlicingError, match="overlap"):
        render_output(plan, "joined", destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "profile",
    [
        {"width": True}, {"width": 0}, {"width": 321}, {"height": 1.0},
        {"fps": True}, {"fps": 0}, {"fps": float("inf")}, {"fps": float("nan")},
    ],
)
def test_invalid_delivery_profile_fails_without_publishing(
    videos: dict, tmp_path: Path, profile: dict,
) -> None:
    destination = tmp_path / "invalid-profile.mp4"
    with pytest.raises(SlicingError):
        render_output(make_plan(videos, [("first", 0, 1_000_000)]), "joined", destination, **profile)
    assert not destination.exists()


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
