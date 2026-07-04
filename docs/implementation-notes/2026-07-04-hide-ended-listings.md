# Implementation notes — hide ended listings from all browsing surfaces

**Date:** 2026-07-04
**Scope:** `src/product_finder/db.py`, `tests/test_web.py`
**Status:** shipped, 324 tests passing (2 new).

## Problem

Listings whose auction end time had passed were still appearing as current
deals. The auction countdown in the UI reaches zero and the card just… stays,
because nothing on the read path considered `listings.end_time` — visibility
only changed when a future watch cycle happened to stop returning the listing
(and the known un-pruned-stale-matches gap means even that isn't guaranteed).

## Design decision: filter at query time, not scan time

An ended listing isn't buyable at any price, so it should vanish **the moment
the clock passes its end time** — not up to an hour later when the next watch
cycle runs. That points at a read-time predicate rather than any
runner/pruning change:

```sql
(l.end_time IS NULL OR l.end_time > strftime('%Y-%m-%dT%H:%M:%S', 'now'))
```

One shared constant (`db._NOT_ENDED`), applied to all four read surfaces:

- `query_matches()` — match tables, dashboard hero/spotlight feed
- `project_top_picks()` — dashboard project preview cards
- `project_summaries()` — project card match counts / best score (inside the
  CASE guards, same pattern as the existing `is_primary_sighting` handling,
  so the LEFT-JOIN `item_count` is unaffected)
- `dashboard_stats()` — the stat strip counts

eBay's `end_time` strings (`2026-07-06T20:51:35.000Z`) are UTC ISO-8601 and
compare lexically against SQLite's UTC `now` rendered in the same
`YYYY-MM-DDTHH:MM:SS` prefix format, so no parameter plumbing or Python clock
is involved. Listings with `end_time` NULL (RSS entries, anything without an
end date) are untouched. The predicate also hides expired fixed-price
listings — correct for the same reason.

## What was deliberately NOT changed

- **Rows are hidden, not deleted** — listings, matches, and price
  observations stay in the DB for provenance and history, consistent with
  how non-primary sightings are handled.
- **`auction_watch` keeps its own queries** — it must keep polling briefly
  *past* end time to capture the closing price (the reliable "ended" signal
  is eBay's stock flip, not the timestamp; see the Phase 2 notes). The new
  predicate exists only on browsing/preview surfaces.
- The general stale-match pruning gap (sources silently dropping listings
  with no end date) is a separate, still-open design question — this change
  only covers the case with a known end timestamp.

## Verification

Real DB had 5 already-ended listings still visible; with the predicate all 5
are excluded from every surface. Regression tests cover both directions: a
past-end listing disappears from `query_matches`, top picks, project
summaries, stats, and the rendered dashboard (project card falls back to the
idle state); a future-end listing remains visible everywhere.

Requires Mark's `web` process restart to take effect (read-path change only —
no schema migration, no watch involvement).
