# SPDX-License-Identifier: AGPL-3.0-or-later
"""The add-on's own words in Anki, as design guide 1.18 gives them (section 6, "In Anki").

Kept apart from the window code so the tests can read them.
"""

from __future__ import annotations

MENU_ITEM = "Sukima: Choose Apple Watch Deck…"
DIALOG_TITLE = "Apple Watch Deck"
DIALOG_LEAD = "Choose the deck Sukima sends to your Apple Watch."
DECK_LABEL = "Deck:"
WHICH_CARDS = (
    "Sukima sends the review cards due today and in the next two days. New cards and cards "
    "you are still learning stay in Anki, and so do cards the watch can't show."
)
ONE_DECK = (
    "One deck goes to the watch at a time. Choosing a sub-deck sends only that sub-deck. "
    "The change takes effect at once."
)
# Added to ONE_DECK only in the release that ships choosing the deck on the watch (DEC-170).
ALSO_ON_WATCH = "You can also change it on your watch."
HELP_LINK = "How choosing a deck works"
HELP_URL = "https://sukima.bibuworkshop.com/help/#choose-a-deck"

# Whether this release lets the watch choose the deck (design guide 1.18, part B).
WATCH_CAN_CHOOSE = False


def one_deck_paragraph(watch_can_choose: bool = WATCH_CAN_CHOOSE) -> str:
    return f"{ONE_DECK} {ALSO_ON_WATCH}" if watch_can_choose else ONE_DECK


def _cards(n: int) -> str:
    return "1 card" if n == 1 else f"{n} cards"


def chosen_tooltip(deck_name: str, ready: int, stay: int) -> str:
    """After OK: "Sukima: Music Theory goes to your Apple Watch. 1 card ready; 26 stay in Anki."

    `ready` and `stay` count today's due review cards: sent, and left in Anki because the
    watch can't show them. The second clause is left out when none stay.
    """
    text = f"Sukima: {deck_name} goes to your Apple Watch. {_cards(ready)} ready"
    if stay:
        text += f"; {stay} {'stays' if stay == 1 else 'stay'} in Anki"
    return text + "."
