"""The desktop cycle, with a stand-in for what only the running Anki app can do
(AK-14, AK-15, AK-16, AK-17; NFR-009, NFR-010)."""

import json
import time

import pytest

from conftest import Deck
from test_replay import HOUR, event, history, write_events
from sukima.cycle import DesktopCycle, anki_is_newer_than_tested, parse_anki_version
from sukima.export import export_batch
from sukima.files import write_json
from sukima.settings import choose_deck, chosen_deck_id, state_dir_for, watch_decks


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class FakeAnki:
    """Stands in for the running app. Records what it was asked, in order."""

    def __init__(self, col):
        self.col = col
        self.clock = int(time.time() * 1000)
        self.version = (26, 9)
        self.enabled = True
        self.running = False
        self.last_finished = None
        self.result = "ok"
        self.reviewing = False
        self.backup_ok = True
        self.asked = []
        self.history_at_backup = None
        self.busy_with_media = False
        self.refreshed = 0
        self.full_wanted = False
        self.cycle = None

    def now_ms(self): return self.clock
    def anki_version(self): return self.version
    def sync_enabled(self): return self.enabled
    def sync_running(self): return self.running
    def last_sync_finished_ms(self): return self.last_finished
    def sync_result(self): return self.result
    def on_review_screen(self): return self.reviewing
    def sync_detail(self): return ""
    def full_sync_wanted(self): return self.full_wanted

    def start_sync(self):
        if self.busy_with_media:
            return False
        self.asked.append("sync")
        self.running = True
        return True

    def backup(self):
        self.asked.append("backup")
        self.history_at_backup = len(history(self.col))
        return self.backup_ok

    def refresh(self):
        self.refreshed += 1

    def finish_sync(self):
        """Anki's sync ends and its hook fires."""
        self.running = False
        self.last_finished = self.clock
        self.cycle.sync_finished(self.col)


class Mac:
    def __init__(self, col, deck, exchange, state):
        self.col, self.deck, self.exchange, self.state = col, deck, exchange, state
        self.cards = [deck.review(f"front {i}", overdue=10 - i) for i in range(6)]
        choose_deck(col, state, deck.id)
        self.batch_id = export_batch(col, deck.id, exchange, state, now_ms=int(time.time() * 1000) - 10 * HOUR).batch_id
        self.anki = FakeAnki(col)
        self.cycle = self.anki.cycle = DesktopCycle(self.anki, exchange, state)
        self.base = int(time.time() * 1000) - 5 * HOUR

    def watch_answers(self, n):
        events = [event(self.batch_id, c, at=self.base + i * 60_000) for i, c in enumerate(self.cards[:n])]
        write_events(self.exchange, events)
        return events

    def request(self, request_id="R-1"):
        write_json(self.exchange / "cycle-request.json",
                   {"format_version": 1, "request_id": request_id, "requested_at_ms": self.anki.clock})

    def run(self):
        """Poll, and let each sync Anki is asked for finish, until the cycle is idle."""
        for _ in range(6):
            self.cycle.poll(self.col)
            if self.anki.running:
                self.anki.finish_sync()
        return read(self.exchange / "status.json").get("last_cycle")

    def log(self):
        return [" ".join(l.split(" ")[1:]) for l in (self.state / "addon.log").read_text().splitlines()]

    def steps(self):
        names = ("cycle_start", "sync_in", "version_check", "backup", "replay_done", "export", "sync_out", "cycle_end")
        return [l.split(" ")[0] for l in self.log() if l.split(" ")[0] in names][1:]  # drop setup's export


@pytest.fixture
def mac(col, deck, exchange, state):
    return Mac(col, deck, exchange, state)


def test_a_normal_cycle_runs_every_step_in_order(mac):
    events = mac.watch_answers(3)
    mac.request()

    last = mac.run()

    assert mac.anki.asked == ["sync", "backup", "sync"]
    assert mac.steps() == ["cycle_start", "sync_in", "backup", "replay_done", "export", "sync_out", "cycle_end"]
    assert len(history(mac.col)) == 3
    assert last["request_id"] == "R-1" and last["outcome"] == "done"
    assert (last["applied"], last["dropped"], last["held"]) == (3, 0, 0)
    assert (last["sync_in"], last["sync_out"], last["export"]) == ("ok", "ok", "ok")
    # A fresh batch without the cards just answered.
    batch = read(mac.exchange / "batch.json")
    assert batch["batch_id"] != mac.batch_id
    assert not {e["card_id"] for e in events} & {c["card_id"] for c in batch["cards"]}
    assert len(batch["cards"]) == 3


def test_nothing_is_replayed_before_ankis_sync_in_has_finished(mac):
    mac.watch_answers(3)
    mac.request()
    mac.cycle.poll(mac.col)
    assert mac.anki.asked == ["sync"] and history(mac.col) == []
    mac.cycle.poll(mac.col)
    mac.cycle.poll(mac.col)
    assert mac.anki.asked == ["sync"] and history(mac.col) == []
    mac.anki.finish_sync()
    assert len(history(mac.col)) == 3


def test_the_backup_completes_before_the_first_answer(mac):
    mac.watch_answers(3)
    mac.request()
    mac.run()
    assert mac.anki.history_at_backup == 0
    assert mac.anki.asked.index("backup") == 1


def test_no_backup_is_asked_for_when_there_is_nothing_to_apply(mac):
    mac.request()
    last = mac.run()
    assert mac.anki.asked == ["sync", "sync"]
    assert last["outcome"] == "done" and last["applied"] == 0
    assert read(mac.exchange / "batch.json")["batch_id"] != mac.batch_id, "the batch is still refreshed"


def test_a_failed_backup_means_nothing_is_replayed(mac):
    events = mac.watch_answers(2)
    mac.anki.backup_ok = False
    mac.request()
    last = mac.run()
    assert history(mac.col) == []
    assert last["held"] == 2 and last["held_because"] == "backup_failed"
    assert [r["reason"] for r in read(mac.exchange / "results.json")["results"]] == ["backup_failed"] * 2


@pytest.mark.parametrize("result, outcome", [("failed", "sync_failed"), ("full_sync_needed", "full_sync_needed")])
def test_a_sync_in_that_fails_or_wants_a_full_sync_stops_the_cycle(mac, result, outcome):
    mac.watch_answers(3)
    mac.anki.result = result
    mac.request()

    last = mac.run()

    assert history(mac.col) == []
    assert mac.anki.asked == ["sync"], "no backup, no second sync"
    assert last["outcome"] == outcome and last["sync_in"] == result
    assert len(read(mac.exchange / "review-events.json")["events"]) == 3, "the review events stay queued"
    assert not (mac.exchange / "results.json").exists()
    assert read(mac.exchange / "batch.json")["batch_id"] == mac.batch_id, "no export either"
    assert f"sync_in result={result} detail=" in mac.log()
    assert f"cycle_end request=R-1 outcome={outcome}" in mac.log()


def test_after_a_stopped_cycle_a_new_request_applies_the_waiting_answers(mac):
    mac.watch_answers(3)
    mac.anki.result = "failed"
    mac.request("R-1")
    mac.run()
    mac.anki.result = "ok"
    mac.request("R-2")
    last = mac.run()
    assert last["request_id"] == "R-2" and last["applied"] == 3
    assert len(history(mac.col)) == 3


def test_a_sync_that_never_finishes_is_treated_as_failed(mac):
    mac.watch_answers(1)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)
    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "sync_failed" and last["sync_in"] == "timed_out"
    assert history(mac.col) == []


@pytest.mark.parametrize("off", ["sync turned off or not logged in"])
def test_with_sync_off_the_cycle_continues_without_a_sync(mac, off):
    mac.watch_answers(2)
    mac.anki.enabled = False
    mac.request()
    last = mac.run()
    assert mac.anki.asked == ["backup"]
    assert len(history(mac.col)) == 2
    assert (last["sync_in"], last["sync_out"], last["outcome"]) == ("skipped", "skipped", "done")
    assert "sync_in result=skipped" in mac.log() and "sync_out result=skipped" in mac.log()


def test_ankis_own_sync_on_opening_counts_as_the_sync_in(mac):
    """Anki has just opened and is syncing by itself when the request is noticed."""
    mac.watch_answers(2)
    mac.request()
    mac.anki.running = True
    mac.cycle.poll(mac.col)
    assert mac.anki.asked == [], "no second sync is asked for while Anki's own is running"
    assert history(mac.col) == []
    mac.anki.finish_sync()
    assert len(history(mac.col)) == 2
    assert mac.anki.asked == ["backup", "sync"]


def test_a_sync_that_finished_moments_ago_counts_as_the_sync_in(mac):
    mac.watch_answers(2)
    mac.request()
    mac.anki.clock += 5_000
    mac.anki.last_finished = mac.anki.clock - 2_000  # after the request was made
    mac.run()
    assert mac.anki.asked == ["backup", "sync"]
    assert len(history(mac.col)) == 2


def test_a_sync_from_before_the_request_does_not_count(mac):
    """A review on the phone could have been synced up since then."""
    mac.watch_answers(2)
    mac.anki.last_finished = mac.anki.clock - 2_000
    mac.request()
    mac.run()
    assert mac.anki.asked == ["sync", "backup", "sync"]


def test_a_recent_sync_that_failed_stops_the_cycle(mac):
    mac.watch_answers(2)
    mac.request()
    mac.anki.clock += 5_000
    mac.anki.last_finished = mac.anki.clock - 2_000
    mac.anki.result = "failed"
    last = mac.run()
    assert last["outcome"] == "sync_failed" and history(mac.col) == []


def test_an_old_sync_does_not_count(mac):
    mac.watch_answers(2)
    mac.request()
    mac.anki.last_finished = mac.anki.clock - 5 * 60_000
    mac.run()
    assert mac.anki.asked == ["sync", "backup", "sync"]


def test_the_cycle_waits_until_the_user_leaves_the_review_screen(mac):
    mac.watch_answers(2)
    mac.request()
    mac.anki.reviewing = True
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert mac.anki.asked == [] and history(mac.col) == []
    assert mac.log().count("waiting_to_leave_review") == 1
    mac.anki.reviewing = False
    mac.run()
    assert mac.anki.asked == ["sync", "backup", "sync"]
    assert len(history(mac.col)) == 2


@pytest.mark.parametrize("version", [(26, 10), (27, 0), (26, 12)])
def test_on_a_newer_anki_nothing_is_replayed_and_a_batch_is_still_exported(mac, version):
    events = mac.watch_answers(3)
    mac.anki.version = version
    mac.request()

    last = mac.run()

    assert history(mac.col) == []
    assert "backup" not in mac.anki.asked
    results = read(mac.exchange / "results.json")
    assert [(r["event_id"], r["outcome"], r["reason"]) for r in results["results"]] == [
        (e["event_id"], "held", "anki_newer_than_tested") for e in events
    ]
    assert (last["applied"], last["dropped"], last["held"]) == (0, 0, 3)
    assert last["held_because"] == "anki_newer_than_tested"
    assert read(mac.exchange / "batch.json")["batch_id"] != mac.batch_id
    assert last["export"] == "ok" and last["outcome"] == "done"


def test_holds_for_a_newer_anki_never_count_towards_dropping(mac):
    mac.watch_answers(1)
    mac.anki.version = (27, 0)
    for i in range(5):
        mac.request(f"R-{i}")
        assert mac.run()["held"] == 1
    mac.anki.version = (26, 9)
    mac.request("R-9")
    assert mac.run()["applied"] == 1


@pytest.mark.parametrize("version, newer", [((26, 9), False), ((26, 8), False), ((25, 12), False), ((26, 10), True), ((27, 1), True)])
def test_which_anki_versions_are_newer_than_tested(version, newer):
    assert anki_is_newer_than_tested(version) is newer


def test_anki_version_text_is_read():
    assert parse_anki_version("26.09.3") == (26, 9)
    assert parse_anki_version("26.10") == (26, 10)
    assert parse_anki_version("27.01rc1") == (27, 1)


def test_the_same_request_is_not_run_twice(mac):
    mac.watch_answers(2)
    mac.request()
    mac.run()
    asked = list(mac.anki.asked)
    batch = read(mac.exchange / "batch.json")["batch_id"]
    for _ in range(3):
        mac.run()
    assert mac.anki.asked == asked
    assert read(mac.exchange / "batch.json")["batch_id"] == batch


def test_a_cycle_cut_short_by_anki_quitting_runs_again_and_doubles_nothing(mac):
    mac.watch_answers(3)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.finish_sync()  # replay and export done; Anki quits during the sync out
    assert len(history(mac.col)) == 3
    reopened = FakeAnki(mac.col)
    reopened.cycle = DesktopCycle(reopened, mac.exchange, mac.state)
    mac.anki, mac.cycle = reopened, reopened.cycle

    last = mac.run()

    assert len(history(mac.col)) == 3
    assert last["request_id"] == "R-1" and last["outcome"] == "done" and last["applied"] == 3


def test_no_request_means_nothing_happens(mac):
    mac.watch_answers(2)
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert mac.anki.asked == [] and history(mac.col) == []


@pytest.mark.parametrize("content", ["{not json", "[]", '{"format_version": 2, "request_id": "R"}', '{"format_version": 1, "request_id": "a b\\nc"}'])
def test_an_unreadable_request_is_ignored(mac, content):
    (mac.exchange / "cycle-request.json").write_text(content)
    mac.cycle.poll(mac.col)
    assert mac.anki.asked == []


def test_the_log_has_one_line_per_step(mac):
    mac.watch_answers(1)
    mac.request()
    mac.run()
    log = mac.log()
    for step in ("cycle_start request=R-1", "sync_in result=ok detail=", "backup result=ok", "replay_done applied=1 dropped=0 held=0", "sync_out result=ok detail=", "cycle_end request=R-1 outcome=done"):
        assert log.count(step) == 1, step
    assert sum(1 for l in log if l.startswith("export ")) == 2  # setup's and the cycle's


# --- Ticket 03: the deck for the watch -------------------------------------------


def filtered_deck(col, name="Cram"):
    return col.decks.new_filtered(name)


def test_filtered_decks_are_not_offered(col, deck):
    filtered = filtered_deck(col)
    offered = watch_decks(col)
    assert (deck.id, "Watch") in offered
    assert filtered not in [i for i, _ in offered]
    assert col.decks.get(filtered)["dyn"]


def test_a_filtered_deck_cannot_be_chosen(col, deck, state):
    filtered = filtered_deck(col)
    with pytest.raises(ValueError):
        choose_deck(col, state, filtered)
    assert chosen_deck_id(col, state) is None


def test_a_filtered_deck_set_by_hand_is_not_exported(col, deck, exchange, state):
    filtered = filtered_deck(col)
    write_json(state / "settings.json", {"format_version": 1, "deck_id": filtered})
    assert chosen_deck_id(col, state) is None
    result = export_batch(col, chosen_deck_id(col, state), exchange, state)
    assert result.outcome == "no_deck_chosen"
    assert not (exchange / "batch.json").exists()
    assert read(exchange / "status.json")["last_export"]["outcome"] == "no_deck_chosen"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"deck_id": "1"}', '{"deck_id": true}', '{"deck_id": 999}'])
def test_a_setting_that_makes_no_sense_means_no_deck_is_chosen(col, state, content):
    state.mkdir()
    (state / "settings.json").write_text(content)
    assert chosen_deck_id(col, state) is None


def test_the_chosen_deck_is_remembered_and_is_the_one_exported(col, deck, exchange, state, tmp_path):
    from anki.collection import Collection

    other = Deck(col, "Other")
    other.review("other card")
    mine = deck.review("my card")
    choose_deck(col, state, deck.id)
    path = col.path
    col.close()
    reopened = Collection(path)  # as after restarting Anki
    try:
        assert chosen_deck_id(reopened, state) == deck.id
        export_batch(reopened, chosen_deck_id(reopened, state), exchange, state)
        assert [c["card_id"] for c in read(exchange / "batch.json")["cards"]] == [mine.id]
        assert read(exchange / "status.json")["deck_name"] == "Watch"
    finally:
        reopened.close()
        col.reopen()


def test_with_no_deck_chosen_a_cycle_writes_no_batch_and_says_why(col, deck, exchange, state):
    deck.review("card")
    anki = FakeAnki(col)
    anki.cycle = cycle = DesktopCycle(anki, exchange, state)
    write_json(exchange / "cycle-request.json", {"format_version": 1, "request_id": "R-1", "requested_at_ms": 1})
    cycle.poll(col)
    anki.finish_sync()
    anki.finish_sync()
    status = read(exchange / "status.json")
    assert not (exchange / "batch.json").exists()
    assert status["last_export"]["outcome"] == "no_deck_chosen"
    assert status["last_cycle"]["export"] == "no_deck_chosen"


def test_each_profile_has_its_own_folder_for_the_add_ons_records(tmp_path):
    a = state_dir_for(tmp_path, "User 1")
    b = state_dir_for(tmp_path, "User_1")
    c = state_dir_for(tmp_path, "../User 1")
    d = state_dir_for(tmp_path, "日本語")
    assert len({a, b, c, d}) == 4
    for folder in (a, b, c, d, state_dir_for(tmp_path, "")):
        assert folder.parent == tmp_path / "profiles"
        assert folder.name not in ("", ".", "..")


# --- Found by independent verification ---------------------------------------------


@pytest.mark.parametrize("route", ["sync off", "anki just synced", "user syncs while waiting", "enters review during sync in"])
def test_nothing_is_replayed_while_the_review_screen_is_open_whatever_the_route(mac, route):
    mac.watch_answers(2)
    mac.request()
    anki = mac.anki
    if route == "sync off":
        anki.enabled = False
        anki.reviewing = True
        mac.cycle.poll(mac.col)
    elif route == "anki just synced":
        anki.clock += 5_000
        anki.last_finished = anki.clock - 1_000
        anki.reviewing = True
        mac.cycle.poll(mac.col)
    elif route == "user syncs while waiting":
        anki.reviewing = True
        mac.cycle.poll(mac.col)  # waits to leave review
        anki.running = True      # the user presses Sync themselves
        mac.cycle.poll(mac.col)
        anki.finish_sync()
    else:
        mac.cycle.poll(mac.col)  # sync in started
        anki.reviewing = True
        anki.finish_sync()
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert history(mac.col) == [], route
    assert "backup" not in anki.asked
    assert "waiting_to_leave_review" in mac.log()

    anki.reviewing = False
    last = mac.run()
    assert len(history(mac.col)) == 2
    assert last["outcome"] == "done"


def test_the_sync_out_waits_until_anki_can_start_one(mac):
    """Right after a sync, Anki is still syncing media and its Sync action starts nothing."""
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.busy_with_media = True
    mac.anki.finish_sync()  # sync in done; replay and export run; the sync out cannot start yet
    assert len(history(mac.col)) == 2
    assert mac.anki.asked == ["sync", "backup"]
    mac.cycle.poll(mac.col)
    assert mac.anki.asked == ["sync", "backup"]
    assert read(mac.exchange / "status.json")["last_cycle"] is None

    mac.anki.busy_with_media = False
    last = mac.run()
    assert mac.anki.asked == ["sync", "backup", "sync"]
    assert last["sync_out"] == "ok" and last["outcome"] == "done"


def test_a_sync_out_that_can_never_start_does_not_hang_the_cycle(mac):
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.busy_with_media = True
    mac.anki.finish_sync()
    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)
    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "done" and last["sync_out"] == "not_started" and last["applied"] == 2


def test_a_sync_out_that_times_out_still_ends_the_cycle(mac):
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.finish_sync()
    assert mac.anki.asked == ["sync", "backup", "sync"]
    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)
    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "done" and last["sync_out"] == "timed_out"


def test_a_sync_finishing_when_no_cycle_is_waiting_does_nothing(mac):
    mac.watch_answers(2)
    mac.anki.finish_sync()
    assert history(mac.col) == [] and mac.anki.asked == []


@pytest.mark.parametrize("sync_on", [True, False])
def test_a_step_that_goes_wrong_ends_the_cycle_with_an_error_and_is_not_retried_forever(mac, monkeypatch, sync_on):
    from sukima import cycle as cycle_module

    mac.watch_answers(2)
    mac.anki.enabled = sync_on
    mac.request()
    calls = [0]

    def boom(*a, **k):
        calls[0] += 1
        raise RuntimeError("SECRET")

    monkeypatch.setattr(cycle_module, "replay", boom)
    for _ in range(6):
        mac.cycle.poll(mac.col)
        if mac.anki.running:
            mac.anki.finish_sync()

    last = read(mac.exchange / "status.json")["last_cycle"]
    assert calls[0] == 1
    assert last["outcome"] == "error" and last["error"] == "RuntimeError"
    assert mac.anki.asked.count("backup") == 1
    assert "cycle_error kind=RuntimeError" in " ".join(mac.log()) and "SECRET" not in " ".join(mac.log())
    # A new request tries again, and replay is safe to repeat.
    monkeypatch.undo()
    mac.request("R-2")
    assert mac.run()["applied"] == 2


def test_results_name_the_request_they_answer(mac):
    mac.watch_answers(2)
    mac.request("R-7")
    mac.run()
    assert read(mac.exchange / "results.json")["request_id"] == "R-7"
    mac.anki.version = (27, 0)
    mac.request("R-8")
    mac.run()
    assert read(mac.exchange / "results.json")["request_id"] == "R-8"


def test_a_profile_with_no_deck_chosen_keeps_the_answers_and_says_why(col, deck, exchange, tmp_path):
    """Another profile must not drop answers that are not its own."""
    watch_profile = Mac(col, deck, exchange, tmp_path / "state-a")
    events = watch_profile.watch_answers(2)
    watch_profile.request("R-1")
    other = FakeAnki(col)
    other.cycle = DesktopCycle(other, exchange, tmp_path / "state-b")

    for _ in range(4):
        other.cycle.poll(col)
        if other.running:
            other.finish_sync()

    assert history(col) == [] and "backup" not in other.asked
    results = read(exchange / "results.json")["results"]
    assert [(r["outcome"], r["reason"]) for r in results] == [("held", "no_deck_chosen")] * 2
    last = read(exchange / "status.json")["last_cycle"]
    assert last["held_because"] == "no_deck_chosen" and last["export"] == "no_deck_chosen"
    assert not (tmp_path / "state-b" / "held.json").exists()
    # The profile the answers belong to still applies them.
    assert watch_profile.run()["applied"] == 2
    assert len(events) == 2


def test_holds_for_a_newer_anki_do_not_use_up_the_three_holds(mac, monkeypatch):
    from sukima import replay as replay_module

    mac.watch_answers(1)
    mac.anki.version = (27, 0)
    for i in range(5):
        mac.request(f"R-{i}")
        mac.run()
    assert not (mac.state / "held.json").exists()
    # Back on a tested Anki, a refusal is the first hold, not the sixth.
    mac.anki.version = (26, 9)
    monkeypatch.setattr(replay_module, "_answer", lambda col, e: (_ for _ in ()).throw(RuntimeError()))
    mac.request("R-9")
    last = mac.run()
    assert (last["held"], last["dropped"]) == (1, 0)


def test_a_sync_out_that_never_reports_its_end_does_not_hang_the_cycle(mac):
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.finish_sync()         # sync in ok; replay and export; the sync out is started
    assert len(history(mac.col)) == 2 and mac.anki.asked[-1] == "sync"
    # Anki's sync never reports that it finished.
    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)
    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "done" and last["sync_out"] == "timed_out"


def test_the_sync_out_gives_up_when_another_sync_runs_on_and_on(mac):
    """Anki says a sync started and never says it finished."""
    mac.watch_answers(1)
    mac.request()
    mac.cycle.poll(mac.col)

    def backup_then_a_sync_that_never_ends():
        mac.anki.running = True
        return True

    mac.anki.backup = backup_then_a_sync_that_never_ends
    mac.anki.finish_sync()  # sync in ok; replay and export; the sync out cannot start
    assert len(history(mac.col)) == 1 and mac.anki.asked == ["sync"]
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert read(mac.exchange / "status.json")["last_cycle"] is None

    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)

    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["sync_out"] == "not_started" and last["outcome"] == "done" and last["applied"] == 1
    # And the next request is taken up.
    mac.anki.running = False
    mac.request("R-2")
    assert mac.run()["request_id"] == "R-2"


def test_nothing_is_replayed_while_anki_is_syncing_by_itself(mac):
    """The user leaves the review screen and presses Sync before the next poll."""
    mac.watch_answers(2)
    mac.anki.enabled = False
    mac.request()
    mac.anki.reviewing = True
    mac.cycle.poll(mac.col)
    mac.anki.reviewing = False
    mac.anki.running = True
    mac.cycle.poll(mac.col)
    assert history(mac.col) == []
    mac.anki.running = False
    mac.cycle.poll(mac.col)
    assert len(history(mac.col)) == 2


def test_a_sync_in_that_cannot_start_is_tried_again_then_given_up_without_replay(mac):
    mac.watch_answers(2)
    mac.anki.busy_with_media = True
    mac.request()
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert mac.anki.asked == [] and history(mac.col) == []
    assert read(mac.exchange / "status.json")["last_cycle"] is None

    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)
    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["sync_in"] == "not_started" and last["outcome"] == "sync_failed"
    assert history(mac.col) == [], "no sync in, so nothing is replayed"


def test_a_sync_in_that_can_start_after_a_moment_goes_ahead(mac):
    mac.watch_answers(2)
    mac.anki.busy_with_media = True
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.busy_with_media = False
    assert mac.run()["applied"] == 2
    assert mac.anki.asked == ["sync", "backup", "sync"]


def test_ankis_window_is_refreshed_after_replay_and_export(mac):
    """Otherwise the deck list keeps its old counts and Undo is not offered."""
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    assert mac.anki.refreshed == 0
    mac.anki.finish_sync()
    assert mac.anki.refreshed == 1 and len(history(mac.col)) == 2


@pytest.mark.parametrize("route", ["sync off", "after a good sync in"])
def test_work_stuck_behind_a_sync_that_never_ends_stops_the_cycle_without_replay(mac, route):
    mac.watch_answers(2)
    mac.request()
    if route == "sync off":
        mac.anki.enabled = False
        mac.anki.reviewing = True
        mac.cycle.poll(mac.col)
    else:
        mac.cycle.poll(mac.col)
        mac.anki.reviewing = True
        mac.anki.finish_sync()
    mac.anki.reviewing = False
    mac.anki.running = True          # a sync starts and never reports its end
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert read(mac.exchange / "status.json")["last_cycle"] is None and history(mac.col) == []

    mac.anki.clock += 6 * 60_000
    mac.cycle.poll(mac.col)

    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "sync_failed" and history(mac.col) == []
    assert "backup" not in mac.anki.asked
    assert len(read(mac.exchange / "review-events.json")["events"]) == 2
    # The next request is taken up.
    mac.anki.running = False
    mac.request("R-2")
    assert mac.run()["applied"] == 2


def test_a_long_time_on_the_review_screen_does_not_use_up_the_limits_that_follow(mac):
    """After ten minutes mid-review, a sync that cannot start at once is still retried."""
    mac.watch_answers(2)
    mac.request()
    mac.anki.reviewing = True
    mac.cycle.poll(mac.col)
    mac.anki.clock += 10 * 60_000
    mac.cycle.poll(mac.col)
    mac.anki.reviewing = False
    mac.anki.busy_with_media = True
    mac.cycle.poll(mac.col)
    assert read(mac.exchange / "status.json")["last_cycle"] is None, "not given up at the first try"
    mac.anki.busy_with_media = False
    assert mac.run()["applied"] == 2


def test_a_long_time_on_the_review_screen_after_the_sync_in_does_not_use_up_the_wait_behind_a_sync(mac):
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.reviewing = True
    mac.anki.finish_sync()           # sync in ok; waiting to leave review
    mac.anki.clock += 30 * 60_000
    mac.cycle.poll(mac.col)
    mac.anki.reviewing = False
    mac.anki.running = True          # a sync of the user's own
    mac.anki.clock += 4 * 60_000
    mac.cycle.poll(mac.col)
    assert read(mac.exchange / "status.json")["last_cycle"] is None, "not given up yet"
    mac.anki.running = False
    assert mac.run()["applied"] == 2


def test_sync_turned_on_during_the_cycle_means_a_sync_in_before_any_replay(mac):
    mac.watch_answers(2)
    mac.anki.enabled = False
    mac.request()
    mac.anki.reviewing = True
    mac.cycle.poll(mac.col)
    mac.anki.enabled = True
    mac.anki.reviewing = False
    mac.cycle.poll(mac.col)
    assert history(mac.col) == [] and mac.anki.asked == ["sync"]
    mac.anki.finish_sync()
    assert len(history(mac.col)) == 2
    assert mac.anki.asked[:2] == ["sync", "backup"]


def test_a_full_sync_wanted_since_the_sync_in_stops_the_cycle_before_replay(mac):
    """The user synced by hand while the cycle waited on the review screen, and Anki now wants a full sync."""
    mac.watch_answers(2)
    mac.request()
    mac.cycle.poll(mac.col)
    mac.anki.reviewing = True
    mac.anki.finish_sync()           # sync in ok; waiting to leave review
    mac.anki.full_wanted = True
    mac.anki.reviewing = False
    mac.cycle.poll(mac.col)

    last = read(mac.exchange / "status.json")["last_cycle"]
    assert last["outcome"] == "full_sync_needed" and history(mac.col) == []
    assert "backup" not in mac.anki.asked
    assert len(read(mac.exchange / "review-events.json")["events"]) == 2


def test_with_sync_off_a_full_sync_wanted_does_not_stop_the_replay(mac):
    """Anki does not sync by itself, so the add-on does not ask. Known and accepted (r1-build status)."""
    mac.watch_answers(2)
    mac.anki.enabled = False
    mac.anki.full_wanted = True
    mac.request()
    last = mac.run()
    assert last["outcome"] == "done" and len(history(mac.col)) == 2


def test_the_window_is_not_refreshed_while_the_review_screen_is_open(mac):
    mac.watch_answers(1)
    mac.anki.enabled = False
    mac.request()
    mac.anki.reviewing = True
    for _ in range(3):
        mac.cycle.poll(mac.col)
    assert mac.anki.refreshed == 0
