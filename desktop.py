"""PyInstaller/dev entry point for the KARAOKE APP desktop application."""
from __future__ import annotations

import sys

from karaoke.desktop import main

if __name__ == "__main__":
    sys.exit(main())
