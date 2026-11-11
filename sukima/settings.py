# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the add-on keeps its files, and the deck chosen for the watch."""

from __future__ import annotations

import re
from pathlib import Path

from anki.collection import Collection

from .files import read_json, write_json

SETTINGS_NAME = "settings.json"

# The one place the exchange folder's location is decided.
EXCHANGE_DIR = Path.home() / "Library" / "Application Support" / "Sukima" / "Exchange"


def state_dir_for(base: Path, profile_name: str) -> Path:
    """The add-on's own folder for one collection: each Anki profile has its own.

    Two profile names never share a folder, whatever characters they hold.
    """
    safe = re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m.group()):x}_", profile_name)
    return base / "profiles" / (safe or "_")


def watch_decks(col: Collection) -> list[tuple[int, str]]:
    """The decks that can go to the watch. Filtered decks are never offered."""
    return [(d.id, d.name) for d in col.decks.all_names_and_ids(include_filtered=False)]


def deck_list(col: Collection) -> list[dict]:
    """The decks the watch can choose from (design guide 1.18): Anki's own deck tree, in
    Anki's order with parents before their sub-decks, each with its full name and its
    "Due" count as Anki's deck list shows it (review cards within the daily limits,
    sub-decks included). Filtered decks are left out, and so is what Anki's deck list
    hides, such as an empty Default deck. One call, as Anki's deck list makes; nothing
    is changed."""
    found: list[dict] = []

    def walk(node, path: str) -> None:
        for child in node.children:
            if child.filtered:
                continue
            name = f"{path}::{child.name}" if path else child.name
            found.append({"deck_id": child.deck_id, "name": name, "due": child.review_count})
            walk(child, name)

    walk(col.sched.deck_due_tree(), "")
    return found


def choose_deck(col: Collection, state_dir: Path, deck_id: int) -> None:
    if deck_id not in {i for i, _ in watch_decks(col)}:
        raise ValueError("not a deck that can go to the watch")
    write_json(state_dir / SETTINGS_NAME, {"format_version": 1, "deck_id": deck_id})


def chosen_deck_id(col: Collection, state_dir: Path) -> int | None:
    """The chosen deck, or None if none is chosen or the choice no longer stands.

    The setting is checked every time, so a filtered or deleted deck cannot be
    exported by editing the file by hand.
    """
    try:
        settings = read_json(state_dir / SETTINGS_NAME)
    except (ValueError, OSError):
        return None
    deck_id = settings.get("deck_id") if isinstance(settings, dict) else None
    if isinstance(deck_id, bool) or not isinstance(deck_id, int):
        return None
    return deck_id if deck_id in {i for i, _ in watch_decks(col)} else None
