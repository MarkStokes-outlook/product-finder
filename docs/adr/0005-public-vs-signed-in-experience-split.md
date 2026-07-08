# ADR-0005: Public vs signed-in experience split

**Status:** Proposed
**Date:** 2026-07-08
**Phase:** 4 of 5 (see ADR-0001)
**Related backlog:** EPIC-104 (FEATURE-1040..1043)

## Context

Every page in the current web app is unauthenticated and shows everything —
including `/sources`, which exposes eBay `app_id`/`cert_id` fields and
per-source enable/disable state (`source_settings`), and dashboard/project
views that assume a single implicit owner. `docs/strategy/roadmap.md`
("Sources and trust", "Users and saved projects") already names the target
split: "Anonymous users can search, browse live deals, and click through...
Signed-in users can create projects, save watched products, configure
alerts, preserve preferences."

Phase 3 (ADR-0004) established project ownership and authorization. This
phase is what makes that distinction visible and safe to expose beyond
localhost.

## Decision

Introduce a public, read-only, unauthenticated surface (a refactored
homepage) that:

- Reuses the shape of the existing dashboard "best deals" query, but against
  an explicitly public-safe data set — not a literal reuse of
  `_dashboard_data`/`_project_detail_data`, which today assume a single
  implicit owner and surface everything.
- Lets anonymous users search and view a **limited** set of results and
  click through via the Phase 1 redirect endpoint (`/out/<listing_id>`,
  still tracked, `user_id` NULL for anonymous clicks).
- Cannot save, track, create a project, enable alerts, or reach any
  authenticated user's private data (projects, notes, candidate listings).
- Renders gated actions (save/track/create project/alerts) as visible but
  disabled, prompting login/registration — not simply hidden — per the
  user's explicit guidance, so the product's value is discoverable before
  signup.
- Hides, at the query/serialization layer (not just in the template):
  scraper diagnostics, connector/source internals and health, admin
  metadata, `source_settings` credentials, and private notes from every
  public-reachable route and API response.

**Route/permission boundary:** introduce an explicit decorator (e.g.
`@public_ok` for routes safe to serve anonymously, default deny otherwise)
in `web/app.py`, so "what is public" is one reviewable, centralised
declaration per route rather than something inferred per-template. This
directly addresses the risk in ADR-0004's Consequences section (routes that
forget a check) by making the *public* side an explicit allow-list instead.

**Entitlement hook (no billing):** each gated action gets a single
`requires_entitlement: bool` (or equivalent) point where a future
subscription check could be inserted. In this phase it is always a no-op —
"logged in" is the only gate — never wired to payment.

## Consequences

- The `app.py` module docstring's "no auth by design... localhost only"
  assumption becomes stale from this phase onward and must be updated —
  binding the app beyond localhost changes its threat model materially and
  that change must be a deliberate, documented decision, not an implicit
  side effect of adding routes.
- Public search needs its own reviewed query path; it cannot silently drift
  from the authenticated dashboard's query as that query evolves.
- Rate limiting / anti-abuse for public search is a real operational
  question once the app can be reached beyond localhost, and is explicitly
  flagged rather than solved here (see Deferred).

## Alternatives considered

- **Template-level hiding only** (keep serving full data, hide fields in
  Jinja) — rejected: private data would still be present in the HTTP
  response for anyone inspecting it; the filtering must happen before
  serialization, not only at render time.
- **Reusing the exact same routes for public and authenticated users with
  conditional rendering** — rejected in favour of an explicit allow-list
  decorator, for the same reason ADR-0004 chose one shared ownership helper
  over per-route ad hoc checks: a default-deny posture is safer than
  default-allow-with-exceptions for anything reachable beyond localhost.

## Deferred

- Subscription/billing gating — entitlement hook only, no payment integration.
- Public API (distinct from the public HTML surface).
- Rate limiting / anti-abuse / bot protection for public search — flagged as
  a security assumption to revisit before real public exposure, not solved
  in this phase.
- Any SEO/content-marketing surface beyond the search/browse experience itself.
