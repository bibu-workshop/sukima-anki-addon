# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading and writing the plain files the add-on exchanges and keeps.

Every file is written whole under a temporary name and then moved into place,
so a reader never sees a partial file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

TEMP_SUFFIX = ".tmp"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX
    )
    try:
        try:
            os.write(fd, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any | None:
    """The file's contents, or None if it does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
