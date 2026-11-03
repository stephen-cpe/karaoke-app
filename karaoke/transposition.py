"""FFmpeg-backed pitch transposition helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from karaoke.catalog import song_stem
from karaoke.config import (
    DEFAULT_FFMPEG_FILTER,
    FFMPEG_TIMEOUT_SECONDS,
    MAX_SEMITONES,
)
from utils import parse_int


class TranspositionCancelled(RuntimeError):
    """Raised when the desktop app is shutting down during FFmpeg work."""


_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_SHUTTING_DOWN = threading.Event()


def ffmpeg_executable() -> str:
    """Return the FFmpeg command or bundled executable path.

    An explicit ``KARAOKE_FFMPEG`` wins, which is useful for advanced local
    setups.  A frozen application looks beside its executable first so the
    distributed FFmpeg is used without PATH configuration.  Development runs
    fall back to the normal ``ffmpeg`` PATH lookup.
    """
    explicit = os.environ.get("KARAOKE_FFMPEG", "").strip()
    if explicit:
        return explicit

    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        bundled = (
            app_dir / "ffmpeg" / "ffmpeg.exe",
            app_dir / "ffmpeg" / "bin" / "ffmpeg.exe",
            app_dir / "ffmpeg.exe",
        )
        for candidate in bundled:
            if candidate.is_file():
                return str(candidate)

    return "ffmpeg"


def check_ffmpeg() -> tuple[bool, str]:
    """Return ``(usable, human description)`` for the local FFmpeg install."""
    executable = ffmpeg_executable()
    if os.path.isabs(executable):
        path = executable
        if not os.path.isfile(path):
            return False, ("not found on PATH - on-demand transposition needs FFmpeg, "
                           "but pre-generated files in transposed/ still play")
    else:
        path = shutil.which(executable)
        if path is None:
            return False, ("not found on PATH - on-demand transposition needs FFmpeg, "
                           "but pre-generated files in transposed/ still play")
    try:
        version = subprocess.run(
            [executable, "-hide_banner", "-version"],
            capture_output=True, timeout=10, check=False,
        )
        filters = subprocess.run(
            [executable, "-hide_banner", "-filters"],
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


def _run_ffmpeg_process(command: list[str], timeout: int) -> None:
    """Run one FFmpeg process and make it cancellable at application exit."""
    if _SHUTTING_DOWN.is_set():
        raise TranspositionCancelled("The application is shutting down.")

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(process)
    try:
        if _SHUTTING_DOWN.is_set():
            process.terminate()
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if _SHUTTING_DOWN.is_set():
            raise TranspositionCancelled("The application is shutting down.")
        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode, command, output=None, stderr=stderr or b"")
    finally:
        with _PROCESS_LOCK:
            _ACTIVE_PROCESSES.discard(process)


def cancel_active_transpositions(timeout: float = 3.0) -> None:
    """Terminate FFmpeg children so the desktop app can exit cleanly."""
    _SHUTTING_DOWN.set()
    with _PROCESS_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        try:
            process.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass


def run_transposition(source_path: str, destination_path: str,
                      audio_filter: str,
                      timeout: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    """Run FFmpeg and move the finished file into place.

    The caller is responsible for creating ``destination_path``'s parent
    directory and for creating/cleaning the temporary output path.
    """
    command = [
        ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", source_path,
        "-vn", "-ar", "44100",
        "-filter:a", audio_filter,
        "-b:a", "192k", "-f", "mp3",
        destination_path,
    ]
    _run_ffmpeg_process(command, timeout)


def validate_semitones(value: Any) -> int:
    """Parse and range-check a semitone offset."""
    semitones = parse_int(value, "semitones must be an integer")
    if abs(semitones) > MAX_SEMITONES:
        raise ValueError(
            f"semitones must be between -{MAX_SEMITONES} and {MAX_SEMITONES}"
        )
    return semitones
