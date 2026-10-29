"""Logging configuration shared by the server and tests."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("karaoke")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_file: str = "karaoke.log", level: int = logging.INFO) -> None:
    """Log to the console and a rotating file, replacing previous handlers.

    Handlers are tagged so calling this twice (tests, dev reloader) does not
    stack duplicates or leak file descriptors.
    """
    logger.setLevel(logging.DEBUG)  # handlers decide what is actually emitted
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_karaoke", False):
            handler.close()
            root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    console._karaoke = True
    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._karaoke = True
    root.addHandler(console)
    root.addHandler(file_handler)
