"""CLI for the independent slicing workflow; no upstream workflow dispatch."""

import argparse
import json
import sys
from pathlib import Path

from .errors import SlicingError
from .io import write_json, write_new_json


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="auto-cut-slicing", description="视频切片、拼接、删词和删段落"
    )
    commands = root.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="保存本机 Auto-Cut Lite 源码依赖位置")
    configure.add_argument("--upstream-root", required=True, type=Path)
    configure.add_argument("--upstream-python", type=Path)
    doctor = commands.add_parser("doctor", help="检查 FFmpeg 和锁定的源码依赖")
    doctor.add_argument("--config", type=Path)
    transcribe = commands.add_parser(
        "transcribe", help="调用现有火山词级 ASR，生成源文件绑定的时间戳"
    )
    transcribe.add_argument("source", type=Path)
    transcribe.add_argument("--output", required=True, type=Path)
    transcribe.add_argument("--env", type=Path)
    transcribe.add_argument("--config", type=Path)
    plan = commands.add_parser("plan", help="校验任务并编译确定性的压缩时间轴")
    plan.add_argument("job", type=Path)
    plan.add_argument("--output", required=True, type=Path)
    draft = commands.add_parser("draft", help="编译任务并输出独立切片或拼接的可编辑剪映草稿")
    draft.add_argument("job", type=Path)
    draft.add_argument("--drafts-root", required=True, type=Path)
    draft.add_argument("--output-id", action="append", help="可重复；默认生成任务内所有输出")
    draft.add_argument("--config", type=Path)
    draft.add_argument("--receipt", type=Path)
    render = commands.add_parser("render", help="明确导出 MP4（草稿之外的可选输出）")
    render.add_argument("job", type=Path)
    render.add_argument("--output-id", required=True)
    render.add_argument("--output", required=True, type=Path)
    return root


def dispatch(args) -> dict:
    if args.command == "configure":
        from .upstream import load_config, validate_upstream

        config = {"upstream_root": str(args.upstream_root.resolve())}
        if args.upstream_python:
            config["upstream_python"] = str(args.upstream_python.resolve())
        # Resolve defaults without loading another machine's saved configuration.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="slicing-config-") as temporary:
            path = write_json(Path(temporary) / "config.json", config)
            config = load_config(path)
        receipt = validate_upstream(config)
        destination = Path(__file__).resolve().parents[1] / ".local" / "config.json"
        write_json(destination, config)
        return {"ok": True, "config_path": str(destination), "dependency": receipt}
    if args.command == "doctor":
        from .media import find_tool
        from .upstream import load_config, validate_upstream

        return {
            "ok": True,
            "ffmpeg": find_tool("ffmpeg"),
            "ffprobe": find_tool("ffprobe"),
            "dependency": validate_upstream(load_config(args.config)),
        }
    if args.command == "transcribe":
        from .upstream import load_config, transcribe_source

        if args.output.exists():
            raise SlicingError(f"Refusing to overwrite: {args.output}")
        return transcribe_source(
            args.source, args.output, config=load_config(args.config), env_path=args.env
        )

    if args.command == "plan" and args.output.exists():
        raise SlicingError(f"Refusing to overwrite: {args.output}")
    if args.command == "draft" and args.receipt and args.receipt.exists():
        raise SlicingError(f"Refusing to overwrite receipt: {args.receipt}")

    from .planner import compile_job

    plan = compile_job(args.job)
    if args.command == "plan":
        write_new_json(args.output, plan)
        return {
            "ok": True,
            "plan_path": str(args.output.resolve()),
            "outputs": [
                {
                    "id": row["id"],
                    "duration_us": row["duration_us"],
                    "segment_count": len(row["segments"]),
                }
                for row in plan["outputs"]
            ],
        }
    if args.command == "draft":
        from .drafts import write_draft
        from .upstream import load_config

        selected = args.output_id or [row["id"] for row in plan["outputs"]]
        available = {row["id"] for row in plan["outputs"]}
        if len(selected) != len(set(selected)) or not set(selected) <= available:
            raise SlicingError("Output IDs must be unique and declared in the job")
        # Preflight the entire batch before generating any draft.
        chosen = [row for row in plan["outputs"] if row["id"] in selected]
        for row in chosen:
            destination = (args.drafts_root / row["name"]).resolve()
            if destination.exists():
                raise SlicingError(f"Draft already exists: {row['name']}")
            if args.receipt and args.receipt.resolve().is_relative_to(destination):
                raise SlicingError("Batch receipt must be outside generated draft directories")
        config = load_config(args.config)
        receipt = {
            "ok": False,
            "status": "in_progress",
            "job_sha256": plan["job_sha256"],
            "planned_output_ids": [row["id"] for row in chosen],
            "drafts": [],
        }
        if args.receipt:
            write_new_json(args.receipt, receipt)
        try:
            for row in chosen:
                receipt["drafts"].append(
                    write_draft(plan, row["id"], args.drafts_root, config=config)
                )
                if args.receipt:
                    write_json(args.receipt, receipt)
        except Exception as error:
            receipt.update(status="failed", error=str(error))
            if args.receipt:
                write_json(args.receipt, receipt)
            raise
        receipt.update(ok=True, status="complete")
        if args.receipt:
            write_json(args.receipt, receipt)
        return receipt
    if args.command == "render":
        from .render import render_output

        return render_output(plan, args.output_id, args.output)
    raise SlicingError("Unknown command")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (SlicingError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
