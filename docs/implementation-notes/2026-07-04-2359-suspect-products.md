# Implementation notes — suspect products (accessory detection)

**Date:** 2026-07-04 (committed at midnight)
**Scope:** `catalogue.py` (accessory signals), `db.py`
(`find_suspect_products`), `web/app.py` + `catalogue.html` (review section,
bulk archive), `tests/test_catalogue_tidy.py` (+8)
**Status:** shipped, 395 tests passing. Follows directly from the operator's
catalogue audit ("I just approved anything with a manufacturer and model
number") and his conclusion that human approval cannot be the primary
quality gate at this volume.

## What shipped

Deterministic, explainable accessory detection from the evidence a
product's own matched listings provide — no inference, no AI:

- `catalogue.ACCESSORY_KEYWORDS` / `accessory_title_share()` — word-boundary
  keyword share across matched titles ("tipped" doesn't fire "tip").
- `catalogue.looks_like_part_number()` — bare article numbers (`2371069`),
  Kärcher part style (`2.863-314.0`). **Supporting signal only**: Wagner
  uses bare article numbers for real sprayers, so shape never accuses alone.
- `db.find_suspect_products()` — active products with ≥2 matches whose
  matched listings average <25% of the item's normal price, or where ≥50%
  of matched titles name an accessory. No evidence, no accusation:
  never-matched products (177 of 284!) are not listed.
- `/catalogue` "Suspect products" section: per-row evidence prose +
  example title, select-all, bulk archive. Reversible — archiving stops
  matching immediately (`list_products_for_matching` excludes archived)
  and existing matches un-verify on each listing's next rescan because
  `record_match`'s UPDATE refreshes `product_id`.

## Live-DB result: 28 flagged, three distinct classes

This run validated the operator's "humans get it wrong" point in an
unexpected way — the flag list itself splits into classes that need
*different* responses, visible only because evidence is shown per row:

1. **True accessory products (~10)** — Festool 204308 (dust bags), Titan
   730-401 (pump repair kit), Kärcher/Nilfisk/Makita part numbers,
   GEO-KIT laser detector, StarTech HDMI cables. Correct response: archive.
2. **Real products with accessory-polluted match streams** — Makita
   VC2000L/VC3012M (real vacuums), DEWALT DW089LG, karcher wd2, Huepar
   HM03CG. Their "100% accessory titles" are listings like "bags FOR
   Makita VC2000L": `catalogue.match()` resolves an accessory listing to
   its parent product because the model number appears in the title.
   Correct response: do NOT archive — the product is right, the *listing*
   should never have verified against it.
3. **Real but cheap/old products** — Intel SR-code CPUs, i7-4790K,
   DW088CG. Price-ratio accuses them because the item's blended
   `normal_price` spans product tiers (exactly the imprecision the
   catalogue exists to fix). Operator's call whether old tiers are wanted
   at all; not accessories.

## Strategic direction this sets (operator's explicit stance)

Human review does not scale as the primary gate — 1,613 blind suggestion
rows produced a polluted catalogue. The pattern that works is: **evidence
gates, humans arbitrate small evidence-rich lists**. Follow-ups this
points at, in rough order of value:

1. **Listing-level accessory gate** (fixes class 2 at the root): before
   `catalogue.match()` verifies a listing against a product, check the
   listing title for accessory-relationship patterns ("for <model>",
   accessory keywords alongside the match term). An accessory listing
   should not resolve to its parent product — this also directly improves
   deal accuracy and the price-observation stream. Roadmap: "Listing
   understanding".
2. **Suggestion-time evidence** so the approval queue carries the same
   signals (sighting listings' price ratio, accessory-keyword share,
   part-number shape) — bulk approve can then skip evidence-poor rows the
   way it already skips brand-only ones, and the auto-approve threshold
   becomes safe to use for evidence-rich ones.
3. Extraction quality work (roadmap) inherits the same discipline:
   classification (product vs accessory vs bundle) before catalogue entry.

## Also fixed in passing

- `db.connect()` treats any string as a path — passing a
  `file:...?mode=ro` URI silently creates a junk directory/DB (it did;
  removed). Worth a guard or URI support someday; noted, not fixed.
