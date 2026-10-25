"""Writes the exchange folders the apps' helper tests read.

Runs real desktop cycles of the add-on against a throwaway collection, with the
tests' stand-in for the Anki app, and copies the exchange folder out:

- exchange-after-cycle/: after a cycle with five review events, of which three
  were applied, one dropped (reviewed elsewhere) and one held (Anki refused it).
- exchange-sync-failed/: after a cycle that stopped because Anki's sync failed.
- exchange-before-first-cycle/: after an export only; no cycle has run.

Run after any change to the exchange formats, naming the folder the apps'
tests read (default: fixtures/):

    .venv/bin/python tools/make_fixture_exchange.py [target-folder]
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from anki.collection import Collection  # noqa: E402

from conftest import Deck  # noqa: E402
from test_cycle import Mac  # noqa: E402
from test_replay import answer_in_anki  # noqa: E402
from sukima import replay as replay_module  # noqa: E402

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures"


def copy_out(exchange: Path, name: str) -> None:
    target = TARGET / name
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(exchange, target)
    print(f"wrote {target}")


def world(tmp: Path, name: str) -> tuple[Collection, Mac]:
    col = Collection(str(tmp / f"{name}.anki2"))
    return col, Mac(col, Deck(col, "Fixture"), tmp / name / "exchange", tmp / name / "state")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    col, mac = world(tmp, "before")
    copy_out(mac.exchange, "exchange-before-first-cycle")
    col.close()

    col, mac = world(tmp, "after")
    events = mac.watch_answers(5)
    answer_in_anki(col, mac.cards[3], mac.base)  # the fourth card is reviewed at the Mac
    real = replay_module._answer

    def refuse_the_fifth(c, event):
        if event["event_id"] == events[4]["event_id"]:
            raise RuntimeError("refused")
        real(c, event)

    replay_module._answer = refuse_the_fifth
    mac.request("fixture-request-1")
    last = mac.run()
    replay_module._answer = real
    assert (last["applied"], last["dropped"], last["held"]) == (3, 1, 1), last
    copy_out(mac.exchange, "exchange-after-cycle")
    col.close()

    col, mac = world(tmp, "failed")
    mac.watch_answers(2)
    mac.anki.result = "failed"
    mac.request("fixture-request-2")
    assert mac.run()["outcome"] == "sync_failed"
    copy_out(mac.exchange, "exchange-sync-failed")
    col.close()
