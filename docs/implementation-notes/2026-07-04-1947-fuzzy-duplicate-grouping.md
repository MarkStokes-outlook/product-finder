# Fuzzy duplicate grouping (identity v2) — implementation notes

**Date:** 2026-07-04 ~19:45
**Design:** docs/design/2026-07-04-fuzzy-duplicate-grouping.md (approved same
day; this note records what shipped and the two deviations)
**Tests:** 362 passing (was 332 — 23 new in tests/test_duplicates.py, 7 new in
tests/test_web.py)

## What shipped

The design was implemented as written; read it first. Summary of the moving
parts and where they live:

- `src/product_finder/duplicates.py` — new pure module (stdlib `difflib`, no
  new dependencies). Gates: normalised-title similarity ≥ 0.80 (with a
  token-Jaccard ≥ 0.5 prefilter), price delta ≤ 50% of the cheaper, and — for
  same-source pairs only — matching non-empty location OR identical image
  URL. Confidence 0–100 (identical-title base 70, price-closeness up to +15,
  same photo +20, same location +10, cross-source −10, cap 99, queue floor
  60), display/ranking only. All constants at the top of the module,
  provisional.
- `db.py` — `listing_duplicates` table (pair-based, `UNIQUE(listing_a,
  listing_b)` with a<b; a pair in any status is never re-proposed — that
  uniqueness IS the don't-ask-again memory). `scan_duplicate_candidates()`
  (called once per `runner.run_once()` cycle; live+primary listings matched
  to the same item; per-item pending cap 50, highest confidence first;
  refreshes `last_seen` on still-pending pairs), `list_duplicate_candidates()`
  (pending rows only shown while both sides are live and primary),
  `confirm_duplicate()` (sets `is_primary_sighting = 0` on the non-kept side —
  the same suppression mechanism canonical identity uses, so every existing
  read path hides it with zero changes; `kept_listing_id=None` auto-picks the
  cheaper live listing, used by bulk confirm), `dismiss_duplicate()`,
  `revert_duplicate()` (restores pending + visibility; safe because
  `resolve_identity()` re-demotes on the next cycle if canonical identity
  disagrees), `pending_duplicate_counts()`.
- `resolve_identity()` grew the designed guard: the cross-source promotion
  branch (native platform row arriving after a proxy) no longer fires for a
  listing a human confirmed as the hidden half of a duplicate pair
  (`_is_hidden_duplicate()`); the human decision wins.
- Web: "Possible duplicates" section on the project page (side-by-side pair
  cards, signal chips, "Same item — keep this one" per side with the cheaper
  suggested, "Different items" dismiss, checkbox bulk confirm/dismiss, and a
  "Decided pairs" fold-away with Undo). Routes
  `/duplicates/<id>/confirm|dismiss|revert` + `/duplicates/bulk-confirm|bulk-dismiss`,
  mirroring the suggestion routes. Dashboard project cards show a quiet
  "N possible duplicates to review" link. Bulk-confirm has a JS `confirm()`
  guard since it's the one place a sloppy click hides listings in bulk.

## Deviations from the design (both discovered on real data, both small)

1. **Display cap.** A smoke test against a scratch **copy** of the real DB
   produced 620 initial pending pairs (410 in one project) — the design's
   ~149 estimate was exact-title-only and underestimated near-identical
   titles. Storage is uncapped (beyond the designed per-item 50) but the
   project page renders only the top 30 by confidence, with the true total
   in the heading; the queue drains decision by decision.
2. **Pending order.** The design said "grouped by item"; with a display cap,
   highest-confidence-first is more useful, and each card carries its item
   label anyway. Decided pairs list newest decision first.

## Real-data verification (scratch copy, not the live DB)

- The two VIEWEDGE monitor listings — the reference case — queue at 89%
  confidence (identical title, 19.9% price delta, same location, different
  photos).
- Top of the queue is exactly the intended semantics: same-seller
  (same-location) pairs at near-identical prices, including several
  same-photo pairs at 99%.
- Rendered pages verified via test client against the scratch DB: dashboard
  note, project-page section, chips, keep buttons all present.

## Known limitations (documented in README)

- Same-source pairs need location or photo-URL equality as seller evidence —
  a relisting seller who hides/changes location slips through (accepted for
  precision; loosening later is a one-constant change).
- A confirmed pair's hidden listing stays hidden if the kept one ends while
  the hidden one is still live — Undo covers it manually.
- No perceptual image hashing, no cross-item pairs, no retroactive
  price-observation/alert cleanup on confirm (at most one extra asking-price
  observation per confirmed dupe already exists).

## Operational note

Mark's `watch` process must be restarted to start generating candidates
(generation lives in `run_once`), and `web` restarted for the new section and
routes. Both are his own terminal processes — never restarted by agents.
