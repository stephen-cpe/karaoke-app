KARAOKE APP — WINDOWS PRE-ALPHA DISTRIBUTION
============================================

Version: {{VERSION}}

This is an early pre-alpha hobby project. Bugs, incomplete behavior, and
unexpected changes are possible. Back up your media before using it.

WHAT IS INCLUDED
----------------
- karaoke-app.exe: the desktop launcher and local karaoke server.
- A self-contained Python runtime and the frontend assets.
- FFmpeg 9.0.2 (Gyan Doshi essentials build, GPLv3) with librubberband for
  on-demand pitch shifting.
- Empty songs/, lyrics/, and transposed/ folders beside the executable.

Songs, original vocal tracks, generated lyrics, AI models, CUDA libraries,
and personal data are NOT included. Add only media you have the right to use.

QUICK START
-----------
1. Extract the entire karaoke-app folder to a writable location, such as
   Documents or the Desktop. Do not extract into Program Files.
2. Double-click karaoke-app.exe.
3. Click "Open player" in the application window, or open:
       http://localhost:8080/player
   If port 8080 is busy, the app automatically uses the next free port
   through 8099. Use the URL shown in the application window.
4. Put instrumental .mp3 or .wav files in songs/. The app scans that folder
   when the library is loaded. Lyrics belong in lyrics/.
5. On the player screen, click "Enable sound" once so the browser allows
   audio playback.
6. On phones and tablets connected to the same private network, open the
   "Phone remotes" URL shown in the window.

REQUIREMENTS AND PRIVACY
-----------------------
- Windows 11 x64.
- A modern browser. Windows 11 includes Microsoft Edge.
- No Python installation and no internet connection are required.
- The application does not download dependencies, check for updates, or
  contact cloud services at runtime.
- Local network connectivity is required for phone remotes. The app binds
  to 0.0.0.0 and has no authentication, matching the original project.
  Use it only on a trusted private network. Do not expose it through router
  port forwarding to the public internet.

WINDOWS FIREWALL
----------------
Windows may ask for permission the first time the app listens on the LAN.
Allow karaoke-app.exe on PRIVATE networks if you want phones to connect.
If you decline, playback on this PC still works, but phone remotes will not.

SMARTSCREEN
-----------
The initial executable is unsigned. Windows SmartScreen may show a warning.
If you obtained the ZIP from the official project release and trust it,
choose "More info" and then "Run anyway". Code signing may be added later.

MEDIA FOLDERS
-------------
The following folders must remain beside karaoke-app.exe and must be
writable:
- songs/       instrumental audio files
- lyrics/      active SRT and .words.json lyric files
- transposed/  generated pitch-shifted MP3 cache

If the application cannot write to these folders, extract it to a writable
folder and try again. Logs are written to karaoke.log beside the executable;
if that is not writable, they fall back to:
    %LOCALAPPDATA%\KaraokeApp\logs\karaoke.log

PHONE REMOTES AND PWA NOTES
---------------------------
The phone remote works over ordinary HTTP on the local network. Browser
security rules require localhost or HTTPS for service-worker caching and
"Install app" support, so the remote may not be installable as a PWA over a
plain LAN HTTP address. The player itself does not require internet access.

PITCH SHIFTING
--------------
The bundled FFmpeg is used only when a non-zero key change is requested and
no cached transposed file exists. Pre-generated files in transposed/ play
without invoking FFmpeg. On-demand pitch shifting is CPU-intensive and may
take time on a basic laptop.

LICENSES
--------
The project source is MIT-licensed. Third-party components and assets retain
their own licenses. See LICENSE and THIRD_PARTY_NOTICES.md, the licenses/
folder, and the ffmpeg/ folder. FFmpeg and Rubber Band source links and build
information are included in ffmpeg/SOURCE-OFFER.txt and ffmpeg/README.txt.
