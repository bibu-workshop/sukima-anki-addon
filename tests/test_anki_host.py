"""What the add-on tells the desktop cycle about Anki's syncs (AK-14).

The Anki-facing module needs Anki's window, which the tests do not have, so
the window is stood in for. The collections and the syncs are real: two
collections sync through Anki's own sync server, run on this machine.
"""

import os
import socket
import subprocess
import sys
import time
import types

import pytest

from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer
from conftest import Deck

FULL_SYNC = 2  # anki.sync.SyncOutput: the user must choose to upload or download


@pytest.fixture
def anki_ui(monkeypatch):
    """The Anki-facing module, imported with stand-ins for Anki's window."""
    def module(name, **names):
        m = types.ModuleType(name)
        for key, value in names.items():
            setattr(m, key, value)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    sync = module("aqt.sync", handle_sync_error=None, full_sync=None)
    module("aqt", sync=sync, gui_hooks=None, mw=None)
    module("aqt.qt", QAction=None, QInputDialog=None, QTimer=None)
    module("aqt.utils", tooltip=None)
    monkeypatch.delitem(sys.modules, "sukima.anki_ui", raising=False)
    import sukima.anki_ui as ui
    yield ui
    sys.modules.pop("sukima.anki_ui", None)


@pytest.fixture
def sync_server(tmp_path):
    """Anki's own sync server on this machine. Returns its address."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, SYNC_USER1="test:test", SYNC_BASE=str(tmp_path / "server"),
               SYNC_HOST="127.0.0.1", SYNC_PORT=str(port), RUST_LOG="error")
    server = subprocess.Popen([sys.executable, "-m", "anki.syncserver"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Anki's sync server did not start")
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.terminate()
        server.wait()


def full_sync(col, auth, upload):
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
    col.reopen(after_full_sync=True)


def answer(col, card):
    states = col._backend.get_scheduling_states(card.id)
    card.start_timer()
    col.sched.answer_card(col.sched.build_answer(card=card, states=states, rating=CardAnswer.GOOD))


class Mac:
    """A synced collection on the Mac, and another device that syncs with the same account."""

    def __init__(self, ui, monkeypatch, tmp_path, endpoint):
        self.col = Collection(str(tmp_path / "mac.anki2"))
        self.cards = [Deck(self.col, "Watch").review(f"front {i}") for i in range(2)]
        self.auth = self.col.sync_login("test", "test", endpoint)
        full_sync(self.col, self.auth, upload=True)
        self.other = Collection(str(tmp_path / "other.anki2"))
        self.other_auth = self.other.sync_login("test", "test", endpoint)
        full_sync(self.other, self.other_auth, upload=False)
        assert self.col.sync_collection(self.auth, False).required == 0

        window = types.SimpleNamespace(col=self.col, pm=types.SimpleNamespace(sync_auth=lambda: self.auth))
        monkeypatch.setattr(ui, "mw", window)
        self.host = ui.AnkiHost()

    def other_device_uploads_a_full_sync(self):
        # Anki takes two collections last changed in the same millisecond to
        # be in step. Real devices are never that close; a test can be.
        time.sleep(0.01)
        notetype = self.other.models.by_name("Basic")
        self.other.models.add_field(notetype, self.other.models.new_field("Extra"))
        self.other.models.update_dict(notetype)
        full_sync(self.other, self.other_auth, upload=True)

    def anki_syncs(self):
        """One of Anki's syncs, as the add-on hears of it. The user cancels a full-sync question."""
        self.host.note_sync(running=True)
        required = self.col.sync_collection(self.auth, False).required
        if required != 0:
            self.host.note_full_sync()
        self.host.note_sync(running=False)
        return required

    def close(self):
        self.col.close()
        self.other.close()


@pytest.fixture
def mac(anki_ui, monkeypatch, tmp_path, sync_server):
    m = Mac(anki_ui, monkeypatch, tmp_path, sync_server)
    yield m
    m.close()


def test_a_full_sync_asked_for_is_wanted_although_the_mac_has_answers_of_its_own_to_send(mac):
    """Anki's sync status usually reads "normal sync" here, so it cannot be what decides."""
    answer(mac.col, mac.cards[0])              # the user has been reviewing on the Mac
    mac.other_device_uploads_a_full_sync()
    assert not mac.host.full_sync_wanted()     # nothing on the Mac knows of it yet

    assert mac.anki_syncs() == FULL_SYNC       # the user syncs by hand, and cancels the question

    # In about one run in twenty the status reads "full sync" after all; what the sync asked for decides.
    assert mac.host.full_sync_asked
    assert mac.host.full_sync_wanted()


def test_a_full_sync_asked_for_is_wanted_when_the_mac_has_nothing_to_send(mac):
    mac.other_device_uploads_a_full_sync()
    assert mac.anki_syncs() == FULL_SYNC
    assert mac.host.full_sync_wanted()


def test_a_full_sync_is_no_longer_wanted_once_a_later_sync_goes_through(mac):
    answer(mac.col, mac.cards[0])
    mac.other_device_uploads_a_full_sync()
    mac.anki_syncs()
    full_sync(mac.col, mac.auth, upload=False)  # the user settles it by downloading
    assert mac.host.full_sync_wanted(), "not until one of Anki's syncs has gone through"

    assert mac.anki_syncs() == 0
    assert not mac.host.full_sync_wanted()


def test_a_change_on_the_mac_that_needs_a_full_sync_is_wanted_without_any_sync(mac):
    time.sleep(0.01)  # not in the same millisecond as the last sync; see other_device_uploads_a_full_sync
    notetype = mac.col.models.by_name("Basic")
    mac.col.models.add_field(notetype, mac.col.models.new_field("Extra"))
    mac.col.models.update_dict(notetype)
    assert mac.host.full_sync_wanted()


def test_no_full_sync_is_wanted_after_an_ordinary_sync_or_with_answers_waiting_to_be_sent(mac):
    assert mac.anki_syncs() == 0
    assert not mac.host.full_sync_wanted()
    answer(mac.col, mac.cards[0])
    assert not mac.host.full_sync_wanted()


def test_a_full_sync_asked_for_is_still_wanted_after_a_later_sync_fails(mac):
    """A sync that fails, on the network or otherwise, says nothing about what AnkiWeb wants."""
    answer(mac.col, mac.cards[0])
    mac.other_device_uploads_a_full_sync()
    mac.anki_syncs()
    mac.host.note_sync(running=True)
    mac.host.note_sync_error()
    mac.host.note_sync(running=False)
    assert mac.host.full_sync_wanted()


def test_a_full_sync_asked_for_again_by_the_next_sync_is_still_wanted(mac):
    answer(mac.col, mac.cards[0])
    mac.other_device_uploads_a_full_sync()
    mac.anki_syncs()
    assert mac.anki_syncs() == FULL_SYNC
    assert mac.host.full_sync_wanted()


def test_a_full_sync_forced_before_the_add_on_loaded_is_heard_at_the_next_sync(mac, anki_ui):
    """While the Mac has answers to send, Anki's status may read "normal sync", so a sync must tell.
    The cycle replays only after one."""
    answer(mac.col, mac.cards[0])
    mac.other_device_uploads_a_full_sync()
    assert mac.col.sync_collection(mac.auth, False).required == FULL_SYNC  # heard by no add-on
    mac.host = anki_ui.AnkiHost()                                         # Anki opens again
    assert mac.anki_syncs() == FULL_SYNC
    assert mac.host.full_sync_wanted()


def test_a_full_sync_asked_for_under_one_profile_is_forgotten_with_it(mac):
    mac.other_device_uploads_a_full_sync()
    mac.anki_syncs()
    mac.host.note_sync_error()
    mac.host.forget_syncs()
    assert not mac.host.full_sync_asked and not mac.host.sync_errored


def test_a_sync_status_that_cannot_be_read_counts_as_no_full_sync_wanted(mac, monkeypatch):
    """Just after a sync in that went through. The sync out that follows would ask again."""
    def offline(auth):
        raise TimeoutError("no network")
    monkeypatch.setattr(mac.col, "sync_status", offline)
    assert not mac.host.full_sync_wanted()
