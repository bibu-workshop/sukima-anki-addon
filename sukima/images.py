# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preparing card images for the watch (image cards spec, ticket 04; DEC-180, DEC-187).

Each image Anki shows on a card is prepared once, with the Qt that ships with
Anki: an SVG rasterised, a GIF's first frame taken, scaled to the zoom size
(`max(416 / width, 496 / height)` times the original, never larger), and saved
as JPEG at quality 70, or PNG when any pixel is transparent. Results are cached
by the original file's SHA-256 in the add-on's state folder, so the next export
decodes nothing it has prepared before.

Only the collection's media folder is read; nothing is written there.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .files import read_json, write_json

CARD_SIDE = 416  # the 46 mm screen's width at 2 pixels per point
ZOOM_HEIGHT = 496  # the 46 mm screen's height at 2 pixels per point
JPEG_QUALITY = 70
CACHE_VERSION = 1

# The helper's formats, remembered from its last cycle request (FORMATS.md, cycle request).
_FORMATS_FILE = "helper-formats.json"


@dataclass(frozen=True)
class Prepared:
    hash: str  # SHA-256 of the prepared file
    ext: str  # "jpg" or "png"
    width: int  # the original's size
    height: int
    file_width: int
    file_height: int
    transparent: bool
    bytes: int

    @property
    def file_name(self) -> str:
        return f"{self.hash}.{self.ext}"


def zoom_size(width: int, height: int) -> tuple[int, int]:
    """The prepared file's size: large enough to fill the 46 mm screen at 2 pixels per point
    one way, never larger than the original."""
    scale = min(1.0, max(CARD_SIDE / width, ZOOM_HEIGHT / height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def prepare(source: Path, cache_dir: Path) -> Prepared | None:
    """The prepared image for one media file, from the cache when it is there. None when Qt
    can't read it: the card then stays in Anki with the reason `image`."""
    data = source.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    meta_path = cache_dir / f"{key}.json"
    meta = read_json(meta_path) if meta_path.exists() else None
    if isinstance(meta, dict) and meta.get("cache_version") == CACHE_VERSION:
        prepared = Prepared(**meta["prepared"])
        if (cache_dir / prepared.file_name).exists():
            return prepared
    made = _make(data, source.suffix.lower() == ".svg")
    if made is None:
        return None
    encoded, ext, size, file_size, transparent = made
    prepared = Prepared(
        hash=hashlib.sha256(encoded).hexdigest(), ext=ext, width=size[0], height=size[1],
        file_width=file_size[0], file_height=file_size[1], transparent=transparent, bytes=len(encoded),
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f".{prepared.file_name}.tmp"
    tmp.write_bytes(encoded)
    os.replace(tmp, cache_dir / prepared.file_name)
    write_json(meta_path, {"cache_version": CACHE_VERSION, "prepared": asdict(prepared)})
    return prepared


def _make(data: bytes, is_svg: bool):
    """Decode, scale and encode with Qt: (bytes, ext, original size, file size, transparent)."""
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt6.QtGui import QImage, QImageWriter, QPainter

    if is_svg:
        from PyQt6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        width, height = size.width(), size.height()
        if width <= 0 or height <= 0:
            return None
        fw, fh = zoom_size(width, height)
        # An SVG has no pixels of its own: drawn at the zoom size, not scaled from a bitmap.
        image = QImage(fw, fh, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
    else:
        image = QImage.fromData(data)  # a GIF gives its first frame
        if image.isNull():
            return None
        width, height = image.width(), image.height()
        fw, fh = zoom_size(width, height)
        if (fw, fh) != (width, height):
            image = image.scaled(fw, fh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    transparent = _has_transparency(image)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    ext = "png" if transparent else "jpg"
    writer = QImageWriter(buffer, b"PNG" if transparent else b"JPG")
    if not transparent:
        writer.setQuality(JPEG_QUALITY)
        image = image.convertToFormat(QImage.Format.Format_RGB32)
    if not writer.write(image):
        return None
    return bytes(buffer.data()), ext, (width, height), (fw, fh), transparent


def _has_transparency(image) -> bool:
    from PyQt6.QtGui import QImage

    if not image.hasAlphaChannel():
        return False
    argb = image.convertToFormat(QImage.Format.Format_ARGB32)
    bits = argb.constBits()
    bits.setsize(argb.sizeInBytes())
    # ARGB32 is stored B, G, R, A on a little-endian Mac: every fourth byte is alpha.
    return min(bytes(bits)[3::4]) < 255


def remember_helper_formats(state_dir: Path, formats) -> None:
    """The formats the helper passes on, from its cycle request. Absent: format 1."""
    valid = sorted({f for f in formats if isinstance(f, int) and not isinstance(f, bool)}) if isinstance(formats, list) else [1]
    path = state_dir / _FORMATS_FILE
    if read_json(path) != {"batch_formats": valid}:
        write_json(path, {"batch_formats": valid})


def helper_reads_format(state_dir: Path, version: int) -> bool:
    """Whether the helper said, in its last cycle request, that it passes on `version`."""
    try:
        data = read_json(state_dir / _FORMATS_FILE)
    except (ValueError, OSError):
        return False
    return isinstance(data, dict) and version in (data.get("batch_formats") or [])


def helper_reads_format_2(state_dir: Path) -> bool:
    return helper_reads_format(state_dir, 2)
