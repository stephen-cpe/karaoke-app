"""Song filename parsing, catalog scanning, and catalog enrichment."""
from __future__ import annotations

import os
from typing import Any

from karaoke.config import AUDIO_EXTENSIONS


def parse_song_filename(filename: str) -> dict[str, str]:
    """Parse ``id_Title_Artist.ext`` into song metadata (best effort).

    Rules:
      - everything before the first ``_`` is the song id
      - everything after the last ``_`` is the artist
      - the middle is the title (may contain underscores)

    Files without an underscore are treated as a bare title, and
    ``id_Title.ext`` works too (artist left blank).
    """
    stem = os.path.splitext(filename)[0]
    if "_" not in stem:
        return {"id": "", "title": stem, "artist": "", "filename": filename}
    song_id, rest = stem.split("_", 1)
    title, separator, artist = rest.rpartition("_")
    if not separator:
        title, artist = rest, ""
    return {"id": song_id, "title": title, "artist": artist, "filename": filename}


def scan_songs(songs_dir: os.PathLike[str] | str) -> list[dict[str, Any]]:
    """Return playable audio files in ``songs_dir`` sorted by title."""
    try:
        with os.scandir(songs_dir) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
    except OSError:
        return []
    found = [
        parse_song_filename(name)
        for name in names
        if name.lower().endswith(AUDIO_EXTENSIONS)
    ]
    found.sort(key=lambda song: (song["title"].casefold(), song["id"]))
    return found


def find_song(songs_dir: os.PathLike[str] | str,
              filename: Any) -> dict[str, Any] | None:
    """Resolve a client-supplied filename to its catalog entry (or None).

    Rejects anything that is not a plain filename (no separators, no path
    traversal, no NUL bytes) or that does not exist on disk.
    """
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        return None
    if (filename != os.path.basename(filename)
            or "/" in filename or "\\" in filename):
        return None
    if not filename.lower().endswith(AUDIO_EXTENSIONS):
        return None
    if not os.path.isfile(os.path.join(songs_dir, filename)):
        return None
    return parse_song_filename(filename)


def song_stem(filename: str) -> str:
    """Return the filename stem used for lyrics and transposed caches."""
    return os.path.splitext(filename)[0]


def enrich_song(song: dict[str, Any],
                lyrics_dir: os.PathLike[str] | str) -> dict[str, Any]:
    """Attach the ``lyrics`` / ``words`` availability flags used by the UI."""
    stem = song_stem(song["filename"])
    song["lyrics"] = os.path.isfile(os.path.join(lyrics_dir, stem + ".srt"))
    song["words"] = os.path.isfile(os.path.join(lyrics_dir, stem + ".words.json"))
    return song
