"""Catalog, lyric-saving, and transposition API routes."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

from flask import Blueprint, current_app, jsonify, request

from karaoke.catalog import enrich_song, find_song, scan_songs
from karaoke.http import editor_access_allowed, json_body
from karaoke.logging import logger
from karaoke.lyrics import (
    ActiveLyricsConflictError,
    DraftConflictError,
    DraftNotFoundError,
    LyricsConflictError,
    publish_draft,
    read_draft,
    read_lyrics_bundle,
    save_draft,
    save_lyrics,
    serialize_word_timings,
    validate_word_timings,
)
from karaoke.transposition import (
    build_audio_filter,
    run_transposition,
    transposed_filename,
    validate_semitones,
)

library = Blueprint("library", __name__)


@library.get("/api/songs")
def api_songs():
    lyrics_dir = current_app.config["LYRICS_DIR"]
    catalog = [
        enrich_song(song, lyrics_dir)
        for song in scan_songs(current_app.config["SONGS_DIR"])
    ]
    return jsonify({"songs": catalog})


@library.route("/transpose", methods=["GET", "POST"])
def transpose_audio():
    if request.method == "POST":
        data = json_body()
        song_id = data.get("song_id")
        semitones = data.get("semitones", 0)
    else:
        song_id = request.args.get("song_id")
        semitones = request.args.get("semitones", 0)

    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "Missing song_id"}), 400

    try:
        semitones = validate_semitones(semitones)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    song = find_song(current_app.config["SONGS_DIR"], song_id)
    if song is None:
        logger.warning(
            "Transpose: song %r not found in %s",
            song_id, current_app.config["SONGS_DIR"],
        )
        return jsonify({"error": "Song not found"}), 404

    source_name = song["filename"]
    logger.info("Transpose: %s (%+d semitones)", source_name, semitones)

    if semitones == 0:
        return jsonify({"url": f"/songs/{source_name}"})

    songs_dir = current_app.config["SONGS_DIR"]
    transposed_dir = current_app.config["TRANSPOSED_DIR"]
    original_path = os.path.abspath(
        os.path.join(songs_dir, source_name)).replace("\\", "/")
    transposed_name = transposed_filename(semitones, source_name)
    transposed_path = os.path.abspath(
        os.path.join(transposed_dir, transposed_name)).replace("\\", "/")

    if os.path.isfile(transposed_path) and os.path.getsize(transposed_path) > 0:
        logger.info("Transpose: serving cached %s", transposed_name)
        return jsonify({"url": f"/transposed/{transposed_name}"})

    try:
        os.makedirs(transposed_dir, exist_ok=True)
    except OSError as exc:
        logger.error("Transpose: could not create %s: %s", transposed_dir, exc)
        return jsonify({"error": "Could not create the transposition cache."}), 500

    template = os.environ.get(
        "KARAOKE_FFMPEG_FILTER", current_app.config["FFMPEG_FILTER"])
    try:
        audio_filter = build_audio_filter(semitones, template)
    except ValueError:
        return jsonify({"error": "Invalid KARAOKE_FFMPEG_FILTER template"}), 500

    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=".transpose-", suffix=".mp3", dir=transposed_dir)
    except OSError as exc:
        logger.error("Transpose: could not create a temporary file: %s", exc)
        return jsonify({"error": "Could not create a temporary file."}), 500
    os.close(fd)

    logger.info("Transpose: generating %s with ffmpeg", transposed_name)
    try:
        run_transposition(original_path, temp_path, audio_filter)
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
            raise RuntimeError("ffmpeg produced no output")
        os.replace(temp_path, transposed_path)
    except FileNotFoundError:
        logger.error(
            "Transpose: ffmpeg not found on PATH while processing %s", source_name)
        return jsonify({
            "error": "ffmpeg not found. Install FFmpeg and add it to your PATH."
        }), 500
    except subprocess.TimeoutExpired:
        logger.error("Transpose: ffmpeg timed out for %s", source_name)
        return jsonify({
            "error": ("Transposition timed out after "
                      f"{current_app.config['FFMPEG_TIMEOUT_SECONDS']} seconds.")
        }), 504
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        logger.error("Transpose failed for %s: %s", source_name, details)
        return jsonify({"error": "Transposition failed", "details": details}), 500
    except (OSError, RuntimeError) as exc:
        logger.error("Transpose failed for %s: %s", source_name, exc)
        return jsonify({"error": "Transposition failed", "details": str(exc)}), 500
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

    logger.info("Transpose: wrote %s", transposed_name)
    return jsonify({"url": f"/transposed/{transposed_name}"})


@library.get("/api/editor/lyrics")
def api_editor_lyrics():
    if not editor_access_allowed():
        return jsonify({"error": "Sync Editor is unavailable."}), 404

    song_id = request.args.get("song_id")
    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404

    try:
        bundle = read_lyrics_bundle(
            current_app.config["LYRICS_DIR"], song_id)
    except (OSError, UnicodeDecodeError) as exc:
        logger.error("Could not read lyrics for %s: %s", song_id, exc)
        return jsonify({"error": "Could not read lyrics."}), 500

    return jsonify({"song_id": song_id, **bundle})


def _words_object(raw: str | None) -> tuple[dict | None, str | None]:
    if raw is None:
        return None, None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, "The active word-timing file is not valid JSON."
    if not isinstance(data, dict):
        return None, "The active word-timing file must contain a JSON object."
    return data, None


@library.get("/api/editor/draft")
def api_editor_draft():
    if not editor_access_allowed():
        return jsonify({"error": "Sync Editor is unavailable."}), 404

    song_id = request.args.get("song_id")
    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404

    try:
        active = read_lyrics_bundle(
            current_app.config["LYRICS_DIR"], song_id)
        draft = read_draft(current_app.config["LYRICS_DIR"], song_id)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.error("Could not read lyrics draft for %s: %s", song_id, exc)
        return jsonify({"error": "Could not read lyrics."}), 500

    if draft is not None:
        return jsonify({
            "song_id": song_id,
            "srt": draft["srt"],
            "words": draft["words"],
            "source": "draft",
            "draft_revision": draft["revision"],
            "base_active_revision": draft["base_active_revision"],
            "active_revision": active["revision"],
            "words_error": None,
        })

    active_words, words_error = _words_object(active["words"])
    if active_words is not None or active["srt"] is not None:
        source = "active"
    else:
        source = "none"
    return jsonify({
        "song_id": song_id,
        "srt": active["srt"],
        "words": active_words,
        "source": source,
        "draft_revision": None,
        "base_active_revision": active["revision"],
        "active_revision": active["revision"],
        "words_error": words_error,
    })


@library.post("/api/editor/draft")
def api_editor_draft_save():
    if not editor_access_allowed():
        return jsonify({"error": "Sync Editor is unavailable."}), 404

    data = json_body()
    song_id = data.get("song_id")
    srt_text = data.get("srt")
    words = data.get("words", None)
    base_draft_revision = data.get("base_draft_revision")
    base_active_revision = data.get("base_active_revision")

    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400
    if base_draft_revision is not None and not isinstance(
            base_draft_revision, str):
        return jsonify({"error": "base_draft_revision must be a string"}), 400
    if not isinstance(base_active_revision, str) or not base_active_revision:
        return jsonify({"error": "base_active_revision is required"}), 400
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404

    try:
        result = save_draft(
            current_app.config["LYRICS_DIR"], song_id, srt_text, words,
            base_draft_revision, base_active_revision)
    except DraftConflictError as exc:
        logger.warning("Draft conflict for %s; current revision %s",
                       song_id, exc.revision)
        return jsonify({
            "error": "The saved draft changed. Reload it before saving.",
            "draft_revision": exc.revision,
        }), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (OSError, UnicodeEncodeError) as exc:
        logger.error("Could not save draft for %s: %s", song_id, exc)
        return jsonify({"error": "Could not save the draft."}), 500

    logger.info("Saved draft for %s (%s)", song_id,
                result["draft_revision"])
    return jsonify(result)


@library.post("/api/editor/approve")
def api_editor_approve():
    if not editor_access_allowed():
        return jsonify({"error": "Sync Editor is unavailable."}), 404

    data = json_body()
    song_id = data.get("song_id")
    draft_revision = data.get("draft_revision")
    base_active_revision = data.get("base_active_revision")

    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400
    if not isinstance(draft_revision, str) or not draft_revision:
        return jsonify({"error": "draft_revision is required"}), 400
    if not isinstance(base_active_revision, str) or not base_active_revision:
        return jsonify({"error": "base_active_revision is required"}), 400
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404

    try:
        result = publish_draft(
            current_app.config["LYRICS_DIR"], song_id, draft_revision,
            base_active_revision)
    except DraftNotFoundError:
        return jsonify({"error": "No saved draft to approve."}), 404
    except DraftConflictError as exc:
        return jsonify({
            "error": "The saved draft changed. Reload it before approving.",
            "draft_revision": exc.revision,
        }), 409
    except ActiveLyricsConflictError as exc:
        return jsonify({
            "error": ("Active lyrics changed since this draft was based on "
                      "them. Reload before approving."),
            "active_revision": exc.revision,
        }), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (OSError, UnicodeEncodeError) as exc:
        logger.error("Could not publish draft for %s: %s", song_id, exc)
        return jsonify({"error": "Could not publish the draft."}), 500

    logger.info("Published lyrics for %s -> %s", song_id, result["saved"])
    return jsonify(result)


@library.post("/api/save-lyrics")
def api_save_lyrics():
    if not editor_access_allowed():
        return jsonify({"error": "Sync Editor is unavailable."}), 404

    data = json_body()
    song_id = data.get("song_id")
    lyrics_text = data.get("lyrics")
    words = data.get("words", None)
    base_revision = data.get("base_revision")

    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400

    words_json = None
    saved_bytes = 0
    if words is not None:
        error = validate_word_timings(words)
        if error is not None:
            return jsonify({"error": f"Invalid word timings: {error}"}), 400
        try:
            words_json = serialize_word_timings(words, song_id)
            saved_bytes = len(words_json.encode("utf-8"))
        except UnicodeEncodeError:
            return jsonify({"error": "Word timings contain invalid text encoding"}), 400
        except ValueError:
            return jsonify({"error": "Word timings are not valid JSON"}), 400
        if saved_bytes > current_app.config["MAX_WORDS_BYTES"]:
            return jsonify({"error": "Word timing file is too large"}), 400
    else:
        if not isinstance(lyrics_text, str) or not lyrics_text.strip():
            return jsonify({"error": "non-empty lyrics are required"}), 400
        try:
            saved_bytes = len(lyrics_text.encode("utf-8"))
        except UnicodeEncodeError:
            return jsonify({"error": "Lyrics contain invalid text encoding"}), 400
        if saved_bytes > current_app.config["MAX_LYRICS_BYTES"]:
            return jsonify({"error": "Lyrics file is too large"}), 400

    # The song must exist in the catalog: this guarantees the .srt name always
    # matches the audio stem and blocks path traversal.
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404
    if not isinstance(base_revision, str) or not base_revision:
        return jsonify({"error": "base_revision is required"}), 400

    try:
        result = save_lyrics(
            current_app.config["LYRICS_DIR"], song_id, lyrics_text, words,
            base_revision=base_revision)
    except LyricsConflictError as exc:
        logger.warning("Lyrics conflict for %s; current revision %s",
                       song_id, exc.revision)
        return jsonify({
            "error": "Lyrics changed on disk. Reload before saving.",
            "revision": exc.revision,
        }), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (OSError, UnicodeEncodeError) as exc:
        logger.error("Could not save lyrics for %s: %s", song_id, exc)
        return jsonify({"error": "Could not save lyrics."}), 500

    logger.info("Saved lyrics for %s -> %s (%d bytes)",
                song_id, result["saved"], saved_bytes)
    return jsonify(result)
