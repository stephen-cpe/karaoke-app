"""QA metric tests: synthetic timing, retention, script, and review reasons."""
from __future__ import annotations

from tools.lyrics import cues, qa


def _line(words, start=0.0, end=3.0, text=None, language="en"):
    return {
        "start": start,
        "end": end,
        "text": text or " ".join(word["text"] for word in words),
        "instrumental": False,
        "words": words,
    }


def test_synthetic_detector_ignores_marked_inferred_words():
    tokens = [
        {"text": "aa", "norm": "aa"},
        {"text": "bb", "norm": "bb"},
        {"text": "cc", "norm": "cc"},
    ]
    words = cues.distribute_words(tokens, 0.0, 3.0)
    line = _line(words)
    metrics = qa.synthetic_timing([line])
    assert metrics["synthetic_lines"] == 1
    assert metrics["unmarked_synthetic_words"] == 0

    for word in words:
        word.pop("inferred", None)
    metrics = qa.synthetic_timing([line])
    assert metrics["synthetic_lines"] == 1
    assert metrics["unmarked_synthetic_words"] == 3


def test_line_retention_counts_present_reference_lines():
    lines = [
        _line([{"text": "a", "start": 0.0, "end": 0.5}],
              text="a"),
        _line([], start=1.0, end=2.0, text="b"),
    ]
    lines[0]["reference_index"] = 0
    lines[1]["reference_index"] = 1
    metrics = qa.line_retention(lines, 3)
    assert metrics["reference_lines"] == 3
    assert metrics["reference_lines_present"] == 2
    assert metrics["line_retention"] == 2 / 3


def test_script_issue_counts_for_target_language_and_outside_it():
    ja_line = _line([{"text": "君", "start": 0.0, "end": 0.5}],
                    text="君の名前")
    metrics = qa.script_issues([ja_line], "ja")
    assert metrics["script_issues"] == 1
    metrics = qa.script_issues([ja_line], "en")
    assert metrics["script_lines_outside_ja_ko"] == 1


def test_duration_sanity_without_audio():
    line = _line([
        {"text": "short", "start": 0.0, "end": 0.01},
        {"text": "long", "start": 1.0, "end": 4.5},
    ], end=5.0)
    metrics = qa.duration_sanity([line], audio=None)
    assert metrics["short_words"] == 1
    assert metrics["long_words"] == 1
    assert metrics["long_words_supported_by_energy"] is None


def test_asr_only_evaluation_needs_review():
    line = _line([{"text": "hello", "start": 0.0, "end": 0.5}],
                 text="hello", end=1.0)
    result = qa.evaluate(
        cues=[line],
        language="en",
        audio=None,
        reference=None,
        reference_line_count=0,
        text_source="asr",
        timing_source="asr",
        language_decision={"confidence": 0.4, "ambiguous": True, "inputs": {}},
        lrclib_status="not_found",
        lrclib_error=None,
    )
    assert result["needs_review"] is True
    assert any("ASR" in reason for reason in result["review_reasons"])
    assert any("language" in reason.lower()
               for reason in result["review_reasons"])


def test_synthetic_detector_skips_measured_asr_timings():
    tokens = [
        {"text": "aa", "norm": "aa"},
        {"text": "bb", "norm": "bb"},
        {"text": "cc", "norm": "cc"},
    ]
    words = cues.distribute_words(tokens, 0.0, 3.0)
    for word in words:
        word.pop("inferred", None)
    line = _line(words)
    asr_result = qa.evaluate(
        cues=[line], language="en", audio=None, reference=None,
        reference_line_count=0, text_source="asr", timing_source="asr",
        language_decision={"confidence": 0.9, "ambiguous": False, "inputs": {}},
        lrclib_status="not_found")
    assert asr_result["synthetic"]["synthetic_lines"] == 0
    reference_result = qa.evaluate(
        cues=[line], language="en", audio=None, reference=None,
        reference_line_count=0, text_source="lrclib-synced",
        timing_source="asr",
        language_decision={"confidence": 0.9, "ambiguous": False, "inputs": {}},
        lrclib_status="ok")
    assert reference_result["synthetic"]["synthetic_lines"] == 1
    assert reference_result["synthetic"]["unmarked_synthetic_words"] == 3
