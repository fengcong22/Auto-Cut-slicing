"""Local context shared by the evidence, intent and QA commands."""

from pathlib import Path
from typing import Any

from .alignment import load_alignment
from .errors import SlicingError
from .io import read_json
from .schema import require_schema


class EditContext:
    """Resolve sources, word alignments, raw feedback and the evidence directory."""

    def __init__(self, path: str | Path, *, describe=None):
        self.path = Path(path).resolve()
        self.data = read_json(self.path)
        require_schema(self.data, "intent-context.v1.schema.json", "Intent context")
        self.base = self.path.parent
        self.evidence_dir = (self.base / self.data["evidence_dir"]).resolve()
        self._describe = describe
        self._sources: dict[str, dict[str, Any]] = {}
        ids = [row["source_id"] for row in self.data["sources"]]
        if len(ids) != len(set(ids)):
            raise SlicingError("Intent context source IDs must be unique")
        instruction_ids = [row["instruction_id"] for row in self.data["instructions"]]
        if len(instruction_ids) != len(set(instruction_ids)):
            raise SlicingError("Intent context instruction IDs must be unique")
        self.instructions = {row["instruction_id"]: row for row in self.data["instructions"]}

    @property
    def timeline_basis(self) -> str:
        return self.data["timeline_basis"]

    def source_ids(self) -> list[str]:
        return [row["source_id"] for row in self.data["sources"]]

    def source_row(self, source_id: str) -> dict[str, Any]:
        for row in self.data["sources"]:
            if row["source_id"] == source_id:
                return row
        raise SlicingError(f"Unknown source in intent context: {source_id}")

    def source_path(self, source_id: str) -> Path:
        path = (self.base / self.source_row(source_id)["path"]).resolve()
        if not path.is_file():
            raise SlicingError(f"Source file is missing for {source_id}: {path}")
        return path

    def asr_path(self, source_id: str) -> Path | None:
        value = self.source_row(source_id)["asr"]
        if value is None:
            return None
        path = (self.base / value).resolve()
        if not path.is_file():
            raise SlicingError(f"ASR file is missing for {source_id}: {path}")
        return path

    def source(self, source_id: str) -> dict[str, Any]:
        """Return the source descriptor plus validated alignment (or None), cached per run."""
        if source_id not in self._sources:
            if self._describe is None:
                from .media import describe_source

                self._describe = describe_source
            path = self.source_path(source_id)
            descriptor = dict(self._describe(path))
            descriptor["path"] = str(path)
            asr = self.asr_path(source_id)
            descriptor["alignment"] = load_alignment(asr, descriptor) if asr else None
            self._sources[source_id] = descriptor
        return self._sources[source_id]

    def alignment(self, source_id: str) -> dict[str, Any]:
        alignment = self.source(source_id)["alignment"]
        if alignment is None:
            raise SlicingError(f"Source {source_id} has no source-bound word alignment")
        return alignment
