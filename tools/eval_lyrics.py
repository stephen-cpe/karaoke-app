"""Evaluate generated lyric sidecars against the section 7 metrics.

Read-only. Works on the active lyrics directory or on a shadow-run output
directory. It does not need the Whisper/aligner models; optional energy checks
reuse cached vocal stems.

Usage (from the project root):
    python tools/eval_lyrics.py
    python tools/eval_lyrics.py --lyrics-dir tools/.cache/baseline-shadow
    python tools/eval_lyrics.py --song 00053 --energy
    python tools/eval_lyrics.py --json > tools/.cache/reports/baseline.json
    python tools/eval_lyrics.py --compare-dir lyrics --lyrics-dir tools/.cache/shadow
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_lyrics import check_sidecar  # noqa: E402
from tools.generate_lyrics import _max_window_rms, load_vocal_audio  # noqa: E402

DEFAULT_LYRICS_DIR = ROOT / "lyrics"
DEFAULT_CACHE_DIR = ROOT / "tools" / ".cache"
DEFAULT_REPORTS_DIR = DEFAULT_CACHE_DIR / "reports"
SAMPLE_RATE = 16000
HELD_NOTE_RMS = 0.003
SYNTHETIC_TOLERANCE = 0.020
MIN_SYNTHETIC_WORDS = 2

KANA = re.compile(r"[\u3040-\u30ff]")
KANJI = re.compile(r"[\u3400-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")


def legacy_norm_token(text: str) -> str:
    """The old ASCII-only normalizer, kept here to detect old timing."""
    text = text.lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


def sanitize_problem(problem: str) -> str:
    """Keep the diagnostic and cue number, but redact quoted lyric text."""
    text = str(problem)
    text = re.sub(r"'[^']*'", "'...'", text)
    text = re.sub(r'"[^"]*"', '"..."', text)
    return text


def character_proportional_words(words: list[dict[str, Any]],
                                 start: float, end: float) -> list[dict[str, float]]:
    """Reproduce the old distribute_words() output for detector comparisons."""
    if not words:
        return []
    if end <= start:
        end = start + 0.5
    weights = [max(1, len(legacy_norm_token(str(word.get("text", ""))))) for word in words]
    total = sum(weights)
    cursor = start
    result = []
    for index, (word, weight) in enumerate(zip(words, weights)):
        share = (end - start) * weight / total
        word_start = cursor
        word_end = end if index == len(words) - 1 else cursor + share
        if word_end <= word_start:
            word_end = word_start + 0.12
        result.append({"start": word_start, "end": word_end})
        cursor = word_end
    return result


def count_script(text: str) -> int:
    return sum(1 for char in text
               if KANA.match(char) or KANJI.match(char) or HANGUL.match(char))


def resolve_vocal_stem(stem: str, cache_dir: Path,
                       pipeline: dict[str, Any]) -> Path | None:
    recorded = pipeline.get("vocal_stem")
    if isinstance(recorded, str) and recorded:
        path = Path(recorded)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            return path
    legacy = cache_dir / "stems" / "htdemucs" / stem / "vocals.wav"
    if legacy.is_file():
        return legacy
    matches = list((cache_dir / "stems").glob(f"*/htdemucs/{stem}/vocals.wav"))
    return matches[0] if matches else None


def read_report(reports_dir: Path, stem: str) -> dict[str, Any]:
    path = reports_dir / f"{stem}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def line_token_count(line: dict[str, Any]) -> int:
    words = line.get("words") or []
    if words:
        return len(words)
    text = str(line.get("text") or "")
    return max(1, len(text.split()))


def evaluate_song(stem: str, lyrics_dir: Path, cache_dir: Path,
                  reports_dir: Path, energy: bool) -> dict[str, Any]:
    data_path = lyrics_dir / f"{stem}.words.json"
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"stem": stem, "error": f"cannot read JSON: {exc}"}
    if not isinstance(data, dict):
        return {"stem": stem, "error": "JSON root is not an object"}

    lines = data.get("lines")
    if not isinstance(lines, list):
        lines = []
    problems, _checked_lines, lyric_count, instrumental_count, word_count = (
        check_sidecar(stem, lyrics_dir))
    pipeline = data.get("pipeline") if isinstance(data.get("pipeline"), dict) else {}
    language = pipeline.get("language") if isinstance(pipeline.get("language"), str) else None
    report = read_report(reports_dir, stem)

    total_tokens = 0
    aligned_tokens = 0
    line_level_lines = 0
    inferred_words = 0
    synthetic_lines = 0
    unmarked_synthetic_words = 0
    short_words = 0
    long_words = 0
    long_words_supported = 0
    script_issues = 0
    script_lines = 0
    line_details: list[dict[str, Any]] = []

    vocal_audio = None
    vocal_path = resolve_vocal_stem(stem, cache_dir, pipeline) if energy else None
    if vocal_path is not None:
        vocal_audio = load_vocal_audio(vocal_path)

    for cue_index, line in enumerate(lines, 1):
        if not isinstance(line, dict) or line.get("instrumental"):
            continue
        text = str(line.get("text") or "")
        words = line.get("words") or []
        tokens = line_token_count(line)
        total_tokens += tokens
        aligned_tokens += len(words) if words else 0
        line_script = count_script(text) > 0 or any(
            count_script(str(word.get("text", ""))) for word in words)
        if not words:
            line_level_lines += 1
            if language in ("ja", "ko") and count_script(text):
                script_issues += 1
            line_details.append({
                "index": cue_index,
                "start": round(float(line.get("start", 0.0)), 3),
                "end": round(float(line.get("end", 0.0)), 3),
                "words": 0,
                "tokens": tokens,
                "coverage": 0.0,
                "line_level": True,
                "inferred": 0,
                "synthetic": False,
                "script": line_script,
            })
            continue
        inferred_words += sum(1 for word in words if word.get("inferred"))
        if language in ("ja", "ko"):
            if line_script:
                script_issues += 1
        elif count_script(text):
            script_lines += 1

        durations = [float(word.get("end", 0.0)) - float(word.get("start", 0.0))
                     for word in words]
        line_short = sum(1 for duration in durations if duration < 0.04)
        short_words += line_short
        line_long = 0
        for word, duration in zip(words, durations):
            if duration <= 3.0:
                continue
            long_words += 1
            line_long += 1
            if vocal_audio is not None:
                rms = _max_window_rms(
                    vocal_audio, SAMPLE_RATE,
                    float(word.get("start", 0.0)), float(word.get("end", 0.0)))
                if rms >= HELD_NOTE_RMS:
                    long_words_supported += 1

        is_synthetic = False
        if len(words) >= MIN_SYNTHETIC_WORDS and all(
                legacy_norm_token(str(word.get("text", ""))) for word in words):
            spread = character_proportional_words(
                words, float(line["start"]), float(line["end"]))
            if len(spread) == len(words) and all(
                    abs((float(word["end"]) - float(word["start"]))
                        - (item["end"] - item["start"])) <= SYNTHETIC_TOLERANCE
                    for word, item in zip(words, spread)):
                is_synthetic = True
                synthetic_lines += 1
                unmarked_synthetic_words += sum(
                    1 for word in words if not word.get("inferred"))

        line_details.append({
            "index": cue_index,
            "start": round(float(line.get("start", 0.0)), 3),
            "end": round(float(line.get("end", 0.0)), 3),
            "words": len(words),
            "tokens": tokens,
            "coverage": len(words) / tokens if tokens else 0.0,
            "line_level": False,
            "inferred": sum(1 for word in words if word.get("inferred")),
            "synthetic": is_synthetic,
            "script": line_script,
            "short_words": line_short,
            "long_words": line_long,
        })

    coverage = aligned_tokens / total_tokens if total_tokens else 0.0
    inferred_rate = inferred_words / word_count if word_count else 0.0

    deviation = report.get("qa", {}).get("line_start_deviation", {})
    if not isinstance(deviation, dict):
        deviation = {}
    needs_review = pipeline.get("needs_review")
    reasons = pipeline.get("review_reasons") if isinstance(
        pipeline.get("review_reasons"), list) else []
    lrclib = pipeline.get("lrclib") if isinstance(pipeline.get("lrclib"), dict) else {}

    return {
        "stem": stem,
        "language": language,
        "language_confidence": pipeline.get("language_confidence"),
        "text_source": pipeline.get("text_source"),
        "timing_source": pipeline.get("timing_source"),
        "lines": len(lines),
        "lyric_lines": lyric_count,
        "instrumental_lines": instrumental_count,
        "words": word_count,
        "aligned_tokens": aligned_tokens,
        "total_tokens": total_tokens,
        "line_level_lines": line_level_lines,
        "coverage": coverage,
        "inferred_words": inferred_words,
        "inferred_rate": inferred_rate,
        "synthetic_lines": synthetic_lines,
        "unmarked_synthetic_words": unmarked_synthetic_words,
        "script_issues": script_issues,
        "script_lines_outside_ja_ko": script_lines,
        "short_words": short_words,
        "long_words": long_words,
        "long_words_supported_by_energy": (
            long_words_supported if vocal_audio is not None else None),
        "structural_problems": [sanitize_problem(problem) for problem in problems],
        "line_details": line_details,
        "line_start_deviation_median": deviation.get("median"),
        "line_start_deviation_p90": deviation.get("p90"),
        "lrclib_status": lrclib.get("status"),
        "needs_review": needs_review,
        "review_reasons": reasons,
    }


def compare_dirs(left: Path, right: Path, stems: list[str]) -> dict[str, Any]:
    comparison: dict[str, Any] = {"songs": [], "totals": {
        "text_matches": 0, "text_diffs": 0, "timing_diffs": 0,
        "missing_left": 0, "missing_right": 0, "line_count_diffs": 0,
    }}
    for stem in stems:
        left_path = left / f"{stem}.words.json"
        right_path = right / f"{stem}.words.json"
        if not left_path.is_file() or not right_path.is_file():
            comparison["totals"]["missing_left"] += int(not left_path.is_file())
            comparison["totals"]["missing_right"] += int(not right_path.is_file())
            comparison["songs"].append({
                "stem": stem, "missing_left": not left_path.is_file(),
                "missing_right": not right_path.is_file(),
            })
            continue
        try:
            left_data = json.loads(left_path.read_text(encoding="utf-8"))
            right_data = json.loads(right_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            comparison["songs"].append({"stem": stem, "error": "cannot read JSON"})
            continue
        left_lines = left_data.get("lines") or []
        right_lines = right_data.get("lines") or []
        text_diffs = []
        timing_diffs = []
        text_matches = 0
        for index, (a, b) in enumerate(zip(left_lines, right_lines), 1):
            a_text = " ".join(str(a.get("text") or "").split())
            b_text = " ".join(str(b.get("text") or "").split())
            if a_text == b_text:
                text_matches += 1
            else:
                text_diffs.append(index)
            if (a_text == b_text
                    and (abs(float(a.get("start", 0)) - float(b.get("start", 0))) > 0.01
                         or abs(float(a.get("end", 0)) - float(b.get("end", 0))) > 0.01)):
                timing_diffs.append(index)
        comparison["songs"].append({
            "stem": stem,
            "left_lines": len(left_lines),
            "right_lines": len(right_lines),
            "text_matches": text_matches,
            "text_diffs": text_diffs,
            "timing_diffs": timing_diffs,
        })
        total = comparison["totals"]
        total["text_matches"] += text_matches
        total["text_diffs"] += len(text_diffs)
        total["timing_diffs"] += len(timing_diffs)
        total["line_count_diffs"] += int(len(left_lines) != len(right_lines))
    return comparison


def aggregate_group(songs: list[dict[str, Any]]) -> dict[str, Any]:
    values = [song for song in songs if not song.get("error")]
    total_tokens = sum(song.get("total_tokens", 0) for song in values)
    aligned_tokens = sum(song.get("aligned_tokens", 0) for song in values)
    total_words = sum(song.get("words", 0) for song in values)
    inferred = sum(song.get("inferred_words", 0) for song in values)
    return {
        "songs": len(values),
        "lyric_lines": sum(song.get("lyric_lines", 0) for song in values),
        "words": total_words,
        "line_level_lines": sum(song.get("line_level_lines", 0) for song in values),
        "line_level_rate": (
            sum(song.get("line_level_lines", 0) for song in values)
            / sum(song.get("lyric_lines", 0) for song in values)
            if sum(song.get("lyric_lines", 0) for song in values) else 0.0),
        "coverage": aligned_tokens / total_tokens if total_tokens else 0.0,
        "inferred_words": inferred,
        "inferred_rate": inferred / total_words if total_words else 0.0,
        "synthetic_lines": sum(song.get("synthetic_lines", 0) for song in values),
        "unmarked_synthetic_words": sum(
            song.get("unmarked_synthetic_words", 0) for song in values),
        "script_lines": sum(song.get("script_issues", 0) for song in values),
        "structural_problem_songs": sum(
            1 for song in values if song.get("structural_problems")),
        "structural_problems": sum(
            len(song.get("structural_problems") or []) for song in values),
        "short_words": sum(song.get("short_words", 0) for song in values),
        "long_words": sum(song.get("long_words", 0) for song in values),
        "long_words_supported_by_energy": sum(
            song.get("long_words_supported_by_energy") or 0 for song in values),
        "long_words_energy_checked": sum(
            1 for song in values
            if song.get("long_words_supported_by_energy") is not None),
        "needs_review": sum(1 for song in values if song.get("needs_review")),
    }


def print_table(songs: list[dict[str, Any]]) -> None:
    header = ("stem", "lang", "lines", "lyr", "instr", "words", "line-lvl",
              "cov%", "inf%", "synth", "script", "<40ms", ">3s", "review")
    print(" | ".join(header))
    for song in songs:
        if song.get("error"):
            print(f"{song['stem']} | ERROR: {song['error']}")
            continue
        print(" | ".join([
            song["stem"],
            song.get("language") or "?",
            str(song["lines"]),
            str(song["lyric_lines"]),
            str(song["instrumental_lines"]),
            str(song["words"]),
            str(song["line_level_lines"]),
            f"{song['coverage'] * 100:.1f}",
            f"{song['inferred_rate'] * 100:.1f}",
            str(song["synthetic_lines"]),
            str(song["script_issues"]),
            str(song["short_words"]),
            str(song["long_words"]),
            "yes" if song.get("needs_review") else "no",
        ]))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lyrics-dir", type=Path, default=DEFAULT_LYRICS_DIR,
                        help=f"lyrics directory to evaluate (default: {DEFAULT_LYRICS_DIR})")
    parser.add_argument("--song", action="append", default=[], metavar="TEXT",
                        help="only evaluate stems containing TEXT (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--energy", action="store_true",
                        help="check long words against cached vocal-stem energy")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help=f"tool cache directory (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR,
                        help=f"per-song report directory (default: {DEFAULT_REPORTS_DIR})")
    parser.add_argument("--compare-dir", type=Path, default=None,
                        help="compare line text/timing with another lyrics directory")
    args = parser.parse_args(argv)

    lyrics_dir = args.lyrics_dir.resolve()
    if not lyrics_dir.is_dir():
        print(f"no such lyrics directory: {lyrics_dir}")
        return 1
    stems = sorted(path.name.removesuffix(".words.json")
                   for path in lyrics_dir.glob("*.words.json"))
    if args.song:
        stems = [stem for stem in stems
                 if any(query.casefold() in stem.casefold() for query in args.song)]
    if not stems:
        print(f"no matching .words.json files in {lyrics_dir}")
        return 1

    songs = [
        evaluate_song(stem, lyrics_dir, args.cache_dir.resolve(),
                      args.reports_dir.resolve(), args.energy)
        for stem in stems
    ]
    totals = aggregate_group(songs)
    languages = {
        language: aggregate_group([
            song for song in songs
            if (song.get("language") or "unknown") == language
        ])
        for language in sorted({song.get("language") or "unknown" for song in songs})
    }
    payload: dict[str, Any] = {
        "lyrics_dir": str(lyrics_dir),
        "songs": songs,
        "languages": languages,
        "totals": totals,
    }
    if args.compare_dir is not None:
        payload["comparison"] = compare_dirs(
            args.compare_dir.resolve(), lyrics_dir, stems)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"lyrics: {lyrics_dir}")
    print_table(songs)
    print("\nper language:")
    for language, values in languages.items():
        print(f"  {language}: {values['songs']} song(s), "
              f"{values['lyric_lines']} lyric lines, {values['words']} words, "
              f"coverage {values['coverage'] * 100:.1f}%, "
              f"inferred {values['inferred_rate'] * 100:.1f}%, "
              f"line-level {values['line_level_lines']}, "
              f"synthetic {values['synthetic_lines']}, "
              f"needs-review {values['needs_review']}")
    if "comparison" in payload:
        comparison = payload["comparison"]
        print("\ncomparison:")
        for song in comparison["songs"]:
            if song.get("missing_left") or song.get("missing_right"):
                print(f"  {song['stem']}: missing "
                      f"left={song.get('missing_left')} right={song.get('missing_right')}")
                continue
            if song.get("error"):
                print(f"  {song['stem']}: {song['error']}")
                continue
            print(f"  {song['stem']}: text matches {song['text_matches']}, "
                  f"text diffs {len(song['text_diffs'])}, "
                  f"timing diffs {len(song['timing_diffs'])}, "
                  f"lines {song['left_lines']} vs {song['right_lines']}")
        totals_compare = comparison["totals"]
        print(f"  total: text matches {totals_compare['text_matches']}, "
              f"text diffs {totals_compare['text_diffs']}, "
              f"timing diffs {totals_compare['timing_diffs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
