# SPDX-License-Identifier: AGPL-3.0-or-later
"""The render pass: Anki's own MathJax draws formulas and tables in a hidden web view
(card content spec, ticket 06; `maths.py` for what is drawn and where it is kept).

It runs on Anki's main thread without blocking it: each step is a Qt callback or timer, and
the desktop cycle polls `busy` (no nested event loop, so nothing can re-enter the cycle).

- One page, loaded once per set of macros, with Anki's MathJax and Anki's TeX settings, but
  without `noerrors`, so a formula MathJax can't read is known as such.
- Each formula is drawn black on white at 18 pt and 2 pixels per point, its box measured
  (with its depth below the baseline and the font's ex), grabbed, and turned into a mask:
  alpha = 255 − luminance (a web view's grab is never transparent; spike, 2026-11-18).
- Each table is drawn in its own colours on `imagePlate`'s colour, 6 pt inside it, at most the
  card's width, and kept as a PNG (guide 1.26, `Seal-Maths` E).
- A formula not drawn within 5 s is left (its card stays in Anki, and it is tried again next
  time); one MathJax can't read is kept as failed. The pass stops after 60 s.

Nothing is fetched: the page is built here and loads only Anki's MathJax files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from . import maths as m

# The page's script. `sukimaFormula` and `sukimaTable` leave their result in window.__sukima,
# tagged with the job's number, after two frames so the grab sees the drawing.
_SCRIPT = r"""
async function sukimaFrames() {
  await document.fonts.ready;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
}
// The job's mark: a corner in a colour of its own, so a grab can tell it shows this job.
function sukimaMark(id) {
  document.getElementById('s').style.background =
    `rgb(${id % 251}, ${Math.floor(id / 251) % 251}, 200)`;
}
function sukimaBox(el) {
  const r = el.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
}
async function sukimaFormula(id, tex, display) {
  window.__sukima = null;
  try {
    document.getElementById('t').innerHTML = '';
    const c = document.getElementById('c');
    c.className = display ? 'display' : '';
    c.innerHTML = '';
    MathJax.texReset();
    const mml = MathJax.tex2mml(tex, {display});
    if (/<merror\b|data-mjx-error/.test(mml)) { window.__sukima = {id, error: 'tex'}; return; }
    const node = await MathJax.tex2chtmlPromise(tex, {display});
    c.appendChild(node);
    MathJax.startup.document.clear();
    MathJax.startup.document.updateDocument();
    await sukimaFrames();
    const math = node.querySelector('mjx-math') || node;
    const box = sukimaBox(math);
    // The baseline: a zero-height inline box sits on it.
    const base = document.createElement('span');
    base.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
    const ex = document.createElement('span');
    ex.style.cssText = 'display:inline-block;width:1ex;height:0';
    node.after(base, ex);
    const baseline = base.getBoundingClientRect().top;
    const exWidth = ex.getBoundingClientRect().width;
    base.remove(); ex.remove();
    await sukimaFrames();
    sukimaMark(id);
    await sukimaFrames();
    window.__sukima = {id, box, depth: box[1] + box[3] - baseline, ex: exWidth};
  } catch (e) {
    window.__sukima = {id, error: String(e)};
  }
}
async function sukimaTable(id, html) {
  window.__sukima = null;
  try {
    document.getElementById('c').innerHTML = '';
    const t = document.getElementById('t');
    t.innerHTML = html;
    await sukimaFrames();
    sukimaMark(id);
    await sukimaFrames();
    window.__sukima = {id, box: sukimaBox(t), plate: true};
  } catch (e) {
    window.__sukima = {id, error: String(e)};
  }
}
"""


def page_html(mathjax_url: str, macros: dict) -> str:
    """The page for one set of macros. Anki's own TeX settings (`mathjax.js` in Anki's web
    folder): mathtools, no textmacros. noerrors and noundefined are left out on purpose: they
    draw a bad formula or an unknown macro as text, with no error to see."""
    config = {
        "tex": {
            "inlineMath": [], "displayMath": [], "processEscapes": False, "processEnvironments": False,
            "processRefs": False, "packages": {"[+]": ["mathtools"], "[-]": ["textmacros", "noerrors", "noundefined"]},
            "macros": macros,
        },
        "loader": {"load": ["[tex]/mathtools"], "paths": {"mathjax": mathjax_url.rstrip("/")}},
        "startup": {"typeset": False},
        "options": {"enableMenu": False},
    }
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; background: #fff; color: #000; }}
#c {{ position: absolute; left: 4px; top: 4px; font: {m.FONT_PT}px serif; white-space: nowrap; }}
#c.display {{ display: inline-block; }}
mjx-container {{ margin: 0 !important; }}
#s {{ position: absolute; left: 0; top: 0; width: 2px; height: 2px; }}
#t {{ position: absolute; left: 4px; top: 4px; width: max-content; max-width: {m.TABLE_WIDTH_PT}px;
  box-sizing: border-box; padding: {m.TABLE_MARGIN_PT}px; background: {m.TABLE_PLATE};
  font: 15px -apple-system, "Helvetica Neue", sans-serif; color: #000; }}
#t table {{ border-collapse: collapse; }}
#t:empty, #c:empty {{ display: none; }}  /* an empty plate would show under a formula */
#t td, #t th {{ border: 1px solid #b8b4ac; padding: 2px 4px; vertical-align: top; }}
</style><script>
window.MathJax = {json.dumps(config)};
window.MathJax.startup.ready = () => {{ MathJax.startup.defaultReady(); document.title = 'ready'; }};
{_SCRIPT}
</script><script src="{mathjax_url}{m.MATHJAX_FILE}"></script></head>
<body><div id="s"></div><div id="c"></div><div id="t"></div></body></html>"""


# Lighter than this is paper, not ink, when trimming.
_INK = 8


def mask_png(image) -> tuple[bytes, tuple[int, int], int] | None:
    """Black on white as a mask: each pixel's alpha is 255 less its luminance; colour black.
    Trimmed to the ink with 1 pixel to spare. Also returns how many rows were cut from the
    bottom (to correct the depth). None when there is no ink."""
    from PyQt6.QtGui import QImage

    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h = gray.width(), gray.height()
    bits = gray.constBits()
    bits.setsize(gray.sizeInBytes())
    src, line = bytes(bits), gray.bytesPerLine()
    invert = bytes(range(255, -1, -1))
    rows = [src[y * line:y * line + w].translate(invert) for y in range(h)]
    inked = [y for y, row in enumerate(rows) if max(row, default=0) > _INK]
    if not inked:
        return None
    top, bottom = max(0, inked[0] - 1), min(h, inked[-1] + 2)
    columns = [x for x in range(w) if any(rows[y][x] > _INK for y in range(top, bottom))]
    left, right = max(0, columns[0] - 1), min(w, columns[-1] + 2)
    cw, ch = right - left, bottom - top
    argb = bytearray(cw * ch * 4)  # ARGB32 is B, G, R, A on a little-endian Mac; black: B=G=R=0
    for i, y in enumerate(range(top, bottom)):
        argb[i * cw * 4 + 3:(i + 1) * cw * 4:4] = rows[y][left:right]
    out = QImage(bytes(argb), cw, ch, cw * 4, QImage.Format.Format_ARGB32)
    return _png(out.copy()), (cw, ch), h - bottom


def _png(image) -> bytes:
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QImageWriter

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    QImageWriter(buffer, b"PNG").write(image)
    return bytes(buffer.data())


class MathsRenderer:
    """Draws jobs into a cache, one after another, from Qt callbacks. One pass at a time."""

    POLL_MS = 10
    PAD = 1
    PAGE_LIMIT_MS = 10_000

    def __init__(self, mathjax_folder: Path, log: Callable[..., None] | None = None) -> None:
        self.mathjax_url = mathjax_folder.as_uri() + "/"
        self.log = log or (lambda *a, **k: None)
        self.view = None
        self.busy = False
        self.page_macros: str | None = None
        self.counts: dict[str, int] = {}

    # -- the pass -------------------------------------------------------------------------

    def start(self, jobs: list, cache: m.MathsCache, on_done: Callable[[], None] | None = None) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.cache, self.on_done = cache, on_done
        # Tables need no macros; formulas go in groups that share theirs, one page each.
        order = sorted(range(len(jobs)), key=lambda i: (jobs[i].macros if isinstance(jobs[i], m.Formula) else "{}", i))
        self.jobs = [jobs[i] for i in order]
        self.next = 0
        self.pass_started = time.monotonic()
        self.counts = {"drawn": 0, "failed": 0, "timed_out": 0}
        try:
            self._make_view()
            self._step()
        except Exception as e:
            self._finish(f"error:{type(e).__name__}")
        return True

    def _make_view(self) -> None:
        if self.view is not None:
            self.view.show()
            return
        from PyQt6.QtCore import QSize, Qt
        from PyQt6.QtWebEngineWidgets import QWebEngineView

        view = QWebEngineView()
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.resize(QSize(4000, 2400))  # device pixels: 2000 × 1200 points of page
        view.setZoomFactor(m.SCALE)
        view.show()
        self.view = view

    def _step(self) -> None:
        """The next job, loading the page first when its macros differ."""
        if self.next >= len(self.jobs):
            return self._finish("done")
        if (time.monotonic() - self.pass_started) * 1000 > m.PASS_LIMIT_MS:
            return self._finish("pass_limit")
        job = self.jobs[self.next]
        macros = job.macros if isinstance(job, m.Formula) else (self.page_macros or "{}")
        if macros != self.page_macros:
            return self._load_page(macros)
        self._draw(job)

    def _load_page(self, macros: str) -> None:
        from PyQt6.QtCore import QUrl

        self.page_macros = None
        started = time.monotonic()
        self.view.setHtml(page_html(self.mathjax_url, json.loads(macros)), QUrl(self.mathjax_url))

        def ready() -> None:
            if self.view.page().title() == "ready":
                self.page_macros = macros
                return self._step()
            if (time.monotonic() - started) * 1000 > self.PAGE_LIMIT_MS:
                return self._finish("page_not_ready")
            self._later(ready, 25)

        self._later(ready, 25)

    def _draw(self, job) -> None:
        number = self.next
        if isinstance(job, m.Formula):
            call = f"sukimaFormula({number}, {json.dumps(job.tex)}, {'true' if job.display else 'false'})"
        else:
            call = f"sukimaTable({number}, {json.dumps(job.html)})"
        started = time.monotonic()
        self.view.page().runJavaScript(call)

        def check(result) -> None:
            if not isinstance(result, dict) or result.get("id") != number:
                if (time.monotonic() - started) * 1000 > m.FORMULA_LIMIT_MS:
                    self.counts["timed_out"] += 1  # not kept: tried again next time
                    return self._done_with(job)
                return self._later(lambda: self.view.page().runJavaScript("window.__sukima", check), self.POLL_MS)
            self._grab(job, number, result, started)

        self._later(lambda: self.view.page().runJavaScript("window.__sukima", check), self.POLL_MS)

    GRAB_TRIES = 40

    def _grab(self, job, number: int, result: dict, started: float, tries: int = 0) -> None:
        """Keeps the job once a grab shows its mark; Qt's picture can lag the page by a frame
        or two (seen in tests: a blank grab, or one drawn before MathJax's fonts had loaded)."""
        try:
            image = None if result.get("error") else self._grab_to(result["box"])
            if image is not None and not self._shows(image, number):
                if tries < self.GRAB_TRIES and (time.monotonic() - started) * 1000 < m.FORMULA_LIMIT_MS:
                    return self._later(lambda: self._grab(job, number, result, started, tries + 1), 15)
                self.counts["timed_out"] += 1  # not kept: tried again next time
                return self._done_with(job)
            self._keep(job, result, image)
        except Exception as e:
            self.log("render_job_error", kind=type(e).__name__)
        self._done_with(job)

    def _grab_to(self, box):
        """The view from its corner (where the mark is) to the box's far edge, plus padding."""
        from PyQt6.QtCore import QRect

        x, y, w, h = box
        right = int((x + w + self.PAD) * m.SCALE) + 2
        bottom = int((y + h + self.PAD) * m.SCALE) + 2
        return self.view.grab(QRect(0, 0, max(right, 8), max(bottom, 8))).toImage()

    @staticmethod
    def _shows(image, number: int) -> bool:
        ratio = image.devicePixelRatio() or 1
        colour = image.pixelColor(int(1 * ratio), int(1 * ratio))
        return (colour.red(), colour.green(), colour.blue()) == (number % 251, (number // 251) % 251, 200)

    def _keep(self, job, result: dict, image) -> None:
        if result.get("error"):
            self.cache.put_failed(job)
            self.counts["failed"] += 1
            return
        x, y, w, h = result["box"]
        # One point more on each side of a formula, so an italic letter's overhang isn't cut;
        # a table's plate is cut exactly.
        pad = 0 if result.get("plate") else self.PAD
        x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
        if w <= 2 * pad or h <= 2 * pad:
            self.cache.put_failed(job)
            self.counts["failed"] += 1
            return
        # The box at 2 pixels per point, grown to whole pixels so no edge is cut. A grab on a
        # Retina screen may come at twice that (untested here): cut there, then scaled back.
        from PyQt6.QtCore import Qt

        left, top = int(x * m.SCALE), int(y * m.SCALE)
        right, bottom = -int(-(x + w) * m.SCALE // 1), -int(-(y + h) * m.SCALE // 1)
        ratio = image.devicePixelRatio() or 1
        image = image.copy(int(left * ratio), int(top * ratio), int((right - left) * ratio), int((bottom - top) * ratio))
        if ratio != 1:
            image = image.scaled(right - left, bottom - top, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            image.setDevicePixelRatio(1)
        if isinstance(job, m.Formula):
            made = mask_png(image)
            if made is None:  # nothing drawn
                self.cache.put_failed(job)
                self.counts["failed"] += 1
                return
            png, size, cut = made
            depth = 0.0 if job.display else float(result["depth"]) + self.PAD - cut / m.SCALE
            self.cache.put(job, png, size, depth=depth, ex=float(result["ex"]))
        else:
            self.cache.put(job, _png(image), (image.width(), image.height()))
        self.counts["drawn"] += 1

    def _done_with(self, job) -> None:
        self.next += 1
        self._later(self._step, 0)

    def _finish(self, how: str) -> None:
        """Ends the pass whatever happens here, so the cycle never waits on a pass for good."""
        self.busy = False
        for step in (
            lambda: self.view is not None and self.view.hide(),  # not one of Anki's open windows (DEC-156)
            lambda: self.log("render_pass", result=how, **self.counts),
            lambda: self.on_done and self.on_done(),
        ):
            try:
                step()
            except Exception:
                pass

    @staticmethod
    def _later(fn: Callable[[], None], ms: int) -> None:
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(ms, fn)
