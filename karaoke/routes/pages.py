"""Page, PWA, and media-serving routes."""
from __future__ import annotations

import os

from flask import Blueprint, abort, current_app, render_template, send_from_directory

from karaoke.config import AUDIO_EXTENSIONS, LYRICS_EXTENSIONS
from karaoke.http import editor_access_allowed, is_local_request

pages = Blueprint("pages", __name__)


@pages.get("/")
def index():
    # The browser on the karaoke machine (localhost) is the audio player.
    # Every phone/tablet on the LAN gets the controls-only remote.
    if is_local_request():
        return render_template(
            "index.html", editor_available=editor_access_allowed())
    return render_template("remote.html")


@pages.get("/player", strict_slashes=False)
def player_page():
    return render_template(
        "index.html", editor_available=editor_access_allowed())


@pages.get("/remote", strict_slashes=False)
def remote_page():
    return render_template("remote.html")


@pages.get("/sync_editor.html")
@pages.get("/lyrics-editor")
def sync_editor():
    if not editor_access_allowed():
        abort(404)
    return render_template("sync_editor.html")


@pages.get("/manifest.webmanifest")
def pwa_manifest():
    return send_from_directory(
        current_app.static_folder, "manifest.webmanifest",
        mimetype="application/manifest+json")


@pages.get("/sw.js")
def pwa_service_worker():
    response = send_from_directory(current_app.static_folder, "sw.js", mimetype="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


def _serve_media(directory: str, filename: str, extensions: tuple[str, ...]):
    """Serve only expected media types from a directory (404 otherwise)."""
    if (not isinstance(filename, str) or "\x00" in filename
            or not filename.lower().endswith(extensions)):
        abort(404)
    return send_from_directory(os.fspath(directory), filename)


@pages.get("/songs/<path:filename>")
def serve_song(filename: str):
    return _serve_media(current_app.config["SONGS_DIR"], filename, AUDIO_EXTENSIONS)


@pages.get("/lyrics/<path:filename>")
def serve_lyrics(filename: str):
    # Active lyrics live directly under lyrics/. Drafts and other nested
    # files must never be reachable through the player-facing media route.
    if "/" in filename or "\\" in filename:
        abort(404)
    return _serve_media(current_app.config["LYRICS_DIR"], filename, LYRICS_EXTENSIONS)


@pages.get("/transposed/<path:filename>")
def serve_transposed(filename: str):
    return _serve_media(
        current_app.config["TRANSPOSED_DIR"], filename, AUDIO_EXTENSIONS)


# Keep this catch-all last in this blueprint. Flask prefers the more specific
# routes above, and unknown paths still resolve to a static-file lookup.
@pages.get("/<path:filename>")
def serve_static(filename: str):
    return send_from_directory(current_app.static_folder, filename)
