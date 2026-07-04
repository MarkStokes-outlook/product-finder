# Design: fuzzy cross-marketplace duplicate grouping (identity v2)

Status: **approved by Mark 2026-07-04 (all three open questions per
recommendation: bulk-confirm included, location gate accepted, condition
mismatch display-only) and implemented the same day — see
docs/implementation-notes/ for what shipped and where it deviated.**
Scope agreed 2026-07-04: candidate grouping on title/price/image similarity,
surfaced for human confirm/dismiss with a "don't ask again" remember workflow,
mirroring `product_suggestions`' pending/approved/dismissed pattern. Never
auto-merge.

## What the real data says (read-only analysis of Mark's DB, 2026-07-04)

Three findings that shaped this design:

1. **The reference duplicate pair is same-marketplace, not cross-marketplace.**
   The two VIEWEDGE C2712FDA-P monitor listings are *both eBay*, with different
   item IDs (178278167468 @ £83.89 "Opened – never used" vs 178276106111 @
   £69.99 "New"), identical titles, and the same masked seller location
   (`EN6***`). Canonical-URL v1 correctly treats them as two records — they
   are two records. Fuzzy grouping must therefore handle same-source duplicate
   listings as a first-class case, not only the eBay-vs-Gumtree cross-post the
   roadmap describes.

2. **Identical title does not mean duplicate.** Among live, primary listings
   there are 716 exact-title pairs — but most are *different sellers selling
   the same product model*, which are genuinely distinct purchasable items
   (that's a catalogue/product-grouping concern, not identity). The only
   seller proxy stored today is `listings.location` (eBay's masked postcode).
   Gating same-source pairs on matching non-empty location cuts 716 → 167
   pairs (149 within a 50% price band) — a reviewable initial queue with a
   much better precision story. The VIEWEDGE pair passes the gate.

3. **Image similarity is weak in v1.** The VIEWEDGE pair has *different*
   image URLs (separate uploads), so image-URL equality misses the flagship
   case. It stays as a confidence boost only (it catches cross-posts reusing
   the same CDN URL); perceptual hashing is explicitly out of scope.

## Conceptual model: a second layer, not an extension of canonical identity

- **Canonical identity (v1, shipped)** = "these rows are the same platform
  record" — provable from a shared native ID, safe to link automatically.
- **Duplicate pair (this work)** = "these two records are probably the same
  physical item" — probabilistic, always human-decided.

These stay in separate tables. `listing_identities` is keyed on a unique
`canonical_key`, so two distinct eBay listings can never share one; its
primary-promotion rule (native platform beats proxy) has no analogue here.
What the two layers *share* is the suppression mechanism:
`listings.is_primary_sighting`, which every read surface (`query_matches`,
`project_top_picks`, `project_summaries`, `dashboard_stats`, alert gating,
price-observation gating) already honours — confirming a duplicate needs zero
changes to any read path.

## Schema

One new table (added to `_SCHEMA`; no migrations needed):

```sql
CREATE TABLE IF NOT EXISTS listing_duplicates (
    id INTEGER PRIMARY KEY,
    listing_a INTEGER NOT NULL REFERENCES listings(id),
    listing_b INTEGER NOT NULL REFERENCES listings(id),  -- listing_a < listing_b, always
    item_id INTEGER NOT NULL REFERENCES items(id),       -- match scope it was detected in
    confidence REAL NOT NULL,                             -- 0-100, display/ranking only
    signals TEXT NOT NULL DEFAULT '{}',                   -- JSON: title_sim, price_delta_pct,
                                                          --       same_location, same_image
    status TEXT NOT NULL DEFAULT 'pending',               -- pending | confirmed | dismissed
    kept_listing_id INTEGER REFERENCES listings(id),      -- set on confirm
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(listing_a, listing_b)
);
```

`UNIQUE(listing_a, listing_b)` (IDs normalised low/high) plus the rule
"a pair in any status is never re-proposed" **is** the don't-ask-again memory —
the same discipline as `record_suggestion_sighting` ignoring already-decided
suggestions: a dismissal is a deliberate "no" that more sightings don't
override.

## Candidate generation

New pure module `src/product_finder/duplicates.py` (same style as
`catalogue.py`/`price_trend.py`: no sqlite knowledge, all thresholds as named
constants at the top). Called once per `runner.run_once()` cycle, after the
match loop, alongside `retailer_price.run_discovery_and_refresh`. Stdlib only
(`difflib.SequenceMatcher`) — no new dependencies.

**Population**: live (`_NOT_ENDED`), primary (`is_primary_sighting = 1`)
listings matched to the *same item* (pairs across different items are out of
scope — item scope both bounds the O(n²) comparison and matches where
double-counting actually hurts: same item, two alerts, two dashboard rows).

**Gates** (all must pass before a pair is even scored):

1. Normalised title similarity ≥ `TITLE_SIM_MIN = 0.80`.
   Normalise = lowercase, strip punctuation, collapse whitespace. A cheap
   token-set Jaccard ≥ 0.5 prefilter runs before `SequenceMatcher` so the
   quadratic pass stays fast at this scale (~4.7k listings, low hundreds per
   item worst case).
2. Price delta ≤ `PRICE_DELTA_MAX_PCT = 50` (of the cheaper price). Beyond
   that, similar titles usually mean different variants/capacities.
3. **Same-source pairs only**: same non-empty `location` OR identical
   `image_url`. Without any seller evidence, same-title-same-source is
   overwhelmingly "different sellers, same model" — queueing those would
   flood review with false candidates and erode trust in the queue.
   Cross-source pairs skip this gate (location formats differ across
   marketplaces) but start from a lower base confidence.

**Confidence** (0–100, capped 99; never triggers any automatic action):

- title similarity 0.80 → 1.00 maps linearly to base 45 → 70
- price closeness: +15 at 0% delta, linear to +0 at 50%
- identical `image_url`: +20
- same non-empty `location`: +10
- queue the pair only if ≥ `MIN_QUEUE_CONFIDENCE = 60`

Worked example — VIEWEDGE pair: title 1.00 → 70; delta 19.9% → +9; same
location → +10 = **89, queued**.

`MAX_PENDING_PER_ITEM = 50` as a safety valve (highest confidence first), so
one noisy item can't flood the queue. Pending pairs seen again get
`last_seen` refreshed; decided pairs are skipped outright.

All constants are provisional and expected to be tuned against the real queue
after the first cycle, same as the scoring recalibration constants.

## Review workflow

Mirrors `product_suggestions`, with one deliberate addition (revert).

- **Where**: a "Possible duplicates" section on the **project detail page**,
  grouped by item (candidates are item-scoped, and the project page is where
  the duplicate rows visibly annoy). The dashboard gets one quiet line/link
  when pending > 0 ("14 possible duplicates to review") — calm-UI compliant.
- **Presentation**: side-by-side pair cards — thumbnail, title, price, source,
  condition, location, first seen — plus confidence and signal chips
  ("identical title", "prices 20% apart", "same location", "same photo").
  Condition mismatch (VIEWEDGE: "New" vs "Opened – never used") is displayed
  but carries no confidence weight in v1 — shown to inform the human, not
  guessed at.
- **Confirm** = click "Keep this one" on one card (the cheaper live listing is
  visually suggested as the default). Sets `status='confirmed'`,
  `kept_listing_id`, and `is_primary_sighting = 0` on the other listing.
  Every existing surface hides it from that moment.
- **Dismiss** = "Different items". `status='dismissed'`, remembered forever.
- **Revert** (goes beyond the suggestions pattern, deliberately): a wrong
  merge hides a real deal, so decided pairs live in a collapsed foldaway with
  an Undo that restores `pending` and sets `is_primary_sighting = 1` back.
  Safe: canonical identity resolution re-demotes on the next cycle if it
  disagrees, and nothing was ever deleted.
- **Bulk**: checkbox bulk-dismiss, mirroring suggestions. Bulk-confirm keeps
  the cheaper live listing of each selected pair — proposed because the
  initial backlog is ~150 pairs, but flagged as an open question (it's the
  one place a sloppy click can hide listings in bulk).

New db functions: `record_duplicate_candidate`, `list_duplicate_candidates`
(project-scoped join with both listings' display fields),
`confirm_duplicate`, `dismiss_duplicate`, `revert_duplicate`,
`pending_duplicate_count`. New routes:
`/duplicates/<id>/confirm|dismiss|revert` + bulk, following the suggestion
routes' shape.

## Interaction edges (designed in, not discovered later)

1. **`resolve_identity` promotion guard**: the cross-source promotion branch
   sets `is_primary_sighting = 1` and could resurrect the hidden half of a
   confirmed pair. Add one `NOT EXISTS` guard (listing is the non-kept side
   of a confirmed `listing_duplicates` row). Verified the rest of
   `resolve_identity` never rewrites the flag on routine rescans, so a fuzzy
   demotion is otherwise stable.
2. **Price observations are not retracted** on confirm — both listings were
   primary at first sighting, so at most one extra asking-price observation
   per confirmed duplicate already exists. Accepted; documented.
3. **Already-sent alerts are not retracted**; future ones stop naturally via
   the existing `is_primary` gate.
4. **Known limitation**: if the kept listing ends, the hidden one stays
   hidden even if still live — the opportunity disappears from view although
   a buyable listing exists. Accepted for v1 (Revert covers it manually);
   goes in README Known Limitations.
5. **Transitivity**: pairs, not groups. Once A–B is confirmed (B hidden), B is
   no longer primary so B–C is never generated; C pairs against A. Groups
   emerge as stars around the kept listing — no group data model needed.

## Explicitly not in v1

- Perceptual image hashing (image-URL equality boost only).
- Cross-item candidate pairs.
- Auto-merge at any confidence — pending forever until a human decides.
- Seller-identity capture (blocked on the connector seller-identity
  capability field, which has no reader yet — parked in the roadmap).
- Resurfacing the hidden member when the kept listing ends.
- Retroactive price-observation cleanup on confirm.

## Test plan (~25–30 new tests)

- `test_duplicates.py`: normalisation, similarity gates, price-band edges,
  same-source location gate, cross-source path, confidence blend (VIEWEDGE
  values as a fixture), per-item cap.
- db: pair ordering + uniqueness, pending→confirmed sets/clears flags,
  dismissed pairs never re-proposed, revert restores, counts.
- `resolve_identity` promotion-guard regression.
- Runner wiring: generation runs per cycle, scoped to live/primary/same-item.
- Web: section rendering, signal chips, confirm/dismiss/revert routes, bulk,
  pending badge, decided foldaway.

## Deployment note

After shipping: Mark's `watch` process needs a restart (generation lives in
`run_once`) and `web` needs a restart (new section/routes). Tell Mark — never
restart his processes.

## Open questions for Mark

1. **Bulk-confirm** ("keep cheaper of each") — include, or one-by-one only?
   Recommendation: include, given the ~150-pair initial backlog.
2. **Location gate strictness** for same-source pairs: it will miss a
   same-seller relist with location hidden or changed. Recommendation: accept
   for precision; it's a one-constant loosening later if real dupes slip
   through.
3. **Condition mismatch** stays display-only (no confidence weight) in v1 —
   agreed?
