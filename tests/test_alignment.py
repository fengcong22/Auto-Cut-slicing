import json

import pytest

from auto_cut_slicing.alignment import load_alignment, parse_time_us, resolve_word_deletion
from auto_cut_slicing.errors import SlicingError


def alignment(words):
    return {
        "words": [{"text": text, "start_us": start, "end_us": end} for text, start, end in words],
        "sha256": "a" * 64,
        "provider": "fixture",
        "model": "fixture-word-model",
        "adapter": "fixture-v1",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1.234567, 1234567),
        ("01:02.123456", 62123456),
        ("1:02:03.5", 3723500000),
        ("90:00", 5400000000),
    ],
)
def test_exact_microsecond_times(value, expected):
    assert parse_time_us(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, -1, float("nan"), float("inf"), 0.0000001, "1:60", "1:60:00", "12", None]
)
def test_invalid_times_are_not_rounded_or_coerced(value):
    with pytest.raises(SlicingError):
        parse_time_us(value)


def test_chinese_exact_match_ignores_only_punctuation_and_spaces():
    asr = alignment(
        [("我们，", 0, 500000), ("学习", 500000, 1000000), ("历史。", 1100000, 1500000)]
    )
    result = resolve_word_deletion(asr, "我们 学习，历史", clip_start_us=0, clip_end_us=2000000)
    assert (result["source_start_us"], result["source_end_us"]) == (0, 1500000)
    assert result["word_indices"] == [0, 1, 2]


def test_partial_asr_token_is_rejected():
    asr = alignment([("历史文化", 0, 1000000)])
    with pytest.raises(SlicingError, match="found 0"):
        resolve_word_deletion(asr, "历史", clip_start_us=0, clip_end_us=2000000)


def test_repeated_phrases_require_a_unique_time_anchored_candidate():
    asr = alignment([("对吧", 1000000, 1500000), ("对吧", 10000000, 10500000)])
    with pytest.raises(SlicingError, match="found 2"):
        resolve_word_deletion(asr, "对吧", clip_start_us=0, clip_end_us=11000000)
    selected = resolve_word_deletion(
        asr,
        "对吧",
        clip_start_us=0,
        clip_end_us=11000000,
        rough_time_us=9800000,
        search_radius_us=1000000,
    )
    assert selected["source_start_us"] == 10000000
    with pytest.raises(SlicingError, match="found 2"):
        resolve_word_deletion(
            asr, "对吧", clip_start_us=0, clip_end_us=11000000, rough_time_us=9800000
        )


def test_long_silence_or_intervening_word_breaks_contiguity():
    for asr in (
        alignment([("我们", 0, 500000), ("学习", 2000001, 2500000)]),
        alignment([("我们", 0, 500000), ("今天", 500000, 1000000), ("学习", 1000000, 1500000)]),
    ):
        with pytest.raises(SlicingError, match="found 0"):
            resolve_word_deletion(asr, "我们学习", clip_start_us=0, clip_end_us=3000000)


def test_must_keep_overlap_and_unverified_phrase_fail():
    asr = alignment([("我们", 0, 500000), ("学习", 500000, 1000000), ("历史", 1000000, 1500000)])
    with pytest.raises(SlicingError, match="overlap must_keep"):
        resolve_word_deletion(
            asr, "学习", clip_start_us=0, clip_end_us=2000000, must_keep=["我们学习"]
        )
    with pytest.raises(SlicingError, match="could not be verified"):
        resolve_word_deletion(asr, "学习", clip_start_us=0, clip_end_us=2000000, must_keep=["未来"])
    result = resolve_word_deletion(
        asr, "学习", clip_start_us=0, clip_end_us=2000000, must_keep=["历史"]
    )
    assert result["must_keep_evidence"][0]["text"] == "历史"


def test_hash_bound_alignment_rejects_wrong_source_and_invalid_word_order(tmp_path):
    path = tmp_path / "asr.json"
    raw = {
        "schema_version": 1,
        "source_sha256": "a" * 64,
        "provider": "test",
        "model": "test",
        "adapter": "test",
        "words": [{"text": "词", "start": 0, "end": 1}],
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SlicingError, match="does not match"):
        load_alignment(path, {"sha256": "b" * 64, "duration_us": 10000000})
    raw["words"].append({"text": "字", "start": 0.5, "end": 1.5})
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SlicingError, match="overlapping"):
        load_alignment(path, {"sha256": "a" * 64, "duration_us": 10000000})
    raw["words"] = [{"text": "词", "start": 0, "end": 1}]
    raw["model"] = "  "
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SlicingError, match="model identity"):
        load_alignment(path, {"sha256": "a" * 64, "duration_us": 10000000})
