"""Validate documents against the repository's JSON Schemas without third-party packages.

Only the draft 2020-12 keywords used by ``schemas/*.json`` are implemented. Any other
validation keyword is rejected so a schema change cannot be silently ignored.
"""

import re
from pathlib import Path
from typing import Any

from .errors import SlicingError
from .io import read_json

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
_ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs"}
_KEYWORDS = {
    "type", "const", "enum", "pattern", "minimum", "minLength", "minItems", "maxItems",
    "required", "properties", "additionalProperties", "items", "oneOf", "allOf",
    "if", "then", "$ref",
}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: type(v) is int,
    "number": lambda v: type(v) in (int, float),
    "boolean": lambda v: type(v) is bool,
    "null": lambda v: v is None,
}


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    if not path.is_file():
        raise SlicingError(f"Schema is not available in this checkout: {name}")
    return read_json(path)


class _Validator:
    def __init__(self, root: dict[str, Any]):
        self.root = root

    def resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise SlicingError(f"Unsupported schema reference: {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def errors(self, schema: dict[str, Any], value: Any, path: str) -> list[str]:
        unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
        if unknown:
            raise SlicingError(f"Schema uses unsupported keywords: {sorted(unknown)}")
        if "$ref" in schema:
            found = self.errors(self.resolve(schema["$ref"]), value, path)
            if found:
                return found
        out: list[str] = []
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_TYPES[name](value) for name in types):
                return [f"{path}: expected {'/'.join(types)}"]
        if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
            out.append(f"{path}: must equal {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            out.append(f"{path}: must be one of {schema['enum']}")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                out.append(f"{path}: shorter than {schema['minLength']}")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                out.append(f"{path}: does not match {schema['pattern']}")
        if type(value) in (int, float) and type(value) is not bool:
            if "minimum" in schema and value < schema["minimum"]:
                out.append(f"{path}: below minimum {schema['minimum']}")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                out.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                out.append(f"{path}: more than {schema['maxItems']} items")
            if "items" in schema:
                for index, item in enumerate(value):
                    out.extend(self.errors(schema["items"], item, f"{path}[{index}]"))
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    out.append(f"{path}: missing {key}")
            properties = schema.get("properties", {})
            for key, item in value.items():
                if key in properties:
                    out.extend(self.errors(properties[key], item, f"{path}.{key}"))
                elif schema.get("additionalProperties") is False:
                    out.append(f"{path}: unknown field {key}")
        for sub in schema.get("allOf", []):
            out.extend(self.errors(sub, value, path))
        if "oneOf" in schema:
            results = [self.errors(sub, value, path) for sub in schema["oneOf"]]
            passing = sum(1 for result in results if not result)
            if passing != 1:
                closest = min(results, key=len) if passing == 0 else []
                detail = f" ({closest[0]})" if closest else ""
                out.append(f"{path}: must match exactly one alternative, matched {passing}{detail}")
        if "if" in schema and not self.errors(schema["if"], value, path) and "then" in schema:
            out.extend(self.errors(schema["then"], value, path))
        return out


def schema_errors(document: Any, schema_name: str) -> list[str]:
    schema = load_schema(schema_name)
    return _Validator(schema).errors(schema, document, "$")


def require_schema(document: Any, schema_name: str, label: str) -> None:
    found = schema_errors(document, schema_name)
    if found:
        shown = "; ".join(found[:8]) + (f"; ... {len(found) - 8} more" if len(found) > 8 else "")
        raise SlicingError(f"{label} does not match {schema_name}: {shown}")
