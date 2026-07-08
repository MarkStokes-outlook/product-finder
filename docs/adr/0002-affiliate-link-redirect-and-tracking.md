# ADR-0002: Outbound link redirect and affiliate attribution

**Status:** Proposed
**Date:** 2026-07-08
**Phase:** 1 of 5 (see ADR-0001)
**Related backlog:** EPIC-101 (FEATURE-1010..1013)

## Context

Listing cards render the marketplace listing URL directly as an outbound
`<a href>`:

- `src/product_finder/web/templates/_ui.html` (`spotlight`, `deal-card` — `row['url']`)
- `src/product_finder/web/templates/_match_table.html` (`row['url']`)
- `src/product_finder/web/templates/auctions.html` (`row['url']`)

`Listing.url` (`src/product_finder/models.py`) is stored verbatim from the
source and used as-is throughout the pipeline (grading, scoring, dedup,
identity resolution all key off other fields — `url` is purely a destination
link plus, for canonical-URL identity resolution, a signal to extract a
platform's native item ID from). There is currently no click tracking and no
mechanism to inject affiliate/referral parameters. Manual-assisted sources
(Gumtree, Facebook, config-defined `links`-type sources) render separately
(`manual.html`, `_project_detail_live.html`) as **search page** links, not
listing links — they have no `listing_id` and are out of scope here.

## Decision

Introduce an internal redirect endpoint, e.g. `GET /out/<listing_id>`, that:

1. Looks up the listing's stored, unmodified `url` by `listing_id`.
2. Resolves an affiliate/referral destination URL **server-side**, per
   source, via a small per-source resolver analogous to the existing
   `Source` contract (`sources/base.py`) — each source that supports
   affiliate parameters declares how to transform a destination URL, driven
   by server-side config.
3. Records a `listing_clicks` row (audit/analytics).
4. Issues an HTTP 302 redirect to the resolved destination.

All three templates above change `href="{{ row['url'] }}"` to
`href="{{ url_for('listing_out', listing_id=row['id']) }}"`, keeping
`target="_blank" rel="noopener"` unchanged.

**`listings.url` is never mutated.** Affiliate URLs are computed at redirect
time from config, not stored — a future affiliate-programme change (new
partner ID, new source added, programme dropped) never requires backfilling
historical listing rows, and the stored URL keeps its existing role as
plain-source evidence (including the canonical-URL identity resolver's use
of it).

**New table `listing_clicks`:** `id`, `listing_id` (FK), `clicked_at`,
`source` (echoing `listings.source` at click time), `context` (e.g.
`dashboard` / `project` / `auctions` — which surface the click came from),
`user_id` (nullable FK — always NULL until Phase 3 introduces users;
present now so Phase 3 doesn't need a second migration on this table).

**Affiliate config is server-side only** — config.yaml / environment,
following the existing pattern of `source_settings` for DB-stored per-source
overrides where persistence is needed, but affiliate partner IDs/secrets are
never rendered into HTML, JS, or API responses, and never logged in plaintext.

## Consequences

- Every outbound listing click gains one redirect hop. Acceptable latency
  cost for tracking + affiliate correctness.
- Existing listing behaviour (what the user sees, where "open listing" leads)
  is unchanged in outcome — same destination, same new-tab behaviour — only
  the mechanism changes.
- Sources that have no configured affiliate programme simply redirect to the
  original URL unchanged; the redirect endpoint and click tracking still
  apply uniformly (tracking is not conditional on monetisation).

## Alternatives considered

- **Client-side affiliate parameter injection** (JS rewrites the href) —
  rejected: exposes affiliate IDs/config in page source, trivially stripped
  by ad-blockers or copy-pasting the link, and produces no server-side click
  audit.
- **Rewriting `listings.url` in place with affiliate params baked in** —
  rejected: conflates evidence/provenance data (used by identity resolution
  and shown as "the listing") with a monetisation detail that can change
  independently of the listing itself; also would require a backfill on
  every affiliate config change.

## Deferred

- Per-user click attribution (needs Phase 3's `user_id`; column exists now, unused).
- Affiliate revenue reporting/dashboards.
- A/B testing or multi-programme selection per source.
- Extending redirect/tracking to manual-assisted search links (Gumtree/Facebook/`links`-type) — those are search pages, not listings, and have no natural `listing_id`.
