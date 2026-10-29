# Third-party notices

This file lists the third-party software and assets used by KARAOKE APP. It is
informational, not legal advice. The project's own code is MIT-licensed (see
`LICENSE`, copyright Stephen Paul Jacildo, 2026). The tables below cover
third-party components and assets; transitive packages (for example `numpy`,
`huggingface-hub`, `tokenizers`, `onnxruntime`, `av`, and `soundfile`) have
their own licenses.

## Runtime Python dependencies

| Package | Version tested | License |
|---|---|---|
| Flask | 3.1.3 | BSD-3-Clause |
| Waitress | 3.0.2 | ZPL 2.1 |

## Development and helper dependencies

| Package | Version tested | License |
|---|---|---|
| pytest | 9.1.1 | MIT |
| pydub | 0.25.1 | MIT |
| audioop-lts | 0.2.2 | PSF-2.0 |

## Lyric timing pipeline (optional)

| Package | Version tested | License |
|---|---|---|
| faster-whisper | 1.2.1 | MIT |
| Demucs | 4.1.0 | MIT |
| PyTorch (`torch`) | 2.10.0 | BSD-3-Clause |
| CTranslate2 | 4.8.2 | MIT |

The optional NVIDIA CUDA runtime packages (`nvidia-cublas-cu12`,
`nvidia-cudnn-cu12`, `nvidia-cuda-nvrtc-cu12`) are proprietary NVIDIA
redistributables. They are not bundled with this project.

The lyric pipeline downloads Demucs and Whisper model weights at runtime. The
code licenses above do not by themselves settle the licenses or training-data
terms of those model weights; audit them separately if you redistribute the
weights or a prebuilt environment.

## Vendored browser assets

| Asset | Version | License | License file |
|---|---|---|---|
| Bootstrap CSS | 5.3.3 | MIT | `static/vendor/bootstrap.LICENSE.txt` |
| WaveSurfer.js | 7.12.12 | BSD-3-Clause | `static/vendor/wavesurfer.LICENSE.txt` |

These files are served locally so the web UI does not need a CDN or Google Fonts.

## External programs and services (not bundled)

- **FFmpeg** is used for pitch shifting. FFmpeg is licensed under the LGPL-2.1-or-later
  by default, but some builds include GPL components and are therefore GPL. Check
  the license of the build you install. This project does not distribute FFmpeg.
- **Rubber Band** is the preferred FFmpeg pitch-shift filter. Rubber Band is
  GPL-2.0-or-later or available under a commercial license. This project does not
  distribute Rubber Band or an FFmpeg build that includes it.
- **LRCLIB** is a third-party lyrics service used by the optional timing pipeline.
  Its API documentation says the service is free, needs no API key, and asks
  clients to identify themselves and behave considerately. Its responses are not
  bundled. Lyrics remain the property of their respective rights holders; using
  LRCLIB does not grant redistribution rights.
- **NVIDIA CUDA runtime packages** are optional proprietary redistributables and
  are not bundled.

## Media and visual assets

The repository intentionally excludes songs, instrumentals, `.srt` files, and
`.words.json` lyric sidecars. The project logo is a generated asset created by
the maintainer with an image generator; confirm that tool's license terms. The
provenance of the PWA icons and the inline SVG icons in `templates/` is not
documented in this repository. Do not assume they are original, cleared, or
licensed for redistribution; replace them with assets you have rights to before
publishing. The host page's animated background is generated at runtime by
`static/background.js` and uses no third-party visual assets.

## Project license status

The project's own source code and documentation are released under the MIT
License in `LICENSE`, copyright Stephen Paul Jacildo, 2026. That license does
not extend to third-party dependencies, vendored assets, or any media you add;
those keep their own terms.
