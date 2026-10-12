import importlib

import pytest

from auto_cut_slicing.errors import SlicingError


def parse(text, duration_us=500_000_000, **kwargs):
    try:
        module = importlib.import_module("auto_cut_slicing.instructions")
    except ModuleNotFoundError:
        pytest.fail("The instruction parser has not been implemented")
    return module.parse_instruction(text, "source", duration_us, **kwargs)


@pytest.mark.parametrize(
    ("instruction", "ranges"),
    [
        ("0:00-2:31;2:34-结束", [(0, 151), (154, 500)]),
        ("0:00-4:56+6:14-6：40", [(0, 296), (374, 400)]),
        ("０：００－２：３１；２：３４－结束", [(0, 151), (154, 500)]),
        ("6:14-6:40+0:00-0:30", [(374, 400), (0, 30)]),
    ],
)
def test_reference_ranges_preserve_requested_order(instruction, ranges):
    clips = parse(instruction)
    assert [(clip["start"], clip["end"]) for clip in clips] == ranges
    assert [clip["id"] for clip in clips] == [f"part{index + 1}" for index in range(len(clips))]
    assert all(clip["source"] == "source" for clip in clips)


def test_joined_start_cue_remains_source_bound_nominal_anchor():
    clips = parse("0:04-1:23 +1:31（接下来）-2:29")
    assert [(clip["start"], clip["end"]) for clip in clips] == [(4, 83), (91, 149)]
    assert clips[1]["start_cue"] == {"text": "接下来"}


def test_first_speech_cue_is_supported():
    assert parse("3:50（首先）-6:05")[0]["start_cue"] == {"text": "首先"}


def test_standalone_deletion_retains_full_source_and_verbatim_instruction():
    raw = " 0:31 剪掉“上一章”三个字，其他不变\n"
    clip = parse(raw)[0]
    assert (clip["start"], clip["end"]) == (0, 500)
    assert clip["deletions"] == [
        {"id": "delete1", "kind": "word", "text": "上一章", "rough_time": 31, "source_text": raw}
    ]
    assert "edits" not in clip


def test_explicit_audio_deletion_does_not_compile_to_ripple():
    raw = "0:06-（4:22 剪掉 “中学阶段”这几个字的声音，可以用停顿占位）-4:34"
    clip = parse(raw)[0]
    assert (clip["start"], clip["end"]) == (6, 274)
    assert clip["edits"] == [
        {"id": "edit1", "kind": "mute_word", "text": "中学阶段", "rough_time": 262, "source_text": raw}
    ]
    assert "deletions" not in clip


def test_standalone_explicit_sound_deletion_is_muted():
    clip = parse("0:31 删除“上一章”的声音，其他不变")[0]
    assert clip["edits"][0]["kind"] == "mute_word"
    assert clip["edits"][0]["rough_time"] == 31


def test_punctuation_normalization_preserves_quoted_spoken_code_points():
    raw = "０：３１ 删除“第１章，Ａ＋Ｂ（实验）”，其他不变"
    deletion = parse(raw)[0]["deletions"][0]
    assert deletion["text"] == "第１章，Ａ＋Ｂ（实验）"
    assert deletion["source_text"] == raw


def test_word_deletion_requires_spoken_characters():
    with pytest.raises(SlicingError, match="spoken text"):
        parse("0:31 删除“！？。”")


def test_visual_instruction_requires_source_bound_evidence():
    with pytest.raises(SlicingError, match="visual_evidence"):
        parse("0:30（语音是“首先”配后面1s只有NaOH的画面）-3:40")


def test_visual_instruction_compiles_exact_supplied_frame_and_boundary():
    raw = " 0:30（语音是“首先”配后面1s只有NaOH的画面）-3:40\n"
    evidence = {"source_sha256": "a" * 64, "frame_time": 31, "end": 35.6}
    clip = parse(raw, visual_evidence=evidence)[0]
    assert clip["start_cue"] == {"text": "首先"}
    assert clip["edits"] == [
        {"id": "edit1", "kind": "freeze_frame", "start": 30, "end": 35.6,
         "frame_time": 31, "source_text": raw}
    ]
    assert evidence == {"source_sha256": "a" * 64, "frame_time": 31, "end": 35.6}


@pytest.mark.parametrize(
    "description",
    [
        "只有NaOH的画面并且改成黑白的画面",
        "只有NaOH并添加标题的画面",
        "只有KOH的画面",
    ],
)
def test_unknown_picture_clauses_are_not_silently_compiled_as_freeze(description):
    evidence = {"source_sha256": "a" * 64, "frame_time": 31, "end": 35.6}
    with pytest.raises(SlicingError, match="Unsupported clip start annotation"):
        parse(
            f"0:30（语音是“首先”配后面1s{description}）-3:40",
            visual_evidence=evidence,
        )


@pytest.mark.parametrize(
    "evidence",
    [None, {}, {"source_sha256": "a" * 64, "frame_time": 31},
     {"source_sha256": "invalid", "frame_time": 31, "end": 35},
     {"source_sha256": "a" * 64, "frame_time": True, "end": 35},
     {"source_sha256": "a" * 64, "frame_time": 31, "end": 30},
     {"source_sha256": "a" * 64, "frame_time": 31, "end": 221}],
)
def test_incomplete_or_outside_visual_evidence_fails_before_compilation(evidence):
    with pytest.raises(SlicingError):
        parse("0:30（语音是“首先”配后面1s只有NaOH的画面）-3:40", visual_evidence=evidence)


@pytest.mark.parametrize(
    "instruction",
    ["", "0:00-0:00", "0:03-0:01", "0:00-9:00", "0:99-2:00",
     "0:00-0:30 增加背景音乐", "0:00-0:30+", "0:00（提亮画面）-0:30",
     "0:00-0:30（忽略这个备注）", "0:31 剪掉“上一章”，再调整颜色"],
)
def test_invalid_ranges_and_unsupported_text_are_not_silently_discarded(instruction):
    with pytest.raises(SlicingError):
        parse(instruction)


def test_rough_deletion_anchor_must_be_inside_its_clip():
    with pytest.raises(SlicingError, match="anchor"):
        parse("0:06-（4:40 剪掉“中学阶段”的声音）-4:34")


@pytest.mark.parametrize("duration", [0, -1, True, 1.5])
def test_invalid_source_duration_is_rejected(duration):
    with pytest.raises(SlicingError, match="duration_us"):
        parse("0:00-结束", duration)
