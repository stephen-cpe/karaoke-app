"""KARAOKE APP — Flask backend.

Serves the karaoke player and sync editor, exposes the song catalog
(scanned live from the songs folder), performs on-demand pitch
transposition with FFmpeg, and saves synced lyrics from the sync editor.
"""
import argparse
import ipaddress
import json
import logging
import os
import queue as thread_queue
import shutil
import socket
import subprocess
import tempfile
import time
from logging.handlers import RotatingFileHandler

from flask import (Flask, Response, abort, g, jsonify, render_template, request,
                   send_from_directory, stream_with_context)
from playback_state import PlaybackError, PlaybackState, format_sse
from utils import is_finite_number, single_line

AUDIO_EXTENSIONS = (".mp3", ".wav")
LYRICS_EXTENSIONS = (".srt", ".words.json")
BACKGROUND_EXTENSIONS = (".mp4", ".webm")
MAX_SEMITONES = 4  # Matches the +/-4 transpose buttons in the UI
MAX_LYRICS_BYTES = 1_000_000  # Sanity cap for saved .srt files
MAX_WORDS_BYTES = 2_000_000  # Sanity cap for saved .words.json files
DEFAULT_FFMPEG_FILTER = "rubberband=pitch={pitch}"
FFMPEG_TIMEOUT_SECONDS = 300  # One song should never take this long on a laptop

logger = logging.getLogger("karaoke")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_file="karaoke.log", level=logging.INFO):
    """Log to the console and to a rotating file, so problems survive a closed window.

    Handlers are tagged so calling this twice (tests, dev reloader) does not
    stack duplicates.
    """
    logger.setLevel(logging.DEBUG)  # the handlers decide what is actually emitted
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_karaoke", False):
            root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    console._karaoke = True
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._karaoke = True
    root.addHandler(console)
    root.addHandler(file_handler)


def check_ffmpeg():
    """Return (usable, human description) for the local FFmpeg installation."""
    path = shutil.which("ffmpeg")
    if path is None:
        return False, ("not found on PATH - on-demand transposition needs FFmpeg, "
                       "but pre-generated files in transposed/ still play")
    try:
        version = subprocess.run(["ffmpeg", "-hide_banner", "-version"],
                                 capture_output=True, timeout=10)
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                 capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True, f"found at {path}, but running it failed"

    first_line = "unknown version"
    if version.stdout:
        lines = version.stdout.decode(errors="replace").splitlines()
        if lines:
            first_line = lines[0]
    if b"rubberband" in (filters.stdout or b""):
        return True, f"{first_line} (rubberband filter available)"
    return True, (f"{first_line} (rubberband filter NOT available - "
                  "set KARAOKE_FFMPEG_FILTER, see README)")


def parse_song_filename(filename):
    """Parse 'id_Title_Artist.ext' into song metadata (best effort).

    Rules:
      - everything before the first '_' is the song id
      - everything after the last '_' is the artist
      - the middle is the title (may contain underscores)
    Files without an underscore are treated as a bare title, and
    'id_Title.ext' works too (artist left blank).
    """
    stem = os.path.splitext(filename)[0]
    if "_" not in stem:
        return {"id": "", "title": stem, "artist": "", "filename": filename}
    song_id, rest = stem.split("_", 1)
    title, sep, artist = rest.rpartition("_")
    if not sep:
        title, artist = rest, ""
    return {"id": song_id, "title": title, "artist": artist, "filename": filename}


def scan_songs(songs_dir):
    """Return playable audio files in songs_dir as song dicts, sorted by title."""
    try:
        with os.scandir(songs_dir) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
    except OSError:
        return []
    found = [
        parse_song_filename(name)
        for name in names
        if name.lower().endswith(AUDIO_EXTENSIONS)
    ]
    found.sort(key=lambda song: (song["title"].casefold(), song["id"]))
    return found


def find_song(songs_dir, filename):
    """Resolve a client-supplied song filename to its catalog entry (or None).

    Rejects anything that is not a plain filename (no separators, no path
    traversal, no NUL bytes) or that does not exist on disk, so callers can
    return 404.
    """
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        return None
    if (filename != os.path.basename(filename)
            or "/" in filename or "\\" in filename):
        return None
    if not filename.lower().endswith(AUDIO_EXTENSIONS):
        return None
    if not os.path.isfile(os.path.join(songs_dir, filename)):
        return None
    return parse_song_filename(filename)


def _serve_media(directory, filename, extensions):
    """Serve only expected media types from a directory (404 otherwise)."""
    if (not isinstance(filename, str) or "\x00" in filename
            or not filename.lower().endswith(extensions)):
        abort(404)
    return send_from_directory(directory, filename)


def _local_ip():
    """Best-effort LAN address for the phone join link."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Connecting a UDP socket only selects the outgoing interface;
            # it does not send any packets.
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    return "127.0.0.1"


def _base_url(host, port):
    if port in (None, "", "80", 80):
        return f"http://{host}"
    return f"http://{host}:{port}"


def _request_port():
    try:
        return int(request.environ.get("SERVER_PORT") or 8080)
    except (TypeError, ValueError):
        return 8080


def _normalize_text(text):
    return " ".join(str(text).split())


def validate_word_timings(data):
    """Return an error string for an invalid .words.json payload, else None.

    This deliberately checks the structural rules enforced by the player and
    ``tools/check_lyrics.py`` so a manual save cannot corrupt word highlighting.
    Line-level cues without word timings are allowed: manual edits are allowed
    to keep one cue's timing without the word-level data, and the player falls
    back to line-level display for that cue.
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
        if end <= start:
            return f"words.lines[{index}] end must be after start"
        if start < previous_end - 0.001:
            return f"words.lines[{index}] overlaps a previous cue"
        previous_end = max(previous_end, end)

        text = line.get("text")
        if not isinstance(text, str) or not _normalize_text(text):
            return f"words.lines[{index}].text must be a non-empty string"

        instrumental = bool(line.get("instrumental"))
        words = line.get("words", [])
        if not isinstance(words, list):
            return f"words.lines[{index}].words must be an array"

        if instrumental:
            if _normalize_text(text) != "[Instrumental]":
                return f"words.lines[{index}] instrumental text must be [Instrumental]"
            if words:
                return f"words.lines[{index}] is instrumental but has word timings"
            continue

        # Lyric text without any word timings is allowed (line-level fallback).
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


def _write_text(path, text):
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


def create_app(songs_dir="songs", lyrics_dir="lyrics",
               transposed_dir="transposed", background_dir="background"):
    app = Flask(__name__)
    app.config.update(
        SONGS_DIR=songs_dir,
        LYRICS_DIR=lyrics_dir,
        TRANSPOSED_DIR=transposed_dir,
        BACKGROUND_DIR=background_dir,
    )
    # One shared karaoke session per server process. Tests create separate
    # app objects, so a test can never leak its queue into another test.
    app.extensions["playback"] = PlaybackState()

    def playback_state():
        return app.extensions["playback"]

    def is_local_request():
        address = request.remote_addr or ""
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return address.lower() in {"localhost", ""}
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return parsed.is_loopback

    def client_id_from(data):
        value = data.get("client_id") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise PlaybackError("client_id is required.", 400)
        return value.strip()[:120]

    @app.errorhandler(PlaybackError)
    def _handle_playback_error(error):
        return jsonify({"error": error.message}), error.status_code

    @app.before_request
    def _start_request_timer():
        g.start_time = time.perf_counter()

    @app.after_request
    def _log_request(response):
        path = request.path
        status = response.status_code
        if status >= 500:
            level = logging.WARNING
        elif status == 404 and path.startswith("/lyrics/"):
            # Not every song has synced lyrics; a missing .srt is normal.
            level = logging.INFO
        elif status >= 400:
            level = logging.WARNING
        elif path.startswith(("/static/", "/background/", "/songs/", "/transposed/", "/lyrics/")):
            # Media/static files can be fetched repeatedly (video loops, audio
            # seeking); keep the log readable unless something goes wrong.
            level = logging.DEBUG
        else:
            level = logging.INFO
        duration_ms = (time.perf_counter() - g.get("start_time", time.perf_counter())) * 1000
        logger.log(level, "%s %s -> %s (%.0f ms) from %s",
                   request.method, path, status, duration_ms, request.remote_addr)
        return response

    # --- Pages ----------------------------------------------------------
    @app.route("/")
    def index():
        # The browser on the karaoke machine (localhost) is the audio player.
        # Every phone/tablet on the LAN gets the controls-only remote.
        return render_template("index.html" if is_local_request() else "remote.html")

    @app.get("/player", strict_slashes=False)
    def player_page():
        return render_template("index.html")

    @app.get("/remote", strict_slashes=False)
    def remote_page():
        return render_template("remote.html")

    @app.route("/sync_editor.html")
    def sync_editor():
        return render_template("sync_editor.html")

    # --- API: shared session -------------------------------------------
    @app.get("/api/state")
    def api_state():
        state = playback_state()
        state.refresh_presence()
        return jsonify({"state": state.snapshot()})

    @app.get("/api/events")
    def api_events():
        role = request.args.get("role", "remote")
        if role not in {"host", "remote"}:
            role = "remote"
        client_id = (request.args.get("client_id") or "").strip()[:120]
        state = playback_state()
        subscriber = state.subscribe(role, client_id)

        def generate():
            try:
                yield "retry: 3000\n\n"
                yield format_sse("state", state.snapshot())
                while True:
                    try:
                        event, data = subscriber.events.get(timeout=15)
                    except thread_queue.Empty:
                        state.refresh_presence()
                        yield ": keep-alive\n\n"
                        continue
                    yield format_sse(event, data)
            finally:
                state.unsubscribe(subscriber)

        response = Response(stream_with_context(generate()),
                            mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/control")
    def api_control():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400

        action = data.get("action")
        state = playback_state()
        state.refresh_presence()
        extra = {}

        if action == "add":
            filename = data.get("filename")
            song = find_song(app.config["SONGS_DIR"], filename)
            if song is None:
                return jsonify({"error": "Song not found."}), 404
            stem = os.path.splitext(song["filename"])[0]
            song["lyrics"] = os.path.isfile(
                os.path.join(app.config["LYRICS_DIR"], stem + ".srt"))
            song["words"] = os.path.isfile(
                os.path.join(app.config["LYRICS_DIR"], stem + ".words.json"))
            result = state.add_song(song)
            snapshot = result["snapshot"]
            extra["started"] = result["started"]
        elif action == "remove":
            snapshot = state.remove_queue(data.get("index"))
        elif action == "clear":
            snapshot = state.clear_queue()
        elif action == "reorder":
            snapshot = state.reorder_queue(data.get("from"), data.get("to"))
        elif action == "skip":
            snapshot = state.skip_remote()
        elif action == "play":
            snapshot = state.play()
        elif action == "pause":
            snapshot = state.pause()
        elif action == "restart":
            snapshot = state.restart()
        elif action == "seek":
            snapshot = state.seek(data.get("position"))
        elif action == "transpose":
            snapshot = state.set_transpose(
                data.get("semitones", data.get("value")))
        elif action == "volume":
            snapshot = state.set_volume(data.get("volume"))
        elif action == "stop":
            snapshot = state.stop()
        else:
            return jsonify({"error": "Unknown control action."}), 400

        return jsonify({"state": snapshot, **extra})

    @app.get("/api/info")
    def api_info():
        port = _request_port()
        lan_ip = _local_ip()
        local = is_local_request()
        state = playback_state()
        state.refresh_presence()
        snapshot = state.snapshot()
        return jsonify({
            "role": "host" if local else "remote",
            "server_id": snapshot["server_id"],
            "host_connected": snapshot["host_connected"],
            "join_url": _base_url(lan_ip, port),
            "remote_url": _base_url(lan_ip, port) + "/remote",
            "player_url": _base_url("localhost", port) + "/player",
            "lan_ip": lan_ip,
            "port": port,
        })

    @app.post("/api/host/register")
    def api_host_register():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400
        state = playback_state()
        snapshot = state.register_host(client_id_from(data))
        return jsonify({
            "state": snapshot,
            "commands": state.pending_commands(),
        })

    @app.post("/api/host/report")
    def api_host_report():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400
        state = playback_state()
        client_id = client_id_from(data)
        report = {key: value for key, value in data.items()
                  if key != "client_id"}
        snapshot = state.report_host(client_id, report)
        # Piggyback queued commands on the heartbeat so a host whose SSE
        # connection dropped still receives controls.
        return jsonify({
            "state": snapshot,
            "commands": state.pending_commands(),
        })

    @app.get("/api/host/commands")
    def api_host_commands():
        state = playback_state()
        client_id = client_id_from(request.args.to_dict())
        return jsonify({
            "commands": state.commands_for_host(client_id),
            "state": state.snapshot(),
        })

    @app.post("/api/host/ended")
    def api_host_ended():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400
        snapshot = playback_state().host_ended(
            client_id_from(data),
            data.get("last_command_id"),
            data.get("filename"),
        )
        return jsonify({"state": snapshot})

    @app.post("/api/host/error")
    def api_host_error():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400
        snapshot = playback_state().host_error(
            client_id_from(data),
            data.get("message"),
            data.get("last_command_id"),
            data.get("filename"),
        )
        return jsonify({"state": snapshot})

    @app.post("/api/host/disconnect")
    def api_host_disconnect():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body."}), 400
        return jsonify({
            "state": playback_state().disconnect_host(client_id_from(data)),
        })

    # --- PWA -------------------------------------------------------------
    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        return send_from_directory("static", "manifest.webmanifest",
                                   mimetype="application/manifest+json")

    @app.get("/sw.js")
    def pwa_service_worker():
        response = send_from_directory("static", "sw.js",
                                       mimetype="text/javascript")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    # --- Media / static file serving ------------------------------------
    @app.route("/<path:filename>")
    def serve_static(filename):
        return send_from_directory("static", filename)

    @app.route("/songs/<path:filename>")
    def serve_song(filename):
        return _serve_media(app.config["SONGS_DIR"], filename, AUDIO_EXTENSIONS)

    @app.route("/lyrics/<path:filename>")
    def serve_lyrics(filename):
        return _serve_media(app.config["LYRICS_DIR"], filename, LYRICS_EXTENSIONS)

    @app.route("/transposed/<path:filename>")
    def serve_transposed(filename):
        return _serve_media(app.config["TRANSPOSED_DIR"], filename, AUDIO_EXTENSIONS)

    @app.route("/background/<path:filename>")
    def serve_background(filename):
        return _serve_media(app.config["BACKGROUND_DIR"], filename, BACKGROUND_EXTENSIONS)

    # --- API: song catalog ------------------------------------------------
    @app.get("/api/songs")
    def api_songs():
        catalog = scan_songs(app.config["SONGS_DIR"])
        lyrics_dir = app.config["LYRICS_DIR"]
        for song in catalog:
            stem = os.path.splitext(song["filename"])[0]
            song["lyrics"] = os.path.isfile(os.path.join(lyrics_dir, stem + ".srt"))
            song["words"] = os.path.isfile(os.path.join(lyrics_dir, stem + ".words.json"))
        return jsonify({"songs": catalog})

    # --- API: transposition ------------------------------------------------
    @app.route("/transpose", methods=["GET", "POST"])
    def transpose_audio():
        if request.method == "POST":
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON body."}), 400
            song_id = data.get("song_id")
            semitones = data.get("semitones", 0)
        else:
            song_id = request.args.get("song_id")
            semitones = request.args.get("semitones", 0)

        if not isinstance(song_id, str) or not song_id:
            return jsonify({"error": "Missing song_id"}), 400

        try:
            semitones = int(semitones)
        except (TypeError, ValueError):
            return jsonify({"error": "semitones must be an integer"}), 400

        if abs(semitones) > MAX_SEMITONES:
            return jsonify({
                "error": f"semitones must be between -{MAX_SEMITONES} and {MAX_SEMITONES}"
            }), 400

        song = find_song(app.config["SONGS_DIR"], song_id)
        if song is None:
            logger.warning(
                "Transpose: song %r not found in %s (%d songs available)",
                song_id, app.config["SONGS_DIR"],
                len(scan_songs(app.config["SONGS_DIR"])),
            )
            return jsonify({"error": "Song not found"}), 404

        source_name = song["filename"]
        logger.info("Transpose: %s (%+d semitones)", source_name, semitones)

        if semitones == 0:
            # No change needed, serve the original song.
            return jsonify({"url": f"/songs/{source_name}"})

        songs_dir = app.config["SONGS_DIR"]
        transposed_dir = app.config["TRANSPOSED_DIR"]
        original_path = os.path.abspath(os.path.join(songs_dir, source_name)).replace("\\", "/")
        stem = os.path.splitext(source_name)[0]
        transposed_filename = f"{semitones:+d}_{stem}.mp3"
        transposed_path = os.path.abspath(os.path.join(transposed_dir, transposed_filename)).replace("\\", "/")

        if os.path.exists(transposed_path):
            logger.info("Transpose: serving cached %s", transposed_filename)
            return jsonify({"url": f"/transposed/{transposed_filename}"})

        try:
            os.makedirs(transposed_dir, exist_ok=True)
        except OSError as e:
            logger.error("Transpose: could not create %s: %s", transposed_dir, e)
            return jsonify({"error": "Could not create the transposition cache."}), 500

        # Pitch factor: 2 ** (semitones / 12). The default rubberband filter
        # shifts pitch while keeping tempo; override KARAOKE_FFMPEG_FILTER if
        # your FFmpeg build lacks librubberband (see README).
        pitch_factor = 2 ** (semitones / 12)
        template = os.environ.get("KARAOKE_FFMPEG_FILTER", DEFAULT_FFMPEG_FILTER)
        try:
            audio_filter = template.format(pitch=pitch_factor)
        except (KeyError, IndexError, ValueError):
            return jsonify({"error": "Invalid KARAOKE_FFMPEG_FILTER template"}), 500

        # Encode to a temporary file and rename only after a successful run, so
        # a failed or interrupted FFmpeg cannot leave a corrupt cache entry.
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".transpose-", suffix=".mp3", dir=transposed_dir)
        except OSError as e:
            logger.error("Transpose: could not create a temporary file: %s", e)
            return jsonify({"error": "Could not create a temporary file."}), 500
        os.close(fd)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", original_path,
            "-vn", "-ar", "44100",
            "-filter:a", audio_filter,
            "-b:a", "192k", "-f", "mp3",
            temp_path,
        ]
        logger.info("Transpose: generating %s with ffmpeg", transposed_filename)
        logger.debug("ffmpeg command: %s", " ".join(cmd))

        try:
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_SECONDS)
            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
                raise RuntimeError("ffmpeg produced no output")
            os.replace(temp_path, transposed_path)
        except FileNotFoundError:
            logger.error("Transpose: ffmpeg not found on PATH while processing %s", source_name)
            return jsonify({
                "error": "ffmpeg not found. Install FFmpeg and add it to your PATH."
            }), 500
        except subprocess.TimeoutExpired:
            logger.error("Transpose: ffmpeg timed out after %d seconds for %s",
                         FFMPEG_TIMEOUT_SECONDS, source_name)
            return jsonify({
                "error": f"Transposition timed out after {FFMPEG_TIMEOUT_SECONDS} seconds."
            }), 504
        except subprocess.CalledProcessError as e:
            details = e.stderr.decode(errors="replace") if e.stderr else str(e)
            logger.error("Transpose failed for %s: %s", source_name, details)
            return jsonify({"error": "Transposition failed", "details": details}), 500
        except (OSError, RuntimeError) as e:
            logger.error("Transpose failed for %s: %s", source_name, e)
            return jsonify({"error": "Transposition failed", "details": str(e)}), 500
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

        logger.info("Transpose: wrote %s", transposed_filename)
        return jsonify({"url": f"/transposed/{transposed_filename}"})

    # --- API: save lyrics from the sync editor -----------------------------
    @app.post("/api/save-lyrics")
    def api_save_lyrics():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON body"}), 400

        song_id = data.get("song_id")
        lyrics = data.get("lyrics")
        words = data.get("words", None)

        if not isinstance(song_id, str) or not song_id:
            return jsonify({"error": "song_id is required"}), 400

        if not isinstance(lyrics, str) or not lyrics.strip():
            return jsonify({"error": "non-empty lyrics are required"}), 400
        if len(lyrics.encode("utf-8")) > MAX_LYRICS_BYTES:
            return jsonify({"error": "Lyrics file is too large"}), 400

        if words is not None:
            error = validate_word_timings(words)
            if error is not None:
                return jsonify({"error": f"Invalid word timings: {error}"}), 400
            words = dict(words)
            words["song"] = os.path.splitext(song_id)[0]
            words.setdefault("version", 1)
            try:
                words_json = json.dumps(words, ensure_ascii=False, indent=2) + "\n"
            except (TypeError, ValueError):
                return jsonify({"error": "Word timings are not valid JSON"}), 400
            if len(words_json.encode("utf-8")) > MAX_WORDS_BYTES:
                return jsonify({"error": "Word timing file is too large"}), 400

        # The song must exist in the catalog: this guarantees the .srt name
        # always matches the audio stem and blocks path traversal.
        if find_song(app.config["SONGS_DIR"], song_id) is None:
            return jsonify({"error": "Song not found"}), 404

        lyrics_dir = app.config["LYRICS_DIR"]
        os.makedirs(lyrics_dir, exist_ok=True)
        srt_name = os.path.splitext(song_id)[0] + ".srt"
        words_name = os.path.splitext(song_id)[0] + ".words.json"

        _write_text(os.path.join(lyrics_dir, srt_name), lyrics)

        if words is not None:
            _write_text(os.path.join(lyrics_dir, words_name), words_json)
            logger.info("Saved word timings for %s -> %s (%d bytes)",
                        song_id, words_name, len(words_json.encode("utf-8")))
        else:
            words_path = os.path.join(lyrics_dir, words_name)
            if os.path.isfile(words_path):
                os.remove(words_path)
                logger.info("Removed stale word timings %s", words_name)

        logger.info("Saved lyrics for %s -> %s (%d bytes)",
                    song_id, srt_name, len(lyrics.encode("utf-8")))
        return jsonify({
            "saved": srt_name,
            "words": words_name if words is not None else None,
        })

    # --- API: browser-side error reporting ---------------------------------
    @app.post("/api/log")
    def api_log():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data.get("message"):
            return jsonify({"error": "message is required"}), 400

        level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(str(data.get("level", "error")).lower(), logging.ERROR)

        # Collapse line breaks so a browser cannot forge extra log lines.
        message = single_line(data["message"], 2000)
        details = data.get("details")
        if details is not None:
            try:
                details_text = json.dumps(details, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                details_text = repr(details)
            message += " | " + single_line(details_text, 2000)

        logger.log(level, "[browser] %s", message)
        return "", 204

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description="Run the KARAOKE APP server.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to bind (default: 0.0.0.0, all interfaces)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port to listen on (default: 8080)")
    parser.add_argument("--dev", action="store_true",
                        help="Use the Flask dev server with auto-reload instead of waitress")
    parser.add_argument("--threads", type=int, default=16,
                        help="Waitress worker threads (default: 16; each remote SSE "
                             "connection holds one thread)")
    args = parser.parse_args()

    level_name = os.environ.get("KARAOKE_LOG_LEVEL", "INFO").upper()
    setup_logging(level=getattr(logging, level_name, logging.INFO))

    logger.info("Starting KARAOKE APP (dev=%s, log level %s)", args.dev, level_name)
    for key in ("SONGS_DIR", "LYRICS_DIR", "TRANSPOSED_DIR", "BACKGROUND_DIR"):
        logger.info("%s: %s", key, os.path.abspath(app.config[key]))
    logger.info("Catalog: %d song(s) found", len(scan_songs(app.config["SONGS_DIR"])))
    ffmpeg_ok, ffmpeg_description = check_ffmpeg()
    (logger.info if ffmpeg_ok else logger.warning)("ffmpeg: %s", ffmpeg_description)

    if args.dev:
        app.run(host=args.host, port=args.port, debug=True, threaded=True)
    else:
        from waitress import serve
        lan_url = _base_url(_local_ip(), args.port)
        print(f"KARAOKE APP player (open on this PC): http://localhost:{args.port}/player")
        print(f"Phone remotes: {lan_url}/remote")
        print(f"From other devices on your network: {lan_url}")
        print(f"Logs are written to {os.path.abspath('karaoke.log')} - check there if something fails.")
        # SSE streams keep one Waitress thread open per connected browser.
        serve(app, host=args.host, port=args.port, threads=max(4, args.threads))


if __name__ == "__main__":
    main()