"""Real-media integration smoke with explicit synthetic ASR, never a service acceptance claim."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_cut_slicing.drafts import write_draft
from auto_cut_slicing.io import file_sha256, write_json
from auto_cut_slicing.media import find_tool
from auto_cut_slicing.planner import compile_job
from auto_cut_slicing.render import render_output
from auto_cut_slicing.upstream import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    (root / "tmp").mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="slicing-smoke-", dir=root / "tmp"))
    ffmpeg = find_tool("ffmpeg")
    talk, broll = temporary / "talk.mp4", temporary / "broll.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(talk),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=320x180:rate=25:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(broll),
        ],
        check=True,
        capture_output=True,
    )
    asr = write_json(
        temporary / "talk.words.json",
        {
            "schema_version": 1,
            "source_sha256": file_sha256(talk),
            "provider": "synthetic_test_fixture",
            "model": "no_real_speech",
            "adapter": "slicing-smoke-v1",
            "words": [
                {"text": "开头", "start": 0.1, "end": 0.8},
                {"text": "就是说", "start": 1, "end": 2},
                {"text": "核心观点", "start": 2.1, "end": 2.9},
                {"text": "重复段落", "start": 3, "end": 4},
                {"text": "结尾", "start": 4.1, "end": 5.8},
            ],
        },
    )
    job = write_json(
        temporary / "job.json",
        {
            "schema_version": 1,
            "name": "SlicingSmoke",
            "sources": [
                {"id": "talk", "path": str(talk), "asr": str(asr)},
                {"id": "broll", "path": str(broll)},
            ],
            "outputs": [
                {
                    "id": "slice",
                    "name": "SmokeSlice",
                    "clips": [
                        {
                            "id": "speech",
                            "source": "talk",
                            "start": 0,
                            "end": 6,
                            "deletions": [
                                {
                                    "id": "word",
                                    "kind": "word",
                                    "text": "就是说",
                                    "must_keep": ["核心观点"],
                                    "source_text": "删除“就是说”；保留“核心观点”",
                                },
                                {
                                    "id": "range",
                                    "kind": "range",
                                    "start": 3,
                                    "end": 4,
                                    "source_text": "00:03–00:04 删除重复段落\n保持其他内容",
                                },
                            ],
                        }
                    ],
                },
                {
                    "id": "montage",
                    "name": "SmokeMontage",
                    "clips": [
                        {"id": "blue", "source": "broll", "start": 0, "end": 2},
                        {"id": "tail", "source": "talk", "start": 4, "end": 6},
                    ],
                },
                {
                    "id": "deleted_source",
                    "name": "SmokeDeletedSource",
                    "clips": [
                        {
                            "id": "erase_talk",
                            "source": "talk",
                            "start": 0,
                            "end": 6,
                            "deletions": [
                                {
                                    "id": "all",
                                    "kind": "range",
                                    "start": 0,
                                    "end": 6,
                                    "source_text": "整段移除，原始视频和音频保留在素材库",
                                }
                            ],
                        },
                        {"id": "keep_blue", "source": "broll", "start": 0, "end": 2},
                    ],
                },
            ],
        },
    )
    plan = compile_job(job)
    assert [row["duration_us"] for row in plan["outputs"]] == [4_000_000, 4_000_000, 2_000_000]
    assert len(plan["outputs"][0]["segments"]) == 3
    config = load_config(args.config)
    receipts = []
    for output in plan["outputs"]:
        receipts.append(write_draft(plan, output["id"], temporary / "drafts", config=config))
    render = render_output(plan, "slice", temporary / "slice.mp4")
    result = {
        "ok": True,
        "test_media": "synthetic video, sine wave and synthetic ASR fixture",
        "workspace": str(temporary),
        "drafts": receipts,
        "render": render,
        "real_asr_executed": False,
        "target_open_verified": False,
        "spoken_final_acceptance": False,
    }
    write_json(temporary / "smoke-report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
