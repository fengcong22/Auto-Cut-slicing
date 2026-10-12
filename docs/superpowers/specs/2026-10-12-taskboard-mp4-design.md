# Taskboard MP4 slicing design

Approved in the chat on 2026-10-12. Implement on `codex/video-slicing-core`.

## Product behavior

A local source video produces independent MP4 outputs from ordered retained source ranges.
The Taskboard path generates MP4 directly, at 1920 x 1080 and 25 fps, with aspect-preserving
scaling and padding. Keep source sound content and timing, except explicit edits; AAC re-encoding
is allowed. Failed outputs do not prevent other valid outputs from rendering.

Ordinary word deletion removes video and audio together and ripples the timeline. Explicit
requests to remove only sound mute the exact word interval without shortening the timeline.
Parenthesized speech cues such as 首先 and 接下来 mean the selected clip starts at the cue's
source-bound unique whole-token ASR start. Numeric times are search anchors, not word boundaries.
Freeze replacement uses a selected source frame until the next animation/content boundary;
audio continues and replacement does not change duration. Preserve raw instruction code points.
One source identity may be reused across multiple outputs regardless of display titles.

## Contract

`python -m auto_cut_slicing run request.json --result result.json [--resume]`.
Request: `contract_version: 1`, stable `request_id`, `source: {id,path,asr?}`, and nonempty
`outputs: [{id,name?,instruction?,clips?,output_path,visual_evidence?}]`. Each output declares
exactly one of `instruction` or structured `clips`. Paths resolve relative to the request file.
Use existing schema_version=1 planner jobs internally; do not build a second timeline planner.

Results preserve request and source hashes and per-output instructions, plan hash, paths, output
hashes, expected/actual duration, decode verification, and structured errors. `ok` is true only
when every output succeeds. Record `complete`, `partial_failure`, or `failed` at batch level.
Exit nonzero on any failed output. Logs go to stderr, final JSON to stdout, atomic result snapshots
to the requested file. Refuse overwrite. Resume only matching requests and verified hashes;
existing unrelated MP4 files never count as success. Globally invalid envelopes fail before
rendering; per-output parse/plan/target failures are recorded while other outputs continue.

## Planner extensions

Clip `start_cue: {text,search_radius?}` resolves around nominal `start` (default radius 3 seconds).
Clip `edits` contains `mute_word` (text, rough_time?, search_radius?, must_keep?, source_text)
or `freeze_frame` (start,end,frame_time,source_text). IDs remain mandatory and stable.
Canonical output `effects` contains kind (`mute` or `freeze_frame`), item_id, clip_id, source_id,
source_text, source_start_us, source_end_us, target_start_us, duration_us, and frame_time_us for
freeze. Map effects through retained segments after ripple deletions. Reject overlapping effects
of the same kind, invalid frame/range coordinates, and unknown operations. Draft writing rejects
MP4-only effects explicitly; ordinary draft behavior remains compatible.

## Instruction interpretation and visual evidence

Support explicit ranges, ordered `+` joins, source end, full-width punctuation, speech cues,
standalone word deletion, and explicit audio-only deletion embedded in ranges. Complex picture
instructions compile only with source-hash-bound visual evidence `{source_sha256,frame_time,end}`.
Analysis can propose the next visual-change boundary but pixel change alone does not verify that
the selected frame shows NaOH or that a semantic animation boundary is correct. Missing evidence
fails that output with an actionable reason; do not silently discard picture instructions.

## Validation and boundary

Python 3.10-3.12, existing FFmpeg/FFprobe and standard library only. Source files, local paths,
generated media and service secrets stay ignored. No upstream runtime mutation, task-cache reuse,
external writes, or service requests during development. Word evidence uses the existing pinned
bridge when real ASR is requested. Synthetic ASR is marked fixture evidence. Real-media tests
assert decoded frames, audio intervals, fixed geometry/fps, ripple mapping, batch continuation,
conflicts and resume integrity. No claim of real speech listening or semantic final acceptance
without supplied user media.
