"""Validation and persistence helpers for synced lyrics."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from karaoke.catalog import song_stem
from utils import is_finite_number

_SAVE_LOCK = threading.Lock()


def _normalize_text(text: Any) -> str:
    return " ".join(str(text).split())


def validate_word_timings(data: Any) -> str | None:
    """Return an error string for an invalid ``.words.json`` payload.

    The checks mirror the rules enforced by the player and
    ``tools/check_lyrics.py`` so a manual save cannot corrupt word
    highlighting. Line-level cues without word timings are allowed: manual
    edits may keep one cue's timing without word-level data, and the player
    falls back to line-level display for that cue.
    """
    if not isinstance(data, dict):
        return "words must be a JSON object"
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        return "words.lines must be a non-empty array"

    previous_end = -1.0
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            return f"words.lines[{index}] must be an object"

        start = line.get("start")
        end = line.get("end")
        if not is_finite_number(start) or not is_finite_number(end):
            return f"words.lines[{index}] needs numeric start and end"
        start = float(start)
        end = float(end)
        if start < 0:
            return f"words.lines[{index}] start must not be negative"
        if end <= start:
            return f"words.lines[{index}] end must be after start"
        if start < previous_end - 0.001:
            return f"words.lines[{index}] overlaps a previous cue"
        previous_end = max(previous_end, end)

        text = line.get("text")
        if not isinstance(text, str) or not _normalize_text(text):
            return f"words.lines[{index}].text must be a non-empty string"

        instrumental = line.get("instrumental", False)
        if not isinstance(instrumental, bool):
            return f"words.lines[{index}].instrumental must be a boolean"
        words = line.get("words", [])
        if not isinstance(words, list):
            return f"words.lines[{index}].words must be an array"

        if instrumental:
            if _normalize_text(text) != "[Instrumental]":
                return f"words.lines[{index}] instrumental text must be [Instrumental]"
            if words:
                return f"words.lines[{index}] is instrumental but has word timings"
            continue

        if not words:
            continue

        previous_word_end = None
        for word_index, word in enumerate(words, 1):
            if not isinstance(word, dict):
                return f"words.lines[{index}].words[{word_index}] must be an object"
            word_start = word.get("start")
            word_end = word.get("end")
            if not is_finite_number(word_start) or not is_finite_number(word_end):
                return (f"words.lines[{index}].words[{word_index}] "
                        "needs numeric start and end")
            word_start = float(word_start)
            word_end = float(word_end)
            if word_end <= word_start:
                return (f"words.lines[{index}].words[{word_index}] "
                        "end must be after start")
            if word_start < start - 0.001:
                return (f"words.lines[{index}].words[{word_index}] "
                        "starts before the cue")
            if word_end > end + 0.001:
                return (f"words.lines[{index}].words[{word_index}] "
                        "ends after the cue")
            if (previous_word_end is not None
                    and word_start < previous_word_end - 0.001):
                return (f"words.lines[{index}].words[{word_index}] "
                        "overlaps a previous word")
            previous_word_end = word_end
            word_text = word.get("text")
            if not isinstance(word_text, str) or not _normalize_text(word_text):
                return (f"words.lines[{index}].words[{word_index}].text "
                        "must be a non-empty string")

        joined = _normalize_text(
            " ".join(str(word.get("text", "")) for word in words)
        )
        if joined != _normalize_text(text):
            return f"words.lines[{index}] text does not match its word timings"
    return None


def serialize_word_timings(words: dict[str, Any], song_id: str) -> str:
    """Return a normalized, standards-compliant ``.words.json`` document."""
    payload = dict(words)
    payload["song"] = song_stem(song_id)
    payload.setdefault("version", 1)
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError("Word timings are not valid JSON") from exc


def write_text_atomic(path: os.PathLike[str] | str, text: str) -> None:
    """Write ``text`` to ``path`` without exposing a partially written file."""
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def save_lyrics(lyrics_dir: os.PathLike[str] | str, song_id: str,
                lyrics_text: str, words: dict[str, Any] | None = None) -> dict[str, str | None]:
    """Persist the SRT and optional word-timing sidecar for one song.

    Saves are serialized per process so two editors cannot interleave the two
    files. Each individual file is replaced atomically.
    """
    os.makedirs(lyrics_dir, exist_ok=True)
    stem = song_stem(song_id)
    srt_name = stem + ".srt"
    words_name = stem + ".words.json"
    words_json = serialize_word_timings(words, song_id) if words is not None else None

    with _SAVE_LOCK:
        write_text_atomic(os.path.join(lyrics_dir, srt_name), lyrics_text)
        words_path = os.path.join(lyrics_dir, words_name)
        if words_json is not None:
            write_text_atomic(words_path, words_json)
        elif os.path.isfile(words_path):
            os.remove(words_path)

    return {"saved": srt_name, "words": words_name if words_json is not None else None}
