"""Code blocks, batch format 4 (card content spec, ticket 02; design guide 1.26)."""

from __future__ import annotations

import json

from sukima.blocks import card_sides_v2, code_text
from sukima.export import export_batch
from sukima.images import remember_helper_formats
from sukima.text import MATHS, OTHER_MARKUP

# Anki's syntax highlighter, as in the owner's Neetcode cards: line numbers in their own cell.
HIGHLIGHTED = (
    '<table class="highlighttable"><tbody><tr><td class="linenos"><div class="linenodiv"><pre>1\n2\n3</pre></div></td>'
    '<td class="code"><div class="highlight"><pre><span style="color: #008000">class</span> Node {\n'
    '    <span>var</span> value: Int\n\tvar next: Node?\n}\n</pre></div></td></tr></tbody></table>'
)


def sides(col, card):
    return card_sides_v2(card, col.media.dir(), sounds=True, code=True)


def test_the_highlighters_text_keeps_its_indentation_and_drops_line_numbers():
    assert code_text(HIGHLIGHTED) == "class Node {\n    var value: Int\n\tvar next: Node?\n}"


def test_a_code_block_is_its_own_block_in_place(col, deck):
    card = deck.review("What does this define?<br>" + HIGHLIGHTED, "a linked list node")
    front, _, front_blocks, _, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [
        {"text": [{"text": "What does this define?", "styles": []}]},
        {"code": "class Node {\n    var value: Int\n\tvar next: Node?\n}"},
    ]
    assert front == [{"text": "What does this define?", "styles": []}]


def test_pre_and_escaped_characters(col, deck):
    card = deck.review("<pre>if a &lt; b &amp;&amp; c:\n    return a</pre>", "the smaller")
    *_, front_blocks, _, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [{"code": "if a < b && c:\n    return a"}]


def test_inline_code_is_a_run_style_in_the_sentence(col, deck):
    card = deck.review("What does <code>len(xs)</code> return?", "the length")
    front, _, front_blocks, _, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [{"text": [
        {"text": "What does ", "styles": []}, {"text": "len(xs)", "styles": ["code"]}, {"text": " return?", "styles": []},
    ]}]


def test_center_is_a_plain_block_and_small_plain_text(col, deck):
    card = deck.review("<center>Two Sum</center>", "a hash map")
    assert sides(col, card)[-1] is None
    card = deck.review('<p><small style="color:#888">Rust Flashcards</small></p><p>A new package?</p>', "cargo new")
    assert sides(col, card)[-1] is None


def test_a_backslash_bracket_inside_code_is_not_maths(col, deck):
    card = deck.review(r"<pre>re.match(r'\(\d+\)', s)</pre>", "digits in brackets")
    assert sides(col, card)[-1] is None
    # Real maths in the text still keeps the card in Anki (maths isn't built yet).
    card = deck.review(r"Solve \(x^2 = 4\)", "x = ±2")
    assert sides(col, card)[-1] == MATHS


def test_without_format_4_code_still_keeps_the_card_in_anki(col, deck):
    card = deck.review("<pre>x = 1</pre>", "one")
    assert card_sides_v2(card, col.media.dir(), sounds=True)[-1] == OTHER_MARKUP


def test_the_export_writes_format_4_only_when_the_helper_passes_it_on(col, deck, exchange, state):
    deck.review("<code>x</code>", "a variable")
    remember_helper_formats(state, [1, 2, 3])
    export_batch(col, deck.id, exchange, state)
    assert json.loads((exchange / "batch.json").read_text())["format_version"] == 1
    remember_helper_formats(state, [1, 2, 3, 4])
    export_batch(col, deck.id, exchange, state)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 4
    assert batch["cards"][0]["front_blocks"][0]["text"][0]["styles"] == ["code"]
