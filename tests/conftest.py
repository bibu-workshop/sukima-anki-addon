"""Fixtures that build a real Anki collection for each test, with no Anki window."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anki.collection import Collection  # noqa: E402
from anki.scheduler.v3 import CardAnswer  # noqa: E402


class Deck:
    """A deck in a test collection, with helpers to put cards in each state."""

    def __init__(self, col: Collection, name: str) -> None:
        self.col = col
        self.id = col.decks.id(name)
        self.name = name

    def new(self, front: str, back: str = "back", model: str = "Basic", fields: dict | None = None):
        note = self.col.new_note(self.col.models.by_name(model))
        for name, value in (fields or {"Front": front, "Back": back}).items():
            note[name] = value
        self.col.add_note(note, self.id)
        return note.cards()[0]

    def review(self, front: str = "", back: str = "back", overdue: int = 1, reps: int = 3, **kw):
        card = self.new(front, back, **kw)
        card.type = card.queue = 2
        card.due = self.col.sched.today - overdue
        card.ivl, card.factor, card.reps = 10, 2500, reps
        self.col.update_card(card)
        return card

    def learning(self, front: str):
        card = self.new(front)
        states = self.col._backend.get_scheduling_states(card.id)
        card.start_timer()
        self.col.sched.answer_card(
            self.col.sched.build_answer(card=card, states=states, rating=CardAnswer.AGAIN)
        )
        return card


@pytest.fixture
def col(tmp_path):
    c = Collection(str(tmp_path / "collection.anki2"))
    yield c
    c.close()


@pytest.fixture
def deck(col):
    return Deck(col, "Watch")


@pytest.fixture
def exchange(tmp_path):
    return tmp_path / "exchange"


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"
