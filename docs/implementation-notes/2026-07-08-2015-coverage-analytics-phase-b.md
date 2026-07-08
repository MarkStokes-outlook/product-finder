# Coverage Analytics — Phase B of the "acquisition platform" roadmap

**Date:** 2026-07-08 ~20:15
**Tests:** 578 passing (575 prior + 3 net new; `test_coverage_analytics.py`
is new, plus 3 page-rendering tests added to `test_coverage.py`)
**Trigger:** Continuation of the six-phase roadmap from
[[acquisition_platform_roadmap]] — Phase A (connector maturity) shipped
earlier the same session; this is Phase B (coverage analytics), scoped by
Mark as: total sightings, unique listings, duplicate suppression rate,
catalogue match rate, deal rate, stale rate, average listing lifetime,
average time-to-first-match, price history coverage — each either honestly
computed or explicitly marked unavailable with a stated reason, no
approximate metrics invented without provenance.

## Design decisions made before writing code

**New function, not an extension of `source_coverage()`.** Phase A
extended `source_health()` in place (same rows, same loop). Phase B
instead got its own `db.source_coverage_analytics()`, composing on top of
`source_coverage()` by calling it and layering new fields — because Mark's
brief explicitly separated "Coverage" (raw counts) from "Coverage
Analytics" (rates) as a UI/conceptual split, and the existing
`source_coverage()` function was already sizeable. Two new lightweight
`GROUP BY` queries added (deal counts, resolved-listing lifetimes); every
other new field is arithmetic on numbers `source_coverage()` already
computed — no per-listing scans, safe for dashboard load per Mark's
"avoid heavy queries" constraint.

**total_sightings / unique_listings / duplicate_suppression_pct** reuse
`source_coverage()`'s existing `listings_total` and `hidden_duplicates`
with zero new SQL — `unique_listings = listings_total - hidden_duplicates`
(i.e. primary-sighting count, `is_primary_sighting = 1`). This reading
(raw pre-dedup count vs. post-dedup count) fit the codebase's existing
`is_primary_sighting` concept directly, and matched the three-metric order
in Mark's brief (total → unique → suppression rate) as a funnel. Considered
and rejected two other readings before settling on this: (a) "sightings"
as a literal rescan-frequency counter, which would have needed a new
`listings.times_seen` column bumped on every upsert — real instrumentation
the brief's "reuse existing data" priority argued against when an existing
concept already answered the question; (b) treating `listings_total` and
"unique listings" as the same number, which would have made two of the
three requested metrics redundant with each other.

**deal_rate_pct filters to primary listings only** (`is_primary_sighting =
1`), joined against `listing_matches.under_target` — which was already the
correct, existing "is this a deal" boolean (no new threshold invented,
same field Phase A's `deals_found` used). Filtering to primary matters:
`record_match()` writes a row for *every* scanned listing regardless of
duplicate status (full provenance, per its own docstring), so an
unfiltered join would let a cross-source duplicate of a genuine deal count
twice.

**avg_lifetime_days / lifetime_sample_size** only include listings that
have *resolved* — `end_time` set and in the past (exact), or no `end_time`
but unseen for `_STALE_AFTER_HOURS` (48h, the same threshold
`source_coverage()`'s `stale` count already uses — approximate, using
`last_seen` as a proxy for the true end). Still-live listings being
rescanned every cycle are deliberately excluded: their lifetime hasn't
concluded, so folding in "time since first seen" would understate the true
average and make it drift downward every cycle purely from more listings
still being active. `lifetime_sample_size` is always reported alongside
the average, and the field is `None` (not `0`) when nothing has resolved
yet — same "don't imply precision that isn't there" pattern as
`catalogue_match_pct`.

**price_history_coverage_pct** is explicitly documented as a ratio of
aggregate counts, not a verified per-listing join —
`product_price_observations` rows are keyed by `(product_id, source,
observed_at)`, not `listing_id`, so there's no way to confirm a specific
observation came from a specific listing without a schema change. Computed
as all-time `product_price_observations` count (deliberately *not*
windowed to 30 days like the existing `price_observations_30d` — a
coverage *rate* wants the full history, not a recent slice) divided by
`matches_catalogued`. Documented as "roughly how much of what we
catalogued from this source ever produced a price data point," not an
exact figure — the honest caveat is in the docstring and worth remembering
if this number is ever queried in isolation.

## What's marked unavailable, and why

**time_to_first_match is always `None`.** Investigated whether it was
computable before writing any code: `listing_matches.matched_at` is
stamped once, at `INSERT` time, in `db.record_match()` — and because
`runner.run_once()` calls `record_match()` on every listing's very first
scan (matching is synchronous with ingestion, not a separate later step),
`matched_at` is always ≈ `first_seen` for the vast majority of rows. Worse,
`product_id` *is* overwritten on every rescan (the `UPDATE` path in
`record_match`), so a listing that starts unmatched and later resolves to
a catalogue product (e.g. once that product gets added) silently gains a
`product_id` with zero record of *when* that transition happened. There is
no honest way to answer "how long after first being seen did this source's
listings typically get catalogued" from what's persisted today — it would
need a new `catalogue_matched_at` column, set once when `product_id` first
transitions from `NULL` to non-`NULL`. Reported via a module-level
constant, `db.TIME_TO_FIRST_MATCH_UNAVAILABLE`, used identically by the
aggregation function and asserted against directly in tests — one string,
not duplicated prose that could drift.

Surfaced in the UI as "not tracked" with the exact reason as a hover
tooltip (`title` attribute) rather than omitting the column — Mark's brief
asked for this to be explained, not silently dropped.

## UI

Fourth table on `sources.html`: "Coverage Analytics", placed directly
after the existing "Coverage" table (grouping the two coverage-related
tables together) and before "Connector Stats" (which is operational/run
history, a different axis — Mark confirmed this grouping preference during
Phase A). No new CSS needed (`.card.table-card`'s existing `overflow-x:
auto` handles the width). Manually rendered via the Flask test client with
mixed data (ended/stale/live listings, a cross-source duplicate, a
catalogue-matched deal, a price observation) to verify the arithmetic by
eye before committing — 3 sightings/2 unique/33% suppression/50% catalogue
match/50% deal rate/3.0d avg lifetime (n=1)/100% price history coverage
all checked out against hand-calculated expected values.

## What's deliberately not done

- No new schema for `catalogue_matched_at` — `time_to_first_match` stays
  honestly unavailable rather than half-building the fix as a Phase B
  side-quest; if this metric becomes a priority it's a small, well-scoped
  follow-up, not something to bolt on here.
- Phases C (capability explorer UI), D (health score/status), E (source
  roadmap metadata), F (orchestration layer) not started.
- No change to `source_coverage()` itself — Phase B is additive, layered
  on top, per the "reuse existing data" instruction.
