"""Card sides as format 2 blocks (image cards spec, ticket 03; answer key IM-08 to IM-10, IM-15)."""

from __future__ import annotations

import pytest

from sukima.blocks import IMAGE_MISSING, card_sides_v2, convert_blocks
from sukima.text import EMPTY_SIDE, IMAGE, IMAGE_OCCLUSION, MATHS, OTHER_MARKUP, TABLE, card_sides


@pytest.fixture
def media(col):
    folder = col.media.dir()
    for name in ("flag.png", "map.png", "a.jpg", "b.jpg", "paste-3b5db7.jpg", "Raphael.jpg"):
        with open(f"{folder}/{name}", "wb") as f:
            f.write(b"stand-in")
    return folder


def kinds(blocks):
    return [("image+" if b["beside_previous"] else "image") if "image" in b else "item" if "list_item" in b else "text" for b in blocks]


def test_a_side_keeps_its_order_text_image_text(media):
    runs, blocks, reasons = convert_blocks('Heat<br><img src="a.jpg"><br>Cold<br><img src="b.jpg">', media)
    assert reasons == set()
    assert kinds(blocks) == ["text", "image", "text", "image"]
    assert [b["image"] for b in blocks if "image" in b] == ["a.jpg", "b.jpg"]
    assert runs == [{"text": "Heat\nCold", "styles": []}]


def test_images_with_nothing_between_are_beside_each_other(media):
    _, blocks, _ = convert_blocks('<img src="a.jpg"><img src="b.jpg"> <img src="map.png">', media)
    assert kinds(blocks) == ["image", "image+", "image+"]
    _, blocks, _ = convert_blocks('<div><img src="a.jpg"></div><div><img src="b.jpg"></div>', media)
    assert kinds(blocks) == ["image", "image"]
    _, blocks, _ = convert_blocks('<img src="a.jpg"><br><img src="b.jpg">', media)
    assert kinds(blocks) == ["image", "image"]


def test_an_image_alone_is_not_an_empty_side(media):
    runs, blocks, reasons = convert_blocks('<img src="flag.png">', media)
    assert EMPTY_SIDE not in reasons
    assert runs == [] and kinds(blocks) == ["image"]


def test_alt_text_is_kept_unless_it_is_a_file_name(media):
    _, blocks, _ = convert_blocks('<img src="Raphael.jpg" alt="The Transfiguration"><img src="a.jpg" alt="paste-3b5db7.jpg">', media)
    assert [b["alt"] for b in blocks] == ["The Transfiguration", None]


def test_a_url_encoded_name_is_read_as_the_file(media):
    _, blocks, reasons = convert_blocks('<img src="paste%2D3b5db7.jpg">', media)
    assert reasons == set() and blocks[0]["image"] == "paste-3b5db7.jpg"


def test_a_missing_file_keeps_the_card_in_anki(media):
    _, _, reasons = convert_blocks('<img src="gone.jpg">', media)
    assert IMAGE_MISSING in reasons
    # Reported before an empty side, so the cards-in-Anki count says why.
    from sukima.blocks import REASONS
    assert REASONS.index(IMAGE_MISSING) < REASONS.index(EMPTY_SIDE)


@pytest.mark.parametrize("src", ["https://example.com/a.jpg", "data:image/png;base64,AAAA", "sub/a.jpg", "a.bmp"])
def test_remote_embedded_or_unknown_pictures_stay_skipped(media, src):
    _, _, reasons = convert_blocks(f'<img src="{src}">', media)
    assert IMAGE in reasons


def test_lists_become_items_with_their_markers_and_levels(media):
    html = "<ul><li>one</li><li>two<ul><li>inner</li></ul></li></ul><ol start='3'><li>three</li><li>four</li></ol>"
    runs, blocks, reasons = convert_blocks(html, media)
    assert reasons == set()
    items = [(b["marker"], b["level"], b["list_item"][0]["text"]) for b in blocks]
    assert items == [("•", 1, "one"), ("•", 1, "two"), ("◦", 2, "inner"), ("3.", 1, "three"), ("4.", 1, "four")]
    assert runs[0]["text"].startswith("• one\n• two\n◦ inner\n3. three")


def test_a_third_list_level_is_skipped(media):
    _, _, reasons = convert_blocks("<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li></ul>", media)
    assert OTHER_MARKUP in reasons


def test_superscript_subscript_and_link_text(media):
    runs, blocks, reasons = convert_blocks('10<sup>2</sup> H<sub>2</sub>O <a href="https://x">Museum</a>', media)
    assert reasons == set()
    assert blocks is not None  # format 2: an older watch would read 10² as 102
    assert {"text": "2", "styles": ["superscript"]} in runs
    assert {"text": "2", "styles": ["subscript"]} in runs
    assert runs[-1]["text"].endswith("Museum")


def test_a_plain_side_needs_no_blocks(media):
    runs, blocks, reasons = convert_blocks("<b>約束</b>[やくそく]", media)
    assert blocks is None and reasons == set()
    assert runs == [{"text": "約束", "styles": ["bold"]}, {"text": "[やくそく]", "styles": []}]


def test_tables_maths_and_code_still_skip(media):
    assert TABLE in convert_blocks("<table><tr><td>a</td></tr></table>", media)[2]
    assert MATHS in convert_blocks(r"\(x^2\)", media)[2]
    assert OTHER_MARKUP in convert_blocks("<pre><code>x = 1</code></pre>", media)[2]


def test_format_1_is_unchanged_for_the_same_cards(col, deck, media):
    """Without format 2 the add-on converts exactly as before: images and lists skip."""
    card = deck.review("Heat<br><img src='a.jpg'>", "<ul><li>x</li></ul>")
    assert card_sides(card)[2] == IMAGE
    front, back, front_blocks, back_blocks, reason = card_sides_v2(card, media)
    assert reason is None and kinds(front_blocks) == ["text", "image"] and kinds(back_blocks) == ["item"]


def test_image_occlusion_enhanced_is_recognised_by_its_fields(col, deck, media):
    model = col.models.new("Image Occlusion Enhanced")
    for name in ("ID (hidden)", "Header", "Image", "Question Mask", "Footer", "Remarks", "Sources", "Extra 1", "Extra 2", "Answer Mask", "Original Mask"):
        col.models.add_field(model, col.models.new_field(name))
    template = col.models.new_template("IO Card")
    template["qfmt"] = "{{Header}}<div>{{Image}}</div>"
    template["afmt"] = "{{FrontSide}}<hr id=answer>{{Footer}}"
    col.models.add_template(model, template)
    col.models.add(model)
    card = deck.review(model="Image Occlusion Enhanced", fields={"Header": "Bones", "Image": '<img src="a.jpg">', "Footer": "Femur"})
    assert card_sides_v2(card, media)[4] == IMAGE_OCCLUSION


def test_io_elements_mark_occlusion_too(media):
    _, _, reasons = convert_blocks('<div id="io-wrapper"><img src="a.jpg"></div>', media)
    assert IMAGE_OCCLUSION in reasons
