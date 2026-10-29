"""Application-wide constants shared by the Flask app and session state."""
from __future__ import annotations

# Media ---------------------------------------------------------------
AUDIO_EXTENSIONS = (".mp3", ".wav")
LYRICS_EXTENSIONS = (".srt", ".words.json")

# Playback / transposition --------------------------------------------
MAX_SEMITONES = 4  # Matches the +/-4 transpose buttons in the UI
DEFAULT_FFMPEG_FILTER = "rubberband=pitch={pitch}"
FFMPEG_TIMEOUT_SECONDS = 300  # One song should never take this long on a laptop

# Lyrics --------------------------------------------------------------
MAX_LYRICS_BYTES = 1_000_000
MAX_WORDS_BYTES = 2_000_000

# Request / API -------------------------------------------------------
MAX_CLIENT_ID_LENGTH = 120
MAX_LOG_MESSAGE_LENGTH = 2_000
MAX_REQUEST_BYTES = 8_000_000
DEFAULT_PORT = 8080

# Host presence -------------------------------------------------------
HOST_TIMEOUT_SECONDS = 8.0
SSE_KEEPALIVE_SECONDS = 5.0
