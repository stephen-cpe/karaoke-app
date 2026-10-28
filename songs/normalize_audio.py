import argparse
import os
import sys
import tempfile

try:
    from pydub import AudioSegment
    from pydub.exceptions import CouldntDecodeError
except ImportError:  # pragma: no cover - depends on the local environment
    sys.exit(
        "pydub is required for normalize_audio.py. "
        "Install it with: pip install -r requirements-media.txt"
    )

# --- Configuration ---
# Target loudness in dBFS (Decibels relative to full scale).
TARGET_DBFS = -20.0

# Target bitrate for compression.
TARGET_BITRATE = "192k"

# Output format. MP3 is required for bitrate-based compression.
OUTPUT_FORMAT = "mp3"

# MP3 is lossy, so encoding and decoding can shift the measured RMS level by a
# few tenths of a dB (about 0.27 dB at 192k in testing). Treat files within
# this window as already normalized; otherwise repeated runs would keep
# "correcting" a level the encoder immediately shifts back.
DBFS_TOLERANCE = 1.0
# ---------------------


def is_normalized(dbfs, tolerance=DBFS_TOLERANCE):
    """Return True if the given level is already within tolerance of target."""
    return abs(dbfs - TARGET_DBFS) <= tolerance


def export_mp3_atomic(segment, destination):
    """Encode to a temporary file, then replace the destination on success.

    This avoids truncating an existing destination (including an MP3 being
    normalized in place) if encoding fails.
    """
    output_dir = os.path.dirname(destination) or os.getcwd()
    fd, temp_path = tempfile.mkstemp(
        prefix=".normalize-", suffix=".tmp", dir=output_dir
    )
    os.close(fd)
    try:
        segment.export(temp_path, format=OUTPUT_FORMAT, bitrate=TARGET_BITRATE)
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def process_audio_files(force=False, tolerance=DBFS_TOLERANCE):
    """
    Scans the current directory for .wav and .mp3 files, normalizes files that
    need it, compresses them to the target bitrate, saves them as .mp3, and
    removes the original .wav files.

    Already-normalized .mp3 files are skipped unless force is True, because
    re-encoding them every run only adds generation loss.
    """
    current_directory = os.getcwd()
    print(f"Scanning for audio files in: {current_directory}\n")

    audio_files = [
        f
        for f in os.listdir(current_directory)
        if f.lower().endswith((".wav", ".mp3"))
        and os.path.isfile(os.path.join(current_directory, f))
    ]
    # Process WAVs first so an MP3 created from a WAV during this run is not
    # picked up again from the original directory listing.
    audio_files.sort(
        key=lambda f: (os.path.splitext(f)[1].lower() != ".wav", f.lower())
    )

    if not audio_files:
        print("No .wav or .mp3 files were found in this directory.")
        return 0, 0, 0

    print(f"Found {len(audio_files)} audio file(s) to process.\n")

    processed = skipped = failed = 0

    for filename in audio_files:
        input_path = os.path.join(current_directory, filename)
        base_filename, original_extension = os.path.splitext(filename)

        # All output files will have the .mp3 extension.
        output_filename = f"{base_filename}.{OUTPUT_FORMAT}"
        output_path = os.path.join(current_directory, output_filename)

        print(f"Processing '{filename}'...")

        try:
            # Load the audio file.
            song = AudioSegment.from_file(input_path)
            current_dbfs = song.dBFS

            if current_dbfs == float("-inf"):
                print("  - ERROR: file contains no measurable audio. Skipping.\n")
                failed += 1
                continue

            # An MP3 that is already at the target level is left untouched.
            # Re-encoding it would only add lossy generation loss.
            if (
                original_extension.lower() == ".mp3"
                and not force
                and is_normalized(current_dbfs, tolerance)
            ):
                print(
                    f"  - Already normalized at {current_dbfs:.2f} dBFS "
                    f"(target {TARGET_DBFS:.2f} +/- {tolerance:.2f} dB). Skipping.\n"
                )
                skipped += 1
                continue

            # --- Step 1: Normalize Audio ---
            change_in_dBFS = TARGET_DBFS - current_dbfs
            normalized_song = song.apply_gain(change_in_dBFS)
            print(
                f"  - Normalized: Loudness changed from {current_dbfs:.2f} dBFS "
                f"to {normalized_song.dBFS:.2f} dBFS (before MP3 encoding)."
            )

            # --- Step 2: Export with Compression ---
            print(f"  - Compressing and exporting to {TARGET_BITRATE} MP3...")
            export_mp3_atomic(normalized_song, output_path)
            print(f"  - Successfully saved as '{output_filename}'.")

            # --- Step 3: Clean Up Original WAV File ---
            # If the original was a .wav file and the new .mp3 was created
            # successfully, remove the original to save space.
            if original_extension.lower() == ".wav":
                os.remove(input_path)
                print("  - Removed original .wav file.")

            processed += 1
            print("-" * 25)

        except CouldntDecodeError:
            print(
                f"  - ERROR: Could not decode '{filename}'. "
                "It may be corrupted. Skipping.\n"
            )
            failed += 1
        except Exception as e:
            print(
                f"  - ERROR: An unexpected error occurred with "
                f"'{filename}': {e}. Skipping.\n"
            )
            failed += 1

    print(
        f"\nAudio processing complete: {processed} processed, "
        f"{skipped} skipped, {failed} failed."
    )
    return processed, skipped, failed


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Normalize .wav/.mp3 files to a consistent loudness and encode "
            "them as 192k MP3 files."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-encode MP3 files even if they are already normalized.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DBFS_TOLERANCE,
        help=(
            "How close to the target level counts as already normalized "
            f"(default: {DBFS_TOLERANCE:.2f} dB)."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.tolerance < 0:
        print("ERROR: --tolerance must be zero or greater.", file=sys.stderr)
        return 2

    print("-------------------------------------------------")
    print("--- Audio Normalization & Compression Script ---")
    print("-------------------------------------------------\n")
    print("This script will:")
    print(f"1. Normalize files to {TARGET_DBFS:.1f} dBFS loudness.")
    print(f"2. Convert and compress them into '{TARGET_BITRATE}' MP3 files.")
    print("3. DELETE the original .wav files after successful conversion.")
    if args.force:
        print("\n--force: MP3 files will be re-encoded even if already normalized.")
    else:
        print(
            f"\nMP3 files already within +/-{args.tolerance:.1f} dB of the target "
            "are left untouched."
        )
    print("\n!! WARNING: This action is irreversible. Back up your files first. !!\n")

    if not args.yes:
        while True:
            choice = input("Do you want to continue? (yes/no): ").lower().strip()
            if choice in ["yes", "y"]:
                break
            elif choice in ["no", "n"]:
                print("Operation cancelled by the user.")
                return 0
            else:
                print("Invalid input. Please enter 'yes' or 'no'.")

    _, _, failed = process_audio_files(
        force=args.force, tolerance=args.tolerance
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
