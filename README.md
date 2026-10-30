# KARAOKE APP

A self-hosted karaoke setup for your home network. The browser on the karaoke PC or
TV is the **only shared-session audio player**; phones and tablets on the same Wi-Fi
act as synchronized remotes for the queue, transport, key, and volume. Sing over
animated space visuals with big synced lyrics, and pitch-shift the key until it fits
your voice.

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
  phone remote stays live over Server-Sent Events (SSE) with polling as a fallback
- **Phone remote and installable PWA** — now playing, progress, the current lyric
  line with word highlighting, song search, queue add/remove/reorder/clear, pause,
  skip, restart, seek, key, and volume, with no audio element on the remote page
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
- **Sync Editor** (waveform + cascading timing) that saves lyrics straight to the
  server; it is linked from the host player but not the phone remote. The route is
  not access-controlled, so anyone on the LAN who knows the URL can open it
- **Logging built in**: terminal + rotating `karaoke.log` when started with
  `python app.py`, with host-side playback and library errors relayed to the server
- Helper scripts for loudness normalization and batch pre-transposing songs


## Requirements

- **Python 3.13+** (developed on 3.13/3.14)
- **FFmpeg** on your PATH — required for on-demand transposition, audio
  normalization, batch pre-transposing, and lyric generation. Playback of
  pre-generated files works without it. Most builds work, but the default
  filter (`rubberband`) requires an FFmpeg compiled with librubberband; see
  [Pitch shifting](#pitch-shifting) for a fallback filter.
- A browser. Before audio can start, the host player needs a user gesture; click
  **Enable sound** once (browser autoplay policy). Phones get the controls-only
  remote by default.

Optional dependency groups:

| File | Use |
|---|---|
| `requirements.txt` | Core Flask + Waitress runtime |
| `requirements-dev.txt` | Core runtime plus pytest, Playwright, and Ruff |
| `requirements-media.txt` | `pydub` (and `audioop-lts` on Python 3.13+) for `songs/normalize_audio.py` |
| `requirements-lyrics.txt` | Demucs, faster-whisper, PyTorch, CTranslate2, and NumPy pipeline |
| `requirements-cuda.txt` | Optional NVIDIA CUDA 12 runtime DLLs for GPU inference |

## Quick start (Windows)

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
# Install the core runtime plus the dev/test, media-helper, and lyric groups:
pip install -r requirements.txt -r requirements-dev.txt -r requirements-lyrics.txt -r requirements-media.txt
# NVIDIA GPU users on Windows/Linux x86_64 can also install the optional CUDA DLLs:
# pip install -r requirements-cuda.txt
# Or install just the core runtime:
# pip install -r requirements.txt
python app.py
```

The server starts on port **8080**:

- **Player** on this PC/TV: <http://localhost:8080> (or <http://localhost:8080/player>)
- **Phone remotes**: `http://<this-pc-ip>:8080` (or `http://<this-pc-ip>:8080/remote`)

Requests from `localhost` automatically get the player UI; other LAN addresses get
the controls-only remote. The explicit `/player` and `/remote` paths override that
choice. Open the player on the TV first and click **Enable sound** once so the
browser allows audio.

For development with auto-reload and debug output (binds to `127.0.0.1` by default;
use `--host` to change it):

```powershell
python app.py --dev
```

> **Note:** the normal server binds to all network interfaces (`0.0.0.0`) by default
> and has **no authentication**. That is on purpose — it is what lets guests queue
> songs from their phones — but anyone on the LAN who knows the URLs can also control
> playback, take over the player, or open the Sync Editor and overwrite lyrics.
> Only run it on networks you trust. Windows Firewall will ask the first time;
> allow it on private networks.

The host and remote use custom CSS; the Sync Editor loads Bootstrap and WaveSurfer
from `static/vendor/`. All pages use system fonts and make no third-party runtime
requests. `app.py` probes FFmpeg at startup for diagnostics; pitch shifting,
normalization, and lyric tooling only invoke it when you use those features.
LRCLIB is used only by the lyric pipeline.

## Shared playback model

- The Flask process keeps the queue and playback state **in memory** in
  `playback_state.py`; restarting the server starts a fresh session.
- The host page owns the only shared-session audio output, registers as the active
  player, and reports its true position every second. The phone remote page never
  creates or plays media; the Sync Editor is a separate editing surface that can
  preview audio for timing work.
- Phones send REST controls. The server broadcasts the new snapshot over
  `/api/events` (SSE) and queues ordered commands for the host, which acknowledges
  them with `last_command_id`.
- Only one player is active at a time. Opening the player in another browser takes
  over automatically; the previous screen stops playing and shows an overlay that
  offers **Use this screen as the player** to take back. The shared queue is kept.
- If the host stops reporting, the server marks it offline, freezes playback at the
  timeout boundary, and re-enables controls when the player reconnects.

### Installing the phone remote

The remote is a PWA. On `localhost` or an HTTPS origin, Chrome/Edge offer **Install**
and the service worker caches the remote shell for offline display. Browsers require
a **secure context** for service workers, so on a plain-HTTP LAN address the remote
still works normally but cannot install as a PWA or cache itself offline. On iOS, use
Share → **Add to Home Screen**; offline caching still requires HTTPS.

## Project structure

```
karaoke_app/
├── app.py                    # entry point: imports/re-exports create_app, logging, Waitress/dev server
├── karaoke/                  # Flask application factory, blueprints, and services
│   ├── __init__.py           # create_app(), app config, error handlers, request logging
│   ├── config.py             # compatibility re-export of the application constants
│   ├── errors.py             # ApiError exception type
│   ├── catalog.py            # filename parsing, scanning, catalog enrichment
│   ├── lyrics.py             # word-timing validation and atomic lyric saves
│   ├── transposition.py      # FFmpeg detection, filter building, transposition runner
│   ├── http.py               # request/client helpers
│   ├── logging.py            # console + rotating-file logging
│   └── routes/               # pages and media, shared session API, library API, browser logs
├── constants.py              # application-wide constants shared with session state
├── playback_state.py         # thread-safe in-memory queue, host lifecycle, commands
├── utils.py                  # shared number validation and log sanitizing
├── songs/                    # instrumental .mp3 / .wav files + helper scripts
│                             #   (media ignored; normalize_audio.py and transpose.py tracked)
├── original_songs/           # vocal versions used only by the lyric timing tool
├── lyrics/                   # .srt files + optional .words.json word-timing sidecars
├── transposed/               # pitch-shifted copies (cache; pre-generated or on demand)
├── static/
│   ├── background.js         # procedural WebGL star/nebula/galaxy background
│   ├── scripts.js            # host-only audio engine + shared-session client
│   ├── remote.js / remote.css # phone remote (controls only, no audio element)
│   ├── sync_editor.js        # sync editor logic (kept out of the HTML template)
│   ├── confirm-dialog.js / .css # shared confirmation dialog used by all pages
│   ├── styles.css            # host player styles
│   ├── sync_editor.css       # sync editor styles
│   ├── favicon.ico, logo.webp # browser/brand assets
│   ├── vendor/               # local Bootstrap + WaveSurfer builds and licenses
│   ├── manifest.webmanifest  # PWA metadata
│   ├── sw.js                 # remote-shell service worker
│   └── icon-*.png, apple-touch-icon.png # PWA / Apple touch icons
├── templates/                # index.html (host), remote.html (phone), sync_editor.html
├── tools/
│   ├── generate_lyrics.py    # Demucs + Whisper + LRCLIB lyric timing pipeline
│   ├── check_lyrics.py       # structural and vocal-energy validator
│   ├── add_lyrics.ps1        # generate + validate wrapper
│   └── .cache/               # separated vocals, transcripts, LRCLIB responses (ignored)
├── tests/                    # pytest suite
│   └── browser/              # Playwright host, remote, and editor UI tests
├── docs/
│   └── AGENTS.md             # agent handoff and lyric-generation workflow
├── pyproject.toml            # Ruff configuration
├── pytest.ini
├── requirements*.txt         # core, dev, media, lyrics, and optional CUDA groups
├── THIRD_PARTY_NOTICES.md    # dependency and asset license notes
├── LICENSE
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

Drop audio files into `songs/` and refresh the page — the catalog is scanned
whenever `/api/songs` is requested, so there is nothing to update by hand. Use this
naming convention:

```
{id}_{Title}_{Artist}.mp3
e.g.  00001_Two Steps Behind_Def Leppard.mp3
```

Parsing rules (see `parse_song_filename` in `karaoke/catalog.py`):

- everything before the **first** `_` is the song **id**
- everything after the **last** `_` is the **artist**
- the middle is the **title** (may contain underscores)
- `id_Title.mp3` also works — the artist is left blank
- files without an underscore use the filename stem as the title (artist blank)

Only `.mp3` and `.wav` files are picked up; other files (`*.jpg`, `*.py`, …) are ignored.

## Adding synced lyrics

1. Open the **Sync editor** from the host player header (or
   <http://localhost:8080/sync_editor.html>).
2. Load a song from the catalog (audio plus any existing `.srt` and `.words.json`),
   or import local `.srt` / `.words.json` files.
3. Nudge each cue with `−0.1` / `+0.1`, ripple later cues with the checkbox, or use
   the Shift all controls. Word timings are shifted with their cue, including the
   **Start** / **End** buttons at the playhead (or the S/E keys). `[Instrumental]`
   cues are preserved.
4. Editing a cue boundary can scale or clamp word timings so they stay inside the cue.
5. Pick the destination song and hit **Save**. The server writes the matching `.srt`
   *and* `.words.json`, so word-by-word highlighting keeps working. If no word
   timings are loaded, saving removes the stale sidecar and the player falls back to
   line-level SRT mode.

The song dropdown marks existing lyric files as `SRT`, `words`, or `SRT+words`. You
can also drop a `.srt` into `lyrics/` manually, as long as its filename matches the
audio filename (only the extension differs).

The Sync Editor is linked from the host player but not the phone remote; the route
is not access-controlled, so anyone on the LAN who knows the URL can open it. The
host player expects standard numbered SRT cues with comma-decimal timestamps
(`00:00:01,000`); the Sync Editor and phone remote accept a few more variants.

### Automatic timing with `tools/generate_lyrics.py`

The lyric tooling expects the **vocal versions** in `original_songs/`, which must
use the same filename stems as the instrumental tracks in `songs/`:

```
songs/00001_Two Steps Behind_Def Leppard.mp3
original_songs/00001_Two Steps Behind_Def Leppard.mp3
```

The tool separates vocals with Demucs, transcribes them with faster-whisper
`large-v3` (CUDA `float16` when available, CPU `int8` fallback), uses LRCLIB
synced lyrics when a usable reference is found (at least 25% token coverage) to
correct the text and line breaks, and maps the online timestamps onto the local
audio. If no usable reference is found, it builds lyrics from the local Whisper
transcript. It writes two files:

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
python tools\check_lyrics.py            # structural checks for every .words.json sidecar
python tools\check_lyrics.py --energy   # also flag silent cues and late vocal onsets
```

Structural problems return a non-zero exit code. Energy findings are warnings:
backing vocals, ad-libs, and separation bleed commonly trigger them.

For an agent handoff, see `docs/AGENTS.md`. The wrapper runs generation and, when
generation succeeds, validation:

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
CPU (`int8`) if CUDA setup or transcription fails. On Windows, install the optional
CUDA 12 runtime packages from `requirements-cuda.txt`; the tool exposes their DLL
folders automatically.

Long instrumental gaps (longer than three seconds) become `[Instrumental]` cues;
shorter gaps with audible vocal energy are left alone. A lyric cue ends at the
first sung phrase: held notes stay with their line, while later backing vocals or
ad-libs that are not in the lyric reference are left to the surrounding
`[Instrumental]` cue. Saving an edited SRT from the Sync Editor writes a matching
`.srt`; if no word timings were loaded, the stale `.words.json` is removed so the
player cannot show stale word timings. When the editor loads word timings, saving
writes both files so word-by-word highlighting is preserved.

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
| GET | `/sync_editor.html` | Lyric Sync Editor (host-oriented; not access-controlled) |
| GET | `/api/info` | Role, `server_id`, join/remote/player URLs, host connectivity, LAN IP and port |
| GET | `/api/state` | Full shared snapshot: `server_id`, `revision`, `host_connected`, `host_last_seen_seconds`, status, position, duration, current song, queue, transpose, volume, error, and `last_command_id` |
| GET | `/api/events?role=host\|remote&client_id=…` | SSE stream: `state` snapshots, `command` frames for the host, and `host_revoked` |
| POST | `/api/control` | Shared controls; body is `{"action": …}` plus action fields |
| POST | `/api/host/register` | Claim the player; returns the snapshot and any pending commands |
| POST | `/api/host/report` | Host heartbeat with status, position, duration, transpose, volume, filename, error, and acknowledged command id |
| GET | `/api/host/commands?client_id=…` | Command polling fallback for the host |
| POST | `/api/host/ended`, `/api/host/error`, `/api/host/disconnect` | Host lifecycle events |
| GET | `/api/songs` | Scanned catalog: `id`, `title`, `artist`, `filename`, `lyrics`, `words` |
| GET/POST | `/transpose?song_id=<filename>&semitones=<n>` | Returns `{"url": ...}` for original / cached / generated audio |
| POST | `/api/save-lyrics` | `{"song_id": <filename>, "lyrics": <srt text>, "words": <words json? optional>}` → writes `lyrics/<stem>.srt` and writes or removes `lyrics/<stem>.words.json`; validates optional word timings |
| POST | `/api/log` | Client-side log relay (`[browser] …`); the host uses it for playback and library errors |
| GET | `/manifest.webmanifest`, `/sw.js` | PWA manifest and remote service worker |
| GET | `/songs/…`, `/lyrics/…`, `/transposed/…` | Media file serving (only `.mp3`/`.wav`, `.srt`/`.words.json` as appropriate) |

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

Browser tests use Playwright against a temporary local Flask server. They cover
host player library/queue interactions, the phone remote, shared confirmation
dialogs, keyboard focus, reduced motion, and Sync Editor prompts:

```powershell
venv\Scripts\python -m playwright install chromium  # once, unless Edge or Chrome is installed
venv\Scripts\python -m pytest tests\browser
venv\Scripts\python -m pytest -m "not browser"
```

Playwright uses an installed Edge, Chrome, or bundled Chromium; if none is
available, the browser tests skip instead of failing the suite.

## Logging & troubleshooting

When started through `python app.py`, the server logs to the terminal **and** to
`karaoke.log` (rotated, ~1 MB per file). To get the same file logging from a WSGI
host, call `setup_logging()` yourself.

- every non-media request with status code, duration and remote address; successful
  media requests are DEBUG. 404s are warnings, except missing `/lyrics/` files,
  which are INFO.
- each transposition: cache hit or FFmpeg generation, plus full FFmpeg output on failure
- **host-side playback and library errors are relayed to the server**, so host
  "could not load audio" problems appear as `[browser] …` lines in the log. The phone
  remote and Sync Editor do not install a global error relay.
- startup diagnostics from `app.py`: resolved folders, catalog size, FFmpeg
  availability, and whether your build has the `rubberband` filter

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
| `[browser] Browser blocked playback until the host screen is clicked` | The browser blocked autoplay — click **Enable sound** on the host page once |
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
- **One player at a time:** only one browser page can be the shared-session audio
  host. Opening `/player` in another browser takes over automatically; the previous
  page stops playing and shows the takeover overlay.
- **SSE connections hold server threads:** each open browser stream occupies one
  waitress worker thread. The default is 16 (`--threads`); add capacity for many
  simultaneous phones.
- **PWA installation needs a secure context:** the phone remote works over plain HTTP
  on the LAN, but service-worker caching and installability require localhost or HTTPS.
- **Background rendering:** the animated backdrop needs WebGL. Without it, the host
  shows a static CSS nebula fallback. `prefers-reduced-motion` freezes the animation.
- **No authentication:** anyone on the network can queue songs, control playback,
  change key or volume, take over the player, or open the Sync Editor and overwrite
  lyrics. Only run it on networks you trust.
- **Large libraries:** the song and queue lists render every row; thousands of songs
  would want pagination or virtual scrolling.
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
