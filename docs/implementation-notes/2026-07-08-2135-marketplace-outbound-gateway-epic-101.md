# Marketplace Outbound Gateway (EPIC-101 / ADR-0002)

**Date:** 2026-07-08
**Epic:** EPIC-101 (FEATURE-1010..1013)
**ADR:** docs/adr/0002-affiliate-link-redirect-and-tracking.md
**Tests:** 698 passing (665 prior + 33 new: 19 in `tests/test_outbound.py`,
14 in `tests/test_web.py`), zero regressions.

## What shipped

EPIC-101 was specced as "Affiliate link redirect & tracking." It's
implemented and framed here as a **Marketplace Outbound Gateway** — a
general-purpose extension point for *any* outbound marketplace navigation
(affiliate parameters, future click analytics needs, future
marketplace-specific redirect quirks), not an affiliate-only feature. The
ADR's specific requirement (affiliate parameter injection, click tracking)
is the gateway's first and, today, only real consumer.

- **`src/product_finder/outbound.py`** (new module) — the gateway itself:
  - `MarketplaceAdapter` (ABC) — one marketplace's outbound-URL policy,
    deliberately mirroring `sources/base.py`'s `Source` contract style.
  - `PassthroughAdapter` — no affiliate programme configured; URL
    unchanged.
  - `QueryParamAffiliateAdapter` — config-driven query-parameter injection;
    covers eBay Partner Network and most affiliate schemes without any
    per-marketplace code.
  - `MarketplaceOutboundService` — dispatches `Listing.source` to the right
    adapter; unknown source or a misbehaving adapter both fail safe to the
    original URL, never blocking navigation.
  - `is_safe_redirect_url()` — open-redirect defence in depth (absolute
    http(s) URL with a network location, or refuse).
- **`GET /out/<int:listing_id>`** (`web/app.py`) — the only route that
  emits a marketplace URL. Looks up the listing (404 if missing), resolves
  via the service above, validates the result, records a `listing_clicks`
  row, then 302s. An unsafe resolved URL aborts 502 with a `failure` click
  recorded, rather than ever redirecting somewhere unsafe.
- **`listing_clicks` table** (`db.py` `_SCHEMA`, `db.record_listing_click`)
  — `listing_id`, `project_id` (nullable), `source`, `context`, `outcome`,
  `affiliate_applied`, `user_id` (nullable, reserved for Phase 3/EPIC-103),
  `clicked_at`.
- **`config.OutboundConfig`** (`AppConfig.outbound`) — server-side-only
  `outbound.affiliate_params` in `config.yaml`, keyed by source name.
  Documented in `config.example.yaml`.
- **Templates** — every listing `<a href>` across the app now calls the
  `listing_out_url()` Jinja global instead of rendering `row['url']`
  directly: `_ui.html` (spotlight/deal_card), `_match_table.html`,
  `auctions.html`, `offers.html`, and `_project_detail_live.html`'s
  duplicate-review `dup_side` macro. `listings.url` itself is never
  written to by this feature — read-only lookup, exactly as ADR-0002
  requires.

## Design decisions worth flagging

- **Scope beyond the ADR's original 3 templates.** ADR-0002/FEATURE-1010
  named `_ui.html`, `_match_table.html`, and `auctions.html`. The actual
  brief for this session asked for a generic gateway with the explicit
  design principle "the application should never emit marketplace URLs
  directly from templates once this lands" — broader than the ADR. Audited
  every template for a listing-level outbound `<a href>` and found two more
  in scope: `offers.html` (same `_MATCH_SELECT`-backed rows, just missed by
  the original ADR) and the duplicate-review `dup_side` macro in
  `_project_detail_live.html`. Both now route through the gateway with
  their own `context` values (`offers`, `duplicate_review`). Left
  deliberately untouched: `manual.html` / the manual-search-link list in
  `_project_detail_live.html` (`ManualLink` has no `listing_id` — a search
  page, not a listing, exactly as ADR-0002's own "Deferred" section says),
  and `product_form.html`'s retailer price-candidate links (`c['url']`) —
  those are new-price research candidates from `retailer_price.py`, a
  different domain concept from a marketplace listing click, not a
  marketplace outbound navigation at all.
- **`context`/`project_id` come from `_MATCH_SELECT` rows, not new
  plumbing.** Every surface this feature touches (dashboard hero/runners,
  project hero/table, auctions, offers) already selects `l.id AS
  listing_id` and `p.id AS project_id` via `db._MATCH_SELECT` — no new
  joins or route parameters were needed. `listing_out_url(listing_id,
  context, project_id)` is a Jinja global registered once in `app.py`;
  every macro/template passes its own literal `context` (or, for
  `spotlight`/`deal_card`/`match_table`, a `context='dashboard'` default
  overridden to `'project'` by the project-detail include).
- **Bug caught against the original FEATURE spec:** FEATURE-1010's
  acceptance criteria said `url_for('listing_out', listing_id=row['id'])`
  — but `_MATCH_SELECT` never selects a plain `id` column (it selects
  `l.id AS listing_id`, `p.id AS project_id`, `i.id AS item_id` to avoid
  exactly this ambiguity). Used `row['listing_id']` throughout instead;
  `row['id']` would have raised `IndexError` on every real page.
- **No `marketplace` field renaming.** The public-facing new
  abstraction is named "Marketplace Outbound Gateway"/"MarketplaceAdapter"
  per this session's brief, but internally it reuses the existing
  `source` string vocabulary throughout (`listings.source`,
  `listing_clicks.source`, `Source.name`) rather than introducing a
  parallel "marketplace" field name into the 2900-line `db.py`. Two names
  for the same concept inside one table would be worse than the
  terminology mismatch between "marketplace" (conceptual, in prose/new
  code) and "source" (the actual column/field name everywhere else).
- **No IP hashing, no anonymous session identifier.** FEATURE-1012's
  original notes mentioned an optional hashed-IP column; this session's
  broader analytics ask also mentioned an "anonymous session identifier if
  appropriate." Neither was added. There is no session concept anywhere
  else in this single-operator, no-auth app today (`app.secret_key` is
  used only for flash messages) — scaffolding a session-id column with
  nothing to populate it would be exactly the kind of speculative field
  this codebase's stated philosophy (`sources/base.py`: "declared not
  inferred... not a wishlist") argues against, and the explicit brief for
  this table was "add only the minimum schema needed for click tracking."
  Both are one migration away whenever a real session/identity phase
  (EPIC-102/103) lands — `user_id` is already there for exactly that
  reason.
- **Adapter failure handling has two layers.** Each adapter is documented
  to never raise for a normal URL and fall back internally
  (`QueryParamAffiliateAdapter.resolve()` wraps its own URL construction in
  try/except). `MarketplaceOutboundService.resolve()` also wraps every
  adapter call, so a *misbehaving* adapter (a bug in a future bespoke
  adapter) still can't take down the redirect endpoint. Belt and braces,
  deliberately, since this is the one place a coding mistake could turn
  into a broken "buy" button across the whole app.
- **`is_safe_redirect_url()` is defence in depth, not the primary
  security boundary.** The primary boundary is that `/out/<listing_id>`
  takes an internal integer ID, never a URL, so there is no
  attacker-controlled destination to validate in the first place (the
  classic open-redirect vector — `?url=`-style parameters — doesn't exist
  here). The safety check exists for the case where the *server's own*
  data (a stored `listings.url`, or adapter-constructed output) is
  malformed, so a bad data row can never become an unsafe redirect
  instead of a loud 502.
- **One existing test fixed, not worked around.**
  `test_project_hero_excludes_flagged_listings_even_if_top_scored` asserted
  raw `example.com/1` / `example.com/2` substrings inside a page-section
  slice to check which listing headlined the hero card — that assertion
  necessarily breaks once hrefs point at `/out/<id>` instead of the raw
  URL. Rewrote it to assert on `/out/<listing_id>` presence/absence for the
  two listings involved, preserving the original test's intent (the
  flagged listing must never headline the hero) rather than loosening it.

## Verification

- `pytest` — 698 passed, 0 failed.
- Manual end-to-end smoke test against the real CLI/Flask entry point (not
  just the Flask test client): seeded a real SQLite DB via
  `product_finder.db`, started `python -m product_finder web` with a
  config declaring `outbound.affiliate_params.ebay.campid`, and confirmed
  via `curl`:
  - Dashboard/project page hrefs are `/out/<id>?context=...&project_id=...`,
    never the raw eBay URL.
  - `GET /out/<id>` 302s with `Location: https://www.ebay.co.uk/itm/...?campid=SMOKE123`.
  - `GET /out/999` (unknown) and `GET /out/abc` (malformed) both 404.
  - The campaign id string never appears anywhere in dashboard/project page
    source (`grep -c SMOKE123` → 0).
  - A `listing_clicks` row was written with the correct
    `source`/`context`/`outcome`/`affiliate_applied`.

## Deferred (per ADR-0002 and this session's explicit exclusions)

- Per-user click attribution — `listing_clicks.user_id` exists, always
  `NULL`, until EPIC-103.
- Affiliate revenue reporting/dashboards.
- A/B testing or multi-programme selection per source.
- Extending tracking to manual-assisted search links (no `listing_id`).
- Anonymous session identifiers (see design decisions above).
- Subscriptions, billing, Authentik, ownership, public homepage, sharing,
  invite flows — explicitly out of scope per this session's brief; nothing
  in this change touches any of them.

## ARCHITECTURE.md

Also added `ARCHITECTURE.md` at the repo root — the new canonical
high-level architecture document (vision, system overview, diagrams, data
ownership model, domain model, request flow, search pipeline, connector
architecture, this gateway, import/export, the future public/auth split,
extension points, roadmap, and ADR references). Complements rather than
replaces the ADRs or `docs/architecture-briefing.md` (which stays as
field-level detail this document deliberately doesn't duplicate).
