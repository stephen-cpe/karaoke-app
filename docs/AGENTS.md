# Agent guide

## Project layout

- `app.py` — entry point: imports/re-exports `create_app()`, configures logging, and
  starts Waitress or the Flask dev server.
- `karaoke/` — Flask application package:
  - `__init__.py` — app factory, config, error handlers, request logging.
  - `catalog.py`, `lyrics.py`, `transposition.py` — shared services.
  - `http.py`, `logging.py`, `config.py` — request, logging, and config helpers
    (`config.py` re-exports the application constants).
  - `errors.py` — `ApiError` exception type.
  - `routes/` — pages and media, shared-session API, library API, and browser-log
    blueprints.
- `constants.py` — constants shared by the Flask package and session state.
- `playback_state.py` — thread-safe in-memory session: queue, host lifecycle,
  presence timeout, ordered host commands, and command acknowledgement.
- `utils.py` — shared finite-number validation, integer parsing, and log sanitizing.
- `static/vendor/` — local Bootstrap and WaveSurfer builds plus their license files.
- `requirements*.txt` — core, dev/test, media-helper, lyric-pipeline, and optional
  CUDA dependency groups.
- `THIRD_PARTY_NOTICES.md` — dependency, asset, and license notes.
- `static/scripts.js` — host-only audio engine and shared-session client.
- `static/background.js` — host-only procedural WebGL star/nebula/galaxy background.
- `static/remote.js`, `static/remote.css`, `templates/remote.html` — controls-only
  phone remote. It must not create audio/video elements or play media.
- `templates/index.html` — host player page, including the Sync Editor link when
  the editor is enabled for localhost.
- `static/sync_editor.js`, `static/sync_editor.css`, `templates/sync_editor.html` —
  experimental local-only lyric sync editor (logic kept out of the template).
- `static/confirm-dialog.js`, `static/confirm-dialog.css` — shared modal confirmation
  dialog used by all pages.
- `static/manifest.webmanifest`, `static/sw.js`, `static/icon-*.png`,
  `static/apple-touch-icon.png` — phone PWA.
- `tests/` — pytest suite (API, shared state, logging, tools, and PWA), including the
  `tests/browser/` Playwright tests.
- `original_songs/` — source tracks (full mix with vocals).
- `songs/` — instrumentals derived from the matching original; same filename
  stem and performance. `normalize_audio.py` and `transpose.py` are the tracked
  helper scripts.
- `lyrics/` — generated active `.srt` files plus `.words.json` word-timing sidecars.
  `lyrics/drafts/` holds ignored editor drafts and is never served to the player.
- `tools/generate_lyrics.py` — Demucs + Whisper + LRCLIB timing pipeline.
- `tools/check_lyrics.py` — structural and vocal-energy validator.

## Shared player / phone remote

- Localhost gets the player at `/`; LAN clients get the remote. `/player` and
  `/remote` force either role. Only the host player page may output audio for the
  shared session; the Sync Editor can preview audio for timing work when enabled.
- The Sync Editor is experimental and disabled by default. Enable it with
  `--enable-editor` or `KARAOKE_EDITOR=1`; the page, draft/approve APIs, and active
  lyric APIs return 404 for LAN clients or when disabled, and the host-page link is
  hidden. Do not use `--dev` as the editor gate.
- Editor drafts live under `lyrics/drafts/<stem>.draft.json`, are ignored by the
  player and media route, and autosave locally. Only an explicit approval publishes
  the validated draft to the active `lyrics/<stem>.srt` and `.words.json` files; the
  draft is removed after a successful publish. Draft and active revisions are checked
  separately, and failed publishes restore the previous active bytes. If active lyrics
  changed after the draft base, approval is rejected and the draft remains; there is
  no automatic rebase.
- The server owns the queue and playback state. Phones call `/api/control`;
  state reaches browsers through `/api/events` (SSE) with polling fallback.
- Only one host is active. A second player takes over and the old host receives
  `host_revoked`; keep that behavior rather than allowing two audio sources.
- State is in-memory and resets on server restart. Do not add persistence unless
  explicitly requested.
- After changing app code, install the test group once and run:

  ```powershell
  .\venv\Scripts\python -m pip install -r requirements-dev.txt
  .\venv\Scripts\python -m pytest -q
  .\venv\Scripts\ruff check .
  node --check static\background.js
  node --check static\scripts.js
  node --check static\remote.js
  node --check static\sync_editor.js
  node --check static\confirm-dialog.js
  node --check static\sw.js
  ```

- Browser/UI tests use Playwright and skip when no Chromium-based browser is
  available. Install one with `.\venv\Scripts\python -m playwright install chromium`
  if Edge or Chrome is not present.
- Do not modify the lyric timing pipeline during app/UI work.

## Repository hygiene

- Never commit audio, video, `.srt`, or `.words.json` files. They are copyrighted
  media, not project source, and `.gitignore` excludes them.
- `tools/.cache/`, `karaoke.log*`, `venv/`, and local editor settings are ignored.
  Do not force-add them. Editor drafts under `lyrics/drafts/` are ignored too and
  must never be served as active lyrics.
- The public tree keeps `songs/`, `original_songs/`, `lyrics/`, and `transposed/`
  as `.gitkeep` placeholders. Only the helper scripts in `songs/` and the
  `.gitkeep` placeholders are tracked.
- The project source is MIT-licensed (see `LICENSE`). Do not change repository
  licensing or asset ownership without explicit user approval.
- Do not add a public version number, release tag, or versioning scheme. Existing
  cache names and the lyric tool's User-Agent are tooling details, not releases.
- The Sync Editor loads Bootstrap and WaveSurfer from `static/vendor/`; the host
  and remote use custom CSS. Do not reintroduce CDN or Google Fonts links.

## Adding lyrics for new songs

The timing pipeline is already built and tuned. Do not modify or reimplement it
as part of adding lyrics; the job is to run it and validate the output.

### 1. Preflight

Every new song needs a matching instrumental:

```powershell
$kara = (Get-ChildItem songs -File).BaseName
Get-ChildItem original_songs -File |
  Where-Object { $_.BaseName -notin $kara } |
  Select-Object -ExpandProperty Name
```

If anything is listed, report the missing instrumental files and stop.

### 2. Generate and validate

```powershell
.\tools\add_lyrics.ps1
```

This runs:

```powershell
python tools\generate_lyrics.py --missing --cache-dir tools\.cache
python tools\check_lyrics.py --energy
```

`--missing` only processes songs without an SRT, so existing files and manual
edits are safe. To regenerate one specific song explicitly:

```powershell
.\tools\add_lyrics.ps1 -Song "Innocent"
```

`-Song` bypasses `--missing` and overwrites that song's existing SRT/JSON.

### 3. Acceptance

- Each new song logs `wrote ... .srt`.
- Both `<stem>.srt` and `<stem>.words.json` exist. The checker scans only
  `.words.json`, so an SRT without a sidecar is not validated.
- Line-level lyric cues without word timings are valid. Use
  `python tools\check_lyrics.py --strict` only when checking pipeline-generated
  files that are expected to have complete word timings.
- The run ends with `done: N ok, 0 failed`.
- The checker ends with `total structural problems: 0`.
- No `duration mismatch` warning. If present, stop and report both durations;
  do not accept the SRT until the user confirms the pairing.

Report only a compact table:

```text
song | cues | words | structural problems | energy warnings
```

Do not paste full lyrics, SRT, or JSON.

## Rules

- Do not use `--force` unless the audio file changed.
- Do not inspect audio, run Whisper manually, write exploratory scripts, or
  search the web unless one specific song fails or has no usable reference.
- Do not modify `songs/`, `original_songs/`, or the timing pipeline during a
  lyric run. Tooling changes require an explicit user request.
- Do not create backups. Generated files replace the matching SRT/JSON directly.
- Do not run `pytest` or `node --check` unless app code changed.
- Energy warnings are usually backing vocals, ad-libs, or separation bleed.
  Ignore them unless a lyric cue is silent or starts more than 1.2s late.
- If `--missing` skips a song because an SRT already exists, report it instead
  of overwriting it.

## If one song fails

Rerun only that song and inspect only its log/output:

```powershell
.\tools\add_lyrics.ps1 -Song "<stem>"
```

The full generation log is `tools/.cache/last_run.log`.

If the log says `no usable online reference` or coverage is below 25%, flag the
song for manual lyric review. Do not invent text or hand-retime it.

## Tooling notes

- `tools/generate_lyrics.py` needs the lyric dependencies plus `ffmpeg`/`ffprobe`.
  `tools/add_lyrics.ps1` uses `python` from PATH by default; pass `-Python`
  (for example `.\venv\Scripts\python.exe`) to select an interpreter that has
  Demucs/faster-whisper/torch installed.
- Whisper tries CUDA `float16` first and falls back to CPU `int8` if CUDA setup or
  transcription fails. CUDA requires the optional CUDA 12 runtime packages listed
  in `requirements-cuda.txt`; keep the fallback.
- Demucs stems, Whisper transcripts, and non-empty LRCLIB responses are cached
  under `tools/.cache/`. Reusing them is intentional. Empty LRCLIB responses are
  not cached, so a later run retries them.
- The pipeline warns when `original_songs` and `songs` durations differ by more
  than 1s. Treat that as a pairing/edit mistake: stop and report it rather than
  accepting the SRT.
