"""NFR-001: the collection changes only through Anki's own scheduler and backup calls.

This reads the add-on's source and refuses what it does not recognise as safe.
It rests on one convention, which it enforces where it can: the collection is
reached only as `col` or as something ending in `.col` (such as `mw.col`).

- Every use of the collection must be one of the dotted names in
  COLLECTION_USES, whatever it hangs off (`col.x`, `mw.col.x`, `self.col.x`,
  `card.col.x`). Anything else fails, including taking a method without
  calling it and assigning into the collection.
- The collection may be passed as an argument, compared, or stored in a
  variable called `col`. Nothing else: no other name for it, and no parameter
  typed as a collection under another name.
- No attribute is assigned, except directly on `self` (`self.x = ...`), the
  two time fields of the answer handed to Anki's scheduler, and the two
  functions of Anki's sync screen that the add-on wraps to hear of a failure.
- Anki's backend is not touched except through the names in COLLECTION_USES.
- The database is read only through `col.db.list` with a select written out
  in place.
- No name in WRITES is called or imported, on anything, and no second
  collection or manager is built.
- No route to the database or to code chosen at run time: sqlite3, Anki's db
  modules, Anki's background operations, __import__, importlib, eval, exec,
  getattr and setattr.

It cannot prove the absence of every possible write. WRITES is a list of known
names, so these get past it: a collection received under another name without
a type (`lambda c: c.decks.remove(...)`), a write method taken without being
called, a route through Anki's window that is not listed, and anything
deliberately disguised. Review of the add-on's calls is the other half of the
check. New uses of the collection are added to COLLECTION_USES only on review.
"""

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "sukima"

# Every way the add-on may use the collection. The three that change it are
# Anki's own deck selection, scheduler and backup calls.
COLLECTION_CHANGES = {"decks.select", "sched.answer_card", "create_backup"}
# Reading the database is allowed through this one call, and only with a
# query written out in place that starts with "select".
DB_READ = "db.list"
COLLECTION_READS = {
    "decks.get",
    "decks.selected",
    "decks.all_names_and_ids",
    # Added 2026-10-11 for the due count (design version 1): a deck's own and
    # child IDs, and a search. Both only read.
    "decks.deck_and_child_ids",
    "find_cards",
    "sched.get_queued_cards",
    "sched.build_answer",
    "_backend.get_scheduling_states",
    "get_card",
    "sync_status",
    DB_READ,
}
COLLECTION_USES = COLLECTION_CHANGES | COLLECTION_READS

# Names that write to the collection, a card, a note, a deck, a note type or
# Anki's files. Anki's managers (decks, models, tags) are only reachable through
# the collection, so their add, remove and update are covered by the allow-list.
WRITES = {
    "execute", "executemany", "executescript", "commit", "save", "flush",
    "update_card", "update_cards", "update_note", "update_notes",
    "add_note", "add_notes", "remove_notes", "remove_cards", "remove_cards_and_orphaned_notes",
    "set_config", "set_config_json", "remove_config", "set_deck", "set_due_date",
    "update_dict", "add_dict", "rename", "rem", "bulk_add", "bulk_remove",
    "add_tag", "remove_tag", "set_tags_from_str", "set_user_flag",
    "set_user_flag_for_cards", "suspend_cards", "suspend_notes", "unsuspend_cards",
    "bury_cards", "bury_notes", "unbury_cards", "unbury_deck", "forget_cards",
    "schedule_cards_as_new", "reset_cards", "reorder_cards", "reposition_new_cards",
    "find_and_replace", "import_anki_package", "restore_backup", "undo", "redo",
    "merge_undo_entries", "add_custom_undo_entry", "empty_filtered_deck",
    "rebuild_filtered_deck", "set_current", "mod_schema", "set_schema_modified",
    "fix_integrity", "optimize", "after_note_updates", "run_in_background",
    # Routes through Anki's window.
    "bury_current_card", "bury_current_note", "suspend_current_card", "suspend_current_note",
    "delete_current_note", "onUndo", "unloadCollection", "_delete",
}
# A second collection, or a manager built by hand. Naming the type is fine; calling it is not.
CONSTRUCTORS = {"Collection", "DeckManager", "ModelManager", "TagManager", "AnkiPackageImporter"}
MODULES = {"sqlite3", "anki.db", "anki.dbproxy", "anki._backend", "importlib", "apsw"}
MODULE_PREFIXES = ("aqt.operations",)
# The answer handed to Anki's scheduler carries the watch's own times.
ANSWER_FIELDS = {"answer.answered_at_millis", "answer.milliseconds_taken"}
# Anki's two functions for a sync that fails or needs a full sync are wrapped so
# the add-on hears of it. Neither touches the collection.
ANSWER_FIELDS |= {"aqt.sync.handle_sync_error", "aqt.sync.full_sync"}
BUILTINS = {"__import__", "eval", "exec", "getattr", "setattr", "delattr", "compile"}
BACKEND = {"_backend", "backend"}


def dotted(node):
    """(root name or None, [attribute names]) for an attribute chain."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.reverse()
    return (node.id if isinstance(node, ast.Name) else None), parts


def collection_path(node):
    """The dotted path used on the collection by this chain, or None if it does not touch one.

    '' means the chain is the collection itself.
    """
    name, parts = dotted(node)
    if "col" in parts:
        i = len(parts) - 1 - parts[::-1].index("col")
        return ".".join(parts[i + 1:])
    if name == "col":
        return ".".join(parts)
    return None


def is_select(call, func) -> bool:
    if not (isinstance(call, ast.Call) and call.func is func and call.args):
        return False
    query = call.args[0]
    return (
        isinstance(query, ast.Constant)
        and isinstance(query.value, str)
        and query.value.lstrip().lower().startswith("select ")
        and ";" not in query.value
    )


def problems(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def may_stand_alone(node):
        """The collection itself, not a use of it: passed on, compared, or stored as `col`."""
        up = parent.get(node)
        if isinstance(up, ast.Call) and node in up.args or isinstance(up, ast.keyword):
            return True
        if isinstance(up, (ast.Compare, ast.If, ast.While, ast.Assert)):
            return True
        if isinstance(up, ast.IfExp) and up.test is node:
            return True
        if isinstance(up, (ast.BoolOp, ast.UnaryOp, ast.IfExp)):
            # `not col`, `col or mw.col`: fine as a test or when stored as `col`.
            return may_stand_alone(up)
        return (
            isinstance(up, ast.Assign)
            and up.value is node
            and all(isinstance(t, ast.Name) and t.id == "col" for t in up.targets)
        )

    for node in ast.walk(tree):
        where = f"line {getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] + [f"{module}.{a.name}" for a in node.names]
            found += [f"{where}: imports {a.name}" for a in node.names if a.name in WRITES]
        else:
            names = []
        found += [
            f"{where}: imports {n}" for n in names if n in MODULES or n.startswith(MODULE_PREFIXES)
        ]

        if isinstance(node, ast.Name) and node.id in BUILTINS | WRITES:
            found.append(f"{where}: uses {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in WRITES:
                found.append(f"{where}: calls .{node.func.attr}()")
        if isinstance(node, ast.Call):
            called = getattr(node.func, "attr", getattr(node.func, "id", None))
            if called in CONSTRUCTORS:
                found.append(f"{where}: builds a {called}")

        # Any attribute or subscript being written, wherever it appears: plain
        # assignment, tuple unpacking, for and with targets, del.
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            own = isinstance(node.value, ast.Name) and node.value.id == "self"
            if not own and ast.unparse(node) not in ANSWER_FIELDS:
                found.append(f"{where}: assigns an attribute")
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if collection_path(node.value) is not None:
                found.append(f"{where}: assigns into the collection")
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            target = node.target
            own = isinstance(target.value, ast.Name) and target.value.id == "self"
            if not own:
                found.append(f"{where}: assigns an attribute")

        if isinstance(node, ast.arg) and node.annotation is not None:
            if "Collection" in ast.unparse(node.annotation) and node.arg != "col":
                found.append(f"{where}: a collection called {node.arg}")

        # Judge each attribute chain once, at its outermost node.
        outermost = isinstance(node, ast.Attribute) and not isinstance(parent.get(node), ast.Attribute)
        if outermost:
            path = collection_path(node)
            if path == "":
                if not may_stand_alone(node):
                    found.append(f"{where}: gives the collection another name")
            elif path is not None:
                if path not in COLLECTION_USES:
                    found.append(f"{where}: uses col.{path}")
                elif path == DB_READ and not is_select(parent.get(node), node):
                    found.append(f"{where}: reads the database with something other than a plain select")
            elif BACKEND & set(dotted(node)[1]):
                found.append(f"{where}: reaches Anki's backend")
        if isinstance(node, ast.Name) and node.id == "col" and isinstance(node.ctx, ast.Load):
            if not isinstance(parent.get(node), ast.Attribute) and not may_stand_alone(node):
                found.append(f"{where}: gives the collection another name")
    return found


def test_add_on_source_has_no_direct_write_to_the_collection():
    files = sorted(SOURCE.rglob("*.py"))
    assert files
    found = {f.name: problems(f.read_text()) for f in files}
    assert {name: p for name, p in found.items() if p} == {}


@pytest.mark.parametrize(
    "line",
    [
        "col.db.execute('update cards set due = 0')",
        "rows = col.db.all('select 1')",
        "col.db.list('update cards set due = 0')",
        "col.db.list('select 1; delete from cards')",
        "col.db.list(query)",
        "col.db.list(f'select {x}')",
        "read = col.db.list",
        "import sqlite3",
        "from anki.db import DB",
        "from anki import db",
        "from anki import dbproxy",
        "__import__('sqlite3')",
        "import importlib",
        "col.update_card(card)",
        "f = col.update_card\nf(card)",
        "card.flush()",
        "card.due = 0",
        "c = col.get_card(1)\nc.due = 0",
        "col.set_config('x', 1)",
        "col.conf['x'] = 1",
        "col.decks.update(deck)",
        "col.models.update(nt)",
        "col.models.add(nt)",
        "col.tags.bulk_add([1], 'x')",
        "col.find_and_replace(note_ids=[1], search='a', replacement='b')",
        "col.sched.set_due_date([1], '0')",
        "col.sched.reset_cards([1])",
        "col.sched.reorder_cards([1])",
        "col._backend.set_config_json(key='x', value_json=b'1')",
        "col._backend.remove_cards([1])",
        "col.import_anki_package(req)",
        "col.remove_notes([1])",
        "open(col.path, 'wb')",
        "mw.col.update_card(card)",
        "getattr(col, 'update_' + 'card')(card)",
        "c = col\n",
        "c = mw.col\n",
        "aqt.mw.col.decks.remove([did])",
        "aqt.mw.col.db.all('update cards set due = 0')",
        "aqt.mw.col.conf['x'] = 1",
        "self.col.models.add(nt)",
        "self.mw.col.sched.bury_cards(ids)",
        "def replay(collection):\n    collection.sched.suspend_cards(ids)",
        "def replay(collection: Collection):\n    collection.decks.remove([1])",
        "card.col.sched.suspend_cards([1])",
        "card.col._backend.remove_cards([1])",
        "mw.backend.remove_cards([1])",
        "mw.backend.anything()",
        "from aqt.operations.scheduling import suspend_cards\nsuspend_cards(parent=mw, card_ids=ids).run_in_background()",
        "from aqt.operations import scheduling",
        "from anki.something import update_card\nupdate_card(card)",
        "c = col or mw.col\nc.decks.remove([1])",
        "c = mw.col if mw else None",
        "c = not col",
        "self.card.due = 0",
        "card.due, card.ivl = 0, 1",
        "[card.due] = [0]",
        "for card.due in range(3):\n    pass",
        "with f() as card.due:\n    pass",
        "card.due += 1",
        "del card.due",
        "self.note.tags = ['x']",
        "from anki._backend import RustBackend",
        "mw.reviewer.bury_current_card()",
        "Collection(path).decks.remove([1])",
        "DeckManager(col).remove([1])",
    ],
)
def test_the_check_catches_a_direct_write(line):
    assert problems(line)


def test_the_check_allows_the_scheduler_and_backup_calls_and_ordinary_code():
    assert not problems(
        "def run(col, mw):\n"
        "    if col is None or not mw.col:\n"
        "        return\n"
        "    if not (col and mw.col):\n"
        "        return\n"
        "    while not col:\n"
        "        return\n"
        "    assert col\n"
        "    col = col or mw.col\n"
        "    if (mw.col if mw else None) is None:\n"
        "        return\n"
        "    col = mw.col\n"
        "    export_batch(mw.col, 1, a, b)\n"
        "    export_batch(col=col)\n"
        "    before = col.decks.selected()\n"
        "    col.decks.select(1)\n"
        "    col.sched.answer_card(a)\n"
        "    col.create_backup(backup_folder=f, force=True, wait_for_completion=True)\n"
        "    card = mw.col.get_card(1)\n"
        "    ids = col.db.list('select id from revlog where cid = ?', 1)\n"
        "    states = col._backend.get_scheduling_states(card.id)\n"
        "    a = col.sched.build_answer(card=card, states=states, rating=r)\n"
        "    counts = {}\n"
        "    counts[card.id] = card.reps\n"
        "    self.seen = set()\n"
        "    self.seen.add(1)\n"
    )


def test_licence_is_agpl():
    root_dir = SOURCE.parent
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (root_dir / "LICENSE").read_text()
    for f in SOURCE.rglob("*.py"):
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in f.read_text().splitlines()[0], f.name
