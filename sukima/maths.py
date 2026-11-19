# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maths and tables for batch format 5 (card content spec, ticket 06; DEC-205; guide 1.26).

Anki's maths (`\\(...\\)` inline, `\\[...\\]` display) and real tables (not code) are drawn
to images by Anki's own MathJax in a hidden web view (`render.py`, Qt), before the export.
This module is the part that needs no Qt: finding formulas and tables in a card, reading a
script that only sets MathJax macros, the cache, and the lookup the card conversion asks.

A formula becomes a mask: black on white turned into alpha (alpha = 255 − luminance), which
the watch draws in `textPrimary`. A table becomes an ordinary picture, in its own colours.
Both are cached by what they were drawn from, as images are by their file's hash, so each
is drawn once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from html import unescape
from pathlib import Path

from .files import read_json, write_json

CACHE_VERSION = 1
# Drawn at the card's text size, 2 pixels per point (the watch's screen).
FONT_PT = 18
SCALE = 2
# Tables are laid out at the 46 mm card's width; wider ones keep their width.
TABLE_WIDTH_PT = 190
# A table is drawn on the colour of the watch's image plate, 6 pt inside it (`Seal-Maths` E).
TABLE_PLATE = "#F2F0EB"
TABLE_MARGIN_PT = 6
# A formula that doesn't draw in this long keeps its card in Anki as `maths` (spec).
FORMULA_LIMIT_MS = 5000
# The whole pass, at most; what is left waits for the next export.
PASS_LIMIT_MS = 60_000

MATHJAX_FILE = "tex-chtml-full.js"
# Where Anki keeps its MathJax: inside the running app's `aqt` package, or the installed app's
# (for tests outside Anki).
_APP_MATHJAX = Path("/Applications/Anki.app/Contents/Resources/app_packages/_aqt/data/web/js/vendor/mathjax")

_INLINE = re.compile(r"\\\((.*?)\\\)", re.S)
_DISPLAY = re.compile(r"\\\[(.*?)\\\]", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
# What a table drawn as a picture can't hold: a picture, sound, script, maths, another table,
# a form field, or hidden content (the picture would show what Anki hides).
_NOT_IN_TABLE = re.compile(
    r"<(img|svg|canvas|object|embed|picture|video|audio|script|iframe|input|textarea|style|anki-mathjax|math)\b"
    r"|\\\(|\\\[|\[latex\]|\[\$|\[sound:|\[anki:|<[^>]*\shidden[\s>=/]|display\s*:\s*none|visibility\s*:\s*hidden",
    re.I,
)

# A script that only sets MathJax's macros, as dzackgarza's maths deck has on every card:
#   MathJax.config.tex.macros = { coloneqq: ['\\mathrel{\\vcenter{:}}=', 0] };
#   MathJax.startup.getComponents();
_MACRO_SCRIPT = re.compile(
    r"^\s*MathJax\.config\.tex\.macros\s*=\s*\{(?P<body>.*)\}\s*;?\s*"
    r"(?:MathJax\.startup\.getComponents\(\)\s*;?\s*)?$",
    re.S,
)
_JS_STRING = r"""(?:'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")"""
_MACRO_ENTRY = re.compile(
    rf"\s*(?P<name>[A-Za-z]+|'[A-Za-z]+'|\"[A-Za-z]+\")\s*:\s*"
    rf"(?:(?P<plain>{_JS_STRING})|\[\s*(?P<def>{_JS_STRING})\s*(?:,\s*(?P<args>\d)\s*)?\])\s*(?:,|$)",
    re.S,
)


def _js_string(literal: str) -> str | None:
    """A JavaScript string literal's value; only the escapes a macro needs."""
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            if i + 1 >= len(body):
                return None
            nxt = body[i + 1]
            if nxt in "\\'\"":
                out.append(nxt)
            elif nxt == "n":
                out.append("\n")
            else:
                return None  # \u and the rest: not read here
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def macros_from_script(script: str) -> dict[str, list] | None:
    """The macros a script sets, when setting MathJax's macros is all it does; otherwise None
    (the card then stays in Anki as `script`)."""
    m = _MACRO_SCRIPT.match(script)
    if not m:
        return None
    body, macros, at = m.group("body"), {}, 0
    while body[at:].strip():
        entry = _MACRO_ENTRY.match(body, at)
        if not entry:
            return None
        at = entry.end()
        name = entry.group("name").strip("'\"")
        literal = entry.group("plain") or entry.group("def")
        value = _js_string(literal)
        if value is None:
            return None
        macros[name] = [value, int(entry.group("args") or 0)]
    return macros


def take_macro_scripts(question: str, answer: str) -> tuple[str, str, dict[str, list]]:
    """Both sides with every macro-only script taken out, and the macros they set together.
    A script that does anything else is left where it is."""
    macros: dict[str, list] = {}

    def take(m: re.Match) -> str:
        found = macros_from_script(m.group(1))
        if found is None:
            return m.group(0)
        macros.update(found)
        return ""

    return _SCRIPT.sub(take, question), _SCRIPT.sub(take, answer), macros


@dataclass(frozen=True)
class Formula:
    """One formula to draw: its TeX (as MathJax reads it, entities and tags undone), whether it
    is display maths, and the card's macros, as sorted JSON."""

    tex: str
    display: bool
    macros: str = "{}"

    def key(self, mathjax_id: str) -> str:
        return _key(["maths", mathjax_id, self.display, self.macros, self.tex])


@dataclass(frozen=True)
class Table:
    """One table to draw, as the card's HTML has it."""

    html: str

    def key(self, mathjax_id: str) -> str:
        return _key(["table", mathjax_id, TABLE_WIDTH_PT, self.html])


def _key(parts: list) -> str:
    return hashlib.sha256(json.dumps([CACHE_VERSION, *parts], ensure_ascii=False).encode()).hexdigest()


def tex_of(inner_html: str) -> str:
    """A formula's TeX from the card's HTML between its delimiters: what MathJax reads from the
    page's text (tags dropped, a line break a space, entities undone)."""
    text = re.sub(r"<br\s*/?>", " ", inner_html, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()


def macros_json(macros: dict[str, list]) -> str:
    return json.dumps(macros, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class Drawn:
    """A drawn formula or table, as the cache keeps it. Sizes in points are the file's pixels
    over 2; `depth` (formulas) is how far the formula reaches below the text's baseline and
    `ex` the height of an x in the formula's font, in points, so the watch can match its own."""

    hash: str  # SHA-256 of the PNG
    file_width: int
    file_height: int
    bytes: int
    mask: bool
    depth: float = 0.0
    ex: float = 0.0

    @property
    def file_name(self) -> str:
        return f"{self.hash}.png"

    @property
    def width(self) -> int:
        return max(1, round(self.file_width / SCALE))

    @property
    def height(self) -> int:
        return max(1, round(self.file_height / SCALE))

    def image_entry(self) -> dict:
        """The batch's image table entry (FORMATS.md, format 5)."""
        entry = {
            "hash": self.hash, "width": self.width, "height": self.height,
            "file_width": self.file_width, "file_height": self.file_height,
            "transparent": self.mask, "bytes": self.bytes,
        }
        if self.mask:
            entry.update(mask=True, depth=round(self.depth, 2), ex=round(self.ex, 2))
        return entry


FAILED = "failed"


class MathsCache:
    """Drawn formulas and tables in the add-on's state folder, by what they were drawn from.
    A formula MathJax can't read is kept as failed, so it isn't drawn again every export."""

    def __init__(self, folder: Path, mathjax_id: str) -> None:
        self.folder = folder
        self.mathjax_id = mathjax_id

    def _meta(self, job: Formula | Table) -> Path:
        return self.folder / f"{job.key(self.mathjax_id)}.json"

    def get(self, job: Formula | Table) -> Drawn | str | None:
        """The drawn job, FAILED, or None when it hasn't been drawn."""
        try:
            meta = read_json(self._meta(job))
        except (ValueError, OSError):
            return None
        if not isinstance(meta, dict) or meta.get("cache_version") != CACHE_VERSION:
            return None
        if meta.get("failed"):
            return FAILED
        try:
            drawn = Drawn(**meta["drawn"])
        except (KeyError, TypeError):
            return None
        return drawn if (self.folder / drawn.file_name).exists() else None

    def put(self, job: Formula | Table, png: bytes, file_size: tuple[int, int], depth: float = 0.0, ex: float = 0.0) -> Drawn:
        drawn = Drawn(
            hash=hashlib.sha256(png).hexdigest(), file_width=file_size[0], file_height=file_size[1],
            bytes=len(png), mask=isinstance(job, Formula), depth=depth, ex=ex,
        )
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f".{drawn.file_name}.tmp"
        tmp.write_bytes(png)
        os.replace(tmp, self.folder / drawn.file_name)
        write_json(self._meta(job), {"cache_version": CACHE_VERSION, "drawn": asdict(drawn)})
        return drawn

    def put_failed(self, job: Formula | Table) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        write_json(self._meta(job), {"cache_version": CACHE_VERSION, "failed": True})

    def path(self, drawn: Drawn) -> Path:
        return self.folder / drawn.file_name


# Stands in for a job not drawn yet while the export only collects what to draw: the card counts
# as taken, so the walk stops where the real export will.
_PENDING = Drawn(hash="0" * 64, file_width=2, file_height=2, bytes=0, mask=True)


@dataclass
class MathsLookup:
    """What the card conversion asks for each formula and table. With `collect`, anything not
    drawn yet is noted in `wanted` and treated as drawn; otherwise it is missing, and its card
    stays in Anki (`maths` or `table`)."""

    cache: MathsCache
    collect: bool = False
    wanted: dict = field(default_factory=dict)  # key -> Formula or Table, in the order met

    def __call__(self, job: Formula | Table) -> Drawn | None:
        found = self.cache.get(job)
        if isinstance(found, Drawn):
            return found
        if found is None and self.collect:
            self.wanted.setdefault(job.key(self.cache.mathjax_id), job)
            return _PENDING
        return None


def mathjax_dir() -> Path | None:
    """Anki's own MathJax folder, or None when it can't be found (maths then stays in Anki)."""
    candidates = []
    try:
        import aqt

        candidates.append(Path(aqt.__file__).parent / "data" / "web" / "js" / "vendor" / "mathjax")
    except Exception:
        pass
    candidates.append(_APP_MATHJAX)
    return next((c for c in candidates if (c / MATHJAX_FILE).is_file()), None)


def mathjax_id(folder: Path) -> str:
    """Names the MathJax that draws: a new one in a new Anki draws everything afresh."""
    return hashlib.sha256((folder / MATHJAX_FILE).read_bytes()).hexdigest()[:16]


# -- finding them in a side ------------------------------------------------------------

MATHS_ELEMENT = "sukima-maths"
TABLE_ELEMENT = "sukima-table"


def take_maths(html: str) -> tuple[str, list[Formula]]:
    """The side with each `\\[...\\]` and `\\(...\\)` replaced by a placeholder element, and the
    formulas. Display first, so an inline pair inside a display formula stays in it."""
    formulas: list[Formula] = []

    def keep(display: bool):
        def put(m: re.Match) -> str:
            formulas.append(Formula(tex_of(m.group(1)), display))
            return f'<{MATHS_ELEMENT} data-index="{len(formulas) - 1}"></{MATHS_ELEMENT}>'
        return put

    html = _DISPLAY.sub(keep(True), html)
    html = _INLINE.sub(keep(False), html)
    return html, formulas


def take_tables(html: str) -> tuple[str, list[Table]]:
    """The side with each table that can be drawn as a picture replaced by a placeholder."""
    tables: list[Table] = []

    def keep(m: re.Match) -> str:
        whole = m.group(0)
        if "<table" in whole[6:].lower() or _NOT_IN_TABLE.search(whole):
            return whole  # stays a table: the card stays in Anki
        tables.append(Table(whole))
        return f'<{TABLE_ELEMENT} data-index="{len(tables) - 1}"></{TABLE_ELEMENT}>'

    return _TABLE.sub(keep, html), tables
