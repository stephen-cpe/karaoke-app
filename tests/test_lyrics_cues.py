"""Cue-building tests for line-level fallback and inferred gap timing."""
from __future__ import annotations

from tools.generate_lyrics import _reject_suspect_reference_offset
from tools.lyrics import cues


def _reference():
    return {
        "lines": [
            {"start": 0.0, "text": "Hello world"},
            {"start": 4.0, "text": "Goodbye now"},
        ],
        "tokens": [
            {"text": "Hello", "norm": "hello", "line": 0, "token": 0,
             "line_start": 0.0},
            {"text": "world", "norm": "world", "line": 0, "token": 1,
             "line_start": 0.0},
            {"text": "Goodbye", "norm": "goodbye", "line": 1, "token": 0,
             "line_start": 4.0},
            {"text": "now", "norm": "now", "line": 1, "token": 1,
             "line_start": 4.0},
        ],
        "token_local": {0: 0},
        "anchors": {0: 0.0},
        "local": [{"text": "Hello", "start": 0.0, "end": 0.8}],
        "offset": 0.0,
    }


def test_low_coverage_line_falls_back_to_line_level():
    built = cues.build_lyric_lines(_reference(), 8.0, audio=None, language="en")
    assert len(built) == 2
    assert built[0]["text"] == "Hello world"
    assert built[0]["words"] == []
    assert built[0]["line_level_reason"]
    assert built[1]["words"] == []


def test_single_missing_word_is_interpolated_and_marked_inferred():
    reference = {
        "lines": [{"start": 0.0, "text": "one two three"}],
        "tokens": [
            {"text": "one", "norm": "one", "line": 0, "token": 0,
             "line_start": 0.0},
            {"text": "two", "norm": "two", "line": 0, "token": 1,
             "line_start": 0.0},
            {"text": "three", "norm": "three", "line": 0, "token": 2,
             "line_start": 0.0},
        ],
        "token_local": {0: 0, 2: 1},
        "anchors": {0: 0.0},
        "local": [
            {"text": "one", "start": 0.0, "end": 0.5},
            {"text": "three", "start": 1.0, "end": 1.5},
        ],
        "offset": 0.0,
    }
    built = cues.build_lyric_lines(reference, 2.0, audio=None, language="en")
    assert len(built) == 1
    words = built[0]["words"]
    assert [word["text"] for word in words] == ["one", "two", "three"]
    assert words[1]["inferred"] is True
    assert words[0].get("inferred") is None
    assert words[2].get("inferred") is None


def test_distribute_words_is_always_marked_inferred():
    words = cues.distribute_words(
        [{"text": "a", "norm": "a"}, {"text": "bb", "norm": "bb"}], 0.0, 2.0)
    assert all(word["inferred"] is True for word in words)


def test_build_from_transcript_text_matches_word_texts():
    transcript = {
        "segments": [{
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 1.0},
            ],
        }],
    }
    lines = cues.build_from_transcript(transcript, 2.0, language="en")
    assert len(lines) == 1
    assert lines[0]["text"] == "Hello world"
    assert " ".join(word["text"] for word in lines[0]["words"]) == lines[0]["text"]


def test_line_timing_is_clamped_to_duration():
    reference = {
        "lines": [{"start": 10.0, "text": "Hello"}],
        "tokens": [{"text": "Hello", "norm": "hello", "line": 0, "token": 0,
                    "line_start": 10.0}],
        "token_local": {},
        "anchors": {},
        "local": [],
        "offset": 0.0,
    }
    built = cues.build_lyric_lines(reference, 5.0, audio=None, language="en")
    assert built
    assert built[0]["start"] <= 5.0
    assert built[0]["end"] <= 5.0


def test_implausible_reference_offset_is_rejected():
    reference = {
        "offset": 45.3,
        "anchors": {1: 2.0},
        "token_local": {0: 0},
    }
    report = {}
    _reject_suspect_reference_offset(reference, report)
    assert reference["offset"] == 0.0
    assert reference["anchors"] == {}
    assert reference["token_local"] == {}
    assert reference["alignment_offset_reset"] == 45.3
    assert report["warnings"]
