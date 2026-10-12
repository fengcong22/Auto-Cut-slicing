"""Subprocess boundary: import upstream code only after identity validation."""

import argparse
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_cut_slicing.errors import SlicingError
from auto_cut_slicing.io import file_sha256, json_sha256, read_json, write_json
from auto_cut_slicing.upstream import validate_upstream


def _replace_paths(value, old: Path, new: Path):
    if isinstance(value, dict):
        return {key: _replace_paths(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, old, new) for item in value]
    if isinstance(value, str):
        # Only complete path prefixes are rewritten; visible source text is never
        # passed through this helper separately from the draft's JSON materials.
        prefix = str(old)
        if value == prefix or value.startswith(prefix + "\\") or value.startswith(prefix + "/"):
            return str(new) + value[len(prefix) :]
    return value


def _draft(payload, upstream):
    import pyJianYingDraft as draft
    from jy_wrapper import JyProject

    from auto_cut_slicing.drafts import _marker_range, _output, _source_ids, validate_saved_draft
    from auto_cut_slicing.media import extract_source_audio, verify_sources

    plan = payload["plan"]
    verify_sources(plan)
    output = _output(plan, payload["output_id"])
    final_path = Path(payload["final_path"]).resolve()
    staging_root = Path(payload["staging_root"]).resolve()
    staged = staging_root / final_path.name
    if final_path.exists() or staged.exists():
        raise SlicingError("Refusing to replace an existing editable draft")
    sources = plan["sources"]
    first = sources[output["segments"][0]["source_id"]]
    project = JyProject(
        final_path.name,
        width=int(first["width"]),
        height=int(first["height"]),
        drafts_root=str(staging_root),
        overwrite=False,
        ui_mode="offline",
    )
    project.script.fps = max(1, round(float(first["fps"])))
    timeline_id = project.script.content["id"]
    project.script.content["main_timeline_id"] = timeline_id
    project.script.add_track(draft.TrackType.video, "V1 Retained Video")
    if any(sources[row["source_id"]].get("has_audio") for row in output["segments"]):
        project.script.add_track(draft.TrackType.audio, "A1 Retained Voice")
    resources = staged / "Resources" / "local"
    resources.mkdir(parents=True, exist_ok=True)
    local_sources = {}
    used_ids = sorted(_source_ids(output))
    for index, source_id in enumerate(used_ids, 1):
        source = sources[source_id]
        original = Path(source["path"]).resolve(strict=True)
        video_path = (
            resources / f"source_{index:03d}_{source['sha256'][:12]}{original.suffix.lower()}"
        )
        shutil.copy2(original, video_path)
        if file_sha256(video_path) != source["sha256"]:
            raise SlicingError("Full source copy failed its SHA-256 check")
        local = {
            "video_path": str(final_path / "Resources" / "local" / video_path.name),
            "sha256": source["sha256"],
            "duration_us": source["duration_us"],
            "staged_video_path": str(video_path),
        }
        if source.get("has_audio"):
            audio_path = resources / f"source_{index:03d}_{source['sha256'][:12]}_full.wav"
            extract_source_audio(video_path, audio_path)
            local.update(
                {
                    "audio_path": str(final_path / "Resources" / "local" / audio_path.name),
                    "audio_sha256": file_sha256(audio_path),
                    "staged_audio_path": str(audio_path),
                }
            )
        local_sources[source_id] = local
        # Preserve wholly deleted source clips in the material bin as well as
        # complete local files, without reintroducing them on playable tracks.
        project.script.materials.videos.append(draft.VideoMaterial(str(video_path)))
        if source.get("has_audio"):
            project.script.materials.audios.append(draft.AudioMaterial(str(audio_path)))
    segments_receipt = []
    for row in output["segments"]:
        local = local_sources[row["source_id"]]
        video = project.add_clip(
            local["staged_video_path"],
            source_start=row["source_start_us"],
            duration=row["duration_us"],
            target_start=row["target_start_us"],
            track_name="V1 Retained Video",
        )
        if video is None:
            raise SlicingError("Upstream could not load a source video")
        video.volume = 0.0 if sources[row["source_id"]].get("has_audio") else 1.0
        saved = {**row, "video_segment_id": video.segment_id}
        if sources[row["source_id"]].get("has_audio"):
            audio = draft.AudioSegment(
                draft.AudioMaterial(local["staged_audio_path"]),
                draft.Timerange(row["target_start_us"], row["duration_us"]),
                source_timerange=draft.Timerange(row["source_start_us"], row["duration_us"]),
            )
            project.script.add_segment(audio, "A1 Retained Voice")
            saved["audio_segment_id"] = audio.segment_id
        segments_receipt.append(saved)
    marker_receipts = []
    for index, marker in enumerate(output.get("markers", []), 1):
        timerange = _marker_range(marker, int(output["duration_us"]))
        segment = project.add_text_simple(
            marker["source_text"],
            start_time=timerange["start"],
            duration=timerange["duration"],
            track_name=f"Review Marker {index}",
            clip_settings=draft.ClipSettings(alpha=0.0),
        )
        marker_receipts.append(
            {
                **marker,
                "segment_id": segment.segment_id,
                "display_timerange": timerange,
                "rendering": "timeline_label_only_alpha_zero",
            }
        )
    project.save(auto_retain=False)

    # The helper serializes material paths from existing staging files. Rewrite
    # only path-valued keys, never marker source text or rich-text content.
    def relocate(value):
        if isinstance(value, dict):
            return {
                key: (
                    _replace_paths(item, staged, final_path)
                    if key
                    in {
                        "path",
                        "media_path",
                        "draft_fold_path",
                        "draft_root_path",
                        "file_Path",
                        "source_path",
                    }
                    else relocate(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [relocate(item) for item in value]
        return value

    for path in staged.rglob("*.json"):
        value = relocate(read_json(path))
        if path.name == "draft_meta_info.json":
            value["draft_fold_path"] = str(final_path)
            value["draft_root_path"] = str(final_path.parent)
        write_json(path, value)
    content = read_json(staged / "draft_content.json")
    content["main_timeline_id"] = timeline_id
    write_json(staged / "draft_content.json", content)
    write_json(staged / "Timelines" / timeline_id / "draft_content.json", content)
    local_sources = {
        source_id: {key: value for key, value in row.items() if not key.startswith("staged_")}
        for source_id, row in local_sources.items()
    }
    receipt = {
        "schema_version": 1,
        "workflow_mode": "slicing",
        "output_id": output["id"],
        "plan_sha256": json_sha256(plan),
        "job_sha256": plan.get("job_sha256"),
        "sources": local_sources,
        "segments": segments_receipt,
        "markers": marker_receipts,
        "edits": output.get("edits", []),
        "upstream": upstream,
        "acceptance": "development_structural",
        "target_open_verified": False,
        "full_reverse_asr_verified": False,
        "baked_windows": [],
    }
    write_json(staged / "edit_receipts.json", receipt)
    validation = validate_saved_draft(staged, plan, output["id"], declared_root=final_path)
    verify_sources(plan)
    return {"staged_path": str(staged), "validation": validation, "upstream": upstream}


def _transcribe(payload, upstream):
    from audio_sound.volc_asr import (
        extract_audio_from_video,
        load_volc_asr_config,
        normalize_result,
        run_volc_asr,
    )

    original = Path(payload["source_path"]).resolve(strict=True)
    if file_sha256(original) != payload["source_sha256"]:
        raise SlicingError("Source changed before ASR execution")
    audio = extract_audio_from_video(original, Path(payload["work_dir"]) / "alignment.m4a")
    config = load_volc_asr_config(payload.get("env_path"))
    normalized = normalize_result(run_volc_asr(audio, config=config))
    if normalized["input_sha256"] != file_sha256(audio):
        raise SlicingError("ASR evidence does not match extracted alignment audio")
    if file_sha256(original) != payload["source_sha256"]:
        raise SlicingError("Source changed during ASR execution")
    return {
        "schema_version": 1,
        "source_sha256": payload["source_sha256"],
        "provider": normalized["provider"],
        "model": "bigmodel",
        "adapter": normalized["adapter_version"],
        "alignment_audio_sha256": normalized["input_sha256"],
        "service_job_id": normalized["service_job_id"],
        "service_result_sha256": normalized["service_result_sha256"],
        "resource_id": normalized["resource_id"],
        "words": normalized["words"],
        "utterances": normalized["utterances"],
        "text": normalized["text"],
        "audio_duration_seconds": normalized["audio_duration_seconds"],
        "upstream_revision": upstream["revision"],
        "evidence_origin": "live_upstream_service",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-root", required=True)
    parser.add_argument("--action", choices=["draft", "transcribe"], required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        upstream = validate_upstream(
            {"upstream_root": args.upstream_root, "upstream_python": sys.executable}
        )
        # Precedence matches the maintained setup_env contract. No upstream import
        # is allowed above the independent identity/revision/source checks.
        root = Path(args.upstream_root).resolve()
        sys.path[0:0] = [str(root / "scripts"), str(root / "scripts" / "vendor"), str(root)]
        payload = read_json(args.payload)
        data = (
            _draft(payload, upstream) if args.action == "draft" else _transcribe(payload, upstream)
        )
        write_json(args.result, {"ok": True, "data": data})
        return 0
    except Exception as exc:
        write_json(args.result, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
