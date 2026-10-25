# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export: write a batch of the chosen deck's due review cards."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anki.collection import Collection
from anki.consts import CARD_TYPE_REV
from anki.scheduler.v3 import QueuedCards

from .files import write_json
from .log import log
from .status import update_status
from .text import card_sides

BATCH_NAME = "batch.json"
BATCH_FORMAT_VERSION = 1
BATCH_CAP = 60

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
    deck = col.decks.get(deck_id, default=False) if deck_id else None
    if not deck:
        outcome = DECK_MISSING if deck_id else NO_DECK_CHOSEN
        return _finish(exchange_dir, state_dir, now_ms, None, ExportResult(outcome), [])

    cards: list[dict] = []
    review_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for entry in _review_queue(col, deck_id):
        if len(cards) == BATCH_CAP:
            break
        card = col.get_card(entry.card.id)
        front, back, reason = card_sides(card)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            log(state_dir, "skip", card=card.id, reason=reason)
            continue
        cards.append({"card_id": card.id, "front": front, "back": back})
        review_counts[str(card.id)] = card.reps

    batch_id = str(uuid.uuid4())
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
            "format_version": BATCH_FORMAT_VERSION,
            "batch_id": batch_id,
            "exported_at_ms": now_ms,
            "deck_name": deck["name"],
            "cards": cards,
        },
    )
    empty_because = None
    if not cards:
        empty_because = NONE_WATCH_READY if skipped else NOTHING_DUE
    result = ExportResult(OK, batch_id, len(cards), skipped, empty_because, _due_count(col, deck_id))
    return _finish(
        exchange_dir, state_dir, now_ms, deck["name"], result, [c["card_id"] for c in cards]
    )


def _due_count(col: Collection, deck_id: int) -> int:
    """How many review cards are due in the deck and its subdecks.

    The daily review limit is ignored, so this is the whole backlog and can be
    more than the deck list shows. Suspended and buried cards are left out,
    and so are cards being relearned, which the batch never holds either.
    A search only: nothing is changed.
    """
    decks = " OR ".join(f"did:{did}" for did in col.decks.deck_and_child_ids(deck_id))
    return len(col.find_cards(f"({decks}) is:due is:review -is:learn -is:suspended -is:buried"))


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
