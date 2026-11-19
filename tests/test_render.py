"""The render pass in a real Qt web view with Anki's own MathJax (card content spec, ticket 06).

Runs with the Qt WebEngine of the test environment (6.11, as Anki 26.9 ships) and the MathJax
inside the installed Anki.app; skipped when either is missing.
"""

from __future__ import annotations

import time

import pytest

from sukima import maths as m

webengine = pytest.importorskip("PyQt6.QtWebEngineWidgets")
MATHJAX = m.mathjax_dir()
pytestmark = pytest.mark.skipif(MATHJAX is None, reason="Anki's MathJax isn't installed here")


def run_pass(jobs, cache, limit_s=30):
    from PyQt6.QtCore import QCoreApplication, QEventLoop

    from sukima.render import MathsRenderer

    logged = []
    renderer = MathsRenderer(MATHJAX, log=lambda what, **f: logged.append((what, f)))
    assert renderer.start(jobs, cache)
    deadline = time.monotonic() + limit_s
    while renderer.busy and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    assert not renderer.busy
    return renderer, logged


def alpha_of(path):
    from PyQt6.QtGui import QImage

    image = QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    data = bytes(bits)
    return image, data[3::4], (data[0::4], data[1::4], data[2::4])


@pytest.fixture
def cache(tmp_path):
    return m.MathsCache(tmp_path / "maths", m.mathjax_id(MATHJAX))


def test_formulas_become_masks_with_their_baseline(cache):
    inline = m.Formula(r"x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}", False)
    display = m.Formula(r"\int_0^1 x^2\,dx = \frac{1}{3}", True)
    letter = m.Formula("x", False)
    _, logged = run_pass([inline, display, letter], cache)
    assert logged[-1] == ("render_pass", {"result": "done", "drawn": 3, "failed": 0, "timed_out": 0})

    drawn = cache.get(inline)
    image, alpha, colour = alpha_of(cache.path(drawn))
    assert (image.width(), image.height()) == (drawn.file_width, drawn.file_height)
    # A mask: black everywhere, smooth edges, and clear background.
    assert set(colour[0]) == set(colour[1]) == set(colour[2]) == {0}
    assert alpha[0] == 0 and max(alpha) >= 250 and len(set(alpha)) > 50
    # 18 pt type: an x is about 8 pt high; the fraction reaches well below the baseline.
    assert 7 <= drawn.ex <= 9
    assert 5 <= drawn.depth <= 12
    assert 90 <= drawn.width <= 140

    # Nothing but the formula: a letter's mask holds no plate or mark in its corner.
    _, x_alpha, _ = alpha_of(cache.path(cache.get(letter)))
    assert x_alpha[0] == 0
    x = cache.get(letter)
    assert x.depth <= 1.5  # an x sits on the baseline
    assert 7 <= x.height <= 11
    assert cache.get(display).depth == 0


def test_a_formula_mathjax_cant_read_is_kept_as_failed(cache):
    bad = m.Formula(r"\undefinedmacro{x}", False)
    _, logged = run_pass([bad, m.Formula("y", False)], cache)
    assert cache.get(bad) == m.FAILED
    assert logged[-1][1]["failed"] == 1 and logged[-1][1]["drawn"] == 1


def test_macros_from_the_card_are_used(cache):
    macros = m.macros_json({"coloneqq": ["\\mathrel{\\vcenter{:}}=", 0]})
    with_macro = m.Formula(r"f \coloneqq g", False, macros)
    without = m.Formula(r"f \coloneqq g", False)
    run_pass([with_macro, without], cache)
    assert isinstance(cache.get(with_macro), m.Drawn)
    # MathJax's mathtools has its own \coloneqq, so this draws too; the cache keeps them apart.
    assert cache.get(without) != cache.get(with_macro) or isinstance(cache.get(without), m.Drawn)


def test_after_a_table_a_formula_has_no_plate_under_it(cache):
    table = m.Table("<table><tr><td>a</td></tr></table>")
    letter = m.Formula("x", False)
    run_pass([table, letter], cache)
    image, alpha, _ = alpha_of(cache.path(cache.get(letter)))
    # Trimmed to the letter's ink: 20 × 20 px; an empty plate under it made it 24 × 22 (seen).
    assert image.height() <= 21 and image.width() <= 21


def test_a_table_is_drawn_in_its_own_colours(cache):
    table = m.Table("<table><tr><th>は</th><th>wa</th></tr><tr><td>topic marker</td><td>が subject</td></tr></table>")
    run_pass([table], cache)
    drawn = cache.get(table)
    image, alpha, colour = alpha_of(cache.path(drawn))
    assert drawn.mask is False
    assert set(alpha) == {255}  # opaque
    # The plate's colour (#F2F0EB) in the corner, dark text inside.
    corner = image.pixelColor(2, 2)
    assert (corner.red(), corner.green(), corner.blue()) == (0xF2, 0xF0, 0xEB)
    assert min(colour[0]) < 60
    assert drawn.width <= m.TABLE_WIDTH_PT


def test_thirty_formulas_draw_quickly(cache):
    jobs = [m.Formula(f"a_{{{i}}} + b^{{{i}}} = c", i % 2 == 0) for i in range(30)]
    started = time.monotonic()
    _, logged = run_pass(jobs, cache)
    assert logged[-1][1]["drawn"] == 30
    assert time.monotonic() - started < 10
