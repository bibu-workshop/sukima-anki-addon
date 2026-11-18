# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sound files for the watch (sound cards spec, ticket 02; DEC-190).

A sound is sent as Anki has it: the file is not changed, only named by its SHA-256.
Files are at most 1 MB (`blocks.MAX_SOUND_BYTES`), so each export hashes them again
rather than keep a cache. Only the collection's media folder is read; nothing is
written there.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .images import helper_reads_format


@dataclass(frozen=True)
class SoundFile:
    hash: str  # SHA-256 of the file
    type: str  # its extension, lower case: one of blocks.SOUND_TYPES
    bytes: int
    source: Path

    @property
    def file_name(self) -> str:
        return f"{self.hash}.{self.type}"

    def entry(self) -> dict:
        return {"hash": self.hash, "type": self.type, "bytes": self.bytes}


def sound_file(source: Path) -> SoundFile:
    data = source.read_bytes()
    return SoundFile(hashlib.sha256(data).hexdigest(), source.suffix.lower().lstrip("."), len(data), source)


def helper_reads_format_3(state_dir: Path) -> bool:
    return helper_reads_format(state_dir, 3)
