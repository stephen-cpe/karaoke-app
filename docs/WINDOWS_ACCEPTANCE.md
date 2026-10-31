# Windows acceptance checklist

This checklist covers the checks that cannot be fully proven by the local build
or automated test suite. Complete it on a clean Windows 11 x64 machine without
Python installed before declaring the release ready.

## Prerequisites

- Windows 11 x64.
- A clean machine or VM with no Python installation and no development tools.
- The unsigned ZIP from the release being tested.
- A trusted private LAN, if testing phone remotes.
- Media you have the right to use, if testing playback and lyrics. Do not use
  repository sample media; the repository does not include any.

## First launch, offline

- [ ] Disconnect the machine from the internet.
- [ ] Extract the ZIP to a writable folder such as `Documents\KaraokeTest`.
- [ ] Confirm `songs\`, `lyrics\`, `transposed\`, and `ffmpeg\` are beside
      `karaoke-app.exe`.
- [ ] Double-click `karaoke-app.exe`.
- [ ] Confirm Windows SmartScreen behavior is documented and the app starts
      after choosing **More info → Run anyway** if needed.
- [ ] Confirm a startup/status window appears with meaningful status text.
- [ ] Confirm the main window displays `KARAOKE APP`,
      `Version: 0.0.1 (PRE-ALPHA)`, `Status: Running`, the local player URL,
      and the detected LAN URLs.
- [ ] Confirm the local player opens in the default browser.
- [ ] Confirm `karaoke.log` is created beside the executable.

## Media and playback

- [ ] Add a user-owned instrumental MP3/WAV to `songs\`, refresh the player,
      and confirm it appears in the library.
- [ ] Queue and play it; confirm audio starts after **Enable sound**.
- [ ] Confirm the queue, pause, skip, restart, seek, volume, and key controls
      work.
- [ ] Confirm the phone remote URL opens on a phone/tablet on the same LAN.
- [ ] Confirm the remote does not create an audio element and does not play
      audio itself.
- [ ] Confirm a `.srt` and/or `.words.json` file in `lyrics\` is discovered and
      displayed by the player.
- [ ] Confirm a pre-generated file in `transposed\` plays without invoking
      FFmpeg.
- [ ] Request a non-zero key change and confirm the bundled FFmpeg produces a
      transposed MP3 in `transposed\` and playback continues.
- [ ] Confirm no CUDA, PyTorch, Demucs, faster-whisper, or other AI/ML runtime
      is needed.

## Network and firewall

- [ ] On first launch, allow `karaoke-app.exe` on **Private networks** in the
      Windows Firewall prompt.
- [ ] Confirm phone remotes work while the firewall rule is allowed.
- [ ] Confirm local playback still works if LAN access is declined/blocked.
- [ ] Confirm the application still binds to `0.0.0.0` and retains the existing
      unauthenticated LAN behavior. Do not expose it through router port
      forwarding to the public internet.
- [ ] Confirm ordinary LAN HTTP remote works; document that PWA installation
      and service-worker caching require localhost or HTTPS.

## Tray and complete shutdown

- [ ] Close the main window and confirm the application minimizes to the tray
      instead of exiting.
- [ ] Restore the window from the tray.
- [ ] Open the player from the tray.
- [ ] Choose Exit from the tray and confirm the window/tray disappear.
- [ ] Confirm `karaoke-app.exe` is gone from Task Manager.
- [ ] Confirm ports 8080–8099 are released (or use `netstat -ano` / Resource
      Monitor).
- [ ] Repeat after a key change/transposition is in progress; confirm no
      orphaned FFmpeg process remains.
- [ ] With a phone remote/SSE connection open, exit the desktop app and confirm
      it still terminates and releases the port.

## Port and instance behavior

- [ ] Occupy port 8080 with another application, launch KARAOKE APP, and
      confirm it selects and displays a free port from 8081–8099.
- [ ] Confirm all generated URLs use the selected port.
- [ ] Occupy ports 8080–8099 and confirm the app shows a clear startup error.
- [ ] Launch a second copy and confirm it does not create a second backend;
      it should direct the user to the existing tray instance.

## Version and release consistency

- [ ] Compare the GUI label, `karaoke-app.exe` FileVersion/ProductVersion,
      `README-DISTRIBUTION.txt`, and the release tag. All must identify
      `0.0.1 (PRE-ALPHA)` / `v0.0.1`.
- [ ] Confirm the GitHub Release is marked as a pre-release and titled
      `Karaoke App v0.0.1`.
- [ ] Confirm the ZIP checksum matches the published `.sha256` asset.
- [ ] Confirm no songs, `original_songs/`, `.srt`, `.words.json`, AI models,
      caches, development tools, or personal data are in the ZIP.
- [ ] Confirm the release includes `ffmpeg/FFMPEG-SOURCE-OFFER.txt`,
      `ffmpeg/LICENSE`, `ffmpeg/README.txt`, and the `licenses/` folder.

## Developer/CI verification

- [ ] `python app.py --help` works.
- [ ] `python app.py` and `python app.py --dev` remain usable in a normal
      development environment.
- [ ] The Python, JavaScript, Ruff, and browser test suites pass.
- [ ] The GitHub Actions Windows workflow builds the expected ZIP for a test
      tag and does not overwrite an existing release.
- [ ] The packaged executable self-test passes:

  ```powershell
  .\dist\karaoke-app\karaoke-app.exe --self-test
  ```

- [ ] The packaged GUI startup/shutdown smoke test passes:

  ```powershell
  .\dist\karaoke-app\karaoke-app.exe --smoke-test-gui
  ```

### Clean-machine sign-off

- [ ] Performed on a clean Windows 11 x64 machine without Python.
- [ ] Tester:
- [ ] Date:
- [ ] Notes / failures:
