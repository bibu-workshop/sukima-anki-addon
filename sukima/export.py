# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export: write a batch of the chosen deck's due review cards."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anki.collection import Collection
from anki.consts import CARD_TYPE_REV
from anki.scheduler.v3 import QueuedCards

from .files import write_json
from .log import log
from .settings import deck_list
from .status import update_status
from .blocks import card_sides_v2
from .images import helper_reads_format_2, prepare
from .sounds import helper_reads_format_3, sound_file
from .text import IMAGE, card_sides

BATCH_NAME = "batch.json"
BATCH_FORMAT_VERSION = 1
# Batch format 2: cards with images, lists, superscript, subscript or links (image cards spec).
BATCH_FORMAT_VERSION_2 = 2
# Format 3: sound cards (sound cards spec).
BATCH_FORMAT_VERSION_3 = 3
SOUNDS_DIR = "sounds"
IMAGES_DIR = "images"
# The prepared images of one batch together, at most (DEC-187 a); distinct images count once.
IMAGE_CAP_BYTES = 8_000_000
BATCH_FULL = "batch_full"
BATCH_CAP = 60
# The Anki days after today whose review cards go to the watch too, each studied
# only from its own day (owner, 2026-11-05: two days ahead).
LOOKAHEAD_DAYS = 2
DAY_MS = 86_400_000

# More than any deck's daily limits will let Anki serve.
_FETCH_LIMIT = 10_000

OK = "ok"
NO_DECK_CHOSEN = "no_deck_chosen"
DECK_MISSING = "deck_missing"
FAILED = "failed"

# Why an exported batch is empty.
NOTHING_DUE = "nothing_due"
NONE_WATCH_READY = "none_watch_ready"


@dataclass
class ExportResult:
    outcome: str
    batch_id: str | None = None
    exported: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    empty_because: str | None = None
    due_count: int | None = None
    # When Anki's day ends, as Anki counts days: after it, these cards are yesterday's.
    next_day_at_ms: int | None = None
    # Today's due review cards: sent to the watch, and left in Anki because the watch
    # can't show them (the tooltip after choosing a deck, design guide 1.18).
    today_ready: int = 0
    today_not_shown: int = 0


def batch_record_path(state_dir: Path, batch_id: str) -> Path:
    return state_dir / "batches" / f"{batch_id}.json"


def export_batch(
    col: Collection,
    deck_id: int | None,
    exchange_dir: Path,
    state_dir: Path,
    now_ms: int | None = None,
) -> ExportResult:
    """Write the batch file, the status file and the add-on's own batch record.

    A card that cannot be converted is skipped, never an error. With no deck
    to export from, no batch is written and the status says why.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        return _export(col, deck_id, exchange_dir, state_dir, now_ms)
    except Exception as e:
        # The exception's text may quote a card, so only its kind is logged.
        return _failed(exchange_dir, state_dir, now_ms, e)


def _failed(exchange_dir: Path, state_dir: Path, now_ms: int, error: Exception) -> ExportResult:
    """Report a failed export as far as the folders allow. Never raises."""
    result = ExportResult(FAILED)
    for report in (
        lambda: log(state_dir, "export_error", kind=type(error).__name__),
        lambda: _finish(exchange_dir, state_dir, now_ms, None, result, []),
    ):
        try:
            report()
        except Exception:
            pass
    return result


def _export(
    col: Collection, deck_id: int | None, exchange_dir: Path, state_dir: Path, now_ms: int
) -> ExportResult:
    day_end = _next_day_at_ms(col)
    deck = col.decks.get(deck_id, default=False) if deck_id else None
    if not deck:
        outcome = DECK_MISSING if deck_id else NO_DECK_CHOSEN
        return _finish(exchange_dir, state_dir, now_ms, None, ExportResult(outcome, next_day_at_ms=day_end), [])

    cards: list[dict] = []
    review_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    # Per covered day, today first: the due review cards left in Anki because the watch
    # can't show them (design guide 1.17, W2). Every skip reason counts.
    not_shown = [0] * (LOOKAHEAD_DAYS + 1)

    # Format 2 only when the helper has said it passes it on (FORMATS.md, cycle request).
    format_2 = helper_reads_format_2(state_dir)
    # Format 3, sound cards, likewise; a helper that passes on 3 passes on 2.
    format_3 = format_2 and helper_reads_format_3(state_dir)
    media_dir = col.media.dir()
    image_cache = state_dir / "images"
    images: dict[str, dict] = {}  # hash -> the batch's image entry
    image_files: dict[str, Path] = {}  # file name -> prepared file in the cache
    sounds: dict[str, dict] = {}  # hash -> the batch's sound entry
    sound_files: dict[str, Path] = {}  # file name -> the file in Anki's media folder
    # Images and sounds share the cap (DEC-190 d): distinct files, counted once.
    image_bytes = 0

    def with_images(blocks: list[dict] | None, new: dict[str, tuple]) -> tuple[list[dict] | None, str | None]:
        """The side's blocks with each image's file name replaced by its prepared hash."""
        if blocks is None:
            return None, None
        out = []
        for b in blocks:
            if "sound" in b:
                found = sound_file(Path(media_dir) / b["sound"])
                if found.hash not in sounds and found.hash not in new:
                    new[found.hash] = (found, None)
                out.append({"sound": found.hash, "beside_previous": b["beside_previous"]})
                continue
            if "image" not in b:
                out.append(b)
                continue
            prepared = prepare(Path(media_dir) / b["image"], image_cache)
            if prepared is None:
                return None, IMAGE
            if prepared.hash not in images and prepared.hash not in new:
                new[prepared.hash] = (prepared, b.get("alt"))
            elif prepared.hash in new and new[prepared.hash][1] is None and b.get("alt"):
                new[prepared.hash] = (prepared, b.get("alt"))
            out.append({"image": prepared.hash, "beside_previous": b["beside_previous"]})
        return out, None

    def take(card, available_from_ms: int | None, day: int = 0) -> None:
        """Adds the card if it converts; a card that does not is counted as skipped."""
        nonlocal image_bytes
        front_blocks = back_blocks = None
        new: dict[str, tuple] = {}
        if format_2:
            front, back, front_blocks, back_blocks, reason = card_sides_v2(card, media_dir, sounds=format_3)
            if not reason:
                front_blocks, reason = with_images(front_blocks, new)
            if not reason:
                back_blocks, reason = with_images(back_blocks, new)
            if not reason and image_bytes + sum(p.bytes for p, _ in new.values()) > IMAGE_CAP_BYTES:
                reason = BATCH_FULL
        else:
            front, back, reason = card_sides(card)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            not_shown[day] += 1
            log(state_dir, "skip", card=card.id, reason=reason)
            return
        for h, (prepared, alt) in new.items():
            if hasattr(prepared, "source"):  # a sound file
                sounds[h] = prepared.entry()
                sound_files[prepared.file_name] = prepared.source
                image_bytes += prepared.bytes
                continue
            entry_image = {
                "hash": h, "width": prepared.width, "height": prepared.height,
                "file_width": prepared.file_width, "file_height": prepared.file_height,
                "transparent": prepared.transparent, "bytes": prepared.bytes,
            }
            if alt:
                entry_image["description"] = alt
            images[h] = entry_image
            image_files[prepared.file_name] = image_cache / prepared.file_name
            image_bytes += prepared.bytes
        entry = {"card_id": card.id, "front": front, "back": back}
        if front_blocks is not None:
            entry["front_blocks"] = front_blocks
        if back_blocks is not None:
            entry["back_blocks"] = back_blocks
        if available_from_ms is not None:
            entry["available_from_ms"] = available_from_ms
        cards.append(entry)
        review_counts[str(card.id)] = card.reps

    # Today: Anki's own queue, with its limits.
    for entry in _review_queue(col, deck_id):
        if len(cards) == BATCH_CAP:
            break
        take(col.get_card(entry.card.id), None)
    today_count, today_skipped = len(cards), sum(skipped.values())

    # The next days: the cards due on each, as a search finds them (read only),
    # capped at the deck's daily review limit and the batch cap. Each can be
    # studied on the watch only from the start of its own Anki day.
    day_starts: list[int] = []
    if day_end is not None:
        per_day = min(_review_limit(col, deck_id), BATCH_CAP)
        day_starts = [day_end - DAY_MS] + [day_end + i * DAY_MS for i in range(LOOKAHEAD_DAYS)]
        for day in range(1, LOOKAHEAD_DAYS + 1):
            start = day_end + (day - 1) * DAY_MS
            first = len(cards)
            for card in _due_on_day(col, deck_id, day):
                if per_day <= 0 or len(cards) - first >= per_day:
                    break
                if str(card.id) in review_counts:
                    continue
                take(card, start, day)

    batch_id = str(uuid.uuid4())
    uses_format_2 = any("front_blocks" in c or "back_blocks" in c for c in cards)
    uses_format_3 = bool(sounds)
    version = BATCH_FORMAT_VERSION_3 if uses_format_3 else BATCH_FORMAT_VERSION_2 if uses_format_2 else BATCH_FORMAT_VERSION
    # The media files first: the helper sends a batch only once every file it names is there.
    _write_images(exchange_dir, image_files if uses_format_2 else {})
    _write_files(exchange_dir / SOUNDS_DIR, sound_files)
    write_json(
        batch_record_path(state_dir, batch_id),
        {
            "format_version": 1,
            "batch_id": batch_id,
            "deck_id": deck_id,
            "exported_at_ms": now_ms,
            "review_counts": review_counts,
        },
    )
    write_json(
        exchange_dir / BATCH_NAME,
        {
            "format_version": version,
            "batch_id": batch_id,
            "exported_at_ms": now_ms,
            "deck_name": deck["name"],
            **({"images": list(images.values())} if uses_format_2 else {}),
            **({"sounds": list(sounds.values())} if uses_format_3 else {}),
            "deck_id": deck_id,
            **_decks(col, state_dir),
            "cards": cards,
            **(
                {
                    "day_starts_ms": day_starts,
                    "covered_until_ms": day_starts[-1] + DAY_MS,
                    "not_shown_by_day": not_shown,
                }
                if day_starts
                else {}
            ),
        },
    )
    empty_because = None
    if not today_count:
        # Nothing to study today, whatever the next days hold.
        empty_because = NONE_WATCH_READY if today_skipped else NOTHING_DUE
    result = ExportResult(
        OK, batch_id, len(cards), skipped, empty_because, _due_count(col, deck_id), next_day_at_ms=day_end,
        today_ready=today_count, today_not_shown=today_skipped,
    )
    return _finish(
        exchange_dir, state_dir, now_ms, deck["name"], result, [c["card_id"] for c in cards]
    )


def _write_images(exchange_dir: Path, files: dict[str, Path]) -> None:
    """The batch's prepared images in `images/`, by hash; any other file there is removed."""
    _write_files(exchange_dir / IMAGES_DIR, files)


def _write_files(folder: Path, files: dict[str, Path]) -> None:
    """`files` in `folder`, by name; any other file there is removed."""
    if not files and not folder.exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = folder / name
        if not target.exists():
            tmp = folder / f".{name}.tmp"
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
    for old in folder.iterdir():
        if old.name not in files and not old.name.startswith("."):
            old.unlink()


def _decks(col: Collection, state_dir: Path) -> dict:
    """The deck list for the watch (design guide 1.18), or nothing if Anki can't give it:
    the watch then says it has no deck list yet, and the export goes on."""
    try:
        return {"decks": deck_list(col)}
    except Exception as e:
        log(state_dir, "deck_list_error", kind=type(e).__name__)
        return {}


def _due_count(col: Collection, deck_id: int) -> int:
    """How many review cards are due in the deck and its subdecks.

    The daily review limit is ignored, so this is the whole backlog and can be
    more than the deck list shows. Suspended and buried cards are left out,
    and so are cards being relearned, which the batch never holds either.
    A search only: nothing is changed.
    """
    decks = " OR ".join(f"did:{did}" for did in col.decks.deck_and_child_ids(deck_id))
    return len(col.find_cards(f"({decks}) is:due is:review -is:learn -is:suspended -is:buried"))


def _due_on_day(col: Collection, deck_id: int, day: int) -> list:
    """The deck's review cards due `day` Anki days from today, earliest due first.

    A search only, like the due count: nothing is changed. Cards being relearned,
    suspended or buried are left out, as today's batch leaves them out.
    """
    decks = " OR ".join(f"did:{did}" for did in col.decks.deck_and_child_ids(deck_id))
    ids = col.find_cards(f"({decks}) prop:due={day} is:review -is:learn -is:suspended -is:buried")
    found = [col.get_card(cid) for cid in ids]
    return sorted(found, key=lambda c: (c.due, c.id))


def _review_limit(col: Collection, deck_id: int) -> int:
    """The deck's daily review limit: its own if it has one, else its preset's.

    A guess for a later day: Anki cannot be asked for tomorrow's queue. Never raises.
    """
    try:
        deck = col.decks.get(deck_id, default=False) or {}
        own = deck.get("reviewLimit")
        if isinstance(own, int) and not isinstance(own, bool):
            return max(0, own)
        per_day = col.decks.config_dict_for_deck_id(deck_id)["rev"]["perDay"]
        return max(0, int(per_day))
    except Exception:
        return BATCH_CAP


def _review_queue(col: Collection, deck_id: int) -> list:
    """The deck's review cards in the order Anki's queue serves them.

    Anki serves the queue for the selected deck, so the deck is selected for
    the call and the user's selection put back afterwards.
    """
    before = col.decks.selected()
    col.decks.select(deck_id)
    try:
        queued = col.sched.get_queued_cards(fetch_limit=_FETCH_LIMIT)
    finally:
        col.decks.select(before)
    return [
        e
        for e in queued.cards
        if e.queue == QueuedCards.REVIEW and e.card.ctype == CARD_TYPE_REV
    ]


def _next_day_at_ms(col: Collection) -> int | None:
    """When Anki's day ends (its next day cutoff, after the rollover hour). Never raises."""
    try:
        return int(col.sched.day_cutoff) * 1000
    except Exception:
        return None


def _finish(
    exchange_dir: Path,
    state_dir: Path,
    now_ms: int,
    deck_name: str | None,
    result: ExportResult,
    card_ids: list[int],
) -> ExportResult:
    update_status(
        exchange_dir,
        deck_name=deck_name,
        last_export={
            "at_ms": now_ms,
            "outcome": result.outcome,
            "batch_id": result.batch_id,
            "exported": result.exported,
            "skipped": result.skipped,
            "empty_because": result.empty_because,
            "due_count": result.due_count,
            "next_day_at_ms": result.next_day_at_ms,
        },
    )
    log(
        state_dir,
        "export",
        outcome=result.outcome,
        batch=result.batch_id,
        exported=result.exported,
        skipped=sum(result.skipped.values()),
        skipped_by_reason=",".join(f"{r}:{n}" for r, n in sorted(result.skipped.items())),
        cards=",".join(map(str, card_ids)),
    )
    return result
