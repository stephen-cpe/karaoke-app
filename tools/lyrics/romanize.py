"""Display romanization for Japanese and Korean lyric text.

The pipeline aligns and displays the same romanized text the singer sees. The
optional dependencies are imported lazily so the web app and CI can import this
module without the lyric extras installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from . import textnorm

_DIACRITICS = str.maketrans({
    "ā": "aa", "ī": "ii", "ū": "uu", "ē": "ee", "ō": "ou",
    "â": "aa", "ê": "ee", "î": "ii", "ô": "ou", "û": "uu",
    "Ā": "Aa", "Ī": "Ii", "Ū": "Uu", "Ē": "Ee", "Ō": "Ou",
    "Â": "Aa", "Ê": "Ee", "Î": "Ii", "Ô": "Ou", "Û": "Uu",
})
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")


@dataclass
class RomanizedLine:
    text: str
    readings: list[dict[str, str]] = field(default_factory=list)
    method: str = "japanese-fugashi"


@lru_cache(maxsize=1)
def _japanese_tagger():
    import fugashi

    return fugashi.Tagger()


@lru_cache(maxsize=1)
def _cutlet():
    import cutlet

    return cutlet.Cutlet(use_foreign_spelling=False)


@lru_cache(maxsize=1)
def _korean_transliter():
    from hangul_romanize import Transliter
    from hangul_romanize.rule import academic

    return Transliter(academic)


def _capitalize_first(text: str) -> str:
    for index, char in enumerate(text):
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1:]
    return text


def _lower_romanized(text: str) -> str:
    """Lowercase romanized Japanese without touching embedded Latin words."""
    # Cutlet capitalizes proper nouns and the first word. The display rule is
    # capital only at line start, so lowercase the whole romanized line here.
    return text.lower()


def normalize_pre_romanized(text: str) -> str:
    """Normalize an already-romanized Japanese line.

    Section 4 permits only macron/circumflex normalization, capitalization, and
    spacing changes here. Internal capitalization is removed; the line's first
    letter is capitalized.
    """
    text = str(text).translate(_DIACRITICS)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.lower()
    return _capitalize_first(text)


def _japanese_attach(previous: bool, pos1: str | None,
                     pos2: str | None, previous_pos1: str | None = None,
                     previous_pos2: str | None = None) -> bool:
    if not previous:
        return False
    if pos1 == "助動詞":
        return True
    if pos1 == "助詞" and pos2 == "接続助詞":
        return True
    if pos1 == "動詞" and pos2 == "非自立可能":
        if previous_pos1 != "助詞" or previous_pos2 == "接続助詞":
            return True
    return False


def _is_symbol(text: str) -> bool:
    return bool(text) and not any(char.isalnum() for char in text)


def romanize_japanese_line(text: str, *, pre_romanized: bool = False
                           ) -> RomanizedLine:
    text = str(text)
    if not _JAPANESE.search(text):
        if pre_romanized:
            return RomanizedLine(
                text=normalize_pre_romanized(text), readings=[],
                method="pre-romanized")
        return RomanizedLine(
            text=re.sub(r"\s+", " ", text).strip(), readings=[],
            method="latin-passthrough")
    parts: list[dict[str, Any]] = []
    readings: list[dict[str, Any]] = []
    previous_pos1 = None
    previous_pos2 = None
    for word in _japanese_tagger()(text):
        surface = str(word.surface or "")
        if not surface:
            continue
        feature = word.feature
        pos1 = getattr(feature, "pos1", None)
        pos2 = getattr(feature, "pos2", None)
        if _is_symbol(surface):
            if parts:
                parts[-1]["text"] = str(parts[-1]["text"]) + surface
            else:
                parts.append({"text": surface, "attach": False,
                              "japanese": False})
            continue
        if not _JAPANESE.search(surface):
            parts.append({"text": surface, "attach": False,
                          "japanese": False})
            previous_pos1 = None
            previous_pos2 = None
            continue
        kana = getattr(feature, "kana", None)
        pron = getattr(feature, "pron", None)
        reading = kana or pron or surface
        if pos1 == "助詞" and pron:
            reading = pron
        if surface in ("こんにちは", "こんばんは", "こんにちわ", "こんばんわ") and pron:
            reading = pron
        if not _JAPANESE.search(reading):
            parts.append({"text": surface, "attach": False,
                          "japanese": False})
            previous_pos1 = None
            previous_pos2 = None
            continue
        try:
            romaji = _cutlet().romaji(reading).replace(" ", "")
        except Exception:
            romaji = reading
        if re.search(r"ン[ヤユヨアイウエオャュョ]", reading):
            romaji = re.sub(r"n(?=[aeiouy])", "n'", romaji, count=1)
        romaji = _lower_romanized(romaji)
        attach = _japanese_attach(
            bool(parts), pos1, pos2, previous_pos1, previous_pos2)
        parts.append({
            "text": romaji,
            "attach": attach,
            "japanese": True,
            "reading": reading,
        })
        previous_pos1 = pos1
        previous_pos2 = pos2
        if textnorm.has_kanji(surface):
            readings.append({
                "text": surface,
                "reading": reading,
                "romaji": romaji,
                "part": len(parts) - 1,
            })

    # Cutlet drops a word-final sokuon. Carry it onto the next word's first
    # consonant (怒っ + て -> Okotte, 走っ + て -> Hashitte).
    for index, part in enumerate(parts[:-1]):
        reading = str(part.get("reading") or "")
        if not part.get("japanese") or not reading.endswith("ッ"):
            continue
        text_value = str(part.get("text") or "")
        if not text_value or not text_value[-1].isalpha():
            continue
        next_text = str(parts[index + 1].get("text") or "")
        if not next_text or not next_text[0].isalpha():
            continue
        first = next_text[0].lower()
        if first in "aeiouny":
            continue
        lower = next_text.lower()
        prefix = "t" if lower.startswith(("ch", "ts")) else first
        if text_value.lower().endswith(prefix):
            continue
        parts[index + 1]["text"] = prefix + next_text
        for entry in readings:
            if entry.get("part") == index:
                entry["sokuon"] = prefix
                break

    for entry in readings:
        entry.pop("part", None)
    output = ""
    for part in parts:
        if output and not part["attach"]:
            output += " "
        output += str(part["text"])
    output = re.sub(r"\s+", " ", output).strip()
    output = output.translate(str.maketrans({
        "！": "!", "？": "?", "。": ".", "、": ",",
        "「": '"', "」": '"', "｢": '"', "｣": '"',
    }))
    output = re.sub(r"(?<=[A-Za-z0-9])\(", " (", output)
    output = re.sub(r"\(\s+", "(", output)
    output = re.sub(r"\s+\)", ")", output)
    return RomanizedLine(
        text=_capitalize_first(output), readings=readings,
        method="japanese-fugashi")


def romanize_korean_line(text: str) -> RomanizedLine:
    text = str(text)
    if not _HANGUL.search(text):
        # Latin/English lines in a mixed Korean song pass through.
        return RomanizedLine(
            text=re.sub(r"\s+", " ", text).strip(), readings=[],
            method="latin-passthrough")
    romanized = _korean_transliter().translit(text)
    romanized = re.sub(r"\s+", " ", romanized).strip()
    return RomanizedLine(
        text=_capitalize_first(romanized), readings=[],
        method="korean-academic")


def romanize_line(text: str, language: str, *,
                  pre_romanized: bool = False) -> RomanizedLine:
    if language == "ja":
        return romanize_japanese_line(text, pre_romanized=pre_romanized)
    if language == "ko":
        return romanize_korean_line(text)
    return RomanizedLine(
        text=re.sub(r"\s+", " ", str(text)).strip(), readings=[],
        method="latin-passthrough")


def romanize_lines(lines: list[dict[str, Any]], language: str, *,
                   pre_romanized: bool = False
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Romanize reference/ASR lines and collect per-line kanji readings."""
    romanized_lines = []
    line_readings = []
    for index, line in enumerate(lines):
        result = romanize_line(
            str(line.get("text") or ""), language,
            pre_romanized=pre_romanized)
        row = dict(line)
        row["romanized_text"] = result.text
        row["text"] = result.text
        romanized_lines.append(row)
        line_readings.append({
            "line": index + 1,
            "method": result.method,
            "readings": result.readings,
        })
    return romanized_lines, line_readings
