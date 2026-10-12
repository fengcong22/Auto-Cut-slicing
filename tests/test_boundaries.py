import copy
import json

import pytest

from auto_cut_slicing import boundaries
from auto_cut_slicing.alignment import load_alignment
from auto_cut_slicing.boundaries import resolve_sentence_range
from auto_cut_slicing.errors import SlicingError


def manifest(tmp_path, utterances, *, duration_us=20_000_000, mutate=None):
    rows = [
        {
            "text": text,
            # Deliberately coarse: sentence cuts must come from word timestamps.
            "start": 0,
            "end": duration_us / 1_000_000,
            "words": [{"text": word, "start": start, "end": end} for word, start, end in words],
        }
        for text, words in utterances
    ]
    raw = {
        "schema_version": 1,
        "source_sha256": "a" * 64,
        "provider": "fixture",
        "model": "fixture-word-model",
        "adapter": "fixture-v1",
        "words": copy.deepcopy([word for row in rows for word in row["words"]]),
        "utterances": rows,
    }
    if mutate:
        mutate(raw)
    path = tmp_path / "source.words.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return load_alignment(path, {"sha256": "a" * 64, "duration_us": duration_us})


@pytest.fixture
def two_sentences(tmp_path):
    return manifest(
        tmp_path,
        [
            (
                " 第一，句。 第二句！ ",
                [("第一", 1, 2), ("句", 2.1, 3), ("第二", 4, 5), ("句", 5.1, 6)],
            )
        ],
    )


def test_expand_numeric_range_and_record_exact_sentence_evidence(two_sentences):
    result = resolve_sentence_range(
        two_sentences, start_us=1_500_000, end_us=5_500_000, duration_us=20_000_000
    )
    assert (result["start_us"], result["end_us"]) == (1_000_000, 6_000_000)
    assert result["evidence"] == [
        {
            "side": "start",
            "requested_us": 1_500_000,
            "resolved_us": 1_000_000,
            "sentence_text": " 第一，句。",
            "sentence_start_us": 1_000_000,
            "sentence_end_us": 3_000_000,
            "word_indices": [0, 1],
            "source_sha256": two_sentences["source_sha256"],
            "alignment_sha256": two_sentences["sha256"],
            "provider": "fixture",
            "model": "fixture-word-model",
            "adapter": "fixture-v1",
            "strategy": "sentence_terminal_whole_token",
        },
        {
            "side": "end",
            "requested_us": 5_500_000,
            "resolved_us": 6_000_000,
            "sentence_text": " 第二句！ ",
            "sentence_start_us": 4_000_000,
            "sentence_end_us": 6_000_000,
            "word_indices": [2, 3],
            "source_sha256": two_sentences["source_sha256"],
            "alignment_sha256": two_sentences["sha256"],
            "provider": "fixture",
            "model": "fixture-word-model",
            "adapter": "fixture-v1",
            "strategy": "sentence_terminal_whole_token",
        },
    ]


@pytest.mark.parametrize("boundary_us", [2_000_000, 2_050_000, 2_100_000])
def test_internal_word_edges_and_gaps_are_inside_sentence(two_sentences, boundary_us):
    result = resolve_sentence_range(
        two_sentences,
        start_us=boundary_us,
        end_us=5_050_000,
        duration_us=20_000_000,
    )
    assert (result["start_us"], result["end_us"]) == (1_000_000, 6_000_000)


def test_long_pause_does_not_invent_a_sentence_end(tmp_path):
    asr = manifest(tmp_path, [("这是，同一句。", [("这是", 1, 2), ("同一句", 9, 10)])])
    result = resolve_sentence_range(
        asr, start_us=5_000_000, end_us=9_500_000, duration_us=20_000_000
    )
    assert (result["start_us"], result["end_us"]) == (1_000_000, 10_000_000)


@pytest.mark.parametrize(
    ("start_us", "end_us"),
    [(0, 20_000_000), (1_000_000, 3_000_000), (3_000_000, 4_000_000), (3_500_000, 6_000_000)],
)
def test_container_sentence_edges_and_intersentence_silence_stay_exact(
    two_sentences, start_us, end_us
):
    assert resolve_sentence_range(
        two_sentences, start_us=start_us, end_us=end_us, duration_us=20_000_000
    ) == {"start_us": start_us, "end_us": end_us, "evidence": []}


def test_cue_start_is_preserved_while_numeric_end_expands(two_sentences):
    result = resolve_sentence_range(
        two_sentences,
        start_us=2_100_000,
        end_us=5_200_000,
        duration_us=20_000_000,
        preserve_start=True,
    )
    assert (result["start_us"], result["end_us"]) == (2_100_000, 6_000_000)
    assert [row["side"] for row in result["evidence"]] == ["end"]


def test_container_endpoints_need_no_sentence_evidence_or_manifest_io(two_sentences):
    # Exempt endpoints remain usable even if the manifest is unavailable.
    two_sentences["path"] = "does-not-exist.json"
    assert (
        resolve_sentence_range(
            two_sentences, start_us=0, end_us=20_000_000, duration_us=20_000_000
        )["evidence"]
        == []
    )
    assert (
        resolve_sentence_range(
            two_sentences,
            start_us=2_100_000,
            end_us=20_000_000,
            duration_us=20_000_000,
            preserve_start=True,
        )["start_us"]
        == 2_100_000
    )


def test_merge_utterances_until_actual_terminal_punctuation(tmp_path):
    asr = manifest(
        tmp_path,
        [("第一部分，", [("第一部分", 1, 2)]), ("接着讲完。", [("接着", 3, 4), ("讲完", 4, 5)])],
    )
    result = resolve_sentence_range(
        asr, start_us=3_500_000, end_us=4_500_000, duration_us=20_000_000
    )
    assert (result["start_us"], result["end_us"]) == (1_000_000, 5_000_000)
    assert result["evidence"][0]["sentence_text"] == "第一部分，接着讲完。"
    assert result["evidence"][0]["word_indices"] == [0, 1, 2]


def test_closing_quotes_and_multiple_terminal_marks_preserve_original_text(tmp_path):
    asr = manifest(
        tmp_path,
        [("他说：“好？！” 下一句。", [("他说", 1, 2), ("好", 2.1, 3), ("下一句", 4, 5)])],
    )
    result = resolve_sentence_range(
        asr, start_us=1_500_000, end_us=4_500_000, duration_us=20_000_000
    )
    assert [row["sentence_text"] for row in result["evidence"]] == [
        "他说：“好？！”",
        " 下一句。",
    ]


@pytest.mark.parametrize(
    ("text", "words", "end_us", "expected_end_us"),
    [
        ("Dr. Smith.", [("Dr.", 1, 2), ("Smith", 2.1, 3)], 1_500_000, 3_000_000),
        (
            "The U.S. team wins.",
            [("The", 1, 2), ("U.S.", 2.1, 3), ("team", 3.1, 4), ("wins", 4.1, 5)],
            2_500_000,
            5_000_000,
        ),
        (
            "Value 3.14 is stable.",
            [("Value", 1, 2), ("3.14", 2.1, 3), ("is", 3.1, 4), ("stable", 4.1, 5)],
            2_500_000,
            5_000_000,
        ),
        (
            "Meet at 10 a.m. tomorrow.",
            [("Meet", 1, 2), ("at", 2, 3), ("10", 3, 4), ("a.m.", 4, 5), ("tomorrow", 5, 6)],
            4_500_000,
            6_000_000,
        ),
    ],
)
def test_decimal_and_abbreviation_dots_do_not_close_sentences(
    tmp_path, text, words, end_us, expected_end_us
):
    asr = manifest(tmp_path, [(text, words)])
    result = resolve_sentence_range(asr, start_us=0, end_us=end_us, duration_us=20_000_000)
    assert result["end_us"] == expected_end_us
    assert result["evidence"][0]["sentence_text"] == text


@pytest.mark.parametrize("text", ["没有句末", "Meet Dr.", "A pause...", "词，"])
def test_unclosed_or_ambiguous_final_sentence_fails(tmp_path, text):
    asr = manifest(tmp_path, [(text, [(text, 1, 2)])])
    with pytest.raises(SlicingError, match="sentence|terminal"):
        resolve_sentence_range(asr, start_us=1_100_000, end_us=1_500_000, duration_us=20_000_000)


def test_unclosed_trailing_speech_fails_even_if_selected_sentence_is_closed(tmp_path):
    asr = manifest(tmp_path, [("完整。未完", [("完整", 1, 2), ("未完", 3, 4)])])
    with pytest.raises(SlicingError, match="sentence|terminal"):
        resolve_sentence_range(asr, start_us=1_100_000, end_us=1_500_000, duration_us=20_000_000)


def test_terminal_punctuation_must_not_split_a_word_token(tmp_path):
    asr = manifest(tmp_path, [("第一句。第二句。", [("第一句第二句", 1, 3)])])
    with pytest.raises(SlicingError, match="token"):
        resolve_sentence_range(asr, start_us=1_500_000, end_us=2_500_000, duration_us=20_000_000)


def test_english_sentences_without_spaces_still_use_complete_word_edges(tmp_path):
    asr = manifest(tmp_path, [("Hello.World.", [("Hello", 1, 2), ("World", 3, 4)])])
    result = resolve_sentence_range(
        asr, start_us=1_500_000, end_us=3_500_000, duration_us=20_000_000
    )
    assert (result["start_us"], result["end_us"]) == (1_000_000, 4_000_000)
    assert [row["sentence_text"] for row in result["evidence"]] == ["Hello.", "World."]


def test_english_sentence_dot_inside_one_token_fails(tmp_path):
    asr = manifest(tmp_path, [("Hello.World.", [("Hello.World", 1, 3)])])
    with pytest.raises(SlicingError, match="token"):
        resolve_sentence_range(asr, start_us=1_500_000, end_us=2_500_000, duration_us=20_000_000)


def test_none_alignment_is_allowed_only_when_sentence_checks_are_exempt():
    assert resolve_sentence_range(None, start_us=0, end_us=20, duration_us=20) == {
        "start_us": 0,
        "end_us": 20,
        "evidence": [],
    }
    with pytest.raises(SlicingError, match="alignment"):
        resolve_sentence_range(None, start_us=0, end_us=10, duration_us=20)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("utterances"),
        lambda raw: raw.update(utterances=[]),
        lambda raw: raw.update(utterances="not a list"),
    ],
)
def test_missing_sentence_evidence_fails_for_noncontainer_endpoint(tmp_path, mutate):
    asr = manifest(tmp_path, [("完整。", [("完整", 1, 2)])], mutate=mutate)
    with pytest.raises(SlicingError, match="utterance|sentence"):
        resolve_sentence_range(asr, start_us=0, end_us=1_500_000, duration_us=20_000_000)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["utterances"][0]["words"][0].update(text="不一致"),
        lambda raw: raw["utterances"][0]["words"][0].update(start=1.1),
        lambda raw: raw["utterances"][0]["words"][0].update(end=1.9),
        lambda raw: raw["utterances"][0]["words"].append({"text": "额外", "start": 3, "end": 4}),
    ],
)
def test_flattened_utterance_words_must_exactly_match_global_alignment(tmp_path, mutate):
    asr = manifest(tmp_path, [("完整。", [("完整", 1, 2)])], mutate=mutate)
    with pytest.raises(SlicingError, match="word|match"):
        resolve_sentence_range(asr, start_us=0, end_us=1_500_000, duration_us=20_000_000)


def test_normalized_utterance_text_must_match_all_its_words(tmp_path):
    asr = manifest(
        tmp_path,
        [("完整。", [("完整", 1, 2)])],
        mutate=lambda raw: raw["utterances"][0].update(text="遗漏内容完整。"),
    )
    with pytest.raises(SlicingError, match="text|match"):
        resolve_sentence_range(asr, start_us=0, end_us=1_500_000, duration_us=20_000_000)


def test_manifest_changed_since_alignment_load_is_rejected_before_read(two_sentences, monkeypatch):
    from pathlib import Path

    Path(two_sentences["path"]).write_text("{}", encoding="utf-8")

    def forbidden_read(_path):
        raise AssertionError("hash must be verified before parsing changed evidence")

    monkeypatch.setattr(boundaries, "read_json", forbidden_read)
    with pytest.raises(SlicingError, match="hash|SHA-256|changed"):
        resolve_sentence_range(two_sentences, start_us=0, end_us=2_500_000, duration_us=20_000_000)


def test_manifest_changed_while_reading_is_rejected_after_read(two_sentences, monkeypatch):
    from pathlib import Path

    original_read = boundaries.read_json

    def changing_read(path):
        raw = original_read(path)
        Path(path).write_text("{}", encoding="utf-8")
        return raw

    monkeypatch.setattr(boundaries, "read_json", changing_read)
    with pytest.raises(SlicingError, match="hash|SHA-256|changed"):
        resolve_sentence_range(two_sentences, start_us=0, end_us=2_500_000, duration_us=20_000_000)


def test_alignment_replaced_after_read_cannot_certify_foreign_sentence_evidence(tmp_path, monkeypatch):
    from pathlib import Path

    from auto_cut_slicing import alignment as alignment_module

    original_read = alignment_module.read_json

    def replace_after_read(path):
        raw = original_read(path)
        replacement = copy.deepcopy(raw)
        replacement['source_sha256'] = 'b' * 64
        replacement['utterances'][0]['text'] = '第一。内容。'
        Path(path).write_text(json.dumps(replacement, ensure_ascii=False), encoding='utf-8')
        return raw

    monkeypatch.setattr(alignment_module, 'read_json', replace_after_read)
    with pytest.raises(SlicingError, match='changed|identity|source_sha256'):
        asr = manifest(tmp_path, [('第一内容。', [('第一', 1, 2), ('内容', 3, 4)])])
        resolve_sentence_range(asr, start_us=0, end_us=1_500_000, duration_us=20_000_000)


@pytest.mark.parametrize('key', ['source_sha256', 'provider', 'model', 'adapter'])
def test_reread_sentence_identity_must_match_validated_alignment(two_sentences, key):
    two_sentences[key] = 'b' * 64 if key == 'source_sha256' else 'different-provider'
    with pytest.raises(SlicingError, match='identity|source_sha256'):
        resolve_sentence_range(two_sentences, start_us=0, end_us=2_500_000, duration_us=20_000_000)


@pytest.mark.parametrize('title', ['Capt', 'Gen', 'Sgt', 'Gov', 'Rev', 'Lt', 'Col', 'Sen', 'Rep'])
def test_common_title_abbreviation_does_not_cut_off_english_predicate(tmp_path, title):
    asr = manifest(tmp_path, [(f'The {title}. spoke clearly.', [
        ('The', 1, 2), (f'{title}.', 2.1, 3), ('spoke', 3.1, 4), ('clearly', 4.1, 5),
    ])])
    result = resolve_sentence_range(asr, start_us=0, end_us=2_500_000, duration_us=20_000_000)
    assert result['end_us'] == 5_000_000


@pytest.mark.parametrize(
    ("start_us", "end_us", "duration_us"),
    [(True, 2, 3), (0, 2.5, 3), (-1, 2, 3), (2, 2, 3), (2, 1, 3), (0, 4, 3), (0, 1, 0)],
)
def test_invalid_ranges_fail_without_coercion(two_sentences, start_us, end_us, duration_us):
    with pytest.raises(SlicingError, match="range|microsecond|duration"):
        resolve_sentence_range(
            two_sentences, start_us=start_us, end_us=end_us, duration_us=duration_us
        )
