"""Refresh the single package-local skill without touching global skill discovery."""

import shutil
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "skills" / "auto-cut-slicing"
    destination = root / ".codex" / "skills" / source.name
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    print(f"Installed repository-local skill: {destination}")


if __name__ == "__main__":
    main()
