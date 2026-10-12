# Taskboard MP4 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement these tasks.

**Goal:** Generate validated batches of MP4 slices from one local video and editing instructions.

**Architecture:** A versioned request adapter compiles each output through the maintained planner.
Planner effects feed an extended renderer; a batch runner records independent success/failure.

**Tech Stack:** Python 3.10-3.12 standard library, pytest, FFmpeg/FFprobe.

**Spec:** `docs/superpowers/specs/2026-10-12-taskboard-mp4-design.md`.

## Global constraints

- Develop on codex/video-slicing-core; do not mutate upstream runtimes.
- Taskboard output is MP4 at 1920 x 1080, 25 fps; scaling preserves aspect ratio.
- Preserve exact raw instruction text; use hash-bound exact word timestamps.
- Failed outputs do not prevent other outputs; never overwrite unrelated output files.
- Semantic visual/real speech acceptance is separate from synthetic structural evidence.

## Task 1: Speech anchors and nondestructive effects

Files: planner.py, drafts.py, tests/test_planner.py.
Interfaces: existing compile_job; clip start_cue and edits; canonical output effects per spec.

- [x] Add tests that cue start uses ASR, mute retains duration, and freeze maps after deletion.
  Example assertion: `assert output['effects'][0]['target_start_us'] == 1_000_000` after a preceding one-second deletion.
- [x] Run `python -m pytest tests/test_planner.py -q` and confirm new cases fail.
- [x] Extend the single planner and reject MP4-only effects in draft output.
- [x] Re-run planner/draft tests; commit verified intended changes with the integrated feature.

## Task 2: Effect-aware renderer and fixed delivery profile

Files: render.py, tests/test_media_render.py.
Interfaces: render_output(plan, output_id, destination, *, width=None,height=None,fps=None).

- [x] Add real-media tests for normalized geometry, silent word intervals and frozen frames.
  Assert a frozen decoded frame equals the selected source frame while audio stays audible.
- [x] Run targeted new tests and confirm failure before implementation.
- [x] Split retained segments at effect boundaries, render frozen picture/muted audio, and
  preserve cumulative frame/sample rounding and atomic validated publication.
- [x] Run existing real-media regression and new cases; integrate and review.

## Task 3: Instruction parser and visual boundary proposals

Files: instructions.py, visual.py, tests/test_instructions.py, tests/test_visual.py.
Interfaces: parse_instruction(text,source_id,duration_us,*,visual_evidence=None) -> clips;
analyze_freeze(source_path,frame_time,*,end=None) -> source-bound boundary proposal.

- [x] Test actual reference patterns, source-end expansion, exact raw labels, unknown text and
  missing visual evidence. Example: `assert clips[1]['start_cue']['text'] == '接下来'`.
- [x] Confirm tests fail before writing implementation.
- [x] Implement conservative grammar and frame-level change proposal; never infer semantic
  NaOH content solely from pixels. Return evidence limitations explicitly.
- [x] Verify parser cases and decoded visual-change fixture; integrate and review.

## Task 4: Taskboard contract, CLI and batch continuation

Files: batch.py, cli.py, tests/test_batch.py, tests/test_cli.py.
Interfaces: run_request(request_path,result_path,*,resume=False) -> result.

- [x] Test invalid first output followed by valid output, request-relative paths, path conflicts,
  fixed delivery profile, result persistence and refusal of unrelated existing outputs.
  Assert `result['status'] == 'partial_failure'` and the subsequent output MP4 exists.
- [x] Confirm new runner/CLI tests fail.
- [x] Implement strict envelope validation, per-output compilation, serial rendering with
  continuation, structured error codes, atomic receipts and hash-verified resume.
- [x] Add run and inspect-freeze CLI entry points; ensure reliable JSON/exit-code contract.
- [x] Run real CLI integration and regression checks.

## Task 5: Documentation, release and broad review

Files: README.md, docs/job-format.md, docs/validation.md, docs/taskboard-contract.md,
examples/taskboard-request.json, maintained and installed repository SKILL.md, pyproject.toml.

- [x] Document exact contract, special-operation evidence, receipt states and resume behavior.
- [x] Run pytest and Ruff; use generated real media for end-to-end fixed-profile acceptance.
- [x] Request independent branch review, fix actionable defects with regression cases.
- [x] Commit verified intended changes, confirm clean git status and report actual verification.

## Verification record

- Final complete suite: 201 passed, including actual FFmpeg picture/audio assertions.
- Ruff and diff whitespace checks passed.
- Actual Python CLI acceptance: 4 MP4 successes, 1 isolated invalid instruction, successful
  files preserved on resume, and inspect-freeze returned semantic_verified=false.
- Independent reviews covered planner, parser/visual, renderer, batch/CLI, skill and documentation.
- Regression fixes cover resume evidence preservation and races, strict picture grammar,
  non-frame-aligned/tail freeze selection, differing source/output FPS and source display aspect.
- User media, real ASR/speech acceptance, semantic picture acceptance and Taskboard host integration
  remain outside this generated-media development run.
