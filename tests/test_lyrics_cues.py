"""Cue-building tests for line-level fallback and inferred gap timing."""
from __future__ import annotations

import pytest

from tools.generate_lyrics import _needs_full_asr, _reject_suspect_reference_offset
from tools.lyrics import cues


def test_needs_full_asr_language_paths():
    assert _needs_full_asr("tl", True, False)[0] is False
    assert _needs_full_asr("en", True, False)[0] is True
    assert _needs_full_asr("en", True, True)[0] is False
    assert _needs_full_asr("ja", True, False)[0] is True
    assert _needs_full_asr("en", False, False)[0] is True


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


def test_trim_leading_alignment_gap_only_moves_silent_leading_words(monkeypatch):
    monkeypatch.setattr(cues, "max_window_rms", lambda *args, **kwargs: 0.001)
    words = [
        {"start": 10.0, "end": 30.0},
        {"start": 30.0, "end": 30.5},
    ]
    cues._trim_leading_alignment_gap(words, 29.0, audio=object())
    assert words[0]["start"] == 29.0

    held = [
        {"start": 10.0, "end": 30.0},
        {"start": 30.0, "end": 30.5},
    ]
    monkeypatch.setattr(cues, "max_window_rms", lambda *args, **kwargs: 0.5)
    cues._trim_leading_alignment_gap(held, 29.0, audio=object())
    assert held[0]["start"] == 10.0


def test_build_aligned_asr_lines_uses_whisper_only_for_failed_line():
    asr_lines = [{
        "start": 0.0,
        "end": 2.0,
        "text": "hello world",
        "words": [
            {"text": "hello", "start": 0.0, "end": 1.0},
            {"text": "world", "start": 1.0, "end": 2.0},
        ],
    }]
    failed = cues.build_aligned_asr_lines(
        asr_lines, [{"coverage": 0.0, "words": []}], 10.0,
        audio=None, language="en")
    assert len(failed) == 1
    assert failed[0]["timing_source"] == "asr"
    assert failed[0]["line_fallback_reason"]
    assert [word["text"] for word in failed[0]["words"]] == ["hello", "world"]

    aligned = cues.build_aligned_asr_lines(
        asr_lines, [{"coverage": 1.0, "words": [
            {"token": 0, "aligned": True, "start": 0.0, "end": 0.5},
            {"token": 1, "aligned": True, "start": 0.5, "end": 1.0},
        ]}], 10.0, audio=None, language="en")
    assert len(aligned) == 1
    assert aligned[0]["timing_source"] == "forced-alignment"
    assert [word["text"] for word in aligned[0]["words"]] == ["hello", "world"]


def test_per_line_windows_cover_each_timed_line():
    from tools.lyrics import align

    lines = [
        {"start": 10.0, "end": 12.0, "text": "one"},
        {"start": 12.5, "end": 14.0, "text": "two"},
    ]
    windows = align.build_per_line_windows(lines, 20.0, audio=None)
    assert len(windows) == 2
    assert windows[0][2] == [0]
    assert windows[1][2] == [1]
    assert windows[0][0] < 10.0
    assert windows[0][1] > 12.0


def test_instrumental_gap_with_vocals_extends_previous_cue(monkeypatch):
    cues_list = [
        {"start": 0.0, "end": 2.0, "text": "hello", "words": [],
         "timing_source": "forced-alignment"},
        {"start": 8.0, "end": 10.0, "text": "world", "words": [],
         "timing_source": "forced-alignment"},
    ]
    monkeypatch.setattr(cues, "_gap_is_vocal", lambda *args: True)
    built = cues.add_instrumental_cues(cues_list, 10.0, audio=object())
    assert [cue["text"] for cue in built] == ["hello", "world"]
    assert built[0]["end"] == 7.8


def test_gap_is_vocal_ignores_sparse_bleed(monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(
        cues, "window_rms",
        lambda *args, **kwargs: (
            np.array([0, 1, 2, 3, 4]),
            np.array([0.1, 0.001, 0.001, 0.001, 0.001])))
    assert cues._gap_is_vocal(object(), 0.0, 5.0) is False
    monkeypatch.setattr(
        cues, "window_rms",
        lambda *args, **kwargs: (
            np.array([0, 1, 2, 3]), np.array([0.1, 0.1, 0.1, 0.001])))
    assert cues._gap_is_vocal(object(), 0.0, 4.0) is True


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
