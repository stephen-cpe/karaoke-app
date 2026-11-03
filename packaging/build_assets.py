"""Generate Windows build assets from the canonical application version."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "packaging" / "build"


def parse_version(value: str) -> tuple[int, int, int]:
    text = value.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise SystemExit(f"Invalid version {value!r}; expected v0.0.1 or 0.0.1")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def write_icon() -> Path:
    source = ROOT / "static" / "logo.webp"
    destination = BUILD_DIR / "karaoke-app.ico"
    image = Image.open(source).convert("RGBA")
    image.save(
        destination,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
               (128, 128), (256, 256)],
    )
    return destination


def write_version_file(version: str) -> Path:
    major, minor, patch = parse_version(version)
    numeric = f"{major}.{minor}.{patch}.0"
    product = f"{major}.{minor}.{patch}"
    is_pre_alpha = major == 0
    description = "KARAOKE APP (PRE-ALPHA)" if is_pre_alpha else "KARAOKE APP"
    destination = BUILD_DIR / "version_info.txt"
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        "040904B0",
        [
          StringStruct("CompanyName", "KARAOKE APP"),
          StringStruct("FileDescription", "{description}"),
          StringStruct("FileVersion", "{numeric}"),
          StringStruct("InternalName", "karaoke-app"),
          StringStruct("LegalCopyright", "Copyright (c) 2026 Stephen Paul Jacildo"),
          StringStruct("OriginalFilename", "karaoke-app.exe"),
          StringStruct("ProductName", "KARAOKE APP"),
          StringStruct("ProductVersion", "{product}"),
        ],
      ),
    ]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""
    destination.write_text(content, encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    icon = write_icon()
    version_file = write_version_file(args.version)
    print(f"icon: {icon}")
    print(f"version file: {version_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
