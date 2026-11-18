"""Sound cards: blocks, files, reasons, the shared cap and format 3 (sound cards spec,
ticket 02; answer key AU-04 to AU-08)."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QGuiApplication, QImage  # noqa: E402

from sukima import export as export_module  # noqa: E402
from sukima.blocks import MAX_SOUND_BYTES, SOUND_MISSING, card_sides_v2  # noqa: E402
from sukima.export import BATCH_FULL, export_batch  # noqa: E402
from sukima.images import remember_helper_formats  # noqa: E402
from sukima.text import AUDIO, VIDEO  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QGuiApplication.instance() or QGuiApplication([])


def media(col, name: str, data: bytes | None = None) -> bytes:
    data = data if data is not None else f"sound {name}".encode()
    with open(os.path.join(col.media.dir(), name), "wb") as f:
        f.write(data)
    return data


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_batch(exchange):
    return json.loads((exchange / "batch.json").read_text())


def sides(col, card):
    return card_sides_v2(card, col.media.dir(), sounds=True)


# Blocks (AU-04)


def test_a_sound_is_a_block_where_anki_has_its_tag(col, deck):
    media(col, "gato.mp3")
    card = deck.review("gato [sound:gato.mp3]", "cat")
    front, back, front_blocks, back_blocks, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [{"text": [{"text": "gato", "styles": []}]}, {"sound": "gato.mp3", "beside_previous": False}]
    assert front == [{"text": "gato", "styles": []}]
    assert back_blocks is None


def test_sounds_with_nothing_between_sit_side_by_side_and_a_break_parts_them(col, deck):
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        media(col, name)
    card = deck.review("word", "[sound:a.mp3][sound:b.mp3] cat<br>[sound:c.mp3]")
    *_, back_blocks, reason = sides(col, card)
    assert reason is None
    assert [(b.get("sound"), b.get("beside_previous")) for b in back_blocks if "sound" in b] == [
        ("a.mp3", False), ("b.mp3", True), ("c.mp3", False)]
    assert "text" in back_blocks[2]


def test_a_listening_card_has_only_a_sound_on_the_front(col, deck):
    media(col, "listen.m4a")
    card = deck.review("[sound:listen.m4a]", "escuchar")
    front, _, front_blocks, _, reason = sides(col, card)
    assert reason is None
    assert front_blocks == [{"sound": "listen.m4a", "beside_previous": False}]
    assert front == []  # no text on that side, and not "empty"


def test_the_back_holds_only_its_own_sounds_after_the_answer_divider(col, deck):
    media(col, "gato.mp3")
    card = deck.review("gato [sound:gato.mp3]", "cat [sound:gato.mp3]")
    *_, front_blocks, back_blocks, reason = sides(col, card)
    assert reason is None
    # The front's copy (from {{FrontSide}}) is cut at the divider; the back's own stays,
    # and the export names the same file by one hash for both.
    assert [b["sound"] for b in back_blocks if "sound" in b] == ["gato.mp3"]
    assert [b["sound"] for b in front_blocks if "sound" in b] == ["gato.mp3"]


# Kinds that stay in Anki (AU-05)


@pytest.mark.parametrize(
    "front, files, reason",
    [
        ("[sound:clip.mp4]", {"clip.mp4": b"v"}, VIDEO),
        ("[sound:word.ogg]", {"word.ogg": b"o"}, AUDIO),
        ("[sound:gone.mp3]", {}, SOUND_MISSING),
        ("[sound:long.mp3]", {"long.mp3": b"x" * (MAX_SOUND_BYTES + 1)}, AUDIO),
        ("<audio src='a.mp3'></audio>", {"a.mp3": b"a"}, AUDIO),
    ],
)
def test_sounds_the_watch_cannot_play_keep_the_card_in_anki(col, deck, front, files, reason):
    for name, data in files.items():
        media(col, name, data)
    card = deck.review(front, "back")
    assert sides(col, card)[-1] == reason


def test_text_to_speech_stays_in_anki(col, deck):
    model = col.models.copy(col.models.by_name("Basic"))
    model["name"] = "Basic TTS"
    model["tmpls"][0]["qfmt"] = "{{Front}} {{tts es_ES:Front}}"
    col.models.update_dict(model)
    card = deck.review(fields={"Front": "hola", "Back": "hi"}, model="Basic TTS")
    assert sides(col, card)[-1] == AUDIO


def test_without_format_3_a_sound_keeps_its_card_in_anki(col, deck):
    media(col, "gato.mp3")
    card = deck.review("gato [sound:gato.mp3]", "cat")
    assert card_sides_v2(card, col.media.dir())[-1] == AUDIO


# The export (AU-04, AU-06, AU-07, AU-08)


@pytest.fixture
def sound_card(col, deck):
    data = media(col, "gato.mp3")
    deck.review("gato [sound:gato.mp3]", "cat [sound:gato.mp3]")
    return data


def test_format_3_carries_the_sound_once_and_its_file(col, deck, sound_card, exchange, state):
    remember_helper_formats(state, [1, 2, 3])
    result = export_batch(col, deck.id, exchange, state)
    assert result.skipped == {}
    batch = read_batch(exchange)
    assert batch["format_version"] == 3
    h = sha(sound_card)
    assert batch["sounds"] == [{"hash": h, "type": "mp3", "bytes": len(sound_card)}]
    [card] = batch["cards"]
    assert card["front_blocks"][-1] == {"sound": h, "beside_previous": False}
    assert card["back_blocks"][-1] == {"sound": h, "beside_previous": False}
    files = list((exchange / "sounds").iterdir())
    assert [f.name for f in files] == [f"{h}.mp3"]
    assert files[0].read_bytes() == sound_card


def test_a_helper_that_passes_on_only_format_2_gets_no_sound_cards(col, deck, sound_card, exchange, state):
    remember_helper_formats(state, [1, 2])
    result = export_batch(col, deck.id, exchange, state)
    assert result.skipped == {AUDIO: 1}
    batch = read_batch(exchange)
    assert batch["format_version"] == 1 and "sounds" not in batch
    assert not (exchange / "sounds").exists()


def test_images_and_sounds_share_the_cap(col, deck, sound_card, exchange, state, monkeypatch):
    remember_helper_formats(state, [1, 2, 3])
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(0)
    image.save(os.path.join(col.media.dir(), "photo.png"))
    deck.review("<img src='photo.png'>", "a picture", overdue=0)
    # Room for the sound (first in Anki's queue) but not the image as well.
    monkeypatch.setattr(export_module, "IMAGE_CAP_BYTES", len(sound_card) + 10)
    result = export_batch(col, deck.id, exchange, state)
    assert result.skipped == {BATCH_FULL: 1}
    batch = read_batch(exchange)
    assert len(batch["cards"]) == 1 and batch["sounds"]


def test_old_sounds_leave_the_exchange_folder(col, deck, sound_card, exchange, state):
    remember_helper_formats(state, [1, 2, 3])
    export_batch(col, deck.id, exchange, state)
    (exchange / "sounds" / "stale.mp3").write_bytes(b"x")
    export_batch(col, deck.id, exchange, state)
    assert len(list((exchange / "sounds").iterdir())) == 1


def test_the_media_folder_is_only_read_with_sounds(col, deck, sound_card, exchange, state):
    remember_helper_formats(state, [1, 2, 3])
    folder = col.media.dir()
    before = {n: os.path.getmtime(os.path.join(folder, n)) for n in os.listdir(folder)}
    export_batch(col, deck.id, exchange, state)
    assert {n: os.path.getmtime(os.path.join(folder, n)) for n in os.listdir(folder)} == before


# Padding and margin (the owner, 2026-11-18: Kaishi 1.5k)


@pytest.mark.parametrize(
    "style, reason",
    [
        ("font-size: 25px; padding-bottom: 20px", None),
        ("margin: 0 auto; padding: 4px 8px", None),
        ("margin-left: -9999px", "hidden_content"),  # caught by the hiding check first
        ("margin-top: -2em", "other_markup"),
        ("width: 40vw", "other_markup"),
    ],
)
def test_padding_and_margin_are_ignored_unless_negative(col, deck, style, reason):
    card = deck.review(f"<div style='{style}'>palabra</div>", "word")
    assert sides(col, card)[-1] == reason
