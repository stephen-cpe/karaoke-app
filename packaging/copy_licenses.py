"""Copy third-party license texts into a staged distribution directory."""
from __future__ import annotations

import importlib.metadata as metadata
import os
import shutil
import sys
from pathlib import Path

DISTRIBUTIONS = (
    "blinker",
    "click",
    "flask",
    "itsdangerous",
    "jinja2",
    "markupsafe",
    "pillow",
    "pystray",
    "pyinstaller",
    "setuptools",
    "six",
    "waitress",
    "werkzeug",
)


def copy_file(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def copy_python_licenses(destination: Path) -> None:
    root = Path(sys.base_prefix)
    python_license = root / "LICENSE.txt"
    if not python_license.is_file():
        python_license = root / "LICENSE"
    copy_file(python_license, destination / "PYTHON-LICENSE.txt")

    for pattern in ("tcl/tk*/license.terms", "tcl/tcl*/license.terms"):
        for tcl_license in root.glob(pattern):
            copy_file(tcl_license, destination / "TCL-TK-LICENSE.txt")
            break


def main(argv: list[str] | None = None) -> int:
    if len(sys.argv) != 2:
        print("usage: copy_licenses.py <destination>", file=sys.stderr)
        return 2
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)

    copy_python_licenses(destination)
    copied = 0
    for name in DISTRIBUTIONS:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        dist_info = Path(getattr(distribution, "_path", ""))
        candidates: list[Path] = []
        for license_name in distribution.metadata.get_all("License-File") or []:
            candidates.append(dist_info / license_name)
        if dist_info.is_dir():
            candidates.extend(
                path for path in dist_info.rglob("*")
                if path.is_file()
                and any(token in path.name.upper()
                        for token in ("LICENSE", "COPYING"))
            )
        seen: set[Path] = set()
        for source in candidates:
            if source in seen:
                continue
            seen.add(source)
            relative = source.relative_to(dist_info) if dist_info in source.parents else source.name
            target = destination / f"{name}-{str(relative).replace(os.sep, '-')}"
            if copy_file(source, target):
                copied += 1
    print(f"copied {copied} package license file(s) to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
