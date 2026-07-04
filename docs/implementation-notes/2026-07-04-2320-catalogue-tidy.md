# Implementation notes — catalogue tidy-up (review UI, dedup, normalisation)

**Date:** 2026-07-04
**Scope:** `catalogue.py` (model placeholders), `db.py` (case-insensitive
suggestion merge, product create guard, `merge_products`/`dedupe_products`,
`list_all_pending_suggestions`, `_recompute_used_price` factored out),
`cli.py` (new `catalogue-tidy`), `web/app.py` + new `catalogue.html` +
nav in `base.html`, `tests/test_catalogue_tidy.py` (new)
**Status:** shipped, 387 tests passing (15 new). Roadmap section: "Catalogue
quality" — motivated directly by the coverage-metrics finding (eBay 7%
catalogue match rate) and the operator's request to make product creation
and duplicate removal workable.

## The problem, measured on the real DB first

- 1,613 pending suggestions across 18 items; the only review surface was
  the item *edit* form — 18 pages, mostly one click per suggestion. The
  operator had approved ~288 by hand across 6 items and stopped.
- 425 pending suggestions were brand-only (model = ''). Approving one
  creates a product whose match term is the bare brand — it would match
  every listing of that brand and price them against one reference, which
  defeats model-level pricing (the catalogue's whole purpose).
- Casing variants split corroboration: 36 case-split pending groups
  (DEWALT/DeWalt/Dewalt), and the same split existed among real products.
- 6 exact-duplicate product rows (same item + manufacturer + model) —
  `products` has no UNIQUE constraint and `create_product` didn't check.
- "Herman Miller NOT FOUND" had 45 sightings as a distinct "product"
  suggestion — extraction placeholder models were treated as real models.
- `renormalize_pending_suggestions` existed but had **no caller** anywhere.

## What shipped

1. **Global review page `/catalogue`** (nav: Catalogue). All pending
   suggestions grouped by item (busiest first), per-item select-all,
   bulk approve/dismiss (reusing the existing endpoints, which gained a
   validated relative-`next` redirect so both surfaces share them), a
   sticky action bar with live selected-count, and the auto-approve
   threshold control. The per-item view on the item form is unchanged.
2. **Brand-only guard.** Bulk approve *skips* model-less suggestions and
   says so in the flash message; individually approving one is still
   allowed as a deliberate act. They're badged "brand only" on the page.
   Deliberately NOT rejected at record time — a heavily-corroborated brand
   is still signal (and future extraction may supply the model later).
3. **Case-insensitive suggestion merging** in `record_suggestion_sighting`
   (`COLLATE NOCASE` lookup; first-seen casing adopted). BRAND_ALIASES is
   now only needed for *display* canonicalisation, not dedup.
4. **Placeholder models** ("NOT FOUND", "None", "null", "0", "various",
   "see description/title/photos/pictures", "no model") collapse to '' in
   `catalogue.normalize_model` — such sightings corroborate the brand-only
   suggestion instead of standing as fake distinct products.
5. **Product dedup.** `create_product` now returns the existing product on
   a case-insensitive (item, manufacturer, model) hit — enforcement lives
   at the single insert path because a UNIQUE migration would fail on
   databases that already contain duplicates. `merge_products` folds one
   product into another: listing_matches, price observations, new-price
   history and price candidates all change owner; match_terms union
   case-insensitively; the keeper's NULL reference prices fill from the
   duplicate; `typical_used_price`/trend recomputed over the combined
   observations (via `_recompute_used_price`, factored out of
   `record_price_observation`). `dedupe_products` sweeps all exact groups,
   oldest row wins.
6. **`catalogue-tidy` CLI** — idempotent maintenance: renormalise pending
   suggestions (replay through current rules) + dedupe products. Also the
   first caller `renormalize_pending_suggestions` has ever had.

## Real-data dry run (VACUUM INTO snapshot)

1,615 pending → 1,573 (42 case-splits merged, 0 rejected), 6 duplicate
products folded, 0 duplicate groups left. "Herman Miller NOT FOUND" ×45 +
"Herman Miller" ×12 → one brand-only suggestion ×57. `/catalogue` rendered
against the snapshot: 18 item groups, 1,573 rows, 397 brand-only badges.

## Live run: NOT done — operator action needed, in this order

`catalogue-tidy` against the live DB failed with `database is locked`
(the operator's long-running watch process holds the writer lock through
long cycles; retries over ~4 minutes never got in). This turned out to be
the right outcome anyway, because the running watch has pre-tidy code that
would re-split casings. Correct sequence for the operator:

1. Restart `watch` (picks up case-insensitive merging + placeholder rules,
   plus everything pending since 09:07: images, recalibration, framework,
   duplicate scanning, coverage recording).
2. Restart `web` (Catalogue nav page, Coverage table on Sources).
3. Run `python -m product_finder catalogue-tidy` once (idempotent; safe to
   re-run any time).

## Deliberately not done

- Fuzzy product reconciliation ("DW088K" vs "DW088K-XJ" as *near*-dupes,
  different-spelling merge proposals) — that's the roadmap's
  reconciliation-under-review objective and needs the same
  propose/confirm pattern as product_suggestions/listing_duplicates, not
  an exact-match sweep. Next natural step in this area.
- Rejecting brand-only suggestions at record time (kept as signal).
- UNIQUE constraint on products (guard at insert path instead — see above).
- Auto-dismissal of anything. Junk like "HARIBO 465137" (a real approved
  product, twice) merges to one row but stays — deleting data is the
  operator's call via the existing product delete/archive UI.
