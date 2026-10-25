"""Builds and checks a release of the add-on, and optionally publishes it.

Run from the repository root:

    .venv/bin/python tools/release.py            # build and check only
    .venv/bin/python tools/release.py --publish  # also tag and create the GitHub release

The package is dist/sukima-<version>.ankiaddon, with <version> read from
sukima/version.py. Publishing needs a clean working tree and the gh tool.
"""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "sukima"

sys.path.insert(0, str(ROOT / "tools"))

from package_addon import package  # noqa: E402


def version() -> str:
    text = (SOURCE / "version.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def check(target: Path) -> None:
    """Raises if the package is not one Anki installs as Sukima for Anki."""
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise SystemExit("manifest.json is not at the top of the package")
        if any("__pycache__" in n or n.endswith(".pyc") for n in names):
            raise SystemExit("the package holds byte-code caches")
        manifest = json.loads(zf.read("manifest.json"))
    if manifest.get("package") != "sukima" or manifest.get("name") != "Sukima for Anki":
        raise SystemExit(f"unexpected manifest: {manifest}")


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    publish = "--publish" in sys.argv[1:]
    tag = f"v{version()}"
    if publish:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        if dirty.strip():
            raise SystemExit("the working tree is not clean")
    run(sys.executable, "-m", "pytest", "tests", "-q")
    target = ROOT / "dist" / f"sukima-{version()}.ankiaddon"
    package(SOURCE, target)
    check(target)
    print(f"built and checked {target.relative_to(ROOT)}")
    if publish:
        run("git", "tag", tag)
        run("git", "push", "origin", tag)
        run("gh", "release", "create", tag, str(target), "--title", f"Sukima for Anki {version()}", "--generate-notes")


if __name__ == "__main__":
    main()
