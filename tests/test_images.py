"""Preparing images and exporting batch format 2 (image cards spec, ticket 04; IM-11 to IM-14)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QGuiApplication, QImage  # noqa: E402

from sukima import export as export_module  # noqa: E402
from sukima.export import BATCH_FULL, export_batch  # noqa: E402
from sukima.images import helper_reads_format_2, prepare, remember_helper_formats, zoom_size  # noqa: E402
from sukima.text import IMAGE  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "design" / "samples" / "image-cards"
needs_samples = pytest.mark.skipif(not SAMPLES.exists(), reason="the owner's sample images are not here")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    # Anki has its application; tests make one, with no window.
    return QGuiApplication.instance() or QGuiApplication([])


def test_the_zoom_size_fills_the_screen_one_way_and_never_enlarges():
    assert zoom_size(1836, 628) == (1450, 496)  # the fretboard: tall enough to fill the height
    assert zoom_size(703, 1024) == (416, 606)  # a tall painting: wide enough to fill the width
    assert zoom_size(65, 65) == (65, 65)  # small: as it is


@needs_samples
@pytest.mark.parametrize("name, ext, transparent, size", [
    ("01.jpg", "jpg", False, (416, 606)),
    ("07.jpg", "jpg", False, (1450, 496)),
    ("10.png", "png", True, None),
    ("06.svg", "jpg", False, None),  # the flag fills its rectangle: no plate (Seal-ImageFront)
])
def test_samples_prepare_as_the_spec_says(tmp_path, name, ext, transparent, size):
    prepared = prepare(SAMPLES / name, tmp_path)
    assert prepared is not None
    assert prepared.ext == ext
    if transparent is not None:
        assert prepared.transparent is transparent
    if size:
        assert (prepared.file_width, prepared.file_height) == size
    data = (tmp_path / prepared.file_name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == prepared.hash and len(data) == prepared.bytes
    image = QImage.fromData(data)
    assert (image.width(), image.height()) == (prepared.file_width, prepared.file_height)


@needs_samples
def test_an_svg_is_rasterised_at_the_zoom_size(tmp_path):
    prepared = prepare(SAMPLES / "06.svg", tmp_path)
    assert (prepared.file_width, prepared.file_height) == zoom_size(prepared.width, prepared.height)


def test_a_file_qt_cannot_read_is_none(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert prepare(bad, tmp_path / "cache") is None


@needs_samples
def test_the_cache_decodes_nothing_twice(tmp_path, monkeypatch):
    first = prepare(SAMPLES / "07.jpg", tmp_path)
    calls = []
    monkeypatch.setattr("sukima.images._make", lambda *a: calls.append(a))
    assert prepare(SAMPLES / "07.jpg", tmp_path) == first
    assert calls == []


def test_the_helpers_formats_are_remembered(tmp_path):
    assert not helper_reads_format_2(tmp_path)
    remember_helper_formats(tmp_path, [1, 2])
    assert helper_reads_format_2(tmp_path)
    remember_helper_formats(tmp_path, None)  # a helper from before image cards
    assert not helper_reads_format_2(tmp_path)


# The export

@pytest.fixture
def picture_card(col, deck):
    """A due card with a transparent PNG on the front and the same image again on the back."""
    image = QImage(300, 200, QImage.Format.Format_ARGB32)
    image.fill(0)
    image.save(os.path.join(col.media.dir(), "diagram.png"))
    return deck.review("<img src='diagram.png'>Cell", "Nucleus<br><img src='diagram.png'>")


def read_batch(exchange):
    return json.loads((exchange / "batch.json").read_text())


def test_without_the_helpers_word_the_batch_is_format_1(col, deck, picture_card, exchange, state):
    result = export_batch(col, deck.id, exchange, state)
    batch = read_batch(exchange)
    assert batch["format_version"] == 1 and batch["cards"] == []
    assert result.skipped == {IMAGE: 1}
    assert not (exchange / "images").exists()


def test_format_2_carries_the_card_its_blocks_and_one_image(col, deck, picture_card, exchange, state):
    remember_helper_formats(state, [1, 2])
    export_batch(col, deck.id, exchange, state)
    batch = read_batch(exchange)
    assert batch["format_version"] == 2
    [card] = batch["cards"]
    [image] = batch["images"]  # on both sides, sent once
    assert card["front_blocks"][0] == {"image": image["hash"], "beside_previous": False}
    assert card["back_blocks"][-1]["image"] == image["hash"]
    assert image["transparent"] is True and (image["width"], image["height"]) == (300, 200)
    files = list((exchange / "images").iterdir())
    assert [f.name for f in files] == [f"{image['hash']}.png"]
    assert hashlib.sha256(files[0].read_bytes()).hexdigest() == image["hash"]


def test_old_images_leave_the_exchange_folder(col, deck, picture_card, exchange, state):
    remember_helper_formats(state, [1, 2])
    export_batch(col, deck.id, exchange, state)
    (exchange / "images" / "stale.jpg").write_bytes(b"x")
    export_batch(col, deck.id, exchange, state)
    assert len(list((exchange / "images").iterdir())) == 1


def test_past_the_cap_a_card_stays_in_anki_as_batch_full(col, deck, picture_card, exchange, state, monkeypatch):
    remember_helper_formats(state, [1, 2])
    deck.review("Plain text card")
    monkeypatch.setattr(export_module, "IMAGE_CAP_BYTES", 10)
    result = export_batch(col, deck.id, exchange, state)
    assert result.skipped == {BATCH_FULL: 1}
    # A text card after it still comes.
    assert [c["front"][0]["text"] for c in read_batch(exchange)["cards"]] == ["Plain text card"]


def test_the_media_folder_is_only_read(col, deck, picture_card, exchange, state):
    remember_helper_formats(state, [1, 2])
    before = sorted(os.listdir(col.media.dir()))
    export_batch(col, deck.id, exchange, state)
    assert sorted(os.listdir(col.media.dir())) == before
