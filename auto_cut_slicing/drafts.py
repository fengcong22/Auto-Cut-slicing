"""Publish segmented editable drafts only after structural acceptance."""

import json
import os
import re
import struct
import tempfile
import wave
from pathlib import Path

from .errors import SlicingError
from .io import file_sha256, json_sha256, read_json, write_json
from .upstream import load_config, run_worker


def _output(plan, output_id):
    matches = [row for row in plan.get("outputs", []) if row.get("id") == output_id]
    if len(matches) != 1:
        raise SlicingError("Output ID must identify exactly one planned output")
    output = matches[0]
    cursor = 0
    for row in output.get("segments", []):
        source = plan.get("sources", {}).get(row.get("source_id"))
        if (
            not source
            or row.get("target_start_us") != cursor
            or not isinstance(row.get("duration_us"), int)
            or row["duration_us"] <= 0
            or not isinstance(row.get("source_start_us"), int)
            or row["source_start_us"] < 0
            or row["source_start_us"] + row["duration_us"] > source["duration_us"]
        ):
            raise SlicingError(
                "Output plan has invalid, overlapping or noncontiguous editable ranges"
            )
        cursor += row["duration_us"]
    if cursor <= 0 or cursor != output.get("duration_us"):
        raise SlicingError("Output plan duration does not equal retained source ranges")
    return output


def _draft_effects(plan, output):
    """Return planned freeze effects; other effects stay MP4-only."""
    from .render import _validate_effects

    effects = output.get("effects", [])
    if not isinstance(effects, list):
        raise SlicingError("Output effects must be a list")
    if any(not isinstance(row, dict) or row.get("kind") != "freeze_frame" for row in effects):
        raise SlicingError("MP4-only effects are not supported in editable draft output")
    _validate_effects(plan, output)
    return effects


def _video_pieces(output):
    """Split retained segments at freeze boundaries exactly as MP4 rendering does."""
    from .render import _render_pieces

    pieces = _render_pieces(output)
    freezes = {}
    for effect in output.get("effects", []):
        start, end = effect["target_start_us"], effect["target_start_us"] + effect["duration_us"]
        for piece in pieces:
            if start <= piece["target_start_us"] < end:
                freezes[piece["target_start_us"]] = effect["item_id"]
    for piece in pieces:
        if "frame_time_us" in piece:
            piece["freeze_item_id"] = freezes[piece["target_start_us"]]
    return pieces


PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def _png_size(path):
    with open(path, "rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise SlicingError("Freeze frame image is not a PNG file")
    return struct.unpack(">II", header[16:24])


def _source_ids(output):
    return set(row["source_id"] for row in output["segments"]) | set(
        row["source_id"] for row in output.get("edits", [])
    )


def _marker_range(marker, duration):
    start = min(max(int(marker["target_start_us"]), 0), duration - 1)
    return {"start": start, "duration": min(2_000_000, duration - start)}


def _material_path(path, declared_root, actual_root):
    target = Path(path)
    if not target.is_absolute():
        raise SlicingError("Draft material paths must be absolute")
    try:
        relative = target.resolve().relative_to(declared_root.resolve() / "Resources" / "local")
    except ValueError as exc:
        raise SlicingError("Draft material is outside its local Resources folder") from exc
    actual = actual_root / "Resources" / "local" / relative
    if not actual.is_file() or actual.is_symlink():
        raise SlicingError("Complete local material is missing or linked externally")
    return actual


def validate_saved_draft(draft_path, plan, output_id, *, declared_root=None) -> dict:
    draft_path = Path(draft_path).resolve()
    declared_root = Path(declared_root or draft_path).resolve()
    output = _output(plan, output_id)
    effects = _draft_effects(plan, output)
    pieces = _video_pieces(output)
    duration = int(output["duration_us"])
    if duration <= 0 or not output.get("segments"):
        raise SlicingError("Cannot accept an empty editable draft")
    content = read_json(draft_path / "draft_content.json")
    timeline_id = content.get("main_timeline_id")
    if not isinstance(timeline_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", timeline_id):
        raise SlicingError("Saved draft is missing a safe active timeline identity")
    active = read_json(draft_path / "Timelines" / timeline_id / "draft_content.json")
    if active != content:
        raise SlicingError("Root and active timeline draft content differ")
    receipt = read_json(draft_path / "edit_receipts.json")
    if receipt.get("plan_sha256") != json_sha256(plan) or receipt.get("output_id") != output_id:
        raise SlicingError("Draft edit receipts do not match the canonical plan")
    if receipt.get("edits") != output.get("edits", []):
        raise SlicingError("Draft edit receipts lost source edit boundaries")
    if int(content.get("duration", -1)) != duration:
        raise SlicingError("Saved duration differs from the compressed plan")
    tracks = content.get("tracks", [])
    if len({row.get("name") for row in tracks}) != len(tracks):
        raise SlicingError("Saved draft contains duplicate track names")
    videos = [row for row in tracks if row.get("type") == "video"]
    audios = [row for row in tracks if row.get("type") == "audio"]
    if len(videos) != 1 or videos[0].get("name") != "V1 Retained Video":
        raise SlicingError("Expected one segmented retained video timeline")
    expected_audio = [
        row for row in output["segments"] if plan["sources"][row["source_id"]].get("has_audio")
    ]
    if (expected_audio and (len(audios) != 1 or audios[0].get("name") != "A1 Retained Voice")) or (
        not expected_audio and audios
    ):
        raise SlicingError("Expected exactly the source-aligned retained audio timeline")
    materials = {
        row["id"]: row
        for bucket in ("videos", "audios")
        for row in content.get("materials", {}).get(bucket, [])
    }
    local_rows = receipt.get("sources", {})
    used = _source_ids(output)
    if set(local_rows) != used:
        raise SlicingError("Local source receipt coverage differs from the plan")
    for source_id, local in local_rows.items():
        source = plan["sources"][source_id]
        source_file = _material_path(local["video_path"], declared_root, draft_path)
        if file_sha256(source_file) != source["sha256"] or local.get("sha256") != source["sha256"]:
            raise SlicingError("Copied full source does not match original bytes")
        if not any(material.get("path") == local["video_path"] for material in materials.values()):
            raise SlicingError("Complete source video is absent from draft material metadata")
        if source.get("has_audio"):
            audio_file = _material_path(local["audio_path"], declared_root, draft_path)
            if file_sha256(audio_file) != local.get("audio_sha256"):
                raise SlicingError("Separated full source audio changed")
            if not any(
                material.get("path") == local["audio_path"] for material in materials.values()
            ):
                raise SlicingError(
                    "Separated full source audio is absent from draft material metadata"
                )
            with wave.open(str(audio_file), "rb") as stream:
                audio_us = round(stream.getnframes() * 1_000_000 / stream.getframerate())
            if abs(audio_us - int(source["duration_us"])) > 100_000:
                raise SlicingError("Separated audio does not cover the full source duration")
    freeze_rows = receipt.get("freeze_frames", [])
    if [row.get("item_id") for row in freeze_rows] != [row["item_id"] for row in effects]:
        raise SlicingError("Freeze frame receipts differ from the planned effects")
    freeze_images = {}
    for row, effect in zip(freeze_rows, effects):
        if (
            row.get("source_id") != effect["source_id"]
            or row.get("frame_time_us") != effect["frame_time_us"]
            or row.get("target_start_us") != effect["target_start_us"]
            or row.get("duration_us") != effect["duration_us"]
        ):
            raise SlicingError("Freeze frame receipt differs from its planned effect")
        image = _material_path(row.get("image_path", ""), declared_root, draft_path)
        if file_sha256(image) != row.get("image_sha256"):
            raise SlicingError("Freeze frame image changed after draft writing")
        source = plan["sources"][effect["source_id"]]
        if _png_size(image) != (int(source["width"]), int(source["height"])):
            raise SlicingError("Freeze frame image size differs from its source picture")
        freeze_images[effect["item_id"]] = row["image_path"]
    video_segments = videos[0].get("segments", [])
    if len(video_segments) != len(pieces):
        raise SlicingError("Saved cut count differs from the source-aligned plan")
    if videos[0].get("attribute", 0) & 3:
        raise SlicingError("Main editable media track is hidden or muted")
    for segment, piece in zip(video_segments, pieces):
        frozen = "frame_time_us" in piece
        source_range = {
            "start": 0 if frozen else piece["source_start_us"],
            "duration": piece["duration_us"],
        }
        target_range = {"start": piece["target_start_us"], "duration": piece["duration_us"]}
        if (
            segment.get("source_timerange") != source_range
            or segment.get("target_timerange") != target_range
        ):
            raise SlicingError("Saved source/target ranges differ from the planned edit")
        material = materials.get(segment.get("material_id"), {})
        expected_path = (
            freeze_images[piece["freeze_item_id"]]
            if frozen
            else local_rows[piece["source_id"]]["video_path"]
        )
        if material.get("path") != expected_path or (material.get("type") == "photo") != frozen:
            raise SlicingError("Saved segment references the wrong complete local source")
        _material_path(material["path"], declared_root, draft_path)
        if float(segment.get("speed", 1.0)) != 1.0:
            raise SlicingError("Draft unexpectedly changes source playback speed")
        expected_volume = (
            0.0 if frozen or plan["sources"][piece["source_id"]].get("has_audio") else 1.0
        )
        if float(segment.get("volume", 1.0)) != expected_volume:
            raise SlicingError("Saved source voice is muted or duplicated")
    for track, expected, media_key in (
        [(audios[0], expected_audio, "audio_path")] if audios else []
    ):
        segments = track.get("segments", [])
        if len(segments) != len(expected):
            raise SlicingError("Saved cut count differs from the source-aligned plan")
        if track.get("attribute", 0) & 3:
            raise SlicingError("Main editable media track is hidden or muted")
        for segment, row in zip(segments, expected):
            source_range = {"start": row["source_start_us"], "duration": row["duration_us"]}
            target_range = {"start": row["target_start_us"], "duration": row["duration_us"]}
            if (
                segment.get("source_timerange") != source_range
                or segment.get("target_timerange") != target_range
            ):
                raise SlicingError("Saved source/target ranges differ from the planned edit")
            material = materials.get(segment.get("material_id"), {})
            if material.get("path") != local_rows[row["source_id"]][media_key]:
                raise SlicingError("Saved segment references the wrong complete local source")
            _material_path(material["path"], declared_root, draft_path)
            if float(segment.get("speed", 1.0)) != 1.0:
                raise SlicingError("Draft unexpectedly changes source playback speed")
            expected_volume = (
                0.0
                if media_key == "video_path" and plan["sources"][row["source_id"]].get("has_audio")
                else 1.0
            )
            if float(segment.get("volume", 1.0)) != expected_volume:
                raise SlicingError("Saved source voice is muted or duplicated")
    text_tracks = [row for row in tracks if row.get("type") == "text"]
    markers = output.get("markers", [])
    if len(text_tracks) != len(markers) or len(receipt.get("markers", [])) != len(markers):
        raise SlicingError("Each source item requires its own verbatim review label")
    texts = {row["id"]: row for row in content.get("materials", {}).get("texts", [])}
    for index, (track, marker, saved) in enumerate(
        zip(text_tracks, markers, receipt.get("markers", [])), 1
    ):
        if track.get("name") != f"Review Marker {index}" or len(track.get("segments", [])) != 1:
            raise SlicingError("Review marker lane is missing or merged")
        segment = track["segments"][0]
        material = texts.get(segment.get("material_id"), {})
        text = json.loads(material.get("content", "{}")).get("text")
        if text != marker["source_text"] or segment.get("target_timerange") != _marker_range(
            marker, duration
        ):
            raise SlicingError("Review marker text or mapped start differs from its source item")
        if (
            saved.get("item_id") != marker["item_id"]
            or saved.get("segment_id") != segment.get("id")
            or saved.get("source_text") != marker["source_text"]
        ):
            raise SlicingError("Review marker receipt mapping differs from the saved draft")
        if segment.get("clip", {}).get("alpha") != 0.0:
            raise SlicingError("Review marker unexpectedly renders over the product video")
    if len({row["item_id"] for row in markers}) != len(markers):
        raise SlicingError("Duplicate stable source item marker IDs")
    return {
        "status": "pass",
        "acceptance": "development_structural",
        "duration_us": duration,
        "video_segment_count": len(pieces),
        "freeze_frame_count": len(effects),
        "audio_segment_count": len(expected_audio),
        "marker_count": len(markers),
        "source_count": len(used),
        "root_active_timeline_equal": True,
        "target_open_verified": False,
        "full_reverse_asr_verified": False,
    }


def write_draft(plan, output_id, drafts_root, *, config=None) -> dict:
    from .media import verify_sources

    output = _output(plan, output_id)
    _draft_effects(plan, output)
    verify_sources(plan)
    name = str(output.get("name") or output_id)
    if (
        name in {"", ".", ".."}
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        or name.rstrip(". ") != name
    ):
        raise SlicingError("Output name is not a safe draft directory name")
    root = Path(drafts_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    if destination.exists():
        raise SlicingError("Draft destination already exists; choose a new name")
    with tempfile.TemporaryDirectory(prefix=".slicing-stage-", dir=root) as temporary:
        payload = {
            "plan": plan,
            "output_id": output_id,
            "staging_root": temporary,
            "final_path": str(destination),
        }
        payload_path = write_json(Path(temporary) / "payload.json", payload)
        worker_result = run_worker(
            config or load_config(), "draft", payload_path, Path(temporary) / "result.json"
        )
        staged = Path(temporary) / name
        validation = validate_saved_draft(staged, plan, output_id, declared_root=destination)
        verify_sources(plan)
        if destination.exists():
            raise SlicingError("Draft destination appeared during staging")
        os.rename(staged, destination)
    validation = validate_saved_draft(destination, plan, output_id)
    return {
        "draft_name": name,
        "draft_path": str(destination),
        "output_id": output_id,
        "validation": validation,
        "edit_receipts_path": str(destination / "edit_receipts.json"),
        "upstream": worker_result.get("upstream"),
        "baked_windows": [],
    }
