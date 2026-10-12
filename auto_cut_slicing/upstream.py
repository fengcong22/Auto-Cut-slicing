"""Isolated, pinned access to Auto-Cut Lite's maintained primitives."""

import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import SlicingError
from .io import read_json

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_config(config_path=None) -> dict:
    path = Path(config_path or REPOSITORY_ROOT / ".local" / "config.json").resolve()
    if not path.is_file():
        raise SlicingError("Configure the dependency first with the configure command")
    value = read_json(path)
    if not isinstance(value, dict) or not value.get("upstream_root"):
        raise SlicingError("Local configuration must declare upstream_root")
    root = Path(value["upstream_root"]).expanduser().resolve()
    default_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = (
        Path(
            value.get("upstream_python")
            or (default_python if default_python.is_file() else sys.executable)
        )
        .expanduser()
        .resolve()
    )
    return {**value, "upstream_root": str(root), "upstream_python": str(python)}


def _run(command, root) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SlicingError(f"Cannot execute dependency check: {exc}") from exc


def validate_upstream(config: dict) -> dict:
    root = Path(config["upstream_root"]).expanduser().resolve()
    python = Path(config.get("upstream_python") or sys.executable).expanduser().resolve()
    # This is deliberately the first command issued against the upstream checkout.
    identity_result = _run([str(python), "scripts/assert_lite_workspace.py"], root)
    try:
        identity = json.loads(identity_result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SlicingError("Upstream identity assertion did not return valid JSON") from exc
    if (
        identity_result.returncode
        or any(
            identity.get(key) != expected
            for key, expected in {
                "ok": True,
                "repository": "auto-cut-lite",
                "primary_branch": "main",
                "workflow_mode": "lite",
            }.items()
        )
        or not identity.get("branch")
    ):
        raise SlicingError("Upstream must be a verified non-detached Auto-Cut Lite source checkout")
    lock = read_json(REPOSITORY_ROOT / "auto-cut-dependency.lock.json")
    if lock.get("mode") != "source_checkout" or lock.get("repository") != "auto-cut-lite":
        raise SlicingError("Only the pinned source-checkout integration is supported")
    revision = _run(["git", "rev-parse", "HEAD"], root)
    if revision.returncode or revision.stdout.strip() != lock.get("revision"):
        raise SlicingError("Upstream revision differs from auto-cut-dependency.lock.json")
    tracked = _run(["git", "diff", "--quiet", "HEAD", "--", "."], root)
    if tracked.returncode:
        raise SlicingError("Upstream has tracked changes; use a clean pinned checkout")
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    if untracked.returncode:
        raise SlicingError("Cannot inspect upstream unmanaged source files")
    managed_roots = {"scripts", "audio_sound", "presets", "plugins", "skills", "tests"}
    code_suffixes = {".py", ".pyw", ".pyd", ".dll", ".so", ".toml", ".json"}
    for relative in filter(None, untracked.stdout.split("\0")):
        path = Path(relative)
        if path.parts[0] in managed_roots or path.suffix.casefold() in code_suffixes:
            raise SlicingError("Upstream contains unmanaged runtime/source files")
    # Ignored task caches remain allowed, but ignored executable code inside runtime
    # directories would affect Python import resolution and must be rejected.
    ignored = _run(
        [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            "scripts",
            "audio_sound",
        ],
        root,
    )
    if ignored.returncode:
        raise SlicingError("Cannot inspect ignored upstream runtime files")
    for relative in filter(None, ignored.stdout.split("\0")):
        path = Path(relative)
        if path.suffix.casefold() in {".py", ".pyw", ".pyd", ".dll", ".so"} or (
            path.suffix.casefold() in {".pyc", ".pyo"} and "__pycache__" not in path.parts
        ):
            raise SlicingError("Upstream contains ignored unmanaged executable source")
    return {
        "status": "pass",
        "repository": "auto-cut-lite",
        "mode": "source_checkout",
        "revision": revision.stdout.strip(),
        "branch": identity["branch"],
        "upstream_root": str(root),
        "upstream_python": str(python),
    }


def run_worker(config, action, payload_path, result_path) -> dict:
    receipt = validate_upstream(config)
    worker = REPOSITORY_ROOT / "auto_cut_slicing" / "draft_worker.py"
    result_path = Path(result_path).resolve()
    if result_path.exists():
        raise SlicingError("Worker result destination already exists")
    # Ignore ambient PYTHONPATH/user site customization and upstream bytecode
    # caches. The isolated worker inserts only the two explicitly pinned roots.
    command = [
        receipt["upstream_python"],
        "-I",
        "-B",
        "-X",
        f"pycache_prefix={result_path.parent / 'worker-bytecode'}",
        str(worker),
        "--upstream-root",
        receipt["upstream_root"],
        "--action",
        action,
        "--payload",
        str(Path(payload_path).resolve()),
        "--result",
        str(result_path),
    ]
    completed = _run(command, receipt["upstream_root"])
    if not result_path.is_file():
        raise SlicingError(
            "Dependency worker failed before producing a result; verify its interpreter dependencies"
        )
    result = read_json(result_path)
    if completed.returncode or not result.get("ok"):
        raise SlicingError(result.get("error", "Dependency worker failed"))
    return result["data"]


def transcribe_source(source, output, *, config=None, env_path=None) -> dict:
    import tempfile

    from .io import file_sha256, write_json, write_new_json

    source_path = Path(source["path"] if isinstance(source, dict) else source).resolve(strict=True)
    expected_hash = source.get("sha256") if isinstance(source, dict) else None
    source_hash = file_sha256(source_path)
    if expected_hash and expected_hash != source_hash:
        raise SlicingError("Source changed before transcription")
    destination = Path(output).resolve()
    if destination.exists():
        raise SlicingError("Transcription output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".transcribe-", dir=destination.parent) as temporary:
        payload_path = write_json(
            Path(temporary) / "payload.json",
            {
                "source_path": str(source_path),
                "source_sha256": source_hash,
                "env_path": str(Path(env_path).resolve()) if env_path else None,
                "work_dir": temporary,
            },
        )
        result = run_worker(
            config or load_config(), "transcribe", payload_path, Path(temporary) / "result.json"
        )
    if file_sha256(source_path) != source_hash:
        raise SlicingError("Source changed during transcription")
    write_new_json(destination, result)
    return result
