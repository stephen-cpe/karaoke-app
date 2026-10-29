"""Catalog, lyric-saving, and transposition API routes."""
from __future__ import annotations

import os
import subprocess
import tempfile

from flask import Blueprint, current_app, jsonify, request

from karaoke.catalog import enrich_song, find_song, scan_songs
from karaoke.http import json_body
from karaoke.logging import logger
from karaoke.lyrics import save_lyrics, serialize_word_timings, validate_word_timings
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


@library.post("/api/save-lyrics")
def api_save_lyrics():
    data = json_body()
    song_id = data.get("song_id")
    lyrics_text = data.get("lyrics")
    words = data.get("words", None)

    if not isinstance(song_id, str) or not song_id:
        return jsonify({"error": "song_id is required"}), 400
    if not isinstance(lyrics_text, str) or not lyrics_text.strip():
        return jsonify({"error": "non-empty lyrics are required"}), 400
    if len(lyrics_text.encode("utf-8")) > current_app.config["MAX_LYRICS_BYTES"]:
        return jsonify({"error": "Lyrics file is too large"}), 400

    if words is not None:
        error = validate_word_timings(words)
        if error is not None:
            return jsonify({"error": f"Invalid word timings: {error}"}), 400
        try:
            words_json = serialize_word_timings(words, song_id)
        except ValueError:
            return jsonify({"error": "Word timings are not valid JSON"}), 400
        if len(words_json.encode("utf-8")) > current_app.config["MAX_WORDS_BYTES"]:
            return jsonify({"error": "Word timing file is too large"}), 400

    # The song must exist in the catalog: this guarantees the .srt name always
    # matches the audio stem and blocks path traversal.
    if find_song(current_app.config["SONGS_DIR"], song_id) is None:
        return jsonify({"error": "Song not found"}), 404

    try:
        result = save_lyrics(
            current_app.config["LYRICS_DIR"], song_id, lyrics_text, words)
    except OSError as exc:
        logger.error("Could not save lyrics for %s: %s", song_id, exc)
        return jsonify({"error": "Could not save lyrics."}), 500

    logger.info("Saved lyrics for %s -> %s (%d bytes)",
                song_id, result["saved"], len(lyrics_text.encode("utf-8")))
    return jsonify(result)
