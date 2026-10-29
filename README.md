# KARAOKE APP

A self-hosted karaoke setup for your home network. The browser on the karaoke
PC or TV is the **only audio player**; phones and tablets on the same Wi-Fi act as
synchronized remotes for the queue, transport, key, and volume. Sing over
animated space visuals with big synced lyrics, and pitch-shift the key until it
fits your voice.

> **Project status:** This is a personal, experimental, educational hobby
> project, not a product. It is **very early pre-alpha** software: behavior and
> file layouts may change, and it is provided **as-is, with no warranty** and no
> support commitment. See [Licensing and media](#licensing-and-media).

## What is not included

This repository does **not** ship songs, instrumentals, or lyrics. Those files
are copyrighted by their respective owners and are excluded by `.gitignore`.
Add your own legally obtained media before using the app. The
`lyrics/*.words.json` sidecars contain full lyrics, not just timings, so they are
excluded as well. `.gitignore` only affects Git: do not publish a ZIP or a
drag-and-drop copy of the working tree, because it still contains the media.

## Features

- **One shared session** — the server owns the queue, now-playing song, key, and
  volume; the host browser plays audio and reports its real position, while every
  phone stays live over Server-Sent Events (SSE) with polling as a fallback
- **Phone remote and installable PWA** — now playing, progress, the current lyric
  line with word highlighting, song search, queue add/remove/reorder/clear, pause,
  skip, restart, seek, key, and volume, with no audio element on the phone
- **Song catalog scanned live** from the `songs/` folder — no manual list to maintain
- Live search, one-click queueing, and an always-visible **Up next** panel
- **Synced lyrics with word-by-word highlighting**, displayed full-screen over a
  procedurally animated space backdrop of stars, nebulae, and galaxies;
  `[Instrumental]` cues cover intros and breaks
- **Built-in procedural background** — WebGL-rendered star fields, nebulae, and a
  spiral galaxy; no external media assets, and it pauses when the tab is hidden
- **Automatic lyric timing tool** (`tools/generate_lyrics.py`) using Demucs vocal
  separation, Whisper word timestamps and LRCLIB text
- **Pitch shifting (±4 semitones)** so singers can find their key, with caching
- **Sync Editor** (waveform + cascading timing) that saves lyrics straight to the server;
  it stays on the host player so phones cannot accidentally open it
- **Logging built in**: terminal + `karaoke.log`, with browser-side errors relayed to the server
- Helper scripts for loudness normalization and batch pre-transposing songs


## Requirements

- **Python 3.13+** (developed on 3.13/3.14)
- **FFmpeg** on your PATH — required for on-demand transposition, audio
  normalization, batch pre-transposing, and lyric generation. Playback of
  pre-generated files works without it. Most builds work, but the default
  filter (`rubberband`) requires an FFmpeg compiled with librubberband; see
  [Pitch shifting](#pitch-shifting) for a fallback filter.
- A browser. The host player needs one click somewhere on the page before audio
  starts (browser autoplay policy). Phones only run the controls-only remote.

Optional dependency groups:

| File | Use |
|---|---|
| `requirements.txt` | Core Flask + Waitress runtime |
| `requirements-dev.txt` | Core runtime plus pytest |
| `requirements-media.txt` | `pydub` for `songs/normalize_audio.py` |
| `requirements-lyrics.txt` | Demucs + faster-whisper + PyTorch pipeline |
| `requirements-cuda.txt` | Optional NVIDIA CUDA 12 runtime DLLs for GPU inference |

## Quick start (Windows)

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
# Install everything (core + dev/test + media helper + lyric pipeline + optional CUDA):
pip install -r requirements.txt -r requirements-cuda.txt -r requirements-dev.txt -r requirements-lyrics.txt -r requirements-media.txt
# Or install just the core runtime:
# pip install -r requirements.txt
python app.py
```

The server starts on port **8080**:

- **Player** on this PC/TV: <http://localhost:8080> (or <http://localhost:8080/player>)
- **Phone remotes**: `http://<this-pc-ip>:8080` (or `http://<this-pc-ip>:8080/remote`)

Requests from `localhost` automatically get the player UI; other LAN addresses get
the controls-only remote. The explicit `/player` and `/remote` paths override that
choice. Open the player on the TV first and click once so the browser allows audio.

For development with auto-reload and debug output (binds to localhost only):

```powershell
python app.py --dev
```

> **Note:** the server binds to all network interfaces (`0.0.0.0`) and has **no
> authentication**. That is on purpose — it's what lets guests queue songs from their
> phones — but only run it on networks you trust. Windows Firewall will ask the first
> time; allow it on private networks.

The web UI loads Bootstrap and WaveSurfer from `static/vendor/` and uses system
fonts, so it makes no third-party runtime requests. FFmpeg, the lyric pipeline,
and LRCLIB are only used when you invoke those features.

## Shared playback model

- The Flask process keeps the queue and playback state **in memory** in
  `playback_state.py`; restarting the server starts a fresh session.
- The host page owns the only `Audio` object, registers as the active player, and
  reports its true position every second. Phones never create or play media.
- Phones send REST controls. The server broadcasts the new snapshot over
  `/api/events` (SSE) and queues ordered commands for the host, which acknowledges
  them with `last_command_id`.
- Only one player is active at a time. Opening the player in another browser banners
  the new screen and offers **Use this screen as the player**; taking over stops the
  old screen and keeps the shared queue.
- If the host stops reporting, the server marks it offline, freezes playback at the
  timeout boundary, and re-enables controls when the player reconnects.

### Installing the phone remote

The remote is a PWA. On `localhost` or an HTTPS origin, Chrome/Edge offer **Install**
and the service worker caches the remote shell for offline display. Browsers require
a **secure context** for service workers, so on a plain-HTTP LAN address the remote
still works normally but cannot be installed or cache itself offline. On iOS, use
Share → **Add to Home Screen** over HTTPS; the same secure-context rule applies.

## Project structure

```
karaoke_app/
├── app.py                    # entry point: create_app(), logging, Waitress/dev server
├── karaoke/                  # Flask application factory, blueprints, and services
│   ├── __init__.py           # create_app(), app config, error handlers, request logging
│   ├── config.py             # shared config values
│   ├── catalog.py            # filename parsing, scanning, catalog enrichment
│   ├── lyrics.py             # word-timing validation and atomic lyric saves
│   ├── transposition.py      # FFmpeg detection, filter building, transposition runner
│   ├── http.py               # request/client helpers
│   ├── logging.py            # console + rotating-file logging
│   └── routes/               # pages, shared session API, library API, browser logs
├── constants.py              # application-wide constants shared with session state
├── playback_state.py         # thread-safe in-memory queue, host lifecycle, commands
├── utils.py                  # shared number validation and log sanitizing
├── songs/                    # instrumental .mp3 / .wav files + helper scripts
│                             #   (media ignored; helper .py files tracked)
├── original_songs/           # vocal versions used only by the lyric timing tool
├── lyrics/                   # .srt files + optional .words.json word-timing sidecars
├── transposed/               # pitch-shifted copies (cache; pre-generated or on demand)
├── static/
│   ├── background.js         # procedural WebGL star/nebula/galaxy background
│   ├── scripts.js            # host-only audio engine + shared-session client
│   ├── remote.js / remote.css # phone remote (controls only, no audio element)
│   ├── sync_editor.js        # sync editor logic (kept out of the HTML template)
│   ├── styles.css            # host player styles
│   ├── sync_editor.css       # sync editor styles
│   ├── vendor/               # local Bootstrap + WaveSurfer builds and licenses
│   ├── manifest.webmanifest  # PWA metadata
│   ├── sw.js                 # remote-shell service worker
│   └── icon-*.png            # PWA / Apple touch icons
├── templates/                # index.html (host), remote.html (phone), sync_editor.html
├── tools/
│   ├── generate_lyrics.py    # Demucs + Whisper + LRCLIB lyric timing pipeline
│   ├── check_lyrics.py       # structural and vocal-energy validator
│   ├── add_lyrics.ps1        # generate + validate wrapper
│   └── .cache/               # separated vocals, transcripts, LRCLIB responses (ignored)
├── tests/                    # pytest suite
├── docs/
│   └── AGENTS.md             # agent handoff and lyric-generation workflow
├── pyproject.toml            # Ruff configuration
├── pytest.ini
├── requirements*.txt         # core, dev, media, lyrics, and optional CUDA groups
├── THIRD_PARTY_NOTICES.md    # dependency and asset license notes
└── README.md
```

The media folders are kept in the public tree with `.gitkeep` placeholders.
Everything you add to `songs/`, `original_songs/`, `lyrics/`, and `transposed/`
is ignored by Git; only the two helper scripts in `songs/` and the `.gitkeep`
placeholders are tracked.

## Preparing new songs manually

1. **Obtain the song legally.** Purchase it or download it from a licensed source,
   then place the full original track in `original_songs/`.
2. **Remove the vocals.** Use a vocal-removal tool such as
   [Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui/)
   to create the instrumental, and place it in `songs/`.
3. **Normalize and pre-transpose.** Install the helper dependencies once, then
   from inside `songs/` run:

   ```powershell
   pip install -r ..\requirements-media.txt
   python normalize_audio.py
   python transpose.py
   ```

   `normalize_audio.py` normalizes loudness to -20 dBFS and re-encodes at 192 kbps
   (needs `pydub` + FFmpeg; the original `.wav` files are deleted). MP3s already
   within 1 dB of the target are skipped, so running it again is safe; use
   `--force` to re-encode them anyway. `transpose.py` pre-generates the ±1–±4
   semitone copies in `transposed/`, so key changes are instant during the party.
   It skips outputs that are already present (`--force` to rebuild,
   `--incremental` when a source is newer).

Keep the filename stem identical in both folders so the lyric pipeline can pair the
tracks:

```text
songs/00010_With Or Without You_U2.mp3
original_songs/00010_With Or Without You_U2.mp3
```

## Adding songs

Drop audio files into `songs/` and refresh the page — the catalog is scanned on every
request, so there is nothing to update by hand. Use this naming convention:

```
{id}_{Title}_{Artist}.mp3
e.g.  00001_Two Steps Behind_Def Leppard.mp3
```

Parsing rules (see `parse_song_filename` in `app.py`):

- everything before the **first** `_` is the song **id**
- everything after the **last** `_` is the **artist**
- the middle is the **title** (may contain underscores)
- `id_Title.mp3` also works — the artist is left blank
- anything else still shows up, using the filename as the title

Non-audio files (`*.jpg`, `*.py`, …) are ignored by the scan.

## Adding synced lyrics

1. Open the **Sync editor** from the header (or <http://localhost:8080/sync_editor.html>).
2. Load a song from the catalog (audio plus any existing `.srt` and `.words.json`),
   or import local `.srt` / `.words.json` files.
3. Nudge each cue with `-.1` / `+.1`, ripple later cues with the checkbox, or use
   the Shift all controls. Word timings are shifted with their cue, including
   `set start` / `set end` at the playhead. `[Instrumental]` cues are preserved.
4. Editing a cue boundary can scale or clamp word timings so they stay inside the cue.
5. Pick the destination song and hit **Save**. The server writes the matching `.srt`
   *and* `.words.json`, so word-by-word highlighting keeps working. If no word
   timings are loaded, saving removes the stale sidecar and the player falls back to
   line-level SRT mode.

A `✓` in the song dropdown means that song already has lyrics. You can also drop a
`.srt` into `lyrics/` manually, as long as its filename matches the audio filename
(only the extension differs).

### Automatic timing with `tools/generate_lyrics.py`

The lyric tooling expects the **vocal versions** in `original_songs/`, which must
use the same filename stems as the instrumental tracks in `songs/`:

```
songs/00001_Two Steps Behind_Def Leppard.mp3
original_songs/00001_Two Steps Behind_Def Leppard.mp3
```

The tool separates vocals with Demucs, transcribes them with faster-whisper
`large-v3` (CUDA `float16` when available, CPU `int8` fallback), uses LRCLIB
synced lyrics to correct the text and line breaks, and maps the online
timestamps onto the local audio. It writes two files:

- `lyrics/<stem>.srt` — ordinary line-level subtitles, portable to any player
- `lyrics/<stem>.words.json` — the same cues plus per-word timings; the web player
  prefers this file for word-by-word highlighting and falls back to the SRT

Install the lyric dependencies separately from the Flask app (they are large and
are not in the core runtime):

```powershell
pip install -r requirements-lyrics.txt
```

For NVIDIA GPU acceleration, install the CUDA build of PyTorch from the official
PyTorch index first, then install the optional CUDA runtime DLLs:

```powershell
pip install -r requirements-cuda.txt
```

The pipeline still runs on CPU (`int8`) without a GPU.

Run from the project root:

```powershell
python tools\generate_lyrics.py                    # all songs with an original
python tools\generate_lyrics.py --missing          # only songs without an SRT yet
python tools\generate_lyrics.py --song "Innocent"  # one song (title or stem)
python tools\generate_lyrics.py --no-online        # local transcript only
python tools\generate_lyrics.py --force            # ignore cached vocals/transcripts
```

Validate the generated files without re-running the audio pipeline:

```powershell
python tools\check_lyrics.py            # structural checks for every song
python tools\check_lyrics.py --energy   # also flag silent cues and late vocal onsets
```

Structural problems return a non-zero exit code. Energy findings are warnings:
backing vocals, ad-libs, and separation bleed commonly trigger them.

For an agent handoff, see `docs/AGENTS.md`. The wrapper runs generation and validation
in one step:

```powershell
.\tools\add_lyrics.ps1
```

The first run is slow: Demucs separation and Whisper transcription are cached under
`tools/.cache/`, so later runs only rebuild the lyrics. Online lookups are cached too,
but an empty LRCLIB response is retried on the next run instead of being cached.
The pipeline sends the track title and artist to LRCLIB; it does not upload the
audio. LRCLIB's API documentation asks clients to identify themselves and behave
considerately; the tool sets a User-Agent and caches responses.
Before generating, the tool warns when the original and instrumental durations differ
by more than one second; that usually means the instrumental came from a different
edit or performance. Whisper prefers CUDA (`float16`) and automatically falls back to
CPU (`int8`) when CTranslate2 cannot load a GPU. On Windows, install the optional
CUDA 12 runtime packages from `requirements-cuda.txt`; the tool exposes their DLL
folders automatically.

Instrumental intros and breaks of three seconds or more become `[Instrumental]`
cues. A lyric cue ends at the first sung phrase: held notes stay with their line,
while later backing vocals or ad-libs that are not in the lyric reference are left
to the surrounding `[Instrumental]` cue. Saving an edited SRT from the Sync Editor
writes a matching `.srt`; if no word timings were loaded, the stale `.words.json`
is removed so the player cannot show stale word timings. When the editor loads
word timings, saving writes both files so word-by-word highlighting is preserved.

## Pitch shifting

The player offers ±4 semitones. When a key change is requested:

1. If `transposed/{+n}_{stem}.mp3` already exists (e.g. from `songs/transpose.py`),
   it is served instantly — this is why pre-generating is worth it.
2. Otherwise the backend generates it on demand with FFmpeg and caches the result in
   `transposed/`. Generation is limited to five minutes per song and writes to a
   temporary file first, so a failed run cannot leave a corrupt cache entry.

On-demand generation uses the `rubberband=pitch={pitch}` filter by default. If your
FFmpeg build lacks librubberband, set a fallback filter before starting the server:

```powershell
$env:KARAOKE_FFMPEG_FILTER = "asetrate=44100*{pitch},atempo=1/{pitch}"
python app.py
```

(That fallback shifts the pitch and keeps normal speed, at slightly lower quality.)

## API

| Method | Path | Description |
|---|---|---|
| GET | `/`, `/player`, `/remote` | Role-aware root (localhost = player, LAN = remote); explicit overrides |
| GET | `/api/info` | Role, join/remote/player URLs, host status, LAN IP and port |
| GET | `/api/state` | Full shared snapshot: `server_id`, `revision`, host presence, status, position, duration, current song, queue, transpose, volume |
| GET | `/api/events?role=host\|remote&client_id=…` | SSE stream: `state` snapshots, `command` frames for the host, and `host_revoked` |
| POST | `/api/control` | Shared controls; body is `{"action": …}` plus action fields |
| POST | `/api/host/register` | Claim the player; returns the snapshot and any pending commands |
| POST | `/api/host/report` | Host heartbeat with status, position, duration, key, volume, and acknowledged command id |
| GET | `/api/host/commands?client_id=…` | Command polling fallback for the host |
| POST | `/api/host/ended`, `/api/host/error`, `/api/host/disconnect` | Host lifecycle events |
| GET | `/api/songs` | Scanned catalog: `id`, `title`, `artist`, `filename`, `lyrics`, `words` |
| GET/POST | `/transpose?song_id=<filename>&semitones=<n>` | Returns `{"url": ...}` for original / cached / generated audio |
| POST | `/api/save-lyrics` | `{"song_id": <filename>, "lyrics": <srt text>, "words": <words json? optional>}` → writes `lyrics/<stem>.srt` and writes or removes `lyrics/<stem>.words.json` |
| POST | `/api/log` | Browser relays client-side errors into the server log (`[browser] …`) |
| GET | `/manifest.webmanifest`, `/sw.js` | PWA manifest and remote service worker |
| GET | `/songs/…`, `/lyrics/…`, `/transposed/…` | Media file serving |

Shared-control actions: `add` (`filename`), `remove` (`index`), `clear`, `reorder`
(`from`, `to`), `skip`, `play`, `pause`, `restart`, `seek` (`position`), `transpose`
(`semitones`), `volume` (`volume`, 0–1), and `stop` (stop + clear).

## Tests

```powershell
pip install -r requirements-dev.txt
venv\Scripts\python -m pytest
venv\Scripts\ruff check .
```

The suite covers filename parsing, the catalog endpoint, media-route extension
filtering, lyrics saving (including traversal, non-string/NUL filenames, oversize
rejection, and stale word-timing cleanup), transposition behavior and failure
cleanup, the shared-state model (host takeover, command acknowledgement, timeout
freezing, queue ordering, stale ended events), the role-aware pages, the
control/state/host APIs, SSE framing, logging sanitization, and the PWA routes.
FFmpeg is mocked, so the tests run anywhere without it. The exact test count is
whatever `pytest` reports for your checkout; it is intentionally not duplicated
here.

## Logging & troubleshooting

The server logs to the terminal **and** to `karaoke.log` (rotated, ~1 MB per file):

- every request with status code, duration and remote address — 404s are warnings
- each transposition: cache hit or FFmpeg generation, plus full FFmpeg output on failure
- **browser-side errors are relayed to the server**, so "could not load audio" problems
  appear as `[browser] …` lines in the log — no need to open the browser dev console
- startup diagnostics: resolved folders, catalog size, FFmpeg availability, and whether
  your build has the `rubberband` filter

Set `KARAOKE_LOG_LEVEL=DEBUG` for extra detail (e.g. successful media file requests):

```powershell
$env:KARAOKE_LOG_LEVEL = "DEBUG"
python app.py
```

Reading the common entries:

| Log line | Meaning |
|---|---|
| `WARNING … GET /songs/00002_….mp3 -> 404` | A catalog entry has no file on disk — refresh the page to rescan |
| `[browser] Audio playback failed … MEDIA_ERR_SRC_NOT_SUPPORTED` | The browser downloaded the file but could not decode it (corrupt/unsupported) |
| `[browser] Playback could not start … NotAllowedError` | The browser blocked autoplay — click anywhere on the page once |
| `ffmpeg: not found on PATH` | Install FFmpeg for on-demand transposition (pre-generated files still play) |
| `Transpose failed … rubberband` | Your FFmpeg lacks librubberband — set `KARAOKE_FFMPEG_FILTER` (see above) |

## Licensing and media

**Project license.** The project's own source code is licensed under the MIT
License; see `LICENSE`. Third-party packages, vendored assets, external programs,
and any media you add are **not** covered by it and keep their own terms. The
project is provided "as-is", without warranty. This section is not legal advice.

Third-party components, vendored assets, and their licenses are listed in
`THIRD_PARTY_NOTICES.md`. FFmpeg and Rubber Band are external programs, not
bundled; their license terms depend on the build you install.

Songs, instrumentals, and lyrics are **not included**. Do not commit media you
do not have rights to. LRCLIB is a third-party service; lyrics returned by it
remain copyrighted, and using the service does not grant redistribution rights.

The project logo is a generated asset created by the maintainer with an image
generator; confirm that tool's license terms give you the rights you need. The
provenance of the PWA icons and the inline SVG icons in the templates is not
documented in this repository. Treat them as placeholders and replace them with
assets you have rights to before publishing. The host background is drawn at
runtime by `static/background.js`; it contains no third-party visual assets.

## Known limitations & ideas for later

- **In-memory session:** the queue and playback state reset when the Flask process
  restarts. Refreshing a browser does not reset them; restarting the server does.
- **One player at a time:** only one browser page can be the audio host. Another page
  can take over, and the previous page shows a takeover overlay instead of playing.
- **SSE connections hold server threads:** each open browser stream occupies one
  waitress worker thread. The default is 16 (`--threads`); add capacity for many
  simultaneous phones.
- **PWA installation needs a secure context:** the phone remote works over plain HTTP
  on the LAN, but service-worker caching and installability require localhost or HTTPS.
- **Background rendering:** the animated backdrop needs WebGL. Without it, the host
  shows a static CSS nebula fallback. `prefers-reduced-motion` freezes the animation.
- **No authentication:** anyone on the network can queue, cancel, seek and transpose.
  Only run it on networks you trust.
- **Large libraries:** the song list renders every row; thousands of songs would want
  pagination or virtual scrolling.
- **Transposition concurrency:** two simultaneous requests for the same uncached song
  can run FFmpeg twice; each writes a unique temporary file and the last successful
  result wins, so the cache cannot be left half-written.
- **Lyric timing is automatic, not magic:** dense mixes and repeated choruses are the
  hardest cases. Spot-check each song before a party; the Sync Editor can nudge any
  line. Transcription takes several minutes per song on CPU and is cached.
- **Backend modules:** `app.py` is the entry point; Flask blueprints and shared
  services live under `karaoke/`. Keep route handlers thin and put reusable
  behavior in the service modules rather than growing `app.py` again.
- **CI:** CI is not configured. The project's own code is MIT-licensed (see
  `LICENSE`).
