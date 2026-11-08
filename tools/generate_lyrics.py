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

from tools.lyrics import asr, audio, cache, cues, langdetect, publish, qa, reference, textnorm  # noqa: E402

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

# Compatibility exports for tests and older helper scripts.
atomic_write_text = cache.atomic_write_text
srt_time = cues.srt_time
norm_token = textnorm.norm_token
clean_text = textnorm.clean_text
split_display_tokens = textnorm.split_display_tokens
parse_lrc = reference.parse_lrc
chunk_ranges = asr.chunk_ranges
load_vocal_audio = audio.load_vocal_audio
_window_rms = audio.window_rms
_max_window_rms = audio.max_window_rms
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
robust_line_anchors = cues.robust_line_anchors
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


def process_song(stem: str, original: Path, karaoke: Path, cache_dir: Path,
                 *, use_online: bool, force: bool, output_dir: Path,
                 overwrite_edited: bool, reports_dir: Path,
                 backups_dir: Path, source_dir: Path) -> dict[str, Any]:
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

        override = reference.user_override(source_dir, stem)
        if override:
            log.info("  user override: %s", override.get("path"))
            report["user_override"] = override.get("path")

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

            prompt = asr.build_prompt(
                title, artist, language,
                reference_text if reference_text else None)
            started = time.perf_counter()
            transcript, cache_info = asr.transcribe_cached(
                stem, vocals, stem_info["original_sha256"], language, prompt,
                cache_dir, force=force, session=session)
            report["stages"]["asr"] = {
                **cache_info,
                "seconds": cache_info.get("seconds", _seconds(started)),
                "language": language,
                "prompt_chars": len(prompt),
                "words": sum(len(segment.get("words") or [])
                             for segment in transcript.get("segments") or []),
            }
            model_info = {
                "model": asr.MODEL_NAME,
                "device": session.device,
                "compute_type": session.compute_type,
                "load_seconds": round(session.load_seconds, 2),
                "fallback_used": session.fallback_used,
            }
            if cache_info.get("cache_hit"):
                log.info("  transcript cache hit: %s", cache_info["path"])
            else:
                log.info("  transcribed in %.1fs", report["stages"]["asr"]["seconds"])
        finally:
            session.close()

        transcript = asr.strip_hallucinations(transcript)
        transcript = asr.filter_silent_words(transcript, vocals, audio_data)
        local = asr.local_words(transcript, language)
        report["stages"]["asr"]["local_words"] = len(local)

        chosen = None
        candidates: list[dict[str, Any]] = []
        reference_dict: dict[str, Any] | None = None
        reference_line_count = 0
        text_source = "asr"
        timing_source = "asr"

        if override and override.get("lines"):
            reference_dict = reference.reference_from_override(
                override, language, duration, local)
            reference_dict["local"] = local
            cues.align_reference_words(reference_dict, local, duration, language)
            _reject_suspect_reference_offset(reference_dict, report)
            reference_line_count = _reference_line_count(reference_dict, language)
            built = cues.build_lyric_lines(
                reference_dict, duration, audio_data, language)
            text_source = "user"
        elif lookup and lookup.records:
            chosen, candidates = reference.choose_candidate(
                lookup.records, language, duration, audio_data, local=local,
                fetch_full=client.fetch_by_id)
            if chosen:
                reference_dict = reference.reference_from_candidate(chosen)
                reference_dict["local"] = local
                cues.align_reference_words(reference_dict, local, duration, language)
                _reject_suspect_reference_offset(reference_dict, report)
                reference_line_count = _reference_line_count(reference_dict, language)
                built = cues.build_lyric_lines(
                    reference_dict, duration, audio_data, language)
                text_source = reference_dict.get("text_source", "lrclib-plain")
                timing_source = "asr"
            else:
                built = cues.build_from_transcript(transcript, duration, language)
                text_source = "asr"
                timing_source = "asr"
        else:
            built = cues.build_from_transcript(transcript, duration, language)
            text_source = "asr"
            timing_source = "asr"

        if not built:
            raise RuntimeError("no lyric lines were produced")

        final_cues = cues.add_instrumental_cues(built, duration, audio_data)
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
            "romanization": "reference-romaji" if decision.get("reference_romaji") else None,
            "needs_review": qa_result["needs_review"],
            "review_reasons": qa_result["review_reasons"],
            "lrclib": {
                "status": lookup.status if lookup else "disabled",
                "cached": lookup.cached if lookup else False,
                "error": lookup.error if lookup else None,
                "record_id": chosen_id,
                "candidate_count": len(lookup.records) if lookup else 0,
                "requests": client.requests,
                "candidate_scores": [_candidate_summary(item) for item in candidates],
            },
            "reference_lines": qa_result["retention"]["reference_lines"],
            "reference_lines_present": qa_result["retention"]["reference_lines_present"],
            "line_retention": qa_result["retention"]["line_retention"],
            "line_level": qa_result["alignment"]["line_level"],
            "alignment": {
                "coverage": round(qa_result["alignment"]["coverage"], 4),
                "inferred_words": qa_result["alignment"]["inferred_words"],
                "inferred_rate": round(qa_result["alignment"]["inferred_rate"], 4),
                "line_level_lines": qa_result["alignment"]["line_level_lines"],
                "line_level_rate": round(qa_result["alignment"]["line_level_rate"], 4),
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
            "models": model_info,
            "stage_seconds": report["stages"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "output_sha256": publish.lines_sha256(final_cues),
        }
        report["pipeline"] = pipeline
        report["qa"] = qa_result
        report["text_source"] = text_source
        report["timing_source"] = timing_source
        report["candidates"] = [_candidate_summary(item) for item in candidates]

        if language in ("ja", "ko") and output_dir.resolve() == LYRICS_DIR.resolve():
            report["status"] = "blocked"
            report["error"] = (
                "JA/KO romanization is not implemented until Phase 3; "
                "use --output-dir for a shadow run")
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
    backups_dir = cache_dir / "backups"
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
                source_dir=SOURCE_DIR)
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
