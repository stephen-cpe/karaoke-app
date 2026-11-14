"""Unicode-aware text normalization and tokenization.

The English matcher intentionally keeps the old ASCII-only behavior so the
existing English corpus and its regression checks remain stable. Tagalog keeps
apostrophes as distinct matching characters. Japanese and Korean tokens keep
their native script in Phase 1; Phase 3 romanizes the same token sequence.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any

KANA = re.compile(r"[\u3040-\u30ff]")
KANJI = re.compile(r"[\u3400-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_APOSTROPHE_ARTIFACT = re.compile(
    r"(?<=[A-Za-z])\?\s*(?=(?:m|s|t|ve|re|ll|d)\b)")
_CAUSE_ARTIFACT = re.compile(r"(^|\s)\?\s*(?=[Cc]ause\b)")


def legacy_norm_token(text: str) -> str:
    """The pre-rewrite English normalizer, kept for exact compatibility."""
    text = text.lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


def _apostrophes(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")


def norm_token(text: str, language: str = "auto") -> str:
    """Normalize one token for matching.

    ``language="en"`` is bit-for-bit the old normalizer. Other languages keep
    Unicode letters and, for Tagalog, the apostrophe. The default ``auto``
    keeps CJK/Hangul tokens while falling back to the exact English behavior
    for Latin-only text.
    """
    if language == "auto":
        if HANGUL.search(str(text)):
            language = "ko"
        elif KANA.search(str(text)) or KANJI.search(str(text)):
            language = "ja"
        else:
            language = "en"
    if language == "en":
        return legacy_norm_token(text)

    text = unicodedata.normalize("NFC", _apostrophes(str(text))).lower()
    if language == "tl":
        return "".join(char for char in text
                       if char.isalnum() or char == "'")
    if language in ("ja", "ko"):
        return "".join(char for char in text
                       if char.isalnum() or char == "'")
    return "".join(char for char in text if char.isalnum() or char == "'")


def capitalize_line(text: str) -> str:
    """Capitalize the first alphabetic character, leaving the rest alone."""
    for index, char in enumerate(text):
        if char.isalpha():
            if char.isascii():
                return text[:index] + char.upper() + text[index + 1:]
            return text
    return text


def clean_text(text: str, language: str = "en", capitalize: bool = True) -> str:
    """Tidy display text and optionally capitalize its first letter."""
    if language == "en":
        # Keep the old English behavior exactly.
        text = _apostrophes(text)
        text = _APOSTROPHE_ARTIFACT.sub("'", text)
        text = _CAUSE_ARTIFACT.sub(r"\1'", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and capitalize:
            text = text[0].upper() + text[1:]
        return text

    text = unicodedata.normalize("NFC", _apostrophes(text))
    text = _APOSTROPHE_ARTIFACT.sub("'", text)
    text = _CAUSE_ARTIFACT.sub(r"\1'", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text and capitalize:
        if language in ("en", "tl"):
            text = text[0].upper() + text[1:]
        else:
            text = capitalize_line(text)
    return text


def has_japanese(text: str) -> bool:
    return bool(_JAPANESE.search(text))


def has_kana(text: str) -> bool:
    return bool(KANA.search(text))


def has_kanji(text: str) -> bool:
    return bool(KANJI.search(text))


def has_hangul(text: str) -> bool:
    return bool(HANGUL.search(text))


@lru_cache(maxsize=1)
def _japanese_tagger():
    import fugashi

    return fugashi.Tagger()


def _is_punctuation_only(text: str) -> bool:
    return bool(text) and not any(char.isalnum() for char in text)


def _token(text: str, language: str) -> dict[str, str]:
    return {"text": text, "norm": norm_token(text, language)}


def _split_japanese(text: str) -> list[dict[str, str]]:
    tagger = _japanese_tagger()
    tokens: list[dict[str, str]] = []
    for chunk in text.split():
        if not has_japanese(chunk):
            tokens.append(_token(chunk, "ja"))
            continue
        chunk_tokens: list[dict[str, str]] = []
        pending_prefix = ""
        for word in tagger(chunk):
            surface = word.surface
            if not surface:
                continue
            if _is_punctuation_only(surface):
                if chunk_tokens:
                    chunk_tokens[-1]["text"] += surface
                    chunk_tokens[-1]["norm"] = norm_token(
                        chunk_tokens[-1]["text"], "ja")
                else:
                    pending_prefix += surface
                continue
            chunk_tokens.append(_token(pending_prefix + surface, "ja"))
            pending_prefix = ""
        if pending_prefix:
            if chunk_tokens:
                chunk_tokens[-1]["text"] += pending_prefix
                chunk_tokens[-1]["norm"] = norm_token(
                    chunk_tokens[-1]["text"], "ja")
            else:
                chunk_tokens.append(_token(pending_prefix, "ja"))
        tokens.extend(chunk_tokens)
    return tokens


def split_display_tokens(text: str, language: str = "en") -> list[dict[str, str]]:
    """Split a display line into tokens that join back with single spaces."""
    text = clean_text(text, language=language, capitalize=False)
    if language == "ja":
        return _split_japanese(text)

    tokens: list[dict[str, str]] = []
    for raw in text.split():
        norm = norm_token(raw, language)
        if not norm:
            if tokens:
                tokens[-1]["text"] += raw
                tokens[-1]["norm"] = norm_token(tokens[-1]["text"], language)
            continue
        tokens.append({"text": raw, "norm": norm})
    return tokens


def join_tokens(tokens: list[dict[str, Any]]) -> str:
    """Return the validator-compatible line text for a token sequence."""
    return " ".join(str(token.get("text", "")) for token in tokens).strip()


def line_text(text: str, language: str = "en") -> str:
    """Normalize a line through the same tokenizer used for timing."""
    tokens = split_display_tokens(text, language=language)
    return join_tokens(tokens)


def token_count(text: str, language: str = "en") -> int:
    return len(split_display_tokens(text, language=language))
