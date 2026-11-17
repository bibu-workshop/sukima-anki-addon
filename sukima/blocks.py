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
The export later replaces each image's file name with the prepared file's hash
(`images.py`). `front` and `back` keep the side's text alone.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

from . import text as t

SUPERSCRIPT, SUBSCRIPT = "superscript", "subscript"
IMAGE_MISSING = "image_missing"

# Image types the add-on can prepare (spec, "Formats").
_IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "webp", "svg"}
# Fields of the Image Occlusion Enhanced add-on's note types, which carry no stock kind.
_IO_ENHANCED_FIELDS = {"Question Mask", "Answer Mask", "Original Mask"}
# A file name used as alt text says nothing (Anki's pasted images: "paste-3b5db7….jpg").
_FILE_NAME = re.compile(r"^[\w.\- ]+\.(jpe?g|png|gif|webp|svg|bmp|tiff?)$", re.I)
_NUMBERED = {"1", "a", "A", "i", "I"}


class _BlockConverter(t._Converter):
    """The format 1 converter, taught images, lists, superscript, subscript and links."""

    def __init__(self, media_dir: str) -> None:
        super().__init__()
        self.media_dir = media_dir
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

    def side(self) -> list[dict]:
        self._flush()
        self._end_item()
        return self.blocks


def convert_blocks(html: str, media_dir: str) -> tuple[list[dict], list[dict] | None, set[str]]:
    """A rendered side as format 1 runs (its text alone), format 2 blocks (None when the side
    needs nothing format 2 adds), and every reason it is not watch-ready."""
    parser = _BlockConverter(media_dir)
    reasons = parser.reasons
    if t._MATHS.search(html):
        reasons.add(t.MATHS)
    if t._TYPE_IN.search(html):
        reasons.add(t.TYPE_IN)
    if t._SOUND.search(html):
        reasons.add(t.AUDIO)
    parser.feed(html)
    parser.close()
    blocks = parser.side()
    runs = _text_alone(blocks)
    length = sum(len(r["text"]) for r in runs)
    if length > t.MAX_SIDE_CHARS:
        reasons.add(t.TOO_LONG)
    has_image = any("image" in b for b in blocks)
    if not has_image and not t._ZERO_WIDTH.sub("", "".join(r["text"] for r in runs)).strip():
        reasons.add(t.EMPTY_SIDE)
    return runs, (blocks if parser.uses_format_2 else None), reasons


def _text_alone(blocks: list[dict]) -> list[dict]:
    """The side's text in order, list items on their own lines with their markers."""
    runs: list[dict] = []
    for b in blocks:
        if "image" in b:
            continue
        piece = b.get("text") or ([{"text": b["marker"] + " ", "styles": []}] + b["list_item"])
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


def card_sides_v2(card, media_dir: str):
    """A card's front and back for format 2: (front runs, back runs, front blocks or None,
    back blocks or None, the reason to skip it or None)."""
    reasons: set[str] = set()
    try:
        note_type = card.note_type()
        rendered = card.render_output()
        question, answer = rendered.question_text, rendered.answer_text
        av_tags = list(rendered.question_av_tags) + list(rendered.answer_av_tags)
    except Exception:
        return [], [], None, None, t.RENDER_ERROR
    field_names = {f.get("name") for f in note_type.get("flds", [])}
    if note_type.get("originalStockKind") == t._STOCK_KIND_IMAGE_OCCLUSION or _IO_ENHANCED_FIELDS <= field_names:
        reasons.add(t.IMAGE_OCCLUSION)
    if t.hides_content(note_type.get("css", "")):
        reasons.add(t.HIDDEN_CONTENT)
    for tag in av_tags:
        is_video = isinstance(tag, t.SoundOrVideoTag) and t._VIDEO_FILE.search(tag.filename)
        reasons.add(t.VIDEO if is_video else t.AUDIO)
    front, front_blocks, front_reasons = convert_blocks(question, media_dir)
    back, back_blocks, back_reasons = convert_blocks(t.back_only(answer), media_dir)
    # What comes before the answer divider can still hide or script what follows.
    whole_answer_reasons = convert_blocks(answer, media_dir)[2] - {t.TOO_LONG, t.EMPTY_SIDE}
    reasons |= front_reasons | back_reasons | whole_answer_reasons
    reason = next((r for r in REASONS if r in reasons), None)
    return front, back, front_blocks, back_blocks, reason


# Format 2's reasons, in the order they are reported: format 1's, with a missing image
# file just after the image reasons.
REASONS = t.REASONS[: t.REASONS.index(t.IMAGE) + 1] + [IMAGE_MISSING] + t.REASONS[t.REASONS.index(t.IMAGE) + 1 :]
