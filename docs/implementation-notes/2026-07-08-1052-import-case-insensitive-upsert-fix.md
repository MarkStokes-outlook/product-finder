# Import upsert case-sensitivity bug — implementation notes

**Date:** 2026-07-08 ~10:52
**Prior work:** docs/implementation-notes/2026-07-08-1041-project-import-export.md
**Tests:** 455 passing (453 prior + 2 new regression tests)
**Trigger:** Mark imported `docs/imports/ai-server.example.yaml` into the AI
Server project (id 5), which already had a hand-created `NVidia RTX 3080 Ti`
item (id 20) with his own notes. The import's item name is `NVIDIA RTX 3080
Ti` — different capitalisation — so the upsert-by-name lookup in
`project_import._find_item_by_name` (exact string match, by design at the
time) didn't find it and created a second item (id 23) instead of updating
the existing one.

## What changed

- `src/product_finder/project_import.py`:
  - `_find_item_by_name` now matches case/whitespace-insensitively, same as
    `_find_project_by_name` already did — an item name is a human label,
    not an identifier, so treating "NVIDIA RTX 3080 Ti" and "NVidia RTX
    3080 Ti" as the same item for upsert purposes is the least surprising
    behaviour. (The DB's `(project_id, name)` UNIQUE constraint stays
    case-sensitive — this only changes the importer's lookup, not the
    schema.)
  - The within-document duplicate-name check (two items in the same
    import sharing a name) is now case-insensitive too, for the same
    reason — previously `"NVIDIA RTX 3080 Ti"` and `"NVidia RTX 3080 Ti"`
    in the *same* file would both have matched onto one existing row
    without any warning that they collided.
  - Two regression tests added: upsert matches an existing item regardless
    of case, and a same-document case-variant collision is now a validation
    error rather than a silent double-upsert.

## Live-DB cleanup (data/product_finder.db, not test data)

Backed up first (`data/product_finder.db.bak.20260708T105156`, untracked,
local-only). Confirmed both items had zero `products`/`listing_matches`
before touching anything (nothing to lose either way):

- Kept item 23 (`NVIDIA RTX 3080 Ti`) — capitalisation consistent with all
  19 other items in the project, all imported from the same file.
- Copied item 20's notes (brand/cooler preferences) onto item 23, since
  item 23's own `notes` was empty.
- Deleted item 20.
- AI Server project is back to 20 items (was 21).

## Operational note

Both `watch` (pid 56125) and `web` (pid 66717) were running throughout;
`web` runs with `debug=False` (no auto-reload — see `cli.py: cmd_web`), so
**it is still running the old exact-match code in memory** and needs a
restart to pick up this fix for future imports. Not restarted by this
session, per standing instruction that Mark's watch/web processes are only
restarted at his request.
