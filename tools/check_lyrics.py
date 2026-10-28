"""Check generated lyric sidecars and SRTs without re-running the audio pipeline.

Usage (from the project root):
    python tools/check_lyrics.py                  # all songs
    python tools/check_lyrics.py --song Innocent  # one song (substring)
    python tools/check_lyrics.py --energy         # also inspect vocal energy

Structural problems make the command exit non-zero. Energy findings are
diagnostic warnings: backing vocals, ad-libs, and bleed can all trip them.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LYRICS_DIR = ROOT / "lyrics"
VOCAL_STEMS = ROOT / "tools" / ".cache" / "stems" / "htdemucs"
SAMPLE_RATE = 16000
ONSET_RMS = 0.003
QUIET_RMS = 0.002

SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def normalize(text: str) -> str:
    return " ".join(str(text).split())


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    cues = []
    text = path.read_text(encoding="utf-8-sig")
    for block in re.split(r"\r?\n\r?\n", text.strip()):
        rows = [row for row in block.splitlines() if row.strip()]
        if len(rows) < 3:
            continue
        offset = 1 if rows[0].strip().isdigit() else 0
        if offset >= len(rows):
            continue
        match = SRT_TIME.fullmatch(rows[offset].strip())
        if not match:
            continue
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        cues.append((start, end, " ".join(rows[offset + 1:])))
    return cues


def check_sidecar(stem: str):
    problems: list[str] = []
    json_path = LYRICS_DIR / f"{stem}.words.json"
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read JSON: {exc}"], [], 0, 0, 0

    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        return ["no cue lines"], [], 0, 0, 0

    lyric_count = 0
    instrumental_count = 0
    word_count = 0
    previous_end = -1.0

    for index, line in enumerate(lines, 1):
        try:
            start = float(line["start"])
            end = float(line["end"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"cue {index}: missing or invalid start/end")
            continue
        if start < previous_end - 0.001:
            problems.append(
                f"cue {index}: overlaps previous cue ({start:.3f} < {previous_end:.3f})"
            )
        if end <= start:
            problems.append(f"cue {index}: non-positive cue ({start:.3f}->{end:.3f})")
        previous_end = max(previous_end, end)

        text = str(line.get("text", ""))
        instrumental = bool(line.get("instrumental"))
        words = line.get("words") or []
        word_count += len(words)

        if instrumental:
            instrumental_count += 1
            if text != "[Instrumental]":
                problems.append(f"cue {index}: instrumental text is {text!r}")
            if words:
                problems.append(f"cue {index}: instrumental cue has words")
            continue

        lyric_count += 1
        if not words:
            problems.append(f"cue {index}: lyric cue has no words: {text!r}")
            continue

        previous_word_end = None
        for word in words:
            try:
                word_start = float(word["start"])
                word_end = float(word["end"])
            except (KeyError, TypeError, ValueError):
                problems.append(f"cue {index}: word has invalid start/end")
                continue
            if word_end <= word_start:
                problems.append(
                    f"cue {index}: word {word.get('text', '')!r} is non-positive"
                )
            if word_start < start - 0.001:
                problems.append(
                    f"cue {index}: word {word.get('text', '')!r} starts before cue"
                )
            if word_end > end + 0.001:
                problems.append(
                    f"cue {index}: word {word.get('text', '')!r} ends after cue"
                )
            if previous_word_end is not None and word_start < previous_word_end - 0.001:
                problems.append(
                    f"cue {index}: word {word.get('text', '')!r} overlaps previous word"
                )
            previous_word_end = word_end

        joined = normalize(" ".join(str(word.get("text", "")) for word in words))
        if joined != normalize(text):
            problems.append(
                f"cue {index}: text/word mismatch ({text!r} vs {joined!r})"
            )

    srt_path = LYRICS_DIR / f"{stem}.srt"
    if not srt_path.exists():
        problems.append("missing matching .srt file")
    else:
        srt_cues = parse_srt(srt_path)
        if len(srt_cues) != len(lines):
            problems.append(
                f"SRT has {len(srt_cues)} cues but JSON has {len(lines)}"
            )
        else:
            for index, (srt_cue, line) in enumerate(zip(srt_cues, lines), 1):
                srt_start, srt_end, srt_text = srt_cue
                if abs(srt_start - float(line["start"])) > 0.002 or abs(
                    srt_end - float(line["end"])
                ) > 0.002:
                    problems.append(f"cue {index}: SRT and JSON timestamps differ")
                if normalize(srt_text) != normalize(line.get("text", "")):
                    problems.append(f"cue {index}: SRT and JSON text differ")

    return problems, lines, lyric_count, instrumental_count, word_count


def _rms_windows(audio, start: float, end: float):
    import numpy as np

    first = max(0, int(start * SAMPLE_RATE))
    last = min(len(audio), int(end * SAMPLE_RATE))
    if last <= first:
        return np.array([0.0])
    samples = np.square(audio[first:last], dtype=np.float64)
    width = max(1, int(0.05 * SAMPLE_RATE))
    hop = max(1, int(0.025 * SAMPLE_RATE))
    if len(samples) <= width:
        return np.array([np.sqrt(np.mean(samples))])
    cumulative = np.concatenate(([0.0], np.cumsum(samples)))
    starts = np.arange(0, len(samples) - width + 1, hop)
    energies = (cumulative[starts + width] - cumulative[starts]) / width
    return np.sqrt(np.maximum(energies, 0.0))


def check_energy(stem: str, lines: list[dict]) -> list[str]:
    try:
        import numpy as np
    except ImportError:
        return ["numpy is not installed; energy check skipped"]

    vocal_path = VOCAL_STEMS / stem / "vocals.wav"
    if not vocal_path.exists():
        return [f"no cached vocal stem: {vocal_path.relative_to(ROOT)}"]

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(vocal_path), "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return ["ffmpeg could not decode the vocal stem"]
    audio = np.frombuffer(proc.stdout, dtype=np.float32)

    warnings: list[str] = []
    for index, line in enumerate(lines, 1):
        values = _rms_windows(audio, float(line["start"]), float(line["end"]))
        peak = float(values.max()) if len(values) else 0.0
        onset = None
        for offset, value in enumerate(values):
            if value >= ONSET_RMS:
                onset = float(line["start"]) + offset * 0.025
                break

        if line.get("instrumental"):
            if peak > 0.02:
                warnings.append(
                    f"cue {index:2d} {line['start']:7.2f}-{line['end']:7.2f} "
                    f"[Instrumental] has vocal energy peak={peak:.4f}"
                )
        else:
            if peak < QUIET_RMS:
                warnings.append(
                    f"cue {index:2d} {line['start']:7.2f}-{line['end']:7.2f} "
                    f"lyric is silent peak={peak:.4f} | {line.get('text', '')}"
                )
            elif onset is not None and onset - float(line["start"]) > 1.2:
                warnings.append(
                    f"cue {index:2d} {line['start']:7.2f}-{line['end']:7.2f} "
                    f"vocal starts +{onset - float(line['start']):.2f}s late | "
                    f"{line.get('text', '')}"
                )
    return warnings


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--song", action="append", default=[], metavar="TEXT",
                        help="only check stems containing TEXT (repeatable)")
    parser.add_argument("--energy", action="store_true",
                        help="inspect cached vocal stems for quiet/late cues")
    parser.add_argument("--verbose", action="store_true",
                        help="show the first and last three cues")
    args = parser.parse_args(argv)

    paths = sorted(LYRICS_DIR.glob("*.words.json"))
    if args.song:
        paths = [path for path in paths
                 if any(query.lower() in path.name.lower() for query in args.song)]
    if not paths:
        print("no matching lyrics/*.words.json files")
        return 1

    total_problems = 0
    for path in paths:
        stem = path.name.removesuffix(".words.json")
        problems, lines, lyric_count, instrumental_count, word_count = check_sidecar(stem)
        total_problems += len(problems)
        print(
            f"{stem}: {len(lines)} cues "
            f"({lyric_count} lyric, {instrumental_count} instrumental), "
            f"{word_count} words, {len(problems)} structural problem(s)"
        )
        for problem in problems[:20]:
            print(f"  PROBLEM: {problem}")
        if len(problems) > 20:
            print(f"  ... {len(problems) - 20} more problem(s)")

        if args.energy and lines and not problems:
            warnings = check_energy(stem, lines)
            print(f"  energy: {len(warnings)} warning(s)")
            for warning in warnings[:8]:
                print(f"    {warning}")
            if len(warnings) > 8:
                print(f"    ... {len(warnings) - 8} more warning(s)")

        if args.verbose and lines:
            for index in list(range(1, min(4, len(lines) + 1))) + list(
                range(max(4, len(lines) - 2), len(lines) + 1)
            ):
                line = lines[index - 1]
                print(
                    f"  {index:2d} {line['start']:7.2f}-{line['end']:7.2f} | "
                    f"{line['text']}"
                )

    print(f"\ntotal structural problems: {total_problems}")
    return 1 if total_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
