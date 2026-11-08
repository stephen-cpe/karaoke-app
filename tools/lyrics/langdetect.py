"""Song-level language decisions from reference script, lexicon, and Whisper LID."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

KANA = re.compile(r"[\u3040-\u30ff]")
KANJI = re.compile(r"[\u3400-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
LATIN_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿñÑ']+")

# Distinctive Tagalog function words. Short sung syllables such as na/pa/ka/
# mo/sa/ay/ko/lang are deliberately weak: English choruses use "na na na" too.
STRONG_TAGALOG = frozenset("""
ang ng mga ikaw ako hindi kung yung kasi para wala mahal naman din rin
tayo kami kayo sila natin ninyo nila ito iyan iyon dito diyan doon
mayroon meron walang akin iyo kanya kanila namin po opo
""".split())
WEAK_TAGALOG = frozenset("""
na pa ka mo sa ay ko lang may ho oo si ni kay
""".split())


def script_summary(text_or_lines: str | list[str]) -> dict[str, Any]:
    lines = ([text_or_lines] if isinstance(text_or_lines, str)
             else list(text_or_lines))
    counts = Counter()
    lines_with = Counter()
    for line in lines:
        line_counts = Counter()
        for char in str(line):
            if KANA.match(char):
                line_counts["kana"] += 1
            elif KANJI.match(char):
                line_counts["kanji"] += 1
            elif HANGUL.match(char):
                line_counts["hangul"] += 1
            elif char.isalpha() and char.isascii():
                line_counts["latin"] += 1
            elif char.isdigit():
                line_counts["digit"] += 1
        counts.update(line_counts)
        for key in ("kana", "kanji", "hangul", "latin"):
            if line_counts[key]:
                lines_with[key] += 1
    counts["lines"] = len(lines)
    return {
        "kana": counts.get("kana", 0),
        "kanji": counts.get("kanji", 0),
        "hangul": counts.get("hangul", 0),
        "latin": counts.get("latin", 0),
        "digit": counts.get("digit", 0),
        "lines_with_kana": lines_with.get("kana", 0),
        "lines_with_kanji": lines_with.get("kanji", 0),
        "lines_with_hangul": lines_with.get("hangul", 0),
        "lines_with_latin": lines_with.get("latin", 0),
        "lines": len(lines),
    }


def tagalog_lexicon(text: str) -> dict[str, Any]:
    words = [word.lower() for word in LATIN_WORD.findall(unicodedata.normalize("NFC", text))]
    strong = [word for word in words if word in STRONG_TAGALOG]
    weak = [word for word in words if word in WEAK_TAGALOG]
    strong_counts = Counter(strong)
    strong_count = len(strong)
    strong_distinct = len(strong_counts)
    latin_count = len(words)
    strong_ratio = strong_count / latin_count if latin_count else 0.0
    return {
        "latin_words": latin_count,
        "strong_count": strong_count,
        "strong_distinct": strong_distinct,
        "strong_ratio": strong_ratio,
        "weak_count": len(weak),
        "strong_words": sorted(strong_counts),
    }


def tagalog_likely(lexicon: dict[str, Any], audio_language: str | None = None) -> bool:
    strong_count = int(lexicon.get("strong_count", 0))
    distinct = int(lexicon.get("strong_distinct", 0))
    ratio = float(lexicon.get("strong_ratio", 0.0))
    if strong_count >= 3 and distinct >= 2 and (ratio >= 0.03 or strong_count >= 6):
        return True
    return audio_language == "tl" and strong_count >= 2 and distinct >= 2


def choose_audio_language(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"language": None, "confidence": 0.0, "votes": {}}
    votes = Counter(str(item.get("language")) for item in results
                    if item.get("language"))
    probabilities: dict[str, float] = {}
    for item in results:
        language = str(item.get("language") or "")
        if language:
            probabilities[language] = probabilities.get(language, 0.0) + float(
                item.get("probability") or 0.0)
    if not votes:
        return {"language": None, "confidence": 0.0, "votes": {}}
    language = max(votes, key=lambda item: (votes[item], probabilities.get(item, 0.0)))
    confidence = probabilities.get(language, 0.0) / max(1, votes[language])
    return {
        "language": language,
        "confidence": min(1.0, confidence),
        "votes": dict(votes),
        "probabilities": {key: round(value, 4)
                          for key, value in probabilities.items()},
    }


def is_latin_text(text: str) -> bool:
    return (bool(LATIN_WORD.search(text))
            and not KANA.search(text) and not KANJI.search(text)
            and not HANGUL.search(text))


def script_compatibility(language: str, text: str) -> float:
    """0..1 score for whether a reference's script fits the audio language."""
    kana = bool(KANA.search(text))
    kanji = bool(KANJI.search(text))
    hangul = bool(HANGUL.search(text))
    latin = bool(LATIN_WORD.search(text))
    if language == "ja":
        if kana or kanji:
            return 1.0
        if latin and not hangul:
            return 0.6
        return 0.0
    if language == "ko":
        if hangul:
            return 1.0
        if latin and not (kana or kanji):
            return 0.6
        return 0.0
    if language in ("tl", "en"):
        return 1.0 if latin and not (kana or kanji or hangul) else 0.0
    return 0.5


def detect_language(reference_text: str | None,
                    audio_lid: list[dict[str, Any]] | None = None,
                    override: str | None = None,
                    lines: list[str] | None = None) -> dict[str, Any]:
    """Combine reference script, Tagalog lexicon, and Whisper LID."""
    text = reference_text or ""
    line_list = lines if lines is not None else (
        [line for line in text.splitlines() if line.strip()] if text else [])
    script = script_summary(line_list if line_list else text)
    audio = choose_audio_language(audio_lid or [])
    lexicon = tagalog_lexicon(text)
    audio_language = audio.get("language")
    inputs = {
        "script": script,
        "tagalog": lexicon,
        "audio_lid": {
            "language": audio_language,
            "confidence": round(float(audio.get("confidence", 0.0)), 4),
            "votes": audio.get("votes", {}),
            "windows": audio_lid or [],
        },
        "override": override,
        "reference_chars": len(text),
        "reference_lines": len(line_list),
    }
    result: dict[str, Any] = {
        "language": "en",
        "confidence": 0.3,
        "source": "default",
        "ambiguous": False,
        "reference_romaji": False,
        "latin_reference": False,
        "inputs": inputs,
    }

    if override:
        result.update({
            "language": override,
            "confidence": 1.0,
            "source": "override",
            "reference_romaji": False,
            "latin_reference": is_latin_text(text),
        })
        return result

    meaningful_kana = (script["kana"] >= 2 or script["lines_with_kana"] >= 2)
    meaningful_hangul = (script["hangul"] >= 2 or script["lines_with_hangul"] >= 2)
    kanji_only = (script["kanji"] > 0 and script["kana"] == 0
                  and script["hangul"] == 0)

    if meaningful_kana:
        confidence = 0.95 if audio_language == "ja" else 0.85
        result.update({
            "language": "ja",
            "confidence": confidence,
            "source": "script-kana",
            "inputs": {**inputs, "audio_agrees": audio_language == "ja"},
        })
        return result

    if meaningful_hangul:
        confidence = 0.95 if audio_language == "ko" else 0.85
        result.update({
            "language": "ko",
            "confidence": confidence,
            "source": "script-hangul",
            "inputs": {**inputs, "audio_agrees": audio_language == "ko"},
        })
        return result

    if kanji_only:
        result.update({
            "language": "ja",
            "confidence": 0.85 if audio_language == "ja" else 0.72,
            "source": "script-kanji-only",
            "ambiguous": True,
            "inputs": {**inputs, "audio_agrees": audio_language == "ja"},
        })
        return result

    if is_latin_text(text):
        if audio_language == "ja":
            result.update({
                "language": "ja",
                "confidence": max(0.75, 0.9 * float(audio.get("confidence", 0.0))),
                "source": "audio-ja-latin-reference",
                "reference_romaji": True,
                "latin_reference": True,
                "ambiguous": False,
            })
            return result
        if audio_language == "ko":
            result.update({
                "language": "ko",
                "confidence": max(0.7, 0.85 * float(audio.get("confidence", 0.0))),
                "source": "audio-ko-latin-reference",
                "latin_reference": True,
                "ambiguous": True,
            })
            return result
        if tagalog_likely(lexicon, audio_language):
            confidence = min(0.95, 0.68 + float(lexicon["strong_ratio"]))
            if audio_language == "tl":
                confidence = max(confidence, float(audio.get("confidence", 0.0)))
            elif audio_language == "en":
                confidence = min(confidence, 0.7)
            result.update({
                "language": "tl",
                "confidence": confidence,
                "source": "tagalog-lexicon",
                "ambiguous": confidence < 0.7,
            })
            return result
        if audio_language in ("tl", "en", "ja", "ko"):
            result.update({
                "language": audio_language,
                "confidence": max(0.5, float(audio.get("confidence", 0.0))),
                "source": "audio-lid",
            })
            return result
        result.update({
            "language": "en",
            "confidence": 0.55,
            "source": "latin-default",
            "ambiguous": True,
        })
        return result

    if audio_language in ("en", "tl", "ja", "ko"):
        result.update({
            "language": audio_language,
            "confidence": max(0.5, float(audio.get("confidence", 0.0))),
            "source": "audio-lid",
        })
        return result

    return result
