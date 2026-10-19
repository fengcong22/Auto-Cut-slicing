# VM / PMO Offline Editing Contract v2

This contract adds parent-bound review rounds and evidence-backed semantic editing. Schema v1 remains frozen in [the v1 contract](vm-pmo-offline-contract-v1.md). Package version `0.3.0` declares execution contract `vm_pmo_review_video` v2. This declaration is only one activation prerequisite; Taskboard independently verifies Bridge provenance, local policy, package identity, Python runtime, and live Base bindings.

The local entry points remain:

```powershell
python -m auto_cut_slicing.vm_pmo plan --input C:\run\input.json --context C:\run\execution-context.json
python -m auto_cut_slicing.vm_pmo render --input C:\run\input.json --context C:\run\execution-context.json --plan C:\run\plan.json
```

All paths come from trusted local context. The clipper does not contact Feishu or VM services, create approval requests, upload files, or write back PMO fields. Word-level operations call the package's configured local ASR provider. Provider settings and credentials stay in the ignored local package configuration; they are never copied into Taskboard input, result JSON, CLI arguments, or ordinary logs. ASR unavailability blocks a semantic edit.

## Input

The top-level object, package, binding, record, review round, source, profile, and delivery target retain the v1 field shapes. The protocol `schema_version` and context `schema_version` are both `2`; `package.version` is `0.3.0`. Binding `stage_id` is `initial` or `review`.

An initial run has `review_round.parent: null` and `source.timeline_basis: "vm_original"`. A review run uses `source.timeline_basis: "previous_review_video"` and this exact managed parent shape:

```json
{
  "kind": "managed_run",
  "run_id": "prior-run",
  "artifact_id": "prior-artifact",
  "delivery_id": "prior-delivery",
  "sha256": "parent MP4 SHA-256",
  "bytes": 123456,
  "duration_ms": 20000
}
```

The parent run must differ from the current run. Its artifact hash, byte count, and duration must exactly equal the selected source descriptor and the actual local source file. A PMO attachment or a filename alone is not a managed parent; importing an untracked historical video is not part of v2.

One current-round instruction snapshot is accepted per run. Its `timeline_basis` must match the stage source and `applicability` must be `current_round`. v2 may include `must_keep`, a list of nonempty phrases that must remain outside destructive word operations. v1 still accepts only initial, parentless, millisecond-aligned range instructions. v2 additionally accepts the maintained instruction parser's exact word deletion and mute operations, complete-word `start_cue`, and `freeze_frame` when explicit source-bound visual evidence is supplied. Unknown operations and unsupported natural-language combinations block; the original instruction text remains in the receipt.

Visual evidence is an optional instruction member with exactly `source_sha256`, `frame_time`, and `end`. The hash must match the selected source. The selected frame and coverage interval are still subject to visual content review; hash validation does not establish that the picture is semantically correct.

Word deletion, mute, and speech cue operations require exact word timestamps. Before compilation, the package creates `alignment.words.json` in the fixed run directory through its configured ASR provider. The alignment must bind to the current source SHA and duration. Planning validates unique exact token matches and any declared `must_keep` phrases. Repeated ambiguous phrases, missing or stale alignment, and protection conflicts block before rendering. Render reuses the saved alignment and verifies it again; a retry does not request another transcript or allocate a new review round.

Pure range selection does not call ASR. All operations are resolved against the selected source timeline: original VM media for initial, and the explicitly bound parent MP4 for review. A review does not reuse original-video coordinates or request a new VM download.

## Plan and Result

The v2 plan retains package, task/run, record, round, source, delivery, raw instruction, and byte-hash bindings. `alignment_sha256` is null for instructions that need no ASR and otherwise identifies the saved source-bound word alignment. Each resolved instruction lists the IDs of the output segments it produced. Output segments record source and target offsets at microsecond precision. Operations record their exact source boundaries, match strategy, ASR identity, and protection evidence where applicable. A `start_cue` operation also records its requested anchor, search end and radius, resolved whole-word text and exact word interval; Taskboard independently confirms this is the unique aligned phrase in that search window. `expected_duration_ms` is computed from the compiled edit plan.

The render result contains exactly one `review_mp4` with the context's output path, SHA-256, byte count, measured duration, H.264/AAC profile, 1920×1080 dimensions, 25 fps, and full-decode result. The host must independently validate identity, paths, hashes, media streams, duration, and decode. `rendered` means a local artifact passed structural validation; it does not mean semantic review, PMO attachment delivery, or stage completion.

Plan, result, and MP4 publication are no-overwrite. Concurrent plan calls for one run serialize through a local lock; a repeated call returns the existing receipt only after revalidating the bound input, source, and any word alignment, while partial or mismatched outputs fail. A blocked operation produces no plan or MP4. A changed source, alignment, plan, parent, or context cannot be accepted as the same run.

## Verification Boundary

Offline package tests use generated media and synthetic ASR word timestamps only to verify deterministic mechanics, ambiguity rejection, hash binding, and protection checks. They do not certify real ASR accuracy, human listening, visual semantics, or Taskboard delivery. Real material still needs the editing checklist in [validation](validation.md) and a separate Taskboard test-record acceptance.
