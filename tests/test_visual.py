import hashlib
import importlib
import shutil
import subprocess

import pytest

from auto_cut_slicing.errors import SlicingError


def analyze(source_path, frame_time, **kwargs):
    try:
        module = importlib.import_module("auto_cut_slicing.visual")
    except ModuleNotFoundError:
        pytest.fail("The frame-boundary analyzer has not been implemented")
    return module.analyze_freeze(source_path, frame_time, **kwargs)


@pytest.fixture
def changing_video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real-media visual inspection requires FFmpeg and ffprobe")
    path = tmp_path / "visual-change.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-f", "lavfi",
         "-i", "color=c=red:size=64x48:rate=25:duration=1", "-f", "lavfi", "-i",
         "color=c=blue:size=64x48:rate=25:duration=1", "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "ffv1", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_next_decoded_visual_change_is_proposed_without_semantic_acceptance(changing_video):
    result = analyze(changing_video, 0.4)
    assert result["source_sha256"] == hashlib.sha256(changing_video.read_bytes()).hexdigest()
    assert result["frame_time"] == 0.4
    assert result["end"] == 1.0
    assert result["method"] == "decoded_frame_pixel_difference"
    assert result["semantic_verified"] is False
    assert result["boundary_evidence"]["change_detected"] is True
    assert result["boundary_evidence"]["candidate_time"] == 1.0
    assert result["boundary_evidence"]["mean_absolute_difference"] > 3
    assert result["limitations"]


def test_explicit_end_is_preserved_as_a_user_boundary(changing_video):
    result = analyze(changing_video, "00:00.400", end=1.4)
    assert (result["frame_time"], result["end"]) == (0.4, 1.4)
    assert result["boundary_evidence"]["end_selection"] == "explicit"
    assert result["semantic_verified"] is False


def test_unchanging_tail_proposes_source_end(changing_video):
    result = analyze(changing_video, 1.2)
    assert result["end"] == 2.0
    assert result["boundary_evidence"]["change_detected"] is False
    assert result["boundary_evidence"]["end_selection"] == "source_end"


def test_frame_time_cannot_silently_seek_to_a_different_frame(changing_video):
    with pytest.raises(SlicingError, match="decoded frame timestamp"):
        analyze(changing_video, 0.41)


@pytest.mark.parametrize(
    ("frame_time", "end"),
    [(True, None), (-0.1, None), (2, None), (0.4, 0.4), (0.4, 3), (0.4, False)],
)
def test_invalid_frame_or_end_coordinates_are_rejected(changing_video, frame_time, end):
    with pytest.raises(SlicingError):
        analyze(changing_video, frame_time, end=end)


def test_missing_source_is_reported_as_slicing_error(tmp_path):
    with pytest.raises(SlicingError, match="Source"):
        analyze(tmp_path / "missing.mp4", 0)
