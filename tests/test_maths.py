"""Maths and tables, batch format 5 (card content spec, ticket 06; design guide 1.26).

The drawing itself needs Anki's web view (`test_render.py`); here the cache is filled by hand
and everything around it is tested: finding formulas, macro scripts, the conversion, the export,
and the cycle's render step.
"""

from __future__ import annotations

import json

import pytest

from sukima import maths as m
from sukima.blocks import card_sides_v2
from sukima.cycle import DesktopCycle
from sukima.export import export_batch
from sukima.files import write_json
from sukima.images import remember_helper_formats
from sukima.settings import choose_deck
from sukima.text import MATHS, SCRIPT, TABLE

DZ_SCRIPT = (
    "<script>\nMathJax.config.tex.macros = {\n\tcoloneqq: ['\\\\mathrel{\\\\vcenter{:}}=', 0]\n};\n"
    "MathJax.startup.getComponents();\n</script>"
)


@pytest.fixture
def cache(tmp_path):
    return m.MathsCache(tmp_path / "maths-cache", "mathjax-test")


def draw(cache, tex, display=False, macros="{}", size=(40, 30), depth=4.0, ex=8.0):
    """Stands in for the render pass: a fake PNG for the formula."""
    return cache.put(m.Formula(tex, display, macros), f"png {tex} {display} {macros}".encode(), size, depth, ex)


def sides(col, card, cache, collect=False):
    lookup = m.MathsLookup(cache, collect=collect)
    return card_sides_v2(card, col.media.dir(), sounds=True, code=True, maths=lookup), lookup


def strip(blocks):
    """Blocks without the export's private notes."""
    out = []
    for b in blocks or []:
        b = {k: v for k, v in b.items() if k != "_drawn"}
        if "text" in b:
            b["text"] = [{k: v for k, v in r.items() if k != "_drawn"} for r in b["text"]]
        out.append(b)
    return out


# -- macro scripts ---------------------------------------------------------------------

def test_dzackgarzas_script_is_read_as_its_macros():
    body = DZ_SCRIPT.removeprefix("<script>").removesuffix("</script>")
    assert m.macros_from_script(body) == {"coloneqq": ["\\mathrel{\\vcenter{:}}=", 0]}


@pytest.mark.parametrize("script", [
    "alert(1)",
    "MathJax.config.tex.macros = {a: 'x'}; alert(1);",
    "MathJax.config.tex.macros = {a: f()};",
    "MathJax.config.tex.macros = {a: '\\u0041'};",
    "document.body.innerHTML = ''",
])
def test_any_other_script_is_not_read(script):
    assert m.macros_from_script(script) is None


def test_several_macros_plain_and_with_arguments():
    script = """MathJax.config.tex.macros = { RR: '{\\\\bf R}', "norm": ["\\\\left\\\\| #1 \\\\right\\\\|", 1], };"""
    assert m.macros_from_script(script) == {"RR": ["{\\bf R}", 0], "norm": ["\\left\\| #1 \\right\\|", 1]}


# -- finding formulas --------------------------------------------------------------------

def test_formulas_are_found_with_their_html_undone():
    html, formulas = m.take_maths(r"If \(a &lt; b\) then <br>\[a<br>+ c &lt; b + c\]")
    assert formulas == [m.Formula("a + c < b + c", True), m.Formula("a < b", False)]
    assert "sukima-maths" in html and "\\(" not in html


# -- the conversion ----------------------------------------------------------------------

def test_inline_maths_is_a_run_in_its_sentence(col, deck, cache):
    drawn = draw(cache, "x^2 = 4")
    card = deck.review(r"Solve \(x^2 = 4\) for x", "x = ±2")
    (front, _, front_blocks, _, reason), _ = sides(col, card, cache)
    assert reason is None
    assert strip(front_blocks) == [{"text": [
        {"text": "Solve ", "styles": []},
        {"text": "x^2 = 4", "styles": ["maths"], "maths": drawn.hash},
        {"text": " for x", "styles": []},
    ]}]
    # The text alone keeps the TeX, with no mask.
    assert front == [{"text": "Solve x^2 = 4 for x", "styles": []}]


def test_two_formulas_side_by_side_stay_two_runs(col, deck, cache):
    a, b = draw(cache, "a"), draw(cache, "b")
    card = deck.review(r"\(a\)\(b\)", "two")
    (_, _, front_blocks, _, reason), _ = sides(col, card, cache)
    assert reason is None
    assert [r.get("maths") for r in strip(front_blocks)[0]["text"]] == [a.hash, b.hash]


def test_display_maths_is_a_block_of_its_own(col, deck, cache):
    drawn = draw(cache, r"\int_0^1 x^2\,dx = \frac{1}{3}", display=True)
    card = deck.review(r"Evaluate \[\int_0^1 x^2\,dx = \frac{1}{3}\] please", "1/3")
    (_, _, front_blocks, _, reason), _ = sides(col, card, cache)
    assert reason is None
    assert strip(front_blocks) == [
        {"text": [{"text": "Evaluate", "styles": []}]},
        {"maths": drawn.hash, "tex": r"\int_0^1 x^2\,dx = \frac{1}{3}"},
        {"text": [{"text": "please", "styles": []}]},
    ]


def test_a_formula_not_drawn_keeps_the_card_in_anki_and_collecting_notes_it(col, deck, cache):
    card = deck.review(r"Solve \(x^2 = 4\)", r"\[x = \pm 2\]")
    (*_, reason), _ = sides(col, card, cache)
    assert reason == MATHS
    (*_, reason), lookup = sides(col, card, cache, collect=True)
    assert reason is None
    assert list(lookup.wanted.values()) == [m.Formula("x^2 = 4", False), m.Formula(r"x = \pm 2", True)]


def test_a_formula_mathjax_cant_read_is_not_asked_for_again(col, deck, cache):
    cache.put_failed(m.Formula(r"\oops", False))
    card = deck.review(r"\(\oops\)", "back")
    (*_, reason), lookup = sides(col, card, cache, collect=True)
    assert reason == MATHS and not lookup.wanted


def test_a_macro_script_is_read_and_its_macros_go_with_the_formulas(col, deck, cache):
    macros = m.macros_json({"coloneqq": ["\\mathrel{\\vcenter{:}}=", 0]})
    draw(cache, r"f \coloneqq g", macros=macros)
    card = deck.review(r"<p>Define</p>" + DZ_SCRIPT, r"\(f \coloneqq g\)" + DZ_SCRIPT)
    (*_, reason), _ = sides(col, card, cache)
    assert reason is None
    # Without format 5 the same card stays in Anki for its script.
    assert card_sides_v2(card, col.media.dir(), sounds=True, code=True)[-1] == SCRIPT


def test_a_script_that_does_more_still_keeps_the_card_in_anki(col, deck, cache):
    card = deck.review("Front<script>MathJax.config.tex.macros = {a: 'b'}; alert(1)</script>", "back")
    (*_, reason), _ = sides(col, card, cache)
    assert reason == SCRIPT


# -- tables ------------------------------------------------------------------------------

TABLE_HTML = "<table><tr><th>は</th><th>wa</th></tr><tr><td>topic</td><td>marker</td></tr></table>"


def test_a_table_is_drawn_as_a_picture(col, deck, cache):
    drawn = cache.put(m.Table(TABLE_HTML), b"table png", (380, 120))
    card = deck.review("Particles " + TABLE_HTML, "back")
    (_, _, front_blocks, _, reason), _ = sides(col, card, cache)
    assert reason is None
    assert strip(front_blocks)[1] == {"image": drawn.hash, "beside_previous": False, "alt": None}
    assert drawn.mask is False


def test_a_table_not_drawn_or_holding_a_picture_stays_in_anki(col, deck, cache):
    card = deck.review("Particles " + TABLE_HTML, "back")
    assert sides(col, card, cache)[0][-1] == TABLE
    with_image = '<table><tr><td><img src="x.png"></td></tr></table>'
    _, tables = m.take_tables(with_image)
    assert tables == []
    _, tables = m.take_tables("<table><tr><td>Hidden Markov model</td></tr></table>")
    assert len(tables) == 1


# -- the export --------------------------------------------------------------------------

def test_the_export_writes_format_5_with_masks_in_the_image_table(col, deck, exchange, state, cache):
    drawn = draw(cache, "e^{i\\pi} + 1 = 0", size=(180, 51), depth=6.5, ex=8.05)
    deck.review(r"\(e^{i\pi} + 1 = 0\)", "Euler")
    remember_helper_formats(state, [1, 2, 3, 4, 5])
    export_batch(col, deck.id, exchange, state, maths=cache)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 5
    assert batch["images"] == [{
        "hash": drawn.hash, "width": 90, "height": 26, "file_width": 180, "file_height": 51,
        "transparent": True, "bytes": drawn.bytes, "mask": True, "depth": 6.5, "ex": 8.05,
    }]
    assert batch["cards"][0]["front_blocks"] == [
        {"text": [{"text": "e^{i\\pi} + 1 = 0", "styles": ["maths"], "maths": drawn.hash}]}]
    assert (exchange / "images" / drawn.file_name).read_bytes() == cache.path(drawn).read_bytes()


def test_without_format_5_maths_stays_in_anki(col, deck, exchange, state, cache):
    draw(cache, "x")
    deck.review(r"\(x\)", "a variable")
    remember_helper_formats(state, [1, 2, 3, 4])
    result = export_batch(col, deck.id, exchange, state, maths=cache)
    assert result.skipped == {MATHS: 1}


def test_a_batch_whose_only_new_thing_is_a_table_needs_only_format_2(col, deck, exchange, state, cache):
    drawn = cache.put(m.Table(TABLE_HTML), b"table png", (380, 120))
    deck.review(TABLE_HTML, "back")
    remember_helper_formats(state, [1, 2, 3, 4, 5])
    export_batch(col, deck.id, exchange, state, maths=cache)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 2
    assert batch["images"][0]["hash"] == drawn.hash and "mask" not in batch["images"][0]
    assert batch["images"][0]["transparent"] is False


def test_collecting_writes_nothing_and_lists_what_to_draw(col, deck, exchange, state, cache):
    deck.review(r"\(a\) and \(b\)", r"\(a\)")
    remember_helper_formats(state, [1, 2, 3, 4, 5])
    result = export_batch(col, deck.id, exchange, state, maths=cache, collect=True)
    assert result.wanted == [m.Formula("a", False), m.Formula("b", False)]
    assert not (exchange / "batch.json").exists()


# -- the cycle ---------------------------------------------------------------------------

class FakeAnki:
    """Just enough of the running app for one cycle with sync off."""

    def __init__(self, cache):
        self.cache, self.clock, self.rendering, self.reviewing, self.asked = cache, 1_800_000_000_000, None, False, []

    def now_ms(self): return self.clock
    def anki_version(self): return (26, 9)
    def sync_enabled(self): return False
    def sync_running(self): return False
    def on_review_screen(self): return self.reviewing
    def anki_busy(self): return None
    def refresh(self): pass
    def maths_cache(self, state_dir): return self.cache
    def start_render(self, jobs, cache):
        self.asked.append(("render", list(jobs)))
        self.rendering = jobs
        return True
    def render_done(self): return self.rendering is None


def test_the_cycle_draws_maths_after_replay_and_exports_once_it_is_drawn(col, deck, exchange, state, cache):
    deck.review(r"Solve \(x^2 = 4\)", "x = ±2")
    choose_deck(col, state, deck.id)
    remember_helper_formats(state, [1, 2, 3, 4, 5])
    anki = FakeAnki(cache)
    cycle = DesktopCycle(anki, exchange, state)
    write_json(exchange / "cycle-request.json", {
        "format_version": 1, "request_id": "R-1", "requested_at_ms": anki.clock, "batch_formats": [1, 2, 3, 4, 5]})

    cycle.poll(col)
    assert anki.asked == [("render", [m.Formula("x^2 = 4", False)])]
    assert not (exchange / "batch.json").exists()  # not before the maths is drawn

    cycle.poll(col)
    assert not (exchange / "batch.json").exists()  # still drawing

    draw(cache, "x^2 = 4")
    anki.rendering = None
    anki.reviewing = True
    cycle.poll(col)
    assert not (exchange / "batch.json").exists()  # drawn, but the review screen is open

    anki.reviewing = False
    cycle.poll(col)
    batch = json.loads((exchange / "batch.json").read_text())
    assert batch["format_version"] == 5 and len(batch["cards"]) == 1
    status = json.loads((exchange / "status.json").read_text())
    assert status["last_cycle"]["outcome"] == "done"


def test_with_everything_drawn_the_cycle_exports_at_once(col, deck, exchange, state, cache):
    draw(cache, "x")
    deck.review(r"\(x\)", "a variable")
    choose_deck(col, state, deck.id)
    anki = FakeAnki(cache)
    cycle = DesktopCycle(anki, exchange, state)
    write_json(exchange / "cycle-request.json", {
        "format_version": 1, "request_id": "R-1", "requested_at_ms": anki.clock, "batch_formats": [1, 2, 3, 4, 5]})
    cycle.poll(col)
    assert anki.asked == []
    assert json.loads((exchange / "batch.json").read_text())["format_version"] == 5
