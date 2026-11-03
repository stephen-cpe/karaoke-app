# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder build for the portable Windows desktop app."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent
BUILD = ROOT / "packaging" / "build"

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]
# Keep Flask's package data (for example, anything required by its own
# templates/tests helpers) discoverable; explicit application data above is
# the source of truth for our UI.
datas += collect_data_files("flask")

hiddenimports = [
    "pystray._win32",
    "PIL._tkinter_finder",
]

excludes = [
    "av",
    "ctranslate2",
    "demucs",
    "faster_whisper",
    "IPython",
    "jedi",
    "matplotlib",
    "numpy",
    "nvidia",
    "onnxruntime",
    "pandas",
    "playwright",
    "pydub",
    "pytest",
    "ruff",
    "scipy",
    "tools",
    "torch",
    "torchgen",
    "tests",
]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="karaoke-app",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(BUILD / "karaoke-app.ico"),
    version=str(BUILD / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="karaoke-app",
)
