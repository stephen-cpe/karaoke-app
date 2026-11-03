"""KARAOKE APP Flask application factory."""
from __future__ import annotations

import logging as stdlib_logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from karaoke.config import (
    DEFAULT_FFMPEG_FILTER,
    FFMPEG_TIMEOUT_SECONDS,
    MAX_LYRICS_BYTES,
    MAX_REQUEST_BYTES,
    MAX_SEMITONES,
    MAX_WORDS_BYTES,
)
from karaoke.errors import ApiError
from karaoke.logging import logger, setup_logging
from karaoke.routes import register_blueprints
from karaoke.version import APP_NAME, IS_PRE_ALPHA, __version__, version_label
from playback_state import PlaybackError, PlaybackState
from utils import single_line

_EDITOR_ENV_VAR = "KARAOKE_EDITOR"


def _resource_root() -> Path:
    """Return the root containing bundled templates and static assets.

    PyInstaller one-folder builds set ``sys._MEIPASS`` to the internal
    bundle directory.  User media always lives beside the executable, so it
    must not use this helper.
    """
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _env_flag(name: str) -> bool:
    """Return True for common truthy environment-variable values."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(songs_dir: Any = "songs", lyrics_dir: Any = "lyrics",
               transposed_dir: Any = "transposed",
               enable_editor: bool | None = None) -> Flask:
    if enable_editor is None:
        enable_editor = _env_flag(_EDITOR_ENV_VAR)
    project_root = _resource_root()
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
    # Config paths are resolved once, against the process working directory,
    # so Flask's send_from_directory() and the service modules never disagree
    # about what a relative "songs" / "lyrics" / "transposed" path means.
    app.config.update(
        SONGS_DIR=Path(songs_dir).expanduser().resolve(),
        LYRICS_DIR=Path(lyrics_dir).expanduser().resolve(),
        TRANSPOSED_DIR=Path(transposed_dir).expanduser().resolve(),
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        MAX_LYRICS_BYTES=MAX_LYRICS_BYTES,
        MAX_WORDS_BYTES=MAX_WORDS_BYTES,
        MAX_SEMITONES=MAX_SEMITONES,
        FFMPEG_FILTER=DEFAULT_FFMPEG_FILTER,
        FFMPEG_TIMEOUT_SECONDS=FFMPEG_TIMEOUT_SECONDS,
        ENABLE_EDITOR=bool(enable_editor),
        APP_NAME=APP_NAME,
        APP_VERSION=__version__,
        APP_VERSION_LABEL=version_label(),
        APP_PRE_ALPHA=IS_PRE_ALPHA,
    )
    # One shared karaoke session per server process. Tests create separate app
    # objects, so a test can never leak its queue into another test.
    app.extensions["playback"] = PlaybackState()
    # The desktop launcher sets this before closing Waitress.  SSE generators
    # check it so active browser streams do not keep the server thread alive.
    app.extensions["shutdown_event"] = threading.Event()
    register_blueprints(app)

    @app.errorhandler(ApiError)
    def _handle_api_error(error: ApiError):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(PlaybackError)
    def _handle_playback_error(error: PlaybackError):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_too_large(error: RequestEntityTooLarge):
        return jsonify({"error": "Request body is too large."}), 413

    @app.before_request
    def _start_request_timer():
        g.start_time = time.perf_counter()

    @app.after_request
    def _log_request(response):
        path = single_line(request.path)
        status = response.status_code
        if status >= 500:
            level = stdlib_logging.WARNING
        elif status == 404 and path.startswith("/lyrics/"):
            # Not every song has synced lyrics; a missing .srt is normal.
            level = stdlib_logging.INFO
        elif status >= 400:
            level = stdlib_logging.WARNING
        elif path.startswith(("/static/", "/songs/", "/transposed/", "/lyrics/")):
            # Static media can be fetched repeatedly (for example, audio
            # seeking); keep the log readable unless something goes wrong.
            level = stdlib_logging.DEBUG
        else:
            level = stdlib_logging.INFO
        duration_ms = (time.perf_counter() - g.get("start_time", time.perf_counter())) * 1000
        logger.log(level, "%s %s -> %s (%.0f ms) from %s",
                   request.method, path, status, duration_ms,
                   single_line(request.remote_addr or ""))
        return response

    return app


__all__ = ["create_app", "logger", "setup_logging"]
