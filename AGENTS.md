# Auto-Cut Slicing repository

This is the independent `auto-cut-slicing` source repository. The primary branch is `main`.
Develop on `codex/*`, `feature/*` or `fix/*` branches. It is not an Auto-Cut Lite workspace.

- Before planning or revising real-media edits, read [project editing standards](docs/editing-standards.md)
  and use the delivery checklist in [validation](docs/validation.md). These apply to future tasks;
  current explicit user requirements take precedence. Preserve confirmed feedback as reusable rules,
  with sample-specific timings kept as examples rather than universal defaults.
- This workflow makes real ripple edits: deleting words or paragraphs reduces output duration.
  Keep complete source files; expose each retained range as an editable video/audio segment.
- Independent slices and ordered multi-source concatenation share one maintained planner.
- Speech deletion requires source-hash-bound word timestamps, a unique exact contiguous match,
  and protected `must_keep` checks. Never substitute rough times for word boundaries.
- Treat requested start times as local editing anchors. Prefer a complete nearby word or phrase;
  do not retain an earlier introductory clause merely to reach an ASR sentence boundary.
  Start adjustments normally stay within about one second. Larger changes require content review,
  not a blind one-second clamp; encode the reviewed start with a source-bound `start_cue`.
- Inspect the first picture at every output opening and splice, plus the surrounding transition.
  Residual previous-topic content requires a reviewed clean later frame and a bounded freeze
  until natural new content can resume, with original audio continuing. Pixel/source equality
  alone does not establish that a cut-in picture is contextually appropriate.
- Review pacing at semantic joins. When a pause is requested, hold the previous picture and
  preserve a verified speech-free source tail when available, including the last word's decay;
  shift the following picture and speech together. Do not treat a cut at the final word as
  sufficient breathing room or include the next excluded word to obtain a pause.
- Unknown operations, ambiguous matches, invalid or empty ranges must fail before draft writing.
- Review labels preserve `source_text` exactly. Keep diagnostic status in receipts only.
- Reuse Auto-Cut Lite through the pinned external dependency bridge. Never mutate its code,
  import its task caches, invoke its Lite review runner for real deletion, or fall back to
  retired Auto-Cut checkouts. Run its identity assertion before reading its runtime.
- Do not hot-patch installed runtimes. Initial bridge supports a verified clean source checkout
  only; packaged runtime support requires its deployment/inventory validation.
- Keep source media, credentials, machine paths, caches, test outputs and generated drafts under
  ignored directories. Local dependency paths live in `.local/config.json`.
- Drafts are the default product. MP4 rendering is an explicit optional output.
- Validate saved root and active timeline segment ranges, audio synchronization, exact markers,
  duration, complete local materials and edit receipts. Never claim target-open or spoken-audio
  final acceptance from structural tests or synthetic ASR fixtures.
- Use meaningful planner, integration and real-media tests. Commit verified intended changes
  and finish with clean Git status, or explain the concrete blocker.
