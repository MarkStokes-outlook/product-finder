# Product Finder — Architecture

This is the canonical, high-level architecture reference for Product
Finder. It complements, but does not replace, the ADRs in `docs/adr/` (the
record of *why* a specific decision was made) and
`docs/architecture-briefing.md` (an earlier, narrower snapshot, now
superseded by this document as the entry point). A new engineer or AI agent
should be able to read this document in 10–15 minutes and understand the
whole system.

---

## 1. Vision and guiding principles

Product Finder monitors second-hand marketplaces for listings matching a
user's wanted items, and judges each match — not just "does this exist" but
"is this genuinely cheap, and is it cheap for a good reason." A plain saved
search tells you a keyword matched; this application adds condition
grading, reference pricing at both item and specific-product granularity,
and a composite deal score on top.

Guiding principles (see `README.md`, "Design Principles" and
`docs/architecture-briefing.md`):

- **Working software over perfect software.** Small, additive, shippable
  changes over big-bang rewrites — see ADR-0001's five-phase roadmap, which
  explicitly rejects a "big-bang SaaS rewrite."
- **Local-first, currently.** Runs on the user's own machine against a
  local SQLite file, no accounts, no cloud dependency today. The roadmap
  (§13) grows this toward a public, multi-user surface *additively*, not by
  replacing this model.
- **Conservatism about inference.** The system prefers a plain, explainable
  rule (keyword match, median of observations, word-boundary regex) over a
  fuzzy or AI-driven guess. Where automation isn't possible without
  violating a marketplace's terms of service, it falls back to a
  human-followed manual link rather than scraping (see §8, connector risk
  model).
- **Declared, not inferred.** Connector capabilities, connector risk, and
  known limitations are explicit dataclass fields the code must state
  (`sources/base.py`'s `SourceCapabilities`/`ConnectorKnowledge`), never
  guessed from a source's name or type.
- **Nothing is hidden by accident.** Risk (connector account risk, scraping
  basis), affiliate config, and known gaps are surfaced, not silently
  absorbed — e.g. a scraping-based connector can exist, but never
  disguised as "automated" with no risk attached.
- **Compliant with marketplace terms of service.** Automation only where a
  marketplace's own official API or an open feed permits it; everything
  else is manual-assisted.

---

## 2. System overview

A single Python package (`src/product_finder/`), run either as a CLI or as
a Flask web app, both operating against one shared SQLite database (WAL
mode, so a background writer and the web UI reader never lock each other
out).

```
                        ┌────────────────────┐
                        │   config.yaml       │  (settings: postcode,
                        │   (config.py)        │   interval, alerts,
                        └─────────┬───────────┘   source *definitions*)
                                  │ seeds/overlays
                                  ▼
   ┌────────────┐   watch/    ┌──────────────────────┐   reads/writes   ┌────────────┐
   │ Marketplace │──run-once──▶│  runner.py            │◀────────────────▶│  db.py      │
   │ connectors  │  fetch      │  (orchestration)       │                  │  (SQLite,   │
   │ (sources/)  │  Listings   │  catalogue/grading/    │                  │   WAL mode) │
   └────────────┘             │  scoring/identity/dedup │                  └─────┬──────┘
                               └───────────┬────────────┘                        │
                                           │ alerts                              │ reads
                                           ▼                                     ▼
                                  ┌────────────────┐                    ┌──────────────────┐
                                  │ alerts/         │                    │ web/app.py         │
                                  │ (console/webhook)│                    │ (Flask, server-    │
                                  └────────────────┘                    │  rendered UI)       │
                                                                          └─────────┬─────────┘
                                                                                    │ every outbound
                                                                                    │ listing click
                                                                                    ▼
                                                                          ┌───────────────────────┐
                                                                          │ outbound.py             │
                                                                          │ Marketplace Outbound    │
                                                                          │ Gateway (§9)            │
                                                                          └───────────┬─────────────┘
                                                                                      ▼
                                                                              real marketplace
```

`watch`/`run-once` and `web` are **independent OS processes**. Search never
happens inside the web process — the dashboard only polls and displays
whatever `watch` finds. This is deliberate: a page load must never block on
a network call to a marketplace.

---

## 3. High-level architecture diagram

Layered by responsibility, narrowest at the bottom:

```
┌───────────────────────────────────────────────────────────────────┐
│  Presentation:  web/app.py (Flask routes) + templates/ (Jinja)      │
│                 cli.py (argparse entry points)                      │
├───────────────────────────────────────────────────────────────────┤
│  Orchestration: runner.py (one search cycle),                       │
│                 orchestrator.py (SearchOrchestrator/ExecutionPolicy) │
├───────────────────────────────────────────────────────────────────┤
│  Domain logic:  catalogue.py (product matching) · grading.py         │
│                 scoring.py (deal score/warnings) · identity.py       │
│                 duplicates.py · price_trend.py · auction_trajectory  │
│                 offers.py · connector_health.py · outbound.py (§9)   │
├───────────────────────────────────────────────────────────────────┤
│  Connectors:    sources/base.py (Source contract) + sources/*.py     │
│                 (ebay, gumtree, facebook, rss, links)                │
├───────────────────────────────────────────────────────────────────┤
│  Storage:       db.py (SQLite/WAL — schema, migrations, all CRUD)    │
│  Config:        config.py (YAML → dataclasses)                       │
└───────────────────────────────────────────────────────────────────┘
```

Each layer only ever calls downward. Presentation and orchestration never
contain marketplace-specific logic (that's the connectors' and, for
outbound navigation, the Marketplace Outbound Gateway's job). Domain logic
never contains SQL or Flask (`catalogue.match()`, `grading.grade()`,
`scoring.evaluate()` etc. are pure functions over plain data).

---

## 4. Data ownership model (platform-owned vs project-owned)

Today (single-user, no accounts) this distinction is architectural intent
more than an enforced boundary — but it already shapes the schema, and is
the basis Phase 3 (ADR-0004) builds real authorization on top of without a
data-model rewrite:

**Platform-owned (shared, global — not scoped to any one project/user):**
- `listings` — a marketplace listing is one real-world fact, not owned by
  whichever item happened to match it first.
- `products` / catalogue price history (`product_price_observations`,
  `product_new_price_history`, `product_price_candidates`) — see
  ADR-0007 (Catalogue globalization, EPIC-100, **shipped**): manufacturer/
  model entries and their price history are shared across every project,
  not duplicated per item. `item_products` is the join/context table for
  an item's *own* tracking of a shared product (match terms, target price,
  wanted/archived) — never a copy of the product itself.
- `listing_identities` / `listing_identity_members` (cross-source identity,
  §7) and `listing_duplicates` (fuzzy dedup, §7) — properties of the
  listings themselves.
- `listing_clicks` (§9) — an audit/analytics fact about a click, not
  project data.
- Source *definitions* (`sources.extra` in YAML) — always config, never
  duplicated into the DB (see §8).

**Project-owned (scoped to a project/user's own intent):**
- `projects`, `items` — what someone is watching for.
- `item_products` — an item's own match terms/target price *against* a
  shared product.
- `listing_matches` — the result of evaluating one listing against one
  item; a listing matching two items produces two independent match rows.
- `alerts_sent` — per-match alert delivery record.
- `source_settings` — currently host-wide (enabled/eBay creds), not
  per-user; Phase 2/3 (ADR-0003/0004) will need to decide whether this
  becomes per-user or stays host-wide.

This split is exactly why Catalogue Globalization (EPIC-100) was a hard
blocking prerequisite before the public/sharing phases (ADR-0001): once
real distinct users exist, per-user catalogue fragmentation would mean
duplicate products and fragmented price history instead of one shared,
improving catalogue.

---

## 5. Core domain model

See `docs/architecture-briefing.md` for the full field-level table (kept
there rather than duplicated here, since field lists rot fast — trust that
document's "Domain Model" section, or `db.py`'s `_SCHEMA`, as current
ground truth). Summary of the entities and how they relate:

```
Project 1───* Item 1───* ItemProduct *───1 Product 1───* ProductPriceObservation
                │                                              (used-price history)
                │                                          Product 1───* ProductNewPriceHistory
                │                                              (new-price history, SearXNG-sourced)
                *
         ListingMatch *───1 Listing
                              │
                              ├──* AuctionSnapshot (per-poll bid/BIN observation history)
                              ├──* ListingClick (Marketplace Outbound Gateway, §9)
                              └── ListingIdentity / ListingDuplicate (§7)
```

- **Project** — a named group of wanted items.
- **Item** — one wanted product search within a project (terms, prices,
  priority, source filter).
- **Product** — a platform-owned manufacturer/model catalogue entry
  (three-tier pricing: MSRP, typical new, typical used).
- **ItemProduct** — the join/context row: this item's own match terms and
  target price *against* a shared product.
- **Listing** — a single marketplace listing as fetched from a connector,
  normalised (`models.Listing`) — everything downstream only ever sees
  this shape, never a marketplace-specific one.
- **ListingMatch** (`models.Evaluation`) — the result of scoring one
  Listing against one Item: grade, deal score, margin, warning flags.
- **ListingClick** — one outbound redirect attempt (§9).

---

## 6. Request flow

A typical dashboard page load:

```
GET /  (web/app.py: dashboard())
  → _get_conn(cfg)                      one SQLite connection per request (g)
  → _dashboard_data(conn, cfg)
      → db.project_summaries()
      → db.project_top_picks()
      → db.query_matches(flagged=False) → best deals ("hero" + runners)
      → db.query_matches(flagged=True)  → warnings
      → db.dashboard_stats()
      → db.pending_duplicate_counts()
  → render_template("dashboard.html", ...)
      → macros in _ui.html / _match_table.html render each row
      → every listing href calls the listing_out_url() Jinja global (§9),
        never row['url'] directly
```

A listing click:

```
GET /out/<listing_id>?context=dashboard[&project_id=N]
  (web/app.py: listing_out())
  → db.get_listing(listing_id)          404 if missing
  → MarketplaceOutboundService.resolve(source, listing.url)   (§9)
  → outbound.is_safe_redirect_url(resolved)   defence in depth
  → db.record_listing_click(...)        never blocks the redirect on failure
  → 302 redirect to the resolved URL
```

Search/matching is a separate process entirely (§7) — a page load never
triggers a marketplace fetch.

---

## 7. Search/matching pipeline

Driven by `runner.py`, invoked by `watch` (continuous, `interval_minutes`)
or `run-once` (single pass). Per project → item → eligible source → search
term:

```
Source.search(term, item)             one connector, one term
  → raw Listing objects
Item exclude_terms / max_price filter
  → db.upsert_listing()                dedup by (source, external_id);
                                        refreshes price/bid/image on rescan
  → db.resolve_identity()              identity v1: canonical-URL match
                                        (identity.py) links a generic-feed
                                        sighting to an already-seen native
                                        listing (eBay item IDs today)
  → catalogue.match()                  resolve to a specific Product, if any
  → scoring.evaluate()                 grade + warning flags + deal score
                                        (scoring.py delegates grading to
                                        grading.py, warning detection to
                                        spec_match.py/price_trend.py)
  → db.record_match()                  one listing_matches row per (listing, item)
  → alerts (console/webhook)           only for genuinely new matches
```

Two-layer deduplication, only the first automatic:
- **Identity v1** (`identity.py`) — same platform-native ID recoverable
  straight from the URL (e.g. an RSS entry linking to an eBay item page).
  Auto-links, no human step.
- **Identity v2 / fuzzy duplicates** (`duplicates.py`) — no shared ID, only
  title/price/location/image similarity. Never auto-merges — only
  *proposes* pairs for human confirm/dismiss (`listing_duplicates` table,
  "Possible duplicates" on the project page). Same-marketplace pairs are
  never proposed (almost always distinct parallel stock, not a re-list).

Auction awareness runs on its own cadence inside the same `watch` process
(not a separate worker) — `auction_watch.py` polls a tracked auction more
frequently as it nears close, and captures a genuine "sold for" price the
moment eBay's availability flag flips, rather than trusting the last
timestamp.

**Search Aggregation Foundation (`orchestrator.py`, roadmap Phase F):** a
seam, not yet a behaviour change. `SearchOrchestrator` + `ExecutionPolicy`
formalise *how* connector search calls are scheduled/executed, decoupled
from *what* `runner.py` does with the results. `DefaultExecutionPolicy`
reproduces today's exact sequential, zero-retry, always-run semantics —
this exists so future work (priority ordering, health-aware skipping,
retry-with-backoff, concurrency) is a new `ExecutionPolicy`, not a rewrite
of `runner.py`.

---

## 8. Connector architecture

Every marketplace connector implements one small contract
(`sources/base.py`'s `Source` ABC): `name`, `capabilities()`,
`search(term, item)` (automated) or `manual_links(item)` (manual-assisted).
Everything downstream — grading, scoring, dedup, the web UI — only ever
sees a normalised `Listing` or `ManualLink`, never marketplace-specific
data.

**Two connector classes, both first-class:**
- **Automated** — official APIs (eBay Browse API), or genuinely open
  RSS/Atom feeds. `search()` does the work.
- **Manual-assisted** — marketplaces whose terms don't permit automation
  (Gumtree, Facebook Marketplace). `manual_links()` generates a
  pre-filtered search link for a human to follow instead.

**Connector risk model** (`SourceCapabilities.account_risk`,
`compliance_mode`, `is_scraping_based`, `requires_user_auth`) — compliance
is not a binary build/don't-build gate. A scraping or user-session
connector *can* exist, but risk must be declared, never hidden behind
`automated=True` (enforced by `__post_init__` validation — e.g.
`is_scraping_based=True` cannot be paired with `account_risk="none"`).
Anything above "low" risk requires explicit per-source opt-in in
`sources.risk_acknowledged` — being "enabled" is never enough on its own
(`sources/__init__.py`'s scheduler-side gate).

**Adding a connector:**
- Most new sites need **zero code** — add an entry under `sources.extra`
  in `config.yaml` (`type: rss` for an automated per-term feed, `type:
  links` for a manual-assisted templated search link).
- A connector needing real API integration (like eBay) gets a `Source`
  subclass registered in `sources/__init__.py`.

**Connector health & knowledge** (`connector_health.py`,
`sources/base.py`'s `ConnectorKnowledge`) — explainable, rule-based health
status built from telemetry already persisted (`source_runs`), not a
black-box score; every triggered rule reports its own reason. Each
connector self-describes its supported listing types, marketplaces,
known limitations, and roadmap notes for the Sources page's Capabilities
reference.

---

## 9. Marketplace outbound gateway

**Every outbound marketplace navigation in this application flows through
one service.** Templates never render a marketplace URL directly:

```
Listing → MarketplaceOutboundService → MarketplaceAdapter → redirect URL → Marketplace
```

- **`web/app.py`'s `GET /out/<listing_id>`** — the only route that emits a
  marketplace URL. Validates the listing exists (404 if not), resolves the
  destination via the service below, validates the result is a safe
  absolute http(s) URL (`outbound.is_safe_redirect_url` — open-redirect
  defence in depth), records a `listing_clicks` audit row, then issues a
  302. A resolution failing the safety check aborts with 502 and records a
  `failure` outcome rather than ever redirecting somewhere unsafe.
- **`outbound.MarketplaceOutboundService`** — the single entry point;
  built once per effective config (affiliate config can be DB-overlaid the
  same way source enable/disable is). Dispatches on `Listing.source` to a
  `MarketplaceAdapter`; an unrecognised source (stale data, a removed
  connector) fails safe to the original URL unchanged rather than blocking
  navigation.
- **`outbound.MarketplaceAdapter`** (ABC) — the extension point every
  future affiliate programme, marketplace quirk, or tracking need plugs
  into. Deliberately mirrors `sources/base.py`'s `Source` contract style.
  Each adapter decides, entirely on its own: whether affiliate parameters
  are supported, how the destination URL is constructed, and how its own
  failures are handled (must not raise for a normal URL; falls back to the
  original URL unchanged on any internal problem). Two generic
  implementations cover every marketplace today:
  - `PassthroughAdapter` — no affiliate programme configured; URL
    unchanged. This is not a bypass of the gateway — tracking and the
    redirect hop still apply uniformly.
  - `QueryParamAffiliateAdapter` — config-driven query-parameter
    injection (covers eBay Partner Network and most affiliate schemes). A
    marketplace needing something more exotic (a cloaked/signed redirect
    URL) gets its own `MarketplaceAdapter` subclass without touching the
    service or the route.
- **`config.OutboundConfig`** (`AppConfig.outbound`) — server-side-only
  affiliate config (`outbound.affiliate_params` in `config.yaml`), keyed by
  source name. Never rendered into a template, script, or API response —
  only the resolved destination URL (in the redirect's `Location` header)
  ever carries a partner ID.
- **`listing_clicks` table** (`db.record_listing_click`) — one row per
  redirect attempt: `listing_id`, `project_id` (nullable — set for
  project-scoped surfaces), `source`, `context` (which page/surface:
  `dashboard`/`project`/`auctions`/`offers`/`duplicate_review`/`unknown`),
  `outcome` (`success`/`failure`), `affiliate_applied`, `user_id` (nullable,
  always `NULL` until Phase 3/EPIC-103 starts writing it — the column
  exists now so that phase needs no second migration), `clicked_at`. A
  failed click-record write never blocks the redirect itself (the route
  wraps the write in its own try/except).

**Deferred, deliberately:** per-user click attribution (needs Phase 3's
real `user_id`), affiliate revenue reporting/dashboards, A/B testing or
multi-programme selection per source, extending tracking to
manual-assisted search links (Gumtree/Facebook/`links`-type sources — those
are search pages, not listings, with no natural `listing_id`), and
anonymous session identifiers (no session concept exists anywhere else in
this single-operator, no-auth app yet — adding one purely for click
tracking would be speculative; it belongs with whatever phase introduces
real sessions). See `docs/adr/0002-affiliate-link-redirect-and-tracking.md`.

---

## 10. Import/export architecture

`project_import.py` — a JSON/YAML backup and bulk-load format
(`product-finder/import/v1`, see `docs/imports/*.example.{yaml,json}`).
Deliberately two-phase:

1. **`build_plan()`** — read-only. Parses a document (naming a project by
   id or by name, optionally creating it, plus a `defaults` block and a
   list of items), validates it, and returns an `ImportPlan` — including a
   full validation error list — for the caller to render as a preview
   before anything commits.
2. **`apply_plan()`** — performs the writes, and must only be called with a
   plan whose `valid` flag is `True`. Callers **must** re-validate
   (call `build_plan()` again on the same raw text) at the point of
   commit rather than trusting an earlier plan — the database may have
   changed in between (e.g. the target project renamed).

`export_project()` produces the same document shape back out, so
export → edit → import round-trips. `to_yaml()`/`to_json()` are the two
supported serialisations.

Separately, `import-config` (CLI command / Projects page button) is a
different, simpler mechanism: merges `config.yaml`'s `projects:` section
into the database by (project slug, item name), for the YAML-as-seed
workflow described in §12 below. It is not part of the JSON/YAML backup
format above.

---

## 11. Public vs authenticated architecture (future)

Not built yet — this section describes the target shape from ADR-0001's
five-phase roadmap, so current work doesn't foreclose it by accident.

Today: every page is unauthenticated, local-only, single implicit owner.
Planned phases (each independently shippable — see §13):

- **Phase 2 (ADR-0003)** — Authentik/OIDC as the authentication backend.
  Login/session plumbing only; no ownership changes yet, so login
  correctness is provable in isolation before authorization rules layer on
  top.
- **Phase 3 (ADR-0004)** — user-owned data. `projects.owner_user_id` and an
  authorization model. Platform-owned data (§4: listings, catalogue) stays
  unrestricted by project ownership — only project/item-scoped data gains
  an owner.
- **Phase 4 (ADR-0005)** — public vs signed-in split. Anonymous users can
  search, browse live deals, and click through (via the outbound gateway,
  §9); gated actions (saving a project, editing sources) require sign-in.
  Subscriptions/billing get no-op hooks here, not real payment
  integration.
- **Phase 5 (ADR-0006)** — project sharing, invites, and cloning (not
  real-time co-editing — two distinct primitives: share-by-invite and
  clone-by-reference). Depends on Catalogue Globalization (EPIC-100,
  already shipped) so cloning references the shared catalogue rather than
  copying it.

Explicitly out of scope for all five phases: subscriptions/billing
enforcement, real-time multi-editor collaboration, a public API, a mobile
app or browser extension, and role-based access control beyond
owner/recipient/anonymous.

---

## 12. Extension points

The places designed to grow without touching the core:

- **New marketplace connector** — implement `sources.base.Source`
  (§8). Zero-code option (`sources.extra` in YAML) for anything needing
  only an RSS feed or a templated search link.
- **New marketplace affiliate programme / outbound behaviour** — implement
  `outbound.MarketplaceAdapter` (§9). Zero-code option
  (`outbound.affiliate_params` in YAML) for a simple query-param scheme.
- **New search execution policy** (scheduling, retry, health-aware
  skipping, concurrency) — implement `orchestrator.ExecutionPolicy` (§7).
- **New catalogue matching strategy** — `catalogue.match()` is the only
  entry point; a future AI-assisted matcher can replace or wrap it without
  touching `runner.py`/`scoring.py`.
- **New alert channel** — `alerts/` package; console and webhook exist
  today behind a common per-match "already sent" guard
  (`alerts_sent` table).
- **New import/export consumer** — `project_import.py`'s
  `product-finder/import/v1` document shape is versioned in its own name,
  so a v2 can be introduced without breaking v1 documents.

---

## 13. Roadmap overview

See `docs/adr/0001-phased-path-to-public-commercial-readiness.md` for the
authoritative sequencing and rationale. Summary:

| # | Phase | ADR | Epic | Status |
|---|---|---|---|---|
| — | Catalogue globalization (prerequisite, not a numbered phase) | ADR-0007 | EPIC-100 | **Shipped** |
| 1 | Marketplace outbound gateway / affiliate links | ADR-0002 | EPIC-101 | **Shipped** |
| 2 | Authentik/OIDC authentication backend | ADR-0003 | EPIC-102 | Planned |
| 3 | User-owned data & authorization | ADR-0004 | EPIC-103 | Planned |
| 4 | Public homepage & search experience | ADR-0005 | EPIC-104 | Planned |
| 5 | Project sharing, invites & cloning | ADR-0006 | EPIC-105 | Planned |

Each phase merges to `main` independently, is useful (or at minimum
inert-and-safe) on its own, and does not require a later phase to be
correct or valuable. No phase requires rewriting the SQLite/WAL storage
model, the `Source` connector contract, or the scoring/catalogue/identity
pipeline — all five are additive to the domain model described in §5.

Separately from the ownership roadmap, `docs/strategy/roadmap.md` tracks
product-quality workstreams (deal accuracy, identity resolution, coverage)
that apply regardless of which ownership phase is current.

---

## 14. References to relevant ADRs

- `docs/adr/0001-phased-path-to-public-commercial-readiness.md` — the
  five-phase sequencing and rationale (§11, §13).
- `docs/adr/0002-affiliate-link-redirect-and-tracking.md` — the Marketplace
  Outbound Gateway decision record (§9).
- `docs/adr/0003-authentik-oidc-authentication-backend.md` — Phase 2 (§11).
- `docs/adr/0004-user-owned-data-model-and-migration.md` — Phase 3,
  ownership boundary (§4, §11).
- `docs/adr/0005-public-vs-signed-in-experience-split.md` — Phase 4 (§11).
- `docs/adr/0006-project-sharing-invites-and-cloning.md` — Phase 5 (§11).
- `docs/adr/0007-catalogue-globalization.md` — the platform-owned/
  project-owned catalogue split (§4, §5), shipped prerequisite.
- `docs/architecture-briefing.md` — earlier, narrower architectural
  snapshot (domain model field-level detail, marketplace support table,
  known limitations) — still useful for detail this document
  deliberately keeps out to avoid duplication/rot.
- `docs/strategy/roadmap.md` — product-quality workstreams orthogonal to
  the ownership roadmap.
- `README.md` — setup, configuration, and day-to-day usage.
