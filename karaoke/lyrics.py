"""Validation and persistence helpers for synced lyrics."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from typing import Any

from karaoke.catalog import song_stem
from utils import is_finite_number

_SAVE_LOCK = threading.RLock()


class LyricsConflictError(Exception):
    """The lyric files changed after the editor loaded them."""

    def __init__(self, revision: str):
        super().__init__("Lyrics changed on disk.")
        self.revision = revision


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


def _srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt_from_words(words: dict[str, Any]) -> str:
    """Render the canonical word-timing model as a portable SRT document."""
    lines = words.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("Word timings have no cue lines")
    blocks = []
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            raise ValueError("Word timings contain a non-object cue")
        text = "[Instrumental]" if line.get("instrumental") else str(
            line.get("text", "")).strip()
        blocks.append(
            f"{index}\n{_srt_time(line['start'])} --> "
            f"{_srt_time(line['end'])}\n{text}\n"
        )
    return "\n".join(blocks)


def _read_optional_bytes(path: os.PathLike[str] | str) -> bytes | None:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _revision_from_bytes(srt_bytes: bytes | None,
                         words_bytes: bytes | None) -> str:
    digest = hashlib.sha256()
    for label, data in ((b"srt", srt_bytes), (b"words", words_bytes)):
        digest.update(label)
        if data is None:
            digest.update(b"\x00missing")
            continue
        digest.update(b"\x00present")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _revision_unlocked(lyrics_dir: os.PathLike[str] | str, song_id: str) -> str:
    stem = song_stem(song_id)
    return _revision_from_bytes(
        _read_optional_bytes(os.path.join(lyrics_dir, stem + ".srt")),
        _read_optional_bytes(os.path.join(lyrics_dir, stem + ".words.json")),
    )


def lyrics_revision(lyrics_dir: os.PathLike[str] | str, song_id: str) -> str:
    """Return a content hash for the SRT/words pair of one song."""
    with _SAVE_LOCK:
        return _revision_unlocked(lyrics_dir, song_id)


def read_lyrics_bundle(lyrics_dir: os.PathLike[str] | str,
                       song_id: str) -> dict[str, str | None]:
    """Return the current SRT text, words text, and revision for one song."""
    stem = song_stem(song_id)
    srt_path = os.path.join(lyrics_dir, stem + ".srt")
    words_path = os.path.join(lyrics_dir, stem + ".words.json")
    with _SAVE_LOCK:
        srt_bytes = _read_optional_bytes(srt_path)
        words_bytes = _read_optional_bytes(words_path)
        revision = _revision_from_bytes(srt_bytes, words_bytes)
    return {
        "srt": srt_bytes.decode("utf-8") if srt_bytes is not None else None,
        "words": words_bytes.decode("utf-8") if words_bytes is not None else None,
        "revision": revision,
    }


def save_lyrics(lyrics_dir: os.PathLike[str] | str, song_id: str,
                 lyrics_text: str | None, words: dict[str, Any] | None = None,
                 base_revision: str | None = None
                 ) -> dict[str, str | None]:
    """Persist one song's lyrics.

    When ``words`` is supplied it is the canonical document: the SRT is
    regenerated from it and any client-supplied SRT is ignored. Without
    ``words``, ``lyrics_text`` is saved as an SRT-only document and the stale
    word-timing sidecar is removed.

    Saves are serialized per process and each file is replaced atomically.
    ``base_revision`` must match the current SRT/words pair so a stale editor
    cannot overwrite a newer save.
    """
    os.makedirs(lyrics_dir, exist_ok=True)
    stem = song_stem(song_id)
    srt_name = stem + ".srt"
    words_name = stem + ".words.json"
    srt_path = os.path.join(lyrics_dir, srt_name)
    words_path = os.path.join(lyrics_dir, words_name)

    if words is not None:
        error = validate_word_timings(words)
        if error is not None:
            raise ValueError(error)
        words_json = serialize_word_timings(words, song_id)
        srt_text = build_srt_from_words(words)
    else:
        if not isinstance(lyrics_text, str) or not lyrics_text.strip():
            raise ValueError("non-empty lyrics are required")
        words_json = None
        srt_text = lyrics_text

    with _SAVE_LOCK:
        current = _revision_unlocked(lyrics_dir, song_id)
        if base_revision != current:
            raise LyricsConflictError(current)

        if words_json is None:
            # Remove the sidecar before writing the SRT so a stale word-timing
            # file can never take precedence over the new line-level lyrics.
            if os.path.isfile(words_path):
                os.remove(words_path)
            write_text_atomic(srt_path, srt_text)
        else:
            # JSON is the source of truth; write it first. A failed SRT write
            # leaves the new JSON intact and the player still reads it.
            write_text_atomic(words_path, words_json)
            write_text_atomic(srt_path, srt_text)

        revision = _revision_unlocked(lyrics_dir, song_id)

    return {
        "saved": srt_name,
        "words": words_name if words_json is not None else None,
        "revision": revision,
    }
