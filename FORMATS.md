# File formats

The add-on and the helper exchange plain files in one folder, the exchange folder. Every file is JSON in UTF-8. Each is written whole under a temporary name (starting with `.` and ending in `.tmp`) and then moved into place, so a reader never sees a partial file. Readers ignore the temporary names.

Every file has a `format_version`. A reader that does not know the version must not guess at the contents.

Times are milliseconds since 1970-01-01 UTC, as whole numbers.

## Batch: `batch.json`, format version 1

Written by the add-on at export. Read by the helper. Replaced whole at each export.

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `batch_id` | text | A new UUID for each export |
| `exported_at_ms` | number | When the batch was exported |
| `deck_name` | text | The chosen deck's full name, as Anki shows it |
| `cards` | list | At most 60 cards, in the order Anki's queue serves them. May be empty. |

Each card:

| Field | Type | Meaning |
|---|---|---|
| `card_id` | number | Anki's card ID |
| `front` | list of runs | The front |
| `back` | list of runs | The back: only what follows Anki's answer divider |

A side is a list of runs. Joining the runs' `text` in order gives the side as plain text, with `\n` for line breaks. A side is never empty and never longer than 2,000 characters.

| Field | Type | Meaning |
|---|---|---|
| `text` | text | A stretch of the side. Never empty. |
| `styles` | list of text | The marks on all of it, in alphabetical order: any of `bold`, `cloze`, `italic`, `underline`. Empty for unmarked text. |

Adjacent runs never have the same styles. A reader must ignore the whole batch if the format version is unknown, and should show a run with a style it does not know as unmarked text.

Furigana is written into the text as word[reading], for example `漢字[かんじ]`. A cloze deletion is a run marked `cloze`: `[...]` on the front, the answer on the back.

```json
{
 "format_version": 1,
 "batch_id": "0b0e6c0e-2f4b-4a53-9a55-0d1d0c9b7e11",
 "exported_at_ms": 1791650000000,
 "deck_name": "Japanese::Japanese 2026",
 "cards": [
  {
   "card_id": 1686948984123,
   "front": [
    {"text": "The capital of ", "styles": []},
    {"text": "[...]", "styles": ["cloze"]}
   ],
   "back": [
    {"text": "The capital of ", "styles": []},
    {"text": "France", "styles": ["cloze"]}
   ]
  }
 ]
}
```

### What is in a batch

The chosen deck's due review cards, subdecks included, as Anki's queue serves them today. That queue respects the deck's daily review limit, so a deck with a large backlog exports at most what is left of today's limit. New cards and cards in learning or relearning steps are never included.

A card that is not watch-ready is skipped: left out of the batch, untouched, and still due in Anki. Skip reasons, of which a card gets the first that applies:

| Reason | The card has |
|---|---|
| `image_occlusion` | An image occlusion note type |
| `type_in` | A type-in-the-answer field |
| `script` | A script or embedded page |
| `video` | Video |
| `audio` | Audio, including text to speech |
| `image` | An image or drawing |
| `maths` | MathJax or LaTeX |
| `table` | A table |
| `hidden_content` | Styling known to hide content, on the card or in its note type |
| `other_markup` | Any other markup outside the allow-list, such as a list, a link, or an inline style other than colour, font and alignment |
| `too_long` | A side longer than 2,000 characters (Unicode code points, after whitespace is tidied) |
| `empty_side` | A front or back with no visible text |
| `render_error` | Anki could not render it |

## Status: `status.json`, format version 1

Written by the add-on. Read by the helper.

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `addon_version` | text | The add-on's version |
| `anki_version` | text | The version of Anki the add-on is running in, as Anki reports it, for example `26.09.3` |
| `deck_name` | text or null | The chosen deck's name; null when no deck could be exported |
| `last_export` | object or null | The last export, below |
| `last_replay` | object | Absent until the first replay. `at_ms`, and the counts `applied`, `dropped`, `held` of the last replay. |
| `last_cycle` | object or null | The last desktop cycle that ended, below. Null until one has. |

`last_export`:

| Field | Type | Meaning |
|---|---|---|
| `at_ms` | number | When it ran |
| `outcome` | text | `ok`; `no_deck_chosen`; `deck_missing`; `failed`. Only `ok` writes a batch. |
| `batch_id` | text or null | The batch written, if any |
| `exported` | number | Cards in the batch |
| `skipped` | object | Count of cards skipped, per reason |
| `empty_because` | text or null | When the batch has no cards: `nothing_due` (no review cards due, or today's review limit is used up) or `none_watch_ready` (every due card was skipped). Otherwise null. |
| `due_count` | number or null | How many review cards are due in the chosen deck and its subdecks, today or earlier, leaving out suspended and buried cards. The daily review limit is ignored, so it can be more than Anki's deck list shows and more than the batch holds. Null when the outcome is not `ok`. Written from add-on version 0.2.0; a status file from an older add-on has no such field. |

`last_cycle`, written once when a desktop cycle ends. The helper knows its request is done when `request_id` is its own.

| Field | Type | Meaning |
|---|---|---|
| `request_id` | text | The cycle request this cycle answered |
| `started_at_ms`, `finished_at_ms` | number | When the add-on took up the request, and when the cycle ended |
| `outcome` | text | `done`; or why it stopped before replay: `sync_failed`, `full_sync_needed`; or `error` if a step went wrong |
| `sync_in` | text | `ok`, `skipped` (sync is off or the user is not logged in), `failed`, `full_sync_needed`, `timed_out`, `not_started` |
| `applied`, `dropped`, `held` | number | Counts from replay. Absent if the cycle stopped before replay. |
| `held_because` | text or null | Set when nothing was replayed and every review event was held: `anki_newer_than_tested`, `backup_failed`, `no_deck_chosen` |
| `export` | text | The outcome of the export, as in `last_export` |
| `sync_out` | text | `ok`, `skipped`, `failed`, `full_sync_needed`, `timed_out`, `not_started`. The cycle is `done` whichever it is. |
| `error` | text | Only with outcome `error`: the kind of error |

When a cycle stops before replay, no results are written and the review events stay queued. When the outcome is `error`, results may or may not have been written; the helper goes by the results file's `request_id`.

The status is the authority on the batch file. When an export does not end `ok`, the batch file from an earlier export is left in place; the helper transfers a batch only when `last_export.outcome` is `ok` and `last_export.batch_id` equals the batch file's `batch_id`.

## Cycle request: `cycle-request.json`, format version 1

Written by the helper to start a desktop cycle. Read by the add-on, which looks for it every few seconds while Anki is open. The helper writes the review events file first, then this.

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `request_id` | text | New for each request. Letters, digits and hyphens, at most 64. |
| `requested_at_ms` | number | When the user chose Sync |

A request is run once per Anki profile. A cycle cut short, by Anki quitting for example, is run again when the add-on next sees the request.

## Review events: `review-events.json`, format version 1

Written by the helper. Read by the add-on. Holds the review events fetched from iCloud and not yet settled; the helper writes the whole list each time.

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `written_at_ms` | number | When the helper wrote the file |
| `events` | list | Review events, in any order |

Each review event, as the watch recorded it:

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `event_id` | text | Unique to this rating. Letters, digits and hyphens, at most 64. |
| `card_id` | number | Anki's card ID |
| `rating` | text | `again`, `hard`, `good` or `easy` |
| `answered_at_ms` | number | The moment of the rating |
| `time_taken_ms` | number | From the card appearing to the rating. Not negative. |
| `batch_id` | text | The batch the card came from |

```json
{
 "format_version": 1,
 "written_at_ms": 1791653000000,
 "events": [
  {
   "format_version": 1,
   "event_id": "5F1C2A0E-7C0B-4D0E-9A53-1F2B3C4D5E6F",
   "card_id": 1686948984123,
   "rating": "good",
   "answered_at_ms": 1791650255111,
   "time_taken_ms": 4250,
   "batch_id": "0b0e6c0e-2f4b-4a53-9a55-0d1d0c9b7e11"
  }
 ]
}
```

## Results: `results.json`, format version 1

Written by the add-on after replay. Read by the helper. One result for every review event in the file it read, in the order handled: oldest answer first.

| Field | Type | Meaning |
|---|---|---|
| `format_version` | number | 1 |
| `request_id` | text or null | The cycle request these results answer. The helper acts on results only when this is its own request. |
| `written_at_ms` | number | When replay finished |
| `counts` | object | `applied`, `dropped`, `held`: how many results have each outcome |
| `results` | list | One per review event |

Each result:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | text or null | The review event. Null only if the event had no readable ID. |
| `outcome` | text | `applied`, `dropped` or `held` |
| `reason` | text or null | Always set for `dropped` and `held`. For `applied`, null or a note. |

The helper removes a review event from iCloud when its outcome is `applied` or `dropped`, and keeps it when `held`.

| Outcome | Reason | Meaning |
|---|---|---|
| `applied` | null | Anki's scheduler recorded the answer in this replay |
| `applied` | `already_applied` | Recorded in an earlier replay; not recorded again |
| `applied` | `undone_in_anki` | Recorded earlier and since undone by the user in Anki; it stays undone |
| `dropped` | `unknown_format` | Unknown format version, or a field missing, of the wrong type or out of range. IDs are letters, digits and hyphens, at most 64. |
| `dropped` | `bad_rating` | A rating outside the four |
| `dropped` | `unknown_batch` | The add-on has no record of the batch named |
| `dropped` | `not_in_batch` | The card is not in the batch named |
| `dropped` | `card_missing` | The card no longer exists |
| `dropped` | `reviewed_elsewhere` | The card's review count is not what it was at export: reviewed, or reset, since. Desktop wins. |
| `dropped` | `changed_since_export` | The card has since been suspended, buried, reset or put in a filtered deck at the Mac |
| `dropped` | `bad_time` | The answer time is more than ten minutes before the batch was exported, or more than ten minutes in the future |
| `dropped` | `held_three_cycles` | Anki refused the answer in three replays |
| `held` | `anki_refused` | Anki refused the answer; it will be tried again |
| `held` | `error` | The add-on could not judge the event; it will be tried again |
| `held` | `anki_newer_than_tested` | Anki is newer than the add-on was tested with; nothing was replayed |
| `held` | `backup_failed` | Anki could not make a backup; nothing was replayed |
| `held` | `no_deck_chosen` | No deck is chosen for the watch in this Anki profile; nothing was replayed |

An event held as `anki_refused` or `error` in three desktop cycles is dropped as `held_three_cycles`. The other three holds do not count towards that.

## Kept by the add-on, not exchanged

Outside the exchange folder, under the add-on's `user_files/profiles/<profile>/`, one folder for each Anki profile:

- `settings.json`: `format_version` (1) and `deck_id`, the deck chosen for the watch.
- `cycle.json`: `format_version` (1) and `finished`, the ID of the last cycle request that ended.

- `batches/<batch_id>.json`: `format_version` (1), `batch_id`, `deck_id`, `exported_at_ms`, and `review_counts`: each exported card's review count at export, by card ID. Replay uses it to tell that a card was reviewed elsewhere. It does not travel to the watch.
- `applied.json`: the event IDs the add-on has applied, each with its card ID and answer time. One per collection.
- `held.json`: how many replays each held event has been held in.
- `addon.log`: one line per export (`export`), per skipped card (`skip`) and per export that went wrong (`export_error`, with the kind of error only), per review event outcome (`replay`), per refusal by Anki (`refused`, with the kind of error only) per replay (`replay_done`), per step of a desktop cycle (`cycle_start`, `sync_in`, `deck_check`, `version_check`, `backup`, `sync_out`, `cycle_end`, `work`, `cycle_error`, `waiting_to_leave_review`), and for a record or file it could not read (`record_unreadable`, `events_unreadable`, `event_error`), with card IDs, counts and reasons. No card content and no deck names.
