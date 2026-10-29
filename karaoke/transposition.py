"""FFmpeg-backed pitch transposition helpers."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

from karaoke.catalog import song_stem
from karaoke.config import (
    DEFAULT_FFMPEG_FILTER,
    FFMPEG_TIMEOUT_SECONDS,
    MAX_SEMITONES,
)
from utils import parse_int


def check_ffmpeg() -> tuple[bool, str]:
    """Return ``(usable, human description)`` for the local FFmpeg install."""
    path = shutil.which("ffmpeg")
    if path is None:
        return False, ("not found on PATH - on-demand transposition needs FFmpeg, "
                       "but pre-generated files in transposed/ still play")
    try:
        version = subprocess.run(
            ["ffmpeg", "-hide_banner", "-version"],
            capture_output=True, timeout=10, check=False,
        )
        filters = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, f"found at {path}, but running it failed"
    if version.returncode != 0 or filters.returncode != 0:
        return False, f"found at {path}, but it returned an error"

    first_line = "unknown version"
    if version.stdout:
        lines = version.stdout.decode(errors="replace").splitlines()
        if lines:
            first_line = lines[0]
    if b"rubberband" in (filters.stdout or b""):
        return True, f"{first_line} (rubberband filter available)"
    return True, (f"{first_line} (rubberband filter NOT available - "
                  "set KARAOKE_FFMPEG_FILTER, see README)")


def transposed_filename(semitones: int, source_name: str) -> str:
    """Return the cache filename for one source song and semitone offset."""
    return f"{semitones:+d}_{song_stem(source_name)}.mp3"


def build_audio_filter(semitones: int, template: str = DEFAULT_FFMPEG_FILTER) -> str:
    """Render the configured FFmpeg filter template for a semitone offset."""
    pitch_factor = 2 ** (semitones / 12)
    try:
        return template.format(pitch=pitch_factor)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("Invalid KARAOKE_FFMPEG_FILTER template") from exc


def run_transposition(source_path: str, destination_path: str,
                      audio_filter: str,
                      timeout: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    """Run FFmpeg and move the finished file into place.

    The caller is responsible for creating ``destination_path``'s parent
    directory and for creating/cleaning the temporary output path.
    """
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", source_path,
        "-vn", "-ar", "44100",
        "-filter:a", audio_filter,
        "-b:a", "192k", "-f", "mp3",
        destination_path,
    ]
    subprocess.run(
        command, check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
    )


def validate_semitones(value: Any) -> int:
    """Parse and range-check a semitone offset."""
    semitones = parse_int(value, "semitones must be an integer")
    if abs(semitones) > MAX_SEMITONES:
        raise ValueError(
            f"semitones must be between -{MAX_SEMITONES} and {MAX_SEMITONES}"
        )
    return semitones
