"""Regression guards for the lyric text/timing path.

These tests pin the English behavior that must stay stable and the Phase 1
fixes for non-Latin normalization and inferred-timing provenance.
"""
import pytest

from tools.generate_lyrics import (
    add_instrumental_cues,
    build_from_transcript,
    build_lyric_lines,
    chunk_ranges,
    distribute_words,
    norm_token,
    parse_lrc,
    split_display_tokens,
)


def test_norm_token_legacy_english():
    assert norm_token("I'm") == "im"
    assert norm_token("I? m") == "im"
    assert norm_token("Hello, world!") == "helloworld"
    assert norm_token("Café") == "caf"
    assert norm_token("don't") == "dont"


def test_split_display_tokens_english():
    tokens = split_display_tokens("Hello, world! I'm here.")
    assert [token["text"] for token in tokens] == [
        "Hello,", "world!", "I'm", "here."]
    assert [token["norm"] for token in tokens] == [
        "hello", "world", "im", "here"]

    punctuation = split_display_tokens("Hello , world")
    assert [token["text"] for token in punctuation] == ["Hello,", "world"]


def test_parse_lrc_basic():
    lines = parse_lrc(
        "[00:01.00]Hello world\n"
        "[00:03.50]Second line\n"
        "[00:05.00]\n"
    )
    assert [(line["start"], line["text"]) for line in lines] == [
        (1.0, "Hello world"),
        (3.5, "Second line"),
    ]


def test_chunk_ranges_partition_song():
    ranges = chunk_ranges(120.0)
    assert ranges[0][0] == 0.0
    assert ranges[-1][1] == 120.0
    for previous, current in zip(ranges, ranges[1:]):
        assert previous[3] <= current[2] + 1e-9


def test_add_instrumental_cues_inserts_gap():
    cues = [
        {"start": 0.0, "end": 2.0, "text": "First", "words": [], "instrumental": False},
        {"start": 8.0, "end": 10.0, "text": "Second", "words": [], "instrumental": False},
    ]
    result = add_instrumental_cues(cues, 12.0, audio=None)
    assert [cue["text"] for cue in result] == [
        "First", "[Instrumental]", "Second"]
    assert result[1]["instrumental"] is True
    assert result[1]["start"] == 2.2
    assert result[1]["end"] == 7.8


def test_add_instrumental_cues_skips_short_gap():
    cues = [
        {"start": 0.0, "end": 2.0, "text": "First", "words": [], "instrumental": False},
        {"start": 4.0, "end": 6.0, "text": "Second", "words": [], "instrumental": False},
    ]
    result = add_instrumental_cues(cues, 7.0, audio=None)
    assert [cue["text"] for cue in result] == ["First", "Second"]


def test_build_lyric_lines_english_with_known_words():
    reference = {
        "lines": [
            {"start": 0.0, "text": "Hello world"},
            {"start": 4.0, "text": "Goodbye now"},
        ],
        "tokens": [
            {"text": "Hello", "norm": "hello", "line": 0, "token": 0, "line_start": 0.0},
            {"text": "world", "norm": "world", "line": 0, "token": 1, "line_start": 0.0},
            {"text": "Goodbye", "norm": "goodbye", "line": 1, "token": 0, "line_start": 4.0},
            {"text": "now", "norm": "now", "line": 1, "token": 1, "line_start": 4.0},
        ],
        "token_local": {0: 0, 1: 1, 2: 2, 3: 3},
        "anchors": {0: 0.0, 1: 4.0},
        "local": [
            {"text": "Hello", "start": 0.0, "end": 0.8},
            {"text": "world", "start": 0.8, "end": 1.6},
            {"text": "Goodbye", "start": 4.0, "end": 4.8},
            {"text": "now", "start": 4.8, "end": 5.6},
        ],
        "offset": 0.0,
    }
    built = build_lyric_lines(reference, 8.0, audio=None)
    assert [line["text"] for line in built] == ["Hello world", "Goodbye now"]
    assert [word["text"] for word in built[0]["words"]] == ["Hello", "world"]
    assert built[0]["start"] == pytest.approx(0.0)
    assert built[0]["words"][-1]["end"] == pytest.approx(1.6)


def test_distribute_words_legacy_character_proportional():
    tokens = [
        {"text": "a", "norm": "a"},
        {"text": "bb", "norm": "bb"},
        {"text": "ccc", "norm": "ccc"},
    ]
    words = distribute_words(tokens, 0.0, 6.0)
    assert [round(word["start"], 3) for word in words] == [0.0, 1.0, 3.0]
    assert [round(word["end"], 3) for word in words] == [1.0, 3.0, 6.0]


def test_norm_token_keeps_japanese():
    assert norm_token("君の名前") != ""


def test_norm_token_keeps_korean():
    assert norm_token("사랑해") != ""


def test_build_from_transcript_keeps_non_latin():
    transcript = {
        "segments": [{
            "words": [{
                "word": "君の名前",
                "start": 0.0,
                "end": 1.0,
            }],
        }],
    }
    lines = build_from_transcript(transcript, 2.0, language="ja")
    assert len(lines) == 1
    assert lines[0]["text"] != ""


def test_distribute_words_marks_fabricated_timing():
    tokens = [
        {"text": "hello", "norm": "hello"},
        {"text": "world", "norm": "world"},
    ]
    words = distribute_words(tokens, 0.0, 2.0)
    assert all(word.get("inferred") is True for word in words)
