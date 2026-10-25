# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turning a rendered card into the text the watch shows.

Works from an allow-list: text, line breaks, bold, italics, underline, cloze
and furigana. A card with anything else is not watch-ready, and the reason is
reported so the card can be skipped.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from anki.sound import SoundOrVideoTag

MAX_SIDE_CHARS = 2000

BOLD, ITALIC, UNDERLINE, CLOZE = "bold", "italic", "underline", "cloze"

_STYLE_BY_TAG = {"b": BOLD, "strong": BOLD, "i": ITALIC, "em": ITALIC, "u": UNDERLINE}
_BLOCKS = {"div", "p", "hr"}
_NEUTRAL = {"span", "ruby", "rb"}

# Skip reasons. When a card has several, the first in this order is reported.
IMAGE_OCCLUSION = "image_occlusion"
TYPE_IN = "type_in"
SCRIPT = "script"
VIDEO = "video"
AUDIO = "audio"
IMAGE = "image"
MATHS = "maths"
TABLE = "table"
HIDDEN_CONTENT = "hidden_content"
OTHER_MARKUP = "other_markup"
TOO_LONG = "too_long"
EMPTY_SIDE = "empty_side"
RENDER_ERROR = "render_error"
REASONS = [
    IMAGE_OCCLUSION, TYPE_IN, SCRIPT, VIDEO, AUDIO, IMAGE, MATHS, TABLE,
    HIDDEN_CONTENT, OTHER_MARKUP, TOO_LONG, EMPTY_SIDE, RENDER_ERROR,
]

_REASON_BY_TAG = {
    "img": IMAGE, "svg": IMAGE, "canvas": IMAGE, "object": IMAGE, "picture": IMAGE,
    "embed": IMAGE,
    "table": TABLE, "tr": TABLE, "td": TABLE, "th": TABLE, "tbody": TABLE,
    "thead": TABLE, "tfoot": TABLE,
    "video": VIDEO,
    "audio": AUDIO,
    "script": SCRIPT, "iframe": SCRIPT,
    "input": TYPE_IN, "textarea": TYPE_IN,
    "anki-mathjax": MATHS, "math": MATHS,
    # A style element can hide anything on the card.
    "style": HIDDEN_CONTENT,
}

# Styling known to hide content. Used on note type styling, where an allow-list
# is not possible without laying the card out; it is a list of known ways and
# cannot be complete.
_HIDES = re.compile(
    r"""(?<![\w-])(?: display\s*:\s*none
      | visibility\s*:\s*(hidden|collapse)
      | opacity\s*:\s*0*\.?0+\s*(%|;|\}|!|$)
      | font-size\s*:\s*0*\.?0+\s*([a-z%]+)?\s*(;|\}|!|$)
      | (-webkit-text-fill-)?color\s*:\s*(transparent
            | (rgb|hsl)a?\([^)]*[,/]\s*0*\.?0+\s*%?\s*\)
            | \#[0-9a-f]{3}0\b | \#[0-9a-f]{6}00\b)
      | (max-)?(height|width)\s*:\s*0+\s*([a-z%]+)?\s*(;|\}|!|$)
      | text-indent\s*:\s*-\d{3,}
      | (left|right|top|bottom|margin(-[a-z]+)?)\s*:\s*-\d{3,}
      | clip(-path)?\s*:
      | transform\s*:[^;}]*scale[xy]?\(\s*0*\.?0+\s*[,)]
      | filter\s*:[^;}]*opacity\(\s*0*\.?0+\s*%?\s*\)
      )
      # Styling pulled in from another file cannot be read here.
      | @import
    """,
    re.I | re.X,
)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# Inline styling the watch can ignore safely. Any other property makes the card
# unsupported, because it may change what the user can see.
_SAFE_PROPERTIES = {
    "color", "background", "background-color", "font-family", "font-size", "font-style",
    "font-weight", "line-height", "text-align", "text-decoration", "text-decoration-line",
}
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_MATHS = re.compile(r"\\\(|\\\[|\[latex\]|\[\$\$?\]", re.I)
_TYPE_IN = re.compile(r"\[\[type:", re.I)
_SOUND = re.compile(r"\[sound:|\[anki:(play|tts)", re.I)
_ANSWER_DIVIDER = re.compile(r"<hr\b[^>]*\bid\s*=\s*[\"']?answer[\"']?[^>]*>", re.I)
_VIDEO_FILE = re.compile(r"\.(mp4|m4v|mov|webm|mkv|avi|ogv|flv|mpe?g|wmv|3gp)$", re.I)

# Anki's note type kind for image occlusion.
_STOCK_KIND_IMAGE_OCCLUSION = 6


def hides_content(css: str) -> bool:
    """Whether styling, from a note type or a style attribute, hides content."""
    return bool(_HIDES.search(_CSS_COMMENT.sub("", css or "")))


def _inline_style_reasons(style: str) -> set[str]:
    reasons = set()
    style = _CSS_COMMENT.sub("", style)
    if hides_content(style):
        reasons.add(HIDDEN_CONTENT)
    for declaration in style.split(";"):
        name = declaration.split(":")[0].strip().lower()
        if name and name not in _SAFE_PROPERTIES:
            reasons.add(OTHER_MARKUP)
    return reasons


class _Converter(HTMLParser):
    """Collects (character, styles) pairs and the reasons a side is not watch-ready."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chars: list[tuple[str, frozenset]] = []
        self.reasons: set[str] = set()
        self.styles: list[str] = []
        self.open: list[tuple[str, str | None]] = []
        self.dropping = 0  # inside <rp>, whose brackets we replace with our own
        self.raw = 0  # inside script or style, whose text is not card text

    def _emit(self, text: str) -> None:
        styles = frozenset(self.styles)
        self.chars.extend((ch, styles) for ch in text)

    def _break(self) -> None:
        if self.chars and self.chars[-1][0] != "\n":
            self._emit("\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "hidden" in a:
            self.reasons.add(HIDDEN_CONTENT)
        self.reasons |= _inline_style_reasons(a.get("style") or "")
        if any(name.lower().startswith("on") for name in a):
            self.reasons.add(SCRIPT)
        if "image-occlusion" in f"{a.get('id') or ''} {a.get('class') or ''}".lower():
            self.reasons.add(IMAGE_OCCLUSION)
        if tag in _REASON_BY_TAG:
            self.reasons.add(_REASON_BY_TAG[tag])
            if tag in ("script", "style"):
                self.raw += 1
            return
        style = _STYLE_BY_TAG.get(tag)
        if tag == "br":
            self._emit("\n")
            return
        if tag in ("rt", "rp") and self.open and self.open[-1][0] in ("rt", "rp"):
            self.handle_endtag(self.open[-1][0])  # their end tags may be left out
        if tag in _BLOCKS:
            self._break()
        elif tag == "rt":
            self._emit("[")
        elif tag == "rp":
            self.dropping += 1
        elif tag == "span" and "cloze" in (a.get("class") or "").split():
            style = CLOZE
        elif style is None and tag not in _NEUTRAL:
            self.reasons.add(OTHER_MARKUP)
            return
        if tag != "hr":
            self.open.append((tag, style))
            if style:
                self.styles.append(style)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.raw = max(0, self.raw - 1)
        # Close back to the matching open tag; a stray end tag is ignored.
        if not any(t == tag for t, _ in self.open):
            return
        while self.open:
            t, style = self.open.pop()
            if style:
                self.styles.remove(style)
            if t == "rt":
                self._emit("]")
            elif t == "rp":
                self.dropping -= 1
            elif t in _BLOCKS:
                self._break()
            if t == tag:
                break

    def handle_data(self, data):
        if self.raw or self.dropping:
            return
        self._emit(re.sub(r"\s+", " ", data.replace("\xa0", " ")))

    def runs(self) -> list[dict]:
        """The text as runs of equally marked characters, with whitespace tidied."""
        chars: list[tuple[str, frozenset]] = []
        for ch, styles in self.chars:
            last = chars[-1][0] if chars else "\n"
            if ch == " " and last in " \n":
                continue  # no leading or doubled spaces
            if ch == "\n":
                while chars and chars[-1][0] == " ":
                    chars.pop()
                if len(chars) >= 2 and chars[-1][0] == chars[-2][0] == "\n":
                    continue  # at most one blank line
            chars.append((ch, styles))
        while chars and chars[-1][0] in " \n":
            chars.pop()
        while chars and chars[0][0] == "\n":
            chars.pop(0)
        runs: list[dict] = []
        current = None
        for ch, styles in chars:
            if current is not None and current[1] == styles:
                current[0].append(ch)
            else:
                current = ([ch], styles)
                runs.append(current)
        return [{"text": "".join(t), "styles": sorted(s)} for t, s in runs]


def convert(html: str) -> tuple[list[dict], set[str]]:
    """A rendered side as runs, with every reason it is not watch-ready."""
    parser = _Converter()
    reasons = parser.reasons
    if _MATHS.search(html):
        reasons.add(MATHS)
    if _TYPE_IN.search(html):
        reasons.add(TYPE_IN)
    if _SOUND.search(html):
        reasons.add(AUDIO)
    parser.feed(html)
    parser.close()
    runs = parser.runs()
    length = sum(len(r["text"]) for r in runs)
    if length > MAX_SIDE_CHARS:
        reasons.add(TOO_LONG)
    if not _ZERO_WIDTH.sub("", "".join(r["text"] for r in runs)).strip():
        reasons.add(EMPTY_SIDE)
    return runs, reasons


def back_only(answer_html: str) -> str:
    """What follows Anki's answer divider, or the whole side if there is none."""
    return _ANSWER_DIVIDER.split(answer_html, maxsplit=1)[-1]


def card_sides(card) -> tuple[list[dict], list[dict], str | None]:
    """A card's front and back as runs, and the reason to skip it, if any."""
    reasons: set[str] = set()
    try:
        note_type = card.note_type()
        rendered = card.render_output()
        question, answer = rendered.question_text, rendered.answer_text
        av_tags = list(rendered.question_av_tags) + list(rendered.answer_av_tags)
    except Exception:
        return [], [], RENDER_ERROR
    if note_type.get("originalStockKind") == _STOCK_KIND_IMAGE_OCCLUSION:
        reasons.add(IMAGE_OCCLUSION)
    if hides_content(note_type.get("css", "")):
        reasons.add(HIDDEN_CONTENT)
    for tag in av_tags:
        is_video = isinstance(tag, SoundOrVideoTag) and _VIDEO_FILE.search(tag.filename)
        reasons.add(VIDEO if is_video else AUDIO)
    front, front_reasons = convert(question)
    back, back_reasons = convert(back_only(answer))
    # What comes before the answer divider can still hide or script what follows.
    whole_answer_reasons = convert(answer)[1] - {TOO_LONG, EMPTY_SIDE}
    reasons |= front_reasons | back_reasons | whole_answer_reasons
    reason = next((r for r in REASONS if r in reasons), None)
    return front, back, reason
