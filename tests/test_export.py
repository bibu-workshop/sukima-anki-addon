import json
import re

from anki.buildinfo import version as anki_version

from conftest import Deck
from sukima import __version__
from sukima.export import BATCH_CAP, batch_record_path, export_batch


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def queue_kinds(col, deck_id):
    before = col.decks.selected()
    col.decks.select(deck_id)
    queued = col.sched.get_queued_cards(fetch_limit=10_000)
    col.decks.select(before)
    return [(e.card.id, e.queue) for e in queued.cards]


def test_batch_holds_review_cards_only_in_queue_order(col, deck, exchange, state):
    reviews = [deck.review(f"review {i}", f"answer {i}", overdue=i + 1) for i in range(5)]
    new = [deck.new(f"new {i}") for i in range(3)]
    learning = deck.learning("learning")
    kinds = queue_kinds(col, deck.id)
    assert {k for _, k in kinds} == {0, 1, 2}, "the queue should hold all three kinds"

    result = export_batch(col, deck.id, exchange, state, now_ms=1_700_000_000_000)

    batch = read(exchange / "batch.json")
    assert batch["format_version"] == 1
    assert batch["batch_id"] == result.batch_id
    assert batch["exported_at_ms"] == 1_700_000_000_000
    assert batch["deck_name"] == "Watch"
    ids = [c["card_id"] for c in batch["cards"]]
    assert ids == [cid for cid, kind in kinds if kind == 2]
    assert sorted(ids) == sorted(c.id for c in reviews)
    assert not set(ids) & ({c.id for c in new} | {learning.id})
    by_id = {c["card_id"]: c for c in batch["cards"]}
    assert set(by_id[reviews[0].id]) == {"card_id", "front", "back"}
    assert by_id[reviews[0].id]["front"] == [{"text": "review 0", "styles": []}]
    assert by_id[reviews[0].id]["back"] == [{"text": "answer 0", "styles": []}]


def test_cards_of_other_decks_are_left_out(col, deck, exchange, state):
    mine = deck.review("mine")
    Deck(col, "Other").review("theirs")
    export_batch(col, deck.id, exchange, state)
    assert [c["card_id"] for c in read(exchange / "batch.json")["cards"]] == [mine.id]


def test_batch_is_capped(col, deck, exchange, state):
    for i in range(BATCH_CAP + 10):
        deck.review(f"card {i}")
    expected = [cid for cid, k in queue_kinds(col, deck.id) if k == 2][:BATCH_CAP]

    result = export_batch(col, deck.id, exchange, state)

    ids = [c["card_id"] for c in read(exchange / "batch.json")["cards"]]
    assert BATCH_CAP == 60
    assert len(ids) == 60 and ids == expected
    assert result.exported == 60


def test_selected_deck_is_restored(col, deck, exchange, state):
    deck.review("card")
    other = Deck(col, "Other")
    col.decks.select(other.id)
    export_batch(col, deck.id, exchange, state)
    assert col.decks.selected() == other.id


def test_review_counts_are_kept_by_the_add_on_and_not_sent(col, deck, exchange, state):
    a = deck.review("a", reps=3)
    b = deck.review("b", reps=7)
    result = export_batch(col, deck.id, exchange, state)

    record = read(batch_record_path(state, result.batch_id))
    assert record["batch_id"] == result.batch_id
    assert record["review_counts"] == {str(a.id): 3, str(b.id): 7}
    assert "reps" not in (exchange / "batch.json").read_text()
    assert "review_count" not in (exchange / "batch.json").read_text()
    assert state not in exchange.parents and exchange not in state.parents


def test_status_file(col, deck, exchange, state):
    deck.review("card")
    result = export_batch(col, deck.id, exchange, state, now_ms=1_700_000_000_000)

    status = read(exchange / "status.json")
    assert status["format_version"] == 1
    assert status["addon_version"] == __version__
    assert status["anki_version"] == anki_version
    assert status["deck_name"] == "Watch"
    assert status["last_export"] == {
        "at_ms": 1_700_000_000_000,
        "outcome": "ok",
        "batch_id": result.batch_id,
        "exported": 1,
        "skipped": {},
        "empty_because": None,
        "due_count": 1,
    }
    assert status["last_cycle"] is None


def test_each_export_has_a_new_batch_id(col, deck, exchange, state):
    deck.review("card")
    first = export_batch(col, deck.id, exchange, state)
    second = export_batch(col, deck.id, exchange, state)
    assert first.batch_id != second.batch_id
    assert read(exchange / "batch.json")["batch_id"] == second.batch_id


def test_deck_with_nothing_due_gives_an_empty_batch(col, deck, exchange, state):
    deck.new("not a review card")
    result = export_batch(col, deck.id, exchange, state)
    assert result.outcome == "ok" and result.exported == 0
    assert read(exchange / "batch.json")["cards"] == []
    assert read(exchange / "status.json")["last_export"]["empty_because"] == "nothing_due"


def test_no_deck_chosen_writes_no_batch_and_says_why(col, exchange, state):
    result = export_batch(col, None, exchange, state)
    assert result.outcome == "no_deck_chosen"
    assert not (exchange / "batch.json").exists()
    assert read(exchange / "status.json")["last_export"]["outcome"] == "no_deck_chosen"


def test_missing_deck_writes_no_batch_and_says_why(col, exchange, state):
    result = export_batch(col, 123456789, exchange, state)
    assert result.outcome == "deck_missing"
    assert not (exchange / "batch.json").exists()


def test_card_outside_the_allow_list_does_not_fail_export(col, deck, exchange, state):
    bad = deck.review('<img src="x.png">')
    good = deck.review("fine")
    result = export_batch(col, deck.id, exchange, state)
    assert [c["card_id"] for c in read(exchange / "batch.json")["cards"]] == [good.id]
    assert sum(result.skipped.values()) == 1
    assert read(exchange / "status.json")["last_export"]["skipped"] == result.skipped
    assert bad.id not in [c["card_id"] for c in read(exchange / "batch.json")["cards"]]


def test_log_has_one_export_line_with_ids_and_no_card_content(col, deck, exchange, state):
    a = deck.review("SECRETFRONT", "SECRETBACK")
    result = export_batch(col, deck.id, exchange, state)

    text = (state / "addon.log").read_text()
    lines = [l for l in text.splitlines() if " export " in l]
    assert len(lines) == 1
    assert f"batch={result.batch_id}" in lines[0]
    assert "exported=1" in lines[0] and "skipped=0" in lines[0]
    assert re.search(rf"cards={a.id}\b", lines[0])
    assert "SECRET" not in text and "Watch" not in text


def test_unreadable_status_file_does_not_break_export(col, deck, exchange, state):
    deck.review("card")
    exchange.mkdir()
    (exchange / "status.json").write_text("{not json")
    result = export_batch(col, deck.id, exchange, state)
    assert result.outcome == "ok"
    assert read(exchange / "status.json")["last_export"]["batch_id"] == result.batch_id


def test_export_that_goes_wrong_says_so_and_restores_the_deck(col, deck, exchange, state, monkeypatch):
    deck.review("SECRETFRONT")
    other = Deck(col, "Other")
    col.decks.select(other.id)

    def boom(*a, **k):
        raise RuntimeError("SECRETFRONT")

    monkeypatch.setattr(col.sched, "get_queued_cards", boom)
    result = export_batch(col, deck.id, exchange, state)

    assert result.outcome == "failed"
    assert not (exchange / "batch.json").exists()
    assert read(exchange / "status.json")["last_export"]["outcome"] == "failed"
    assert col.decks.selected() == other.id
    log = (state / "addon.log").read_text()
    assert "export_error kind=RuntimeError" in log and "SECRET" not in log


def test_export_never_raises_even_when_nothing_can_be_written(col, deck, tmp_path):
    deck.review("card")
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    result = export_batch(col, deck.id, blocked / "exchange", blocked / "state")
    assert result.outcome == "failed"


def test_due_count_is_the_whole_backlog_whatever_the_daily_limit(col, deck, exchange, state):
    for n in range(8):
        deck.review(f"due {n}")
    sub = type(deck)(col, "Watch::Verbs")
    sub.review("in a subdeck")
    deck.new("new card")
    deck.learning("learning card")
    deck.review("not due yet", overdue=-3)
    other = type(deck)(col, "Other")
    other.review("another deck")
    suspended = deck.review("suspended")
    suspended.queue = -1
    col.update_card(suspended)
    buried = deck.review("buried")
    buried.queue = -2
    col.update_card(buried)
    # A daily limit below the number due: the batch is cut short, the count is not.
    config = col.decks.config_dict_for_deck_id(deck.id)
    config["rev"]["perDay"] = 3
    col.decks.update_config(config)

    result = export_batch(col, deck.id, exchange, state, now_ms=1_700_000_000_000)

    assert result.due_count == 9
    assert read(exchange / "status.json")["last_export"]["due_count"] == 9
    assert len(read(exchange / "batch.json")["cards"]) < 9


def test_due_count_is_null_when_no_deck_was_exported(col, exchange, state):
    export_batch(col, None, exchange, state, now_ms=1_700_000_000_000)
    assert read(exchange / "status.json")["last_export"]["due_count"] is None


def test_counting_what_is_due_changes_nothing_in_the_collection(col, deck, exchange, state):
    for n in range(4):
        deck.review(f"due {n}")
    before = (col.db.scalar("select max(mod) from cards"), col.db.scalar("select count() from revlog"), col.mod)
    from sukima.export import _due_count

    assert _due_count(col, deck.id) == 4
    after = (col.db.scalar("select max(mod) from cards"), col.db.scalar("select count() from revlog"), col.mod)
    assert before == after


def test_due_count_leaves_out_cards_being_relearned(col, deck, exchange, state):
    from anki.scheduler.v3 import CardAnswer
    from sukima.export import _due_count

    deck.review("still a review")
    lapsed = deck.review("answered Again, now relearning")
    states = col._backend.get_scheduling_states(lapsed.id)
    lapsed.start_timer()
    col.sched.answer_card(col.sched.build_answer(card=lapsed, states=states, rating=CardAnswer.AGAIN))
    assert col.get_card(lapsed.id).queue in (1, 3)

    assert _due_count(col, deck.id) == 1
