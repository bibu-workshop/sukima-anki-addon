# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card sides as blocks, for batch format 2 (image cards spec, ticket 03).

Format 2 lets a side carry images, lists, superscript, subscript and link text
(DEC-175, DEC-187). It is written only when the helper says it can pass format
2 on (`batch_formats` in the cycle request); otherwise the add-on converts
exactly as in format 1 (`text.convert`), where all of these skip the card.

A side becomes a list of blocks in the card's own order:
  {"text": [runs]}
  {"image": <media file name>, "beside_previous": bool, "alt": text or None}
  {"list_item": [runs], "level": 1 or 2, "marker": "•", "◦" or "3."}
  {"sound": <media file name>, "beside_previous": bool}   (format 3 only)
  {"code": text, exactly as written}                       (format 4 only)
  {"maths": <mask hash>, "tex": TeX}                         (format 5 only: display maths)
Format 5 also puts inline maths in a text block as a run: {"text": TeX, "styles": ["maths"],
"maths": <mask hash>}; and a table drawn as a picture is an image block (`maths.py`).
  {"hint": [blocks], "label": text}                          (format 6 only)
A hint is Anki's own `{{hint:Field}}`: its link text (the field's name, "Hint" if empty) and
what it hides, as blocks of their own. Any other scripted button still keeps the card in Anki.
The export later replaces each image's file name with the prepared file's hash
(`images.py`) and each sound's with its file's hash (`sounds.py`). `front` and `back`
keep the side's text alone.

Format 3 (sound cards spec) is written only when the helper passes it on. Anki's
rendering puts a marker, `[anki:play:q:N]` or `[anki:play:a:N]`, where each sound tag
was, numbered into that side's list of tags; each becomes a sound block in its place.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from urllib.parse import unquote

from . import maths as m
from . import text as t

SUPERSCRIPT, SUBSCRIPT = "superscript", "subscript"
IMAGE_MISSING = "image_missing"
SOUND_MISSING = "sound_missing"

# Sound files the watch plays (sound cards spec, "Kinds that stay in Anki"); copied unchanged.
SOUND_TYPES = {"mp3", "m4a", "aac", "wav", "caf", "aiff"}
# A file larger than this is not a card's word or sentence (spec, "Size"): it stays in Anki.
MAX_SOUND_BYTES = 1_000_000
# Anki's marker where a sound tag was, and the element it becomes so the parser sees it in place.
_PLAY_MARKER = re.compile(r"\[anki:play:([qa]):(\d+)\]")
_SOUND_ELEMENT = "sukima-sound"

# Format 4 (card content spec, code blocks): a code block becomes this element before parsing,
# its text kept apart with every space and line break; inline <code> becomes the run style "code".
CODE = "code"
_CODE_ELEMENT = "sukima-code"
_CODE_BLOCK = re.compile(
    r"<div\b[^>]*\bclass=\"[^\"]*\bhighlight\b[^\"]*\"[^>]*>\s*<pre\b.*?</pre>\s*</div>"  # the highlighter's wrapper
    r"|<table\b[^>]*\bclass=\"[^\"]*\bhighlighttable\b[^\"]*\"[^>]*>.*?</table>"  # with line numbers
    r"|<pre\b.*?</pre>",
    re.S | re.I,
)
_LINE_NUMBERS = re.compile(r"<td\b[^>]*\bclass=\"[^\"]*\blinenos\b[^\"]*\"[^>]*>.*?</td>", re.S | re.I)
_SCRIPT_ELEMENT = re.compile(r"<script\b.*?</script>", re.S | re.I)

# Format 6 (card content spec, hints): exactly the link and hidden div Anki writes for
# `{{hint:Field}}` (Anki 26.09; older versions also set `window.location.hash`).
HINT = "hint"
_HINT_ELEMENT = "sukima-hint"
_HINT_LINK = re.compile(
    r"""<a\s+class=["']?hint["']?\s+href="\#"\s+onclick="this\.style\.display='none';\s*"""
    r"""document\.getElementById\('(?P<id>hint[0-9a-f]+)'\)\.style\.display='block';\s*"""
    r"""(?:window\.location\.hash='(?P=id)';\s*)?return\s+false;"\s*(?:draggable=false\s*)?>"""
    r"""(?P<label>[^<]*)</a>\s*""",
    re.I,
)
_DIV_TAG = re.compile(r"<(/?)div\b[^>]*>", re.I)


def _take_hints(html: str) -> tuple[str, list[tuple[str, str]]]:
    """The side with each of Anki's hints replaced by a placeholder, and each hint's link text
    and hidden HTML. A link not followed by its own hidden div is left as it is (a script)."""
    from html import unescape

    hints: list[tuple[str, str]] = []
    out, at = [], 0
    for m in _HINT_LINK.finditer(html):
        if m.start() < at:
            continue
        opening = re.compile(
            rf"""<div\s+id=["']?{m.group('id')}["']?\s+class=["']?hint["']?\s+style="display:\s*none;?">""", re.I
        ).match(html, m.end())
        if not opening:
            continue
        depth, end = 1, None
        for tag in _DIV_TAG.finditer(html, opening.end()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = tag
                break
        if end is None:
            continue
        label = unescape(m.group("label")).replace("\xa0", " ").strip() or "Hint"
        hints.append((label, html[opening.end():end.start()]))
        out.append(html[at:m.start()])
        out.append(f'<{_HINT_ELEMENT} data-index="{len(hints) - 1}"></{_HINT_ELEMENT}>')
        at = end.end()
    out.append(html[at:])
    return "".join(out), hints


def code_text(block_html: str) -> str:
    """A code block's text as written: line numbers dropped, tags and colours gone, every space,
    tab and line break kept; a final line break dropped."""
    from html import unescape

    inner = _LINE_NUMBERS.sub("", block_html)
    inner = re.sub(r"<br\s*/?>", "\n", inner, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", "", inner)).replace("\xa0", " ").rstrip("\n")


def _take_code(html: str) -> tuple[str, list[str]]:
    """The side with each code block replaced by a placeholder, and the blocks' texts."""
    texts: list[str] = []

    def keep(m: re.Match) -> str:
        texts.append(code_text(m.group(0)))
        return f'<{_CODE_ELEMENT} data-index="{len(texts) - 1}"></{_CODE_ELEMENT}>'

    return _CODE_BLOCK.sub(keep, html), texts

# Image types the add-on can prepare (spec, "Formats").
_IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "webp", "svg"}
# Fields of the Image Occlusion Enhanced add-on's note types, which carry no stock kind.
_IO_ENHANCED_FIELDS = {"Question Mask", "Answer Mask", "Original Mask"}
# A file name used as alt text says nothing (Anki's pasted images: "paste-3b5db7….jpg").
_FILE_NAME = re.compile(r"^[\w.\- ]+\.(jpe?g|png|gif|webp|svg|bmp|tiff?)$", re.I)
_NUMBERED = {"1", "a", "A", "i", "I"}


class _BlockConverter(t._Converter):
    """The format 1 converter, taught images, lists, superscript, subscript and links."""

    def __init__(
        self, media_dir: str, av_tags: dict | None = None, code: list[str] | None = None,
        maths: "Maths | None" = None, formulas: list | None = None, tables: list | None = None,
        hints: list | None = None,
    ) -> None:
        super().__init__()
        self.media_dir = media_dir
        # With format 6: the side's hints (link text, hidden HTML), by placeholder index.
        self.hints = hints
        # With format 5: the lookup for drawn formulas and tables, and the side's own, by index.
        self.maths = maths
        self.formulas = formulas or []
        self.tables = tables or []
        self.inline_maths: dict[str, tuple[str, str]] = {}  # placeholder style -> (TeX, mask hash)
        # With format 4: the side's code blocks' texts, by placeholder index; None writes no code.
        self.code = code
        # With format 3: Anki's sound tags of each side, by "q" and "a"; None writes no sounds.
        self.av_tags = av_tags
        self.blocks: list[dict] = []
        self.lists: list[dict] = []  # open <ul>/<ol>: {"ordered": bool, "count": int}
        self.item: dict | None = None  # the open list item's block
        self.since_image = False  # anything but spaces since the last image
        self.broke = False  # a line break or a block since the last image
        self.uses_format_2 = False

    # Text collected so far becomes a block; spaces alone make none.
    def _flush(self) -> None:
        runs = self.runs()
        self.chars = []
        if not runs:
            return
        if self.item is not None:
            self.item["list_item"].extend(runs)
        else:
            self.blocks.append({"text": runs})

    def _emit(self, text: str) -> None:
        if text.strip():
            self.since_image = True
        super()._emit(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        # The checks format 1 makes on every tag, for the tags handled here.
        if "hidden" in a:
            self.reasons.add(t.HIDDEN_CONTENT)
        self.reasons |= t._inline_style_reasons(a.get("style") or "")
        if any(name.lower().startswith("on") for name in a):
            self.reasons.add(t.SCRIPT)
        if tag == "br" or tag in t._BLOCKS or tag in ("ul", "ol", "li"):
            self.broke = True
        cls_id = f"{a.get('id') or ''} {a.get('class') or ''}".lower().split()
        if any(word.startswith("io-") for word in cls_id):
            self.reasons.add(t.IMAGE_OCCLUSION)
        if tag == "img":
            self._image(a)
            return
        if tag == _SOUND_ELEMENT:
            self._sound(a.get("data-side") or "", int(a.get("data-index") or 0))
            return
        if tag == _CODE_ELEMENT and self.code is not None:
            index = int(a.get("data-index") or 0)
            self._flush()
            self.blocks.append({"code": self.code[index] if index < len(self.code) else ""})
            self.broke = True
            self.uses_format_2 = True
            return
        if tag == _HINT_ELEMENT and self.hints is not None:
            self._hint(int(a.get("data-index") or 0))
            return
        if tag == m.MATHS_ELEMENT and self.maths is not None:
            self._formula(int(a.get("data-index") or 0))
            return
        if tag == m.TABLE_ELEMENT and self.maths is not None:
            self._table(int(a.get("data-index") or 0))
            return
        if tag == "code" and self.code is not None:
            self.uses_format_2 = True
            self.open.append((tag, CODE))
            self.styles.append(CODE)
            return
        if tag in ("sup", "sub"):
            self.uses_format_2 = True
            style = SUPERSCRIPT if tag == "sup" else SUBSCRIPT
            self.open.append((tag, style))
            self.styles.append(style)
            return
        if tag == "a":
            self.uses_format_2 = True
            self.open.append((tag, None))
            return
        if tag in ("ul", "ol"):
            self._flush()
            if len(self.lists) == 2:
                self.reasons.add(t.OTHER_MARKUP)  # the guide sets two levels
            ordered = tag == "ol" and (a.get("type") or "1") in _NUMBERED
            start = int(a["start"]) - 1 if (a.get("start") or "").isdigit() else 0
            self.lists.append({"ordered": ordered, "count": start})
            self.uses_format_2 = True
            self.open.append((tag, None))
            return
        if tag == "li":
            self._flush()
            if self.item is not None:
                self._end_item()
            level = max(1, min(len(self.lists), 2))
            if self.lists:
                current = self.lists[-1]
                current["count"] += 1
                marker = f"{current['count']}." if current["ordered"] else ("•" if level == 1 else "◦")
            else:
                marker = "•"
            self.item = {"list_item": [], "level": level, "marker": marker}
            self.uses_format_2 = True
            self.open.append((tag, None))
            return
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in t._BLOCKS:
            self.broke = True
        if tag == "li" and any(o == "li" for o, _ in self.open):
            self._flush()
            self._end_item()
        if tag in ("ul", "ol") and any(o == tag for o, _ in self.open):
            self._flush()
            if self.item is not None:
                self._end_item()
            if self.lists:
                self.lists.pop()
        super().handle_endtag(tag)

    def _end_item(self) -> None:
        if self.item is not None:
            self.blocks.append(self.item)
            self.item = None

    def _image(self, a: dict) -> None:
        src = unquote(a.get("src") or "").strip()
        ext = src.rsplit(".", 1)[-1].lower() if "." in src else ""
        if not src or "/" in src or ":" in src or ext not in _IMAGE_TYPES:
            self.reasons.add(t.IMAGE)  # a remote, embedded or unknown kind of picture
            return
        if not os.path.isfile(os.path.join(self.media_dir, src)):
            self.reasons.add(IMAGE_MISSING)
            return
        beside = bool(self.blocks) and "image" in self.blocks[-1] and not self.since_image and not self.broke
        self._flush()
        alt = (a.get("alt") or "").strip()
        self.blocks.append({
            "image": src,
            "beside_previous": beside,
            "alt": alt if alt and not _FILE_NAME.match(alt) else None,
        })
        self.since_image = False
        self.broke = False
        self.uses_format_2 = True

    def _sound(self, side: str, index: int) -> None:
        """A sound block where Anki had the tag, or the reason the card stays in Anki."""
        tags = (self.av_tags or {}).get(side) or []
        tag = tags[index] if index < len(tags) else None
        if tag is None or not isinstance(tag, t.SoundOrVideoTag):
            self.reasons.add(t.AUDIO)  # text to speech ({{tts}}) is not designed
            return
        name = tag.filename
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if t._VIDEO_FILE.search(name):
            self.reasons.add(t.VIDEO)
            return
        if "/" in name or ext not in SOUND_TYPES:
            self.reasons.add(t.AUDIO)  # a kind the watch doesn't play
            return
        path = os.path.join(self.media_dir, name)
        if not os.path.isfile(path):
            self.reasons.add(SOUND_MISSING)
            return
        if os.path.getsize(path) > MAX_SOUND_BYTES:
            self.reasons.add(t.AUDIO)
            return
        beside = bool(self.blocks) and "sound" in self.blocks[-1] and not self.since_image and not self.broke
        self._flush()
        self.blocks.append({"sound": name, "beside_previous": beside})
        self.since_image = False
        self.broke = False
        self.uses_format_2 = True

    def _hint(self, index: int) -> None:
        """One of Anki's hints, in its place: its link text and what it hides, converted as a
        side of its own (text, images, lists, code, maths). Whatever keeps that content off the
        watch keeps the card in Anki."""
        if index >= len(self.hints):
            return
        label, inner = self.hints[index]
        runs, blocks, reasons = convert_blocks(
            inner, self.media_dir, self.av_tags, self.code is not None, self.maths, hints=True)
        self.reasons |= reasons - {t.EMPTY_SIDE, t.TOO_LONG}
        self._flush()
        self.blocks.append({"hint": blocks if blocks is not None else ([{"text": runs}] if runs else []), "label": label})
        self.broke = True
        self.uses_format_2 = True

    def _formula(self, index: int) -> None:
        """Display maths a block of its own; inline maths a run in its sentence. A formula not
        drawn keeps the card in Anki as `maths`."""
        if index >= len(self.formulas) or not self.formulas[index].tex:
            return  # an empty pair draws nothing in Anki either
        formula = replace(self.formulas[index], macros=self.maths.macros)
        drawn = self.maths.lookup(formula)
        if drawn is None:
            self.reasons.add(t.MATHS)
            return
        self.uses_format_2 = True
        if formula.display and self.item is None:
            self._flush()
            self.blocks.append({"maths": drawn.hash, "tex": formula.tex, "_drawn": drawn})
            self.broke = True
            return
        # A placeholder style keeps the formula one run, apart from any formula beside it.
        style = f"\x00maths{len(self.inline_maths)}"
        self.inline_maths[style] = (formula.tex, drawn)
        self.styles.append(style)
        self._emit(formula.tex)
        self.styles.remove(style)

    def _table(self, index: int) -> None:
        """A table drawn as a picture, as an image block. One not drawn keeps the card in Anki."""
        drawn = self.maths.lookup(self.tables[index]) if index < len(self.tables) else None
        if drawn is None:
            self.reasons.add(t.TABLE)
            return
        self._flush()
        self.blocks.append({"image": drawn.hash, "beside_previous": False, "alt": None, "_drawn": drawn})
        self.since_image = False
        self.broke = True
        self.uses_format_2 = True

    def runs(self) -> list[dict]:
        """Format 1's runs, with each inline formula as its own run holding its TeX and mask."""
        out = []
        for run in super().runs():
            placeholder = next((s for s in run["styles"] if s in self.inline_maths), None)
            if placeholder is None:
                out.append(run)
                continue
            tex, drawn = self.inline_maths[placeholder]
            out.append({"text": tex, "styles": ["maths"], "maths": drawn.hash, "_drawn": drawn})
        return out

    def side(self) -> list[dict]:
        self._flush()
        self._end_item()
        return self.blocks


@dataclass(frozen=True)
class Maths:
    """Format 5: how a side's formulas and tables are looked up, and the card's macros."""

    lookup: "m.MathsLookup"
    macros: str = "{}"


def convert_blocks(
    html: str, media_dir: str, av_tags: dict | None = None, code: bool = False, maths: Maths | None = None,
    hints: bool = False,
) -> tuple[list[dict], list[dict] | None, set[str]]:
    """A rendered side as format 1 runs (its text alone), format 2 blocks (None when the side
    needs nothing format 2 adds), and every reason it is not watch-ready. With `av_tags`
    (format 3), each of Anki's sound markers becomes a sound block instead of a reason. With
    `code` (format 4), code becomes code blocks and the run style "code", `<center>` is a plain
    block, and the maths check reads only what lies outside code and scripts. With `maths`
    (format 5), formulas and tables Anki's MathJax has drawn take their place."""
    texts = None
    formulas = tables = hint_list = None
    if hints:
        html, hint_list = _take_hints(html)
    if code:
        html, texts = _take_code(html)
        html = re.sub(r"<(/?)center\b", r"<\1div", html, flags=re.I)
        # <small> only makes text smaller (Coding Flashcards' label "Rust Flashcards"): plain text.
        html = re.sub(r"<(/?)small\b", r"<\1span", html, flags=re.I)
    if maths is not None:
        html, tables = m.take_tables(html)
        html, formulas = m.take_maths(html)
    parser = _BlockConverter(media_dir, av_tags, texts, maths, formulas, tables, hint_list)
    reasons = parser.reasons
    looked_at = re.sub(r"<code\b.*?</code>", "", _SCRIPT_ELEMENT.sub("", html), flags=re.S | re.I) if code else html
    if t._MATHS.search(looked_at):
        reasons.add(t.MATHS)
    if t._TYPE_IN.search(html):
        reasons.add(t.TYPE_IN)
    if av_tags is not None:
        html = _PLAY_MARKER.sub(
            lambda m: f'<{_SOUND_ELEMENT} data-side="{m.group(1)}" data-index="{m.group(2)}"></{_SOUND_ELEMENT}>', html)
    if t._SOUND.search(html):
        reasons.add(t.AUDIO)
    parser.feed(html)
    parser.close()
    blocks = parser.side()
    runs = _text_alone(blocks)
    length = sum(len(r["text"]) for r in runs) + _hidden_length(blocks)
    if length > t.MAX_SIDE_CHARS:
        reasons.add(t.TOO_LONG)
    has_image = any("image" in b or "sound" in b or "maths" in b or "hint" in b or b.get("code", "").strip() for b in blocks)
    if not has_image and not t._ZERO_WIDTH.sub("", "".join(r["text"] for r in runs)).strip():
        reasons.add(t.EMPTY_SIDE)
    return runs, (blocks if parser.uses_format_2 else None), reasons


def _hidden_length(blocks: list[dict]) -> int:
    """What counts towards the side's length besides its text alone: code, and hints' content."""
    total = 0
    for b in blocks:
        if "code" in b:
            total += len(b["code"])
        if "hint" in b:
            inner = b["hint"]
            total += len(b["label"]) + sum(len(r["text"]) for r in _text_alone(inner)) + _hidden_length(inner)
    return total


def _text_alone(blocks: list[dict]) -> list[dict]:
    """The side's text in order, list items on their own lines with their markers."""
    runs: list[dict] = []
    for b in blocks:
        if "image" in b or "sound" in b or "code" in b or "maths" in b or "hint" in b:
            continue  # a hint's text is left out: the front alone must not give it away
        piece = b.get("text") or ([{"text": b["marker"] + " ", "styles": []}] + b["list_item"])
        # Text alone: an inline formula is its TeX, unmarked.
        piece = [{"text": r["text"], "styles": [s for s in r["styles"] if s != "maths"]} for r in piece]
        if runs:
            runs.append({"text": "\n", "styles": []})
        runs.extend(piece)
    merged: list[dict] = []
    for r in runs:
        if merged and merged[-1]["styles"] == r["styles"]:
            merged[-1] = {"text": merged[-1]["text"] + r["text"], "styles": r["styles"]}
        else:
            merged.append(dict(r))
    return merged


def card_sides_v2(
    card, media_dir: str, sounds: bool = False, code: bool = False, maths: "m.MathsLookup | None" = None,
    hints: bool = False,
):
    """A card's front and back for format 2: (front runs, back runs, front blocks or None,
    back blocks or None, the reason to skip it or None). With `sounds` (format 3), Anki's
    sound tags become sound blocks; otherwise any sound keeps the card in Anki. With `maths`
    (format 5, with `code`), formulas and tables come drawn from it, and a script that only
    sets MathJax's macros is read instead of keeping the card in Anki. With `hints` (format 6),
    Anki's own `{{hint:}}` becomes a hint block instead of keeping the card in Anki as `script`."""
    reasons: set[str] = set()
    try:
        note_type = card.note_type()
        rendered = card.render_output()
        question, answer = rendered.question_text, rendered.answer_text
        av_tags = list(rendered.question_av_tags) + list(rendered.answer_av_tags)
    except Exception:
        return [], [], None, None, t.RENDER_ERROR
    side_maths = None
    if maths is not None and code:
        question, answer, macros = m.take_macro_scripts(question, answer)
        side_maths = Maths(maths, m.macros_json(macros))
    field_names = {f.get("name") for f in note_type.get("flds", [])}
    if note_type.get("originalStockKind") == t._STOCK_KIND_IMAGE_OCCLUSION or _IO_ENHANCED_FIELDS <= field_names:
        reasons.add(t.IMAGE_OCCLUSION)
    if t.note_type_hides(note_type.get("css", ""), question + answer):
        reasons.add(t.HIDDEN_CONTENT)
    tags = None
    if sounds:
        tags = {"q": list(rendered.question_av_tags), "a": list(rendered.answer_av_tags)}
    else:
        for tag in av_tags:
            is_video = isinstance(tag, t.SoundOrVideoTag) and t._VIDEO_FILE.search(tag.filename)
            reasons.add(t.VIDEO if is_video else t.AUDIO)
    front, front_blocks, front_reasons = convert_blocks(question, media_dir, tags, code, side_maths, hints)
    back, back_blocks, back_reasons = convert_blocks(t.back_only(answer), media_dir, tags, code, side_maths, hints)
    # What comes before the answer divider can still hide or script what follows.
    whole_answer_reasons = convert_blocks(answer, media_dir, tags, code, side_maths, hints)[2] - {t.TOO_LONG, t.EMPTY_SIDE}
    reasons |= front_reasons | back_reasons | whole_answer_reasons
    reason = next((r for r in REASONS if r in reasons), None)
    return front, back, front_blocks, back_blocks, reason


# Format 2's reasons, in the order they are reported: format 1's, with a missing image
# file just after the image reasons.
# Format 3 adds a missing sound file just after the audio reason.
REASONS = t.REASONS[: t.REASONS.index(t.IMAGE) + 1] + [IMAGE_MISSING] + t.REASONS[t.REASONS.index(t.IMAGE) + 1 :]
REASONS = REASONS[: REASONS.index(t.AUDIO) + 1] + [SOUND_MISSING] + REASONS[REASONS.index(t.AUDIO) + 1 :]
