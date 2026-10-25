"""Exports the batch file the apps' tests read.

Builds a throwaway collection with 45 due review cards, runs the add-on's own
export, and copies the batch file out. Run after any change to the batch
format, naming the folder the apps' tests read (default: fixtures/):

    .venv/bin/python tools/make_fixture_batch.py [target-folder]
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from anki.collection import Collection  # noqa: E402

from conftest import Deck  # noqa: E402
from sukima.export import export_batch  # noqa: E402

TARGET = (Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures") / "batch.json"

FIRST = [
    ("plain front", "plain back"),
    ("a <b>bold</b>, an <i>italic</i> and an <u>underlined</u> word", "<b><i>both</i></b>"),
    ("two<br>lines", "日本語 🙂 &amp; more"),
]

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    col = Collection(str(tmp / "collection.anki2"))
    try:
        deck = Deck(col, "Fixture")
        for i, (front, back) in enumerate(FIRST):
            deck.review(front, back, overdue=100 - i)
        deck.review(
            model="Cloze",
            fields={"Text": "The capital of {{c1::France}} is Paris.", "Back Extra": ""},
            overdue=90,
        )
        for i in range(41):
            deck.review(f"front {i}", f"back {i}", overdue=80 - i)
        result = export_batch(col, deck.id, tmp / "exchange", tmp / "state", now_ms=1_791_650_000_000)
        assert result.exported == 45 and not result.skipped, result
    finally:
        col.close()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp / "exchange" / "batch.json", TARGET)
    print(f"wrote {TARGET}")
