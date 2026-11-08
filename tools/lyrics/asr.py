"""faster-whisper wrapper: language ID windows, content-keyed transcription."""
from __future__ import annotations

import logging
import os
import site
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import cache, textnorm
from .audio import (
    SAMPLE_RATE,
    SILENCE_RMS,
    duration_of,
    energy_windows,
    load_vocal_audio,
    max_window_rms,
)

log = logging.getLogger("lyrics")

MODEL_NAME = "large-v3"
TRANSCRIBE_CHUNK = 55.0
TRANSCRIBE_OVERLAP = 8.0
TRANSCRIBE_STEP = TRANSCRIBE_CHUNK - TRANSCRIBE_OVERLAP
TRANSCRIBER_VERSION = "v2-i18n"

_NVIDIA_DLL_HANDLES: list[Any] = []
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
    configure_nvidia_dlls()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def chunk_ranges(duration: float) -> list[tuple[float, float, float, float]]:
    """Split audio into overlapping clips and return their non-overlapping cores."""
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


class WhisperSession:
    """One loaded Whisper model, reused for LID and transcription."""

    def __init__(self, model_name: str = MODEL_NAME, prefer_cuda: bool = True):
        self.model_name = model_name
        self.prefer_cuda = prefer_cuda
        self.model = None
        self.device = None
        self.compute_type = None
        self.load_seconds = 0.0
        self.fallback_used = False

    def load(self):
        if self.model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run: pip install -r requirements-lyrics-i18n.txt"
            ) from exc
        devices = []
        if self.prefer_cuda and cuda_whisper_available():
            devices.append(("cuda", "float16"))
        devices.append(("cpu", "int8"))
        last_error = None
        for device, compute_type in devices:
            started = time.perf_counter()
            try:
                model = WhisperModel(
                    self.model_name, device=device, compute_type=compute_type,
                    cpu_threads=0,
                )
            except Exception as exc:
                last_error = exc
                if device == "cuda":
                    log.warning("  CUDA Whisper load failed (%s); trying CPU int8", exc)
                    continue
                raise
            self.model = model
            self.device = device
            self.compute_type = compute_type
            self.load_seconds = time.perf_counter() - started
            log.info("  loaded Whisper %s (%s %s) in %.1fs",
                     self.model_name, device.upper(), compute_type, self.load_seconds)
            return
        raise RuntimeError(f"could not load Whisper: {last_error}")

    def close(self):
        self.model = None
        self.device = None
        self.compute_type = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _fallback_cpu(self):
        log.warning("  CUDA Whisper failed during use; reloading CPU int8")
        self.close()
        from faster_whisper import WhisperModel
        started = time.perf_counter()
        self.model = WhisperModel(
            self.model_name, device="cpu", compute_type="int8", cpu_threads=0)
        self.device = "cpu"
        self.compute_type = "int8"
        self.load_seconds = time.perf_counter() - started
        self.fallback_used = True

    def detect_language_windows(self, vocals: Path, duration: float,
                                audio=None, window: float = 20.0,
                                max_windows: int = 5) -> list[dict[str, Any]]:
        self.load()
        if audio is None:
            audio = load_vocal_audio(vocals)
        if duration <= 0:
            duration = duration_of(vocals)
        if duration <= 0:
            return []

        starts: list[float] = []
        if audio is not None and len(audio):
            candidates = energy_windows(audio, duration, window=window, hop=5.0)
            regions = [(0.0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.0)]
            for low, high in regions:
                region = [(start, rms) for start, rms in candidates
                          if low * duration <= start < high * duration]
                if region:
                    starts.append(max(region, key=lambda item: item[1])[0])
            for start, _ in sorted(candidates, key=lambda item: item[1], reverse=True)[:2]:
                starts.append(start)
        if not starts:
            fractions = (0.08, 0.28, 0.5, 0.72, 0.9)
            starts = [max(0.0, min(duration - window, duration * fraction - window / 2))
                      for fraction in fractions]

        selected: list[float] = []
        for start in sorted(starts):
            start = max(0.0, min(duration - 0.5, start))
            if all(abs(start - existing) >= window * 0.6 for existing in selected):
                selected.append(start)
        selected = selected[:max_windows]

        results = []
        for start in selected:
            end = min(duration, start + window)
            first = int(start * SAMPLE_RATE)
            last = int(end * SAMPLE_RATE)
            if audio is None or last <= first:
                continue
            clip = audio[first:last]
            if clip.size == 0:
                continue
            started = time.perf_counter()
            try:
                language, probability, all_probs = self.model.detect_language(
                    audio=clip, vad_filter=False)
            except Exception as exc:
                if self.device == "cuda":
                    self._fallback_cpu()
                    language, probability, all_probs = self.model.detect_language(
                        audio=clip, vad_filter=False)
                else:
                    log.warning("  language ID failed for %.1f-%.1f: %s", start, end, exc)
                    continue
            top = [{"language": item[0], "probability": round(float(item[1]), 4)}
                   for item in (all_probs or [])[:3]]
            results.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "language": language,
                "probability": round(float(probability), 4),
                "top": top,
                "seconds": round(time.perf_counter() - started, 3),
            })
            log.info("    LID %.1f-%.1f: %s p=%.3f",
                     start, end, language, probability)
        return results

    def _transcribe_with_model(self, vocals: Path, language: str, prompt: str,
                               duration: float) -> tuple[list[dict], float]:
        started = time.perf_counter()
        rows = []
        with tempfile.TemporaryDirectory(prefix="karaoke-whisper-") as tmp:
            tmp_dir = Path(tmp)
            for clip_start, clip_end, core_start, core_end in chunk_ranges(duration):
                clip_path = tmp_dir / f"clip-{clip_start:07.2f}.wav"
                result = subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-ss", f"{clip_start:.3f}", "-t", f"{clip_end - clip_start:.3f}",
                    "-i", str(vocals), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(clip_path),
                ], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if not clip_path.exists():
                    raise RuntimeError(
                        f"ffmpeg could not extract {clip_start:.1f}-{clip_end:.1f}s\n"
                        f"{(result.stderr or result.stdout or '')[-2000:]}")
                segments, _info = self.model.transcribe(
                    str(clip_path),
                    language=language,
                    beam_size=5,
                    temperature=0.0,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    initial_prompt=prompt or None,
                )
                chunk_words = []
                log_probs = []
                for segment in segments:
                    log_probs.append(segment.avg_logprob)
                    for word in segment.words or []:
                        text = (word.word or "").strip()
                        if not text:
                            continue
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
                    "avg_logprob": round(sum(log_probs) / len(log_probs), 4)
                    if log_probs else -1.0,
                    "no_speech_prob": 0.0,
                    "words": chunk_words,
                })
                log.info("    [%6.1f-%6.1f] %d words", clip_start, clip_end,
                         len(chunk_words))
        return rows, time.perf_counter() - started

    def transcribe(self, vocals: Path, language: str, prompt: str) -> tuple[dict, float]:
        self.load()
        duration = duration_of(vocals)
        if duration <= 0:
            raise RuntimeError(f"could not read duration of {vocals}")
        try:
            rows, seconds = self._transcribe_with_model(
                vocals, language, prompt, duration)
        except Exception as exc:
            if self.device == "cuda":
                log.warning("  CUDA Whisper failed (%s); falling back to CPU int8", exc)
                self._fallback_cpu()
                rows, seconds = self._transcribe_with_model(
                    vocals, language, prompt, duration)
            else:
                raise
        data = {
            "transcriber": TRANSCRIBER_VERSION,
            "model": self.model_name,
            "language": language,
            "prompt": prompt,
            "chunk": TRANSCRIBE_CHUNK,
            "overlap": TRANSCRIBE_OVERLAP,
            "info": {
                "duration": duration,
                "language": language,
                "language_probability": 1.0,
                "device": self.device,
                "compute_type": self.compute_type,
                "load_seconds": round(self.load_seconds, 2),
                "transcribe_seconds": round(seconds, 2),
            },
            "segments": rows,
        }
        return data, seconds


def transcript_key(vocals: Path, original_hash: str, language: str, prompt: str) -> str:
    return cache.transcript_cache_key(
        vocal_hash=cache.sha256_file(vocals),
        original_hash=original_hash,
        model=MODEL_NAME,
        language=language,
        prompt=prompt,
        chunk=TRANSCRIBE_CHUNK,
        overlap=TRANSCRIBE_OVERLAP,
        transcriber_version=TRANSCRIBER_VERSION,
        beam_size=5,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=True,
    )


def transcribe_cached(stem: str, vocals: Path, original_hash: str, language: str,
                      prompt: str, cache_dir: Path, force: bool = False,
                      session: WhisperSession | None = None
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    key = transcript_key(vocals, original_hash, language, prompt)
    path = cache.transcript_cache_path(cache_dir, stem, key)
    if not force:
        cached = cache.load_transcript_cache(path, key)
        if cached is not None:
            log.info("  transcript cached: %s", path)
            return cached, {"cache_hit": True, "key": key, "path": str(path),
                            "seconds": 0.0}
    own_session = session is None
    session = session or WhisperSession()
    try:
        data, seconds = session.transcribe(vocals, language, prompt)
    finally:
        if own_session:
            session.close()
    data["key"] = key
    cache.save_transcript_cache(path, key, data)
    return data, {"cache_hit": False, "key": key, "path": str(path),
                  "seconds": round(seconds, 2)}


def strip_hallucinations(transcript: dict) -> dict:
    """Remove boilerplate Whisper invents at the end of video-sourced audio."""
    phrases = (
        "thank you for watching",
        "thanks for watching",
        "thank you watching",
        "please subscribe",
        "like and subscribe",
    )
    removed = 0
    segments = []
    for segment in transcript.get("segments", []):
        words = list(segment.get("words", []))
        norms = [textnorm.legacy_norm_token(word.get("word", "")) for word in words]
        for phrase in phrases:
            parts = [textnorm.legacy_norm_token(part) for part in phrase.split()]
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


def filter_silent_words(transcript: dict, vocals: Path, audio=None) -> dict:
    """Drop words Whisper invented in silent parts of the vocal stem."""
    if audio is None:
        audio = load_vocal_audio(vocals)
    if audio is None:
        log.warning("  could not analyse vocal energy; keeping all transcript words")
        return transcript
    dropped = 0
    segments = []
    for segment in transcript.get("segments", []):
        kept = []
        for word in segment.get("words", []):
            rms = max_window_rms(audio, float(word.get("start", 0.0)),
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


def local_words(transcript: dict, language: str = "en") -> list[dict[str, Any]]:
    words = []
    for segment in transcript.get("segments", []):
        for word in segment.get("words", []):
            text = (word.get("word") or "").strip()
            norm = textnorm.norm_token(text, language)
            if not norm:
                continue
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start + 0.15))
            if end <= start:
                end = start + 0.15
            if words and abs(start - words[-1]["start"]) < 0.001 \
                    and abs(end - words[-1]["end"]) < 0.001:
                continue
            words.append({"text": text, "norm": norm, "start": start, "end": end})
    return words


def build_prompt(title: str, artist: str, language: str,
                 reference_text: str | None = None) -> str:
    """Keep the prompt short and in the song's language/script."""
    if language in ("en", "tl"):
        prompt = f"{artist} - {title}" if artist else title
        return prompt.strip()[:120]

    def target(value: str) -> bool:
        if language == "ja":
            return any("\u3040" <= char <= "\u30ff"
                       or "\u3400" <= char <= "\u9fff" for char in value)
        if language == "ko":
            return any("\uac00" <= char <= "\ud7af" for char in value)
        return False

    if reference_text:
        for line in reference_text.splitlines():
            line = line.strip()
            if line and target(line):
                return line[:120]
    candidate = f"{artist} {title}".strip()
    if candidate and target(candidate):
        return candidate[:120]
    return ""


def top_language(results: list[dict[str, Any]]) -> dict[str, Any]:
    from . import langdetect
    return langdetect.choose_audio_language(results)
