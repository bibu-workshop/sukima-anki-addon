"""Replay through Anki's scheduler (US-002, US-003, US-013; AK-12, AK-13, AK-19, AK-21, AK-22, AK-33, AK-34, AK-41, AK-44)."""

import json
import time
import uuid

import pytest
from anki.scheduler.v3 import CardAnswer

from conftest import Deck
from sukima import replay as replay_module
from sukima.export import export_batch
from sukima.files import write_json
from sukima.replay import replay

HOUR = 3_600_000
BUTTON = {"again": 1, "hard": 2, "good": 3, "easy": 4}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def history(col, card_id=None):
    """Anki's review history as (time ms, card, button, ms taken, new interval) rows, oldest first."""
    where = f"where cid = {card_id}" if card_id else ""
    return [tuple(r) for r in col.db.all(f"select id, cid, ease, time, ivl from revlog {where} order by id")]


def answers(col, card_id):
    """History rows that are answers; a reset leaves a row with no button."""
    return [row for row in history(col, card_id) if row[2] > 0]


def event(batch_id, card, rating="good", at=None, taken=4250, event_id=None, **changes):
    e = {
        "format_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "card_id": card.id if hasattr(card, "id") else card,
        "rating": rating,
        "answered_at_ms": at if at is not None else int(time.time() * 1000) - HOUR,
        "time_taken_ms": taken,
        "batch_id": batch_id,
    }
    e.update(changes)
    return e


def write_events(exchange, events):
    write_json(exchange / "review-events.json", {"format_version": 1, "written_at_ms": 1, "events": events})


def outcomes(result):
    return [(r["outcome"], r["reason"]) for r in result.results]


class World:
    """A deck of due review cards, exported, ready for review events."""

    def __init__(self, col, deck, exchange, state, cards=10):
        self.col, self.deck, self.exchange, self.state = col, deck, exchange, state
        self.cards = [deck.review(f"front {i}", f"back {i}", overdue=cards - i) for i in range(cards)]
        # Exported ten hours ago, so answers given since then are believable.
        exported = int(time.time() * 1000) - 10 * HOUR
        self.batch_id = export_batch(col, deck.id, exchange, state, now_ms=exported).batch_id
        self.base = int(time.time() * 1000) - 5 * HOUR

    def events(self, n=None, rating="good"):
        """One event per card, a minute apart, oldest first."""
        cards = self.cards[: n or len(self.cards)]
        return [event(self.batch_id, c, rating, at=self.base + i * 60_000) for i, c in enumerate(cards)]

    def replay(self, events=None):
        if events is not None:
            write_events(self.exchange, events)
        return replay(self.col, self.exchange, self.state)


@pytest.fixture
def world(col, deck, exchange, state):
    return World(col, deck, exchange, state)


# --- Ticket 04: the path where every event applies -------------------------------


def test_each_event_is_in_the_history_once_with_its_time_rating_and_time_taken(world):
    ratings = ["again", "hard", "good", "easy"]
    events = [
        event(world.batch_id, card, ratings[i % 4], at=world.base + i * 1000, taken=1000 + i)
        for i, card in enumerate(world.cards)
    ]
    shuffled = events[5:] + events[:5][::-1]

    result = world.replay(shuffled)

    assert [(t, cid, button, taken) for t, cid, button, taken, _ in history(world.col)] == [
        (e["answered_at_ms"], e["card_id"], BUTTON[e["rating"]], e["time_taken_ms"]) for e in events
    ]
    assert outcomes(result) == [("applied", None)] * 10
    # Results are reported in the order applied: oldest answer first.
    assert [r["event_id"] for r in result.results] == [e["event_id"] for e in events]


def test_intervals_are_the_ones_ankis_scheduler_computes(world, tmp_path):
    """The same answers given straight to Anki in a twin collection give the same cards."""
    from anki.collection import Collection

    events = world.events(4)
    for e, rating in zip(events, ["again", "hard", "good", "easy"]):
        e["rating"] = rating
    world.col.close()
    import shutil

    shutil.copy(world.col.path, tmp_path / "twin.anki2")
    world.col.reopen()
    twin = Collection(str(tmp_path / "twin.anki2"))
    try:
        for e in events:
            card = twin.get_card(e["card_id"])
            states = twin._backend.get_scheduling_states(card.id)
            card.start_timer()
            answer = twin.sched.build_answer(card=card, states=states, rating=BUTTON[e["rating"]] - 1)
            answer.answered_at_millis = e["answered_at_ms"]
            answer.milliseconds_taken = e["time_taken_ms"]
            twin.sched.answer_card(answer)
        expected = [
            (c.ivl, c.due, c.factor, c.type, c.queue, c.reps, c.lapses)
            for c in (twin.get_card(e["card_id"]) for e in events)
        ]
        expected_history = history(twin)
    finally:
        twin.close()

    world.replay(events)

    got = [
        (c.ivl, c.due, c.factor, c.type, c.queue, c.reps, c.lapses)
        for c in (world.col.get_card(e["card_id"]) for e in events)
    ]
    assert got == expected
    assert history(world.col) == expected_history
    assert len({row[4] for row in history(world.col)}) > 1, "the four ratings should give different intervals"


def test_replay_works_when_anki_has_a_study_queue_built(world, monkeypatch):
    col = world.col
    col.decks.select(world.deck.id)
    top = col.sched.get_queued_cards().cards[0].card.id
    not_top = [c for c in world.cards if c.id != top]
    assert len(not_top) >= 2

    result = world.replay([event(world.batch_id, c, at=world.base + i) for i, c in enumerate(not_top)])

    assert outcomes(result) == [("applied", None)] * len(not_top)
    assert all(len(history(col, c.id)) == 1 for c in not_top)
    assert col.decks.selected() == world.deck.id


def test_without_clearing_the_queue_anki_refuses_a_card_that_is_not_at_its_top(world, monkeypatch):
    """Shows the test above means something: the clearing step is what makes it work."""
    col = world.col
    col.decks.select(world.deck.id)
    top = col.sched.get_queued_cards().cards[0].card.id
    not_top = [c for c in world.cards if c.id != top]
    monkeypatch.setattr(replay_module, "_clear_study_queue", lambda col: None)

    result = world.replay([event(world.batch_id, not_top[0])])

    assert outcomes(result) == [("held", "anki_refused")]


def test_selected_deck_is_the_same_after_replay(world):
    other = Deck(world.col, "Other")
    world.col.decks.select(other.id)
    world.replay(world.events(3))
    assert world.col.decks.selected() == other.id


def test_two_answers_in_the_same_millisecond_are_both_applied(world):
    a, b = world.cards[:2]
    events = [event(world.batch_id, a, at=world.base), event(world.batch_id, b, at=world.base)]
    result = world.replay(events)
    assert outcomes(result) == [("applied", None)] * 2
    assert len(history(world.col, a.id)) == 1 and len(history(world.col, b.id)) == 1
    assert {t for t, *_ in history(world.col)} <= {world.base, world.base + 1}


def test_results_file_and_status_carry_every_event_and_the_counts(world):
    events = world.events(3)
    world.replay(events)

    results = read(world.exchange / "results.json")
    assert results["format_version"] == 1
    assert results["counts"] == {"applied": 3, "dropped": 0, "held": 0}
    assert results["results"] == [
        {"event_id": e["event_id"], "outcome": "applied", "reason": None} for e in events
    ]
    status = read(world.exchange / "status.json")
    assert {k: status["last_replay"][k] for k in ("applied", "dropped", "held")} == results["counts"]
    assert status["last_export"]["batch_id"] == world.batch_id, "replay must not wipe the export's status"


def test_each_event_goes_into_the_applied_log_before_the_next_is_handled(world, monkeypatch):
    events = world.events(3)
    seen = []
    real = replay_module._answer

    def spy(col, e):
        applied = read(world.state / "applied.json")["applied"] if (world.state / "applied.json").exists() else {}
        seen.append(sorted(applied))
        real(col, e)

    monkeypatch.setattr(replay_module, "_answer", spy)
    world.replay(events)

    assert seen == [sorted(e["event_id"] for e in events[:i]) for i in range(3)]
    assert sorted(read(world.state / "applied.json")["applied"]) == sorted(e["event_id"] for e in events)


def test_log_has_one_line_per_event_with_the_card_id_and_no_content(col, deck, exchange, state):
    card = deck.review("SECRETFRONT", "SECRETBACK")
    batch_id = export_batch(col, deck.id, exchange, state, now_ms=int(time.time() * 1000) - 10 * HOUR).batch_id
    e = event(batch_id, card)
    write_events(exchange, [e])
    replay(col, exchange, state)

    text = (state / "addon.log").read_text()
    lines = [l for l in text.splitlines() if " replay " in l]
    assert len(lines) == 1
    assert f"event={e['event_id']} card={card.id} outcome=applied" in lines[0]
    assert "SECRET" not in text


def test_no_events_file_means_nothing_to_do(world):
    result = world.replay()
    assert result.results == []
    assert history(world.col) == []
    assert read(world.exchange / "results.json")["results"] == []


# --- Ticket 05: never double a review -------------------------------------------


@pytest.mark.parametrize("applied_before_interruption", range(10))
def test_interrupted_after_any_event_then_run_again_gives_exactly_ten_reviews(world, monkeypatch, applied_before_interruption):
    events = world.events(10)
    write_events(world.exchange, events)
    real = replay_module._answer
    calls = [0]

    class Quit(BaseException):
        """Anki quitting: nothing after this point runs."""

    def interrupted(col, e):
        if calls[0] == applied_before_interruption:
            raise Quit()
        calls[0] += 1
        real(col, e)

    monkeypatch.setattr(replay_module, "_answer", interrupted)
    with pytest.raises(Quit):
        world.replay()
    assert len(history(world.col)) == applied_before_interruption
    monkeypatch.setattr(replay_module, "_answer", real)

    result = world.replay()

    rows = history(world.col)
    assert len(rows) == 10
    assert sorted(cid for _, cid, *_ in rows) == sorted(e["card_id"] for e in events)
    assert [o for o, _ in outcomes(result)] == ["applied"] * 10
    assert [r for _, r in outcomes(result)] == ["already_applied"] * applied_before_interruption + [None] * (
        10 - applied_before_interruption
    )


def test_interrupted_between_anki_recording_the_answer_and_the_applied_log(world, monkeypatch):
    """The narrowest gap: Anki has the review, the add-on has not yet noted it."""
    events = world.events(10)
    write_events(world.exchange, events)
    real = replay_module._Records.mark_applied
    calls = [0]

    def dies_on_fifth(self, e):
        calls[0] += 1
        if calls[0] == 5:
            raise KeyboardInterrupt()
        real(self, e)

    monkeypatch.setattr(replay_module._Records, "mark_applied", dies_on_fifth)
    with pytest.raises(KeyboardInterrupt):
        world.replay()
    assert len(history(world.col)) == 5
    assert len(read(world.state / "applied.json")["applied"]) == 4
    monkeypatch.setattr(replay_module._Records, "mark_applied", real)

    world.replay()

    assert len(history(world.col)) == 10
    assert len(read(world.state / "applied.json")["applied"]) == 10


def test_running_twice_over_the_same_file_adds_nothing(world):
    events = world.events(10)
    world.replay(events)
    before = history(world.col)
    cards_before = [(c.ivl, c.due, c.reps) for c in (world.col.get_card(c.id) for c in world.cards)]

    result = world.replay()

    assert history(world.col) == before
    assert [(c.ivl, c.due, c.reps) for c in (world.col.get_card(c.id) for c in world.cards)] == cards_before
    assert outcomes(result) == [("applied", "already_applied")] * 10


def test_with_the_applied_log_removed_the_history_alone_recognises_an_applied_event(world):
    events = world.events(10)
    world.replay(events)
    (world.state / "applied.json").unlink()

    result = world.replay()

    assert len(history(world.col)) == 10
    assert outcomes(result) == [("applied", "already_applied")] * 10
    # And the log is rebuilt from what the history showed.
    assert len(read(world.state / "applied.json")["applied"]) == 10


def test_with_the_history_guard_off_the_applied_log_alone_recognises_an_applied_event(world, monkeypatch):
    events = world.events(10)
    world.replay(events)
    monkeypatch.setattr(replay_module, "_in_review_history", lambda col, e: False)

    result = world.replay()

    assert len(history(world.col)) == 10
    assert [o for o, _ in outcomes(result)] == ["applied"] * 10


def test_the_same_answer_under_a_new_event_id_is_not_applied_twice(world):
    """The history guard goes by card and answer time, not by event ID."""
    first = world.events(1)
    world.replay(first)
    again = [dict(first[0], event_id=str(uuid.uuid4()))]
    result = world.replay(again)
    assert len(history(world.col)) == 1
    assert outcomes(result) == [("applied", "already_applied")]


def test_a_replayed_review_undone_in_anki_stays_undone(world):
    events = world.events(1)
    world.replay(events)
    card = world.cards[0]
    assert len(history(world.col, card.id)) == 1
    world.col.undo()
    assert history(world.col, card.id) == []
    reps_after_undo = world.col.get_card(card.id).reps

    result = world.replay()

    assert history(world.col, card.id) == []
    assert world.col.get_card(card.id).reps == reps_after_undo
    assert outcomes(result) == [("applied", "undone_in_anki")]


# --- Ticket 06: dropped and held, with a reason -----------------------------------


def test_events_that_cannot_be_trusted_are_dropped_each_with_its_reason(world):
    good = event(world.batch_id, world.cards[0])
    not_in_batch = Deck(world.col, "Other").review("elsewhere")
    events = [
        event(world.batch_id, world.cards[1], format_version=2),
        event(world.batch_id, world.cards[2], rating="perfect"),
        event(world.batch_id, world.cards[3], rating=3),
        event(world.batch_id, not_in_batch),
        event("no-such-batch", world.cards[4]),
        event(world.batch_id, world.cards[5], answered_at_ms="yesterday"),
        event(world.batch_id, world.cards[6], time_taken_ms=-5),
        "not an event",
        good,
    ]
    result = world.replay(events)

    by_id = {r["event_id"]: (r["outcome"], r["reason"]) for r in result.results}
    assert [by_id.get(e["event_id"]) for e in events if isinstance(e, dict)] == [
        ("dropped", "unknown_format"),
        ("dropped", "bad_rating"),
        ("dropped", "bad_rating"),
        ("dropped", "not_in_batch"),
        ("dropped", "unknown_batch"),
        ("dropped", "unknown_format"),
        ("dropped", "unknown_format"),
        ("applied", None),
    ]
    assert by_id[None] == ("dropped", "unknown_format")
    assert [cid for _, cid, *_ in history(world.col)] == [world.cards[0].id]


def test_a_card_that_no_longer_exists_is_dropped(world):
    gone = world.cards[0]
    world.col.remove_notes([gone.nid])
    result = world.replay([event(world.batch_id, gone), event(world.batch_id, world.cards[1])])
    assert sorted(outcomes(result), key=str) == sorted([("dropped", "card_missing"), ("applied", None)], key=str)


def answer_in_anki(col, card, at_ms):
    """A review given in Anki itself, or synced in from another client."""
    card = col.get_card(card.id)
    states = col._backend.get_scheduling_states(card.id)
    card.start_timer()
    answer = col.sched.build_answer(card=card, states=states, rating=CardAnswer.HARD)
    answer.answered_at_millis = at_ms
    col.sched.answer_card(answer)


def test_watch_first_then_anki_the_watch_answer_is_dropped(world):
    """AK-33: watch at 9:00, another client at 11:00, before the desktop cycle."""
    card = world.cards[0]
    nine, eleven = world.base, world.base + 2 * HOUR
    answer_in_anki(world.col, card, eleven)

    result = world.replay([event(world.batch_id, card, "good", at=nine)])

    assert outcomes(result) == [("dropped", "reviewed_elsewhere")]
    assert [(t, button) for t, _, button, *_ in history(world.col, card.id)] == [(eleven, 2)]
    assert "outcome=dropped reason=reviewed_elsewhere" in (world.state / "addon.log").read_text()


def test_anki_first_then_watch_the_watch_answer_is_still_dropped(world):
    """AK-34: the other client at 9:00, the watch at 11:00."""
    card = world.cards[0]
    nine, eleven = world.base, world.base + 2 * HOUR
    answer_in_anki(world.col, card, nine)

    result = world.replay([event(world.batch_id, card, "good", at=eleven)])

    assert outcomes(result) == [("dropped", "reviewed_elsewhere")]
    assert [(t, button) for t, _, button, *_ in history(world.col, card.id)] == [(nine, 2)]


def test_reviewed_elsewhere_goes_by_the_review_count_at_export(world):
    card = world.cards[0]
    record = read(world.state / "batches" / f"{world.batch_id}.json")
    assert record["review_counts"][str(card.id)] == world.col.get_card(card.id).reps
    # One more review than at export, by whatever route, and the watch answer is dropped.
    c = world.col.get_card(card.id)
    c.reps += 1
    world.col.update_card(c)
    assert outcomes(world.replay([event(world.batch_id, card)])) == [("dropped", "reviewed_elsewhere")]


def test_a_second_answer_for_the_same_card_is_dropped(world):
    card = world.cards[0]
    events = [event(world.batch_id, card, at=world.base), event(world.batch_id, card, at=world.base + 60_000)]
    result = world.replay(events)
    assert outcomes(result) == [("applied", None), ("dropped", "reviewed_elsewhere")]
    assert len(history(world.col, card.id)) == 1


def refuse(monkeypatch, refused_ids):
    real = replay_module._answer

    def maybe(col, e):
        if e["event_id"] in refused_ids:
            raise RuntimeError("SECRET card text in an error")
        real(col, e)

    monkeypatch.setattr(replay_module, "_answer", maybe)


def test_an_event_anki_refuses_is_held_and_the_rest_are_still_handled(world, monkeypatch):
    events = world.events(3)
    refuse(monkeypatch, {events[1]["event_id"]})

    result = world.replay(events)

    assert outcomes(result) == [("applied", None), ("held", "anki_refused"), ("applied", None)]
    assert len(history(world.col)) == 2
    results = read(world.exchange / "results.json")
    assert results["counts"] == {"applied": 2, "dropped": 0, "held": 1}
    log = (world.state / "addon.log").read_text()
    assert f"refused event={events[1]['event_id']} kind=RuntimeError" in log
    assert "SECRET" not in log


def test_an_event_held_in_three_cycles_is_dropped(world, monkeypatch):
    events = world.events(2)
    refuse(monkeypatch, {events[0]["event_id"]})

    first = world.replay(events)
    second = world.replay()
    third = world.replay()

    assert outcomes(first)[0] == ("held", "anki_refused")
    assert outcomes(second)[0] == ("held", "anki_refused")
    assert outcomes(third)[0] == ("dropped", "held_three_cycles")
    assert "outcome=dropped reason=held_three_cycles" in (world.state / "addon.log").read_text()
    assert history(world.col, events[0]["card_id"]) == []
    assert read(world.exchange / "results.json")["counts"] == {"applied": 1, "dropped": 1, "held": 0}


def test_a_held_event_that_anki_then_accepts_is_applied(world, monkeypatch):
    events = world.events(1)
    refuse(monkeypatch, {events[0]["event_id"]})
    world.replay(events)
    world.replay()
    monkeypatch.undo()

    result = world.replay()

    assert outcomes(result) == [("applied", None)]
    assert len(history(world.col)) == 1
    assert read(world.state / "held.json")["held"] == {}


def test_every_event_not_applied_has_a_reason_in_the_results_and_the_log(world, monkeypatch):
    events = world.events(4)
    events[0]["rating"] = "perfect"
    refuse(monkeypatch, {events[1]["event_id"]})
    answer_in_anki(world.col, world.cards[2], world.base)

    world.replay(events)

    results = read(world.exchange / "results.json")
    assert results["counts"] == {"applied": 1, "dropped": 2, "held": 1}
    status = read(world.exchange / "status.json")["last_replay"]
    assert (status["applied"], status["dropped"], status["held"]) == (1, 2, 1)
    log = (world.state / "addon.log").read_text()
    for r in results["results"]:
        if r["outcome"] != "applied":
            assert r["reason"]
            assert f"event={r['event_id']}" in log and f"reason={r['reason']}" in log


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"rating": ["good"]}, "bad_rating"),
        ({"rating": {"a": 1}}, "bad_rating"),
        ({"rating": "Good"}, "bad_rating"),
        ({"answered_at_ms": 10**30}, "unknown_format"),
        ({"answered_at_ms": -5}, "unknown_format"),
        ({"answered_at_ms": 0}, "unknown_format"),
        ({"answered_at_ms": 1.5}, "unknown_format"),
        ({"answered_at_ms": True}, "unknown_format"),
        ({"time_taken_ms": 2**40}, "unknown_format"),
        ({"batch_id": "a\x00b"}, "unknown_format"),
        ({"batch_id": "x" * 5000}, "unknown_format"),
        ({"batch_id": "../../etc/passwd"}, "unknown_format"),
        ({"event_id": "x\n2026 replay event=forged outcome=applied"}, "unknown_format"),
        ({"event_id": 7}, "unknown_format"),
        ({"card_id": "123"}, "unknown_format"),
        ({"card_id": 10**30}, "unknown_format"),
        ({"card_id": None}, "unknown_format"),
        ({"format_version": True}, "unknown_format"),
        ({"format_version": 1.0}, "unknown_format"),
        ({"format_version": None}, "unknown_format"),
    ],
)
def test_a_malformed_event_is_dropped_and_the_ones_after_it_are_still_handled(world, changes, reason):
    bad = event(world.batch_id, world.cards[0], at=world.base)
    bad.update(changes)
    good = event(world.batch_id, world.cards[1], at=world.base + 60_000)

    result = world.replay([bad, good])

    by_outcome = sorted(outcomes(result), key=str)
    assert by_outcome == sorted([("dropped", reason), ("applied", None)], key=str)
    assert [cid for _, cid, *_ in history(world.col)] == [world.cards[1].id]
    results = read(world.exchange / "results.json")
    assert len(results["results"]) == 2
    assert all(r["event_id"] is None or isinstance(r["event_id"], str) for r in results["results"])
    assert "forged" not in (world.state / "addon.log").read_text()


def test_a_held_count_that_is_not_a_number_is_ignored(world, monkeypatch):
    events = world.events(1)
    write_json(world.state / "held.json", {"format_version": 1, "held": {events[0]["event_id"]: "two"}})
    refuse(monkeypatch, {events[0]["event_id"]})
    assert outcomes(world.replay(events)) == [("held", "anki_refused")]


def test_an_event_that_cannot_be_judged_is_held_and_the_rest_carry_on(world, monkeypatch):
    events = world.events(3)
    real = replay_module._in_review_history

    def breaks_on_second(col, e):
        if e["event_id"] == events[1]["event_id"]:
            raise RuntimeError("SECRET")
        return real(col, e)

    monkeypatch.setattr(replay_module, "_in_review_history", breaks_on_second)
    result = world.replay(events)
    assert outcomes(result) == [("applied", None), ("held", "error"), ("applied", None)]
    assert "SECRET" not in (world.state / "addon.log").read_text()


def test_an_answer_time_from_before_the_batch_or_from_the_future_is_dropped(world):
    exported = read(world.state / "batches" / f"{world.batch_id}.json")["exported_at_ms"]
    now = int(time.time() * 1000)
    events = [
        event(world.batch_id, world.cards[0], at=exported - HOUR),
        event(world.batch_id, world.cards[1], at=now + 2 * 24 * HOUR),
        event(world.batch_id, world.cards[2], at=now + 60_000),  # a watch clock a minute fast
    ]
    result = world.replay(events)
    assert outcomes(result) == [("dropped", "bad_time"), ("applied", None), ("dropped", "bad_time")]
    assert [cid for _, cid, *_ in history(world.col)] == [world.cards[2].id]


@pytest.mark.parametrize("name", ["applied.json", "held.json"])
@pytest.mark.parametrize("content", ["{not json", "[]", '{"applied": [], "held": 3}', ""])
def test_a_damaged_record_of_the_add_ons_does_not_stop_replay_or_double_a_review(world, name, content):
    events = world.events(3)
    world.replay(events)
    (world.state / name).write_text(content)

    result = world.replay()

    assert len(history(world.col)) == 3
    assert [o for o, _ in outcomes(result)] == ["applied"] * 3
    if content != "":
        pass
    assert "record_unreadable" in (world.state / "addon.log").read_text()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"format_version": 2, "events": []}', '{"format_version": 1, "events": {}}'])
def test_an_unreadable_events_file_holds_no_events_and_is_logged(world, content):
    world.exchange.mkdir(exist_ok=True)
    (world.exchange / "review-events.json").write_text(content)
    result = world.replay()
    assert result.results == []
    assert read(world.exchange / "results.json")["results"] == []
    assert "events_unreadable" in (world.state / "addon.log").read_text()


def test_a_damaged_batch_record_means_the_batch_is_unknown(world):
    (world.state / "batches" / f"{world.batch_id}.json").write_text("{not json")
    assert outcomes(world.replay(world.events(1))) == [("dropped", "unknown_batch")]


def test_a_card_reset_at_the_mac_since_export_does_not_get_the_watch_answer(world):
    card = world.col.get_card(world.cards[0].id)
    world.col.sched.schedule_cards_as_new([card.id], restore_position=True, reset_counts=True)
    assert world.col.get_card(card.id).reps < card.reps
    assert outcomes(world.replay(world.events(1))) == [("dropped", "reviewed_elsewhere")]
    assert answers(world.col, card.id) == []


def changed_at_the_mac(col, card, how):
    if how == "suspended":
        col.sched.suspend_cards([card.id])
    elif how == "buried":
        col.sched.bury_cards([card.id])
    elif how == "reset, counts kept":
        col.sched.schedule_cards_as_new([card.id], restore_position=True, reset_counts=False)
    elif how == "in a filtered deck":
        did = col.decks.new_filtered("Cram")
        deck = col.decks.get(did)
        deck["terms"] = [[f"cid:{card.id}", 10, 0]]
        col.decks.save(deck)
        col.sched.rebuild_filtered_deck(did)
        assert col.get_card(card.id).odid


@pytest.mark.parametrize("how", ["suspended", "buried", "reset, counts kept", "in a filtered deck"])
def test_a_card_changed_at_the_mac_since_export_does_not_get_the_watch_answer(world, how):
    card = world.cards[0]
    changed_at_the_mac(world.col, card, how)
    before = world.col.get_card(card.id)

    result = world.replay(world.events(2))

    assert outcomes(result) == [("dropped", "changed_since_export"), ("applied", None)]
    after = world.col.get_card(card.id)
    assert (after.queue, after.type, after.due, after.ivl, after.did) == (
        before.queue, before.type, before.due, before.ivl, before.did
    )
    assert answers(world.col, card.id) == []
