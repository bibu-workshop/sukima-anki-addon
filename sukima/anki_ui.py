# SPDX-License-Identifier: AGPL-3.0-or-later
"""The part of the add-on that needs Anki's window. Kept thin.

It tells the desktop cycle when to move on, does for it what only the running
app can do, and offers the deck chooser. Anki syncs and backs up; this asks.
"""

from __future__ import annotations

import time
from pathlib import Path

import aqt.sync
from anki.buildinfo import version as anki_version
from aqt import gui_hooks, mw
from aqt.qt import QAction, QInputDialog, QTimer
from aqt.utils import tooltip

from . import cycle as cycle_module
from .cycle import DesktopCycle, parse_anki_version
from .export import export_batch
from .log import log
from .settings import EXCHANGE_DIR, choose_deck, chosen_deck_id, state_dir_for, watch_decks

POLL_MS = 3000
USER_FILES = Path(__file__).resolve().parent / "user_files"


class AnkiHost:
    """What the desktop cycle needs from the running app."""

    def __init__(self) -> None:
        self.syncing = False
        self.sync_finished_ms: int | None = None
        self.sync_errored = False
        self.full_sync_asked = False
        self.asked_in_this_sync = False
        self.detail = ""

    def sync_detail(self) -> str:
        return self.detail

    def note_sync(self, running: bool) -> None:
        """Called from Anki's sync hooks.

        A full sync asked for stands until one of Anki's syncs ends with no
        error and without asking again: in Anki 26.9.3 such a sync has gone
        through. A sync that fails says nothing about it.
        """
        self.syncing = running
        if running:
            self.sync_errored = False
            self.asked_in_this_sync = False
        else:
            self.sync_finished_ms = self.now_ms()
            if not (self.sync_errored or self.asked_in_this_sync):
                self.full_sync_asked = False

    def note_sync_error(self) -> None:
        self.sync_errored = True

    def note_full_sync(self) -> None:
        self.full_sync_asked = True
        self.asked_in_this_sync = True

    def forget_syncs(self) -> None:
        """A sync under one profile says nothing about the next."""
        self.sync_finished_ms = None
        self.sync_errored = False
        self.full_sync_asked = False
        self.asked_in_this_sync = False

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def anki_version(self) -> tuple[int, int]:
        return parse_anki_version(anki_version)

    def sync_enabled(self) -> bool:
        return bool(mw.pm.sync_auth()) and mw.pm.auto_syncing_enabled()

    def sync_running(self) -> bool:
        return self.syncing

    def last_sync_finished_ms(self) -> int | None:
        return self.sync_finished_ms

    def start_sync(self) -> bool:
        # While Anki is still syncing media, its Sync action shows the media
        # log and starts nothing.
        if self.syncing or mw.media_syncer.is_syncing():
            return False
        mw.on_sync_button_clicked()
        return True

    def sync_result(self) -> str:
        """How the sync that just ended went.

        A sync that showed an error, or asked for a full upload or download,
        is reported as such. Anki's sync status cannot say whether a sync
        failed: it is remembered for some minutes, so it can read "nothing to
        sync" after a failure, and after a full upload it reads "sync needed"
        although Anki's syncs go through and find nothing to do. It is used
        only to catch a full sync that is still wanted.
        """
        if self.sync_errored:
            self.detail = "anki_showed_a_sync_error"
            return cycle_module.SYNC_FAILED
        if self.full_sync_asked:
            self.detail = "anki_asked_for_a_full_sync"
            return cycle_module.SYNC_FULL_NEEDED
        try:
            required = mw.col.sync_status(mw.pm.sync_auth()).required
        except Exception as e:
            self.detail = f"sync_status_raised_{type(e).__name__}"
            return cycle_module.SYNC_FAILED
        self.detail = f"no_error_shown_and_sync_status_required_{required}"
        return cycle_module.SYNC_FULL_NEEDED if required == 2 else cycle_module.SYNC_OK

    def full_sync_wanted(self) -> bool:
        """Whether Anki's last sync asked for a full sync, or its status says one is needed.

        The status alone is not enough: while the collection has changes of
        its own to send it reads "normal sync", whatever AnkiWeb holds. What
        a sync asked for stands until a later one goes through (`note_sync`).
        The cycle replays only after a sync in this session, so a full sync
        AnkiWeb wants is asked for, and heard, before then.
        """
        if self.full_sync_asked:
            return True
        try:
            return mw.col.sync_status(mw.pm.sync_auth()).required == 2
        except Exception:
            return False

    def on_review_screen(self) -> bool:
        return mw.state == "review"

    def refresh(self) -> None:
        mw.reset()
        mw.update_undo_actions()

    def backup(self) -> bool:
        try:
            mw.create_backup_now()
        except Exception:
            return False
        return True


class _AddOn:
    def __init__(self) -> None:
        self.host = AnkiHost()
        self.cycle: DesktopCycle | None = None
        self.state_dir: Path | None = None
        self.timer = QTimer(mw)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)

    def profile_opened(self) -> None:
        self.state_dir = state_dir_for(USER_FILES, mw.pm.name)
        self.cycle = DesktopCycle(self.host, EXCHANGE_DIR, self.state_dir)
        log(self.state_dir, "profile_opened")

    def profile_closing(self) -> None:
        self.cycle = None
        self.state_dir = None
        self.host.forget_syncs()

    def sync_started(self) -> None:
        self.host.note_sync(running=True)

    def sync_finished(self) -> None:
        self.host.note_sync(running=False)
        if self.cycle and mw.col:
            self.cycle.sync_finished(mw.col)

    def poll(self) -> None:
        if self.cycle and mw.col:
            self.cycle.poll(mw.col)

    def choose_watch_deck(self) -> None:
        if not (mw.col and self.state_dir):
            return
        decks = watch_decks(mw.col)
        names = [name for _, name in decks]
        current = chosen_deck_id(mw.col, self.state_dir)
        index = next((i for i, (deck_id, _) in enumerate(decks) if deck_id == current), 0)
        name, ok = QInputDialog.getItem(
            mw, "Watch deck", "Deck to send to the watch:", names, index, False
        )
        if not ok or name not in names:
            return
        choose_deck(mw.col, self.state_dir, decks[names.index(name)][0])
        result = export_batch(
            mw.col, chosen_deck_id(mw.col, self.state_dir), EXCHANGE_DIR, self.state_dir
        )
        skipped = sum(result.skipped.values())
        tooltip(f"Watch deck: {name}. {result.exported} cards ready, {skipped} skipped.")


def _watch_ankis_sync(host: AnkiHost) -> None:
    """Learn when one of Anki's syncs ends in an error or asks for a full sync.

    Anki's hooks say only that a sync finished, so its own two functions for
    those cases are wrapped. They still do exactly what they did.
    """
    show_error = aqt.sync.handle_sync_error
    full_sync = aqt.sync.full_sync

    def noting_error(*args, **kwargs):
        host.note_sync_error()
        return show_error(*args, **kwargs)

    def noting_full_sync(*args, **kwargs):
        host.note_full_sync()
        return full_sync(*args, **kwargs)

    aqt.sync.handle_sync_error = noting_error
    aqt.sync.full_sync = noting_full_sync


def start() -> None:
    addon = _AddOn()
    _watch_ankis_sync(addon.host)
    gui_hooks.profile_did_open.append(addon.profile_opened)
    gui_hooks.profile_will_close.append(addon.profile_closing)
    gui_hooks.sync_will_start.append(addon.sync_started)
    gui_hooks.sync_did_finish.append(addon.sync_finished)
    action = QAction("Watch deck…", mw)
    action.triggered.connect(addon.choose_watch_deck)
    mw.form.menuTools.addAction(action)
