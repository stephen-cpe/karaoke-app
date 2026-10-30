"""Validation and persistence helpers for synced lyrics."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Any

from karaoke.catalog import song_stem
from utils import is_finite_number

_SAVE_LOCK = threading.RLock()
_DRAFT_DIR = "drafts"
_SRT_TIMING = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


class LyricsConflictError(Exception):
    """The lyric files changed after the editor loaded them."""

    def __init__(self, revision: str):
        super().__init__("Lyrics changed on disk.")
        self.revision = revision


class DraftConflictError(Exception):
    """The saved draft changed after the editor loaded it."""

    def __init__(self, revision: str | None):
        super().__init__("Draft changed on disk.")
        self.revision = revision


class ActiveLyricsConflictError(Exception):
    """The active lyrics changed since the draft was based on them."""

    def __init__(self, revision: str):
        super().__init__("Active lyrics changed on disk.")
        self.revision = revision


class DraftNotFoundError(Exception):
    """No saved draft exists for this song."""


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


def validate_srt_text(text: Any) -> str | None:
    """Return an error string for an invalid SRT document.

    SRT-only saves are accepted without a word-timing sidecar, so they need
    the same structural checks as the editor before they reach the player.
    Cue numbers are optional; comma and dot decimal separators both work.
    """
    if not isinstance(text, str) or not text.strip():
        return "SRT text is required"
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip())
    previous_end = -1.0
    cue_count = 0
    for index, block in enumerate(blocks, 1):
        rows = [row for row in block.split("\n") if row.strip()]
        if not rows:
            continue
        offset = 1 if rows[0].strip().isdigit() else 0
        if offset >= len(rows):
            return f"cue {index}: missing timing line"
        match = _SRT_TIMING.fullmatch(rows[offset].strip())
        if match is None:
            return f"cue {index}: invalid SRT timing"
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        if end <= start:
            return f"cue {index}: end must be after start"
        if start < previous_end - 0.001:
            return f"cue {index}: overlaps the previous cue"
        text_lines = " ".join(rows[offset + 1:]).strip()
        if not text_lines:
            return f"cue {index}: text is empty"
        previous_end = max(previous_end, end)
        cue_count += 1
    if not cue_count:
        return "SRT has no cues"
    return None


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


def _draft_path(lyrics_dir: os.PathLike[str] | str, song_id: str) -> str:
    return os.path.join(
        os.fspath(lyrics_dir), _DRAFT_DIR, song_stem(song_id) + ".draft.json")


def _normalize_review(review: Any) -> list[dict[str, Any]]:
    """Validate editor-only review flags for a draft envelope.

    Review metadata never reaches the active lyric files. It exists only so a
    draft can remember which lines had timing inferred during a text edit.
    """
    if review is None:
        return []
    if not isinstance(review, list):
        raise ValueError("Draft review must be an array")
    if len(review) > 500:
        raise ValueError("Draft has too many review entries")
    normalized: list[dict[str, Any]] = []
    for item in review:
        if not isinstance(item, dict):
            raise ValueError("Each draft review entry must be an object")
        line = item.get("line")
        reason = item.get("reason")
        if (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            raise ValueError("Draft review line must be a positive integer")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Draft review reason must be non-empty text")
        normalized.append({"line": line, "reason": reason.strip()[:120]})
    return normalized


def _draft_revision_unlocked(lyrics_dir: os.PathLike[str] | str,
                             song_id: str) -> str | None:
    data = _read_optional_bytes(_draft_path(lyrics_dir, song_id))
    return hashlib.sha256(data).hexdigest() if data is not None else None


def draft_revision(lyrics_dir: os.PathLike[str] | str,
                   song_id: str) -> str | None:
    """Return a content hash for the saved draft, or None if absent."""
    with _SAVE_LOCK:
        return _draft_revision_unlocked(lyrics_dir, song_id)


def _read_draft_unlocked(lyrics_dir: os.PathLike[str] | str,
                         song_id: str) -> tuple[dict[str, Any], str] | None:
    data = _read_optional_bytes(_draft_path(lyrics_dir, song_id))
    if data is None:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Draft file is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Draft file must contain an object")
    srt = payload.get("srt")
    words = payload.get("words")
    base_active = payload.get("base_active_revision")
    if srt is not None and not isinstance(srt, str):
        raise ValueError("Draft SRT must be a string or null")
    if words is not None and not isinstance(words, dict):
        raise ValueError("Draft words must be an object or null")
    if not isinstance(base_active, str) or not base_active:
        raise ValueError("Draft has no base_active_revision")
    _normalize_review(payload.get("review"))
    return payload, hashlib.sha256(data).hexdigest()


def read_draft(lyrics_dir: os.PathLike[str] | str,
               song_id: str) -> dict[str, Any] | None:
    """Return a saved draft's content and revision, or None."""
    with _SAVE_LOCK:
        result = _read_draft_unlocked(lyrics_dir, song_id)
    if result is None:
        return None
    payload, revision = result
    return {
        "srt": payload.get("srt"),
        "words": payload.get("words"),
        "review": _normalize_review(payload.get("review")),
        "base_active_revision": payload["base_active_revision"],
        "revision": revision,
    }


def save_draft(lyrics_dir: os.PathLike[str] | str, song_id: str,
               srt_text: str | None, words: dict[str, Any] | None,
               base_draft_revision: str | None,
               base_active_revision: str,
               review: list[dict[str, Any]] | None = None
               ) -> dict[str, str | None]:
    """Save an isolated draft without changing the active lyric pair."""
    if not isinstance(base_active_revision, str) or not base_active_revision:
        raise ValueError("base_active_revision is required")

    if words is not None:
        error = validate_word_timings(words)
        if error is not None:
            raise ValueError(error)
        words_payload = json.loads(serialize_word_timings(words, song_id))
        srt_text = build_srt_from_words(words_payload)
    else:
        if not isinstance(srt_text, str) or not srt_text.strip():
            raise ValueError("non-empty SRT text is required")
        error = validate_srt_text(srt_text)
        if error is not None:
            raise ValueError(error)
        words_payload = None

    payload = {
        "version": 1,
        "song": song_stem(song_id),
        "base_active_revision": base_active_revision,
        "srt": srt_text,
        "words": words_payload,
        "review": _normalize_review(review),
    }
    try:
        draft_json = json.dumps(
            payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        draft_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Draft contains invalid text encoding") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("Draft is not valid JSON") from exc

    path = _draft_path(lyrics_dir, song_id)
    with _SAVE_LOCK:
        current = _draft_revision_unlocked(lyrics_dir, song_id)
        if base_draft_revision != current:
            raise DraftConflictError(current)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, draft_json)
        revision = _draft_revision_unlocked(lyrics_dir, song_id)
        active_revision = _revision_unlocked(lyrics_dir, song_id)
    return {
        "draft_revision": revision,
        "active_revision": active_revision,
    }


def _stage_text(directory: os.PathLike[str] | str, text: str) -> str:
    fd, temp_path = tempfile.mkstemp(
        dir=os.fspath(directory), prefix=".tmp-publish-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return temp_path


def _restore_file(path: os.PathLike[str] | str, data: bytes | None) -> None:
    """Restore an active file directly after a failed publish."""
    if data is None:
        if os.path.isfile(path):
            os.remove(path)
        return
    with open(path, "wb") as fh:
        fh.write(data)


def publish_draft(lyrics_dir: os.PathLike[str] | str, song_id: str,
                  draft_revision_value: str,
                  base_active_revision: str) -> dict[str, str | None]:
    """Publish the saved draft to the active lyric pair after validation.

    All content is validated and staged before the active files are touched.
    If any active replacement or draft removal fails, the previous active
    bytes are restored and the draft is left in place.
    """
    if not isinstance(draft_revision_value, str) or not draft_revision_value:
        raise ValueError("draft_revision is required")
    if not isinstance(base_active_revision, str) or not base_active_revision:
        raise ValueError("base_active_revision is required")

    stem = song_stem(song_id)
    srt_name = stem + ".srt"
    words_name = stem + ".words.json"
    srt_path = os.path.join(lyrics_dir, srt_name)
    words_path = os.path.join(lyrics_dir, words_name)
    draft_path = _draft_path(lyrics_dir, song_id)

    with _SAVE_LOCK:
        result = _read_draft_unlocked(lyrics_dir, song_id)
        if result is None:
            raise DraftNotFoundError()
        draft, revision = result
        if draft_revision_value != revision:
            raise DraftConflictError(revision)

        current_active = _revision_unlocked(lyrics_dir, song_id)
        if base_active_revision != draft["base_active_revision"]:
            raise ActiveLyricsConflictError(current_active)
        if current_active != draft["base_active_revision"]:
            raise ActiveLyricsConflictError(current_active)

        words = draft.get("words")
        if words is not None:
            error = validate_word_timings(words)
            if error is not None:
                raise ValueError(f"Draft is invalid: {error}")
            words_payload = json.loads(serialize_word_timings(words, song_id))
            srt_text = build_srt_from_words(words_payload)
            words_json = serialize_word_timings(words_payload, song_id)
        else:
            srt_text = draft.get("srt")
            error = validate_srt_text(srt_text)
            if error is not None:
                raise ValueError(f"Draft is invalid: {error}")
            words_json = None

        old_srt = _read_optional_bytes(srt_path)
        old_words = _read_optional_bytes(words_path)
        temp_paths: list[str] = []
        try:
            temp_srt = _stage_text(lyrics_dir, srt_text)
            temp_paths.append(temp_srt)
            temp_words = None
            if words_json is not None:
                temp_words = _stage_text(lyrics_dir, words_json)
                temp_paths.append(temp_words)

            try:
                if words_json is not None:
                    os.replace(temp_words, words_path)
                    os.replace(temp_srt, srt_path)
                else:
                    os.replace(temp_srt, srt_path)
                    if os.path.isfile(words_path):
                        os.remove(words_path)
            except BaseException:
                _restore_file(srt_path, old_srt)
                _restore_file(words_path, old_words)
                raise

            try:
                os.remove(draft_path)
            except OSError:
                _restore_file(srt_path, old_srt)
                _restore_file(words_path, old_words)
                raise
        finally:
            for path in temp_paths:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

        new_revision = _revision_unlocked(lyrics_dir, song_id)

    return {
        "saved": srt_name,
        "words": words_name if words_json is not None else None,
        "revision": new_revision,
        "draft_revision": None,
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
        srt_error = validate_srt_text(lyrics_text)
        if srt_error is not None:
            raise ValueError(srt_error)
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
