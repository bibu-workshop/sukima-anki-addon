# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desktop cycle: Anki sync in, backup, replay, export, Anki sync out.

The order of steps and the reasons to stop live here and need no Anki window.
What only the running app can do (sync, backup, which screen is open) comes
through a Host. The add-on asks Anki to sync; it contacts nothing itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from anki.collection import Collection

from .export import export_batch
from .files import read_json, write_json
from .log import log
from .replay import hold_all, pending_events, replay
from .settings import chosen_deck_id
from .status import update_status
from .version import MAX_TESTED_ANKI

REQUEST_NAME = "cycle-request.json"
REQUEST_FORMAT_VERSION = 1

# Results of asking Anki whether a sync left anything behind.
SYNC_OK = "ok"
SYNC_FAILED = "failed"
SYNC_FULL_NEEDED = "full_sync_needed"
SYNC_SKIPPED = "skipped"
SYNC_TIMED_OUT = "timed_out"
SYNC_NOT_STARTED = "not_started"

# Outcomes of a desktop cycle.
DONE = "done"
STOPPED_SYNC_FAILED = "sync_failed"
STOPPED_FULL_SYNC = "full_sync_needed"
STOPPED_ERROR = "error"

# Reasons every review event is held.
ANKI_NEWER_THAN_TESTED = "anki_newer_than_tested"
NO_DECK_CHOSEN = "no_deck_chosen"
BACKUP_FAILED = "backup_failed"

# A sync of Anki's own that finished after the request was made, and this
# recently, counts as this cycle's sync in: the one Anki runs on opening.
RECENT_SYNC_MS = 60_000
SYNC_TIMEOUT_MS = 5 * 60_000

_IDLE = "idle"
_WANTS_SYNC_IN = "wants_sync_in"  # waiting to be able to ask Anki for the sync in
_SYNCING_IN = "syncing_in"
_WANTS_TO_WORK = "wants_to_work"  # synced in; waiting for the user to leave the review screen
_WANTS_SYNC_OUT = "wants_sync_out"
_SYNCING_OUT = "syncing_out"


class Host(Protocol):
    """What the desktop cycle needs from the running Anki app."""

    def now_ms(self) -> int: ...
    def anki_version(self) -> tuple[int, int]: ...
    def sync_enabled(self) -> bool:
        """Logged in, with automatic sync turned on."""
    def sync_running(self) -> bool: ...
    def last_sync_finished_ms(self) -> int | None: ...
    def start_sync(self) -> bool:
        """Ask Anki to sync, as its Sync button does. False if Anki cannot start one now."""
    def sync_result(self) -> str:
        """After a sync: SYNC_OK, SYNC_FAILED or SYNC_FULL_NEEDED.

        Must report SYNC_FAILED for a sync that ended in an error, whatever
        Anki's sync status says afterwards."""
    def full_sync_wanted(self) -> bool:
        """Whether Anki now says the collection needs a full upload or download."""
    def sync_detail(self) -> str:
        """What the last sync_result was based on, for the log. No card content."""
    def on_review_screen(self) -> bool: ...
    def backup(self) -> bool:
        """Ask Anki for a backup and wait for it. False if it did not complete."""
    def refresh(self) -> None:
        """Have Anki's window show the collection as it now is: counts, and Undo."""


def anki_is_newer_than_tested(version: tuple[int, int]) -> bool:
    return tuple(version[:2]) > MAX_TESTED_ANKI


def parse_anki_version(text: str) -> tuple[int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", text)[:2]]
    return (numbers + [0, 0])[0], (numbers + [0, 0])[1]


class DesktopCycle:
    """Runs desktop cycles for one collection.

    `poll(col)` is called every few seconds while Anki is open, and
    `sync_finished(col)` whenever one of Anki's syncs ends. Each moves the cycle
    on as far as it can go without waiting.
    """

    def __init__(self, host: Host, exchange_dir: Path, state_dir: Path) -> None:
        self.host = host
        self.exchange_dir = exchange_dir
        self.state_dir = state_dir
        self.state = _IDLE
        self.request_id: str | None = None
        self.requested_at = 0
        self.failed_request_id: str | None = None
        self.waiting_since = 0
        self.noted: str | None = None
        self.report: dict = {}

    # -- what moves the cycle on -------------------------------------------

    def poll(self, col: Collection) -> None:
        try:
            self._poll(col)
        except Exception as e:
            self._failed(e)

    def sync_finished(self, col: Collection) -> None:
        if self.state in (_SYNCING_IN, _SYNCING_OUT):
            try:
                self._sync_done(col, self.host.sync_result())
            except Exception as e:
                self._failed(e)

    def _poll(self, col: Collection) -> None:
        if self.state == _IDLE:
            request = self._new_request()
            if request is None:
                return
            self.request_id, self.requested_at = request
            self.report = {"request_id": self.request_id, "started_at_ms": self.host.now_ms()}
            self._log("cycle_start", request=self.request_id)
            self._wait(_WANTS_SYNC_IN)
            self._sync_in(col)
        elif self.state == _WANTS_SYNC_IN:
            self._sync_in(col)
        elif self.state == _WANTS_TO_WORK:
            self._work(col)
        elif self.state == _WANTS_SYNC_OUT:
            self._sync_out()
        elif self.state in (_SYNCING_IN, _SYNCING_OUT):
            if self._waited_too_long():
                self._sync_done(col, SYNC_TIMED_OUT)

    # -- the steps ----------------------------------------------------------

    def _sync_in(self, col: Collection) -> None:
        host = self.host
        if not host.sync_enabled():
            self._log("sync_in", result=SYNC_SKIPPED)
            self.report["sync_in"] = SYNC_SKIPPED
            self._work(col)
            return
        if host.sync_running():
            self._wait(_SYNCING_IN)
            return
        finished = host.last_sync_finished_ms()
        if (
            finished is not None
            and finished >= self.requested_at
            and 0 <= host.now_ms() - finished <= RECENT_SYNC_MS
        ):
            # Anki has synced by itself since the request was made: on opening.
            self.state = _SYNCING_IN
            self._sync_done(col, host.sync_result())
            return
        if host.on_review_screen():
            # The wait on the review screen has no limit; the limits on what
            # follows it start counting when the user leaves.
            self.waiting_since = host.now_ms()
            self._note_waiting("waiting_to_leave_review")
            return
        if host.start_sync():
            self._wait(_SYNCING_IN)
        elif self._waited_too_long():
            self.state = _SYNCING_IN
            self._sync_done(col, SYNC_NOT_STARTED)

    def _wait(self, state: str) -> None:
        self.state = state
        self.waiting_since = self.host.now_ms()
        self.noted = None

    def _waited_too_long(self) -> bool:
        return self.host.now_ms() - self.waiting_since > SYNC_TIMEOUT_MS

    def _note_waiting(self, what: str) -> None:
        if self.noted != what:
            self._log(what)
            self.noted = what

    def _sync_done(self, col: Collection, result: str) -> None:
        if self.state == _SYNCING_IN:
            self._log("sync_in", result=result, detail=self.host.sync_detail())
            self.report["sync_in"] = result
            if result == SYNC_OK:
                self._wait(_WANTS_TO_WORK)
                self._work(col)
            else:
                # Nothing is replayed. The review events stay queued.
                outcome = STOPPED_FULL_SYNC if result == SYNC_FULL_NEEDED else STOPPED_SYNC_FAILED
                self._finish(outcome)
        else:
            self._log("sync_out", result=result, detail=self.host.sync_detail())
            self.report["sync_out"] = result
            self._finish(DONE)

    def _work(self, col: Collection) -> None:
        """Version check, backup, replay and export. Then sync out."""
        host = self.host
        if self.state != _WANTS_TO_WORK:
            self._wait(_WANTS_TO_WORK)
        if host.on_review_screen():
            # Whatever led here, nothing is replayed under a review in progress.
            # This wait has no limit.
            self.waiting_since = host.now_ms()
            self._note_waiting("waiting_to_leave_review")
            return
        if host.sync_running():
            # Nor while Anki is in the middle of a sync of its own. If that
            # sync never reports its end, the cycle stops rather than hang.
            if self._waited_too_long():
                self._log("work", result="stopped_behind_a_sync_that_did_not_end")
                self._finish(STOPPED_SYNC_FAILED)
            return
        if self.report.get("sync_in") == SYNC_SKIPPED and host.sync_enabled():
            # Sync was off when the cycle began and has been turned on since:
            # reviews from other devices must come in before anything is replayed.
            self._log("sync_in", result="wanted_after_all")
            self._wait(_WANTS_SYNC_IN)
            self._sync_in(col)
            return
        if host.sync_enabled() and host.full_sync_wanted():
            # A sync since the sync in has left Anki wanting a full upload or
            # download. A download would throw away anything replayed now.
            self._log("work", result="stopped_full_sync_wanted")
            self._finish(STOPPED_FULL_SYNC)
            return
        waiting = len(pending_events(self.exchange_dir, self.state_dir))
        deck_id = chosen_deck_id(col, self.state_dir)
        held_because = None
        if deck_id is None:
            # This profile is not set up for the watch. The answers may belong
            # to another profile, so they are kept, not judged here.
            held_because = NO_DECK_CHOSEN
            self._log("deck_check", result=held_because)
        elif anki_is_newer_than_tested(host.anki_version()):
            held_because = ANKI_NEWER_THAN_TESTED
            self._log("version_check", result=held_because)
        elif waiting:
            backed_up = host.backup()
            self._log("backup", result="ok" if backed_up else "failed")
            if not backed_up:
                held_because = BACKUP_FAILED

        if held_because:
            result = hold_all(self.exchange_dir, self.state_dir, held_because, self.request_id)
        else:
            result = replay(
                col, self.exchange_dir, self.state_dir, now_ms=host.now_ms(),
                request_id=self.request_id,
            )
        self.report.update(result.counts, held_because=held_because)

        export = export_batch(col, deck_id, self.exchange_dir, self.state_dir, now_ms=host.now_ms())
        self.report["export"] = export.outcome
        # So the deck list shows the new counts, and Undo offers the replayed answers.
        host.refresh()

        if host.sync_enabled():
            self._wait(_WANTS_SYNC_OUT)
            self._sync_out()
        else:
            self._log("sync_out", result=SYNC_SKIPPED)
            self.report["sync_out"] = SYNC_SKIPPED
            self._finish(DONE)

    def _sync_out(self) -> None:
        """Ask Anki to sync, as soon as it can start one."""
        host = self.host
        if not host.sync_running() and host.start_sync():
            self._wait(_SYNCING_OUT)
        elif self._waited_too_long():
            # The answers are applied and the batch exported; Anki will sync them later.
            self._log("sync_out", result=SYNC_NOT_STARTED)
            self.report["sync_out"] = SYNC_NOT_STARTED
            self._finish(DONE)

    def _failed(self, error: Exception) -> None:
        """Something went wrong inside a step. Say so and be done with the request,
        so it is not tried again on every poll. Replay is safe to repeat on the next one."""
        try:
            # The error's text may quote a card, so only its kind is logged.
            self._log("cycle_error", kind=type(error).__name__, state=self.state)
            self.report["error"] = type(error).__name__
            self._finish(STOPPED_ERROR)
        except Exception:
            self.state = _IDLE
            self.failed_request_id = self.request_id
            self.request_id = None

    def _finish(self, outcome: str) -> None:
        self.report.update(outcome=outcome, finished_at_ms=self.host.now_ms())
        update_status(self.exchange_dir, last_cycle=self.report)
        # Only now is the request done with. A cycle cut short before this
        # point runs again; replay is safe to repeat.
        write_json(self.state_dir / "cycle.json", {"format_version": 1, "finished": self.request_id})
        self._log("cycle_end", request=self.request_id, outcome=outcome)
        self.state = _IDLE
        self.request_id = None

    # -- helpers --------------------------------------------------------------

    def _new_request(self) -> tuple[str, int] | None:
        """A cycle request that has not been finished, as (ID, when it was made)."""
        try:
            request = read_json(self.exchange_dir / REQUEST_NAME)
            finished = read_json(self.state_dir / "cycle.json")
        except (ValueError, OSError):
            return None
        if not isinstance(request, dict) or request.get("format_version") != REQUEST_FORMAT_VERSION:
            return None
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id):
            return None
        if isinstance(finished, dict) and finished.get("finished") == request_id:
            return None
        if request_id == self.failed_request_id:
            return None
        requested_at = request.get("requested_at_ms")
        if isinstance(requested_at, bool) or not isinstance(requested_at, int):
            requested_at = self.host.now_ms()
        return request_id, requested_at

    def _log(self, what: str, **fields) -> None:
        log(self.state_dir, what, **fields)
