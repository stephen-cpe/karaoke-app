"""Application version metadata shared by the web app and desktop launcher.

The Git tag is the release authority.  CI validates the tag against this
module before building, so the executable, GUI, and release metadata cannot
silently drift apart.
"""
from __future__ import annotations

APP_NAME = "KARAOKE APP"
__version__ = "0.0.1"
IS_PRE_ALPHA = True


def version_label() -> str:
    """Return the user-facing version label, including pre-alpha status."""
    if IS_PRE_ALPHA:
        return f"{__version__} (PRE-ALPHA)"
    return __version__


__all__ = ["APP_NAME", "__version__", "IS_PRE_ALPHA", "version_label"]
