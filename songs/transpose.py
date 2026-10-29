import argparse
import concurrent.futures as futures
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_FFMPEG_FILTER = "rubberband=pitch={pitch}"
FFMPEG_TIMEOUT_SECONDS = 300


def _run_ffmpeg(src, dst, semitones, bitrate):
    """Encode one transposed file atomically. Worker-safe."""
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)

    pitch = 2 ** (semitones / 12.0)
    template = os.environ.get("KARAOKE_FFMPEG_FILTER", DEFAULT_FFMPEG_FILTER)
    try:
        audio_filter = template.format(pitch=pitch)
    except (KeyError, IndexError, ValueError) as exc:
        return (False, "Invalid KARAOKE_FFMPEG_FILTER template: %s" % exc)
    fd, tmp = tempfile.mkstemp(prefix=".transpose-", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-y",
            "-i", src,
            "-vn", "-ar", "44100",
            "-filter:a", audio_filter,
            "-b:a", bitrate,
            "-f", "mp3",
            tmp,
        ]
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if res.returncode != 0:
            return (False, str(res.stderr.decode("utf-8", errors="replace"))[:800])
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            return (False, "ffmpeg produced no output.")
        os.replace(tmp, dst)
        return (True, "")
    except Exception as exc:  # pylint: disable=broad-except
        return (False, str(exc))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _parse_semitones(text):
    items = [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]
    try:
        values = [int(x) for x in items]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Invalid semitone list '{0}': {1}".format(text, exc)
        ) from exc
    if not values:
        raise argparse.ArgumentTypeError("Semitone list must not be empty.")
    return values


def transpose_songs():
    songs_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(songs_dir)
    default_out = os.path.join(parent_dir, "transposed")

    parser = argparse.ArgumentParser(
        description="Pitch-shift every song by the given semitone offsets."
    )
    parser.add_argument("--output", "-o", default=default_out,
                        help="Output directory (default: {0})".format(default_out))
    parser.add_argument("--bitrate", "-b", default="192k",
                        help="Output MP3 bitrate (default: 192k).")
    parser.add_argument("--semitones", "-s", default="-4,-3,-2,-1,1,2,3,4",
                        help="Comma separated semitone offsets.",
                        type=_parse_semitones)
    parser.add_argument("--force", "-f", action="store_true",
                        help="Re-transpose even if destination is up to date.")
    parser.add_argument("--jobs", "-j", type=int,
                        default=min(4, os.cpu_count() or 1),
                        help="Concurrent ffmpeg processes (default: min(4, cpu_count)).")
    parser.add_argument(
        "--incremental", action="store_true",
        help="Also re-transpose if the source file is newer than the destination.",
    )
    parser.add_argument(
        "--wait", action="store_true",
        help="Wait for Enter before exiting (useful for double-click runs).",
    )
    args = parser.parse_args()

    transposed_dir = os.path.abspath(args.output)
    try:
        output_inside_songs = os.path.commonpath([transposed_dir, songs_dir]) == songs_dir
    except ValueError:
        output_inside_songs = False
    if output_inside_songs:
        print("Error: --output must not be inside the songs source directory.")
        return 2
    semitones_list = args.semitones
    target_bitrate = args.bitrate

    print("Source Directory (songs): {0}".format(songs_dir))
    print("Target Directory (transposed): {0}".format(transposed_dir))
    print("Semitones: {0}".format(semitones_list))
    print("Bitrate: {0}".format(target_bitrate))
    print("-" * 50)

    os.makedirs(transposed_dir, exist_ok=True)

    try:
        files_in_dir = sorted(os.listdir(songs_dir))
    except FileNotFoundError:
        print("Error: The source directory '{0}' was not found.".format(songs_dir))
        return 1

    songs = [
        f for f in files_in_dir
        if f.lower().endswith((".mp3", ".wav"))
        and not re.match(r"^[+-]\d+_", f)
        and os.path.isfile(os.path.join(songs_dir, f))
    ]
    # Avoid two sources with the same stem (for example foo.wav and foo.mp3)
    # racing to write the same transposed cache file.
    songs.sort(key=lambda f: (os.path.splitext(f)[0].lower(), 0 if f.lower().endswith(".mp3") else 1))
    deduped = []
    seen_stems = set()
    for filename in songs:
        stem = os.path.splitext(filename)[0].lower()
        if stem in seen_stems:
            print("Skipping duplicate stem: {0}".format(filename))
            continue
        seen_stems.add(stem)
        deduped.append(filename)
    songs = deduped

    if not songs:
        print("No .mp3/.wav files found in '{0}'.".format(songs_dir))
        return 0

    tasks = []
    skipped = 0
    for filename in songs:
        src = os.path.join(songs_dir, filename)
        for semitones in semitones_list:
            # The encoder always writes MP3, so the cache name must be .mp3
            # even when the source is a .wav file.
            dst = os.path.join(
                transposed_dir,
                "{0:+d}_{1}.mp3".format(
                    semitones, os.path.splitext(filename)[0]))
            if not args.force and os.path.exists(dst):
                try:
                    if (not args.incremental) or (os.path.getmtime(dst) >= os.path.getmtime(src)):
                        skipped += 1
                        continue
                except OSError:
                    if not args.incremental:
                        skipped += 1
                        continue
            tasks.append((src, dst, semitones, target_bitrate))

    total_possible = len(songs) * len(semitones_list)
    print("Jobs to run: {0} of {1} ({2} already up to date).\n".format(
        len(tasks), total_possible, skipped))

    if not tasks:
        print("Nothing to do; everything is already up to date.")
        print("-" * 50)
        print("Processing complete.")
        return 0

    failed = 0
    errors_shown = 0
    MAX_ERRORS = 5
    jobs = max(1, args.jobs)

    if jobs == 1:
        for index, (src, dst, semitones, bitrate) in enumerate(tasks, 1):
            filename = os.path.basename(src)
            print("[{0}/{1}] {2} {3:+d} semitones...".format(
                index, len(tasks), filename, semitones))
            ok, err = _run_ffmpeg(src, dst, semitones, bitrate)
            if not ok:
                failed += 1
                if errors_shown < MAX_ERRORS:
                    print("    ERROR:\n{0}".format(err))
                    errors_shown += 1
    else:
        task_for = {}
        try:
            with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futures_list = [pool.submit(_run_ffmpeg, *t) for t in tasks]
                task_for = {fut: (i, t) for i, (fut, t) in
                            enumerate(zip(futures_list, tasks), 1)}
                for fut in futures.as_completed(futures_list):
                    index, task = task_for[fut]
                    src, _, semitones, _ = task
                    filename = os.path.basename(src)
                    ok, err = fut.result()
                    if ok:
                        print("[{0}/{1}] OK: {2} ({3:+d})".format(
                            index, len(tasks), filename, semitones))
                    else:
                        failed += 1
                        if errors_shown < MAX_ERRORS:
                            print("[{0}/{1}] FAIL: {2} ({3:+d}): {4}".format(
                                index, len(tasks), filename, semitones, err))
                            errors_shown += 1
        except KeyboardInterrupt:
            print("\nInterrupted by user. Partial work saved atomically where possible.")
            return 130

    print("-" * 50)
    print("Done. {0} succeeded, {1} failed ({2} skipped as up-to-date).".format(
        len(tasks) - failed, failed, skipped))
    return 1 if failed else 0


def main():
    rc = transpose_songs()
    if "--wait" in sys.argv and rc != 130:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
