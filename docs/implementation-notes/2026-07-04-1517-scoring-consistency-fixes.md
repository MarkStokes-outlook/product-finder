# Implementation notes — scoring consistency fixes

**Date:** 2026-07-04
**Scope:** `src/product_finder/scoring.py`, `src/product_finder/db.py`,
`tests/test_scoring.py`, `tests/test_web.py`
**Status:** shipped, 322 tests passing (6 new). Follows directly from the same-day
deal_score recalibration (see `2026-07-04-1434-deal-score-recalibration.md`).

## What changed and why

Three places where the codebase's own design principle — *never reward a price
the buyer couldn't actually commit to* — was applied inconsistently across
otherwise-equivalent categories.

### 1. Live auctions can no longer be `under_target`

`evaluate()` already refused `under_target` for multi-item/price-range
listings and (since the recalibration) implausible prices, but a live
auction's current bid could still be marked as meeting the item's target —
despite the codebase elsewhere treating a live bid as an uncommittable price
(auctions are structurally excluded from hero cards for exactly this reason).
`under_target` now also requires the listing not be a live auction.

### 2. The target bonus inside `deal_score()` tracks the same three categories

The +10 target bonus was only being withheld for implausible prices. It is
now withheld for all three ambiguous-price categories — live auction,
multi-item/price range, implausible price — keeping `deal_score()` and
`evaluate()`'s `under_target` in lockstep. Detection is via the flag strings
already present in the `flags` list passed to `deal_score()`, so no signature
change; the flag literals (`live auction`, `multiple items / price range`)
were promoted to module constants (`FLAG_LIVE_AUCTION`, `FLAG_MULTI_ITEM`)
alongside the existing `FLAG_IMPLAUSIBLE_PRICE`. Stored flag values in the DB
are unchanged — the constants carry the same strings.

### 3. `project_top_picks()` only surfaces clean matches

The dashboard hero already filtered to clean listings, but the per-project
preview cards took the highest score regardless of flags — so a project's
"top pick" could be a spares/repair listing the hero would refuse to show.
`project_top_picks()` now applies the identical predicate to
`query_matches(flagged=False)` and `dashboard_stats()`:
`m.flags = '[]' AND m.grade != 'spares/repair'`. A project whose matches are
all flagged now falls back to the existing "Still watching" idle state rather
than promoting a warned listing.

## Score impact

Small and one-directional: live auctions and bundle listings that sit under
an item's target lose the +10 bonus (they already carried an −8 flag
penalty), so a fixed-price/single-item listing now strictly outscores its
ambiguous equivalent. No other scores move. Stored scores refresh as listings
re-match each watch cycle (watch process restart required, as with the
recalibration commit).

## Tests added

- Live auction under target price → flagged, `under_target == False`.
- `deal_score()` with a live-auction or multi-item flag scores identically
  with and without a target price (bonus provably withheld).
- Fixed-price listing strictly outscores an equivalent live auction;
  single-item strictly outscores an equivalent "job lot" bundle.
- `project_top_picks()` returns the lower-scoring clean match over a
  higher-scoring flagged/spares one; a project with only flagged matches
  yields no pick and renders the idle preview.
