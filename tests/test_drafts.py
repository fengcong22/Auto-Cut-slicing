import copy
import json
import subprocess
import sys
import types
import wave

import pytest

from auto_cut_slicing.drafts import validate_saved_draft, write_draft
from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.io import file_sha256, json_sha256, read_json, write_json
from auto_cut_slicing.upstream import validate_upstream


@pytest.fixture
def saved_draft(tmp_path):
    root = tmp_path / "可编辑切片"
    local = root / "Resources" / "local"
    local.mkdir(parents=True)
    video = local / "original.mp4"
    video.write_bytes(b"complete original source: retained and deleted sections")
    audio = local / "original_full.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 8000)
    source = {
        "path": str(video),
        "sha256": file_sha256(video),
        "duration_us": 1_000_000,
        "width": 320,
        "height": 180,
        "fps": 25,
        "has_audio": True,
    }
    segments = [
        {
            "id": "s1",
            "source_id": "src",
            "clip_id": "c1",
            "source_start_us": 0,
            "target_start_us": 0,
            "duration_us": 200_000,
        },
        {
            "id": "s2",
            "source_id": "src",
            "clip_id": "c1",
            "source_start_us": 600_000,
            "target_start_us": 200_000,
            "duration_us": 400_000,
        },
    ]
    marker = {
        "item_id": "delete-1",
        "source_text": "00:00 删掉“这句话”？\n 保留空格。",
        "source_id": "src",
        "source_start_us": 200_000,
        "source_end_us": 600_000,
        "target_start_us": 200_000,
        "execution_status": "executed",
    }
    output = {
        "id": "out",
        "name": root.name,
        "duration_us": 600_000,
        "segments": segments,
        "markers": [marker],
        "edits": [
            {
                "item_id": "delete-1",
                "source_id": "src",
                "source_start_us": 200_000,
                "source_end_us": 600_000,
            }
        ],
    }
    plan = {
        "schema_version": 1,
        "workflow_mode": "slicing",
        "sources": {"src": source},
        "outputs": [output],
        "job_sha256": "job",
    }

    def media_segments(material, volume):
        return [
            {
                "id": f"{material}-{index}",
                "material_id": material,
                "source_timerange": {
                    "start": row["source_start_us"],
                    "duration": row["duration_us"],
                },
                "target_timerange": {
                    "start": row["target_start_us"],
                    "duration": row["duration_us"],
                },
                "volume": volume,
                "speed": 1.0,
            }
            for index, row in enumerate(segments)
        ]

    content = {
        "duration": 600_000,
        "main_timeline_id": "timeline-main",
        "materials": {
            "videos": [{"id": "v", "path": str(video)}],
            "audios": [{"id": "a", "path": str(audio)}],
            "texts": [
                {
                    "id": "text",
                    "content": json.dumps({"text": marker["source_text"]}, ensure_ascii=False),
                }
            ],
        },
        "tracks": [
            {"name": "V1 Retained Video", "type": "video", "segments": media_segments("v", 0.0)},
            {"name": "A1 Retained Voice", "type": "audio", "segments": media_segments("a", 1.0)},
            {
                "name": "Review Marker 1",
                "type": "text",
                "segments": [
                    {
                        "id": "label",
                        "material_id": "text",
                        "target_timerange": {"start": 200_000, "duration": 400_000},
                        "clip": {"alpha": 0.0},
                    }
                ],
            },
        ],
    }
    receipt = {
        "plan_sha256": json_sha256(plan),
        "output_id": "out",
        "edits": output["edits"],
        "sources": {
            "src": {
                "video_path": str(video),
                "sha256": source["sha256"],
                "audio_path": str(audio),
                "audio_sha256": file_sha256(audio),
            }
        },
        "markers": [{**marker, "segment_id": "label"}],
    }
    write_json(root / "draft_content.json", content)
    write_json(root / "Timelines" / "timeline-main" / "draft_content.json", content)
    write_json(root / "edit_receipts.json", receipt)
    return root, plan, content


def _save_both(root, content):
    write_json(root / "draft_content.json", content)
    write_json(root / "Timelines" / "timeline-main" / "draft_content.json", content)


def test_saved_draft_retains_editable_ranges_exact_labels_and_full_sources(saved_draft):
    root, plan, _ = saved_draft
    result = validate_saved_draft(root, plan, "out")
    assert result["video_segment_count"] == result["audio_segment_count"] == 2
    assert result["duration_us"] == 600_000
    assert result["marker_count"] == 1
    assert result["target_open_verified"] is False
    assert result["full_reverse_asr_verified"] is False


@pytest.mark.parametrize(
    "change",
    [
        "audio_offset",
        "missing_cut",
        "audible_deleted_track",
        "marker_text",
        "marker_alpha",
        "source_path",
        "muted_voice",
    ],
)
def test_saved_draft_rejects_regressions(saved_draft, change):
    root, plan, content = saved_draft
    if change == "audio_offset":
        content["tracks"][1]["segments"][1]["source_timerange"]["start"] = 0
    elif change == "missing_cut":
        content["tracks"][0]["segments"].pop()
    elif change == "audible_deleted_track":
        content["tracks"].append({"name": "Deleted voice", "type": "audio", "segments": []})
    elif change == "marker_text":
        content["materials"]["texts"][0]["content"] = json.dumps({"text": "删掉这句话"})
    elif change == "marker_alpha":
        content["tracks"][2]["segments"][0]["clip"]["alpha"] = 1.0
    elif change == "source_path":
        content["materials"]["videos"][0]["path"] = str(root.parent / "external.mp4")
    elif change == "muted_voice":
        content["tracks"][1]["segments"][0]["volume"] = 0.0
    _save_both(root, content)
    with pytest.raises(SlicingError):
        validate_saved_draft(root, plan, "out")


def test_saved_draft_checks_both_root_and_active_timeline(saved_draft):
    root, plan, content = saved_draft
    content["duration"] += 1
    write_json(root / "Timelines" / "timeline-main" / "draft_content.json", content)
    with pytest.raises(SlicingError, match="Root and active"):
        validate_saved_draft(root, plan, "out")


def test_saved_draft_checks_original_bytes(saved_draft):
    root, plan, _ = saved_draft
    (root / "Resources" / "local" / "original.mp4").write_bytes(b"flattened preview")
    with pytest.raises(SlicingError, match="original bytes"):
        validate_saved_draft(root, plan, "out")


def test_write_refuses_existing_destination_without_worker(saved_draft, monkeypatch):
    root, plan, _ = saved_draft
    monkeypatch.setattr("auto_cut_slicing.media.verify_sources", lambda plan: None)
    monkeypatch.setattr(
        "auto_cut_slicing.drafts.run_worker", lambda *args: pytest.fail("worker must not run")
    )
    with pytest.raises(SlicingError, match="already exists"):
        write_draft(plan, "out", root.parent)


def test_dependency_checks_identity_before_revision_and_source_cleanliness(tmp_path, monkeypatch):
    from auto_cut_slicing import upstream

    lock = read_json(upstream.REPOSITORY_ROOT / "auto-cut-dependency.lock.json")
    calls = []

    def fake_run(command, root):
        calls.append(command)
        if len(calls) == 1:
            result = json.dumps(
                {
                    "ok": True,
                    "repository": "auto-cut-lite",
                    "primary_branch": "main",
                    "workflow_mode": "lite",
                    "branch": "main",
                }
            )
        elif command[1:3] == ["rev-parse", "HEAD"]:
            result = lock["revision"]
        else:
            result = ""
        return subprocess.CompletedProcess(command, 0, result, "")

    monkeypatch.setattr(upstream, "_run", fake_run)
    result = validate_upstream({"upstream_root": str(tmp_path), "upstream_python": "python"})
    assert calls[0][-1] == "scripts/assert_lite_workspace.py"
    assert result["revision"] == lock["revision"]


def test_output_plan_cannot_bypass_contiguous_target_gate(saved_draft):
    root, plan, _ = saved_draft
    bad = copy.deepcopy(plan)
    bad["outputs"][0]["segments"][1]["target_start_us"] += 1
    with pytest.raises(SlicingError, match="noncontiguous"):
        validate_saved_draft(root, bad, "out")


def test_wholly_deleted_source_still_requires_full_local_material(saved_draft):
    root, plan, content = saved_draft
    removed = root / "Resources" / "local" / "wholly_deleted.mp4"
    removed.write_bytes(b"complete source of a wholly deleted clip")
    plan["sources"]["removed"] = {
        "path": str(removed),
        "sha256": file_sha256(removed),
        "duration_us": 1_000_000,
        "has_audio": False,
    }
    plan["outputs"][0]["edits"].append(
        {
            "item_id": "delete-all",
            "source_id": "removed",
            "source_start_us": 0,
            "source_end_us": 1_000_000,
        }
    )
    receipt = read_json(root / "edit_receipts.json")
    receipt["plan_sha256"] = json_sha256(plan)
    receipt["edits"] = plan["outputs"][0]["edits"]
    receipt["sources"]["removed"] = {"video_path": str(removed), "sha256": file_sha256(removed)}
    write_json(root / "edit_receipts.json", receipt)
    with pytest.raises(SlicingError, match="absent from draft material metadata"):
        validate_saved_draft(root, plan, "out")
    content["materials"]["videos"].append({"id": "unplayed-original", "path": str(removed)})
    _save_both(root, content)
    assert validate_saved_draft(root, plan, "out")["source_count"] == 2
    assert len(content["tracks"][0]["segments"]) == 2


@pytest.mark.parametrize("bad_audio_hash", [False, True])
def test_transcription_binds_source_and_alignment_audio_without_network(
    tmp_path, monkeypatch, bad_audio_hash
):
    from auto_cut_slicing.draft_worker import _transcribe

    source = tmp_path / "source.mp4"
    source.write_bytes(b"complete original video")
    extracted = tmp_path / "alignment.m4a"

    def extract(original, output):
        assert original == source
        output.write_bytes(b"alignment audio extracted from full source")
        return output

    def recognize(audio, *, config):
        return {
            "input_sha256": "0" * 64 if bad_audio_hash else file_sha256(audio),
            "provider": "volc_asr",
            "adapter_version": "auto-cut-volc-asr-v4",
            "service_job_id": "mock-service-job",
            "service_result_sha256": "b" * 64,
            "resource_id": "volc.bigasr.auc",
            "words": [{"text": "你好", "start": 0.1, "end": 0.3}],
            "utterances": [],
            "text": "你好",
            "audio_duration_seconds": 1.0,
        }

    adapter = types.ModuleType("audio_sound.volc_asr")
    adapter.extract_audio_from_video = extract
    adapter.load_volc_asr_config = lambda env: object()
    adapter.run_volc_asr = recognize
    adapter.normalize_result = lambda payload: payload
    monkeypatch.setitem(sys.modules, "audio_sound.volc_asr", adapter)
    payload = {
        "source_path": str(source),
        "source_sha256": file_sha256(source),
        "work_dir": str(tmp_path),
        "env_path": None,
    }
    if bad_audio_hash:
        with pytest.raises(SlicingError, match="does not match extracted"):
            _transcribe(payload, {"revision": "pinned"})
    else:
        result = _transcribe(payload, {"revision": "pinned"})
        assert result["source_sha256"] == file_sha256(source)
        assert result["alignment_audio_sha256"] == file_sha256(extracted)
        assert result["model"] == "bigmodel"
        assert result["resource_id"] == "volc.bigasr.auc"
        assert result["service_job_id"] == "mock-service-job"
