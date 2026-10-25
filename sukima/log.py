# SPDX-License-Identifier: AGPL-3.0-or-later
"""The add-on's log: one line per step and per outcome.

Lines carry card IDs, counts and reasons. Never card content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "addon.log"


def log(state_dir: Path, what: str, /, **fields: object) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    parts = [stamp, what] + [f"{k}={v}" for k, v in fields.items()]
    with open(state_dir / LOG_NAME, "a", encoding="utf-8") as f:
        f.write(" ".join(parts) + "\n")
