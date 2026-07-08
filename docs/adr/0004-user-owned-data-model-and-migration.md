# ADR-0004: User-owned data model and migration

**Status:** Proposed (revised 2026-07-08 — see Revision note)
**Date:** 2026-07-08
**Phase:** 3 of 5 (see ADR-0001)
**Related backlog:** EPIC-103 (FEATURE-1030..1034)

> **Current-state note:** This ADR is preserved as historical planning for
> user-owned data. Its catalogue schema gap was later resolved by EPIC-100 /
> ADR-0007. Use `docs/platform-domain-model.md` and `ARCHITECTURE.md` for the
> current ownership and schema model.

## Revision note (2026-07-08)

The original version of this ADR described `products`, `product_price_observations`,
and other catalogue tables as owned "transitively" alongside `items` — i.e.
implicitly scoped to whichever project's item referenced them. That was
wrong against the intended product boundary: **the catalogue (products,
listings, price history) is platform-owned/shared data, not per-user data.**
Only projects, items (watched-item/search intent), candidate matches, and
user decisions/feedback are user/project-owned. This revision corrects the
ownership boundary and flags a real, pre-existing schema mismatch it
uncovered (see "Known schema gap" below). Nothing has shipped yet — this is
a planning correction, not a rollback of built code.

## Context

`src/product_finder/db.py` owns all schema via a single `_SCHEMA` script plus
an ordered, additive `_MIGRATIONS` list applied idempotently on every
`connect()` — there is no separate migration tool or version table
(`docs/architecture-briefing.md`, Storage). Phase 2 (ADR-0003) introduced a
`users` table but nothing reads or restricts on it yet.

The actual current schema (verified against `db.py`'s `_SCHEMA`, not just
the architecture briefing prose) is:

- `listings` — **no FK to items or projects at all.** Global, deduplicated
  by `UNIQUE(source, external_id)`. Already structurally shared today.
- `listing_matches` — join table: `listing_id` + `item_id`,
  `UNIQUE(listing_id, item_id)`. One listing can have many match rows, one
  per item that matches it. This is the "candidate match between watched
  item and listing" the target model describes, and it is already correctly
  item-scoped (→ project-scoped) today.
- `alerts_sent` — FK to `listing_matches.match_id`, so transitively item/project-scoped.
- `products`, `product_price_observations`, `product_new_price_history`,
  `product_price_candidates` — **all FK to `item_id` or `product_id` chaining
  back to a single `item_id`.** Today's catalogue product is scoped to one
  item (i.e. one project), not global.
- `product_suggestions` — also `item_id`-scoped (pending-review candidates for that item's catalogue).
- `listing_identities`, `listing_identity_members`, `listing_duplicates`,
  `auction_snapshots`, `source_runs` — all keyed off `listings`/`source`
  globally (`listing_duplicates.item_id` records which item's matching
  *surfaced* a candidate pair for review, not an ownership scope).

## Target ownership boundary (per product decision, 2026-07-08)

| Data | Owner | Notes |
|---|---|---|
| `listings`, `listing_identities`, `listing_identity_members`, `listing_duplicates`, `auction_snapshots`, `source_runs` | **Platform / shared** | Already structurally global today — no change needed. |
| `products`, `product_price_observations`, `product_new_price_history`, `product_price_candidates` | **Platform / shared** (intended) | **Currently `item_id`-scoped in the schema — a pre-existing mismatch, see Known schema gap below.** |
| `projects` | **User** | `owner_user_id`, per this ADR. |
| `items` (watched item, search terms, exclude terms, max/target price, alert prefs, notes) | **Project** | Owned transitively via `project_id`. Already correctly modelled today. |
| `listing_matches` (candidate match between a watched item and a shared listing) | **Project/item** | Owned transitively via `item_id`. Already correctly modelled today. |
| `alerts_sent` | **Project/item** | Owned transitively via `match_id → listing_matches → item_id`. Already correctly modelled today. |
| Saved/ignored/shortlisted decisions, "wrong item/accessory/not relevant" feedback | **Project/item** | **Does not exist in the schema yet** — see FEATURE-1034. |

The one-line version: **a listing (and a catalogue product) is stored once
and surfaced to many users/projects; what's user-specific is the match,
the decision, and the context around it — never the listing or product row
itself.**

## Decision

**Single ownership boundary on user-owned data:** add a nullable
`owner_user_id` FK column to `projects` only. `items`, `listing_matches`,
`alerts_sent`, and the new decision/feedback table (FEATURE-1034) are owned
**transitively** through their existing FK chain to `projects` (directly or
via `items`) — they do not get their own `user_id` column. This avoids a
migration touching every table, and avoids the drift risk of a child row's
owner disagreeing with its parent project's.

**Catalogue and listing data (`listings`, `products`,
`product_price_observations`, `product_new_price_history`,
`product_price_candidates`, `product_suggestions`, `listing_identities`,
`listing_duplicates`, `auction_snapshots`, `source_runs`) get no
`owner_user_id` and no ownership-based read restriction.** They remain
readable across every project and every user — that is the entire point of
storing a listing/product once and surfacing it many times. This phase adds
no authorization gate to any of these tables.

**Migration (additive, following the existing `_MIGRATIONS` pattern):**

1. Add `projects.owner_user_id` as a nullable column (idempotent `ALTER TABLE`, consistent with existing migration style).
2. On first boot after this phase, backfill existing projects to a single,
   deterministically-created "legacy owner" user record (created by the
   migration itself, not by a real signup) — so existing single-user
   installs keep working with **zero manual steps**. This is the concrete
   mechanism for "existing local usage must migrate cleanly."
3. `owner_user_id IS NULL` is not a supported steady state after migration —
   it exists only transiently during the migration step itself.
4. No column is added to `products` or any catalogue/listing table by this migration.

**Authorization:** every `projects`/`items`/`listing_matches`/`alerts_sent`
(and future decision/feedback) read/write route gains an ownership check. A
single helper (e.g. `db.assert_project_owner(conn, project_id, user_id)` or
an equivalent query-scoping helper) is used consistently from `web/app.py`
route handlers, rather than each route hand-rolling its own check — this is
a deliberate defence against the "one route forgot the check" class of bug.
**The same helper must never be applied to a pure catalogue/listing read**
(e.g. fetching a `products` row's reference price, or a `listings` row's
detail) — those stay unrestricted regardless of which project's item is
asking.

When `auth.enabled=false` (Phase 2's default/local mode), the app continues
to operate as the legacy-owner's implicit session — no login prompt, no
behaviour change.

## Known schema gap — now tracked as ADR-0007 / EPIC-100

`products` (and its dependent price tables) are `item_id`-scoped in the
schema today, not global — this predates this ADR and predates the phased
roadmap entirely. It is a real mismatch against "catalogue = platform-owned"
even before any ownership work happens: two projects tracking the same
Makita SP6000 today get two independent `products` rows, two independent
`typical_used_price` medians, and no shared benefit from each other's price
observations.

This ADR does **not** attempt to decouple `products` into a true global
table with a per-item/per-project "tracked product" reference. That work is
now specced in **ADR-0007 (Catalogue globalization, EPIC-100)**, recommended
to land before this phase per ADR-0001's revised sequencing — cheaper to
migrate now than after real distinct users start accumulating their own
item-scoped catalogue entries at volume, and it removes the need for the
carve-out below entirely rather than requiring Phase 3 to route around it
indefinitely.

If Phase 3 ships before ADR-0007 lands (permitted — not a hard functional
blocker, see ADR-0001), the authorization rule below still keeps it correct:
**product/price reads are never gated by project ownership, even though the
underlying row happens to carry one project's `item_id` today.** This keeps
behaviour correct (no accidental cross-user leak *of private data*, since
product rows aren't private) while leaving the "two projects can't share one
product's price history" limitation exactly as it is today — not worsened,
not fixed — until ADR-0007 lands.

## Consequences

- This is the highest-risk phase for a cross-user data leak of **user-owned**
  data specifically (projects, items, matches, decisions/feedback). It
  requires the heaviest test investment of the five phases (see Required
  tests below).
- Every existing route that lists, reads, exports, or mutates a project (or
  anything reachable from one) must be audited against the new ownership
  helper — including less-obvious paths: CSV/JSON export, product-suggestion
  approve/dismiss, catalogue settings, manual-search-link generation. Routes
  that only ever touch catalogue/listing data (no project/item in scope) are
  explicitly out of scope for this audit.
- `source_settings` (global source enable/disable, eBay credentials) is
  **not** made per-user in this phase — it remains a global/operator setting;
  scoping it per-user is out of scope unless a real multi-tenant hosting need
  appears (flagged, not solved).

## Alternatives considered

- **`user_id` column on every table, including `products`/`listings`** —
  rejected outright: this is precisely the mistake the product boundary
  rules out. A listing or catalogue product must never carry a single
  owning user.
- **Row-level security at the SQLite layer** — rejected: SQLite has no
  native RLS; enforcing this in application code via one shared helper is
  simpler and matches the codebase's existing preference for explicit,
  auditable logic over framework magic.

## Deferred

- Fine-grained per-item sharing (Phase 5, ADR-0006).
- Org/team ownership — not requested, not built.
- Soft-delete/undo for ownership mistakes.
- **Catalogue globalization** (decoupling `products` from single-`item_id`
  scoping into a true shared table) — not part of this phase; see
  ADR-0007 / EPIC-100, recommended sequenced before this phase.

## Required tests (explicit, not optional for this phase)

- Authorization tests asserting user A cannot read, list, edit, delete, or
  export user B's **projects, items, notes, or candidate match/decision
  data** — via every route, not just the obvious CRUD ones.
- A complementary "shared data is not wrongly isolated" test: two users with
  their own projects/items both matching the same catalogue product/listing
  can both read that product's reference prices and that listing's details —
  proving the ownership helper was not accidentally applied to catalogue reads.
- Migration test: a pre-Phase-3 database (no `owner_user_id` column, or
  legacy fixture) migrates cleanly to a single legacy-owner project set with
  no data loss and no manual steps.
- Regression test: `auth.enabled=false` behaviour is provably unchanged from
  pre-Phase-3 behaviour.
