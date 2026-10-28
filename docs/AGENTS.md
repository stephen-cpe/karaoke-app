# Agent guide

## Project layout

- `app.py` — Flask routes: role-aware pages, shared-session API, SSE, catalog,
  transposition, lyrics saving, and PWA endpoints.
- `playback_state.py` — thread-safe in-memory session: queue, host lifecycle,
  presence timeout, ordered host commands, and command acknowledgement.
- `utils.py` — shared finite-number validation and log-line sanitizing.
- `static/vendor/` — local Bootstrap and WaveSurfer builds plus their license files.
- `requirements*.txt` — core, dev/test, media-helper, lyric-pipeline, and optional
  CUDA dependency groups.
- `THIRD_PARTY_NOTICES.md` — dependency, asset, and license notes.
- `static/scripts.js` — host-only audio engine and shared-session client.
- `static/remote.js`, `static/remote.css`, `templates/remote.html` — controls-only
  phone remote. It must not create audio/video elements or play media.
- `templates/index.html` — host player page, including the sync editor link.
- `static/manifest.webmanifest`, `static/sw.js`, `static/icon-*.png` — phone PWA.
- `tests/` — Flask app, API, shared-state, and PWA tests.
- `original_songs/` — source tracks (full mix with vocals).
- `songs/` — instrumentals derived from the matching original; same filename
  stem and performance.
- `lyrics/` — generated `.srt` files plus `.words.json` word-timing sidecars.
- `tools/generate_lyrics.py` — Demucs + Whisper + LRCLIB timing pipeline.
- `tools/check_lyrics.py` — structural and vocal-energy validator.

## Shared player / phone remote

- Localhost gets the player at `/`; LAN clients get the remote. `/player` and
  `/remote` force either role. Only the player page may output audio.
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
  node --check static\scripts.js
  node --check static\remote.js
  node --check static\sw.js
  ```

- Do not modify the lyric timing pipeline during app/UI work.

## Repository hygiene

- Never commit audio, video, `.srt`, or `.words.json` files. They are copyrighted
  media, not project source, and `.gitignore` excludes them.
- `tools/.cache/`, `karaoke.log*`, `venv/`, and local editor settings are ignored.
  Do not force-add them.
- The public tree keeps `songs/`, `original_songs/`, `lyrics/`, `transposed/`, and
  `background/` as `.gitkeep` placeholders. Only the helper scripts in `songs/`
  and the `.gitkeep` placeholders are tracked.
- The project source is MIT-licensed (see `LICENSE`). Do not change repository
  licensing or asset ownership without explicit user approval.
- Do not add a public version number, release tag, or versioning scheme. Existing
  cache names and the lyric tool's User-Agent are tooling details, not releases.
- The sync editor loads Bootstrap and WaveSurfer from `static/vendor/`; do not
  reintroduce CDN or Google Fonts links.

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

- Audio tooling uses the global Python 3.14.9 install, not `.venv` (the venv has
  the web app and test dependencies but not Demucs/faster-whisper/torch).
- Whisper tries CUDA `float16` first and falls back to CPU `int8`. CUDA requires
  the optional CUDA 12 runtime packages listed in `requirements-cuda.txt`; keep
  the fallback.
- Demucs stems, Whisper transcripts, and non-empty LRCLIB responses are cached
  under `tools/.cache/`. Reusing them is intentional. Empty LRCLIB responses are
  not cached, so a later run retries them.
- The pipeline warns when `original_songs` and `songs` durations differ by more
  than 1s. Treat that as a pairing/edit mistake: stop and report it rather than
  accepting the SRT.
