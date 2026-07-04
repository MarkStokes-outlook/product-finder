# Implementation notes — source coverage metrics

**Date:** 2026-07-04
**Scope:** `db.py` (new `source_coverage()`), `web/app.py` (`/sources` route),
`sources.html` (new Coverage table), `tests/test_coverage.py` (new)
**Status:** shipped, 372 tests passing (10 new). Second deliverable of the
roadmap's "Market coverage" phase — the roadmap says these metrics "should
steer connector work", so they land *before* the next connector does.

## Intent

`source_health()` (connector framework v1) answers "did the connector's runs
succeed". It says nothing about whether those runs were *worth anything*.
`db.source_coverage()` answers the second question from the accumulated data
itself, per source:

- **Ingest**: total/live listing counts, new in 24h / 7d.
- **Freshness**: stale count — no `end_time` and not returned by the source
  for 48h (`_STALE_AFTER_HOURS`). This is the "probably sold/delisted but we
  can't know" bucket the ended-listings work couldn't address.
- **Duplicate suppression**: `is_primary_sighting = 0` count — one number
  covering both identity v1 (canonical URL) and confirmed v2 (fuzzy) hides.
  A high share means the source mostly re-shows items already seen elsewhere.
- **Catalogue match rate**: matches with a `product_id` / all matches for
  that source's listings. This is the headline number for the catalogue
  bottleneck — on the real DB it reads eBay 7%, hardwareswapuk 0%, which is
  the accessory-pollution problem made visible and trackable.
- **Price-history contribution**: `product_price_observations` in the last
  30 days, keyed by the observation's own `source` column (so
  auction-close-style contributors appear even with no listing rows).

Rendered as a second, deliberately quiet table on the Sources page
(automated-class connectors only; manual-assisted connectors never ingest, so
no row). No new tables, no schema change, no writes — pure read-time
aggregation, one query per metric family.

## Decisions

- **Stale ≠ old.** A listing the source *still returns* but which is
  months-old is handled by `max_age_days` at fetch time. Stale here means
  the source *stopped returning it* and nothing explains why (no end_time).
  These are precisely the rows a future pruning strategy would target — the
  metric sizes that problem before anyone designs the fix.
- **Windows**: 24h/7d for ingest (matches how often Mark actually looks),
  30d for price observations (matches `source_runs` retention and the price
  history window). All computed in Python as ISO strings, consistent with
  the rest of db.py — no SQLite date functions on the hot path.
- **Percentages rounded to integers** — this is a steering table, not a
  finance report.

## Explicitly not measurable yet (documented in the docstring)

- Product-suggestion yield by marketplace: `product_suggestions.source`
  records the *discovery mechanism* (`ebay-structured` / `ollama`), not the
  marketplace the sighting came from. Would need a column addition; not
  worth it until a second enrichment-capable connector exists.
- Enrichment success rate: only the attempt is recorded
  (`listings.brand_checked`), not the outcome.

## Deliberately not done

- No CLI subcommand — the Sources page is the established surface for
  source-level operational data; a CLI view can follow if Mark asks.
- No per-day breakdown/sparklines — counts in two windows are enough to
  steer; charting is polish this tool doesn't need yet.
- No retention/pruning action from the stale metric — measure first.

## Verification

- 10 new tests: window edges (24h/7d/30d/48h), ended-vs-stale distinction,
  per-source isolation, match-rate arithmetic including the no-matches
  `None` case, hidden-duplicate counting, observation sources without
  listings, plus Sources-page rendering (populated + empty states).
- Real-data smoke on a `VACUUM INTO` snapshot of the live DB (Mark's
  running web/watch processes untouched): eBay 4,978 listings / 782 new in
  24h / 7% catalogue match / 107 duplicates hidden / 77 price observations;
  hardwareswapuk 8 listings / 0% match. Rendered page verified against the
  same snapshot.

## Follow-ups this data already suggests

- The 7% eBay catalogue match rate quantifies the extraction-quality
  objective (roadmap "Catalogue quality") — re-check this number after any
  extraction work to see if it moved.
- Next connector candidates can now be judged against a baseline: a new
  source that ingests but never contributes catalogue matches or price
  observations shows up immediately.
