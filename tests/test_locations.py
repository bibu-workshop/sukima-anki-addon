"""The exchange folder the add-on uses must stay where the helper looks for it
(settings.py decides it in one place; the helper decides it in Locations.swift,
which is checked in the apps' repository)."""

from pathlib import Path

from sukima.settings import EXCHANGE_DIR


def test_the_exchange_folder_is_under_application_support_sukima():
    assert EXCHANGE_DIR == Path.home() / "Library" / "Application Support" / "Sukima" / "Exchange"
