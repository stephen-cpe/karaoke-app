"""Automatic QA and needs-review decisions for generated lyric files."""
from __future__ import annotations

import re
import statistics
from typing import Any

from . import textnorm
from .audio import HELD_NOTE_RMS, max_window_rms
from .cues import distribute_words

KANA = re.compile(r"[\u3040-\u30ff]")
KANJI = re.compile(r"[\u3400-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")


def script_count(text: str) -> int:
    return sum(1 for char in text
               if KANA.match(char) or KANJI.match(char) or HANGUL.match(char))


def _line_tokens(line: dict[str, Any], language: str) -> int:
    words = line.get("words") or []
    if words:
        return len(words)
    return max(1, textnorm.token_count(str(line.get("text") or ""), language))


def synthetic_timing(lines: list[dict[str, Any]],
                     language: str = "en") -> dict[str, Any]:
    """Flag lines whose word durations match the old character spread."""
    line_indexes = []
    unmarked_words = 0
    for index, line in enumerate(lines, 1):
        if line.get("instrumental"):
            continue
        words = line.get("words") or []
        if len(words) < 2:
            continue
        tokens = [{"text": word.get("text", ""),
                   "norm": textnorm.norm_token(word.get("text", ""), language)}
                  for word in words]
        if not all(token["norm"] for token in tokens):
            continue
        spread = distribute_words(tokens, float(line["start"]), float(line["end"]))
        if len(spread) != len(words):
            continue
        actual = [float(word["end"]) - float(word["start"]) for word in words]
        expected = [item["end"] - item["start"] for item in spread]
        if all(abs(a - b) <= 0.020 for a, b in zip(actual, expected)):
            line_indexes.append(index)
            unmarked_words += sum(1 for word in words if not word.get("inferred"))
    return {
        "synthetic_lines": len(line_indexes),
        "synthetic_line_indexes": line_indexes,
        "unmarked_synthetic_words": unmarked_words,
    }


def duration_sanity(lines: list[dict[str, Any]], audio=None) -> dict[str, Any]:
    short_words = 0
    long_words = 0
    long_words_supported = 0
    for line in lines:
        for word in line.get("words") or []:
            duration = float(word.get("end", 0.0)) - float(word.get("start", 0.0))
            if duration < 0.04:
                short_words += 1
            if duration > 3.0:
                long_words += 1
                if audio is not None:
                    rms = max_window_rms(
                        audio, float(word.get("start", 0.0)),
                        float(word.get("end", 0.0)))
                    if rms >= HELD_NOTE_RMS:
                        long_words_supported += 1
    return {
        "short_words": short_words,
        "long_words": long_words,
        "long_words_supported_by_energy": (
            long_words_supported if audio is not None else None),
    }


def alignment_metrics(lines: list[dict[str, Any]], language: str) -> dict[str, Any]:
    total_tokens = 0
    aligned_tokens = 0
    line_level = []
    inferred_words = 0
    lyric_lines = 0
    for index, line in enumerate(lines, 1):
        if line.get("instrumental"):
            continue
        lyric_lines += 1
        words = line.get("words") or []
        total_tokens += _line_tokens(line, language)
        aligned_tokens += len(words)
        inferred_words += sum(1 for word in words if word.get("inferred"))
        if not words:
            line_level.append({
                "index": index,
                "reason": str(line.get("line_level_reason")
                              or "no usable word timing"),
            })
    total_words = sum(len(line.get("words") or []) for line in lines
                      if not line.get("instrumental"))
    return {
        "lyric_lines": lyric_lines,
        "total_tokens": total_tokens,
        "aligned_tokens": aligned_tokens,
        "coverage": aligned_tokens / total_tokens if total_tokens else 0.0,
        "inferred_words": inferred_words,
        "inferred_rate": inferred_words / total_words if total_words else 0.0,
        "line_level_lines": len(line_level),
        "line_level_rate": len(line_level) / lyric_lines if lyric_lines else 0.0,
        "line_level": line_level,
    }


def script_issues(lines: list[dict[str, Any]], language: str) -> dict[str, int]:
    issues = 0
    script_lines_outside = 0
    for line in lines:
        if line.get("instrumental"):
            continue
        text = str(line.get("text") or "")
        has_script = bool(script_count(text)) or any(
            script_count(str(word.get("text", "")))
            for word in line.get("words") or [])
        if language in ("ja", "ko"):
            if has_script:
                issues += 1
        elif script_count(text):
            script_lines_outside += 1
    return {
        "script_issues": issues,
        "script_lines_outside_ja_ko": script_lines_outside,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    clean = sorted(float(value) for value in values)
    if not clean:
        return None
    index = min(len(clean) - 1, max(0, int(round(fraction * (len(clean) - 1)))))
    return clean[index]


def line_start_deviation(cues: list[dict[str, Any]],
                         reference: dict[str, Any] | None,
                         language: str) -> dict[str, Any]:
    if not reference:
        return {"median": None, "p90": None, "count": 0}
    lines = reference.get("lines") or []
    offset = float(reference.get("offset") or 0.0)
    deviations = []
    for cue in cues:
        if cue.get("instrumental"):
            continue
        index = cue.get("reference_index")
        if not isinstance(index, int) or not 0 <= index < len(lines):
            continue
        start = lines[index].get("start")
        if start is None:
            continue
        deviations.append(float(cue["start"]) - (float(start) + offset))
    if not deviations:
        return {"median": None, "p90": None, "count": 0}
    return {
        "median": round(statistics.median(deviations), 3),
        "p90": round(_percentile([abs(value) for value in deviations], 0.9), 3),
        "count": len(deviations),
    }


def line_retention(cues: list[dict[str, Any]],
                   reference_line_count: int) -> dict[str, Any]:
    present = len({
        cue.get("reference_index")
        for cue in cues
        if not cue.get("instrumental") and cue.get("reference_index") is not None
    })
    return {
        "reference_lines": reference_line_count,
        "reference_lines_present": present,
        "line_retention": present / reference_line_count if reference_line_count else None,
    }


def evaluate(*, cues: list[dict[str, Any]], language: str, audio,
             reference: dict[str, Any] | None, reference_line_count: int,
             text_source: str, timing_source: str, language_decision: dict[str, Any],
             lrclib_status: str, lrclib_error: str | None = None) -> dict[str, Any]:
    alignment = alignment_metrics(cues, language)
    script = script_issues(cues, language)
    sanity = duration_sanity(cues, audio)
    if text_source == "asr":
        # Whisper timings are measured; the character-spread detector is for
        # the old reference-matching path, not for ASR-only songs.
        synthetic = {
            "synthetic_lines": 0,
            "synthetic_line_indexes": [],
            "unmarked_synthetic_words": 0,
        }
    else:
        synthetic = synthetic_timing(cues, language)
    retention = line_retention(cues, reference_line_count)
    deviation = line_start_deviation(cues, reference, language)

    reasons: list[str] = []
    if text_source == "asr":
        reasons.append("text came from ASR because no usable lyric reference was available")
    if lrclib_status == "unreachable":
        reasons.append(f"LRCLIB was unreachable: {lrclib_error or 'request failed'}")
    if float(language_decision.get("confidence") or 0.0) < 0.6:
        reasons.append(
            f"language detection confidence is low ({language_decision.get('confidence')})")
    if language_decision.get("ambiguous"):
        reasons.append("language detection was ambiguous")
    if language in ("ja", "ko") and script["script_issues"]:
        reasons.append(
            "JA/KO display text still contains native script; romanization is Phase 3")
    if script["script_issues"]:
        reasons.append(f"{script['script_issues']} line(s) still contain CJK/Hangul script")
    if script["script_lines_outside_ja_ko"]:
        reasons.append(
            f"{script['script_lines_outside_ja_ko']} line(s) contain unexpected script")
    if synthetic["unmarked_synthetic_words"]:
        reasons.append(
            f"{synthetic['unmarked_synthetic_words']} word(s) have unmarked synthetic timing")
    if sanity["short_words"]:
        reasons.append(f"{sanity['short_words']} word(s) are shorter than 40 ms")
    if sanity["long_words_supported_by_energy"] is not None:
        unsupported = (sanity["long_words"]
                       - sanity["long_words_supported_by_energy"])
        if unsupported:
            reasons.append(
                f"{unsupported} long word(s) lack vocal-energy support")
    if retention["reference_lines"] and retention["line_retention"] is not None:
        if retention["line_retention"] < 0.95:
            reasons.append(
                f"reference line retention is {retention['line_retention']:.1%}"
                " (target 95%)")
    if reference and alignment["line_level_rate"] > 0.10:
        reasons.append(
            f"line-level fallback rate is {alignment['line_level_rate']:.1%}"
            " (target 10%)")
    if reference and alignment["inferred_rate"] > 0.05:
        reasons.append(
            f"inferred-word rate is {alignment['inferred_rate']:.1%} (target 5%)")

    return {
        "alignment": alignment,
        "script": script,
        "sanity": sanity,
        "synthetic": synthetic,
        "retention": retention,
        "line_start_deviation": deviation,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
    }
