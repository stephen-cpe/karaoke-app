# Karaoke App {{VERSION}}

**Early pre-alpha build. Bugs, incomplete functionality, and unexpected behavior are possible.**

This release provides the portable Windows 11 x64 build of KARAOKE APP. The
application is a hobby project and is not yet a stable release.

## Download and run

1. Download `karaoke-app-{{VERSION}}-win64.zip` below.
2. Extract the complete `karaoke-app` folder to a writable location such as
   Documents or the Desktop. Do not extract it into Program Files.
3. Double-click `karaoke-app.exe`.
4. Use **Open player** in the application window, or open the player URL shown
   there, and click **Enable sound** once on the player screen.

Python and FFmpeg do not need to be installed. The runtime is bundled. No
internet connection is required, but phones and tablets must be on the same
private network as the player computer.

## Included

- Windows x64 desktop launcher and local Flask/Waitress player server.
- Player, phone remote, queue management, synced lyrics, and pitch shifting.
- Bundled FFmpeg 9.0.2 essentials build with librubberband support.
- Empty `songs/`, `lyrics/`, and `transposed/` folders beside the executable.

Songs, original vocal tracks, lyrics, AI models, CUDA libraries, caches, and
personal data are not included. Add only media you have the right to use.

## Important notes

- **Windows Firewall:** allow `karaoke-app.exe` on private networks if you
  want phone remotes. If you decline, local playback still works.
- **SmartScreen:** the executable is unsigned. If you downloaded this release
  from the official repository and trust it, choose **More info → Run anyway**
  when Windows shows a warning.
- **Network security:** the server binds to `0.0.0.0` and has no
  authentication, matching the existing project design. Use it only on a
  trusted private network. Do not expose it through router port forwarding to
  the public internet.
- **Phone PWA:** the remote works over ordinary LAN HTTP, but service-worker
  caching and installation require localhost or HTTPS.
- **Port:** the app tries 8080 and automatically uses the next available port
  through 8099. The window always displays the actual URLs.
- **Pitch shifting:** on-demand key changes use bundled FFmpeg and may be
  CPU-intensive on a basic laptop. Pre-generated files in `transposed/` are
  served without invoking FFmpeg.

## Third-party licenses

The project source is MIT-licensed. Third-party components retain their own
licenses. See `LICENSE`, `THIRD_PARTY_NOTICES.md`, `licenses/`, and
`ffmpeg/README.txt`/`ffmpeg/SOURCE-OFFER.txt` in the distribution.

## Verification scope

The packaged application has been built and tested in the project's available
Windows environment. A clean Windows 11 machine without Python installed
remains a required manual acceptance test. See the repository documentation
for the full acceptance checklist.
