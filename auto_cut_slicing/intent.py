"""Validate an AI-written edit intent and compile it into a maintained slicing job.

Rules V01-V14 are defined in docs/intent-schema.md. Every time value is recomputed from its
declared source; the compiled job is planned by the existing planner and checked against the
selected word indices before it is published.
"""

import hashlib
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from .alignment import find_phrase_matches, normalized_text, parse_time_us
from .boundaries import _sentences
from .context import EditContext
from .errors import SlicingError
from .evidence import load_manifest, manifest_path
from .io import file_sha256, json_sha256, read_json, write_json, write_new_json
from .schema import schema_errors

BLOCKING = {"unsupported", "needs_evidence", "ambiguous"}
CUE_RADIUS_US = 3_000_000
WORD_RADIUS_US = 30_000_000
VISUAL_TOLERANCE_US = 500_000
_DIGITS = str.maketrans("０１２３４５６７８９：．", "0123456789:.")
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


class IntentError(SlicingError):
    def __init__(self, problems: list[dict[str, Any]]):
        self.problems = problems
        head = "; ".join(f"{p['rule']} {p['code']}: {p['message']}" for p in problems[:6])
        more = f"; ... {len(problems) - 6} more" if len(problems) > 6 else ""
        super().__init__(f"Edit intent is not valid: {head}{more}")


def _problem(rule, code, message, path="$"):
    return {"rule": rule, "code": code, "message": message, "path": path}


def _cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN and ch != "十" for ch in text):
        return None
    if "十" not in text:
        return _CN[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_CN[tens] if tens else 1) * 10 + (_CN[ones] if ones else 0)


def parse_user_time(text: str) -> int:
    """Parse a time the user wrote: 4:56, 4：56, 1:02:03.5, 4分34(秒), 第6秒, 第六秒, 6s."""
    value = text.translate(_DIGITS).strip()
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?", value):
        return parse_time_us(value, field="user time")
    match = re.fullmatch(r"(\d+)分(\d{1,2}(?:\.\d+)?)秒?", value)
    if match:
        return parse_time_us(f"{match[1]}:{match[2].zfill(2) if '.' not in match[2] else match[2]}",
                             field="user time")
    match = re.fullmatch(r"第?([0-9一二两三四五六七八九十]+(?:\.\d+)?)\s*(?:秒|s|S)", value)
    if match:
        number = match[1]
        if re.fullmatch(r"\d+(?:\.\d+)?", number):
            return parse_time_us(number if "." in number else int(number), field="user time")
        converted = _cn_number(number)
        if converted is not None:
            return converted * 1_000_000
    raise SlicingError(f"Cannot parse a user-written time from {text!r}")


def parse_ordinal(text: str) -> tuple[int, bool]:
    """Return (ordinal, from_end) for 第二遍 / 第2次 / 最后一次 / 倒数第二."""
    value = text.translate(_DIGITS)
    match = re.search(r"倒数第([0-9一二两三四五六七八九十]+)", value)
    if match and _cn_number(match[1]):
        return _cn_number(match[1]), True
    if re.search(r"最后|末", value):
        return 1, True
    match = re.search(r"第([0-9一二两三四五六七八九十]+)", value)
    if match and _cn_number(match[1]):
        return _cn_number(match[1]), False
    raise SlicingError(f"Cannot parse an ordinal from {text!r}")


def _sec(us: int) -> int | float:
    return us // 1_000_000 if us % 1_000_000 == 0 else us / 1_000_000


class _Checker:
    def __init__(self, intent: dict[str, Any], context: EditContext):
        self.intent = intent
        self.context = context
        self.problems: list[dict[str, Any]] = []
        self.raw = {row["instruction_id"]: row["raw_text"] for row in intent["instructions"]}
        self.manifest = None
        self.user_anchors: set[int] = set()

    def add(self, rule, code, message, path="$"):
        self.problems.append(_problem(rule, code, message, path))

    # V02-V04 ---------------------------------------------------------------
    def bindings(self):
        for index, row in enumerate(self.intent["instructions"]):
            path = f"$.instructions[{index}]"
            source = self.context.instructions.get(row["instruction_id"])
            if source is None:
                self.add("V02", "INTENT_CONFLICT", "instruction is not in the context", path)
                continue
            if row["raw_text"] != source["raw_text"]:
                self.add("V02", "INTENT_CONFLICT", "raw_text differs from the original feedback", path)
            digest = hashlib.sha256(row["raw_text"].encode("utf-8")).hexdigest()
            if row["raw_text_sha256"] != digest:
                self.add("V02", "INTENT_CONFLICT", "raw_text_sha256 does not match raw_text", path)
        missing = set(self.context.instructions) - set(self.raw)
        if missing:
            self.add("V02", "INTENT_UNCOVERED_TEXT", f"instructions missing from intent: {sorted(missing)}")
        binding = self.intent["bindings"]
        if binding["timeline_basis"] != self.context.timeline_basis:
            self.add("V03", "INTENT_EVIDENCE_STALE", "timeline_basis differs from the context")
        for index, row in enumerate(binding["sources"]):
            path = f"$.bindings.sources[{index}]"
            try:
                source = self.context.source(row["source_id"])
            except SlicingError as error:
                self.add("V03", "INTENT_EVIDENCE_STALE", str(error), path)
                continue
            alignment = source["alignment"]
            actual = {"sha256": source["sha256"], "duration_us": source["duration_us"],
                      "alignment_sha256": alignment["sha256"] if alignment else None}
            for key, value in actual.items():
                if row[key] != value:
                    self.add("V03", "INTENT_EVIDENCE_STALE", f"{key} differs from the current file", path)
        manifest_hash = binding["evidence_manifest_sha256"]
        path = manifest_path(self.context)
        if manifest_hash is not None:
            if not path.is_file():
                self.add("V03", "INTENT_EVIDENCE_MISSING", "evidence manifest file is missing")
            elif file_sha256(path) != manifest_hash:
                self.add("V03", "INTENT_EVIDENCE_STALE", "evidence manifest hash differs")
            else:
                self.manifest = load_manifest(self.context)

    def bound_source(self, source_id, path):
        if source_id not in {row["source_id"] for row in self.intent["bindings"]["sources"]}:
            self.add("V03", "INTENT_EVIDENCE_STALE", f"source {source_id} is not bound", path)
            return None
        try:
            return self.context.source(source_id)
        except SlicingError as error:
            self.add("V03", "INTENT_EVIDENCE_MISSING", str(error), path)
            return None

    def evidence(self, evidence_id, path, *, kind=None, source=None):
        if self.manifest is None:
            self.add("V04", "INTENT_EVIDENCE_MISSING", f"evidence {evidence_id} cited without a manifest", path)
            return None
        row = next((item for item in self.manifest["entries"] if item["evidence_id"] == evidence_id), None)
        if row is None:
            self.add("V04", "INTENT_EVIDENCE_MISSING", f"evidence {evidence_id} is not registered", path)
            return None
        if kind and row["kind"] not in kind:
            self.add("V04", "INTENT_EVIDENCE_MISSING", f"evidence {evidence_id} is {row['kind']}, need {kind}", path)
        if source is not None and row["source_sha256"] != source["sha256"]:
            self.add("V04", "INTENT_EVIDENCE_STALE", f"evidence {evidence_id} belongs to other media", path)
        return row

    # V05-V06 ---------------------------------------------------------------
    def span_text(self, span, path, rule="V05"):
        raw = self.raw.get(span["instruction_id"])
        if raw is None or not 0 <= span["start"] < span["end"] <= len(raw):
            self.add(rule, "INTENT_CONFLICT", "span is outside its instruction", path)
            return None
        return raw[span["start"]:span["end"]]

    def coverage(self, intent_ids: set[str]):
        covered = {key: set() for key in self.raw}
        operation_spans = []
        blocking = False
        for index, row in enumerate(self.intent["coverage"]):
            path = f"$.coverage[{index}]"
            text = self.span_text(row["span"], path)
            if text is None:
                continue
            if text != row["quote"]:
                self.add("V05", "INTENT_CONFLICT", "quote differs from the instruction text", path)
            covered[row["span"]["instruction_id"]].update(range(row["span"]["start"], row["span"]["end"]))
            if row["disposition"] in BLOCKING:
                blocking = True
            if row["disposition"] == "operation":
                operation_spans.append(row)
            for intent_id in row["intent_ids"]:
                if intent_id not in intent_ids:
                    self.add("V05", "INTENT_CONFLICT", f"unknown intent_id {intent_id}", path)
        for key, raw in self.raw.items():
            missing = [ch for i, ch in enumerate(raw) if i not in covered[key] and not ch.isspace()
                       and not unicodedata.category(ch).startswith(("P", "C"))]
            if missing:
                self.add("V05", "INTENT_UNCOVERED_TEXT",
                         f"instruction {key} has uncovered text: {''.join(missing)[:40]!r}")
        status = self.intent["status"]
        if blocking and status != "blocked":
            self.add("V06", "INTENT_CONFLICT", "blocking dispositions require status blocked")

    # V07 -----------------------------------------------------------------------
    def time(self, ref, path, source_id, source):
        kind = ref["ref"]
        value = ref["value_us"]
        if kind == "user_anchor":
            text = self.span_text(ref["span"], path, "V07")
            if text is None:
                return None
            try:
                parsed = parse_user_time(text)
            except SlicingError as error:
                self.add("V07", "INTENT_TIME_UNSOURCED", str(error), path)
                return None
            if parsed != value:
                self.add("V07", "INTENT_TIME_UNSOURCED", f"{text!r} is {parsed}us, not {value}us", path)
                return None
            self.user_anchors.add(value)
            return value
        if kind == "source_edge":
            if ref["source_id"] != source_id:
                self.add("V07", "INTENT_TIME_UNSOURCED", "source_edge refers to another source", path)
                return None
            expected = 0 if ref["edge"] == "start" else source["duration_us"]
            if value != expected:
                self.add("V07", "INTENT_TIME_UNSOURCED", f"source {ref['edge']} is {expected}us", path)
                return None
            return value
        if kind == "word_edge":
            alignment = source["alignment"]
            if ref["source_id"] != source_id or alignment is None or ref["alignment_sha256"] != alignment["sha256"]:
                self.add("V07", "INTENT_EVIDENCE_STALE", "word_edge is not bound to this source alignment", path)
                return None
            words = alignment["words"]
            if not 0 <= ref["index"] < len(words) or normalized_text(words[ref["index"]]["text"]) != normalized_text(ref["text"]):
                self.add("V07", "INTENT_TIME_UNSOURCED", "word index/text does not match the alignment", path)
                return None
            expected = words[ref["index"]][f"{ref['edge']}_us"]
            if value != expected:
                self.add("V07", "INTENT_TIME_UNSOURCED", f"word {ref['edge']} is {expected}us", path)
                return None
            if "claim" in ref:
                side = ref["claim"].split("_")[1]
                try:
                    sentences = _sentences(alignment, source["duration_us"])
                except SlicingError as error:
                    self.add("V07", "INTENT_EVIDENCE_MISSING", f"sentence evidence: {error}", path)
                    return None
                position = -1 if side == "end" else 0
                if not any(row["word_indices"][position] == ref["index"] for row in sentences):
                    self.add("V07", "INTENT_TIME_UNSOURCED", f"word is not a sentence {side}", path)
                    return None
            return value
        if kind == "frame":
            if ref["source_id"] != source_id or ref["source_sha256"] != source["sha256"]:
                self.add("V07", "INTENT_EVIDENCE_STALE", "frame refers to other media", path)
                return None
            row = self.evidence(ref["evidence_id"], path, kind={"frames"}, source=source)
            if row is not None and value not in row.get("frame_times_us", []):
                self.add("V07", "INTENT_TIME_UNSOURCED", "time is not a decoded frame in that evidence", path)
                return None
            return value if row is not None else None
        self.add("V07", "INTENT_EVIDENCE_MISSING",
                 f"{kind} needs audio-gap evidence, which this version does not produce", path)
        return None

    # V08 -----------------------------------------------------------------------
    def word_span(self, span, path, source_id, source, *, purpose, allowed_scope):
        alignment = source["alignment"]
        if span["source_id"] != source_id or alignment is None or span["alignment_sha256"] != alignment["sha256"]:
            self.add("V08", "INTENT_EVIDENCE_STALE", "phrase is not bound to this source alignment", path)
            return None
        words = alignment["words"]
        first, last = span["first_index"], span["last_index"]
        if not 0 <= first <= last < len(words):
            self.add("V08", "INTENT_NO_MATCH", "word indices are outside the alignment", path)
            return None
        if normalized_text("".join(w["text"] for w in words[first:last + 1])) != normalized_text(span["text"]):
            self.add("V08", "INTENT_NO_MATCH", "text differs from the selected words", path)
            return None
        sel = span["selection"]
        if sel["scope_start_us"] not in allowed_scope or sel["scope_end_us"] not in allowed_scope:
            self.add("V08", "INTENT_AMBIGUOUS_MATCH",
                     "scope edges must be clip edges, source edges or user-written times", path)
        if sel["evidence_id"] is not None:
            self.evidence(sel["evidence_id"], path, kind={"phrase_search"}, source=source)
        found = find_phrase_matches(alignment, span["text"], clip_start_us=sel["scope_start_us"],
                                    clip_end_us=sel["scope_end_us"])
        actual = sorted((m["word_indices"][0], m["word_indices"][-1], m["source_start_us"], m["source_end_us"]) for m in found)
        declared = sorted((c["first_index"], c["last_index"], c["start_us"], c["end_us"]) for c in sel["candidates"])
        if actual != declared:
            self.add("V08", "INTENT_AMBIGUOUS_MATCH", f"candidates differ from the alignment: {len(actual)} found", path)
            return None
        chosen = next((row for row in actual if row[0] == first and row[1] == last), None)
        if chosen is None:
            self.add("V08", "INTENT_NO_MATCH", "selected words are not one of the candidates", path)
            return None
        method = sel["method"]
        if method == "unique_in_scope" and len(actual) != 1:
            self.add("V08", "INTENT_AMBIGUOUS_MATCH", "scope contains more than one candidate", path)
        elif method == "user_anchor":
            anchor = self.time(sel["anchor"], f"{path}.selection.anchor", source_id, source)
            limit = CUE_RADIUS_US if purpose == "cue" else WORD_RADIUS_US
            if sel["radius_us"] > limit:
                self.add("V08", "INTENT_AMBIGUOUS_MATCH", f"radius exceeds {limit}us", path)
            if anchor is not None:
                near = [row for row in actual if abs(row[2] - anchor) <= sel["radius_us"]]
                if near != [chosen]:
                    self.add("V08", "INTENT_AMBIGUOUS_MATCH",
                             f"{len(near)} candidates lie within the anchor radius", path)
        elif method == "user_ordinal":
            text = self.span_text(sel["ordinal_span"], path, "V08")
            if text is not None:
                try:
                    ordinal, from_end = parse_ordinal(text)
                except SlicingError as error:
                    self.add("V08", "INTENT_AMBIGUOUS_MATCH", str(error), path)
                else:
                    if ordinal != sel["ordinal"] or from_end != bool(sel.get("ordinal_from_end", False)):
                        self.add("V08", "INTENT_AMBIGUOUS_MATCH", f"{text!r} is not that ordinal", path)
                    elif ordinal > len(actual) or (actual[-ordinal] if from_end else actual[ordinal - 1]) != chosen:
                        self.add("V08", "INTENT_AMBIGUOUS_MATCH", "ordinal selects a different candidate", path)
        elif method == "user_context":
            text = self.span_text(sel["context_span"], path, "V08")
            if text is not None and text != sel["context_text"]:
                self.add("V08", "INTENT_AMBIGUOUS_MATCH", "context_text differs from the instruction", path)
            elif text is not None:
                contexts = find_phrase_matches(alignment, text, clip_start_us=sel["scope_start_us"],
                                               clip_end_us=sel["scope_end_us"])
                if len(contexts) != 1 or not (contexts[0]["word_indices"][0] <= first
                                              and last <= contexts[0]["word_indices"][-1]):
                    self.add("V08", "INTENT_AMBIGUOUS_MATCH",
                             "user context must match once and contain the selection", path)
        return {"first": first, "last": last, "start_us": chosen[2], "end_us": chosen[3], "text": span["text"]}


def _sentence_edge(source, value, side):
    alignment = source["alignment"]
    if alignment is None:
        raise SlicingError("sentence mode requires source-bound ASR")
    for row in _sentences(alignment, source["duration_us"]):
        if row["sentence_start_us"] < value < row["sentence_end_us"]:
            return row[f"sentence_{side}_us"], row
    return value, None


def _narrow_radius(alignment, text, rough_us, chosen_start, scope, cap):
    """Largest planner search radius around rough_us that reaches only the chosen candidate."""
    matches = find_phrase_matches(alignment, text, clip_start_us=scope[0], clip_end_us=scope[1])
    if sum(1 for m in matches if m["source_start_us"] == chosen_start) != 1:
        raise SlicingError(f"selected {text!r} is not uniquely reachable by the planner search")
    others = [abs(m["source_start_us"] - rough_us) for m in matches if m["source_start_us"] != chosen_start]
    radius = min([cap, *[distance - 1 for distance in others]])
    if radius < abs(chosen_start - rough_us):
        raise SlicingError(f"cannot narrow the planner search to the selected {text!r}")
    return radius


def check_intent(intent_path, context: EditContext) -> dict[str, Any]:
    """Run V01-V13, resolve clip/operation values and return the compile plan or problems."""
    intent = read_json(intent_path)
    found = schema_errors(intent, "edit-intent.v1.schema.json")
    if found:
        return {"intent": intent, "problems": [_problem("V01", "INTENT_CONFLICT", msg) for msg in found[:20]],
                "resolved": None}
    checker = _Checker(intent, context)
    checker.bindings()
    if checker.problems:
        return {"intent": intent, "problems": checker.problems, "resolved": None}

    covers, ids = {}, []
    for output in intent["outputs"]:
        for clip in output["clips"]:
            ids.append(clip["intent_id"])
            covers[clip["intent_id"]] = clip["covers"]
            for op in clip["operations"]:
                ids.append(op["intent_id"])
                covers[f"op:{op['intent_id']}"] = op["covers"]
    duplicates = sorted({x for x in ids if ids.count(x) > 1})
    if duplicates:
        checker.add("V13", "INTENT_CONFLICT", f"duplicate intent IDs: {duplicates}")
    checker.coverage(set(ids))
    for key, spans in covers.items():
        intent_id = key[3:] if key.startswith("op:") else key
        if key.startswith("op:") and len({span["instruction_id"] for span in spans}) > 1:
            checker.add("V05", "INTENT_CONFLICT", f"{intent_id} covers more than one instruction")
        for span in spans:
            if not any(row["disposition"] == "operation" and row["span"]["instruction_id"] == span["instruction_id"]
                       and row["span"]["start"] <= span["start"] and span["end"] <= row["span"]["end"]
                       and intent_id in row["intent_ids"] for row in intent["coverage"]):
                checker.add("V05", "INTENT_CONFLICT", f"{intent_id} covers text no operation span assigns to it")

    def scan(node, path):
        if isinstance(node, dict):
            if node.get("ref") == "user_anchor":
                checker.time(node, path, None, None)
            for key, value in node.items():
                scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan(value, f"{path}[{index}]")

    scan(intent["outputs"], "$.outputs")
    if checker.problems:
        return {"intent": intent, "problems": checker.problems, "resolved": None}
    resolved_outputs = []
    for o_index, output in enumerate(intent["outputs"]):
        resolved_clips = []
        for c_index, clip in enumerate(output["clips"]):
            path = f"$.outputs[{o_index}].clips[{c_index}]"
            source_id = clip["source_id"]
            source = checker.bound_source(source_id, path)
            if source is None:
                continue
            item = {"intent_id": clip["intent_id"], "source_id": source_id, "operations": [],
                    "must_keep": [row["text"] for row in clip["must_keep"]], "notes": {}}
            start = clip["start"]
            if start["mode"] == "cue":
                end_value = checker.time(clip["end"]["at"], f"{path}.end.at", source_id, source)
                if end_value is None:
                    continue
                anchors = {0, source["duration_us"], end_value, *checker.user_anchors}
                chosen = checker.word_span(start["phrase"], f"{path}.start.phrase", source_id, source,
                                           purpose="cue", allowed_scope=anchors)
                if chosen is None:
                    continue
                item["cue"] = chosen
                start_us = chosen["start_us"]
                if "visual_anchor" in start:
                    visual = checker.time(start["visual_anchor"], f"{path}.start.visual_anchor", source_id, source)
                    if visual is not None:
                        if abs(visual - start_us) > VISUAL_TOLERANCE_US:
                            checker.add("V10", "INTENT_CONFLICT", "speech cue is more than 500ms from the frame", path)
                        item["visual_anchor_us"] = visual
            else:
                value = checker.time(start["at"], f"{path}.start.at", source_id, source)
                if value is None:
                    continue
                start_us = value
                if start["mode"] == "sentence":
                    try:
                        start_us, row = _sentence_edge(source, value, "start")
                    except SlicingError as error:
                        checker.add("V07", "INTENT_EVIDENCE_MISSING", str(error), path)
                        continue
                    item["notes"]["start_sentence"] = row and row["sentence_text"]
                end_value = checker.time(clip["end"]["at"], f"{path}.end.at", source_id, source)
                if end_value is None:
                    continue
            end_us = end_value
            if clip["end"]["mode"] == "sentence":
                try:
                    end_us, row = _sentence_edge(source, end_value, "end")
                except SlicingError as error:
                    checker.add("V07", "INTENT_EVIDENCE_MISSING", str(error), path)
                    continue
                item["notes"]["end_sentence"] = row and row["sentence_text"]
            if not 0 <= start_us < end_us <= source["duration_us"]:
                checker.add("V09", "INTENT_CONFLICT", "clip range is empty or outside the source", path)
                continue
            item.update(start_us=start_us, end_us=end_us)
            scope = {0, source["duration_us"], start_us, end_us, end_value, *checker.user_anchors}
            spans = {"delete": [], "mute": [], "freeze": []}
            for p_index, op in enumerate(clip["operations"]):
                op_path = f"{path}.operations[{p_index}]"
                instruction_id = op["covers"][0]["instruction_id"]
                row = {"intent_id": op["intent_id"], "kind": op["kind"],
                       "source_text": checker.raw.get(instruction_id, "")}
                if op["kind"] in ("delete_phrase", "mute_phrase"):
                    chosen = checker.word_span(op["phrase"], f"{op_path}.phrase", source_id, source,
                                               purpose="word", allowed_scope=scope)
                    if chosen is None:
                        continue
                    row["phrase"] = chosen
                    a, b = chosen["start_us"], chosen["end_us"]
                    group = "delete" if op["kind"] == "delete_phrase" else "mute"
                elif op["kind"] == "delete_range":
                    a = checker.time(op["start"], f"{op_path}.start", source_id, source)
                    b = checker.time(op["end"], f"{op_path}.end", source_id, source)
                    if a is None or b is None:
                        continue
                    group = "delete"
                else:
                    a = checker.time(op["start"], f"{op_path}.start", source_id, source)
                    b = checker.time(op["end"], f"{op_path}.end", source_id, source)
                    frame = checker.time(op["frame"], f"{op_path}.frame", source_id, source)
                    for evidence_id in op["visual_review"]["evidence_ids"]:
                        checker.evidence(evidence_id, f"{op_path}.visual_review")
                    if a is None or b is None or frame is None:
                        continue
                    row["frame_us"] = frame
                    row["visual_review"] = op["visual_review"]["status"]
                    group = "freeze"
                if not start_us <= a < b <= end_us:
                    checker.add("V09", "INTENT_CONFLICT", "operation is empty or outside its clip", op_path)
                    continue
                if any(a < y and x < b for x, y in spans[group]):
                    checker.add("V09", "INTENT_CONFLICT", f"overlapping {group} operations", op_path)
                spans[group].append((a, b))
                row.update(start_us=a, end_us=b)
                item["operations"].append(row)
            alignment = source["alignment"]
            for k_index, keep in enumerate(clip["must_keep"]):
                k_path = f"{path}.must_keep[{k_index}]"
                origin = keep["origin"]
                if origin["kind"] == "instruction_span":
                    text = checker.span_text(origin["span"], k_path, "V12")
                    if text is not None and normalized_text(keep["text"]) not in normalized_text(text):
                        checker.add("V12", "INTENT_CONFLICT", "must_keep text is not in its span", k_path)
                elif keep["text"] not in context.instructions.get(origin["instruction_id"], {}).get("must_keep", []):
                    checker.add("V12", "INTENT_CONFLICT", "must_keep is not supplied by that instruction", k_path)
                if alignment is None:
                    checker.add("V12", "INTENT_EVIDENCE_MISSING", "must_keep needs source-bound ASR", k_path)
                    continue
                hits = find_phrase_matches(alignment, keep["text"], clip_start_us=start_us, clip_end_us=end_us)
                if not hits:
                    checker.add("V12", "INTENT_NO_MATCH", f"must_keep {keep['text']!r} is not in the clip", k_path)
                for hit in hits:
                    for x, y in spans["delete"] + spans["mute"]:
                        if hit["source_start_us"] < y and x < hit["source_end_us"]:
                            checker.add("V12", "INTENT_CONFLICT", f"an operation removes must_keep {keep['text']!r}", k_path)
            resolved_clips.append(item)
        resolved_outputs.append({"output_id": output["output_id"], "name": output.get("name"), "clips": resolved_clips})
    return {"intent": intent, "problems": checker.problems, "resolved": resolved_outputs}


def validate(intent_path, context: EditContext) -> dict[str, Any]:
    result = check_intent(intent_path, context)
    intent = result["intent"]
    status = intent.get("status") if isinstance(intent, dict) else None
    return {"ok": not result["problems"], "status": status,
            "compilable": not result["problems"] and status == "proposed",
            "intent_sha256": file_sha256(intent_path), "problems": result["problems"],
            "blockers": intent.get("blockers", []) if isinstance(intent, dict) else []}


def _job(context: EditContext, resolved) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mappings = []
    used = sorted({clip["source_id"] for output in resolved for clip in output["clips"]})
    sources = []
    for source_id in used:
        row = {"id": source_id, "path": str(context.source_path(source_id))}
        asr = context.asr_path(source_id)
        if asr is not None:
            row["asr"] = str(asr)
        sources.append(row)
    outputs = []
    for output in resolved:
        clips = []
        for clip in output["clips"]:
            source = context.source(clip["source_id"])
            alignment = source["alignment"]
            row = {"id": clip["intent_id"], "source": clip["source_id"], "boundary_policy": "exact",
                   "start": _sec(clip["start_us"]), "end": _sec(clip["end_us"])}
            mapping = {"intent_id": clip["intent_id"], "kind": "clip", "start_us": clip["start_us"],
                       "end_us": clip["end_us"], **clip["notes"]}
            if "cue" in clip:
                cue = clip["cue"]
                rough = clip.get("visual_anchor_us", cue["start_us"])
                cap = VISUAL_TOLERANCE_US if "visual_anchor_us" in clip else CUE_RADIUS_US
                radius = _narrow_radius(alignment, cue["text"], rough, cue["start_us"], (0, clip["end_us"]), cap)
                row["start_cue"] = {"text": cue["text"], "search_radius": _sec(radius)}
                if "visual_anchor_us" in clip:
                    row["start_cue"]["visual_anchor"] = _sec(clip["visual_anchor_us"])
                mapping.update(cue_word_indices=[cue["first"], cue["last"]], cue_search_radius_us=radius)
            mappings.append(mapping)
            for op in clip["operations"]:
                op_map = {"intent_id": op["intent_id"], "kind": op["kind"], "clip_id": clip["intent_id"],
                          "start_us": op["start_us"], "end_us": op["end_us"]}
                if op["kind"] in ("delete_phrase", "mute_phrase"):
                    phrase = op["phrase"]
                    radius = _narrow_radius(alignment, phrase["text"], phrase["start_us"], phrase["start_us"],
                                            (clip["start_us"], clip["end_us"]), WORD_RADIUS_US)
                    item = {"id": op["intent_id"], "kind": "word" if op["kind"] == "delete_phrase" else "mute_word",
                            "text": phrase["text"], "rough_time": _sec(phrase["start_us"]),
                            "search_radius": _sec(radius), "must_keep": clip["must_keep"],
                            "source_text": op["source_text"]}
                    op_map.update(word_indices=[phrase["first"], phrase["last"]], search_radius_us=radius)
                    row.setdefault("deletions" if op["kind"] == "delete_phrase" else "edits", []).append(item)
                elif op["kind"] == "delete_range":
                    row.setdefault("deletions", []).append({
                        "id": op["intent_id"], "kind": "range", "start": _sec(op["start_us"]),
                        "end": _sec(op["end_us"]), "source_text": op["source_text"]})
                else:
                    row.setdefault("edits", []).append({
                        "id": op["intent_id"], "kind": "freeze_frame", "start": _sec(op["start_us"]),
                        "end": _sec(op["end_us"]), "frame_time": _sec(op["frame_us"]),
                        "source_text": op["source_text"]})
                    op_map.update(frame_us=op["frame_us"], visual_review=op["visual_review"])
                mappings.append(op_map)
            clips.append(row)
        outputs.append({"id": output["output_id"], "name": output["name"] or output["output_id"], "clips": clips})
    return {"schema_version": 1, "name": "edit-intent", "sources": sources, "outputs": outputs}, mappings


def _verify_plan(plan, mappings) -> list[dict[str, Any]]:
    """V14: the planner must resolve exactly the selected words and boundaries."""
    problems = []
    cues, edits, effects = {}, {}, {}
    for output in plan["outputs"]:
        cues.update({row["clip_id"]: row for row in output.get("start_cues", [])})
        edits.update({row["item_id"]: row for row in output.get("edits", [])})
        effects.update({row["item_id"]: row for row in output.get("effects", [])})
    for row in mappings:
        if row["kind"] == "clip" and "cue_word_indices" in row:
            cue = cues.get(row["intent_id"])
            if cue is None or [cue["word_indices"][0], cue["word_indices"][-1]] != row["cue_word_indices"]:
                problems.append(_problem("V14", "INTENT_CONFLICT", f"planner cue for {row['intent_id']} differs"))
        elif row["kind"] in ("delete_phrase", "delete_range"):
            hit = edits.get(f"{row['clip_id']}/{row['intent_id']}")
            if hit is None or (hit["source_start_us"], hit["source_end_us"]) != (row["start_us"], row["end_us"]):
                problems.append(_problem("V14", "INTENT_CONFLICT", f"planner deletion {row['intent_id']} differs"))
        elif row["kind"] == "mute_phrase":
            hit = effects.get(f"{row['clip_id']}/{row['intent_id']}")
            if hit is None or [hit["word_indices"][0], hit["word_indices"][-1]] != row["word_indices"]:
                problems.append(_problem("V14", "INTENT_CONFLICT", f"planner mute {row['intent_id']} differs"))
        elif row["kind"] == "freeze_cover":
            if f"{row['clip_id']}/{row['intent_id']}" not in effects:
                problems.append(_problem("V14", "INTENT_CONFLICT", f"planner freeze {row['intent_id']} is missing"))
    return problems


def compile_intent(intent_path, context: EditContext, job_path, receipt_path, plan_path=None) -> dict[str, Any]:
    from .planner import compile_job

    for target in (job_path, receipt_path, plan_path):
        if target is not None and Path(target).exists():
            raise SlicingError(f"Refusing to overwrite: {target}")
    result = check_intent(intent_path, context)
    if result["problems"]:
        raise IntentError(result["problems"])
    intent = result["intent"]
    if intent["status"] != "proposed":
        codes = ", ".join(row["code"] for row in intent["blockers"])
        raise SlicingError(f"Edit intent is blocked and cannot be compiled: {codes}")
    try:
        job, mappings = _job(context, result["resolved"])
    except SlicingError as error:
        raise IntentError([_problem("V14", "INTENT_AMBIGUOUS_MATCH", str(error))]) from error
    target_dir = Path(job_path).resolve().parent
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".intent-compile-", dir=target_dir) as temporary:
        staged = write_json(Path(temporary) / "job.json", job)
        by_path = {
            context.source_path(sid): {k: v for k, v in context.source(sid).items() if k != "alignment"}
            for sid in context.source_ids()
        }
        plan = compile_job(staged, describe=lambda path: by_path[Path(path).resolve()])
    problems = _verify_plan(plan, mappings)
    if problems:
        raise IntentError(problems)
    write_new_json(job_path, job)
    if plan_path is not None:
        write_new_json(plan_path, plan)
    receipt = {
        "schema": "auto-cut-slicing/intent-compile-receipt", "schema_version": 1,
        "intent_sha256": file_sha256(intent_path),
        "context_sha256": file_sha256(context.path),
        "evidence_manifest_sha256": intent["bindings"]["evidence_manifest_sha256"],
        "job_sha256": json_sha256(job), "plan_sha256": plan["plan_sha256"],
        "rules_checked": [f"V{n:02d}" for n in range(1, 15)],
        "mappings": mappings,
        "outputs": [{"id": row["id"], "duration_us": row["duration_us"], "segments": len(row["segments"])}
                    for row in plan["outputs"]],
    }
    write_new_json(receipt_path, receipt)
    return {"ok": True, "job_path": str(Path(job_path).resolve()), "receipt": receipt}
