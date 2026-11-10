"""The add-on's words in Anki: design guide 1.18, section 6, "In Anki"."""

from sukima import wording


def test_menu_item_and_dialog_words_are_the_guides():
    assert wording.MENU_ITEM == "Sukima: Choose Apple Watch Deck…"
    assert wording.DIALOG_TITLE == "Apple Watch Deck"
    assert wording.DIALOG_LEAD == "Choose the deck Sukima sends to your Apple Watch."
    assert wording.DECK_LABEL == "Deck:"
    assert wording.HELP_LINK == "How choosing a deck works"
    assert wording.HELP_URL == "https://sukima.bibuworkshop.com/help/#choose-a-deck"


def test_the_watch_sentence_waits_for_the_release_that_ships_it():
    assert wording.one_deck_paragraph(False) == (
        "One deck goes to the watch at a time. Choosing a sub-deck sends only that sub-deck. "
        "The change takes effect at once."
    )
    assert wording.one_deck_paragraph(True).endswith("The change takes effect at once. You can also change it on your watch.")


def test_tooltip():
    assert wording.chosen_tooltip("Music Theory", 1, 26) == (
        "Sukima: Music Theory goes to your Apple Watch. 1 card ready; 26 stay in Anki."
    )
    assert wording.chosen_tooltip("Japanese::Grammar", 12, 0) == (
        "Sukima: Japanese::Grammar goes to your Apple Watch. 12 cards ready."
    )
    assert wording.chosen_tooltip("Music Theory", 1, 1).endswith("1 card ready; 1 stays in Anki.")
    assert wording.chosen_tooltip("Empty", 0, 0).endswith("0 cards ready.")
