"""Check whether a rendered output carries out its edit intent (schemas/qa-report.v1).

Automatic checks cover coverage, compiled operations, duration, word-level cut positions and,
when the output has its own source-bound reverse ASR, phrase absence/presence. Pictures,
listening and pacing are exported or listed for review and never marked as passed here.
"""

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .alignment import find_phrase_matches, load_alignment, normalized_text
from .boundaries import _sentences
from .context import EditContext
from .errors import SlicingError
from .intent import check_intent
from .io import file_sha256, read_json, write_json, write_new_json
from .schema import require_schema

_PHRASE_WINDOW_US = 2_000_000
_SILENCE_DBFS = -60.0
_DARK_LUMA = 16.0  # an all-black frame averages far below any real picture
_BLUR_VARIANCE = 12.0  # Laplacian variance of a frame with no usable detail
_FLASH_LUMA = 60.0  # luma jump between adjacent frames that reads as a white flash
_PICTURE_CHANGE = 8.0  # mean gray difference (0-255) on a 48x27 proxy
_MOTION_PEAK = 40  # a single pixel this far from the held frame is real motion, not compression noise
_MOTION_PIXELS = 4  # that many pixels must move together, so one noisy pixel does not count


# Questions an AI reviewer answers for each picture item. The wording is fixed so that a review
# file can be checked against the report rather than trusted blindly.
_QUESTIONS = {
    "opening_frame": [("opening_clean", "Does the opening frame show only this output's own content, "
                                       "with no frame, icon or text from another page?")],
    "seam_frames": [("seam_clean", "At this join, does the first frame of the new clip show only new "
                                   "content, with no leftovers, fragments or flashes of the previous one?")],
    "freeze_frames": [("freeze_clean", "Do the held frame and the frame where playback resumes show only "
                                       "the intended picture, with no flash of other content?")],
}


def _check(rows, check_id, kind, status, method, detail, *, intent_id=None, start=None, end=None,
           frames=None):
    row = {"check_id": check_id, "intent_id": intent_id, "kind": kind, "status": status,
           "method": method, "evidence_ids": [], "target_start_us": start,
           "target_end_us": end, "detail": detail}
    if frames:
        row["frames"] = list(frames)
    rows.append(row)


def _export_frame(video: Path, target_us: int, lead_us: int, image: Path) -> dict[str, str]:
    """Write one review frame and describe it; the SHA-256 binds any later AI answer to these bytes."""
    from .media import find_tool

    subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{max(0, target_us - lead_us) / 1_000_000:.6f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=960:-2", str(image)],
        check=True, capture_output=True,
    )
    if not image.is_file():
        # A timestamp past the last decodable frame yields no picture; the caller skips it.
        return {}
    return {"file": image.name, "sha256": file_sha256(image)}


def _gray(video: Path, at_us: int) -> bytes:
    from .media import find_tool

    return subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{at_us / 1_000_000:.6f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=48:27,format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout


def _gray_run(video: Path, start_us: int, duration_us: int) -> list[bytes]:
    from .media import find_tool

    raw = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{start_us / 1_000_000:.6f}", "-t", f"{duration_us / 1_000_000:.6f}", "-i", str(video),
         "-vf", "fps=25,scale=48:27,format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout
    size = 48 * 27
    return [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]


def _frame_timestamps(video: Path) -> list[int]:
    """Presentation timestamps (µs) of every decoded frame, in order."""
    from .media import _run, find_tool

    result = _run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(video)],
        purpose="Frame timestamp probe",
    )
    stamps = []
    for line in result.stdout.splitlines():
        try:
            stamps.append(round(float(line.strip().rstrip(",")) * 1_000_000))
        except ValueError:
            continue
    return sorted(stamps)


def _frame_stats(video: Path, *, start_us: int | None = None, duration_us: int | None = None,
                 fps: Fraction | None = None) -> list[tuple[float, float, bytes]]:
    """Per-frame (mean luma, Laplacian variance, 32x18 signature) in decode order.

    With a range and a rate, the frames of that stretch resampled to the rate: what a render of it shows.
    """
    from .media import find_tool

    seek = [] if start_us is None else [
        "-ss", f"{start_us / 1_000_000:.6f}", "-t", f"{duration_us / 1_000_000:.6f}"]
    rate = "" if fps is None else f"fps={fps.numerator}/{fps.denominator},"
    result = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", *seek, "-i", str(video),
         "-vf", f"{rate}scale=64:36,format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    )
    size = 64 * 36
    raw = result.stdout
    stats = []
    for start in range(0, len(raw) - size + 1, size):
        frame = raw[start:start + size]
        mean = sum(frame) / size
        # 4-neighbour Laplacian: sum of squared responses flags a frame with no edge detail.
        total = 0
        for y in range(1, 35):
            row = y * 64
            for x in range(1, 63):
                i = row + x
                delta = 4 * frame[i] - frame[i - 1] - frame[i + 1] - frame[i - 64] - frame[i + 64]
                total += delta * delta
        # Half-resolution signature: 32x18 samples, enough to tell a held frame from a new one.
        signature = b"".join(
            frame[y * 64:(y + 1) * 64:2] for y in range(0, 36, 2))
        stats.append((mean, total / ((62) * 34) / 16, signature))
    return stats


def _mechanical_picture_checks(rows, output, video, video_asr=None, context=None):
    """Deterministic picture checks on the rendered file itself.

    These compare the file with the plan and look for frames that cannot be intended — missing or
    extra frames, unexplained still frames, black or blank frames, flashes and blurred pictures.
    They judge no content; whether a clean frame is semantically right stays a separate question.
    """
    try:
        stamps = _frame_timestamps(video)
        stats = _frame_stats(video)
    except SlicingError as error:
        _check(rows, "frames", "frame_count", "failed", "automatic",
               f"Frame inspection failed: {error}")
        return
    # The output's own frame rate is authoritative; a plan may predate the rendered profile.
    expected_fps = Fraction(str(_video_fps(video))).limit_denominator(1001)
    planned = round(Fraction(output["duration_us"], 1_000_000) * expected_fps)
    # Rounding may absorb at most the half frame that the planned frame count itself rounds away.
    _check(rows, "frames", "frame_count",
           "passed" if len(stamps) == planned else "failed", "automatic",
           f"{len(stamps)} decoded frames against {planned} planned at {float(expected_fps):g} fps",
           start=0, end=output["duration_us"])
    if len(stamps) > 1:
        steps = [b - a for a, b in zip(stamps, stamps[1:])]
        nominal = round(1_000_000 / float(expected_fps))
        jumps = [i for i, step in enumerate(steps) if abs(step - nominal) > nominal // 2]
        _check(rows, "frames", "frame_timing",
               "passed" if not jumps else "failed", "automatic",
               f"{len(jumps)} irregular frame intervals; first at frame {jumps[0]} "
               f"({stamps[jumps[0]]}us)" if jumps else "Every frame interval matches the output rate",
               start=stamps[jumps[0]] if jumps else 0)
    if stats:
        dark = [i for i, row in enumerate(stats) if row[0] < _DARK_LUMA]
        _check(rows, "frames", "black_frames",
               "passed" if not dark else "failed", "automatic",
               f"{len(dark)} near-black frames; first at frame {dark[0]}" if dark
               else "No near-black frames", start=stamps[dark[0]] if dark and dark[0] < len(stamps) else 0)
        blurred = [i for i, row in enumerate(stats)
                   if row[1] < _BLUR_VARIANCE and row[0] >= _DARK_LUMA]
        _check(rows, "frames", "blurred_frames",
               "passed" if not blurred else "failed", "automatic",
               f"{len(blurred)} frames without usable detail; first at frame {blurred[0]}" if blurred
               else "Every frame carries edge detail", start=stamps[blurred[0]] if blurred and blurred[0] < len(stamps) else 0)
        flashes = [i for i in range(1, len(stats))
                   if abs(stats[i][0] - stats[i - 1][0]) > _FLASH_LUMA
                   and not _near_planned_change(output, stamps, i)]
        _check(rows, "frames", "luma_flashes",
               "passed" if not flashes else "failed", "automatic",
               f"{len(flashes)} unexplained luma jumps between adjacent frames; first at frame {flashes[0]}"
               if flashes else "No unexplained brightness jumps",
               start=stamps[flashes[0]] if flashes and flashes[0] < len(stamps) else 0)
        _held_frames(rows, output, stats, stamps, expected_fps, context)
    if video_asr is not None:
        _check(rows, "speech", "speech_present", "passed", "automatic",
               "Output speech was transcribed (--video-asr); the comparison is in the phrase checks.")


def _video_fps(video: Path) -> float:
    from .media import _probe

    for stream in _probe(video).get("streams", []):
        if stream.get("codec_type") == "video":
            try:
                value = float(Fraction(str(stream.get("avg_frame_rate", "0/0"))))
            except (ValueError, ZeroDivisionError):
                continue
            if value > 0:
                return value
    raise SlicingError("Rendered file has no readable video frame rate")


def _near_planned_change(output, stamps, index):
    """A jump at a planned seam or freeze boundary is the edit itself, not a defect."""
    if index < len(stamps):
        at = stamps[index]
        boundaries = {row["target_start_us"] for row in output["segments"]}
        for effect in output.get("effects", []):
            boundaries.add(effect["target_start_us"])
            boundaries.add(effect["target_start_us"] + effect["duration_us"])
        return any(abs(at - boundary) <= 1_000_000 for boundary in boundaries)
    return False


def _same_frame(first: bytes, second: bytes) -> bool:
    """Two frames are the same picture when almost every sampled pixel matches."""
    if not first or len(first) != len(second):
        return False
    close = sum(1 for a, b in zip(first, second) if abs(a - b) <= 2)
    return close >= len(first) * 0.995


def _source_frames(context, output, stamps, fps):
    """For every output frame, the signature of the source frame the plan puts there (None if unknown).

    Each segment's source range is decoded once at the output rate, so frame k of a segment lines up
    with output frame target_start + k. Frames under a planned effect or outside any segment stay None.
    """
    if context is None:
        return None
    nominal = 1_000_000 / float(fps)
    signatures = [None] * len(stamps)
    for segment in output["segments"]:
        try:
            frames = _frame_stats(Path(context.source(segment["source_id"])["path"]),
                                  start_us=segment["source_start_us"], duration_us=segment["duration_us"],
                                  fps=Fraction(fps).limit_denominator(1001))
        except (SlicingError, subprocess.CalledProcessError, OSError):
            continue
        start, end = segment["target_start_us"], segment["target_start_us"] + segment["duration_us"]
        for index, at in enumerate(stamps):
            if start <= at < end:
                k = round((at - start) / nominal)
                if 0 <= k < len(frames):
                    signatures[index] = frames[k][2]
    return signatures


def _held_frames(rows, output, stats, stamps, fps, context=None):
    """Still stretches must be the planned freezes, not a stalled decoder or a wrong trim.

    A stretch the source itself holds (a slide, a paused screen recording) is the content, not a stall:
    it is explained when each held frame matches the source frame the plan puts there.
    """
    nominal = round(1_000_000 / float(fps))
    stills = []
    run_start = 0
    for i in range(1, len(stats)):
        same = _same_frame(stats[i][2], stats[run_start][2])
        if not same:
            if i - run_start > 1:
                stills.append((run_start, i))
            run_start = i
    if len(stats) - run_start > 1:
        stills.append((run_start, len(stats)))
    planned = []
    for start_us, end_us in sorted(
        (effect["target_start_us"], effect["target_start_us"] + effect["duration_us"])
        for effect in output.get("effects", []) if effect["kind"] == "freeze_frame"
    ):
        # Adjacent freezes read as one still stretch, and a seam pause may hold across both.
        if planned and start_us <= planned[-1][1] + nominal:
            planned[-1] = (planned[-1][0], max(planned[-1][1], end_us))
        else:
            planned.append((start_us, end_us))
    source = _source_frames(context, output, stamps, fps) if stills else None

    def source_held(first, last):
        # Every held output frame shows the source frame the plan puts there: the source held too.
        if source is None or last > len(source):
            return False
        return all(source[i] is not None and _same_frame(stats[i][2], source[i]) for i in range(first, last))

    unexplained = []
    for first, last in stills:
        if source_held(first, last):
            continue
        start_us = stamps[first] if first < len(stamps) else 0
        end_us = stamps[last] if last < len(stamps) else start_us + (last - first) * nominal
        if not any(start_us >= p0 - 2 * nominal and end_us <= p1 + 2 * nominal for p0, p1 in planned):
            unexplained.append((first, (last - first) * nominal))
    _check(rows, "frames", "still_frames",
           "passed" if not unexplained else "failed", "automatic",
           f"{len(unexplained)} still stretches that neither a planned freeze nor the source explains; "
           f"first {unexplained[0][1]}us at frame {unexplained[0][0]}" if unexplained
           else "Every still stretch is a planned freeze or held in the source",
           start=stamps[unexplained[0][0]] if unexplained and unexplained[0][0] < len(stamps) else 0)


def _hidden_motion(source_path: Path, start_us: int, end_us: int) -> tuple[float, int | None]:
    """Largest frame-to-frame change inside a source range a freeze replaces, and where it occurs."""
    frames = _gray_run(source_path, start_us, end_us - start_us)
    steps = [(_difference(a, b), index) for index, (a, b) in enumerate(zip(frames, frames[1:]), start=1)]
    if not steps:
        return 0.0, None
    worst, index = max(steps)
    return worst, start_us + index * 40_000


def _difference(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _moves(base: bytes, frame: bytes) -> bool:
    """Whether a frame has left the held picture, including a transition that covers little of it.

    A mean difference misses a splash or wipe that has only started: a few bright pixels average out
    over the 48x27 proxy. A peak change catches those, and requiring several pixels to move together
    keeps a single compression-noise pixel from counting as motion.
    """
    if not base or len(base) != len(frame):
        return True
    diffs = [abs(x - y) for x, y in zip(base, frame)]
    return max(diffs) > _MOTION_PEAK and sum(value > 30 for value in diffs) >= _MOTION_PIXELS


def _motion_start(source_path: Path, start_us: int, horizon_us: int) -> int | None:
    """First source frame (within the horizon) that has left the picture at start_us."""
    frames = _gray_run(source_path, start_us, horizon_us - start_us)
    for index, frame in enumerate(frames[1:], start=1):
        if _moves(frames[0], frame):
            return start_us + index * 40_000
    return None


def _pause_picture_checks(rows, context, output, video, frame_us, duration_us, frame_dir=None):
    """At every seam, the pause keeps the previous picture until the next picture starts moving.

    The next clip may cut in earlier than its sound: at the first frame of its transition, or at the
    first frame where old content is fully covered. A freeze that runs past that frame and skips the
    rest of the transition fails here, not only in freeze_frames.
    """
    segments = output["segments"]
    for previous, segment in zip(segments, segments[1:]):
        if previous["clip_id"] == segment["clip_id"]:
            continue  # a deletion join inside one clip, not a semantic seam
        join = segment["target_start_us"]
        source = context.source(segment["source_id"])
        alignment = source["alignment"]
        if alignment is None:
            _check(rows, f"pause-picture-{join}", "seam_pause_picture", "not_run", "automatic",
                   "The incoming clip has no word alignment.", start=join, end=join)
            continue
        words = alignment["words"]
        index = next((i for i, w in enumerate(words) if w["start_us"] >= segment["source_start_us"]), None)
        if index is None:
            continue
        first = words[index]["start_us"]
        if index > 0:
            # Sound before the first word (a transition effect, a breath) starts the next clip too.
            from .audio import analyze_gap

            measured = analyze_gap(source["path"], words, index - 1, source["duration_us"])["low_energy_end"]
            if measured is not None and measured >= segment["source_start_us"]:
                first = min(first, measured)
        onset = min(segment["target_start_us"] + first - segment["source_start_us"],
                    segment["target_start_us"] + segment["duration_us"], duration_us)
        if onset - join < 2 * frame_us:
            _check(rows, f"pause-picture-{join}", "seam_pause_picture", "passed", "automatic",
                   "The next clip starts speaking at the seam; there is no lead-in pause to cover.",
                   intent_id=segment["clip_id"], start=join, end=onset)
            continue
        # The pause must hold until the next picture starts moving, which can be a frame before the
        # sound: low-energy detection lands on the onset sample, whose own frame may already move.
        moving = _motion_start(Path(source["path"]), segment["source_start_us"], first)
        picture_change = onset
        if moving is not None and moving < first:
            picture_change = min(onset, join + moving - segment["source_start_us"])
        # A freeze over the lead-in ends where the next picture is allowed to begin.
        freeze_end = max((effect["target_start_us"] + effect["duration_us"]
                          for effect in output.get("effects", [])
                          if effect["kind"] == "freeze_frame" and effect["clip_id"] == segment["clip_id"]
                          and effect["target_start_us"] <= join), default=join)
        held = _gray(video, join - frame_us)
        # The pause runs up to, but not including, the frame where the next picture is allowed to begin:
        # that frame is the cut-in, so comparing it against the held frame would flag a correct entry.
        until = min(freeze_end, picture_change) - frame_us
        # Every frame of the pause, not a few samples: a short transition can fall between samples.
        span = _gray_run(video, join, max(frame_us, until - join))
        # Peak, not mean: a splash that covers little of the frame averages out over the proxy, the same
        # reason _moves exists. Comparing against the frame just before the seam catches the next clip
        # showing its own picture during the pause.
        worst = max((_difference(held, frame) for frame in span), default=0.0)
        problems = []
        if any(_moves(held, frame) for frame in span):
            problems.append(f"shows a different picture before the freeze ends (max change {worst:.1f})")
        played = segment["source_start_us"] + max(0, freeze_end - join)
        review = ""
        frames = []
        if moving is not None and played > moving + frame_us and not problems:
            # Playback resumes after the next picture has already started moving, so part of the
            # transition never plays. That is right only when the hidden frames still show the previous
            # topic (a wipe that has not covered it yet) and the cut-in is the first frame where it is
            # covered. Pixels cannot tell that residue from a transition that has already finished, so
            # the frames go to a person instead of a pass or a fail.
            if frame_dir is not None:
                # The hidden frames exist only in the source: in the output they are the held frame.
                # Export them up to the resume point, then the output's first played frame.
                step = max(frame_us, (played - moving) // 12 // frame_us * frame_us)
                for t in range(moving, played, step):
                    image = frame_dir / f"src{t}us_hidden.png"
                    exported_row = _export_frame(Path(source["path"]), t, 0, image)
                    if exported_row:
                        frames.append(exported_row)
                image = frame_dir / f"t{freeze_end}us_cut-in.png"
                exported_row = _export_frame(video, freeze_end, 0, image)
                if exported_row:
                    frames.append(exported_row)
            review = (f" Playback resumes at source {played}us, after the next picture starts moving "
                      f"(source {moving}us). Confirm the skipped frames still show the previous topic and "
                      f"the cut-in is the first frame where it is covered"
                      + (f": {frame_dir.name}/{', '.join(row['file'] for row in frames)}"
                         if frames else "") + ".")
        status = "failed" if problems else ("needs_human" if review else "passed")
        _check(rows, f"pause-picture-{join}", "seam_pause_picture", status, "automatic",
               (f"The {(onset - join) / 1e6:.2f}s pause holds the previous picture until the next picture "
                f"starts moving (source {segment['source_start_us'] + max(0, freeze_end - join)}us)."
                + review if not problems else
                f"The {(onset - join) / 1e6:.2f}s pause before the next clip's sound " + "; ".join(problems) +
                ". Hold the previous clip's last frame until the transition's first frame, or the first frame "
                "where old content is covered, and let the rest of the transition play."),
               intent_id=segment["clip_id"], start=join, end=onset,
               frames=[row["file"] for row in frames] if review and frame_dir is not None else None)


def _rms_dbfs(video: Path, start_us: int, duration_us: int) -> float | None:
    """RMS level in dBFS; -inf for digital silence, None when no audio samples decode."""
    import array
    import math

    from .media import find_tool

    raw = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{start_us / 1_000_000:.6f}", "-t", f"{duration_us / 1_000_000:.6f}",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    samples = array.array("h", raw[: len(raw) // 2 * 2])
    if not samples:
        return None
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else float("-inf")


def _seam_status(context, source_id, words, side, cut, inside, before, after):
    """Compare a cut with the measured decay of the previous word and onset of the next."""
    from .audio import analyze_gap

    if inside:
        return "needs_human", "automatic", "Cut splits a word; see cut_not_inside_word."
    if before is None:
        return "needs_human", "automatic", f"{side} cut at {cut}us precedes the first ASR word; listen."
    source = context.source(source_id)
    index = words.index(before)
    gap = analyze_gap(source["path"], words, index, source["duration_us"])
    decay, onset = gap["decay_end"], gap["low_energy_end"]
    label = f"{side} cut at source {cut}us after {before['text']!r}"
    if decay is None:
        return "needs_human", "automatic", f"{label}: no measurable pause before {after['text'] if after else 'the end'!r}."
    numbers = f"decay ends {decay}us, next onset {onset}us (threshold {gap['threshold_dbfs']} dBFS)"
    if cut < decay:
        return "needs_human", "automatic", f"{label} cuts {decay - cut}us of the word's decay; {numbers}."
    if cut > onset:
        return "needs_human", "automatic", f"{label} includes {cut - onset}us of the next word's onset; {numbers}."
    return "passed", "automatic", f"{label} lies in the measured pause; {numbers}. Pacing still needs listening."


def _cuts(output: dict[str, Any], durations: dict[str, int]):
    """Yield (segment, side, source_us, target_us) for every cut inside a source."""
    for segment in output["segments"]:
        start = segment["source_start_us"]
        end = start + segment["duration_us"]
        if start > 0:
            yield segment, "in", start, segment["target_start_us"]
        if end < durations[segment["source_id"]]:
            yield segment, "out", end, segment["target_start_us"] + segment["duration_us"]


def _review_request(report_path, frames_dir, rows):
    """Describe the picture items an AI can review, with the exact frames it must look at.

    The package never calls a model: whoever runs the review reads this file, answers each question,
    and writes a qa-review file that ``apply_review`` checks against this request and the report.
    """
    items = []
    for row in rows:
        if row["status"] != "needs_human" or not row.get("frames"):
            continue
        questions = _QUESTIONS.get(row["kind"])
        if not questions:
            continue
        shown = []
        for name in row["frames"]:
            path = frames_dir / name
            if path.is_file():
                shown.append({"path": str(path), "file": name, "sha256": file_sha256(path)})
        if not shown:
            continue
        items.append({
            "check_id": row["check_id"], "kind": row["kind"], "intent_id": row["intent_id"],
            "target_start_us": row["target_start_us"], "target_end_us": row["target_end_us"],
            "detail": row["detail"],
            "frames": shown,
            "questions": [{"question_id": key, "text": text} for key, text in questions],
        })
    return {
        "schema": "auto-cut-slicing/qa-review-request", "schema_version": 1,
        "qa_path": str(report_path), "frames_dir": str(frames_dir), "items": items,
        "instruction": "Look at every listed frame and answer each question. Write a qa-review file "
                       "(schemas/qa-review.v1.schema.json) with qa_sha256 of the QA report, the frame "
                       "hashes you checked, and one item per question: clean, defect or unclear.",
    }


def _review_problems(review, report, report_path, frames_dir):
    """Everything wrong with a review file, as human-readable strings; empty means it applies."""
    problems = []
    if not isinstance(review, dict):
        return ["review is not an object"]
    if review.get("qa_sha256") != file_sha256(report_path):
        problems.append("qa_sha256 does not match the QA report")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, dict) or not reviewer.get("agent") or not reviewer.get("model"):
        problems.append("reviewer agent and model are required")
    wanted = {}
    for row in report["checks"]:
        if row["status"] == "needs_human" and row.get("frames"):
            for key, _ in _QUESTIONS.get(row["kind"], []):
                wanted[(row["check_id"], key)] = row["frames"]
    seen = set()
    hashes = review.get("frames_sha256") or {}
    for item in review.get("items") or []:
        if not isinstance(item, dict):
            problems.append("review item is not an object")
            continue
        key = (item.get("check_id"), item.get("question_id"))
        if key not in wanted:
            problems.append(f"{key[0]}/{key[1]} is not a reviewable question in this report")
            continue
        if key in seen:
            problems.append(f"{key[0]}/{key[1]} is answered more than once")
            continue
        seen.add(key)
        if item.get("verdict") not in ("clean", "defect", "unclear"):
            problems.append(f"{key[0]}/{key[1]} has no usable verdict")
        if not str(item.get("observation") or "").strip():
            problems.append(f"{key[0]}/{key[1]} has no observation")
        listed = item.get("frames") or []
        if not listed:
            problems.append(f"{key[0]}/{key[1]} cites no frames")
        for name in listed:
            if name not in wanted[key]:
                problems.append(f"{key[0]}/{key[1]} cites {name}, which is not one of its frames")
    missing = sorted(f"{cid}/{qid}" for (cid, qid) in wanted if (cid, qid) not in seen)
    if missing:
        problems.append("answers missing for: " + ", ".join(missing[:10]))
    for name, digest in hashes.items():
        path = Path(frames_dir) / name
        if not path.is_file() or file_sha256(path) != digest:
            problems.append(f"frame {name} does not match its recorded hash")
        if not any(name in row["frames"] for row in report["checks"] if row.get("frames")):
            problems.append(f"frame {name} is not part of this report")
    return problems


def apply_review(report_path, frames_dir, review_path):
    """Merge AI picture verdicts into a QA report.

    Only picture items the report left as ``needs_human`` can be answered. A clean answer becomes
    ``ai_reviewed`` with method ``ai_visual``; a defect answer becomes ``failed``; ``unclear`` stays
    ``needs_human``. Automatic results and listening or pacing items are never touched.
    """
    try:
        require_schema(read_json(review_path), "qa-review.v1.schema.json", "QA review")
    except SlicingError as error:
        raise SlicingError(f"QA review does not apply: {error}") from error
    report = read_json(report_path)
    problems = _review_problems(read_json(review_path), report, report_path, frames_dir)
    if problems:
        raise SlicingError("QA review does not apply: " + "; ".join(problems[:8]))
    review = read_json(review_path)
    answers = {(item["check_id"], item["question_id"]): item for item in review["items"]}
    answers_by_check = {}
    for (check_id, _), item in answers.items():
        answers_by_check.setdefault(check_id, []).append(item)
    for row in report["checks"]:
        if row["check_id"] not in answers_by_check:
            continue
        if row["status"] != "needs_human":
            continue
        items = answers_by_check[row["check_id"]]
        defects = [item for item in items if item["verdict"] == "defect"]
        unclear = [item for item in items if item["verdict"] == "unclear"]
        notes = " | ".join(f"{item['observation']}" for item in items)
        if defects:
            row["status"], row["method"] = "failed", "ai_visual"
            row["detail"] = f"AI visual review found a defect: {notes} Original: {row['detail']}"
        elif unclear:
            row["status"], row["method"] = "needs_human", "ai_visual"
            row["detail"] = f"AI visual review was unclear: {notes} Original: {row['detail']}"
        else:
            row["status"], row["method"] = "ai_reviewed", "ai_visual"
            row["detail"] = f"AI visual review: {notes} Original: {row['detail']}"
        row["frames_sha256"] = {item["frames"][0]: review["frames_sha256"][item["frames"][0]]
                                for item in items if item.get("frames")}
    report["overall"] = "failed" if any(row["status"] == "failed" for row in report["checks"]) else "ready_for_review"
    report["review"] = {"agent": review["reviewer"]["agent"], "model": review["reviewer"]["model"],
                        "path": str(Path(review_path).resolve())}
    require_schema(report, "qa-report.v1.schema.json", "QA report")
    # The report itself is rewritten in place: the review is the only writer of these fields, and
    # the frame hashes and qa_sha256 of the review no longer match once it is applied.
    write_json(report_path, report)
    counts = {}
    for row in report["checks"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"ok": report["overall"] != "failed", "overall": report["overall"], "counts": counts,
            "report_path": str(Path(report_path).resolve())}


def run_qa(context: EditContext, intent_path, plan_path, output_id, video, video_asr, report_path,
           *, plan_binding_path=None):
    """``plan_binding_path`` names the persisted plan file the report binds when ``plan_path``
    is a temporary engine plan derived from it (VM/PMO binds its own ``plan.json``)."""
    from .media import describe_source

    report_path = Path(report_path)
    if report_path.exists():
        raise SlicingError(f"Refusing to overwrite: {report_path}")
    plan = read_json(plan_path)
    output = next((row for row in plan.get("outputs", []) if row["id"] == output_id), None)
    if output is None:
        raise SlicingError(f"Plan has no output {output_id}")
    rows: list[dict[str, Any]] = []
    checked = check_intent(intent_path, context)
    intent = checked["intent"]
    if checked["problems"] or checked["resolved"] is None:
        _check(rows, "coverage", "coverage_complete", "failed", "automatic",
               "; ".join(f"{p['rule']} {p['message']}" for p in checked["problems"][:5]) or "intent invalid")
        resolved = []
    else:
        _check(rows, "coverage", "coverage_complete", "passed", "automatic",
               "Every feedback span has a disposition; V01-V13 passed.")
        resolved = next((o["clips"] for o in checked["resolved"] if o["output_id"] == output_id), [])

    # Compiled operations and unlisted changes -------------------------------
    plan_items = {row["item_id"] for row in output.get("edits", [])} | {
        row["item_id"] for row in output.get("effects", [])}
    plan_clips = {row["clip_id"] for row in output["segments"]}
    wanted_items = {f"{clip['intent_id']}/{op['intent_id']}" for clip in resolved for op in clip["operations"]}
    wanted_clips = {clip["intent_id"] for clip in resolved}
    missing = sorted((wanted_items - plan_items) | (wanted_clips - plan_clips))
    _check(rows, "compiled", "operation_compiled", "failed" if missing or not resolved else "passed",
           "automatic", f"missing from plan: {missing}" if missing else
           f"{len(wanted_clips)} clips and {len(wanted_items)} operations are in the plan")
    extra = sorted((plan_items - wanted_items) | (plan_clips - wanted_clips))
    _check(rows, "unlisted", "unlisted_change", "failed" if extra else "passed", "automatic",
           f"plan items without an intent: {extra}" if extra else "No plan operation lacks an intent.")

    # Media ------------------------------------------------------------------
    descriptor = describe_source(video)
    tolerance = round(1_000_000 / Fraction(descriptor["fps"])) + 1_000
    delta = abs(descriptor["duration_us"] - output["duration_us"])
    _check(rows, "duration", "duration_match", "passed" if delta <= tolerance else "failed", "automatic",
           f"video {descriptor['duration_us']}us vs plan {output['duration_us']}us (delta {delta}us)",
           start=0, end=descriptor["duration_us"])

    # Word-level cut positions ---------------------------------------------------
    durations = {sid: context.source(sid)["duration_us"] for sid in {s["source_id"] for s in output["segments"]}}
    cue_starts = {row["clip_id"]: row["source_start_us"] for row in output.get("start_cues", [])}
    deletion_edges = {(row["source_id"], edge) for row in output.get("edits", [])
                      for edge in (row["source_start_us"], row["source_end_us"])}
    for number, (segment, side, cut, target) in enumerate(_cuts(output, durations), start=1):
        alignment = context.source(segment["source_id"])["alignment"]
        prefix = f"cut{number}"
        if alignment is None:
            _check(rows, prefix, "cut_not_inside_word", "not_run", "automatic",
                   f"{segment['source_id']} has no word alignment", start=target, end=target)
            continue
        words = alignment["words"]
        inside = [w for w in words if w["start_us"] < cut < w["end_us"]]
        before = max((w for w in words if w["end_us"] <= cut), key=lambda w: w["end_us"], default=None)
        after = min((w for w in words if w["start_us"] >= cut), key=lambda w: w["start_us"], default=None)
        where = f"{side} cut at source {cut}us"
        _check(rows, prefix, "cut_not_inside_word", "failed" if inside else "passed", "automatic",
               f"{where} splits {inside[0]['text']!r}" if inside else f"{where} is between words",
               intent_id=segment["clip_id"], start=target, end=target)
        _check(rows, f"{prefix}-gap", "seam_gap", *_seam_status(context, segment["source_id"], words, side,
                                                                cut, inside, before, after),
               intent_id=segment["clip_id"], start=target, end=target)
        if (segment["source_id"], cut) in deletion_edges:
            _check(rows, f"{prefix}-sentence", "sentence_complete", "passed", "automatic",
                   f"{where} is a reviewed word/range deletion join, not a clip edge",
                   intent_id=segment["clip_id"], start=target, end=target)
            continue
        try:
            sentences = _sentences(alignment, durations[segment["source_id"]])
        except SlicingError as error:
            _check(rows, f"{prefix}-sentence", "sentence_complete", "not_run", "automatic", str(error),
                   intent_id=segment["clip_id"], start=target, end=target)
            continue
        interior = next((s for s in sentences if s["sentence_start_us"] < cut < s["sentence_end_us"]), None)
        cue = side == "in" and cue_starts.get(segment["clip_id"]) == cut
        if interior is None:
            status, detail = "passed", f"{where} is at a sentence boundary or pause"
        elif side == "out":
            status, detail = "failed", f"{where} ends inside sentence: {interior['sentence_text']}"
        elif cue:
            status, detail = "passed", f"{where} starts at the reviewed start_cue word"
        else:
            status, detail = "needs_human", f"{where} starts mid-sentence: {interior['sentence_text']}"
        _check(rows, f"{prefix}-sentence", "sentence_complete", status, "automatic", detail,
               intent_id=segment["clip_id"], start=target, end=target)

    # Reverse ASR of the rendered output -----------------------------------------
    out_alignment = None
    if video_asr is not None:
        out_alignment = load_alignment(video_asr, descriptor)
    clip_targets = {}
    for segment in output["segments"]:
        a, b = clip_targets.get(segment["clip_id"], (segment["target_start_us"], 0))
        clip_targets[segment["clip_id"]] = (min(a, segment["target_start_us"]),
                                            max(b, segment["target_start_us"] + segment["duration_us"]))
    edits = {row["item_id"]: row for row in output.get("edits", [])}
    effects = {}
    for row in output.get("effects", []):
        effects.setdefault(row["item_id"], []).append(row)
    for clip in resolved:
        cid = clip["intent_id"]
        target = clip_targets.get(cid)
        checks = []
        for op in clip["operations"]:
            item = f"{cid}/{op['intent_id']}"
            if op["kind"] == "delete_phrase" and item in edits:
                join = edits[item]["target_join_us"]
                checks.append((op["intent_id"], "phrase_absent", op["phrase"]["text"],
                               max(0, join - _PHRASE_WINDOW_US), join + _PHRASE_WINDOW_US, False))
            elif op["kind"] == "mute_phrase" and item in effects:
                first = effects[item][0]
                checks.append((op["intent_id"], "phrase_absent", op["phrase"]["text"],
                               max(0, first["target_start_us"] - 500_000),
                               first["target_start_us"] + first["duration_us"] + 500_000, False))
        if target:
            for keep in clip["must_keep"]:
                checks.append((cid, "must_keep_present", keep, target[0], target[1], True))
        for intent_id, kind, text, a, b, want in checks:
            if out_alignment is None:
                _check(rows, f"{intent_id}-{kind}", kind, "not_run", "automatic",
                       "No reverse ASR of the rendered output was supplied (--video-asr).",
                       intent_id=intent_id, start=a, end=b)
                continue
            b = min(b, descriptor["duration_us"])
            hits = find_phrase_matches(out_alignment, text, clip_start_us=a, clip_end_us=b)
            ok = bool(hits) == want
            _check(rows, f"{intent_id}-{kind}", kind, "passed" if ok else "failed", "automatic",
                   f"{text!r} {'found' if hits else 'not found'} in output {a}-{b}us; reverse ASR can "
                   "misrecognize, so listen before acting on a failure.", intent_id=intent_id, start=a, end=b)
        if "cue" in clip and target:
            if out_alignment is None:
                _check(rows, f"{cid}-cue", "cue_first_word", "not_run", "automatic",
                       "No reverse ASR of the rendered output was supplied (--video-asr).",
                       intent_id=cid, start=target[0], end=target[0])
            else:
                following = [w for w in out_alignment["words"] if w["start_us"] >= target[0] - 100_000][:12]
                spoken = normalized_text("".join(w["text"] for w in following))
                ok = spoken.startswith(normalized_text(clip["cue"]["text"]))
                _check(rows, f"{cid}-cue", "cue_first_word", "passed" if ok else "failed", "automatic",
                       f"output speech after the clip start begins {spoken[:16]!r}",
                       intent_id=cid, start=target[0], end=target[0])

    # Mechanical picture checks on the rendered file -------------------------------------
    _mechanical_picture_checks(rows, output, Path(descriptor["path"]), video_asr, context)

    # Pictures for review ----------------------------------------------------------------
    frame_dir = report_path.with_name(report_path.stem + "-frames")
    frame_dir.mkdir(parents=True, exist_ok=False)
    frame_us = round(1_000_000 / Fraction(descriptor["fps"]))
    lead = min(10_000, frame_us * 2 // 5)
    joins = sorted({segment["target_start_us"] for segment in output["segments"]})
    exported = []
    for join in joins:
        frames = []
        for offset, label in ((-frame_us, "before"), (0, "after")):
            t = join + offset
            if not 0 <= t < descriptor["duration_us"]:
                continue
            image = frame_dir / f"t{t}us_{label}.png"
            exported_row = _export_frame(Path(descriptor["path"]), t, lead, image)
            if exported_row:
                frames.append(exported_row)
        exported.append((join, frames))
    _pause_picture_checks(rows, context, output, Path(descriptor["path"]), frame_us, descriptor["duration_us"], frame_dir)
    for join, frames in exported:
        kind = "opening_frame" if join == 0 else "seam_frames"
        _check(rows, f"frames-{join}", kind, "needs_human", "automatic",
               f"Review {frame_dir.name}/{', '.join(row['file'] for row in frames)} for previous-topic "
               "residue or flashes.",
               start=join, end=join, frames=[row["file"] for row in frames])
    for effect in output.get("effects", []):
        a = effect["target_start_us"]
        b = a + effect["duration_us"]
        op_id = effect["item_id"].split("/")[-1]
        if effect["kind"] == "freeze_frame":
            frames = []
            for t, label in ((b - frame_us, "freeze-last"), (b, "resume")):
                if 0 <= t < descriptor["duration_us"]:
                    image = frame_dir / f"t{t}us_{label}.png"
                    exported_row = _export_frame(Path(descriptor["path"]), t, lead, image)
            if exported_row:
                frames.append(exported_row)
            source = context.source(effect["source_id"])
            motion, at = _hidden_motion(Path(source["path"]), effect["source_start_us"], effect["source_end_us"])
            hidden = ""
            if motion > 3.0:
                after, _ = _hidden_motion(Path(source["path"]), effect["source_end_us"],
                                          min(effect["source_end_us"] + 240_000, source["duration_us"]))
                if after < 1.0:
                    hidden = (f" The freeze hides source motion up to {motion:.1f} (around source {at}us) that has "
                              "already stopped when playback resumes: a transition into the next picture may be "
                              "skipped. End the freeze at the transition's first frame, or at the first frame where "
                              "old content is covered, and let the rest play.")
                else:
                    hidden = (f" The freeze hides the start of source motion (up to {motion:.1f} around source "
                              f"{at}us) that continues after playback resumes; confirm the hidden frames are the "
                              "ones showing old content.")
            _check(rows, f"freeze-{effect['item_id']}", "freeze_frames", "needs_human", "automatic",
                   f"Review the held frame and the hand-back to playback: "
                   f"{frame_dir.name}/{', '.join(row['file'] for row in frames)}; "
                   f"check for flashes of old content or a frozen new animation.{hidden}",
                   intent_id=op_id, start=a, end=b, frames=[row["file"] for row in frames])
        elif effect["kind"] == "mute":
            inner = max(0, effect["duration_us"] - 40_000)
            level = _rms_dbfs(Path(descriptor["path"]), a + 20_000, inner) if inner else None
            if level is None:
                _check(rows, f"mute-{effect['item_id']}-{a}", "mute_silence", "not_run", "automatic",
                       "No audio samples could be measured in the muted range.",
                       intent_id=op_id, start=a, end=b)
            else:
                shown = "digital silence" if level == float("-inf") else f"{level:.1f} dBFS"
                _check(rows, f"mute-{effect['item_id']}-{a}", "mute_silence",
                       "passed" if level <= _SILENCE_DBFS else "failed", "automatic",
                       f"Output audio in the muted range measures {shown} (threshold {_SILENCE_DBFS} dBFS).",
                       intent_id=op_id, start=a, end=b)
    _check(rows, "listening", "listening", "needs_human", "human",
           "Listen to every cut, the opening and the ending.")
    _check(rows, "pacing", "pacing", "needs_human", "human", "Judge pacing at semantic joins.")

    report = {
        "schema": "auto-cut-slicing/qa-report", "schema_version": 1,
        "bindings": {
            "intent_sha256": file_sha256(intent_path), "plan_sha256": file_sha256(plan_binding_path or plan_path),
            "output_id": output_id, "output_sha256": descriptor["sha256"],
            "source_sha256s": sorted({context.source(sid)["sha256"] for sid in durations}),
            "output_alignment_sha256": out_alignment["sha256"] if out_alignment else None,
        },
        "overall": "failed" if any(row["status"] == "failed" for row in rows) else "ready_for_review",
        "checks": rows,
        "limitations": [
            "Automatic checks rely on provider ASR word edges; they do not replace listening.",
            "Frame exports are for review; picture cleanliness is not judged automatically.",
            "ready_for_review is not acceptance.",
        ],
    }
    if isinstance(intent, dict) and intent.get("status") == "blocked":
        report["limitations"].append("The intent is blocked; this output does not implement it.")
    require_schema(report, "qa-report.v1.schema.json", "QA report")
    write_new_json(report_path, report)
    request_path = report_path.with_name(report_path.stem + "-review-request.json")
    request = _review_request(report_path, frame_dir, rows)
    write_new_json(request_path, request)
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"ok": report["overall"] != "failed", "overall": report["overall"], "counts": counts,
            "report_path": str(report_path.resolve()), "frames_dir": str(frame_dir.resolve()),
            "review_request_path": str(request_path.resolve()),
            "reviewable_items": len(request["items"])}
