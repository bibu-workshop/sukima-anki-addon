"""Hints, batch format 6 (card content spec, ticket 04; design guide 1.26 "Hints").

Only the link and hidden div Anki itself writes for `{{hint:Field}}` become hint blocks; any
other scripted button still keeps the card in Anki.
"""

from __future__ import annotations

import json
import os

import pytest
from PyQt6.QtGui import QGuiApplication, QImage

from sukima.blocks import _take_hints, card_sides_v2
from sukima.export import export_batch
from sukima.images import remember_helper_formats
from sukima.text import SCRIPT


@pytest.fixture(scope="module", autouse=True)
def qt():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def hinted(col):
    """A note type whose front shows {{hint:Mnemonic}} and {{hint:Extra}} after the question."""
    mm = col.models
    m = mm.copy(mm.by_name("Basic"), add=False)
    m["name"] = "Hinted"
    for name in ("Mnemonic", "Extra"):
        mm.add_field(m, mm.new_field(name))
    m["tmpls"][0]["qfmt"] = "{{Front}}<br>{{hint:Mnemonic}}{{hint:Extra}}"
    m["tmpls"][0]["afmt"] = "{{FrontSide}}<hr id=answer>{{Back}}"
    mm.add(m)
    return "Hinted"


def review(deck, front, back, mnemonic="", extra=""):
    return deck.review(front, back, model="Hinted", fields={"Front": front, "Back": back, "Mnemonic": mnemonic, "Extra": extra})


def sides(col, card, hints=True):
    return card_sides_v2(card, col.media.dir(), sounds=True, code=True, hints=hints)


def test_a_hint_is_a_block_in_its_place_with_its_link_text(col, deck, hinted):
    card = review(deck, "Which nerve supplies the deltoid?", "Axillary nerve", mnemonic="Same nerve as <b>teres minor</b>.")
    front, back, front_blocks, back_blocks, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [
        {"text": [{"text": "Which nerve supplies the deltoid?", "styles": []}]},
        {"hint": [{"text": [{"text": "Same nerve as ", "styles": []}, {"text": "teres minor", "styles": ["bold"]},
                            {"text": ".", "styles": []}]}],
         "label": "Mnemonic"},
    ]
    # The front's text alone doesn't give the hint away.
    assert front == [{"text": "Which nerve supplies the deltoid?", "styles": []}]
    assert back == [{"text": "Axillary nerve", "styles": []}]


def test_without_format_6_a_hint_keeps_the_card_in_anki(col, deck, hinted):
    card = review(deck, "Q", "A", mnemonic="a hint")
    assert sides(col, card, hints=False)[-1] == SCRIPT


def test_two_hints_in_the_cards_order_and_an_empty_field_draws_none(col, deck, hinted):
    card = review(deck, "Q", "A", mnemonic="first", extra="second")
    *_, front_blocks, _, reason = sides(col, card)
    assert reason is None
    assert [b["label"] for b in front_blocks if "hint" in b] == ["Mnemonic", "Extra"]
    card = review(deck, "Q", "A", extra="only")
    *_, front_blocks, _, _ = sides(col, card)
    assert [b["label"] for b in front_blocks if "hint" in b] == ["Extra"]


def test_a_hint_holding_its_own_divs_ends_at_its_own_div(col, deck, hinted):
    card = review(deck, "Q", "A", mnemonic="<div>one</div><div>two</div>")
    *_, front_blocks, _, reason = sides(col, card)
    assert reason is None
    hint = front_blocks[-1]
    assert hint["label"] == "Mnemonic"
    assert "".join(r["text"] for b in hint["hint"] for r in b["text"]) == "one\ntwo"


def test_any_other_scripted_button_still_keeps_the_card_in_anki(col, deck):
    card = deck.review("""Q<a href="#" onclick="document.getElementById('x').style.display='block'">Show</a>"""
                       """<div id="x" style="display:none">secret</div>""", "A")
    assert sides(col, card)[-1] == SCRIPT


def test_older_ankis_markup_and_an_empty_link_text():
    html = ("""<a class=hint href="#"\nonclick="this.style.display='none';\n"""
            """document.getElementById('hint1a').style.display='block';\nwindow.location.hash='hint1a';\n"""
            """return false;">\n</a>\n<div id="hint1a" class=hint style="display: none">x</div>""")
    rest, hints = _take_hints(html)
    assert hints == [("Hint", "x")]
    assert "onclick" not in rest


def test_a_hint_with_an_image_goes_with_the_image_prepared(col, deck, hinted, exchange, state):
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(0xFF3366)
    image.save(os.path.join(col.media.dir(), "plexus.png"))
    review(deck, "Which nerve?", "Axillary", mnemonic="<img src='plexus.png'>")
    remember_helper_formats(state, [1, 2, 3, 4, 5, 6])
    export_batch(col, deck.id, exchange, state)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 6
    hint = batch["cards"][0]["front_blocks"][-1]
    [inner] = hint["hint"]
    assert inner["image"] == batch["images"][0]["hash"]
    assert (exchange / "images").exists() and len(os.listdir(exchange / "images")) == 1


def test_the_export_writes_format_6_only_when_the_helper_passes_it_on(col, deck, hinted, exchange, state):
    review(deck, "Q", "A", mnemonic="a hint")
    remember_helper_formats(state, [1, 2, 3, 4, 5])
    result = export_batch(col, deck.id, exchange, state)
    assert result.skipped == {SCRIPT: 1}
    remember_helper_formats(state, [1, 2, 3, 4, 5, 6])
    export_batch(col, deck.id, exchange, state)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 6 and len(batch["cards"]) == 1
