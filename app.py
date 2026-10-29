"""KARAOKE APP — Flask server entry point.

The application factory and route blueprints live in the ``karaoke`` package.
This module keeps the historical ``python app.py`` entry point (and the
``from app import ...`` compatibility surface used by tests and scripts).
"""
from __future__ import annotations

import argparse
import logging
import os

from constants import DEFAULT_PORT
from karaoke import create_app, logger, setup_logging
from karaoke.catalog import parse_song_filename, scan_songs
from karaoke.http import base_url, local_ip
from karaoke.transposition import check_ffmpeg

__all__ = [
    "app",
    "check_ffmpeg",
    "create_app",
    "logger",
    "parse_song_filename",
    "scan_songs",
    "setup_logging",
]

app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the KARAOKE APP server.")
    parser.add_argument(
        "--host", default=None,
        help="Interface to bind (default: 0.0.0.0; 127.0.0.1 with --dev)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--dev", action="store_true",
        help="Use the Flask dev server with auto-reload instead of waitress")
    parser.add_argument(
        "--threads", type=int, default=16,
        help="Waitress worker threads (default: 16; each remote SSE "
             "connection holds one thread)")
    args = parser.parse_args()

    level_name = os.environ.get("KARAOKE_LOG_LEVEL", "INFO").upper()
    setup_logging(level=getattr(logging, level_name, logging.INFO))
    host = args.host or ("127.0.0.1" if args.dev else "0.0.0.0")

    logger.info("Starting KARAOKE APP (dev=%s, log level %s)", args.dev, level_name)
    for key in ("SONGS_DIR", "LYRICS_DIR", "TRANSPOSED_DIR"):
        logger.info("%s: %s", key, os.path.abspath(app.config[key]))
    logger.info("Catalog: %d song(s) found", len(scan_songs(app.config["SONGS_DIR"])))
    ffmpeg_ok, ffmpeg_description = check_ffmpeg()
    (logger.info if ffmpeg_ok else logger.warning)("ffmpeg: %s", ffmpeg_description)

    if args.dev:
        # Keep the Werkzeug debugger off the LAN; it is a development tool,
        # not a remote administration surface.
        app.run(host=host, port=args.port, debug=True, threaded=True)
    else:
        from waitress import serve

        lan_url = base_url(local_ip(), args.port)
        print(f"KARAOKE APP player (open on this PC): http://localhost:{args.port}/player")
        print(f"Phone remotes: {lan_url}/remote")
        print(f"From other devices on your network: {lan_url}")
        print(f"Logs are written to {os.path.abspath('karaoke.log')} - "
              "check there if something fails.")
        # SSE streams keep one Waitress thread open per connected browser.
        serve(app, host=host, port=args.port, threads=max(4, args.threads))


if __name__ == "__main__":
    main()
