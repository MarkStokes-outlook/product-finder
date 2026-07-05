# Same-marketplace duplicate detection removed — implementation notes

**Date:** 2026-07-05 ~20:10
**Prior design:** docs/design/2026-07-04-fuzzy-duplicate-grouping.md (this
note supersedes the same-source branch of it; cross-marketplace detection is
unchanged)
**Tests:** 419 passing (no count change — existing tests rewritten, not added)
**Trigger:** Mark spotted a bunch of "potential duplicate" listings that were
actually one seller with multiple identical units of stock for sale under
different listing IDs — each free to sell or reprice independently of the
other. His rule: different listing ID always means a different listing.

## What changed

- `src/product_finder/duplicates.py` `evaluate_pair()`: same-marketplace
  pairs are now rejected unconditionally, first thing, before any
  title/price/location/image scoring runs. Previously they were allowed
  through if they shared a non-empty `location` or an identical `image_url`
  (the "seller proxy" gate) — that gate is gone. Only cross-marketplace pairs
  are ever scored now; `cross_source` in the signals dict is therefore always
  `True`, and `CROSS_SOURCE_PENALTY` is applied unconditionally.
- In hindsight, the design doc's own flagship justifying example (two eBay
  VIEWEDGE monitor listings, same location, different item IDs, different
  price, different condition) was exactly this false-positive pattern, not a
  genuine duplicate. `tests/test_duplicates.py` keeps that case as a named
  regression test (`test_same_marketplace_pair_never_queues_even_with_every_signal_matching`)
  and adds a cross-marketplace version of the same title/price/location to
  confirm the scoring math itself still works when the pair is genuinely
  cross-marketplace.
- All other `test_duplicates.py`/`test_web.py` fixtures that seeded pairs on
  the same source (`"ebay"`/`"ebay"`) now seed cross-marketplace pairs
  (`"ebay"`/`"gumtree"`, sometimes a third `"facebook"` for cap tests) so they
  keep exercising the db/web plumbing rather than tripping the new gate.

## Live-DB cleanup (data/product_finder.db, not test data)

Backed up first (`data/product_finder.db.bak.20260705T200501`, untracked,
gitignored pattern doesn't match it — left in `data/` as a local-only
safety copy, not committed). Audited before touching anything:

- 691 `listing_duplicates` rows existed; **all 691 were same-marketplace** —
  cross-marketplace candidates have never actually fired in practice.
- 608 were `pending` (the false-positive backlog Mark saw) → bulk-dismissed.
- 80 were already `confirmed` — meaning 80 listings were hidden
  (`is_primary_sighting = 0`) on the old heuristic → bulk-reverted (restores
  visibility) then bulk-dismissed (so they don't re-enter the pending queue;
  dismissed pairs are remembered forever per the existing uniqueness rule).
- 3 were already `dismissed`, untouched.
- Verified after: 699 rows, all `dismissed`; every pair's both sides are
  `is_primary_sighting = 1` except 4 that were already hidden by unrelated
  canonical-identity (v1) resolution, not by this bug.

## What's unchanged

Cross-marketplace fuzzy duplicate detection (the original "eBay vs Gumtree
cross-post" motivating case from the roadmap) is untouched — same gates,
same confidence blend, same review workflow. It has just never produced a
candidate yet in Mark's real data, since same-marketplace was the only thing
actually firing.

## Operational note

Mark's `watch`/`web` processes are currently stopped at his request, so no
restart was needed for this change to take effect on the live DB (the fix
was applied directly via `db.dismiss_duplicate`/`db.revert_duplicate`, same
as the app would). When `watch` restarts, `scan_duplicate_candidates` will
simply stop generating same-marketplace rows going forward — never
restarted by agents.
