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
from aqt.qt import (
    QAction,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QEvent,
    QFormLayout,
    QLabel,
    QObject,
    QPalette,
    Qt,
    QTimer,
    QVBoxLayout,
)
from aqt.utils import openLink, tooltip

from . import wording

from . import cycle as cycle_module
from .cycle import DesktopCycle, parse_anki_version, profile_opened
from .export import export_batch
from .log import log
from .settings import EXCHANGE_DIR, choose_deck, chosen_deck_id, state_dir_for, watch_decks

POLL_MS = 3000
# A scheduled cycle waits until Anki has not been used for this long (DEC-156).
QUIET_MS = 60_000


def busy_reason(*, reviewing: bool, open_windows: list[str], last_used_ms: int, now_ms: int) -> str | None:
    """Why a scheduled cycle should not start yet, or None when Anki is quiet (DEC-156).

    `window_open` names the windows, for the log: `window_open:Browser,AddCards`.
    """
    if reviewing:
        return "review_screen"
    if open_windows:
        return "window_open:" + ",".join(sorted(set(open_windows)))
    if now_ms - last_used_ms < QUIET_MS:
        return "in_use"
    return None


def open_windows(main, top_level, modal) -> list[str]:
    """The kinds of window open besides Anki's main one: dialogs, the Browser,
    Add, Edit, options, any modal. Tooltips, menus and the like do not count."""
    passing = {Qt.WindowType.ToolTip, Qt.WindowType.Popup, Qt.WindowType.SplashScreen}
    names = []
    if modal is not None and modal is not main:
        names.append(type(modal).__name__)
    for widget in top_level:
        if widget is main or widget is modal or not widget.isVisible():
            continue
        if widget.windowType() in passing:
            continue
        names.append(type(widget).__name__)
    return names
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
        # Opening Anki counts as using it: a scheduled cycle never opens it (DEC-130).
        self.last_used_ms = self.now_ms()

    def note_use(self) -> None:
        """A key, click or scroll in one of Anki's windows."""
        self.last_used_ms = self.now_ms()

    def anki_busy(self) -> str | None:
        return busy_reason(
            reviewing=self.on_review_screen(),
            open_windows=open_windows(mw, QApplication.topLevelWidgets(), QApplication.activeModalWidget()),
            last_used_ms=self.last_used_ms,
            now_ms=self.now_ms(),
        )

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
        if mw.col:
            try:
                profile_opened(mw.col, EXCHANGE_DIR, self.state_dir)
            except Exception as e:
                log(self.state_dir, "profile_opened_error", kind=type(e).__name__)

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
        if not decks:
            return
        current = chosen_deck_id(mw.col, self.state_dir)
        index = next((i for i, (deck_id, _) in enumerate(decks) if deck_id == current), 0)
        picked = deck_dialog(mw, [name for _, name in decks], index)
        if picked is None:
            return
        deck_id, name = decks[picked]
        choose_deck(mw.col, self.state_dir, deck_id)
        result = export_batch(mw.col, deck_id, EXCHANGE_DIR, self.state_dir)
        tooltip(wording.chosen_tooltip(name, result.today_ready, result.today_not_shown))


def deck_dialog(parent, names: list[str], index: int) -> int | None:
    """The "Apple Watch Deck" dialog (design guide 1.18, `Addon-DeckDialog`): a plain Qt
    dialog in Anki's own look. Returns the index of the deck chosen, or None on Cancel."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(wording.DIALOG_TITLE)
    layout = QVBoxLayout(dialog)

    lead = QLabel(wording.DIALOG_LEAD)
    font = lead.font()
    font.setBold(True)
    lead.setFont(font)
    lead.setWordWrap(True)
    layout.addWidget(lead)

    menu = QComboBox()
    menu.addItems(names)
    menu.setCurrentIndex(index)
    form = QFormLayout()
    form.addRow(wording.DECK_LABEL, menu)
    layout.addLayout(form)

    # The two paragraphs in the secondary text colour, as Anki's theme sets it.
    secondary = dialog.palette().color(QPalette.ColorRole.PlaceholderText).name()
    for text in (wording.WHICH_CARDS, wording.one_deck_paragraph()):
        paragraph = QLabel(text)
        paragraph.setWordWrap(True)
        paragraph.setStyleSheet(f"color: {secondary};")
        layout.addWidget(paragraph)

    link = QLabel(f'<a href="{wording.HELP_URL}">{wording.HELP_LINK}</a>')
    link.setTextFormat(Qt.TextFormat.RichText)
    link.linkActivated.connect(openLink)
    layout.addWidget(link)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(420)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return menu.currentIndex()


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


def _watch_use(host: AnkiHost) -> None:
    """Learn when Anki was last used: a key, click or scroll in any of its windows.
    Only notes the time; every event goes on unchanged."""
    used = {QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel}

    class UseWatcher(QObject):
        def eventFilter(self, obj, event):  # noqa: N802 (Qt's name)
            if event.type() in used:
                host.note_use()
            return False

    watcher = UseWatcher(mw)
    QApplication.instance().installEventFilter(watcher)


def start() -> None:
    addon = _AddOn()
    _watch_ankis_sync(addon.host)
    _watch_use(addon.host)
    gui_hooks.profile_did_open.append(addon.profile_opened)
    gui_hooks.profile_will_close.append(addon.profile_closing)
    gui_hooks.sync_will_start.append(addon.sync_started)
    gui_hooks.sync_did_finish.append(addon.sync_finished)
    action = QAction(wording.MENU_ITEM, mw)
    action.triggered.connect(addon.choose_watch_deck)
    mw.form.menuTools.addAction(action)
