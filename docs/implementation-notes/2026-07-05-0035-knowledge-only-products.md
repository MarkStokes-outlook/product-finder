# Implementation notes — knowledge-only products (`products.wanted`)

**Date:** 2026-07-05
**Scope:** `db.py` (migration, `_WANTED` predicate on four read paths,
`set_product_wanted`), `catalogue.py` (`Product.wanted`), `runner.py`
(alert gate), `web/app.py` (toggle + bulk routes), `catalogue.html` +
`item_form.html`, `tests/test_catalogue_tidy.py` (+6)
**Status:** shipped, 401 tests passing. Direct response to the operator:
"I want older CPUs and products in the catalogue, but I don't want them
matched against that project."

## The concept: identification ≠ endorsement

The suspect-products run exposed a class of catalogue entries that are
*real products the item just isn't after* (old Intel/AMD CPU generations
under a current-gen "CPU" item). Archiving them would be wrong twice over:

- it stops `catalogue.match()` recognising their listings, so those cheap
  listings fall back to the item's blended price → implausible-price flags
  or fake deals, plus renewed product-suggestion churn;
- it throws away pricing knowledge the roadmap counts as a compounding
  asset.

So a third state between "wanted product" and "archived":
**knowledge-only** (`wanted = 0`). Still matched, still priced, never
surfaced.

## Mechanics

- Migration `("products", "wanted", "INTEGER NOT NULL DEFAULT 1")`.
- Matching unchanged: `list_products_for_matching` still includes
  knowledge-only products; `catalogue.match()` doesn't consult `wanted`.
  Price observations keep accumulating in `runner.run_once`.
- Surfacing gated in exactly two places:
  1. **Runner**: no `MatchAlert` when the resolved product has
     `wanted = False` (the match row itself is still recorded, with
     `product_id` intact — full provenance).
  2. **Read paths**: shared predicate `_WANTED = "(m.product_id IS NULL
     OR pr.wanted = 1)"` applied to `query_matches`,
     `project_top_picks`, `dashboard_stats`, `project_summaries`.
     Read-time gating means toggling is instant and fully reversible —
     no rescan, no data change.
- `find_suspect_products` excludes `wanted = 0` (verdict delivered, stop
  accusing).

## UI

- Suspect-products section now has two verdicts matching the two real
  classes: **Archive selected** (true accessories — Festool bags, Titan
  repair kits) and **Keep as knowledge-only** (real-but-unwanted — old
  CPUs, cheap laser tiers).
- Item form: "knowledge only" badge + per-product toggle
  ("Knowledge only" / "Surface deals").

## Deliberately not done

- Class 2 from the suspect analysis (accessory *listings* matching parent
  products — "bags for Makita VC2000L") is NOT solved by this flag and
  must not be "fixed" by marking those products knowledge-only: the
  products are wanted, the listings are wrong. That's the listing-level
  accessory gate, still the top catalogue follow-up.
- No per-project/per-item wanted matrix — products are per-item already,
  so a single boolean covers today's shape. If products ever become
  item-independent (roadmap, "Product knowledge beyond price"), wanted
  becomes a property of the item↔product link, not the product.

## Live state

Migration applied to the operator's DB (284/284 wanted=1, dashboards
byte-identical until something is toggled). The 28 suspects await his
verdicts on /catalogue; watch/web still stopped at his request.
