"""Audio probing and vocal-energy helpers shared by the lyric stages."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("lyrics")

SAMPLE_RATE = 16000
SILENCE_RMS = 0.0015
ONSET_RMS = 0.003
STRONG_ONSET_RMS = 0.02
PHRASE_GAP = 2.5
PHRASE_LINGER = 0.6
HELD_NOTE_RMS = 0.003


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kwargs)


def duration_of(path: Path) -> float:
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def load_vocal_audio(vocals: Path):
    """Decode the vocal stem once to mono 16 kHz float samples."""
    try:
        import numpy as np
    except ImportError:
        log.warning("numpy is not installed; vocal-energy helpers are unavailable")
        return None

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(vocals), "-ac", "1", "-ar",
         str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        log.warning("ffmpeg could not decode %s", vocals)
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def window_rms(audio, start: float, end: float,
               window: float = 0.05, hop: float = 0.025,
               sample_rate: int = SAMPLE_RATE):
    """RMS for fixed-size windows in [start, end), with absolute window times."""
    import numpy as np

    if audio is None:
        return np.array([]), np.array([])
    first = max(0, int(start * sample_rate))
    last = min(len(audio), int(end * sample_rate))
    if last <= first:
        return np.array([]), np.array([])
    samples = np.square(audio[first:last], dtype=np.float64)
    width = max(1, int(window * sample_rate))
    if len(samples) <= width:
        value = float(np.sqrt(np.mean(samples)))
        return np.array([start]), np.array([value])
    cumulative = np.concatenate(([0.0], np.cumsum(samples)))
    step = max(1, int(hop * sample_rate))
    starts = np.arange(0, len(samples) - width + 1, step)
    times = start + starts / sample_rate
    energies = (cumulative[starts + width] - cumulative[starts]) / width
    return times, np.sqrt(np.maximum(energies, 0))


def max_window_rms(audio, start: float, end: float, window: float = 0.05,
                   sample_rate: int = SAMPLE_RATE) -> float:
    """Loudest 50 ms inside a word, so held notes are not judged as silence."""
    import numpy as np

    if audio is None:
        return 0.0
    first = max(0, int(start * sample_rate))
    last = min(len(audio), int(end * sample_rate))
    if last <= first:
        return 0.0
    samples = np.square(audio[first:last], dtype=np.float64)
    width = max(1, int(window * sample_rate))
    if len(samples) <= width:
        return float(np.sqrt(np.mean(samples)))
    cumulative = np.concatenate(([0.0], np.cumsum(samples)))
    starts = np.arange(0, len(samples) - width + 1, max(1, width // 2))
    energies = (cumulative[starts + width] - cumulative[starts]) / width
    return float(np.sqrt(np.max(energies)))


def vocal_onset(audio, start: float, end: float,
                threshold: float = ONSET_RMS, sample_rate: int = SAMPLE_RATE):
    """Return the first moment in [start, end) where singing is audible."""
    if audio is None or end <= start:
        return None
    times, values = window_rms(audio, start, end, sample_rate=sample_rate)
    for moment, value in zip(times, values):
        if value >= threshold:
            return float(moment)
    return None


def vocal_phrase_end(audio, start: float, limit: float,
                     threshold: float = ONSET_RMS, sample_rate: int = SAMPLE_RATE,
                     phrase_gap: float = PHRASE_GAP):
    """Return the last audible moment of the first sung phrase in [start, limit)."""
    if audio is None or limit <= start:
        return None
    times, values = window_rms(audio, start, limit, sample_rate=sample_rate)
    if not len(times):
        return None

    local_peak = float(values.max())
    if local_peak < threshold:
        return None
    gate = max(threshold, local_peak * 0.08)

    active = values >= gate
    first = int(active.argmax())
    if not active[first]:
        return None
    last_active = first
    for index in range(first + 1, len(times)):
        if active[index]:
            last_active = index
        elif times[index] - times[last_active] >= phrase_gap:
            break
    return min(limit, float(times[last_active]) + 0.05)


def energy_windows(audio, duration: float, window: float = 20.0,
                   hop: float = 5.0, sample_rate: int = SAMPLE_RATE):
    """Return (start, rms) for coarse windows, used to choose LID samples."""
    if audio is None or duration <= 0:
        return []
    step = max(1.0, hop)
    starts = []
    cursor = 0.0
    while cursor < duration:
        starts.append(cursor)
        cursor += step
    results = []
    for start in starts:
        end = min(duration, start + window)
        _, values = window_rms(audio, start, end, window=1.0, hop=1.0,
                               sample_rate=sample_rate)
        if len(values):
            results.append((start, float(values.mean())))
    return results
