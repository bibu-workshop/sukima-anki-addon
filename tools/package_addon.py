"""Packs the add-on into sukima.ankiaddon, the file Anki installs from.

Anki reads the package's manifest.json only when it installs a packaged file,
so this is how the add-on list comes to show "Sukima for Anki". Run from
addon/:

    .venv/bin/python tools/package_addon.py

The file is written to dist/. Byte-code caches and the add-on's own
user_files/ are left out.
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "sukima"
TARGET = ROOT / "dist" / "sukima.ankiaddon"

LEFT_OUT = {"__pycache__", "user_files", "meta.json", ".DS_Store"}


def package(source: Path, target: Path) -> list[str]:
    """Writes the package and returns the names it holds, in order."""
    target.parent.mkdir(parents=True, exist_ok=True)
    names = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or LEFT_OUT & set(relative.parts):
                continue
            zf.write(path, relative.as_posix())
            names.append(relative.as_posix())
    return names


if __name__ == "__main__":
    for name in package(SOURCE, TARGET):
        print(name)
    print(f"wrote {TARGET.relative_to(ROOT)}", file=sys.stderr)
