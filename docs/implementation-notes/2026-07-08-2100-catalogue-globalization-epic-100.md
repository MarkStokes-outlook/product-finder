# Catalogue Globalization — EPIC-100 (ADR-0007)

**Date:** 2026-07-08 ~21:00
**Tests:** 665 passing (654 prior, all preserved with zero regressions, plus 11 net new in `test_catalogue_globalization.py`)
**Trigger:** Implementation of [[public_commercial_readiness_roadmap]]'s prerequisite epic, EPIC-100/ADR-0007 — decouple `products` from `items` so the catalogue is genuinely platform-owned/shared, ahead of the auth/ownership phases that depend on it. Scope: implementation, migration, tests, notes — explicitly not Authentik/OIDC, public homepage, ownership enforcement, or sharing/invites.

## What changed, in one paragraph

`products` is no longer scoped to a single `item_id`. A new `item_products`
join table holds each item's own tracking of a catalogue product —
`match_terms`, `target_deal_price` override, `archived`, `wanted` — while
`products` keeps only what's genuinely global: manufacturer/model identity
and market data (`msrp`, `typical_new_price`, `typical_used_price`, retailer
URL, price trend). Two items naming "the same" manufacturer/model now
converge on one global product and one shared price-observation history
instead of minting two disconnected rows.

## Design decisions made before writing code

**`catalogue.Product.id` stays the global product id; a new `item_product_id`
field carries the item_products row id.** This was the single hardest call
in the whole implementation, and I reversed it twice before settling. The
constraint that decided it: `runner.py`'s `db.record_match()` and
`db.record_price_observation()` calls need the *global* id (that's what
`listing_matches.product_id`/`product_price_observations.product_id`
actually reference), and an existing test
(`test_matched_product_shown_on_project_detail`, test_web.py) does a raw
`UPDATE listing_matches SET product_id = ?` using exactly what
`db.create_product()` returns — so `create_product()` returning anything
other than the global id would silently corrupt that FK. Once that pinned
`create_product`'s return value to the global id, `Product.id` being the
global id (not `item_product_id`) followed directly, since `catalogue.match()`'s
output feeds straight into those two runner.py calls. `item_product_id` is
the secondary field the web UI's per-item routes need.

**Web UI mutation routes (`edit`/`archive`/`delete`/`toggle-wanted`/bulk
actions) key on `item_products.id`, not the global product id — same URL
shapes, different meaning.** `/products/<int:product_id>/edit` etc. keep
their exact route strings; the integer now means "this item's tracking
row." This was necessary, not cosmetic: once a product can be tracked by
more than one item, a route keyed on the global id alone can't say *which*
item's `match_terms`/`target_deal_price` it's editing. Retailer
price-candidate routes (`price_candidates_search`/`price_candidates_dismiss`)
resolve the item_products row first, then use its `product_id` field
internally for the actual `product_price_candidates` write — those stay
correctly scoped to the shared global product.

**`db.get_product()` returns the bare global row; `db.get_item_product()`/
`get_item_product_by_id()` return the joined, item-contextualised view.**
Splitting these was forced by a genuine conflict in the existing test suite:
some tests wanted `get_product(id)` to expose `match_terms`/`wanted`/
`archived` (item-scoped), others needed the same call to return only global
market fields with no item context at all
(`test_price_history.py`'s `db._product_from_row(db.get_product(conn, product_id))`
pattern, which only ever asserts on `typical_used_price`/`price_trend_pct`).
Keeping `get_product` global-only and adding the joined variant resolved
both without inventing a third accessor.

**Deliberately did not add a second, global-level `archived` flag on
`products`**, despite ADR-0007's own text proposing one ("this isn't a real
product, delete/ignore"). Nothing in this epic's scope needed it — the
existing `merge_products`/`dedupe_products`/`delete_product` machinery
already provides a genuine "remove this from the catalogue entirely" path
for real garbage entries, and inventing a parallel global-archived concept
this pass would have been scope creep beyond what FEATURE-1000..1005 asked
for. Noted as a deliberate deviation from the ADR text, not an oversight.

**`delete_product` (global, destructive) and `delete_item_product` (this
item's tracking only) are two different functions with two different blast
radii — kept both.** The web UI's per-item "Delete" button now calls
`delete_item_product`, which removes only this item's `item_products` row
and nulls only this item's own `listing_matches` — the shared product and
its price history survive, even if this was the last item tracking it
(deliberately: destroying accumulated platform evidence because one item
stopped caring about it is exactly the fragmentation this epic exists to
undo). `delete_product` still exists, unchanged in spirit from before
(purges everywhere), for genuine catalogue garbage — not wired into any
route in this pass, kept as a `db.py`-level maintenance function alongside
`merge_products`/`dedupe_products`.

This is a real, user-visible behaviour change from before: `/products/<id>/delete`
and `/items/<id>/delete` both used to hard-delete the underlying product and
its price history. Two existing tests
(`test_product_archive_and_delete`, `test_deleting_item_also_deletes_its_products`
— test_web.py) encoded the old behaviour directly; both were updated to
assert the new, intended behaviour (tracking removed, global product and
its price history preserved) rather than left passing against a stale
assumption.

## Migration strategy

Three steps, one transaction, gated behind a cheap `PRAGMA table_info`
check (`item_id` present on `products` → not yet migrated → run; absent →
no-op), so it's safe to call on every `connect()` exactly like every other
migration in this codebase:

1. **Backfill** — one `item_products` row per existing `products` row,
   reproducing today's tracking relationship exactly (`INSERT ... WHERE id
   NOT IN (SELECT product_id FROM item_products)`, idempotent).
2. **Global dedupe** — `find_duplicate_products`/`merge_products`, already
   existing tools, re-scoped to drop `item_id` from the grouping key.
   `merge_products` was extended (not replaced) to reconcile
   `item_products`: an item that tracked only the duplicate gets repointed;
   an item that (unusually) already tracked *both* sides of a merge has its
   two `item_products` rows folded into one (union match_terms, coalesce
   overrides) — the same-item double-tracking edge case, now exercised by
   `test_migration_merges_cross_item_duplicates_and_reconciles_item_products`.
3. **Rebuild** — `CREATE products_new` (final 12-column shape, no `item_id`,
   no long-dead `normal_price`) → copy → `DROP` → `RENAME`. SQLite has no
   in-place `DROP COLUMN` for a `NOT NULL REFERENCES` column, so this is
   the first non-additive migration this codebase has done. Wrapped in
   `BEGIN IMMEDIATE` / `commit()` / `except: rollback(); raise` so a
   mid-migration failure leaves the pre-migration schema intact rather than
   a half-rebuilt table.

Dry-run proof, per the brief's instruction 6: `test_migration_against_real_backup_loses_no_data`
and `test_migration_is_idempotent_on_real_backup` run this exact migration
against a **copy** of `data/product_finder.db.bak.20260705T200501` — a real,
previously-accumulated backup (328 products, 18 items, 5666 listing
matches, 122 price observations), not a synthetic fixture — and assert
zero data loss, zero orphaned foreign keys, and that every pre-migration
`(item, product)` tracking relationship (match_terms, target_deal_price,
archived, wanted) survives exactly. That backup happens to contain no
real cross-item duplicates, so `test_migration_merges_cross_item_duplicates_and_reconciles_item_products`
uses a small synthetic fixture (`_make_pre_globalization_db`) built to the
exact pre-migration schema specifically to exercise the merge path the
real backup doesn't.

## Data integrity incident during this session — full account

**The migration ran against the real `data/product_finder.db` mid-session,
without the explicit dry-run-then-confirm gate this task asked for.**
Discovered when I queried the real database (for context while designing
the test fixtures) and found it already had `item_products` and no
`products.item_id`, while every dated backup still showed the old schema.

Root cause, confirmed with Mark: he had a `watch` process running in the
background while I was editing `db.py`. `watch` opens a fresh connection
roughly every 20 seconds; the package is installed editable
(`pip install -e .`), pointing straight at the source file being edited —
so the running process picked up my in-progress migration code on its next
reconnect and ran it, entirely outside my visibility (no
`product-finder`/`watch` process was visible to `ps aux`/`lsof` by the time
I checked, since Mark noticed and stopped it before I did). I did not run
this migration myself against the real path at any point — verified by
reading every test file for any non-`tmp_path` database access before
concluding the test suite wasn't the cause.

**Verified integrity directly against the live file before proceeding:**
row counts coherent (232 products → 232 `item_products`, 7020
`listing_matches`, 8692 `listings`, 192 price observations), zero orphaned
`listing_matches`/`item_products` foreign keys, sample `item_products` rows
carrying correct `target_deal_price`/`archived`/`wanted` per item. No data
loss. The transactional design meant the worst case, had it failed
partway, was a clean rollback to the pre-migration schema — not
corruption.

**One real bug this exposed, now fixed:** at the moment the live migration
ran, `_MIGRATIONS`' `("products", "wanted", ...)` entry was still
unconditional. The rebuild's `CREATE TABLE products_new` correctly excludes
`wanted` (it belongs on `item_products` now) — but on the *next* reconnect
after the rebuild, the unconditional migrations loop saw `wanted` missing
from the now-rebuilt `products` table and added it straight back via
`ALTER TABLE`. Caught by a new test
(`test_products_table_has_no_item_scoped_columns`) against a *fresh*
database, not the live one — fixed by skipping that specific migration
entry once `item_id` is no longer present on `products` (see `connect()`).
**The live `data/product_finder.db` still carries this leftover `wanted`
column on `products` as a result** — confirmed harmless (nothing in the
fixed codebase reads `products.wanted` any more; `item_products.wanted` is
authoritative) but not yet tidied away. Flagged as deferred cleanup below
rather than fixed by touching the live file again unprompted, given what
already happened once this session.

## Risks and open questions carried forward (from ADR-0007, still open)

- **Who can edit shared global product fields** (`manufacturer`/`model`/
  `msrp`/`typical_new_price` via `/products/<id>/edit`) is unchanged from
  before this epic — any operator can edit any item's product form, which
  now silently affects every other item tracking that product. Not solved
  here; intersects with the ownership work in EPIC-103.
- **No audit trail on global merges.** `merge_products` still deletes the
  duplicate row outright. Blast radius is now platform-wide instead of
  per-item, which raises the stakes of a wrong merge; still flagged, not
  built.
- **`product_suggestions` approval already goes through the global
  find-or-create path** (via `create_product`, unchanged call site in
  `approve_suggestion`) — confirmed by
  `test_approve_with_corrected_model_keeps_article_number_as_alias`'s
  "later sighting converges on the same product" case, so this specific
  open item from ADR-0007 is resolved by construction, not left open.

## Deferred cleanup

- **Live `data/product_finder.db.products.wanted` leftover column** — harmless,
  unused, safe to drop whenever convenient (`ALTER TABLE products DROP
  COLUMN wanted` on SQLite ≥ 3.35, or just leave it; nothing reads it).
  Deliberately not touched by me this session.
- **Real backups predate this epic** (`*.bak-20260703*`, `*.bak.20260705*`)
  — left untouched, as they should be; they're now useful fixtures (see
  `_REAL_BACKUP` in the new test file) as well as recovery points.
- Not done, and correctly out of scope per the brief: Authentik/OIDC,
  public homepage, ownership enforcement (EPIC-103), sharing/invites
  (EPIC-105). ADR-0006's clone-by-reference behaviour (Phase 5) can now be
  built for real — the data-model property it depends on is proven by
  `test_second_project_referencing_same_product_shares_it_not_duplicates` —
  but the actual cloning feature itself was not started.
