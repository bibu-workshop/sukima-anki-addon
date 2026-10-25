# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay: record the watch's review events through Anki's own scheduler.

The add-on decides only whether an event is applied, dropped or held. Every
interval is Anki's.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from anki.collection import Collection
from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV
from anki.errors import NotFoundError
from anki.scheduler.v3 import CardAnswer

from .export import batch_record_path
from .files import read_json, write_json
from .log import log
from .status import update_status

EVENTS_NAME = "review-events.json"
RESULTS_NAME = "results.json"
EVENTS_FORMAT_VERSION = 1
EVENT_FORMAT_VERSION = 1
RESULTS_FORMAT_VERSION = 1

APPLIED, DROPPED, HELD = "applied", "dropped", "held"

# Reasons an event is dropped.
UNKNOWN_FORMAT = "unknown_format"
BAD_RATING = "bad_rating"
UNKNOWN_BATCH = "unknown_batch"
NOT_IN_BATCH = "not_in_batch"
CARD_MISSING = "card_missing"
REVIEWED_ELSEWHERE = "reviewed_elsewhere"
CHANGED_SINCE_EXPORT = "changed_since_export"
BAD_TIME = "bad_time"
ERROR = "error"
HELD_TOO_LONG = "held_three_cycles"
# Reasons an event is held.
ANKI_REFUSED = "anki_refused"
# Notes on an applied event.
ALREADY_APPLIED = "already_applied"
UNDONE_IN_ANKI = "undone_in_anki"

MAX_CYCLES_HELD = 3
# Anki moves an answer on by a millisecond when two share one, so an entry in
# its review history matches an event when it is this close to the answer time.
HISTORY_MATCH_MS = 10

# How far a watch's clock may disagree with the Mac's before an answer time is not believed.
CLOCK_SLACK_MS = 10 * 60 * 1000
_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_MAX_TIME_TAKEN_MS = 2**31 - 1

_RATINGS = {
    "again": CardAnswer.AGAIN,
    "hard": CardAnswer.HARD,
    "good": CardAnswer.GOOD,
    "easy": CardAnswer.EASY,
}


@dataclass
class ReplayResult:
    results: list[dict] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r["outcome"] == outcome)

    @property
    def counts(self) -> dict[str, int]:
        return {o: self.count(o) for o in (APPLIED, DROPPED, HELD)}


class _Records:
    """The add-on's own records of what it has applied and what it is holding."""

    def __init__(self, state_dir: Path) -> None:
        self.applied_path = state_dir / "applied.json"
        self.held_path = state_dir / "held.json"
        self.applied: dict = _read_record(state_dir, self.applied_path, "applied")
        self.held: dict = _read_record(state_dir, self.held_path, "held")

    def mark_applied(self, event: dict) -> None:
        self.applied[event["event_id"]] = {
            "card_id": event["card_id"],
            "answered_at_ms": event["answered_at_ms"],
        }
        write_json(self.applied_path, {"format_version": 1, "applied": self.applied})

    def mark_held(self, event_id: str) -> int:
        self.held[event_id] = self.held.get(event_id, 0) + 1
        self._write_held()
        return self.held[event_id]

    def settle(self, event_id: str) -> None:
        if self.held.pop(event_id, None) is not None:
            self._write_held()

    def _write_held(self) -> None:
        write_json(self.held_path, {"format_version": 1, "held": self.held})


def _read_record(state_dir: Path, path: Path, key: str) -> dict:
    """One of the add-on's own records. A damaged one is logged and read as empty;
    Anki's review history still guards against applying an answer twice."""
    try:
        data = read_json(path)
    except (ValueError, OSError):
        data = "damaged"
    if data is None:
        return {}
    record = data.get(key) if isinstance(data, dict) else None
    if not isinstance(record, dict):
        log(state_dir, "record_unreadable", file=path.name)
        return {}
    if key == "held":
        record = {k: v for k, v in record.items() if _is_int(v)}
    return record


def pending_events(exchange_dir: Path, state_dir: Path | None = None) -> list:
    """The review events waiting in the exchange folder, as written.

    A file that cannot be understood holds no events; that is logged.
    """
    try:
        data = read_json(exchange_dir / EVENTS_NAME)
    except (ValueError, OSError):
        data = "damaged"
    if data is None:
        return []
    events = data.get("events") if isinstance(data, dict) else None
    if _version(data) != EVENTS_FORMAT_VERSION or not isinstance(events, list):
        if state_dir is not None:
            log(state_dir, "events_unreadable")
        return []
    return events


def _version(data):
    version = data.get("format_version") if isinstance(data, dict) else None
    return version if _is_int(version) else None


def replay(
    col: Collection,
    exchange_dir: Path,
    state_dir: Path,
    now_ms: int | None = None,
    request_id: str | None = None,
) -> ReplayResult:
    """Replay the waiting review events, oldest answer first, and write the results.

    Safe to interrupt and to run again: an event already applied is recognised
    by the applied log and by Anki's review history, each on its own.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    events = pending_events(exchange_dir, state_dir)
    records = _Records(state_dir)
    result = ReplayResult()
    queue_cleared = False

    def order(indexed):
        index, event = indexed
        answered = event.get("answered_at_ms") if isinstance(event, dict) else None
        return (answered if _is_int(answered) else 0, index)

    for _, event in sorted(enumerate(events), key=order):
        try:
            outcome, reason = _settled_without_answering(col, state_dir, records, event, now_ms)
        except Exception as e:
            # One event that cannot be judged must not stop the others.
            # It is held, not dropped: the fault may be the add-on's, not the event's.
            log(state_dir, "event_error", kind=type(e).__name__)
            outcome, reason = _hold(records, event, ERROR)
        if outcome is None:
            if not queue_cleared:
                _clear_study_queue(col)
                queue_cleared = True
            try:
                _answer(col, event)
            except Exception as e:
                # The error's text may quote a card, so only its kind is logged.
                log(state_dir, "refused", event=event["event_id"], kind=type(e).__name__)
                outcome, reason = _hold(records, event, ANKI_REFUSED)
            else:
                # Into the applied log before the next event is handled.
                records.mark_applied(event)
                outcome, reason = APPLIED, None
        event_id = event.get("event_id") if isinstance(event, dict) else None
        card_id = event.get("card_id") if isinstance(event, dict) else None
        if not (isinstance(event_id, str) and _ID.fullmatch(event_id)):
            event_id = None
        if not _is_int(card_id):
            card_id = None
        if outcome != HELD and event_id is not None:
            records.settle(event_id)
        result.results.append({"event_id": event_id, "outcome": outcome, "reason": reason})
        log(state_dir, "replay", event=event_id, card=card_id, outcome=outcome, reason=reason)

    _write_results(exchange_dir, state_dir, result, now_ms, request_id)
    return result


def _hold(records: "_Records", event, reason: str) -> tuple[str, str]:
    """Hold an event for another desktop cycle, or drop it once it has been held in three."""
    event_id = event.get("event_id") if isinstance(event, dict) else None
    if not (isinstance(event_id, str) and _ID.fullmatch(event_id)):
        return DROPPED, UNKNOWN_FORMAT
    if records.mark_held(event_id) >= MAX_CYCLES_HELD:
        return DROPPED, HELD_TOO_LONG
    return HELD, reason


def hold_all(
    exchange_dir: Path, state_dir: Path, reason: str, request_id: str | None = None
) -> ReplayResult:
    """Report every waiting review event as held, without touching the collection.

    Used when replay must not run at all. These holds do not count towards the
    three after which an event is dropped: Anki has refused nothing.
    """
    result = ReplayResult()
    for event in pending_events(exchange_dir, state_dir):
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not (isinstance(event_id, str) and _ID.fullmatch(event_id)):
            event_id = None
        result.results.append({"event_id": event_id, "outcome": HELD, "reason": reason})
        log(state_dir, "replay", event=event_id, outcome=HELD, reason=reason)
    _write_results(exchange_dir, state_dir, result, int(time.time() * 1000), request_id)
    return result


def _write_results(
    exchange_dir: Path, state_dir: Path, result: ReplayResult, now_ms: int, request_id: str | None
) -> None:
    write_json(
        exchange_dir / RESULTS_NAME,
        {
            "format_version": RESULTS_FORMAT_VERSION,
            "request_id": request_id,
            "written_at_ms": int(time.time() * 1000),
            "counts": result.counts,
            "results": result.results,
        },
    )
    update_status(exchange_dir, last_replay={"at_ms": now_ms, **result.counts})
    log(state_dir, "replay_done", **result.counts)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _settled_without_answering(col, state_dir: Path, records: _Records, event, now_ms: int):
    """(outcome, reason) for an event that must not be answered, or (None, None)."""
    if _version(event) != EVENT_FORMAT_VERSION:
        return DROPPED, UNKNOWN_FORMAT
    event_id, batch_id = event.get("event_id"), event.get("batch_id")
    card_id, answered, taken = (
        event.get("card_id"), event.get("answered_at_ms"), event.get("time_taken_ms")
    )
    well_formed = (
        isinstance(event_id, str)
        and _ID.fullmatch(event_id)
        and isinstance(batch_id, str)
        and _ID.fullmatch(batch_id)
        and _is_int(card_id)
        and 0 < card_id < 2**63
        and _is_int(answered)
        and 0 < answered < 2**62
        and _is_int(taken)
        and 0 <= taken <= _MAX_TIME_TAKEN_MS
    )
    if not well_formed:
        return DROPPED, UNKNOWN_FORMAT
    if not isinstance(event.get("rating"), str) or event["rating"] not in _RATINGS:
        return DROPPED, BAD_RATING
    try:
        batch = read_json(batch_record_path(state_dir, batch_id))
    except (ValueError, OSError):
        batch = None
    counts = batch.get("review_counts") if isinstance(batch, dict) else None
    if not isinstance(counts, dict) or not _is_int(batch.get("exported_at_ms")):
        return DROPPED, UNKNOWN_BATCH
    count_at_export = counts.get(str(card_id))
    if not _is_int(count_at_export):
        return DROPPED, NOT_IN_BATCH

    in_history = _in_review_history(col, event)
    if event_id in records.applied:
        # Applied before. If it has gone from the history, the user undid it
        # in Anki, and it stays undone.
        return APPLIED, (ALREADY_APPLIED if in_history else UNDONE_IN_ANKI)
    if in_history:
        records.mark_applied(event)
        return APPLIED, ALREADY_APPLIED
    # An answer cannot come from before its batch or from the future.
    if not batch["exported_at_ms"] - CLOCK_SLACK_MS <= answered <= now_ms + CLOCK_SLACK_MS:
        return DROPPED, BAD_TIME
    try:
        card = col.get_card(card_id)
    except NotFoundError:
        return DROPPED, CARD_MISSING
    if card.reps != count_at_export:
        return DROPPED, REVIEWED_ELSEWHERE  # desktop wins
    if card.type != CARD_TYPE_REV or card.queue != QUEUE_TYPE_REV or card.odid:
        # Suspended, buried, reset or put in a filtered deck at the Mac since
        # export: what was done at the Mac stands.
        return DROPPED, CHANGED_SINCE_EXPORT
    return None, None


def _in_review_history(col: Collection, event: dict) -> bool:
    at = event["answered_at_ms"]
    return bool(
        col.db.list(
            "select id from revlog where cid = ? and id between ? and ?",
            event["card_id"],
            at - HISTORY_MATCH_MS,
            at + HISTORY_MATCH_MS,
        )
    )


def _clear_study_queue(col: Collection) -> None:
    """Make Anki forget any study queue it has built.

    Anki refuses to answer a card that is not at the top of a built queue.
    Switching the selected deck away and back is what clears it.
    """
    selected = col.decks.selected()
    other = next((d.id for d in col.decks.all_names_and_ids() if d.id != selected), None)
    if other is not None:
        col.decks.select(other)
        col.decks.select(selected)


def _answer(col: Collection, event: dict) -> None:
    card = col.get_card(event["card_id"])
    states = col._backend.get_scheduling_states(card.id)
    card.start_timer()
    answer = col.sched.build_answer(card=card, states=states, rating=_RATINGS[event["rating"]])
    answer.answered_at_millis = event["answered_at_ms"]
    answer.milliseconds_taken = event["time_taken_ms"]
    col.sched.answer_card(answer)
