# SPDX-License-Identifier: AGPL-3.0-or-later
"""The status file: what the helper knows about the add-on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anki.buildinfo import version as anki_version

from .files import read_json, write_json
from .version import __version__

STATUS_NAME = "status.json"
STATUS_FORMAT_VERSION = 1


def update_status(exchange_dir: Path, **changes: Any) -> dict:
    """Merge changes into the status file and return what was written."""
    path = exchange_dir / STATUS_NAME
    try:
        status = read_json(path)
    except ValueError:
        status = None  # unreadable: start again
    if not isinstance(status, dict) or status.get("format_version") != STATUS_FORMAT_VERSION:
        status = {"deck_name": None, "last_export": None, "last_cycle": None}
    status = {
        **status,
        **changes,
        "format_version": STATUS_FORMAT_VERSION,
        "addon_version": __version__,
        "anki_version": anki_version,
    }
    write_json(path, status)
    return status
