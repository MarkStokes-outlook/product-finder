# Connector Maturity — Phase A of the "acquisition platform" roadmap

**Date:** 2026-07-08 ~19:00
**Tests:** 560 passing (557 prior + 3 net new test functions, but 11 new
assertions-worth of coverage across `test_connectors.py` and
`test_coverage.py`)
**Trigger:** Mark reframed the project's goal from "search eBay well" to
"become the best acquisition platform for second-hand hardware" and laid
out a six-phase roadmap (A: connector maturity, B: coverage analytics, C:
capability explorer, D: health model, E: source roadmap, F: orchestration
foundation). This session delivered Phase A only, in two commits, per his
explicit "small commits" constraint. B–F not started.

## Reality check before building anything

Explored the existing connector framework before designing anything new
(see prior session's `2026-07-08-1155-coverage-phase-...` note — this is
the same framework, one session later). Found more already built than the
roadmap brief implied:

- `SourceCapabilities` (Phase C's ask) already exists in full, including
  the risk/compliance model from the previous session.
- `source_runs` + `db.record_source_run()` + `db.source_health()` already
  existed — searches/listings/errors per cycle, consecutive-failure
  streaks, 24h ingest volume.
- `db.source_coverage()` (Phase B's ask, largely) already existed — live
  vs. total listings, new in 24h/7d, stale count, hidden duplicates,
  catalogue match rate, 30-day price observation count.
- The Sources page already merges a Sources table and a Coverage table,
  driven live from these two functions — not the static config-only page
  the brief assumed.

So Phase A's actual gap was narrower than written: `first_seen`,
`last_failed_run`, `average_runtime`, and per-run
new/duplicate/catalogue-match/deal counts. Health *score* was explicitly
deferred to Phase D (confirmed with Mark before starting) rather than
building a throwaway scoring model now.

## Commit 1 — schema, runner instrumentation, aggregation (`9eceb94`)

`source_runs` gained `duration_ms`, `new_listings`, `duplicates`,
`catalogue_matches`, `deals_found` via the existing `_MIGRATIONS`
ALTER-TABLE mechanism (additive, `NOT NULL DEFAULT 0`, no backfill needed).

`first_seen` went on `source_settings` instead of `source_runs`, on
purpose: `source_runs` prunes anything older than 30 days on every write
(`_SOURCE_RUN_RETENTION_DAYS`), so a durable "when did we first see this
connector" fact can't live there without silently going stale/disappearing
once a connector's early history ages out. `source_settings` is already a
one-row-per-source table (enable overrides, eBay keys); adding a column
there was smaller than a new table. Write pattern is
`INSERT ... ON CONFLICT DO UPDATE SET first_seen = COALESCE(existing, new)`
— set once, in one statement, no read-before-write race.

`runner.run_once()` now times each `source.search()` call with
`time.perf_counter()` (deliberately scoped to the network call only, not
the per-listing DB/matching work after it — that's the number an
orchestrator would actually want later for Phase F scheduling decisions),
and reads three signals that were already being computed inline but
discarded: `upsert_listing()`'s `is_new` return (new vs. rescanned),
`resolve_identity()`'s `is_primary` (duplicate cross-source sighting), and
`evaluation.under_target` (already-defined "this is a deal" boolean, no new
threshold invented). `catalogue_matches` is just `product is not None`.
Nothing about the existing control flow changed — only extra counters
alongside it.

`source_health()` extended in place (same rows, same loop, not a new
function) with `first_seen`, `last_failed_at`, `total_runs`/`ok_runs` →
`success_rate`, and `avg_*` fields computed over whatever's left in the
30-day retention window. `last_failed_at` is bounded by that same window —
deliberately consistent with how `last_success_at` already behaved (a
success/failure older than 30 days silently isn't visible), rather than
building new durable failure logging that `first_seen` needed but
`last_failed_at` doesn't.

**Test worth noting:** `test_run_once_records_duplicates_for_secondary_cross_source_sighting`
relies on `enabled_names()` ordering (eBay before extras) to make eBay the
first-processed, primary sighting and an RSS proxy of the same eBay URL
the non-primary "duplicate" — confirmed the ordering in `config.py` rather
than assuming it.

## Commit 2 — Connector Stats table (`af563b4`)

New third table on `sources.html`, alongside the existing Sources and
Coverage tables, per Mark's confirmed preference over cramming ~10 more
columns into either existing table. No app.py route changes needed —
`source_list()` already passed the full `source_health()` dict through as
`row["health"]`, so the extended fields were already there.

Reused the existing `timeago` Jinja filter (already used elsewhere for
`first_seen`) for `first_seen`/`last_success_at`/`last_failed_at` rather
than inventing new formatting. `.card.table-card` already has
`overflow-x: auto` in `base.html`, so the wide table needed no new CSS.

Manually rendered the page via the Flask test client with mixed
success/failure data (no browser available in this environment) to eyeball
real output before committing — table renders correctly, consecutive
failures shows the existing `badge flag` style, success rate divides
correctly across mixed runs.

**Test-writing note:** first draft of
`test_sources_page_connector_stats_no_health_score_or_status_shown`
asserted `b"health score"` wasn't in the response — failed immediately
because the table's own hint text says "No health score yet" as a
transparency note to the user. Fixed by narrowing the assertion to the
actual Phase D status vocabulary (`Degraded`/`Offline`) rather than the
phrase itself; the prose explaining what's *not* built yet is a feature,
not a leak.

## What's deliberately not done

- No health score or Healthy/Warning/Degraded/Offline status — Phase D,
  confirmed deferred before starting.
- No capability-explorer UI changes (✓/✗ capability grid) — Phase C.
- No source roadmap metadata (current/planned/limitations per connector) —
  Phase E.
- No orchestration layer (scheduling, concurrency, rate-limit persistence,
  prioritisation) — Phase F. The watch loop still iterates every
  enabled/risk-allowed connector every cycle, unchanged.
- Coverage page metrics (average listing lifetime, time-to-first-match) not
  extended — Phase B.
- `average_runtime`/`average_listings_found`/etc. are windowed to whatever
  `source_runs` currently retains (≤30 days), not a separately configurable
  window — same bound the codebase already accepted for `last_success_at`.
