"""A fixture deck with one due review card of each kind (US-006, AK-08, AK-28, AK-29)."""

import json

import pytest

from sukima.export import export_batch
from sukima.text import MAX_SIDE_CHARS, REASONS, convert


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def plain(text):
    return [{"text": text, "styles": []}]


def copy_of_basic(col, name, **changes):
    """A note type like Basic with its css or front template changed."""
    nt = col.models.copy(col.models.by_name("Basic"))
    nt["name"] = name
    if "css" in changes:
        nt["css"] += changes["css"]
    if "front" in changes:
        nt["tmpls"][0]["qfmt"] = changes["front"]
    col.models.update_dict(nt)
    return name


WATCH_READY = {
    "plain": ("What is the capital of France?", plain("What is the capital of France?")),
    "line breaks": ("one<br>two<div>three</div>", plain("one\ntwo\nthree")),
    "bold": (
        "a <b>bold</b> and <strong>strong</strong> word",
        [
            {"text": "a ", "styles": []},
            {"text": "bold", "styles": ["bold"]},
            {"text": " and ", "styles": []},
            {"text": "strong", "styles": ["bold"]},
            {"text": " word", "styles": []},
        ],
    ),
    "italics": (
        "<i>it</i> and <em>em</em>",
        [
            {"text": "it", "styles": ["italic"]},
            {"text": " and ", "styles": []},
            {"text": "em", "styles": ["italic"]},
        ],
    ),
    "underline": ("<u>under</u>", [{"text": "under", "styles": ["underline"]}]),
    "nested": (
        "<b>bold <i>both</i></b>",
        [{"text": "bold ", "styles": ["bold"]}, {"text": "both", "styles": ["bold", "italic"]}],
    ),
    "non-Latin": ("日本語 한국어 العربية ελληνικά 🙂", plain("日本語 한국어 العربية ελληνικά 🙂")),
    "entities": ("5 &lt; 6 &amp; 7&nbsp;&gt; 2", plain("5 < 6 & 7 > 2")),
    "harmless span": ('<span style="color: red">red</span>', plain("red")),
}

SKIPPED = {
    "image": '<img src="cat.png"> a cat',
    "audio": "listen [sound:word.mp3]",
    "video": "watch [sound:clip.mp4]",
    "table": "<table><tr><td>cell</td></tr></table>",
    "maths": r"solve \(x^2 = 4\)",
    "script": "<script>document.write('x')</script>text",
    "hidden_content": 'shown<span style="display:none">hidden</span>',
    "too_long": "x" * (MAX_SIDE_CHARS + 1),
    "other_markup": "<ul><li>one</li><li>two</li></ul>",
}


@pytest.fixture
def fixture_deck(col, deck):
    """Returns ({name: card} watch-ready, {reason: card} to be skipped)."""
    ready = {name: deck.review(front, "back") for name, (front, _) in WATCH_READY.items()}
    ready["cloze"] = deck.review(
        model="Cloze", fields={"Text": "The capital of {{c1::France}} is <b>Paris</b>.", "Back Extra": ""}
    )
    furigana = copy_of_basic(col, "Furigana", front="{{furigana:Front}}")
    ready["furigana"] = deck.review("私は 漢字[かんじ]を 勉強[べんきょう]する", "I study kanji", model=furigana)
    ready["back divider"] = deck.review("front only here", "back only here")

    skipped = {reason: deck.review(front, "back") for reason, front in SKIPPED.items()}
    skipped["type_in"] = deck.review("type it", "answer", model="Basic (type in the answer)")
    skipped["image_occlusion"] = deck.review(
        model="Image Occlusion",
        fields={"Occlusion": "{{c1::image-occlusion:rect:left=.1:top=.1:width=.2:height=.2}}", "Image": '<img src="map.png">'},
    )
    hiding = copy_of_basic(col, "Hiding", css=".extra { display: none; }")
    skipped["hidden_content (note type styling)"] = deck.review("looks plain", "back", model=hiding)
    skipped["too_long (back)"] = deck.review("short front", "y" * (MAX_SIDE_CHARS + 1))
    skipped["empty_side"] = deck.review("front", "<br>")
    return ready, skipped


def test_each_kind_is_converted_or_skipped_with_its_reason(col, deck, fixture_deck, exchange, state):
    ready, skipped = fixture_deck
    due_before = set(col.find_cards(f'deck:"{deck.name}" is:due'))

    result = export_batch(col, deck.id, exchange, state)

    assert result.outcome == "ok"
    cards = {c["card_id"]: c for c in read(exchange / "batch.json")["cards"]}
    assert set(cards) == {c.id for c in ready.values()}
    for name, (_, expected) in WATCH_READY.items():
        assert cards[ready[name].id]["front"] == expected, name
        assert cards[ready[name].id]["back"] == plain("back"), name

    expected_counts = {}
    for label in skipped:
        reason = label.split(" (")[0]
        expected_counts[reason] = expected_counts.get(reason, 0) + 1
    assert result.skipped == expected_counts
    assert read(exchange / "status.json")["last_export"]["skipped"] == expected_counts
    assert set(expected_counts) <= set(REASONS)
    # Every reason the spec names has a card here.
    assert set(expected_counts) >= {
        "image", "audio", "video", "table", "maths", "script", "type_in",
        "image_occlusion", "hidden_content", "too_long",
    }

    log = (state / "addon.log").read_text().splitlines()
    for label, card in skipped.items():
        reason = label.split(" (")[0]
        assert any(l.endswith(f"skip card={card.id} reason={reason}") for l in log), label
    export_line = next(l for l in log if " export " in l)
    assert "skipped_by_reason=" + ",".join(f"{r}:{n}" for r, n in sorted(expected_counts.items())) in export_line

    # Skipped cards are untouched and still due in Anki.
    assert set(col.find_cards(f'deck:"{deck.name}" is:due')) == due_before
    assert {c.id for c in skipped.values()} <= due_before


def test_cloze_is_marked_on_both_sides(fixture_deck, col, deck, exchange, state):
    ready, _ = fixture_deck
    export_batch(col, deck.id, exchange, state)
    card = next(c for c in read(exchange / "batch.json")["cards"] if c["card_id"] == ready["cloze"].id)
    assert card["front"] == [
        {"text": "The capital of ", "styles": []},
        {"text": "[...]", "styles": ["cloze"]},
        {"text": " is ", "styles": []},
        {"text": "Paris", "styles": ["bold"]},
        {"text": ".", "styles": []},
    ]
    assert card["back"] == [
        {"text": "The capital of ", "styles": []},
        {"text": "France", "styles": ["cloze"]},
        {"text": " is ", "styles": []},
        {"text": "Paris", "styles": ["bold"]},
        {"text": ".", "styles": []},
    ]


def test_furigana_reads_as_word_then_reading_in_brackets(fixture_deck, col, deck, exchange, state):
    ready, _ = fixture_deck
    export_batch(col, deck.id, exchange, state)
    card = next(c for c in read(exchange / "batch.json")["cards"] if c["card_id"] == ready["furigana"].id)
    assert card["front"] == plain("私は漢字[かんじ]を勉強[べんきょう]する")


def test_back_holds_only_what_follows_the_answer_divider(fixture_deck, col, deck, exchange, state):
    ready, _ = fixture_deck
    assert "front only here" in ready["back divider"].render_output().answer_text
    export_batch(col, deck.id, exchange, state)
    card = next(c for c in read(exchange / "batch.json")["cards"] if c["card_id"] == ready["back divider"].id)
    assert card["back"] == plain("back only here")


def test_type_in_card_is_left_out_and_still_due(col, deck, exchange, state):
    card = deck.review("type it", "answer", model="Basic (type in the answer)")
    result = export_batch(col, deck.id, exchange, state)
    assert result.outcome == "ok"
    assert read(exchange / "batch.json")["cards"] == []
    assert result.skipped == {"type_in": 1}
    assert card.id in col.find_cards("is:due")


def test_export_counts_past_skipped_cards_to_fill_the_batch(col, deck, exchange, state):
    for i in range(100):
        deck.review('<img src="x.png">' if i % 4 == 0 else f"card {i}", overdue=100 - i)
    result = export_batch(col, deck.id, exchange, state)
    assert result.exported == 60
    assert result.skipped == {"image": 20}


def test_deck_with_no_watch_ready_cards_gives_an_empty_batch_that_says_why(col, deck, exchange, state):
    deck.review('<img src="a.png">')
    deck.review("[sound:a.mp3]")
    result = export_batch(col, deck.id, exchange, state)
    assert result.outcome == "ok" and result.exported == 0
    assert read(exchange / "batch.json")["cards"] == []
    last = read(exchange / "status.json")["last_export"]
    assert last["empty_because"] == "none_watch_ready"
    assert last["skipped"] == {"image": 1, "audio": 1}


@pytest.mark.parametrize(
    "html, reason",
    [
        ("<video src='a.mp4'></video>", "video"),
        ("<audio src='a.mp3'></audio>", "audio"),
        ("[anki:play:q:0]", "audio"),
        ("<svg><circle/></svg>", "image"),
        ("<input type=text>", "type_in"),
        ("[[type:Back]]", "type_in"),
        ("<anki-mathjax>x</anki-mathjax>", "maths"),
        ("[latex]x[/latex]", "maths"),
        (r"\[x\]", "maths"),
        ("<iframe src='x'></iframe>", "script"),
        ("<style>.a{display:none}</style>x", "hidden_content"),
        ("<span hidden>x</span>", "hidden_content"),
        ("<div style='VISIBILITY : hidden'>x</div>", "hidden_content"),
        ("<a href='x'>link</a>", "other_markup"),
        ("x<sup>2</sup>", "other_markup"),
        ("<h1>t</h1>", "other_markup"),
        ("<div id='image-occlusion-container'></div>", "image_occlusion"),
        ("<span style='opacity:0'>x</span>", "hidden_content"),
        ("<span style='opacity: 0.0 !important'>x</span>", "hidden_content"),
        ("<span style='font-size:0'>x</span>", "hidden_content"),
        ("<span style='font-size: 0px;'>x</span>", "hidden_content"),
        ("<span style='color:transparent'>x</span>", "hidden_content"),
        ("<span style='color: rgba(0, 0, 0, 0)'>x</span>", "hidden_content"),
        ("<span style='display/**/:none'>x</span>", "hidden_content"),
        ("<div style='height:0;overflow:hidden'>x</div>", "hidden_content"),
        ("<div style='max-height:0'>x</div>", "hidden_content"),
        ("<div style='width:0'>x</div>", "hidden_content"),
        ("<div style='position:absolute;left:-9999px'>x</div>", "hidden_content"),
        ("<div style='text-indent:-9999px'>x</div>", "hidden_content"),
        ("<div style='clip: rect(0,0,0,0)'>x</div>", "hidden_content"),
        ("<div style='transform:scale(0)'>x</div>", "hidden_content"),
        ("<div style='filter:opacity(0)'>x</div>", "hidden_content"),
        ("<div style='position:absolute'>x</div>", "other_markup"),
        ("<div style='overflow:hidden'>x</div>", "other_markup"),
        ("<div style='white-space:pre'>x</div>", "other_markup"),
        ("<span style='color:#0000'>x</span>", "hidden_content"),
        ("<span style='color:#00000000'>x</span>", "hidden_content"),
        ("<span style='color: rgb(0 0 0 / 0)'>x</span>", "hidden_content"),
        ("<span style='color: rgba(0,0,0,0%)'>x</span>", "hidden_content"),
        ("<span onclick='go()'>x</span>", "script"),
        ("<b ONMOUSEOVER='go()'>x</b>", "script"),
        ("&#8203;", "empty_side"),
        ("\u200b \ufeff", "empty_side"),
        ("", "empty_side"),
        (" <br> &nbsp; ", "empty_side"),
    ],
)
def test_reasons(html, reason):
    assert reason in convert(html)[1]


@pytest.mark.parametrize(
    "html, text",
    [
        ("a\n\nb", "a b"),
        ("  a  <br>  b  ", "a\nb"),
        ("a<br><br><br><br>b", "a\n\nb"),
        ("<div>a</div><div>b</div>", "a\nb"),
        ("<p>a</p><p>b</p>", "a\nb"),
        ("<ruby>漢<rp>(</rp><rt>かん</rt><rp>)</rp></ruby>", "漢[かん]"),
        ("<!-- note -->a", "a"),
        ("a</b>b", "ab"),
        ("x" * 2000, "x" * 2000),
        ("<ruby>漢<rp>(<rt>かん<rp>)</ruby>字", "漢[かん]字"),
        ("<ruby>漢<rt>かん</ruby>字", "漢[かん]字"),
        ("the image-occlusion note type and 2 \\ 3", "the image-occlusion note type and 2 \\ 3"),
        ("<span style='color: red; font-weight: bold; font-size: 0.8em'>x</span>", "x"),
        ("<span style='background-color: transparent; color: #000'>x</span>", "x"),
        ("<span style='background-color: rgba(0, 0, 0, 0); color: rgba(0,0,0,0.87)'>x</span>", "x"),
        ("<span style='color: #000000; line-height: 0.9'>x</span>", "x"),
        ("<span style='opacity: 0.5; color: #000'>x</span>".replace("opacity: 0.5; ", ""), "x"),
    ],
)
def test_text(html, text):
    runs, reasons = convert(html)
    assert "".join(r["text"] for r in runs) == text
    assert not reasons


def test_adjacent_text_with_the_same_marks_is_one_run():
    assert convert("<b>a</b><b>b</b> c")[0] == [
        {"text": "ab", "styles": ["bold"]},
        {"text": " c", "styles": []},
    ]


def test_the_limit_is_two_thousand_characters_a_side():
    assert MAX_SIDE_CHARS == 2000
    assert "too_long" in convert("x" * 2001)[1]
    assert "too_long" not in convert("日" * 2000)[1]
    assert "too_long" in convert("日" * 2001)[1]


@pytest.mark.parametrize(
    "css",
    [
        ".x { display: none }", ".x{visibility:hidden}", ".x { opacity: 0; }", ".x{opacity:0}",
        ".x { font-size: 0 }", ".x { color: transparent }", ".x { height: 0; overflow: hidden }",
        ".x { position: absolute; left: -9999px }", ".x { display /* a */ : none }",
        '@import url("_shared.css");', ".x { -webkit-text-fill-color: transparent }",
    ],
)
def test_note_type_styling_that_hides_content_skips_its_cards(col, deck, exchange, state, css):
    model = copy_of_basic(col, "Hiding", css=css)
    deck.review("looks plain", "back", model=model)
    assert export_batch(col, deck.id, exchange, state).skipped == {"hidden_content": 1}


@pytest.mark.parametrize(
    "css",
    [
        ".x { opacity: 0.5 }", ".x { font-size: 0.8em; height: 10px; color: red }",
        ".card { background-color: transparent }", "a { border-color: transparent }",
        "div { min-width: 0; min-height: 0 }", "hr { border-width: 0 }", "h1 { background-clip: text }",
        "li { text-indent: -1em }", ".nightMode .card { color: white }", "img { max-width: 100% }",
    ],
)
def test_ordinary_note_type_styling_is_fine(col, deck, exchange, state, css):
    model = copy_of_basic(col, "Styled", css=css)
    deck.review("looks plain", "back", model=model)
    assert export_batch(col, deck.id, exchange, state).exported == 1


@pytest.mark.parametrize(
    "before_divider, reason",
    [
        ("<style>.b { display: none }</style>", "hidden_content"),
        ("<div style='display:none'>", "hidden_content"),
        ("<script>hide()</script>", "script"),
    ],
)
def test_what_comes_before_the_answer_divider_still_counts(col, deck, exchange, state, before_divider, reason):
    nt = col.models.copy(col.models.by_name("Basic"))
    nt["name"] = "Tricky"
    nt["tmpls"][0]["afmt"] = before_divider + "<hr id=answer><span class=b>{{Back}}</span>"
    col.models.update_dict(nt)
    deck.review("front", "back", model="Tricky")
    assert export_batch(col, deck.id, exchange, state).skipped == {reason: 1}


def test_image_occlusion_is_known_by_its_note_type_alone(col, deck):
    """Even if such a card rendered as plain text, its note type's kind rules it out."""
    from sukima.text import card_sides

    plain_card = deck.review("front", "back")
    assert card_sides(plain_card)[2] is None

    class OcclusionCard:
        def note_type(self):
            return {**plain_card.note_type(), "originalStockKind": 6}

        def render_output(self):
            return plain_card.render_output()

    assert card_sides(OcclusionCard())[2] == "image_occlusion"
    real = col.models.by_name("Image Occlusion")
    assert real["originalStockKind"] == 6
