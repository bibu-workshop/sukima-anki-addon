# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sukima for Anki: the add-on.

The only part of the product that touches the collection, and only through
Anki's own calls. It exchanges plain files with the helper; see FORMATS.md.
"""

from .version import __version__

__all__ = ["__version__"]

try:
    from aqt import mw
except ImportError:  # Anki's library without its window, as in the tests
    mw = None

if mw is not None:
    from .anki_ui import start

    start()
