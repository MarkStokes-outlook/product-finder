# ADR-0007: Catalogue globalization — decouple products from items

**Status:** Proposed
**Date:** 2026-07-08
**Related backlog:** EPIC-100 (FEATURE-1000..1005)
**Related ADRs:** ADR-0004 (revises its "Known schema gap" into tracked work), ADR-0006 (forward-looking note)
**Sequencing:** planning only, no implementation. Recommended to land **before** EPIC-103 (Phase 3, user-owned data); **hard blocking dependency** before EPIC-104 (Phase 4, public rollout) and EPIC-105 (Phase 5, cloning). See ADR-0001 (revised).

## Context

ADR-0004 flagged, but explicitly did not fix, a real pre-existing schema
mismatch: `listings` is already global (no FK to `items`/`projects`), but
`products` is not — verified directly against `src/product_finder/db.py`'s
`_SCHEMA` and `_MIGRATIONS`:

```sql
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),   -- <-- the problem
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    match_terms TEXT NOT NULL DEFAULT '[]',
    normal_price REAL,              -- legacy, superseded by typical_new_price
    target_deal_price REAL,
    archived INTEGER NOT NULL DEFAULT 0,
    msrp REAL,
    typical_new_price REAL,
    typical_used_price REAL,
    canonical_price_url TEXT,
    price_search_checked INTEGER NOT NULL DEFAULT 0,
    last_price_check_at TEXT,
    last_price_check_ok INTEGER,
    price_trend_pct REAL,
    price_trend_confidence REAL NOT NULL DEFAULT 0,
    wanted INTEGER NOT NULL DEFAULT 1
);
```

`product_price_observations`, `product_new_price_history`, and
`product_price_candidates` all key off `product_id`, so they inherit the
same item-scoping transitively. `listing_matches.product_id` and
`product_suggestions.item_id` complete the picture: a listing resolves to a
product, but that product only ever belonged to one item.

**Concretely, two projects independently tracking "Makita SP6000" today get
two unrelated `products` rows, two unrelated `typical_used_price` medians,
and no shared benefit from each other's observed listings** — the exact
opposite of "a listing/product is stored once and surfaced to many."

This is pre-existing debt (`docs/architecture-briefing.md` already names "no
cross-item or cross-source deduplication" as architectural debt), not
something introduced by the phased public-readiness roadmap. It matters now
because that roadmap is about to make "many distinct projects/users tracking
overlapping products" the normal case rather than a single operator's
private quirk.

**What already exists and doesn't need reinvention:**

- `catalogue.model_key()` — a casing/spacing/punctuation-insensitive
  identity key, already used for duplicate detection (`db.find_duplicate_products`,
  scoped by `(item_id, manufacturer_key, model_key)`).
- `db.merge_products(conn, keep_id, dup_id)` — already re-points
  `listing_matches`, `product_price_observations`, `product_new_price_history`,
  `product_price_candidates` onto the kept product, unions `match_terms`,
  coalesces reference prices, recomputes `typical_used_price`, deletes the
  duplicate. This is most of the mechanism a global merge needs — it is
  currently only ever invoked within one item's duplicate group.
- `db.record_price_observation()` / `_recompute_used_price()` — already
  pure `product_id`-keyed functions with no `item_id` anywhere in them.
  **These need zero code changes to become correct once `product_id` refers
  to a shared product.**
- `runner.py` — the only place matching candidates are sourced is
  `db.list_products_for_matching(conn, item_id)`, feeding `catalogue.match(text,
  products)`. `catalogue.match()` itself is item-agnostic — it just scores
  whatever `Product` list it's handed. **Only the query inside
  `list_products_for_matching` needs to change**, not the matching logic.

This is a smaller, more contained change than it first looks, because the
codebase already separates "what is a product" (data) from "how do I match
one" (pure function over a list) — the only real work is where that list
comes from and how the underlying row is keyed.

## Proposed schema direction

**Split what's currently one `products` row into two concerns:**

1. **`products` (global, platform-owned)** — loses `item_id` entirely.
   Keeps: `manufacturer`, `model`, `msrp`, `typical_new_price`,
   `typical_used_price`, `canonical_price_url`, `price_search_checked`,
   `last_price_check_at`, `last_price_check_ok`, `price_trend_pct`,
   `price_trend_confidence`, `archived` (global "this isn't a real
   product, delete/ignore" flag — distinct from an item's local
   `wanted`/tracking decision). These are all **market facts** — true
   regardless of who's watching for the product.

2. **`item_products` (new, project-scoped join table)** — the "this item
   tracks this catalogue product, and here's my local context" row:
   `id`, `item_id NOT NULL REFERENCES items(id)`, `product_id NOT NULL
   REFERENCES products(id)`, `match_terms` (this item's own recognition
   terms — kept local because two items may legitimately want different
   match breadth for the same product), `target_deal_price` (this item's
   override of when this specific product counts as a deal for it —
   distinct from the item's own blanket `target_deal_price`), `archived`
   (this item stopped tracking it — does not affect other items), `wanted`
   (this item doesn't want deals surfaced for it, but still wants
   identification/history — does not affect other items), `UNIQUE(item_id,
   product_id)`.

`listing_matches.product_id` keeps pointing at `products.id` — unchanged FK
target, now global. `product_price_observations`, `product_new_price_history`,
`product_price_candidates` keep pointing at `product_id` — unchanged FK
target, now global, and now genuinely aggregate evidence across every
project tracking that product.

**The dividing line matches the target ownership boundary exactly:**
identity + market price = platform; recognition terms + personal deal
threshold + tracking decision = project/item.

## Migration strategy for existing item-scoped products

This repository has a real, currently-used database
(`data/product_finder.db`) with real accumulated products and price
history — this is not a greenfield migration, and it must be validated
against a copy of that real data, not only synthetic fixtures, before it is
considered safe.

1. **Create `item_products`** via the existing additive `_MIGRATIONS`
   pattern in `db.py` — purely additive, no risk to existing tables.
2. **Backfill, one row per existing product:** for every current `products`
   row, insert one `item_products` row carrying that row's `item_id`,
   `match_terms`, `target_deal_price`, `archived`, `wanted` — this exactly
   preserves today's behaviour (every product still tracked by exactly the
   one item that created it) with zero data loss and zero behaviour change
   at this checkpoint. This step alone is safe to ship and verify before
   the next step.
3. **Global dedupe pass:** run a globalized version of
   `find_duplicate_products`/`merge_products` that groups by
   `(model_key(manufacturer), model_key(model))` **without** `item_id`, and
   merges cross-item duplicates. `merge_products` must be extended (not
   replaced) to also reconcile `item_products`: when merging `dup_id` into
   `keep_id`, any `item_products` row pointing at `dup_id` is repointed to
   `keep_id`; if the *same* item already tracks both (an item that had
   independently created what turns out to be the same product twice — the
   in-item duplicate case the existing tool already handles), the two
   `item_products` rows for that item are themselves merged (union
   `match_terms`, `COALESCE` the override fields) rather than left as two
   rows referencing one product.
4. **Retire `products.item_id`.** SQLite cannot drop a `NOT NULL REFERENCES`
   column in place — this requires a table rebuild
   (`CREATE products_new ... ; INSERT ... SELECT ... ; DROP products ;
   ALTER TABLE products_new RENAME TO products;`), the **first genuinely
   non-additive migration this codebase will have done** (see Risks). Two
   options, to be decided explicitly rather than defaulted into:
   - **(A) Rebuild now** — clean end state, `products` has no `item_id` at
     all afterward. Higher one-time risk, no lingering confusion later.
   - **(B) Deprecate in place** — leave the column, stop reading/writing it,
     document it as dead. Zero rebuild risk, but a permanently misleading
     `NOT NULL REFERENCES items(id)` sits in the schema forever, which cuts
     against "small, maintainable codebase."
   **Recommendation: (A), but only behind a mandatory backup + dry-run
   against a copy of the real database + explicit transaction, given it's
   the highest-risk step here.** This decision should be confirmed, not
   assumed, before FEATURE-1003 starts.

## Dedupe/identity strategy for manufacturer/model catalogue entries

No new identity scheme is proposed — **reuse `catalogue.model_key()`
exactly as it works today**, just with `item_id` dropped from the grouping
key everywhere it currently appears (`find_duplicate_products`, the
existing-product lookup inside `create_product`). Manufacturer+model,
casing/spacing/punctuation-insensitive, is already trusted for this purpose
within one item; extending its scope to the whole catalogue doesn't change
its precision characteristics, only its blast radius if it's ever wrong
(see Risks).

## How item-specific overrides should work

- **Reads:** an item's effective product view is
  `item_products JOIN products ON item_products.product_id = products.id
  WHERE item_products.item_id = ?` — the item sees the global market facts
  (`msrp`/`typical_new_price`/`typical_used_price`/trend) plus its own
  `match_terms`/`target_deal_price`/`archived`/`wanted` layered on top.
- **Precedence:** `item_products.target_deal_price`, when set, overrides
  scoring's use of the product for that item; when NULL, scoring falls back
  exactly as it does today for a product with no override (ultimately to
  the item's own blanket `target_deal_price`/`normal_price`).
- **Never overridable per-item:** `manufacturer`, `model`, `msrp`,
  `typical_new_price`, `typical_used_price`, `canonical_price_url`, trend
  fields — these describe the product itself, not one item's opinion of it.
- **New tracking of an existing global product:** creating a "new" product
  from an item first looks up the global identity key; if found, it creates
  (or reuses) an `item_products` row against the existing global product
  instead of inserting a second `products` row — this replaces
  `create_product`'s current item-scoped lookup with a global one, same
  shape, wider scope.

## How price history should be treated

`product_price_observations`/`product_new_price_history`/`product_price_candidates`
become genuinely global: every project's matched listings for a given
product contribute to the same `typical_used_price` median and the same
trend calculation. This is a direct quality improvement, not just a
side-effect of tidying ownership — more observations per product means a
more reliable median sooner, which is exactly the "accumulated pricing
knowledge" asset `docs/strategy/roadmap.md` names as one of the system's
core long-term assets. No changes are needed to `record_price_observation`,
`_recompute_used_price`, or `price_trend.py` — they are already correctly
`product_id`-scoped with no `item_id` dependency; only the *volume and
diversity* of what feeds them changes.

## Risks and open questions

- **First non-additive migration in the codebase.** Every migration to date
  is an additive column via `_MIGRATIONS`; retiring `products.item_id`
  (Option A above) requires a table rebuild. This must be its own carefully
  reviewed, transactional, backed-up step — not casually bundled with the
  additive `item_products` creation.
- **Merge blast radius increases.** Today a wrong per-item merge affects one
  item. A wrong global merge affects every project tracking that product.
  `merge_products` currently **deletes** the duplicate row outright with no
  soft-delete/undo. Recommend an audit log (or at minimum a "recently
  merged" record with enough detail to manually reconstruct) for global
  merges specifically, given the larger blast radius — flagged as an open
  design question, not resolved here.
- **`product_suggestions` approval path.** Suggestions stay item-scoped
  (legitimately item-context-driven discovery), but *approving* one must go
  through the same global find-or-create path as manual product creation —
  otherwise two items independently suggesting the same real product from
  their own eBay structured data would still create two global products.
  This is in scope for FEATURE-1004, called out explicitly so it isn't missed.
- **Who can edit shared global product fields?** Today any operator can
  edit any product inline. Once a product is genuinely shared across
  projects/users, editing its `msrp`/`typical_new_price`/`canonical_price_url`
  affects everyone tracking it. This phase does not propose a moderation or
  versioning model for that — it's an open question for whoever picks this
  up, likely intersecting with Phase 3's authorization work (editing a
  *global* row isn't naturally covered by "project ownership"). Left
  unresolved here rather than guessed at.
- **Identity key collisions remain a low, accepted risk** — two genuinely
  different products sharing the same manufacturer+model spelling — same
  risk profile the current per-item dedupe already accepts, just wider
  scope. Not a new risk class introduced by this change.
- **Interacts with ADR-0006 (Phase 5 cloning).** Once this lands, a project
  clone should *reference* the shared product rather than deep-copy it (see
  ADR-0006's forward-looking note, now pointing at this ADR by number).

## Why this should be separate from the auth/ownership roadmap

- **It is a data-model/catalogue-quality fix, not an authorization fix.**
  ADR-0004 already establishes correct authorization behaviour (product
  reads unrestricted by project ownership) independent of whether the
  underlying schema has been globalized yet — this work improves data
  quality and shared value, it does not fix a security bug.
- **It carries its own, different risk profile** — a first-of-its-kind
  non-additive schema migration against a real production database — which
  should not be compounded with Phase 3's own highest-risk work
  (cross-user authorization correctness). Bundling two independently risky
  migrations into one release is exactly the kind of big-bang change
  ADR-0001 rules out.
- **It gets cheaper the earlier it happens.** Every item-scoped product
  created between now and whenever this ships is more data the global
  dedupe pass has to reconcile. Landing it before Phase 3 (before real
  distinct users start accumulating their own item-scoped catalogue entries
  at volume) is strictly lower-risk than landing it after.
- **It simplifies, rather than blocks, the phases after it.** Phase 3
  no longer needs ADR-0004's "known gap" carve-out once this ships — the
  schema matches the authorization model instead of needing an explanatory
  footnote. Phase 4 (public browsing) and Phase 5 (cloning) both become
  more correct by construction (shared catalogue, clone-by-reference)
  instead of needing to work around per-item fragmentation.

## Acceptance criteria

- `item_products` exists and correctly represents every pre-existing
  `products` row's original tracking relationship after the backfill step,
  with zero data loss (verified by row-count and spot-check comparison
  against a real database copy).
- No two `products` rows exist with the same `(model_key(manufacturer),
  model_key(model))` after the global dedupe pass, except where a human has
  explicitly declined to merge (if such an override is built — otherwise,
  none at all).
- `listing_matches`, `product_price_observations`, `product_new_price_history`,
  and `product_price_candidates` for every pre-migration duplicate group
  are correctly consolidated onto the surviving global product, with row
  counts before/after reconciling exactly (merged, not duplicated or lost).
- An item's `target_deal_price` override, `match_terms`, `archived`, and
  `wanted` state are unchanged in effect from the item's perspective before
  and after migration — the *outcome* (what this item matches, what it
  considers a deal) does not change, only where the data lives.
- Two different items/projects tracking the same real-world product resolve
  to the same global `products.id` post-migration.
- `catalogue.match()` requires no code changes — only
  `list_products_for_matching` and the product create/update/merge paths in
  `db.py` change.
- The full existing test suite continues to pass with no behavioural
  regression for a single-item/single-project install.

## Tests needed

- **Migration integrity:** run the full migration against a sanitised copy
  of the real `data/product_finder.db`, assert no data loss (row-count
  reconciliation across all affected tables) and assert scoring/matching
  output for a fixed set of known listings is unchanged before/after.
- **Global dedupe correctness:** two items independently create "the same"
  product (by identity key); after the dedupe pass, exactly one `products`
  row exists, both items have their own `item_products` row pointing at it,
  and each item's own `match_terms`/`target_deal_price` are preserved
  independently.
- **Same-item double-tracking edge case:** one item that already had two
  in-item duplicate products (the case `dedupe_products` already handles
  today) migrates to exactly one `item_products` row for that item, not two.
- **Price history aggregation:** two projects' matched listings for the
  same global product both contribute to one shared `typical_used_price`
  median — verified with a synthetic fixture where each project alone would
  compute a different median than the combined set.
- **Item override precedence:** an item with a `target_deal_price` override
  on `item_products` scores differently from an item with no override
  tracking the same product, using the same global `typical_new_price`.
- **Suggestion approval reuses global identity:** approving a suggestion for
  a manufacturer/model that already exists globally attaches an
  `item_products` row to the existing product rather than creating a new one.
- **Regression:** the existing catalogue/scoring/matching/web test suites
  (`test_catalogue.py`, `test_scoring.py`, `test_web.py`, etc.) pass
  unmodified in outcome for a single-project database.
