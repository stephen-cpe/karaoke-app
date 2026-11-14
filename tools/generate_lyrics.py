"""Generate karaoke lyrics (SRT + word timings) from the original songs.

Pipeline per song:
  1. Separate vocals with Demucs; cache the stem by original content hash.
  2. Acquire lyrics from a user override, LRCLIB, or speech recognition.
  3. Detect the song language from script, a Tagalog lexicon, and Whisper LID.
  4. Transcribe with faster-whisper using the detected language.
  5. Build cues and word timings, insert [Instrumental] gaps, and publish with
     provenance, edited-file protection, and backups.

The SRT is ordinary, portable line-level subtitles. The JSON sidecar adds
per-word timings for the karaoke player's word-by-word highlighting.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.lyrics import (  # noqa: E402
    align,
    asr,
    audio,
    cache,
    cues,
    langdetect,
    publish,
    qa,
    reference,
    romanize,
    textnorm,
)

SONGS_DIR = PROJECT_ROOT / "songs"
ORIGINALS_DIR = PROJECT_ROOT / "original_songs"
LYRICS_DIR = PROJECT_ROOT / "lyrics"
CACHE_DIR = PROJECT_ROOT / "tools" / ".cache"
REPORTS_DIR = CACHE_DIR / "reports"
BACKUPS_DIR = CACHE_DIR / "backups"
SOURCE_DIR = LYRICS_DIR / "source"

AUDIO_EXTENSIONS = (".mp3", ".wav")
DURATION_MISMATCH_WARN = 1.0
MAX_REFERENCE_OFFSET = 5.0
ALIGNMENT_TIME_SHIFTS = {
    "tl": -0.20,
    "ja": -0.20,
    "ko": -0.20,
}


def _alignment_time_shift(language: str) -> float:
    return ALIGNMENT_TIME_SHIFTS.get(language, align.ALIGNMENT_TIME_SHIFT)

# Compatibility exports for tests and older helper scripts.
atomic_write_text = cache.atomic_write_text
srt_time = cues.srt_time
norm_token = textnorm.norm_token
clean_text = textnorm.clean_text
split_display_tokens = textnorm.split_display_tokens
parse_lrc = reference.parse_lrc
chunk_ranges = asr.chunk_ranges
load_vocal_audio = audio.load_vocal_audio
vocal_onset = audio.vocal_onset
vocal_phrase_end = audio.vocal_phrase_end
distribute_words = cues.distribute_words
interpolate_known = cues.interpolate_known
local_timing_is_reasonable = cues.local_timing_is_reasonable
build_lyric_lines = cues.build_lyric_lines
add_instrumental_cues = cues.add_instrumental_cues
build_from_transcript = cues.build_from_transcript
flat_lrc_tokens = cues.flat_lrc_tokens
map_lrc_time = cues.map_lrc_time
align_reference_words = cues.align_reference_words
tokens_similar = cues.tokens_similar
duration_of = audio.duration_of
configure_nvidia_dlls = asr.configure_nvidia_dlls
cuda_whisper_available = asr.cuda_whisper_available

log = logging.getLogger("lyrics")


def split_stem(stem: str) -> tuple[str, str]:
    """Split 'id_Title_Artist' into (title, artist), matching the app's rules."""
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
    originals = {path.stem: path for path in ORIGINALS_DIR.glob("*")
                 if path.suffix.lower() in AUDIO_EXTENSIONS}
    for stem, original in sorted(originals.items(), key=lambda item: item[0].casefold()):
        if song_filter and song_filter.casefold() not in stem.casefold():
            continue
        karaoke = SONGS_DIR / f"{stem}{original.suffix.lower()}"
        if not karaoke.exists():
            candidates = [path for path in SONGS_DIR.glob(f"{stem}.*")
                          if path.suffix.lower() in AUDIO_EXTENSIONS]
            if not candidates:
                log.warning("%s: no matching file in %s, skipping", stem, SONGS_DIR)
                continue
            karaoke = candidates[0]
        found.append((stem, original, karaoke))
    return found


def _seconds(started: float) -> float:
    return round(time.perf_counter() - started, 2)


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def _write_report(reports_dir: Path, stem: str, report: dict[str, Any]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    cache.atomic_write_json(reports_dir / f"{stem}.json", report)


def separate_vocals(stem: str, original: Path, cache_dir: Path,
                    force: bool = False) -> tuple[Path, dict[str, Any]]:
    """Separate vocals into a cache keyed by the original's content hash."""
    original_hash = cache.sha256_file(original)
    vocals = cache.stem_cache_path(cache_dir, stem, original_hash)
    info = {
        "original_sha256": original_hash,
        "device": None,
        "cache_hit": False,
        "seconds": 0.0,
        "path": _relative(vocals),
    }
    if vocals.is_file() and vocals.stat().st_size > 0 and not force:
        log.info("  vocals cached: %s", vocals)
        info["cache_hit"] = True
        return vocals, info

    log.info("  separating vocals with Demucs...")
    root = cache.stem_cache_root(cache_dir, original_hash)
    devices = []
    try:
        import torch
        if torch.cuda.is_available():
            devices.append("cuda")
    except Exception:
        pass
    devices.append("cpu")
    last_error = None
    for device in devices:
        started = time.perf_counter()
        result = audio.run([
            sys.executable, "-m", "demucs", "--two-stems=vocals",
            "-n", "htdemucs", "-d", device, "-o", str(root), str(original),
        ])
        if (result.returncode == 0 and vocals.is_file()
                and vocals.stat().st_size > 0):
            info.update({"device": device, "seconds": _seconds(started)})
            log.info("  separated vocals on %s in %.1fs", device.upper(),
                     info["seconds"])
            return vocals, info
        last_error = (result.stderr or result.stdout or "")[-3000:]
        if device == "cuda":
            log.warning("  Demucs CUDA failed; trying CPU")
        else:
            break
    raise RuntimeError(f"Demucs did not produce {vocals}\n{last_error}")


def _reference_text(reference_dict: dict[str, Any] | None) -> str:
    if not reference_dict:
        return ""
    return "\n".join(str(line.get("text") or "")
                     for line in reference_dict.get("lines") or [])


def _reference_line_count(reference_dict: dict[str, Any] | None,
                          language: str) -> int:
    if not reference_dict:
        return 0
    return sum(
        1 for line in reference_dict.get("lines") or []
        if textnorm.token_count(str(line.get("text") or ""), language) > 0
    )


def _candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    record = item.get("record") or {}
    return {
        "id": record.get("id"),
        "track": record.get("trackName"),
        "artist": record.get("artistName"),
        "album": record.get("albumName"),
        "duration": record.get("duration"),
        "has_word_sync": record.get("hasWordSync"),
        "form": item.get("form_name"),
        "score": round(float(item.get("score", 0.0)), 3),
        "duration_error": round(float(item.get("duration_error", 0.0)), 3),
        "coverage": round(float(item.get("coverage", 0.0)), 4),
        "local_coverage": round(float(item.get("local_coverage", 0.0)), 4),
        "offset": round(float(item.get("offset", 0.0)), 3),
        "energy_score": round(float(item.get("energy_score", 0.0)), 4),
        "penalty": round(float(item.get("penalty", 0.0)), 3),
        "word_sync": bool(item.get("word_sync")),
        "alignment": item.get("alignment"),
        "build_quality": item.get("build_quality"),
    }


def _provisional_reference(records: list[dict[str, Any]], language: str,
                           duration: float, audio_data,
                           client: reference.LRCLIBClient):
    chosen, scored = reference.choose_candidate(
        records, language, duration, audio_data, local=None,
        fetch_full=client.fetch_by_id)
    return chosen, scored


def _reject_suspect_reference_offset(reference_dict: dict[str, Any] | None,
                                     report: dict[str, Any]) -> None:
    if not reference_dict:
        return
    try:
        offset = float(reference_dict.get("offset") or 0.0)
    except (TypeError, ValueError):
        offset = 0.0
    if abs(offset) <= MAX_REFERENCE_OFFSET:
        return
    reference_dict["alignment_offset_reset"] = round(offset, 3)
    reference_dict["offset"] = 0.0
    reference_dict["anchors"] = {}
    reference_dict["token_local"] = {}
    message = (f"reference timing offset {offset:+.1f}s is implausible for "
               "a same-duration master; falling back to LRCLIB/onset timing")
    report.setdefault("warnings", []).append(message)
    log.warning("  %s", message)


def _needs_full_asr(language: str, has_reference: bool,
                    align_english: bool) -> tuple[bool, str]:
    if not has_reference:
        return True, "ASR text is the lyric source"
    if language == "en":
        if align_english:
            return False, "English forced-alignment path requested"
        return True, "English compatibility path keeps ASR timing"
    if language in ("ja", "ko"):
        return False, "known JA/KO text is romanized and force-aligned"
    if language == "tl":
        return False, "known Tagalog text is force-aligned"
    return True, "no forced-alignment path for this language"


def _apply_text_replacements(lines: list[dict[str, Any]],
                             replacements: dict[str, str] | None
                             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply display-spelling replacements before alignment.

    Replacements are matched against the romanized line text. The line's
    token list is rebuilt from the replaced text so the timings belong to the
    words the singer actually sees.
    """
    if not replacements:
        return lines, []
    cleaned = {
        str(source): str(target)
        for source, target in replacements.items()
        if str(source) and str(target)
    }
    if not cleaned:
        return lines, []
    result = []
    applied = []
    for index, line in enumerate(lines):
        row = dict(line)
        text = str(row.get("text") or "")
        row_applied = []
        for source, target in cleaned.items():
            if source in text:
                count = text.count(source)
                text = text.replace(source, target)
                row_applied.append({
                    "line": index + 1,
                    "from": source,
                    "to": target,
                    "count": count,
                })
        if row_applied:
            row["text"] = text
            row["words"] = []
            applied.extend(row_applied)
        result.append(row)
    return result, applied


def _romanize_candidate(item: dict[str, Any], language: str,
                        replacements: dict[str, str] | None = None) -> None:
    """Replace native-script reference text with the display romanization."""
    lines = item.get("lines") or []
    texts = [str(line.get("text") or "") for line in lines]
    pre_romanized = (
        language == "ja"
        and not any(textnorm.has_japanese(text) for text in texts))
    romanized_lines, readings = romanize.romanize_lines(
        lines, language, pre_romanized=pre_romanized)
    for row in romanized_lines:
        blocks = []
        for word in row.get("words") or []:
            result = romanize.romanize_line(
                str(word.get("text") or ""), language,
                pre_romanized=pre_romanized)
            block_text = result.text
            if replacements:
                block_lines, _ = _apply_text_replacements(
                    [{"text": block_text}], replacements)
                block_text = str(block_lines[0].get("text") or "")
            if block_text:
                blocks.append({**word, "romanized": block_text})
        row["word_sync_blocks"] = blocks
    replaced_lines, applied = _apply_text_replacements(
        romanized_lines, replacements)
    item["lines"] = replaced_lines
    item["tokens"] = reference.flatten_tokens(replaced_lines, "en")
    item["romanized_readings"] = readings
    item["replacements_applied"] = applied
    item["romanization"] = {
        "language": language,
        "pre_romanized": pre_romanized,
        "method": ("pre-romanized"
                   if pre_romanized else
                   "korean-academic" if language == "ko"
                   else "japanese-fugashi"),
        "replacements": applied,
    }


def _romanize_asr_lines(lines: list[dict[str, Any]], language: str
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    romanized_lines, readings = romanize.romanize_lines(lines, language)
    for line in romanized_lines:
        words = []
        for word in line.get("words") or []:
            result = romanize.romanize_line(
                str(word.get("text") or ""), language)
            words.append({**word, "text": result.text})
        line["words"] = words
    return romanized_lines, readings


def _script_gate_blocks_publication(language: str,
                                    gate: dict[str, Any]) -> bool:
    """A JA/KO song is not written when display text still leaks script."""
    return language in ("ja", "ko") and not bool(gate.get("passed"))


def _script_gate(cues: list[dict[str, Any]], language: str) -> dict[str, Any]:
    """Hard gate: display text and word text must contain no native script."""
    leaks = []
    for index, cue in enumerate(cues, 1):
        if cue.get("instrumental"):
            continue
        text = str(cue.get("text") or "")
        word_text = " ".join(
            str(word.get("text") or "") for word in cue.get("words") or [])
        combined = f"{text} {word_text}"
        if language == "ja":
            leak = textnorm.has_kana(combined) or textnorm.has_kanji(combined)
        elif language == "ko":
            leak = textnorm.has_hangul(combined)
        else:
            leak = False
        if leak:
            leaks.append({
                "line": index,
                "text": text,
                "words": word_text,
            })
    return {"passed": not leaks, "language": language, "leaks": leaks}


def _prepare_alignment_candidates(records: list[dict[str, Any]], language: str,
                                  duration: float, audio_data, client,
                                  replacements: dict[str, str] | None = None
                                  ) -> list[dict[str, Any]]:
    prepared = []
    for record in records:
        try:
            forms = reference.record_forms(record)
            item = reference.score_record(
                record, forms, language, duration, audio_data, local=None)
        except Exception as exc:
            log.warning("  candidate parse failed for %s: %s",
                        record.get("id"), exc)
            continue
        if item["duration_error"] > reference.LRCLIB_DURATION_TOLERANCE:
            continue
        if item["record"].get("instrumental"):
            continue
        if float(item.get("compatibility") or 0.0) < 0.5:
            continue
        prepared.append(item)

    prepared.sort(key=lambda item: item["score"], reverse=True)
    full = []
    for item in prepared:
        record_id = item["record"].get("id")
        if record_id is not None:
            try:
                full_record = client.fetch_by_id(int(record_id))
            except Exception as exc:
                log.warning("  LRCLIB by-id fetch failed for %s: %s",
                            record_id, exc)
                full_record = None
            if full_record:
                forms = reference.record_forms(full_record)
                rescored = reference.score_record(
                    full_record, forms, language, duration, audio_data,
                    local=None)
                if rescored["duration_error"] <= reference.LRCLIB_DURATION_TOLERANCE:
                    item = rescored
        if language in ("ja", "ko"):
            _romanize_candidate(item, language, replacements)
        full.append(item)
    return full


def _alignment_choice_key(summary: dict[str, Any]) -> tuple:
    """Prefer the pass that leaves the most lines above the word threshold."""
    return (
        float(summary.get("line_coverage") or 0.0),
        float(summary.get("word_coverage") or 0.0),
        float(summary.get("mean_score") or -999.0),
    )


def _local_word_onset(audio_data, start: float, end: float):
    times, values = audio.window_rms(
        audio_data, max(0.0, start - 0.75), min(end + 0.05, start + 0.25),
        window=0.05, hop=0.025)
    if not len(values):
        return None
    peak = float(values.max())
    threshold = max(audio.ONSET_RMS, 0.2 * peak)
    active = [bool(value >= threshold) for value in values]
    transition = None
    for index in range(1, len(active)):
        if (not active[index - 1] and active[index]
                and times[index] <= start + 0.10):
            transition = float(times[index])
    return transition


def _timing_agreement(built: list[dict[str, Any]], audio_data,
                      max_words: int = 300) -> dict[str, Any]:
    if audio_data is None:
        return {"n": 0, "within_0_2": 0.0, "p90_abs": 9.99,
                "median": 0.0}
    word_intervals = []
    for cue in built:
        if cue.get("instrumental"):
            continue
        for word in cue.get("words") or []:
            word_intervals.append((float(word["start"]), float(word["end"])))
    if not word_intervals:
        return {"n": 0, "within_0_2": 0.0, "p90_abs": 9.99,
                "median": 0.0}
    if len(word_intervals) > max_words:
        step = len(word_intervals) / float(max_words)
        word_intervals = [word_intervals[int(index * step)]
                          for index in range(max_words)]
    diffs = []
    for start, end in word_intervals:
        onset = _local_word_onset(audio_data, start, end)
        if onset is not None:
            diffs.append(start - onset)
    if not diffs:
        return {"n": 0, "within_0_2": 0.0, "p90_abs": 9.99,
                "median": 0.0}
    abs_values = sorted(abs(value) for value in diffs)
    p90 = abs_values[min(len(abs_values) - 1, round(0.9 * (len(abs_values) - 1)))]
    diffs.sort()
    median = diffs[len(diffs) // 2]
    return {
        "n": len(diffs),
        "within_0_2": round(
            sum(1 for value in diffs if abs(value) <= 0.2) / len(diffs), 4),
        "p90_abs": round(p90, 3),
        "median": round(median, 3),
    }


def _built_quality(built: list[dict[str, Any]], audio_data,
                   total_tokens: int, max_words: int = 60
                   ) -> dict[str, Any]:
    lyric_lines = sum(1 for cue in built if not cue.get("instrumental"))
    line_level = sum(1 for cue in built
                     if not cue.get("instrumental") and not cue.get("words"))
    words = sum(len(cue.get("words") or []) for cue in built)
    agreement = _timing_agreement(built, audio_data, max_words=max_words)
    coverage = words / total_tokens if total_tokens else 0.0
    line_level_rate = line_level / lyric_lines if lyric_lines else 0.0
    first_start = None
    for cue in built:
        if cue.get("instrumental") or not cue.get("words"):
            continue
        first_start = float(cue["words"][0]["start"])
        break
    first_rms = None
    if first_start is not None and audio_data is not None:
        first_rms = audio.max_window_rms(
            audio_data, max(0.0, first_start - 0.2), first_start + 0.5)
    silent_first = (first_rms is not None
                    and first_rms < audio.ONSET_RMS)
    score = (coverage - line_level_rate
             + agreement["within_0_2"] - agreement["p90_abs"] * 0.5
             - (0.75 if silent_first else 0.0))
    return {
        "lyric_lines": lyric_lines,
        "line_level_lines": line_level,
        "line_level_rate": round(line_level_rate, 4),
        "coverage": round(coverage, 4),
        "agreement": agreement,
        "first_word_rms": (round(first_rms, 5)
                           if first_rms is not None else None),
        "silent_first_word": silent_first,
        "score": round(score, 4),
    }


def _align_candidates(candidates: list[dict[str, Any]], language: str,
                      audio_data, session: align.AlignSession,
                      duration: float
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    emissions, stride = session.prepare_emissions(audio_data)
    emissions_seconds = session.seconds
    total_align_seconds = 0.0
    for item in candidates:
        known_jako = language in ("ja", "ko")
        duration_error = float(item.get("duration_error") or 0.0)
        windowed = duration_error > 1.5
        result = session.align_candidate(
            emissions, stride, item["lines"], item["tokens"], language,
            windowed=windowed,
            window_mode="line",
            star_frequency="edges" if windowed else "line",
            audio_data=audio_data, snap_repeated=True,
            time_shift=_alignment_time_shift(language))
        if known_jako:
            base_key = _alignment_choice_key(result.summary)
            if base_key[0] < 0.98 or base_key[1] < 0.98:
                refined = session.align_candidate(
                    emissions, stride, item["lines"], item["tokens"], language,
                    windowed=True, window_mode="line", star_frequency="edges",
                    audio_data=audio_data, snap_repeated=True,
                    merge_passes=True, retry_low_coverage=True,
                    start_pad=align.LINE_WINDOW_START_PAD,
                    end_pad=align.LINE_WINDOW_END_PAD,
                    time_shift=_alignment_time_shift(language))
                if (_alignment_choice_key(refined.summary)
                        > base_key):
                    result = refined
        allow_edges = known_jako
        built = cues.build_aligned_lines(
            item, result.lines, duration, audio_data, language,
            allow_edge_inference=allow_edges)
        forced_quality = _built_quality(
            built, audio_data, len(item["tokens"]))
        item["forced_built"] = built
        item["forced_quality"] = forced_quality
        item["build_quality"] = forced_quality
        item["word_sync"] = None
        if any(line.get("word_sync_blocks") for line in item["lines"]):
            word_alignments = cues.build_word_sync_lines(
                item, duration, language)
            word_built = cues.build_aligned_lines(
                item, word_alignments, duration, audio_data, language,
                allow_edge_inference=known_jako)
            for cue in word_built:
                if not cue.get("instrumental") and cue.get("words"):
                    cue["timing_source"] = "lrclib-word-sync"
            item["word_sync"] = {
                "line_alignment": word_alignments,
                "built": word_built,
                "quality": _built_quality(
                    word_built, audio_data, len(item["tokens"])),
            }
        item["alignment"] = result.summary
        item["line_alignment"] = result.lines
        total_align_seconds += float(result.summary.get("seconds") or 0.0)
    candidates.sort(key=align.candidate_rank_key, reverse=True)
    word_sync_info = {"evaluated": 0, "won": False}
    word_candidates = [item for item in candidates
                       if item.get("word_sync")]
    if word_candidates:
        word_best = max(
            word_candidates,
            key=lambda item: item["word_sync"]["quality"]["score"])
        forced_best = candidates[0]
        forced_quality = forced_best.get("forced_quality") or {}
        word_quality = word_best["word_sync"]["quality"]
        word_sync_info.update({
            "evaluated": len(word_candidates),
            "candidate": (word_best.get("record") or {}).get("id"),
            "forced_score": forced_quality.get("score"),
            "word_sync_score": word_quality.get("score"),
            "forced_agreement": forced_quality.get("agreement"),
            "word_sync_agreement": word_quality.get("agreement"),
        })
        if (word_quality["score"]
                > float(forced_quality.get("score") or -999.0) + 0.02):
            candidates.remove(word_best)
            candidates.insert(0, word_best)
            word_best["chosen_tier"] = "word-sync"
            word_sync_info["won"] = True
        else:
            forced_best["chosen_tier"] = "forced-alignment"
    if candidates and not candidates[0].get("chosen_tier"):
        candidates[0]["chosen_tier"] = "forced-alignment"
    info = {
        "model": session.model_name,
        "device": session.device,
        "dtype": str(session.dtype),
        "load_seconds": round(session.load_seconds, 2),
        "emissions_seconds": round(emissions_seconds, 2),
        "candidate_seconds": round(total_align_seconds, 2),
        "candidates_aligned": len(candidates),
        "word_sync": word_sync_info,
    }
    return candidates, info


def _alignment_lines_summary(line_alignments: list[dict[str, Any]]
                             ) -> list[dict[str, Any]]:
    return [{
        "reference_line": int(line.get("index", index)) + 1,
        "coverage": round(float(line.get("coverage") or 0.0), 4),
        "aligned_words": int(line.get("aligned_words") or 0),
        "total_words": int(line.get("total_words") or 0),
        "mean_score": line.get("mean_score"),
        "min_score": line.get("min_score"),
    } for index, line in enumerate(line_alignments)]


def _timing_source_summary(final_cues: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    asr_fallback_lines = []
    line_level_lines = []
    for index, cue in enumerate(final_cues, 1):
        source = str(cue.get("timing_source") or "none")
        counts[source] = counts.get(source, 0) + 1
        if source == "asr" and not cue.get("instrumental"):
            if cue.get("line_fallback_reason"):
                asr_fallback_lines.append(index)
        if (not cue.get("instrumental") and not cue.get("words")):
            line_level_lines.append(index)
    return {
        "counts": counts,
        "asr_fallback_lines": asr_fallback_lines,
        "line_level_lines": line_level_lines,
    }


def _clean_asr_lines(asr_lines: list[dict[str, Any]],
                     line_alignments: list[dict[str, Any]],
                     audio_data, duration: float, language: str,
                     threshold: float) -> tuple[list[dict[str, Any]],
                                                list[dict[str, Any]]]:
    """Remove ASR words with no vocal energy or low aligner confidence."""
    first_strong = None
    if audio_data is not None:
        first_strong = audio.vocal_onset(
            audio_data, 0.0, duration, threshold=audio.STRONG_ONSET_RMS)
    cleaned = []
    dropped = []
    for line_index, line in enumerate(asr_lines):
        alignment = (line_alignments[line_index]
                     if line_index < len(line_alignments) else {})
        by_token = {
            int(word.token): word for word in alignment.get("words") or []
            if getattr(word, "token", None) is not None
        }
        kept = []
        for token_index, word in enumerate(line.get("words") or []):
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start + 0.15))
            timing = by_token.get(token_index)
            rms = (audio.max_window_rms(audio_data, start, end)
                   if audio_data is not None else 0.0)
            reason = None
            if timing is not None and timing.score < threshold:
                reason = (f"aligner score {timing.score:.3f} "
                          f"< {threshold:.2f}")
            elif audio_data is not None and rms < audio.ONSET_RMS:
                reason = f"no vocal energy (rms {rms:.5f})"
            elif (audio_data is not None and first_strong is not None
                  and end <= first_strong + 0.05
                  and rms < audio.STRONG_ONSET_RMS):
                reason = f"before first vocal onset ({first_strong:.2f}s)"
            if reason:
                dropped.append({
                    "line": line_index + 1,
                    "text": str(word.get("text") or "").strip(),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "score": (round(timing.score, 4)
                              if timing is not None else None),
                    "reason": reason,
                })
                continue
            kept.append(word)
        if not kept:
            continue
        text = textnorm.clean_text(
            " ".join(str(word.get("text") or "") for word in kept),
            language=language)
        if not text:
            continue
        row = dict(line)
        row["start"] = round(float(kept[0]["start"]), 3)
        row["end"] = round(min(duration, float(kept[-1]["end"]) + 0.35), 3)
        row["text"] = text
        row["words"] = kept
        cleaned.append(row)
    return cleaned, dropped


def process_song(stem: str, original: Path, karaoke: Path, cache_dir: Path,
                 *, use_online: bool, force: bool, output_dir: Path,
                 overwrite_edited: bool, reports_dir: Path,
                 backups_dir: Path, source_dir: Path,
                 align_english: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {
        "stem": stem,
        "status": "running",
        "stages": {},
        "errors": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    song_started = time.perf_counter()
    try:
        log.info("")
        log.info("=== %s ===", stem)
        duration = audio.duration_of(karaoke)
        if duration <= 0:
            raise RuntimeError(f"could not read duration of {karaoke}")
        log.info("  duration: %.1fs", duration)
        report["duration"] = round(duration, 3)

        original_duration = audio.duration_of(original)
        if original_duration <= 0:
            log.warning("  could not read duration of %s", original.name)
        elif abs(original_duration - duration) > DURATION_MISMATCH_WARN:
            log.warning(
                "  duration mismatch: original %.1fs vs instrumental %.1fs; "
                "check that the instrumental was made from this original",
                original_duration, duration)

        title, artist = split_stem(stem)
        report["title"] = title
        report["artist"] = artist

        started = time.perf_counter()
        vocals, stem_info = separate_vocals(stem, original, cache_dir, force)
        report["stages"]["demucs"] = stem_info
        report["vocal_stem"] = stem_info["path"]
        audio_data = audio.load_vocal_audio(vocals)
        if audio_data is None:
            report["errors"].append("vocal stem could not be decoded for energy checks")
        music_path = vocals.with_name("no_vocals.wav")
        music_data = (audio.load_vocal_audio(music_path)
                      if music_path.is_file() else None)
        if music_data is None:
            report["errors"].append(
                "instrumental stem could not be decoded for gap classification")

        override = reference.user_override(source_dir, stem)
        replacements = (override.get("replacements")
                        if override and isinstance(override.get("replacements"), dict)
                        else None)
        if override:
            log.info("  user override: %s", override.get("path"))
            report["user_override"] = override.get("path")
            if replacements:
                log.info("  text replacements: %d phrase(s)", len(replacements))

        client = reference.LRCLIBClient(cache_dir)
        lookup = None
        if not use_online:
            lookup = reference.LookupResult("disabled", [], error="--no-online")
        elif override and override.get("lines"):
            lookup = reference.LookupResult("disabled", [], error="user override")
        else:
            started = time.perf_counter()
            try:
                lookup = client.lookup(title, artist, duration)
            except (reference.LRCLIBUnavailable, reference.LRCLIBError) as exc:
                lookup = reference.LookupResult("unreachable", [], error=str(exc),
                                                requests=client.requests)
            report["stages"]["lrclib"] = {
                "seconds": _seconds(started),
                "status": lookup.status,
                "cached": lookup.cached,
                "candidates": len(lookup.records),
                "requests": lookup.requests,
                "error": lookup.error,
                "artist_fuzzy_match": lookup.artist_fuzzy_match,
            }
            if lookup.status == "ok":
                log.info("  LRCLIB: %d candidate(s)%s", len(lookup.records),
                         " (cached)" if lookup.cached else "")
            elif lookup.status == "not_found":
                log.warning("  LRCLIB: not found")
            elif lookup.status == "unreachable":
                log.warning("  LRCLIB unreachable: %s", lookup.error)

        session = asr.WhisperSession()
        lid_results: list[dict[str, Any]] = []
        model_info: dict[str, Any] = {}
        transcript: dict[str, Any] | None = None
        dropped_asr_words: list[dict[str, Any]] = []
        local: list[dict[str, Any]] = []
        asr_used = False
        asr_reason = ""
        try:
            started = time.perf_counter()
            try:
                lid_results = session.detect_language_windows(
                    vocals, duration, audio=audio_data)
            except Exception as exc:
                report["errors"].append(f"language ID failed: {exc}")
                log.warning("  language ID failed: %s", exc)
            report["stages"]["language_id"] = {
                "seconds": _seconds(started),
                "windows": len(lid_results),
            }
            lid_summary = langdetect.choose_audio_language(lid_results)
            guess_language = lid_summary.get("language") or "en"

            provisional = None
            if override and override.get("lines"):
                reference_lines = reference.clean_lines(
                    override.get("lines") or [], override.get("language") or guess_language)
                reference_text = "\n".join(line["text"] for line in reference_lines)
                reference_line_texts = [line["text"] for line in reference_lines]
            elif lookup and lookup.records:
                provisional, _ = _provisional_reference(
                    lookup.records, guess_language, duration, audio_data, client)
                reference_text = _reference_text(provisional)
                reference_line_texts = [line.get("text", "")
                                        for line in (provisional or {}).get("lines", [])]
            else:
                reference_text = ""
                reference_line_texts = []

            decision = langdetect.detect_language(
                reference_text, lid_results,
                override=override.get("language") if override else None,
                lines=reference_line_texts)
            language = decision["language"]
            log.info("  language: %s confidence=%.2f source=%s",
                     language, float(decision.get("confidence", 0.0)),
                     decision.get("source"))
            report["language"] = language
            report["language_decision"] = decision
            report["stages"]["language_id"]["result"] = decision.get("source")
            report["stages"]["language_id"]["language"] = language
            report["stages"]["language_id"]["confidence"] = round(
                float(decision.get("confidence", 0.0)), 4)
            report["stages"]["language_id"]["audio"] = lid_summary

            model_info = {
                "model": asr.MODEL_NAME,
                "device": session.device,
                "compute_type": session.compute_type,
                "load_seconds": round(session.load_seconds, 2),
                "fallback_used": session.fallback_used,
                "purpose": "language-id",
            }
            has_reference = bool(
                (override and override.get("lines"))
                or (lookup and lookup.records))
            full_asr_needed, asr_reason = _needs_full_asr(
                language, has_reference, align_english)
            english_compat = (
                language == "en" and has_reference and not align_english)
            if full_asr_needed:
                prompt = asr.build_prompt(title, artist, language, None)
                started = time.perf_counter()
                legacy_transcript = None
                legacy_path = None
                if english_compat and not force:
                    legacy_transcript, legacy_path = asr.load_legacy_transcript(
                        cache_dir, stem, duration, language)
                if legacy_transcript is not None:
                    transcript = legacy_transcript
                    asr_reason = (
                        "English compatibility reuses the pre-Phase-1 transcript cache")
                    report["stages"]["asr"] = {
                        "used": True,
                        "legacy_transcript": True,
                        "transcript_cache": "legacy",
                        "cache_hit": True,
                        "path": legacy_path,
                        "seconds": 0.0,
                        "language": language,
                        "prompt_chars": 0,
                        "words": sum(len(segment.get("words") or [])
                                     for segment in transcript.get("segments") or []),
                        "reason": asr_reason,
                    }
                    model_info["purpose"] = "language-id + legacy transcript"
                    asr_used = True
                    log.info("  using legacy English transcript: %s", legacy_path)
                else:
                    transcript, cache_info = asr.transcribe_cached(
                        stem, vocals, stem_info["original_sha256"], language, prompt,
                        cache_dir, force=force, session=session)
                    report["stages"]["asr"] = {
                        **cache_info,
                        "transcript_cache": "content-keyed",
                        "seconds": cache_info.get("seconds", _seconds(started)),
                        "language": language,
                        "prompt_chars": len(prompt),
                        "words": sum(len(segment.get("words") or [])
                                     for segment in transcript.get("segments") or []),
                        "used": True,
                        "reason": asr_reason,
                    }
                    model_info["purpose"] = "language-id + transcription"
                    asr_used = True
                    if cache_info.get("cache_hit"):
                        log.info("  transcript cache hit: %s", cache_info["path"])
                    else:
                        log.info("  transcribed in %.1fs",
                                 report["stages"]["asr"]["seconds"])
            else:
                report["stages"]["asr"] = {
                    "used": False,
                    "transcript_cache": "none",
                    "reason": asr_reason,
                    "seconds": 0.0,
                    "language": language,
                }
                log.info("  full ASR skipped: %s", asr_reason)
        finally:
            session.close()

        if transcript is not None:
            transcript = asr.strip_hallucinations(
                transcript, dropped=dropped_asr_words)
            transcript = asr.filter_silent_words(
                transcript, vocals, audio_data, dropped=dropped_asr_words)
            local = asr.local_words(transcript, language)
            report["stages"]["asr"]["local_words"] = len(local)
            report["stages"]["asr"]["dropped_words"] = len(dropped_asr_words)

        chosen = None
        candidates: list[dict[str, Any]] = []
        reference_dict: dict[str, Any] | None = None
        reference_line_count = 0
        text_source = "asr"
        timing_source = "asr"
        built: list[dict[str, Any]] = []
        alignment_info: dict[str, Any] | None = None
        line_alignments: list[dict[str, Any]] = []
        aligner_info: dict[str, Any] = {}
        asr_lines: list[dict[str, Any]] | None = None
        romanization_info: dict[str, Any] | None = None

        if override and override.get("lines"):
            if full_asr_needed:
                reference_dict = reference.reference_from_override(
                    override, language, duration, local)
                reference_dict["local"] = local
                cues.align_reference_words(reference_dict, local, duration, language)
                cues.drop_duplicate_unmatched_lines(reference_dict, language)
                _reject_suspect_reference_offset(reference_dict, report)
                reference_line_count = _reference_line_count(reference_dict, language)
                built = cues.build_lyric_lines(
                    reference_dict, duration, audio_data, language)
                text_source = "user"
                timing_source = "asr"
            else:
                reference_dict = reference.reference_from_override(
                    override, language, duration, [])
                reference_line_count = _reference_line_count(reference_dict, language)
                align_session = align.shared_session()
                try:
                    started = time.perf_counter()
                    known_jako = language in ("ja", "ko")
                    result = align_session.align_audio_candidate(
                        audio_data, reference_dict["lines"],
                        reference_dict["tokens"], language, duration,
                        windowed=known_jako,
                        window_mode="line",
                        star_frequency="edges" if known_jako else None,
                        snap_repeated=True,
                        merge_passes=known_jako,
                        retry_low_coverage=known_jako,
                        start_pad=(align.LINE_WINDOW_START_PAD
                                   if known_jako else None),
                        end_pad=(align.LINE_WINDOW_END_PAD
                                 if known_jako else None),
                        time_shift=_alignment_time_shift(language))
                    alignment_info = result.summary
                    line_alignments = result.lines
                    aligner_info = {
                        "model": align_session.model_name,
                        "device": align_session.device,
                        "dtype": str(align_session.dtype),
                        "load_seconds": round(align_session.load_seconds, 2),
                        "seconds": _seconds(started),
                    }
                    report["stages"]["alignment"] = aligner_info
                    built = cues.build_aligned_lines(
                        reference_dict, line_alignments, duration,
                        audio_data, language,
                        allow_edge_inference=language in ("ja", "ko"))
                    text_source = "user"
                    timing_source = "forced-alignment"
                finally:
                    align_session.close()
        elif lookup and lookup.records:
            if not full_asr_needed:
                candidate_items = _prepare_alignment_candidates(
                    lookup.records, language, duration, audio_data, client,
                    replacements=replacements)
                if not candidate_items:
                    raise RuntimeError(
                        "no LRCLIB candidate could be prepared for forced alignment")
                align_session = align.shared_session()
                try:
                    started = time.perf_counter()
                    candidates, aligner_info = _align_candidates(
                        candidate_items, language, audio_data, align_session,
                        duration)
                    chosen = candidates[0]
                    reference_dict = reference.reference_from_candidate(chosen)
                    reference_line_count = _reference_line_count(
                        reference_dict, language)
                    aligner_info["seconds"] = _seconds(started)
                    report["stages"]["alignment"] = aligner_info
                    if chosen.get("chosen_tier") == "word-sync":
                        word_sync = chosen["word_sync"]
                        quality = word_sync["quality"]
                        alignment_info = {
                            "word_coverage": quality["coverage"],
                            "line_coverage": round(
                                1.0 - quality["line_level_rate"], 4),
                            "mean_score": 0.0,
                            "lines": len(reference_dict.get("lines") or []),
                            "windowed": False,
                            "word_sync": {
                                **(aligner_info.get("word_sync") or {}),
                                "chosen": True,
                                "quality": quality,
                            },
                            "timing_agreement": quality["agreement"],
                            "build_quality": quality,
                        }
                        line_alignments = word_sync["line_alignment"]
                        built = word_sync["built"]
                        timing_source = "lrclib-word-sync"
                        log.info(
                            "  word-sync tier: chosen id=%s coverage=%.1f%% "
                            "line-level=%d agreement=%s",
                            (chosen.get("record") or {}).get("id"),
                            float(quality.get("coverage") or 0.0) * 100,
                            int(quality.get("line_level_lines") or 0),
                            quality.get("agreement"))
                    else:
                        alignment_info = chosen.get("alignment")
                        if aligner_info.get("word_sync"):
                            alignment_info["word_sync"] = aligner_info[
                                "word_sync"]
                        line_alignments = chosen.get("line_alignment") or []
                        built = cues.build_aligned_lines(
                            reference_dict, line_alignments, duration,
                            audio_data, language,
                            allow_edge_inference=language in ("ja", "ko"))
                        timing_source = "forced-alignment"
                        log.info(
                            "  forced alignment: chosen id=%s coverage=%.1f%% "
                            "mean_score=%.3f",
                            (chosen.get("record") or {}).get("id"),
                            float(alignment_info.get("word_coverage", 0.0)) * 100,
                            float(alignment_info.get("mean_score", 0.0)))
                    text_source = reference_dict.get(
                        "text_source", "lrclib-plain")
                finally:
                    align_session.close()
            else:
                legacy_records = None
                if english_compat:
                    legacy_records = reference.load_legacy_search(
                        cache_dir, artist, title)
                if english_compat and legacy_records:
                    chosen, candidates = reference.choose_legacy_candidate(
                        legacy_records, language, duration, local)
                    log.info("  English compatibility: %d legacy candidate(s)",
                             len(candidates))
                else:
                    chosen, candidates = reference.choose_candidate(
                        lookup.records, language, duration, audio_data, local=local,
                        fetch_full=client.fetch_by_id)
                if chosen:
                    reference_dict = reference.reference_from_candidate(chosen)
                    reference_dict["local"] = local
                    cues.align_reference_words(reference_dict, local, duration, language)
                    cues.drop_duplicate_unmatched_lines(reference_dict, language)
                    _reject_suspect_reference_offset(reference_dict, report)
                    reference_line_count = _reference_line_count(
                        reference_dict, language)
                    built = cues.build_lyric_lines(
                        reference_dict, duration, audio_data, language)
                    text_source = reference_dict.get("text_source", "lrclib-plain")
                    timing_source = "asr"
                else:
                    built = cues.build_from_transcript(
                        transcript, duration, language)
                    text_source = "asr"
                    timing_source = "asr"
        else:
            if transcript is None:
                raise RuntimeError("no lyric source and no ASR transcript")
            asr_lines = cues.build_from_transcript(transcript, duration, language)
            if not asr_lines:
                raise RuntimeError("ASR produced no lyric lines")
            asr_readings: list[dict[str, Any]] = []
            if language in ("ja", "ko"):
                asr_lines, asr_readings = _romanize_asr_lines(
                    asr_lines, language)
                romanization_info = {
                    "language": language,
                    "method": ("korean-academic" if language == "ko"
                               else "japanese-fugashi"),
                    "readings": asr_readings,
                }
            tokens = cues.flat_lrc_tokens(asr_lines, "en")
            align_session = align.shared_session()
            try:
                started = time.perf_counter()
                result = align_session.align_audio_candidate(
                    audio_data, asr_lines, tokens, language, duration,
                    windowed=True, window_mode="line", star_frequency="edges",
                    min_word_score=align.MIN_WORD_SCORE,
                    time_shift=_alignment_time_shift(language))
                cleaned_lines, cleanup_dropped = _clean_asr_lines(
                    asr_lines, result.lines, audio_data, duration, language,
                    align.MIN_WORD_SCORE)
                if cleanup_dropped:
                    dropped_asr_words.extend(cleanup_dropped)
                    report["stages"]["asr"]["dropped_words"] = len(
                        dropped_asr_words)
                    log.info("  dropped %d low-confidence/no-energy ASR word(s)",
                             len(cleanup_dropped))
                if cleaned_lines:
                    asr_lines = cleaned_lines
                    tokens = cues.flat_lrc_tokens(
                        asr_lines, "en" if language in ("ja", "ko") else language)
                    result = align_session.align_audio_candidate(
                        audio_data, asr_lines, tokens, language, duration,
                        windowed=True, window_mode="line",
                        star_frequency="edges",
                        min_word_score=align.MIN_WORD_SCORE,
                        time_shift=_alignment_time_shift(language))
                alignment_info = result.summary
                line_alignments = result.lines
                aligner_info = {
                    "model": align_session.model_name,
                    "device": align_session.device,
                    "dtype": str(align_session.dtype),
                    "load_seconds": round(align_session.load_seconds, 2),
                    "seconds": _seconds(started),
                }
                report["stages"]["alignment"] = aligner_info
                built = cues.build_aligned_asr_lines(
                    asr_lines, line_alignments, duration, audio_data, language)
                text_source = "asr"
                timing_source = "forced-alignment"
            finally:
                align_session.close()

        if romanization_info is None and reference_dict:
            if reference_dict.get("romanization"):
                romanization_info = {
                    **reference_dict["romanization"],
                    "readings": reference_dict.get("romanized_readings") or [],
                    "replacements": (
                        reference_dict.get("replacements_applied")
                        or reference_dict["romanization"].get("replacements")
                        or []),
                }

        if not built:
            raise RuntimeError("no lyric lines were produced")

        final_cues = cues.add_instrumental_cues(
            built, duration, audio_data, music_audio=music_data)
        qa_result = qa.evaluate(
            cues=final_cues,
            language=language,
            audio=audio_data,
            reference=reference_dict,
            reference_line_count=reference_line_count,
            text_source=text_source,
            timing_source=timing_source,
            language_decision=decision,
            lrclib_status=lookup.status if lookup else "disabled",
            lrclib_error=lookup.error if lookup else None,
        )
        offset_reset = (reference_dict.get("alignment_offset_reset")
                        if reference_dict else None)
        if offset_reset is not None:
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"reference timing offset {offset_reset:+.1f}s was rejected; "
                "line timings use LRCLIB/onset fallback")
        line_timing_sources = _timing_source_summary(final_cues)
        if line_timing_sources["asr_fallback_lines"]:
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"{len(line_timing_sources['asr_fallback_lines'])} line(s) use "
                "Whisper word timing because forced alignment failed")
        low_confidence_lines = (
            alignment_info.get("low_confidence_lines") if alignment_info else [])
        if low_confidence_lines:
            threshold = (alignment_info.get("score_threshold")
                         if alignment_info else None)
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"{len(low_confidence_lines)} line(s) fell below the forced-alignment "
                f"confidence threshold (score < {threshold}): "
                + ", ".join(str(index + 1) for index in low_confidence_lines))
        snapped_lines = (
            alignment_info.get("snapped_repeated_lines") if alignment_info else [])
        if snapped_lines:
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"{len(snapped_lines)} repeated line(s) were snapped to their "
                "offset-corrected LRCLIB occurrence: "
                + ", ".join(str(index + 1) for index in snapped_lines))
        fuzzy = lookup.artist_fuzzy_match if lookup else None
        if fuzzy:
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                "LRCLIB artist name was fuzzy-matched by title-only search: "
                f"{fuzzy.get('query')!r} -> {fuzzy.get('matched')!r}")
        if text_source == "asr" and dropped_asr_words:
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"{len(dropped_asr_words)} ASR word(s) were dropped for "
                "no vocal energy, low aligner confidence, or intro hallucination")
        if alignment_info and not reference_dict:
            if not line_timing_sources["counts"].get("forced-alignment"):
                timing_source = "asr"
        elif alignment_info:
            if line_timing_sources["counts"].get("lrclib-word-sync"):
                timing_source = "lrclib-word-sync"
            else:
                timing_source = "forced-alignment"

        chosen_id = None
        if chosen and chosen.get("record"):
            chosen_id = chosen["record"].get("id")
        if lookup is not None and "lrclib" in report["stages"]:
            report["stages"]["lrclib"]["requests"] = client.requests
        pipeline = {
            "version": "v2-i18n",
            "language": language,
            "language_confidence": round(float(decision.get("confidence", 0.0)), 4),
            "language_inputs": decision.get("inputs", {}),
            "text_source": text_source,
            "timing_source": timing_source,
            "romanization": (
                romanization_info.get("method")
                if romanization_info else None),
            "romanization_info": romanization_info,
            "needs_review": qa_result["needs_review"],
            "review_reasons": qa_result["review_reasons"],
            "lrclib": {
                "status": lookup.status if lookup else "disabled",
                "cached": lookup.cached if lookup else False,
                "error": lookup.error if lookup else None,
                "record_id": chosen_id,
                "candidate_count": len(lookup.records) if lookup else 0,
                "requests": client.requests,
                "artist_fuzzy_match": (
                    lookup.artist_fuzzy_match if lookup else None),
                "candidate_scores": [_candidate_summary(item) for item in candidates],
            },
            "reference_lines": qa_result["retention"]["reference_lines"],
            "reference_lines_present": qa_result["retention"]["reference_lines_present"],
            "line_retention": qa_result["retention"]["line_retention"],
            "line_level": qa_result["alignment"]["line_level"],
            "line_timing_sources": line_timing_sources,
            "alignment": {
                "coverage": round(qa_result["alignment"]["coverage"], 4),
                "inferred_words": qa_result["alignment"]["inferred_words"],
                "inferred_rate": round(qa_result["alignment"]["inferred_rate"], 4),
                "line_level_lines": qa_result["alignment"]["line_level_lines"],
                "line_level_rate": round(qa_result["alignment"]["line_level_rate"], 4),
                "forced": alignment_info is not None,
                "candidate": alignment_info,
                "low_confidence_lines": (
                    alignment_info.get("low_confidence_lines")
                    if alignment_info else []),
                "score_stats": ({
                    "threshold": alignment_info.get("score_threshold"),
                    "min": alignment_info.get("score_min"),
                    "p10": alignment_info.get("score_p10"),
                    "median": alignment_info.get("score_median"),
                    "mean": alignment_info.get("mean_score"),
                    "low_score_words": alignment_info.get("low_score_words"),
                } if alignment_info else None),
                "reference_lines": (_alignment_lines_summary(line_alignments)
                                    if line_alignments else []),
            },
            "asr": {
                "used": asr_used,
                "reason": asr_reason,
                "transcript_cache": (
                    report["stages"].get("asr") or {}).get("transcript_cache"),
                "dropped_words": dropped_asr_words,
                "stage": report["stages"].get("asr"),
            },
            "script": qa_result["script"],
            "sanity": qa_result["sanity"],
            "synthetic": qa_result["synthetic"],
            "line_start_deviation": qa_result["line_start_deviation"],
            "alignment_warnings": ([{
                "code": "reference_offset_rejected",
                "seconds": offset_reset,
            }] if offset_reset is not None else []),
            "vocal_stem": stem_info["path"],
            "vocal_stem_sha256": cache.sha256_file(vocals),
            "models": {**model_info, "aligner": aligner_info},
            "stage_seconds": report["stages"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "output_sha256": publish.lines_sha256(final_cues),
        }
        report["pipeline"] = pipeline
        report["qa"] = qa_result
        report["text_source"] = text_source
        report["timing_source"] = timing_source
        report["candidates"] = [_candidate_summary(item) for item in candidates]

        gate = _script_gate(final_cues, language)
        pipeline["script_gate"] = gate
        if _script_gate_blocks_publication(language, gate):
            qa_result["needs_review"] = True
            qa_result["review_reasons"].append(
                f"script gate found {len(gate['leaks'])} leaking line(s)")
            pipeline["needs_review"] = True
            pipeline["review_reasons"] = qa_result["review_reasons"]
            report["status"] = "blocked"
            report["error"] = (
                "script gate failed: active display text still contains "
                "Japanese/Korean script; the song was not written")
            _write_report(reports_dir, stem, report)
            return report

        publish_result = publish.write_outputs(
            stem, final_cues, duration, pipeline, output_dir,
            overwrite_edited=overwrite_edited, backup_dir=backups_dir)
        report["publish"] = {
            "written": publish_result.get("written"),
            "skipped": publish_result.get("skipped"),
            "reason": publish_result.get("reason"),
            "srt": _relative(publish_result["srt_path"]),
            "words": _relative(publish_result["words_path"]),
            "backups": publish_result.get("backups", []),
            "output_sha256": publish_result.get("output_sha256"),
        }
        report["status"] = "ok" if publish_result.get("written") else "skipped"
        report["stage_seconds_total"] = _seconds(song_started)
        _write_report(reports_dir, stem, report)
        lyric_count = sum(1 for cue in final_cues if not cue.get("instrumental"))
        instrumental_count = len(final_cues) - lyric_count
        if publish_result.get("written"):
            log.info("  wrote %s (%d lyric lines, %d instrumental cues, "
                     "language=%s, text=%s)", publish_result["srt_path"].name,
                     lyric_count, instrumental_count, language, text_source)
            log.info("  wrote %s", publish_result["words_path"].name)
            if publish_result.get("backups"):
                log.info("  backed up %d previous file(s)", len(publish_result["backups"]))
        else:
            log.warning("  skipped publish: %s", publish_result.get("reason"))
        return report
    except Exception as exc:
        if report.get("status") == "running":
            report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stage_seconds_total"] = _seconds(song_started)
        _write_report(reports_dir, stem, report)
        raise


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--song", help="only process stems containing this text")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR,
                        help="where vocals/transcripts/LRCLIB responses are cached")
    parser.add_argument("--output-dir", type=Path, default=LYRICS_DIR,
                        help="write lyric files here instead of lyrics/")
    parser.add_argument("--no-online", action="store_true",
                        help="do not use LRCLIB; use ASR or a user override")
    parser.add_argument("--force", action="store_true",
                        help="re-run Demucs and Whisper even when cached")
    parser.add_argument("--overwrite-edited", action="store_true",
                        help="replace lyric files that were edited outside the pipeline")
    parser.add_argument("--align-english", action="store_true",
                        help="use forced alignment for English songs instead of ASR timing")
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
    output_dir = args.output_dir.resolve()
    if args.missing:
        songs = [song for song in songs
                 if not (output_dir / f"{song[0]}.srt").exists()]
    if not songs:
        log.info("nothing to do; every matching song already has lyrics")
        return 0

    cache_dir = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = cache_dir / "reports"
    if output_dir.resolve() == LYRICS_DIR.resolve():
        backups_dir = cache_dir / "backups"
    else:
        backups_dir = output_dir / "backups"
    log.info("processing %d song(s); cache: %s; output: %s",
             len(songs), cache_dir, output_dir)

    failures = 0
    skipped = 0
    blocked = 0
    for stem, original, karaoke in songs:
        try:
            report = process_song(
                stem, original, karaoke, cache_dir,
                use_online=not args.no_online, force=args.force,
                output_dir=output_dir, overwrite_edited=args.overwrite_edited,
                reports_dir=reports_dir, backups_dir=backups_dir,
                source_dir=SOURCE_DIR, align_english=args.align_english)
            if report.get("status") == "skipped":
                skipped += 1
            elif report.get("status") == "blocked":
                blocked += 1
        except Exception as exc:
            failures += 1
            log.exception("FAILED: %s: %s", stem, exc)
    log.info("")
    log.info("done: %d ok, %d failed, %d skipped, %d blocked",
             len(songs) - failures - skipped - blocked, failures, skipped, blocked)
    return 1 if failures or blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
