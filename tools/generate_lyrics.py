"""Generate karaoke lyrics (SRT + word timings) from the original songs.

Pipeline per song:
  1. Separate the vocals with Demucs (GPU if available) so Whisper hears the
     singer instead of the band.
  2. Transcribe with faster-whisper large-v3 and word timestamps.
  3. Optionally fetch LRCLIB synced lyrics. The local audio supplies the timing;
     the online text supplies line breaks and fixes words Whisper misheard.
  4. Build one cue per lyric line, insert [Instrumental] cues for long gaps,
     and write:
       lyrics/<song stem>.srt
       lyrics/<song stem>.words.json

The SRT is ordinary, portable line-level subtitles. The JSON sidecar adds
per-word timings for the karaoke player's word-by-word highlighting.

Requirements (installed separately from the Flask app):
    pip install faster-whisper demucs torch

Run from the project root, for example:
    python tools/generate_lyrics.py --song "Two Steps Behind"
    python tools/generate_lyrics.py              # all songs
    python tools/generate_lyrics.py --no-online  # local transcript only
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SONGS_DIR = PROJECT_ROOT / "songs"
ORIGINALS_DIR = PROJECT_ROOT / "original_songs"
LYRICS_DIR = PROJECT_ROOT / "lyrics"
CACHE_DIR = PROJECT_ROOT / "tools" / ".cache"

AUDIO_EXTENSIONS = (".mp3", ".wav")
LRC_TIMESTAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
USER_AGENT = "karaoke-lyrics-sync/1.0 (personal local use)"
LRCLIB_TIMEOUT = 30
DURATION_MISMATCH_WARN = 1.0  # seconds between original and instrumental
INSTRUMENTAL_GAP = 3.0  # seconds; shorter gaps just leave the screen blank briefly
PHRASE_GAP = 2.5        # seconds of silence that ends a sung phrase
PHRASE_LINGER = 0.6     # keep a finished line on screen briefly before clearing it
# Whisper sometimes "hears" words at the ends of abruptly cut recordings.
# These are never lyrics.
HALLUCINATION_PHRASES = (
    "thank you for watching",
    "thanks for watching",
    "thank you watching",
    "please subscribe",
    "like and subscribe",
)

log = logging.getLogger("lyrics")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kwargs)


def norm_token(text: str) -> str:
    """Lowercase and strip punctuation so 'I'm', 'I? m' and 'Im' compare equal."""
    text = text.lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


def clean_text(text: str, capitalize: bool = True) -> str:
    """Fix the '?' apostrophe artifacts common in LRCLIB text and tidy spacing."""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"(?<=[A-Za-z])\?\s*(?=(?:m|s|t|ve|re|ll|d)\b)", "'", text)
    text = re.sub(r"(^|\s)\?\s*(?=[Cc]ause\b)", r"\1'", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text and capitalize:
        text = text[0].upper() + text[1:]
    return text


def srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def split_stem(stem: str) -> tuple[str, str]:
    """Split 'id_Title_Artist' into (title, artist), matching app.py's rules."""
    _, separator, rest = stem.partition("_")
    if not separator:
        return stem, ""
    title, separator, artist = rest.rpartition("_")
    if not separator:
        return rest, ""
    return title, artist


def find_song_files(song_filter: str | None) -> list[tuple[str, Path, Path]]:
    """Return (stem, original_path, karaoke_path) for matching songs."""
    found = []
    originals = {p.stem: p for p in ORIGINALS_DIR.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS}
    for stem, original in sorted(originals.items(), key=lambda item: item[0].casefold()):
        if song_filter and song_filter.casefold() not in stem.casefold():
            continue
        karaoke = SONGS_DIR / f"{stem}{original.suffix.lower()}"
        if not karaoke.exists():
            # Allow a different extension in songs/ (e.g. original wav, karaoke mp3).
            candidates = [p for p in SONGS_DIR.glob(f"{stem}.*") if p.suffix.lower() in AUDIO_EXTENSIONS]
            if not candidates:
                log.warning("%s: no matching file in %s, skipping", stem, SONGS_DIR)
                continue
            karaoke = candidates[0]
        found.append((stem, original, karaoke))
    return found


def duration_of(path: Path) -> float:
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Stage 1 + 2: vocals and transcription
# ---------------------------------------------------------------------------

def separate_vocals(stem: str, original: Path, cache_dir: Path, force: bool = False) -> Path:
    vocals = cache_dir / "stems" / "htdemucs" / stem / "vocals.wav"
    if vocals.exists() and not force:
        log.info("  vocals cached: %s", vocals)
        return vocals

    log.info("  separating vocals with Demucs...")
    device = "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
    except Exception:
        pass

    result = run([sys.executable, "-m", "demucs", "--two-stems=vocals",
                  "-n", "htdemucs", "-d", device,
                  "-o", str(cache_dir / "stems"), str(original)])
    if not vocals.exists():
        details = (result.stderr or result.stdout or "")[-3000:]
        raise RuntimeError(f"Demucs did not produce {vocals}\n{details}")
    return vocals


TRANSCRIBE_CHUNK = 55.0
TRANSCRIBE_OVERLAP = 8.0
TRANSCRIBE_STEP = TRANSCRIBE_CHUNK - TRANSCRIBE_OVERLAP
TRANSCRIBER_VERSION = "chunked-v2"


def chunk_ranges(duration: float) -> list[tuple[float, float, float, float]]:
    """Split audio into overlapping clips and return their non-overlapping cores.

    Each tuple is (clip_start, clip_end, core_start, core_end). Whisper timestamps
    drift badly on long music files, so every region is transcribed in a clip
    where it sits away from the clip edges; the cores then partition the song
    without duplicate or boundary-cut words.
    """
    clips: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(duration, start + TRANSCRIBE_CHUNK)
        clips.append((start, end))
        if end >= duration - 0.01:
            break
        start += TRANSCRIBE_STEP
    result = []
    for index, (clip_start, clip_end) in enumerate(clips):
        core_start = 0.0 if index == 0 else (clip_start + clips[index - 1][1]) / 2
        core_end = (duration if index == len(clips) - 1
                    else (clip_end + clips[index + 1][0]) / 2)
        if core_end <= core_start:
            core_start, core_end = clip_start, clip_end
        result.append((clip_start, clip_end, core_start, core_end))
    return result


# Handles must stay alive for the process lifetime; closing one removes the
# directory from the Windows DLL search path again.
_NVIDIA_DLL_HANDLES = []
_NVIDIA_DLL_DIRS: set[str] = set()


def configure_nvidia_dlls() -> None:
    """Expose pip-installed CUDA 12 DLLs to CTranslate2 on Windows."""
    if os.name != "nt":
        return
    try:
        site_roots = list(site.getsitepackages())
        site_roots.append(site.getusersitepackages())
    except (AttributeError, OSError):
        return
    for site_root in site_roots:
        for package in ("cublas", "cudnn"):
            bin_dir = Path(site_root) / "nvidia" / package / "bin"
            if not bin_dir.is_dir() or str(bin_dir) in _NVIDIA_DLL_DIRS:
                continue
            try:
                _NVIDIA_DLL_HANDLES.append(os.add_dll_directory(str(bin_dir)))
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            _NVIDIA_DLL_DIRS.add(str(bin_dir))


def cuda_whisper_available() -> bool:
    """Return True when CTranslate2 can see a CUDA device.

    Detection only proves the driver is visible; cuBLAS/cuDNN can still fail
    when the model is loaded or used. ``transcribe`` therefore falls back to
    CPU if an actual CUDA transcription fails.
    """
    configure_nvidia_dlls()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _transcribe_with_model(model, vocals: Path, prompt: str) -> tuple[list[dict], str, float, float]:
    """Run the chunked transcription with an already loaded Whisper model."""
    started = time.time()
    duration = duration_of(vocals)
    if duration <= 0:
        raise RuntimeError(f"could not read duration of {vocals}")

    rows = []
    language = "en"
    language_probability = 1.0
    with tempfile.TemporaryDirectory(prefix="karaoke-whisper-") as tmp:
        tmp_dir = Path(tmp)
        for clip_start, clip_end, core_start, core_end in chunk_ranges(duration):
            clip_path = tmp_dir / f"clip-{clip_start:07.2f}.wav"
            result = run([
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{clip_start:.3f}", "-t", f"{clip_end - clip_start:.3f}",
                "-i", str(vocals), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(clip_path),
            ])
            if not clip_path.exists():
                raise RuntimeError(
                    f"ffmpeg could not extract {clip_start:.1f}-{clip_end:.1f}s\n"
                    f"{(result.stderr or result.stdout or '')[-2000:]}"
                )
            segments, info = model.transcribe(
                str(clip_path),
                language="en",
                beam_size=5,
                temperature=0.0,
                word_timestamps=True,
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            language = info.language
            language_probability = info.language_probability

            chunk_words = []
            log_probs = []
            for segment in segments:
                log_probs.append(segment.avg_logprob)
                for word in segment.words or []:
                    text = (word.word or "").strip()
                    if not text:
                        continue
                    # Timestamps are relative to the extracted clip.
                    word_start = float(word.start) + clip_start
                    word_end = float(word.end) + clip_start
                    if word_end <= word_start:
                        word_end = word_start + 0.15
                    midpoint = (word_start + word_end) / 2
                    if midpoint < core_start or midpoint >= core_end:
                        continue
                    chunk_words.append({
                        "word": text,
                        "start": round(word_start, 3),
                        "end": round(word_end, 3),
                        "prob": round(word.probability, 4),
                    })
            if not chunk_words:
                log.info("    [%6.1f-%6.1f] no words", clip_start, clip_end)
                continue
            rows.append({
                "start": chunk_words[0]["start"],
                "end": chunk_words[-1]["end"],
                "text": " ".join(word["word"] for word in chunk_words),
                "avg_logprob": round(sum(log_probs) / len(log_probs), 4) if log_probs else -1.0,
                "no_speech_prob": 0.0,
                "words": chunk_words,
            })
            log.info("    [%6.1f-%6.1f] %d words: %s", clip_start, clip_end,
                     len(chunk_words),
                     " ".join(word["word"] for word in chunk_words)[:90])

    log.info("  transcribed in %.1fs (%d segments, %d words)", time.time() - started,
             len(rows), sum(len(row["words"]) for row in rows))
    return rows, language, language_probability, duration


def transcribe(stem: str, vocals: Path, prompt: str, cache_dir: Path,
               force: bool = False) -> dict:
    out = cache_dir / "transcripts" / f"{stem}.json"
    if out.exists() and not force:
        cached = json.loads(out.read_text(encoding="utf-8"))
        if cached.get("transcriber") == TRANSCRIBER_VERSION:
            log.info("  transcript cached: %s", out)
            return cached
        log.info("  transcript cache is outdated; re-running Whisper")

    configure_nvidia_dlls()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper demucs torch"
        ) from exc

    devices = []
    if cuda_whisper_available():
        devices.append(("cuda", "float16"))
    devices.append(("cpu", "int8"))

    data = None
    for device, compute_type in devices:
        log.info("  loading Whisper large-v3 (%s %s)...", device.upper(), compute_type)
        try:
            model = WhisperModel("large-v3", device=device, compute_type=compute_type,
                                 cpu_threads=0)
            log.info("  transcribing in %.0fs overlapping clips on %s...",
                     TRANSCRIBE_CHUNK, device.upper())
            rows, language, language_probability, duration = _transcribe_with_model(
                model, vocals, prompt)
        except Exception as exc:
            if device == "cuda":
                log.warning("  CUDA Whisper failed (%s); falling back to CPU int8", exc)
                continue
            raise
        data = {
            "transcriber": TRANSCRIBER_VERSION,
            "info": {"duration": duration, "language": language,
                     "language_probability": language_probability},
            "segments": rows,
        }
        break

    if data is None:
        raise RuntimeError("no Whisper device could transcribe the vocals")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Stage 3: online reference (LRCLIB)
# ---------------------------------------------------------------------------

def lrclib_search(artist: str, title: str, cache_dir: Path) -> list[dict]:
    """Fetch LRCLIB search results, cached by song title/artist."""
    key = re.sub(r"[^a-z0-9]+", "_", f"{artist}_{title}".lower()).strip("_")
    cache = cache_dir / "lrclib" / f"{key}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, list) and cached:
            return cached
        # Empty responses used to be cached forever, which blocked a later
        # retry from ever finding lyrics. Treat them as missing.
        log.info("  LRCLIB cache is empty; retrying online")

    query = urllib.parse.urlencode({"q": f"{artist} {title}",
                                    "track_name": title, "artist_name": artist})
    url = f"https://lrclib.net/api/search?{query}"
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=LRCLIB_TIMEOUT) as response:
                data = json.load(response)
            if not isinstance(data, list):
                raise ValueError("LRCLIB returned an unexpected payload")
            if data:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
            else:
                log.info("  LRCLIB returned no results (not cached; will retry next run)")
            return data
        except Exception as exc:
            last = exc
            log.warning("  LRCLIB attempt %d failed: %s", attempt, exc)
            time.sleep(2 * attempt)
    log.warning("  LRCLIB unavailable (%s); continuing with local transcript", last)
    return []


def parse_lrc(synced: str) -> list[dict]:
    lines = []
    for raw in (synced or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        stamps = LRC_TIMESTAMP.findall(raw)
        if not stamps:
            continue
        text = clean_text(LRC_TIMESTAMP.sub("", raw).strip())
        if not text:
            continue
        if re.fullmatch(r"\[[^\]]+\]", text) or re.fullmatch(r"\(instrumental\)", text, re.I):
            continue
        for minutes, seconds in stamps:
            lines.append({"start": int(minutes) * 60 + float(seconds), "text": text})
    lines.sort(key=lambda line: line["start"])
    return lines


def split_display_tokens(text: str) -> list[dict]:
    tokens = []
    for raw in text.split():
        norm = norm_token(raw)
        if not norm:
            if tokens:
                tokens[-1]["text"] += raw
            continue
        tokens.append({"text": raw, "norm": norm})
    return tokens


SILENCE_RMS = 0.0015  # ~-56 dBFS; below this a "word" is Whisper hallucinating silence
ONSET_RMS = 0.003      # ~-50 dBFS; first window loud enough to be singing
STRONG_ONSET_RMS = 0.02  # used when looking backwards; avoids nabbing bleed


def load_vocal_audio(vocals: Path):
    """Decode the vocal stem once to mono 16 kHz float samples for energy checks."""
    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(vocals), "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _window_rms(audio, sample_rate: int, start: float, end: float,
                window: float = 0.05, hop: float = 0.025):
    """RMS for fixed-size windows in [start, end), with absolute window times."""
    import numpy as np

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


def vocal_onset(audio, start: float, end: float,
                threshold: float = ONSET_RMS, sample_rate: int = 16000):
    """Return the first moment in [start, end) where singing is audible."""
    if audio is None or end <= start:
        return None
    times, values = _window_rms(audio, sample_rate, start, end)
    for time, value in zip(times, values):
        if value >= threshold:
            return float(time)
    return None


def vocal_phrase_end(audio, start: float, limit: float,
                     threshold: float = ONSET_RMS, sample_rate: int = 16000,
                     phrase_gap: float = PHRASE_GAP):
    """Return the last audible moment of the first sung phrase in [start, limit).

    A lyric line can be followed by a long instrumental tail; without this the
    cue (and its word timings) stretches all the way to the next line. Stop at
    the first real silence instead, ignoring short gaps between syllables.
    """
    if audio is None or limit <= start:
        return None
    times, values = _window_rms(audio, sample_rate, start, limit)
    if not len(times):
        return None

    # Scale the gate to the loudness of this phrase so quiet verses still work
    # while instrument bleed in a loud mix does not.
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


def _max_window_rms(audio, sample_rate: int, start: float, end: float,
                    window: float = 0.05) -> float:
    """Loudest 50 ms inside a word, so long held notes are not judged as silence."""
    import numpy as np

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


def filter_silent_words(transcript: dict, vocals: Path, audio=None) -> dict:
    """Drop words Whisper invented in silent parts of the vocal stem.

    Music prompts and song titles can make Whisper emit text over an intro or
    instrumental break. Those words always sit in near-silence, while real
    singing (even quiet backing vocals) has measurable energy.
    """
    if audio is None:
        audio = load_vocal_audio(vocals)
    if audio is None:
        log.warning("  could not analyse vocal energy; keeping all transcript words")
        return transcript
    sample_rate = 16000

    dropped = 0
    segments = []
    for segment in transcript.get("segments", []):
        kept = []
        for word in segment.get("words", []):
            rms = _max_window_rms(audio, sample_rate,
                                  float(word.get("start", 0.0)),
                                  float(word.get("end", 0.0)))
            if rms < SILENCE_RMS:
                dropped += 1
                continue
            kept.append(word)
        if not kept:
            continue
        segment["words"] = kept
        segment["start"] = kept[0]["start"]
        segment["end"] = kept[-1]["end"]
        segment["text"] = " ".join(word.get("word", "") for word in kept).strip()
        segments.append(segment)
    transcript["segments"] = segments
    if dropped:
        log.info("  removed %d hallucinated word(s) from silence", dropped)
    return transcript


def strip_hallucinations(transcript: dict) -> dict:
    """Remove boilerplate Whisper invents at the end of video-sourced audio."""
    removed = 0
    segments = []
    for segment in transcript.get("segments", []):
        words = list(segment.get("words", []))
        norms = [norm_token(word.get("word", "")) for word in words]
        for phrase in HALLUCINATION_PHRASES:
            parts = [norm_token(part) for part in phrase.split()]
            if len(norms) < len(parts) or norms[-len(parts):] != parts:
                continue
            removed += len(parts)
            words = words[:-len(parts)]
            norms = norms[:-len(parts)]
            break
        if not words:
            continue
        segment["words"] = words
        segment["start"] = words[0]["start"]
        segment["end"] = words[-1]["end"]
        segment["text"] = " ".join(word.get("word", "") for word in words).strip()
        segments.append(segment)
    transcript["segments"] = segments
    if removed:
        log.info("  removed %d word(s) from a Whisper boilerplate phrase", removed)
    return transcript


def local_words(transcript: dict) -> list[dict]:
    words = []
    for segment in transcript.get("segments", []):
        for word in segment.get("words", []):
            text = (word.get("word") or "").strip()
            norm = norm_token(text)
            if not norm:
                continue
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start + 0.15))
            if end <= start:
                end = start + 0.15
            # Chunk boundaries and repeated choruses can make Whisper emit the
            # same word twice at the exact same time. One copy is enough.
            if words and abs(start - words[-1]["start"]) < 0.001 \
                    and abs(end - words[-1]["end"]) < 0.001:
                continue
            words.append({"text": text, "norm": norm, "start": start, "end": end})
    return words


def flat_lrc_tokens(lines: list[dict]) -> list[dict]:
    tokens = []
    for line_index, line in enumerate(lines):
        for token_index, token in enumerate(split_display_tokens(line["text"])):
            tokens.append({
                "text": token["text"],
                "norm": token["norm"],
                "line": line_index,
                "token": token_index,
                "line_start": line["start"],
            })
    return tokens


VERSION_PENALTIES = {
    "karaoke": 6.0,
    "instrumental": 6.0,
    "cover": 3.0,
    "acoustic": 1.2,
    "live": 1.2,
    "remix": 1.2,
    "demo": 1.2,
}


def version_penalty(candidate: dict, duration: float) -> float:
    """Penalise alternate versions so the studio recording wins when possible."""
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("trackName", "albumName", "artistName")
    ).lower()
    penalty = sum(weight for word, weight in VERSION_PENALTIES.items()
                  if re.search(rf"\b{re.escape(word)}\b", haystack))
    candidate_duration = candidate.get("duration")
    if candidate_duration and duration > 0:
        penalty += min(2.0, abs(float(candidate_duration) - duration) / 10.0)
    return penalty


def choose_reference(local: list[dict], candidates: list[dict],
                     duration: float = 0.0) -> dict | None:
    """Pick the LRCLIB version whose words best match the local transcript."""
    best = None
    for candidate in candidates:
        lines = parse_lrc(candidate.get("syncedLyrics") or "")
        if not lines:
            continue
        tokens = flat_lrc_tokens(lines)
        if not tokens:
            continue
        matcher = difflib.SequenceMatcher(
            None, [word["norm"] for word in local], [token["norm"] for token in tokens],
            autojunk=False)
        blocks = [block for block in matcher.get_matching_blocks() if block.size]
        matched = sum(block.size for block in blocks)
        if not matched:
            continue
        token_local = {}
        for block in blocks:
            for offset in range(block.size):
                token_local[block.b + offset] = block.a + offset

        anchors, anchor_stats = robust_line_anchors(lines, tokens, token_local, local)
        if not anchors:
            continue
        offset = anchor_stats["median_offset"]
        coverage = matched / len(tokens)
        local_coverage = matched / len(local) if local else 0.0
        penalty = version_penalty(candidate, duration)
        score = 8 * coverage + 4 * local_coverage - 0.05 * abs(offset) - penalty
        record = {
            "score": score,
            "coverage": coverage,
            "local_coverage": local_coverage,
            "offset": offset,
            "anchor_mad": anchor_stats["mad"],
            "anchor_tolerance": anchor_stats["tolerance"],
            "penalty": penalty,
            "lines": lines,
            "tokens": tokens,
            "token_local": token_local,
            "anchors": anchors,
            "candidate": candidate,
        }
        if best is None or record["score"] > best["score"]:
            best = record
    return best


def robust_line_anchors(lines: list[dict], tokens: list[dict],
                        token_local: dict[int, int], local: list[dict]) -> tuple[dict[int, float], dict]:
    """Keep only line anchors that agree with the song's overall offset.

    LRCLIB text often repeats choruses, and Whisper can match a later repetition
    to an earlier occurrence in the reference. Those mismatches produce anchor
    offsets tens of seconds away from the rest; a median/MAD filter removes them.
    """
    starts_by_line: dict[int, list[float]] = {}
    first_token: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        first_token.setdefault(token["line"], token_index)
    for token_index, local_index in token_local.items():
        line_index = tokens[token_index]["line"]
        # Only the line's first word is a reliable line-start anchor. If
        # Whisper missed the opening word (common with held notes or backing
        # vocals), the next word can be seconds later.
        if token_index != first_token[line_index]:
            continue
        starts_by_line.setdefault(line_index, []).append(local[local_index]["start"])
    if not starts_by_line:
        return {}, {}

    offsets = sorted(min(starts) - lines[line_index]["start"]
                     for line_index, starts in starts_by_line.items())
    median = offsets[len(offsets) // 2]
    mad = sorted(abs(offset - median) for offset in offsets)[len(offsets) // 2]
    tolerance = max(1.25, min(3.0, 5.0 * mad))

    anchors = {}
    for line_index, starts in starts_by_line.items():
        start = min(starts)
        if abs(start - lines[line_index]["start"] - median) <= tolerance:
            anchors[line_index] = start

    # Anchors must move forward with the reference timestamps.
    monotonic = {}
    last_start = None
    for line_index in sorted(anchors):
        if last_start is not None and anchors[line_index] <= last_start:
            continue
        monotonic[line_index] = anchors[line_index]
        last_start = anchors[line_index]

    return monotonic, {"median_offset": median, "mad": mad, "tolerance": tolerance}


def mapped_line_windows(lines: list[dict], anchors: dict[int, float],
                        duration: float, offset: float = 0.0) -> list[tuple[float, float]]:
    """Map every LRC line start to local time and return (start, end) windows."""
    starts = [max(0.0, map_lrc_time(anchors, lines, line["start"], offset))
              for line in lines]
    windows = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else duration
        if end <= start:
            end = start + 0.5
        windows.append((start, end))
    return windows


def align_reference_words(reference: dict, local: list[dict], duration: float,
                          iterations: int = 2) -> None:
    """Re-align reference tokens using local time windows instead of global order.

    Repeated chorus lines are common, so a single global diff can tie a local
    chorus to the wrong repetition. After a robust first mapping, each line is
    matched only against words near that line's mapped time; iterating tightens
    the anchors for lines whose word timings were initially unreliable.
    """
    lines = reference["lines"]
    tokens = reference["tokens"]
    anchors = reference["anchors"]
    tokens_by_line: dict[int, list[int]] = {}
    for token_index, token in enumerate(tokens):
        tokens_by_line.setdefault(token["line"], []).append(token_index)

    token_local: dict[int, int] = {}
    for _ in range(max(1, iterations)):
        offset = reference.get("offset", 0.0)
        windows = mapped_line_windows(lines, anchors, duration, offset)
        new_token_local: dict[int, int] = {}
        # Keep anchors that were already trusted; tighten them where this
        # iteration found a stronger text match.
        new_anchors: dict[int, float] = dict(anchors)
        # Each local word belongs to at most one reference line. Without this,
        # a duplicated LRC line can steal the previous line's words and produce
        # a squeezed duplicate cue.
        claimed: set[int] = set()
        for line_index, token_indices in tokens_by_line.items():
            start, end = windows[line_index]
            candidates = [
                (local_index, word)
                for local_index, word in enumerate(local)
                if local_index not in claimed
                and start - 1.5 <= word["start"] <= end + 1.5
            ]
            if not candidates:
                continue
            matcher = difflib.SequenceMatcher(
                None,
                [tokens[index]["norm"] for index in token_indices],
                [word["norm"] for _, word in candidates],
                autojunk=False,
            )
            matched: dict[int, int] = {}
            for block in matcher.get_matching_blocks():
                for token_offset in range(block.size):
                    matched[block.a + token_offset] = candidates[block.b + token_offset][0]
            if not matched:
                continue
            # A misheard final word (love -> life) breaks the exact diff and
            # would otherwise drop the last word of the cue. Recover a short
            # trailing run when the unused local word sounds close enough and
            # sits immediately after the last match.
            last_offset = max(matched)
            if last_offset < len(token_indices) - 1:
                used = set(matched.values())
                last_end = local[matched[last_offset]]["end"]
                spare = [
                    (local_index, word)
                    for local_index, word in candidates
                    if local_index not in used
                    and word["start"] >= last_end - 0.05
                    and word["start"] <= last_end + 1.2
                ]
                for token_offset, (local_index, word) in zip(
                        range(last_offset + 1, len(token_indices)), spare):
                    if tokens_similar(tokens[token_indices[token_offset]]["norm"],
                                      word["norm"]):
                        matched[token_offset] = local_index
                        last_end = word["end"]
                    else:
                        break
            ratio = len(matched) / len(token_indices)
            longest = max((block.size for block in matcher.get_matching_blocks()),
                          default=0)
            # A line whose second half was lost by Whisper can still have a
            # trustworthy run of two or more consecutive words in the middle.
            # Keep those; a couple of scattered matches are not enough.
            if ratio < 0.34 and len(token_indices) > 3 and longest < 2:
                continue
            for token_offset, local_index in matched.items():
                new_token_local[token_indices[token_offset]] = local_index
                claimed.add(local_index)

            # Use the first matched word as the line anchor when enough of
            # the line matched. A reference timestamp can be several seconds
            # off (an extra repeated chorus or a displaced line); the audio
            # match is more trustworthy than the LRC clock.
            first_offset = min(matched)
            first_start = local[matched[first_offset]]["start"]
            if first_offset <= 1 and ratio >= 0.5:
                # If the line's first word was dropped as silent, step back a
                # little so it still gets a slice at the start of the cue.
                new_anchors[line_index] = max(0.0, first_start - 0.35 * first_offset)
            elif ratio >= 0.75:
                new_anchors[line_index] = max(0.0, first_start - 0.35 * min(first_offset, 2))

        if not new_anchors:
            break
        monotonic = {}
        last_start = None
        for line_index in sorted(new_anchors):
            if last_start is not None and new_anchors[line_index] <= last_start:
                continue
            monotonic[line_index] = new_anchors[line_index]
            last_start = new_anchors[line_index]
        anchors = monotonic
        token_local = new_token_local

    reference["anchors"] = anchors
    reference["token_local"] = token_local
    offsets = sorted(anchors[index] - lines[index]["start"] for index in anchors)
    if offsets:
        reference["offset"] = offsets[len(offsets) // 2]


def drop_duplicate_unmatched_lines(reference: dict) -> None:
    """Remove a repeated LRC line that has no audio match of its own.

    LRCLIB sometimes lists the same line twice while Whisper merges both
    repetitions into one phrase. Keeping the second line creates a squeezed
    duplicate cue that highlights the first line's words again.
    """
    lines = reference["lines"]
    tokens = reference["tokens"]
    token_local = reference.get("token_local", {})
    tokens_by_line: dict[int, list[int]] = {index: [] for index in range(len(lines))}
    for token_index, token in enumerate(tokens):
        tokens_by_line[token["line"]].append(token_index)

    def line_norm(text: str) -> str:
        return " ".join(token["norm"] for token in split_display_tokens(text))

    skip: set[int] = set()
    for line_index in range(1, len(lines)):
        if line_norm(lines[line_index]["text"]) != line_norm(lines[line_index - 1]["text"]):
            continue
        if any(index in token_local for index in tokens_by_line[line_index]):
            continue
        skip.add(line_index)
    if not skip:
        return

    line_map: dict[int, int] = {}
    new_lines = []
    for line_index, line in enumerate(lines):
        if line_index in skip:
            continue
        line_map[line_index] = len(new_lines)
        new_lines.append(line)

    token_map: dict[int, int] = {}
    new_tokens = []
    for token_index, token in enumerate(tokens):
        if token["line"] in skip:
            continue
        token_map[token_index] = len(new_tokens)
        token = dict(token)
        token["line"] = line_map[token["line"]]
        new_tokens.append(token)

    reference["lines"] = new_lines
    reference["tokens"] = new_tokens
    reference["anchors"] = {
        line_map[line_index]: value
        for line_index, value in reference.get("anchors", {}).items()
        if line_index in line_map
    }
    reference["token_local"] = {
        token_map[token_index]: local_index
        for token_index, local_index in token_local.items()
        if token_index in token_map
    }


# ---------------------------------------------------------------------------
# Stage 4: build cues and word timings
# ---------------------------------------------------------------------------

def tokens_similar(left: str, right: str) -> bool:
    """True when two short words are one typo apart (e.g. love/life).

    Whisper occasionally mishears a word while keeping its timing. Only permit
    this for words long enough that a single-character difference is unlikely
    to turn one real word into another unrelated one.
    """
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def map_lrc_time(anchors: dict[int, float], lines: list[dict], value: float,
                 default_offset: float = 0.0) -> float:
    """Map an LRC timestamp to local audio time using matched line anchors."""
    pairs = sorted((lines[index]["start"], local_start)
                   for index, local_start in anchors.items())
    if not pairs:
        return value + default_offset
    if value == pairs[0][0]:
        return pairs[0][1]
    if value == pairs[-1][0]:
        return pairs[-1][1]
    if value < pairs[0][0] or value > pairs[-1][0]:
        # Outside the anchored range, a single global offset is safer than
        # extending the slope of the nearest pair.
        return value + default_offset
    for (lrc0, local0), (lrc1, local1) in zip(pairs, pairs[1:]):
        if lrc0 <= value <= lrc1:
            if lrc1 == lrc0:
                return local0
            return local0 + (local1 - local0) * (value - lrc0) / (lrc1 - lrc0)
    return value + default_offset


def distribute_words(tokens: list[dict], start: float, end: float) -> list[dict]:
    """Give every token a slice of [start, end], weighted by word length."""
    if not tokens:
        return []
    if end <= start:
        end = start + 0.5
    weights = [max(1, len(token["norm"])) for token in tokens]
    total = sum(weights)
    cursor = start
    words = []
    for index, (token, weight) in enumerate(zip(tokens, weights)):
        share = (end - start) * weight / total
        word_start = cursor
        word_end = end if index == len(tokens) - 1 else cursor + share
        if word_end <= word_start:
            word_end = word_start + 0.12
        words.append({"text": token["text"],
                      "start": round(word_start, 3),
                      "end": round(word_end, 3)})
        cursor = word_end
    return words


def interpolate_known(tokens: list[dict], known: dict[int, tuple[float, float]],
                      start: float, end: float) -> list[dict]:
    """Use real word timings where available, fill the gaps smoothly."""
    words = [None] * len(tokens)
    for index, times in known.items():
        if not 0 <= index < len(tokens):
            continue
        word_start, word_end = times
        # A word timed after the line has already ended belongs to a later
        # line (often the next lyric line matched by mistake). Let the gap
        # filler distribute it instead of clipping it to zero length.
        if word_start >= end - 0.05 or word_end <= start + 0.05:
            continue
        words[index] = {"text": tokens[index]["text"],
                        "start": max(start, word_start),
                        "end": min(end, max(word_start + 0.12, word_end))}

    # Fill each run of unknown tokens between two known neighbours.
    index = 0
    while index < len(tokens):
        if words[index] is not None:
            index += 1
            continue
        run_start = index
        while index < len(tokens) and words[index] is None:
            index += 1
        run_end = index
        gap_start = words[run_start - 1]["end"] if run_start > 0 else start
        gap_end = words[run_end]["start"] if run_end < len(tokens) else end
        run = tokens[run_start:run_end]
        if gap_end <= gap_start:
            gap_end = gap_start + 0.12 * len(run)
        filled = distribute_words(run, gap_start, gap_end)
        words[run_start:run_end] = filled

    # Clamp, enforce order, and keep every word inside the line.
    previous_end = start
    result = []
    for word in words:
        word_start = max(start, previous_end, word["start"])
        word_end = min(end, max(word_start + 0.12, word["end"]))
        result.append({"text": word["text"],
                       "start": round(word_start, 3),
                       "end": round(word_end, 3)})
        previous_end = word_end
    return result


def local_timing_is_reasonable(tokens: list[dict], known: dict[int, tuple[float, float]],
                               line_start: float, line_end: float) -> bool:
    if not known:
        return False
    coverage = len(known) / len(tokens)
    if coverage < 0.55:
        # Whisper can drop the start or end of a line but still time a solid
        # run in the middle. Trust that run instead of re-distributing every
        # word evenly across a cue that may be several seconds long.
        offsets = sorted(known)
        consecutive = offsets == list(range(offsets[0], offsets[-1] + 1))
        if coverage < 0.25 or len(offsets) < 2 or not consecutive:
            return False
    ordered = sorted(known.items())
    first_start = ordered[0][1][0]
    last_end = ordered[-1][1][1]
    if first_start < line_start - 1.5 or first_start > line_start + 2.0:
        return False
    if last_end < line_start + 0.25 * (line_end - line_start):
        return False
    previous_end = None
    for _, (word_start, word_end) in ordered:
        if word_start < line_start - 1.5 or word_end > line_end + 0.75:
            return False
        if word_end - word_start > 5.0:
            return False
        if previous_end is not None and word_start - previous_end > 6.0:
            return False
        previous_end = word_end
    return True


def build_lyric_lines(reference: dict, duration: float, audio=None) -> list[dict]:
    lines = reference["lines"]
    tokens = reference["tokens"]
    token_local = reference["token_local"]
    anchors = reference["anchors"]

    tokens_by_line: dict[int, list[int]] = {index: [] for index in range(len(lines))}
    for token_index, token in enumerate(tokens):
        tokens_by_line[token["line"]].append(token_index)

    # First pass: decide when each line starts. A known first word is more
    # accurate than the LRC timestamp, but if it disagrees wildly the reference
    # wins. Starts are made monotonic so cues can never overlap.
    line_info = []
    previous_start = -1.0
    previous_word_end = 0.0
    for line_index, line in enumerate(lines):
        line_tokens = tokens_by_line[line_index]
        mapped_start = map_lrc_time(anchors, lines, line["start"],
                                    reference.get("offset", 0.0))
        next_lrc = lines[line_index + 1]["start"] if line_index + 1 < len(lines) else duration
        mapped_next = map_lrc_time(anchors, lines, next_lrc,
                                   reference.get("offset", 0.0))
        if mapped_next <= mapped_start:
            mapped_next = mapped_start + 1.0

        known = {}
        for token_index in line_tokens:
            if token_index in token_local:
                local = reference["local"][token_local[token_index]]
                known[token_index - line_tokens[0]] = (local["start"], local["end"])

        if known and 0 in known and abs(known[0][0] - mapped_start) <= 2.0:
            line_start = known[0][0]
        else:
            line_start = mapped_start

        # If no word matched this line, the LRC timestamp is the only hint and
        # may be early (or belong to a different edition). The first audible
        # vocal in the mapped window is a safer start.
        if audio is not None:
            onset = vocal_onset(audio, line_start + 0.15,
                                min(mapped_next, line_start + 4.0))
            if onset is not None:
                if not known and onset > line_start + 0.5:
                    line_start = onset
                elif known and 0 in known:
                    first_known = known[0][0]
                    if first_known > line_start + 1.2 and onset < first_known - 0.05:
                        line_start = onset

        # A reference timestamp can point at the wrong repeat of a chorus.
        # When this line is far from the previous one, look just before the
        # mapped start for a strong vocal onset. Requiring silence at the
        # search boundary keeps us from stealing the previous phrase's tail.
        if audio is not None and previous_start >= 0 and line_start - previous_start > 6.0:
            back_start = max(previous_start + 0.2, line_start - 2.0)
            back_onset = vocal_onset(audio, back_start, line_start + 0.1,
                                     threshold=STRONG_ONSET_RMS)
            if back_onset is not None and back_start + 0.2 <= back_onset < line_start - 0.2:
                line_start = back_onset

        # The reference sometimes starts the next line while the previous
        # line's last word is still being sung. Do not cut that word in half;
        # delay the next cue slightly instead.
        if previous_word_end and line_start < previous_word_end + 0.05 \
                and previous_word_end - line_start < 2.0:
            line_start = previous_word_end + 0.05

        if line_start <= previous_start:
            line_start = previous_start + 0.12
        previous_start = line_start
        line_info.append((line_tokens, mapped_start, mapped_next, known, line_start))

        # Reserve enough room after the last known word for any trailing
        # tokens that Whisper missed. Otherwise a delayed next cue can clip
        # the final word even though the reference text includes it.
        valid = {offset: times for offset, times in known.items()
                 if 0.0 <= times[1] - times[0] <= 5.0}
        if valid:
            last_offset = max(valid)
            trailing = max(0, len(line_tokens) - 1 - last_offset)
            previous_word_end = valid[last_offset][1] + 0.12 * trailing
        else:
            previous_word_end = 0.0

    built = []
    for line_index, (line_tokens, mapped_start, mapped_next, known, line_start) in enumerate(line_info):
        if not line_tokens:
            continue

        # Keep a single line on screen for a sane amount of time. A long
        # "Oh-oh" cue over a 30-second instrumental break is not useful, but
        # a normal six-word line must not be cut off just because its words
        # are held for a while.
        token_count = len(line_tokens)
        if token_count <= 2:
            max_duration = 8.0
        elif token_count <= 4:
            max_duration = 10.0
        else:
            max_duration = 14.0
        # Empty LRC lines are separators, not cues. Keep looking for the next
        # line that actually has words so the current line can use its full
        # vocal phrase instead of stopping at an arbitrary separator.
        next_start = duration
        for later_tokens, _, _, _, later_start in line_info[line_index + 1:]:
            if later_tokens:
                next_start = later_start
                break
        line_end = min(next_start - 0.05, line_start + max_duration)
        if line_end <= line_start:
            line_end = line_start + 0.8

        valid = {offset: times for offset, times in known.items()
                 if 0.0 <= times[1] - times[0] <= 5.0}
        required_end = 0.0
        trailing = 0
        if valid:
            last_offset = max(valid)
            trailing = max(0, token_count - 1 - last_offset)
            required_end = valid[last_offset][1] + 0.12 * trailing

        # Do not stretch a line across the instrumental tail that follows it.
        # End the cue shortly after the singing stops; add_instrumental_cues()
        # will label the remaining gap.
        if audio is not None:
            phrase_end = vocal_phrase_end(audio, line_start, line_end)
            if phrase_end is not None:
                line_end = min(line_end, max(line_start + 0.6,
                                             phrase_end + PHRASE_LINGER))

        # The 2.5s phrase gap can bridge a held note and a following backing
        # vocal or ad-lib that Whisper never transcribed. For a fully timed
        # line, split the tail at a shorter silence so the cue ends with its
        # own phrase. Starting after the final word keeps held notes intact.
        if valid and trailing == 0 and len(valid) / token_count >= 0.6 and audio is not None:
            last_end = valid[max(valid)][1]
            if line_end - last_end > PHRASE_LINGER:
                tail_end = vocal_phrase_end(audio, last_end + 0.05, line_end,
                                            phrase_gap=0.8)
                if tail_end is not None:
                    line_end = max(required_end,
                                   min(line_end, tail_end + PHRASE_LINGER))

        # A fully matched line should not keep one word on screen for many
        # seconds while a separate vocal phrase plays. Only trim the tail when
        # another local word actually starts in the gap; a long held note has
        # no new word after it and must keep its full phrase.
        if valid and trailing == 0 and len(valid) / token_count >= 0.75:
            last_end = valid[last_offset][1]
            gap_cutoff = next_start - 1.0 if next_start < duration else duration
            new_phrase = any(last_end + 0.05 <= word["start"] < gap_cutoff
                             for word in reference.get("local", []))
            if new_phrase:
                line_end = min(line_end, last_end + PHRASE_LINGER)

        # Whisper can drop the tail of a line (especially a quiet final word).
        # If only the middle of a line matched, do not let the unmatched tail
        # stretch for many seconds over music that follows.
        if valid and trailing > 0:
            last_end = valid[last_offset][1]
            if line_end - last_end > 3.0:
                line_end = min(line_end,
                               last_end + max(1.2, 0.55 * trailing) + PHRASE_LINGER)
        line_end = max(line_end, required_end)

        line_tokens_list = [tokens[index] for index in line_tokens]
        if local_timing_is_reasonable(line_tokens_list, known, line_start, line_end):
            words = interpolate_known(line_tokens_list, known, line_start, line_end)
        else:
            words = distribute_words(line_tokens_list, line_start, line_end)

        # Remove zero-length/overlapping edge cases introduced by clamping.
        words = [word for word in words if word["text"] and word["end"] > word["start"]]
        if not words:
            continue
        words[0]["start"] = round(max(0.0, line_start), 3)
        words[-1]["end"] = round(min(line_end, max(words[-1]["start"] + 0.12, words[-1]["end"])), 3)

        built.append({
            "start": words[0]["start"],
            "end": line_end,
            "text": lines[line_index]["text"],
            "words": words,
            "instrumental": False,
        })
    return built


def add_instrumental_cues(lines: list[dict], duration: float, audio=None) -> list[dict]:
    cues = [dict(line) for line in lines]
    cues.sort(key=lambda cue: cue["start"])

    def make_gap(gap_start: float, gap_end: float):
        if gap_end - gap_start <= 0.8:
            return None
        if audio is not None and gap_end - gap_start < 4.5:
            _, values = _window_rms(audio, 16000, gap_start, gap_end)
            if len(values) and float(values.max()) >= 0.12:
                # A held note or ad-lib, not a real instrument-only break.
                return None
        return {
            "start": round(gap_start, 3),
            "end": round(gap_end, 3),
            "text": "[Instrumental]",
            "words": [],
            "instrumental": True,
        }

    with_gaps = []
    previous_end = 0.0
    for cue in cues:
        if cue["start"] - previous_end > INSTRUMENTAL_GAP:
            gap = make_gap(previous_end + (0.2 if with_gaps else 0.4),
                           cue["start"] - 0.2)
            if gap:
                with_gaps.append(gap)
        with_gaps.append(cue)
        previous_end = max(previous_end, cue["end"])

    if duration - previous_end > INSTRUMENTAL_GAP:
        gap = make_gap(previous_end + 0.2, duration - 0.8)
        if gap:
            with_gaps.append(gap)

    # Make the timeline strictly ordered and non-overlapping. When a lyric
    # line is pushed later because the previous line is still on screen, its
    # word timings have to move too. Re-time them sequentially rather than
    # pinning every stale word to the new cue start (which would make them
    # overlap each other).
    ordered = []
    previous_end = -1.0
    for cue in sorted(with_gaps, key=lambda item: item["start"]):
        if cue["start"] <= previous_end:
            cue["start"] = previous_end + 0.01
        if cue["end"] <= cue["start"]:
            cue["end"] = cue["start"] + 0.5

        words = cue.get("words", [])
        cursor = cue["start"]
        for index, word in enumerate(words):
            remaining = len(words) - index - 1
            latest_start = max(cursor, cue["end"] - 0.12 * (remaining + 1))
            word_start = max(cursor, min(word["start"], latest_start))
            word_end = min(cue["end"], max(word_start + 0.12, word["end"]))
            if word_end < word_start + 0.12:
                # Not enough room for this word; extend the cue so the
                # validator and the player still get a visible highlight.
                cue["end"] = word_start + 0.12 * (remaining + 1)
                word_end = word_start + 0.12
            word["start"] = round(word_start, 3)
            word["end"] = round(word_end, 3)
            cursor = word_end

        ordered.append(cue)
        previous_end = cue["end"]
    return ordered


# ---------------------------------------------------------------------------
# Offline fallback: split local transcript segments into readable lines
# ---------------------------------------------------------------------------

def build_from_transcript(transcript: dict, duration: float) -> list[dict]:
    lines = []
    for segment in transcript.get("segments", []):
        words = []
        for word in segment.get("words", []):
            text = (word.get("word") or "").strip()
            if not norm_token(text):
                continue
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start + 0.15))
            words.append({"text": text, "start": start, "end": max(start + 0.12, end)})
        if not words:
            continue

        chunk = []
        chunk_start = words[0]["start"]
        for index, word in enumerate(words):
            chunk.append(word)
            is_last = index == len(words) - 1
            next_word = None if is_last else words[index + 1]
            pause = 0.0 if next_word is None else next_word["start"] - word["end"]
            ended_sentence = bool(re.search(r"[.!?][\"')\]]?$", word["text"]))
            long_chunk = word["end"] - chunk_start >= 7.0 or len(chunk) >= 10
            if is_last or ended_sentence or long_chunk or pause >= 0.8:
                for item in chunk:
                    item["text"] = clean_text(item["text"], capitalize=False)
                if chunk and chunk[0]["text"]:
                    chunk[0]["text"] = chunk[0]["text"][0].upper() + chunk[0]["text"][1:]
                text = " ".join(item["text"] for item in chunk)
                text = clean_text(text)
                end = min(duration, chunk[-1]["end"] + 0.35)
                lines.append({"start": round(chunk_start, 3), "end": round(end, 3),
                              "text": text, "words": chunk, "instrumental": False})
                chunk = []
                if next_word is not None:
                    chunk_start = next_word["start"]
    return lines


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_outputs(stem: str, cues: list[dict], duration: float) -> tuple[Path, Path]:
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    srt_path = LYRICS_DIR / f"{stem}.srt"
    json_path = LYRICS_DIR / f"{stem}.words.json"

    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(f"{index}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}\n")
    srt_path.write_text("\n".join(blocks), encoding="utf-8")

    payload = {
        "version": 1,
        "song": stem,
        "duration": round(duration, 3),
        "generated_by": "Demucs + faster-whisper large-v3; LRCLIB reference for text correction",
        "lines": [
            {
                "start": round(cue["start"], 3),
                "end": round(cue["end"], 3),
                "text": cue["text"],
                "instrumental": bool(cue.get("instrumental")),
                "words": [
                    {"text": word["text"],
                     "start": round(word["start"], 3),
                     "end": round(word["end"], 3)}
                    for word in cue.get("words", [])
                ],
            }
            for cue in cues
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return srt_path, json_path


# ---------------------------------------------------------------------------
# Per-song orchestration
# ---------------------------------------------------------------------------

def process_song(stem: str, original: Path, karaoke: Path, cache_dir: Path,
                 use_online: bool, force: bool) -> None:
    log.info("")
    log.info("=== %s ===", stem)
    duration = duration_of(karaoke)
    if duration <= 0:
        raise RuntimeError(f"could not read duration of {karaoke}")
    log.info("  duration: %.1fs", duration)

    original_duration = duration_of(original)
    if original_duration <= 0:
        log.warning("  could not read duration of %s", original.name)
    elif abs(original_duration - duration) > DURATION_MISMATCH_WARN:
        log.warning(
            "  duration mismatch: original %.1fs vs instrumental %.1fs; "
            "check that the instrumental was made from this original",
            original_duration, duration,
        )

    title, artist = split_stem(stem)
    prompt = f"{artist} - {title}" if artist else title

    vocals = separate_vocals(stem, original, cache_dir, force=force)
    transcript = transcribe(stem, vocals, prompt, cache_dir, force=force)
    audio = load_vocal_audio(vocals)
    transcript = strip_hallucinations(transcript)
    transcript = filter_silent_words(transcript, vocals, audio)
    local = local_words(transcript)

    reference = None
    if use_online and local:
        candidates = lrclib_search(artist, title, cache_dir)
        if candidates:
            reference = choose_reference(local, candidates, duration)
            if reference:
                reference["local"] = local
                align_reference_words(reference, local, duration)
                drop_duplicate_unmatched_lines(reference)
                log.info("  reference: score=%.2f coverage=%.0f%% local=%.0f%% offset=%+.2fs anchors=%d penalty=%.2f (%s - %s)",
                         reference["score"], reference["coverage"] * 100,
                         reference["local_coverage"] * 100, reference["offset"],
                         len(reference["anchors"]), reference["penalty"],
                         reference["candidate"].get("artistName"),
                         reference["candidate"].get("trackName"))

    if reference and reference["coverage"] >= 0.25:
        cues = build_lyric_lines(reference, duration, audio)
        source = "reference"
    else:
        log.warning("  no usable online reference; using local transcript text")
        cues = build_from_transcript(transcript, duration)
        source = "local"

    if not cues:
        raise RuntimeError("no lyric lines were produced")

    cues = add_instrumental_cues(cues, duration, audio)
    srt_path, json_path = write_outputs(stem, cues, duration)
    lyric_count = sum(1 for cue in cues if not cue.get("instrumental"))
    instrumental_count = len(cues) - lyric_count
    log.info("  wrote %s (%d lyric lines, %d instrumental cues, source=%s)",
             srt_path.name, lyric_count, instrumental_count, source)
    log.info("  wrote %s", json_path.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--song", help="only process stems containing this text")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR,
                        help="where vocals/transcripts/LRCLIB responses are cached")
    parser.add_argument("--no-online", action="store_true",
                        help="do not use LRCLIB; use the local Whisper transcript only")
    parser.add_argument("--force", action="store_true",
                        help="re-run Demucs and Whisper even when cached")
    parser.add_argument("--missing", action="store_true",
                        help="only process songs that do not have an .srt yet")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        log.error("ffmpeg and ffprobe must be on PATH")
        return 1

    songs = find_song_files(args.song)
    if not songs:
        log.error("no matching songs found in %s", ORIGINALS_DIR)
        return 1
    if args.missing:
        songs = [
            song for song in songs
            if not (LYRICS_DIR / f"{song[0]}.srt").exists()
        ]
    if not songs:
        log.info("nothing to do; every matching song already has lyrics")
        return 0

    cache_dir = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    log.info("processing %d song(s); cache: %s", len(songs), cache_dir)

    failures = 0
    for stem, original, karaoke in songs:
        try:
            process_song(stem, original, karaoke, cache_dir,
                         use_online=not args.no_online, force=args.force)
        except Exception as exc:
            failures += 1
            log.exception("FAILED: %s: %s", stem, exc)
    log.info("")
    log.info("done: %d ok, %d failed", len(songs) - failures, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
