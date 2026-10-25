# Sukima for Anki

**Review your Anki cards on your Apple Watch.** Sukima is an unofficial companion for Anki. It is not made by or affiliated with Anki or Ankitects.

This add-on is one of three parts. You also need:

- the **Sukima** app on your Apple Watch (watchOS 27 or later)
- the **Sukima** helper on your Mac (macOS 27 or later)
- Anki 26.09 or later on that Mac

The watch app and the helper are not released yet.

## How it works

1. The add-on prepares your due cards inside Anki.
2. The Mac helper passes them to your watch through your own iCloud.
3. You review on the watch, offline if you like.
4. When Anki is open on your Mac, your answers are saved into your collection through Anki's own scheduler, as if you had reviewed on the Mac. Then sync as usual, and AnkiMobile and AnkiWeb see them.

## What it never does

- It never signs in to or syncs with AnkiWeb, and never asks for your AnkiWeb password.
- It never changes your collection while Anki is closed, and never schedules cards itself.
- It collects no data: no analytics, no accounts.

## Good to know

- The watch shows text cards. Cards it can't show well (images, audio, complex layouts) stay in Anki for you to review there.
- Answers wait on iCloud until Anki is next open on your Mac.
- If Anki is newer than the latest version we've tested, you'll see a note, and your answers are still saved.

## Source and licence

This repository is the add-on's source. It is licensed under the AGPL, version 3 or later; see `LICENSE`. The add-on and the apps share only plain files in a documented format (`FORMATS.md`); no code is shared.

Status: in development. Not yet published on AnkiWeb.

## Layout

- `sukima/`: the add-on. Export logic needs only Anki's library, not its window.
- `tests/`: tests that build a real Anki collection for each case, with no Anki window.
- `FORMATS.md`: the files the add-on writes.
- `sukima/manifest.json`: the add-on's title, "Sukima for Anki", which Anki shows when it is installed from a package.
- `tools/package_addon.py`: packs the add-on into `dist/sukima.ankiaddon`. A plain copy of the folder in Anki's add-ons folder shows as `sukima` instead.
- `tools/release.py`: checks and builds a release, and creates the tagged GitHub release.

## Running the tests

Once:

```sh
python3 -m venv .venv
.venv/bin/pip install anki==26.9.3 pytest
```

Then, from this folder:

```sh
.venv/bin/python -m pytest tests
```

## Releasing

Set `__version__` in `sukima/version.py`, commit, then:

```sh
.venv/bin/python tools/release.py            # builds and checks dist/sukima-<version>.ankiaddon
.venv/bin/python tools/release.py --publish  # also tags v<version> and creates the GitHub release
```
