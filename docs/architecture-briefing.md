# Product Finder — Architectural Briefing

> **Status:** Historical briefing. This document is superseded by
> `ARCHITECTURE.md` for current-state architecture. It is preserved as an
> earlier field-level snapshot and may contain stale statements about Ollama,
> SearXNG, duplicate behaviour, catalogue ownership, and test counts.

## Executive Summary

Product Finder is a **local, single-user Python application** that monitors second-hand marketplaces (currently eBay UK, with manual-assisted support for Gumtree and Facebook Marketplace, plus arbitrary config-defined RSS/link sources) for listings matching a user-defined set of wanted items, and scores each match for how good a deal it genuinely is.

The problem it solves: a plain saved search on eBay tells you *that* something matching your keywords exists, not whether it's actually cheap, whether it's broken, or whether it's cheap *because* it's broken. Product Finder adds a layer of domain judgement on top of raw search results — condition grading from listing text, price comparison against known-good reference prices (at both item and specific-product granularity), and a composite deal score — so that a £50 "track saw" that's faulty and a £350 Makita track saw in great condition aren't judged by the same yardstick just because they share a search term.

It has evolved from an MVP (search + console alert + simple score) into a system with: a persistent SQLite-backed domain model (projects/items editable via a web UI, not just YAML), a manufacturer/model product catalogue with three-tier reference pricing (MSRP, typical new, typical used), an automatically-updating used-price index built from its own observed listings, live-auction awareness (excluding uncommitted bid prices from deal surfacing), and a mechanism for auto-discovering catalogue products from eBay's own structured item data.

## Vision

The stated design philosophy (README) is: working software over perfect software, simple configuration, local-first, compliant with marketplace terms of service, a small and maintainable codebase, easy to extend. It runs entirely on the user's own machine against a local SQLite file, with no accounts, no cloud dependency, and no server-side auth (the web UI binds to localhost only by design).

The recurring theme across the codebase is **conservatism about inference**: the system prefers a plain, explainable rule (keyword match, median of observations, word-boundary regex) over a fuzzy or AI-driven guess, and where automation isn't possible without violating a marketplace's terms of service (Gumtree, Facebook), it falls back to generating a pre-filtered manual search link rather than scraping.

## Architecture

The codebase is a single Python package (`src/product_finder/`) run either as a CLI or as a Flask web app, both operating against one shared SQLite database.

- **Config layer** (`config.py`) — loads and validates `config.yaml` into dataclasses (`AppConfig`, `ProjectConfig`, `ItemConfig`, `SourcesConfig`, etc.). Owns settings that stay YAML-only: postcode, radius, interval, alerts, and source *definitions*.
- **Storage layer** (`db.py`) — SQLite (WAL mode) is the source of truth for projects, items, the product catalogue, listings, matches, alerts-sent, and per-source setting overrides. Owns all CRUD, plus schema migrations applied on connect.
- **Marketplace connectors** (`sources/`) — one class per marketplace behind a common `Source` contract (`base.py`); a registry (`sources/__init__.py`) builds the active set from config.
- **Listing pipeline / orchestration** (`runner.py`) — one search cycle: for each project/item/source/term, fetch listings, apply exclude-term and max-price filters, resolve to a catalogue product, score, persist, and queue alerts.
- **Catalogue matching** (`catalogue.py`) — resolves listing text to a specific manufacturer/model product.
- **Grading** (`grading.py`) — keyword-based condition classification.
- **Scoring** (`scoring.py`) — warning flags, margins, and the composite deal score.
- **Auction tracking** (`auction_watch.py`) — polls live eBay auctions nearing their close and captures a genuine closing price.
- **Alerting** (`alerts/`) — console print and outbound webhook POST.
- **Web UI** (`web/app.py` + `templates/`) — Flask, server-rendered, read/write dashboard and CRUD over the DB.
- **CLI** (`cli.py`) — argparse entry point wiring all of the above into runnable commands.

Interaction boundary: everything downstream of a `Source` only ever sees a normalised `Listing` dataclass — grading, scoring, dedup-by-upsert, and the web UI have no marketplace-specific knowledge. `catalogue.match()` similarly knows nothing about SQLite or Flask; it's pure text-matching over `Product` objects. The CLI's `watch` command and the `web` command are **independent processes** reading/writes the same SQLite file concurrently (enabled by WAL mode) — searching never happens inside the web process.

There is no AI/Ollama integration, no SearXNG integration, and no separate background worker/task-queue framework in the codebase today — see **AI** and **Workers** sections below.

## Domain Model

- **Project** (`projects` table) — a named group of wanted items (e.g. "The Coachhouse Tools"). Fields: `slug` (unique), `name`, `archived`, `sources` (optional JSON list restricting which sources apply to every item inside it). Has many Items.
- **Item** (`items` table) — one wanted product search within a project (e.g. "Track Saw"). Fields: `name`, `priority` (high/normal/low), `max_price` (hard filter), `normal_price` (blended expected market value), `target_deal_price`, `notes`, `terms` (JSON list of search phrases), `exclude_terms` (JSON list), `sources` (optional per-item restriction), `archived`. Belongs to a Project; has many Products, Product Suggestions, and (via matches) Listings.
- **Product** (`products` table) — a specific manufacturer/model tracked under one Item's catalogue (e.g. "Makita SP6000"). Fields: `manufacturer`, `model`, `match_terms` (JSON list of keyword phrases checked against listing title+description), `msrp` (informational only), `typical_new_price` (manually maintained, used for scoring as "the new price"), `typical_used_price` (auto-computed rolling 90-day median of observed used prices — never set by hand), `target_deal_price` (overrides the item's if set), `archived`. Optional — an item works fine with no catalogue at all.
- **Product Price Observation** (`product_price_observations` table) — one used-market price sighting for a Product: `price`, `source`, `observed_at`. Feeds the rolling `typical_used_price` median; one row per distinct matched listing (not per rescan).
- **Product Suggestion** (`product_suggestions` table) — a candidate manufacturer/model spotted automatically from eBay structured data, awaiting review. Fields: `manufacturer`, `model`, `confidence` (0–100, starts at 70, +8 per corroborating sighting, capped at 99), `sighting_count`, `source` (`ebay-structured`), `example_url`, `status` (`pending`/`approved`/`dismissed`). Unique per (item, manufacturer, model).
- **Listing** (`listings` table + `Listing` dataclass) — a single marketplace listing as fetched from a source. Fields: `source`, `external_id` (unique per source), `title`, `price`, `currency`, `url`, `location`, `description`, `condition`, `first_seen`/`last_seen`, `buying_options` (JSON, e.g. `["AUCTION"]`), `bid_count`, `end_time`, `last_poll_at`, `sold_captured` (auction-close flag), `brand_checked` (has this listing already been probed for structured brand/model data). Listings are never marketplace-specific downstream — always accessed as this one normalised shape.
- **Listing Match** (`listing_matches` table + `Evaluation` dataclass) — the result of evaluating one Listing against one Item (and optionally a matched Product): `grade`, `deal_score`, `margin_abs`, `margin_pct`, `under_target`, `flags` (JSON list), `product_id` (nullable). Unique per (listing, item) — a listing matching two items' terms produces two independent match rows.
- **Alert Sent** (`alerts_sent` table) — records that a match has already been alerted on a given channel (`console`/`webhook`), so a match is never re-alerted.
- **Source Settings** (`source_settings` table) — DB-stored overrides on top of YAML source definitions: `enabled` (nullable = inherit YAML) and, for eBay, `ebay_app_id`/`ebay_cert_id`/`ebay_env`.
- **App Settings** (`app_settings` table) — generic key/value store; currently used only for the catalogue auto-approve confidence threshold.

There is no explicit "Marketplace", "Seller", "Auction", "Price History", "Alert" (as a distinct row type beyond `alerts_sent`), "Brand", "Category", or "Deal" entity as a first-class table — those concepts exist as fields or derived query results on the entities above (e.g. "auction" is a state of a Listing, "brand" is a field on Product/Product Suggestion, "deal" is what a scored Listing Match represents).

## Marketplace Support

| Marketplace | Discovery | Automated? | API | Auth | Limitations |
|---|---|---|---|---|---|
| **eBay UK** | Full-text search via the official Browse API, per search term | Yes, when app credentials are configured | eBay Browse API (`item_summary/search`, `item/{id}`) | Free developer account; OAuth2 client-credentials grant (`app_id`/`cert_id`), token cached and auto-refreshed | GB-located listings only; the API does not support distance filtering (postcode is used only in manual-link fallback); no access to eBay's Marketplace Insights (sold-price) API, so true sold prices aren't available except via auction-close capture |
| **Gumtree UK** | None — generates pre-filtered search links | No | None | None | No official public API; scraping is against Gumtree's terms, so this is manual-assisted only |
| **Facebook Marketplace** | None — generates search links | No | None | None | Login-walled, no public API; manual-assisted only |
| **Config-defined RSS sources** (e.g. Reddit search feeds) | Per-term RSS/Atom feed fetch | Yes (if the site offers a searchable feed) | Whatever public feed the site exposes | None | Needs a `£` price literal in the entry title/description or the entry is skipped; optional `max_age_days` filter; requests throttled to at least 3s apart across all RSS sources |
| **Config-defined link sources** (e.g. Vinted, John Pye Auctions, Preloved, CeX) | None — generates a templated search link | No | None | None | Manual-assisted only, by design (no code required to add one) |

No source bypasses logins, CAPTCHAs, or bot protection anywhere in the codebase; source failures are caught per-term/per-source and logged, never crashing a run.

## Pricing Engine

Three separate reference prices exist per catalogue Product, because a single blended "normal price" hides real differences:

- **MSRP** — manufacturer's list price. Purely informational; never used in scoring.
- **Typical new price** — what it costs to buy new today. This is what scoring treats as "the new price." Manually maintained; no automated retailer price-watching exists (no public listing-search API for retailers, and scraping raises the same ToS problem as Gumtree/Facebook).
- **Typical used price** — a rolling median of the last 90 days of `product_price_observations`, recomputed on every new observation (`db.record_price_observation`). Never set by hand. One observation is recorded per distinct matched listing at first sighting only (not on every rescan), and never from a live auction's asking price.

When an item has no matched catalogue product, scoring falls back to the item's own single `normal_price`/`target_deal_price` estimate — the catalogue is opt-in, not required (`scoring.effective_prices`).

**Deal score** (`scoring.deal_score`, 0–100) is a heuristic sum:
- Baseline 40, plus up to +36 for percentage below `normal_price` (capped contribution, scaled 0.6×, floor −20/ceiling 60 before scaling).
- +15 if price is at or below `target_deal_price`.
- Condition-grade adjustment: A +10, B +5, C −10, spares/repair −40, unknown −5.
- Priority adjustment: high +10, low −5, normal 0.
- −8 per warning flag present, capped at −30.
- −5 if the title is fewer than 3 words (vague listing).
- −20 additional if it's a "likely false bargain" (price < 50% of normal_price *and* has warning flags).
- If a `typical_used_price` exists and the listing is priced *above* it by more than the noise band, a negative adjustment (scaled 0.4×, floor −30) — because beating the new price means nothing if it's still priced above the going used-market rate.
- Result clamped to [0, 100].

**Margin** (`scoring.margins`) is simple absolute and percentage difference between listing price and the effective `normal_price`.

**Confidence** exists only in the *catalogue-suggestion* sense (`suggestion_confidence`), not as a scoring-margin concept — see AI section.

**Auction handling**: a listing with `"AUCTION"` in its `buying_options` is flagged `"live auction"` and structurally excluded from dashboard/project "best deal" hero cards (its price is just a current bid, not a committed price) and never contributes an asking-price observation to the used-price index. Separately, `auction_watch.poll_and_capture()` runs inside the `watch` loop's tight tick (every 20 seconds) and, for auctions matched to a catalogue product, polls at a cadence that tightens as the end time approaches (>10min → every 5min, 2–10min → every 1min, <2min → every 20s). It confirms a close by checking eBay's item availability status flipping to `OUT_OF_STOCK` (not just the clock, since the API keeps returning the last bid price briefly after the nominal end time), then records that price as a `<source>-close`-tagged observation — a genuine "sold for" proxy, distinct from ordinary asking-price observations. It gives up (marks the listing as captured without a confirmed price) if no close can be confirmed 10 minutes past the nominal end time. This does not (and cannot, via this API) confirm the sale actually completed — a reserve-not-met auction is captured as if it sold.

## AI

**No AI/LLM/Ollama integration exists in the codebase today.** There is no dependency on Ollama, no prompt files driving runtime behavior, and no reasoning-model call anywhere in `src/product_finder`.

What might be mistaken for "AI" is entirely deterministic, rule-based code:
- **Condition grading** (`grading.py`) — a fixed, ordered list of keyword phrases (spares/repair terms checked first, then C/A/B) matched with word-boundary regex against listing title + condition + description text. No model involved.
- **Catalogue matching** (`catalogue.py`) — plain keyword lookup (longest matching term wins), same style as grading.
- **Product discovery** (the "suggestion" mechanism) — draws exclusively from eBay's own structured `brand`/`mpn` item-specifics fields (a seller-declared fact fetched via `EbaySource.get_item_details`), not from any inference over free text. The confidence score that climbs with corroborating sightings is a fixed arithmetic formula (`catalogue.suggestion_confidence`), not a model output.

The README explicitly lists "AI-assisted listing analysis" and a "free-text fallback" for products with no structured brand/model data as a **future idea**, not yet built. There is deterministic code across the entire pipeline — grading, catalogue matching, scoring, and suggestion confidence are all closed-form functions operating on text/data with no model boundary to speak of, because no model is called.

## Workers

There is no separate worker framework (no Celery, no task queue, no cron abstraction). Background execution is two independent long-running OS processes, both driven by the CLI:

- **`watch` process** (`cli.cmd_watch`) — an infinite loop. On each 20-second tick it: (1) runs a full search cycle (`runner.run_once`) if the configured `interval_minutes` has elapsed since the last one, and (2) always calls `auction_watch.poll_and_capture` to check any auctions due a price poll. A fresh SQLite connection is opened and closed every tick. Exceptions in a cycle are logged and the loop continues; Ctrl-C stops it cleanly. No persistence of its own schedule state beyond an in-memory `next_full_run` timestamp — restarting the process runs a full search immediately.
- **`web` process** (`cli.cmd_web`) — a Flask development server (`app.run`, `debug=False`). Never performs searches itself; only reads/writes the DB in response to HTTP requests and polls (`/dashboard/live`, `/projects/<id>/live`, `/api/status`) that the browser's own JS drives every 15 seconds.
- **`run-once`** (`cli.cmd_run_once`) — a single foreground search cycle, not a background worker, intended for cron-style external scheduling if desired (not itself scheduled by the app).

Both `watch` and `web` share the same SQLite file concurrently; WAL mode plus a 10-second busy timeout is what makes that safe. There is no retry/backoff logic for the search loop itself beyond the fixed interval, and no distributed or multi-machine worker coordination.

## Storage

- **Database**: SQLite at `data/product_finder.db` (configurable via `db_path`), opened in WAL mode with a 10s busy timeout. Schema is created via a single `CREATE TABLE IF NOT EXISTS` script (`db._SCHEMA`) plus an ordered list of additive column migrations (`db._MIGRATIONS`) applied idempotently on every `connect()` call — there is no separate migration tool or version table. Tables: `projects`, `items`, `products`, `product_price_observations`, `product_suggestions`, `app_settings`, `listings`, `listing_matches`, `alerts_sent`, `source_settings`.
- **Configuration**: `config.yaml` (YAML) is the source of truth for postcode, radius, interval, alerts config, and source *definitions* (built-in enable flags and any `sources.extra` entries). It seeds the DB's `projects`/`items` on first empty run and can be re-imported (merge by slug/name) via `import-config`; after seeding, the DB is authoritative for projects/items, and the YAML is not re-read for them automatically.
- **Reports**: no report-file generation exists. `reports/.gitkeep` is present but empty — the README states explicitly that the web UI *is* the report; there's no generated artefact to open.
- **Caching**: only in-process, ephemeral caching — the eBay OAuth bearer token is cached in-memory on the `EbaySource` instance until 60 seconds before expiry; a module-level `_last_request_at` timestamp throttles RSS requests. No on-disk or cross-process cache layer.
- **Generated artefacts**: none beyond the SQLite file itself (and its WAL/SHM companion files visible in the working tree).

## Web Application

Flask, server-rendered (Jinja2 templates), single process, bound to `127.0.0.1` only, no authentication, no accounts — a `secret_key` exists only to support Flask's flash-message session cookie.

Pages (see `base.html` nav — four top-level links):
- **Dashboard** (`/`) — hero strip of the best current live deals (cards: title, price, saving %, "under target"), a per-project card showing a live preview of that project's current best pick (or a "still watching" placeholder), and demoted tables of everything-else and warnings/false-bargains further down. Polls `/dashboard/live` (a base.html-free fragment render) every 15s via JS and swaps content without a full reload. `/api/status` exposes `last_activity` (max listing `last_seen`) as a cheap "did a search just run" signal.
- **Project detail** (`/projects/<id>`) — hub for one project: a best-deal callout expanding to up to 4 cards when several listings score ≥70 ("hot"), inline item CRUD (add/edit/archive/delete, with terms/prices/priority/source filters), each item's matched listings in a collapsed-by-default, client-side-paginated (10/page) section with source/grade/warning-flag/sort filters, and that project's manual search links. Same 15s live-refresh pattern via `/projects/<id>/live`.
- **Projects** (`/projects`) — create, rename (edit), archive/unarchive, delete projects; set/edit per-project source restriction; an "Import from YAML" action (`/import-config`).
- **Manual searches** (`/manual`) — Gumtree/Facebook/keyless-eBay/link-type source URLs, grouped by project and item.
- **Sources** (`/sources`) — enable/disable any source (built-in or config-defined `extra`), and set eBay API credentials (`app_id`/`cert_id`/`env`) — both take effect on the next search in any process, no restart.

Item CRUD (`/items/new`, `/items/<id>/edit`, archive, delete) and the product catalogue CRUD (`/items/<id>/products/new`, `/products/<id>/edit`, archive, delete) live under the project/item pages rather than as separate top-level sections. Product suggestions are approved/dismissed inline on the item edit page (`/suggestions/<id>/approve|dismiss`), alongside a settable auto-approve confidence threshold (`/catalogue-settings`).

## CLI

All commands accept `-c/--config` (default `config.yaml`) and `-v/--verbose` (debug logging).

| Command | Effect |
|---|---|
| `run-once` | Runs a single full search cycle across all projects/items/sources, alerts on new matches, prints a summary (automated sources searched, count of manual-assisted search links available). |
| `watch` | Runs continuously: a full search cycle every `interval_minutes`, plus an auction-close poll every 20 seconds, until Ctrl-C. |
| `import-config` | Merges the YAML `projects:`/`items:` section into the DB (upsert by project slug / item name within a project). |
| `list-projects` | Prints each project's slug, name, and item count. |
| `list-items` | Prints each project's items with price/priority summary and search terms. |
| `web` | Starts the Flask UI (`-p/--port`, default 8765). |

## Reporting

There is no generated report file (PDF, CSV, static HTML, etc.) anywhere in the codebase. "Reporting" is entirely the live web UI — the dashboard and project-detail pages are the only surfaces summarizing results, and they read live from SQLite on every request/poll rather than from any pre-built artefact. The `reports/` directory exists but is empty (`.gitkeep` only).

## Testing

- **Framework**: pytest (`pyproject.toml` configures `testpaths = ["tests"]`, `pythonpath = ["src"]`).
- **Current status**: 170 tests, all passing (verified in this session).
- **Coverage by file**: `test_grading.py` (condition classification), `test_catalogue.py` (product matching, suggestion confidence), `test_scoring.py` (margins, deal score, warning flags, live-auction/used-price adjustments), `test_dedup.py` (listing upsert/dedup behavior), `test_config.py` (YAML loading/validation), `test_sources.py` (largest test file at 538 lines — per-source search/manual-link behavior, including eBay API interaction), `test_price_history.py` (rolling used-price median, observation windowing), `test_auction_watch.py` (cadence tiering, close-detection, give-up logic), `test_suggestions.py` (product-suggestion lifecycle: sighting, corroboration, auto-approve, dismiss), `test_web.py` (largest overall at 722 lines — dashboard, project pages, sources page, project/item CRUD via Flask's test client).
- **Strategy**: unit-level tests around pure functions (scoring, grading, catalogue matching) plus integration-style tests against a real temporary SQLite DB and a real Flask test client for the web layer — no mocking framework in evidence; the eBay HTTP calls appear to be tested via `requests`-level fixtures/monkeypatching rather than a live network dependency (not fully confirmed by inspection depth here, but no test hits the real eBay API).

## Current Capabilities

- Monitor eBay UK automatically via the official Browse API (when credentials are configured), falling back to manual search links otherwise.
- Generate manual-assisted search links for Gumtree UK, Facebook Marketplace, and any config-defined `links`-type source.
- Automatically search any config-defined RSS/Atom feed source, extracting price from entry text and filtering by feed age.
- Group wanted items into projects, each independently restrictable to a subset of sources; items can further narrow that set.
- Manage projects and items entirely through the web UI (create/edit/archive/delete), with the DB as source of truth after initial YAML seeding; re-import from YAML on demand.
- Maintain an optional, per-item catalogue of specific manufacturer/model products, each with its own match terms, MSRP, typical new price, and an automatically-computed rolling 90-day median typical used price.
- Automatically discover new catalogue-product candidates from eBay's structured brand/mpn item-specifics data, with a corroboration-based confidence score, manual approve/dismiss, and an optional auto-approve confidence threshold.
- Classify listing condition into five grades (A/B/C/spares-repair/unknown) from title/condition/description keywords.
- Detect "false bargain" warning flags (faulty, not working, broken, untested, missing battery/charger, incomplete, etc.) from listing text.
- Compute a composite 0–100 deal score blending margin vs. reference price, condition grade, item priority, warning flags, vague-title penalty, false-bargain penalty, and used-price-vs-new-price comparison.
- Detect live (uncommitted-bid) eBay auctions, exclude them from "best deal" hero surfacing and from feeding the used-price index, while still listing them (flagged) for visibility.
- Track live auctions toward their close on a tightening polling cadence and capture a genuine closing ("sold for") price the moment the item goes out of stock.
- Send console alerts and/or outbound webhook POSTs for new (not previously alerted) matches, deduplicated per channel.
- Serve a local, live-refreshing (15s poll) web dashboard and per-project detail pages, with filter/sort controls, pagination, and inline CRUD for projects, items, and catalogue products.
- Enable/disable any source and set eBay API credentials from the web UI, taking effect on the next search cycle in any running process without a restart.
- Run `watch` (continuous) and `web` (UI) as independent, concurrently-safe processes against one shared SQLite (WAL-mode) database.
- Provide CLI commands for one-off runs, continuous watching, config import, and listing projects/items.

## Current Limitations

(As stated in the README's "Known Limitations" and corroborated by the code.)

- One listing can match multiple items if their search terms overlap — no cross-item dedup.
- No de-duplication across sources — the same item listed on eBay and Gumtree (e.g. as a manual link a user separately enters) counts twice.
- `normal_price`/`target_deal_price` (item- or product-level) are user estimates, not market data; margins/scores are only as good as those estimates.
- Product catalogue matching and condition grading are both plain keyword/word-boundary lookups — no fuzzy matching, so typos or unusual phrasing won't resolve.
- "Typical used price" is built mostly from *asking* prices on active listings, not confirmed sold prices — eBay's Marketplace Insights (sold-price) API isn't available to this app's developer account. The one exception is auction-close capture, which is a genuine (if unconfirmed-as-sold) closing price.
- The auction-close poller cannot confirm a sale actually completed (e.g. a reserve-not-met auction is captured as if sold) — it infers "sold" purely from the availability status flipping.
- "Typical new price" is manually maintained; no automated retailer price-watching exists (no public API from retailers, and scraping raises the same ToS issue as Gumtree/Facebook).
- Deal scores are heuristic; a vague or missing description skews grading and scoring.
- `watch` is a simple fixed-interval loop with no backoff or rate limiting beyond the configured interval and the RSS request throttle.
- SQLite database grows indefinitely; no pruning of old listings/observations exists.
- No AI/ML component anywhere; the free-text (non-structured-data) product-suggestion fallback mentioned in the README is explicitly not built.
- No authentication on the web UI (by design, localhost-only) — not suitable for exposing beyond the local machine as-is.
- No report/export artefact generation (CSV, PDF, etc.) — the web UI is the only output surface.

## Repository Structure

```
product-finder/
├── README.md               # primary documentation: vision, config, commands, limitations
├── config.example.yaml     # annotated example configuration
├── config.yaml             # user's actual config (gitignored contents vary)
├── pyproject.toml          # package metadata, dependencies (PyYAML, requests, flask), pytest config
├── data/                   # SQLite DB + WAL/SHM files (product_finder.db)
├── reports/                # empty; present but unused
├── prompts/                # original design prompts (01-initial-cli, 02-web-ui) — historical, not code
├── src/product_finder/
│   ├── cli.py               # argparse entry point / command dispatch
│   ├── config.py            # YAML config loading/validation dataclasses
│   ├── db.py                # SQLite schema, migrations, all CRUD and query functions
│   ├── models.py             # shared dataclasses: Listing, Evaluation, ManualLink, MatchAlert, AuctionSnapshot
│   ├── catalogue.py          # manufacturer/model product matching + suggestion confidence
│   ├── grading.py            # keyword-based condition classification
│   ├── scoring.py            # warning flags, margins, deal score, auction/used-price logic
│   ├── runner.py             # one search cycle: fetch → filter → match → score → persist → alert
│   ├── auction_watch.py      # end-of-auction price capture / polling cadence
│   ├── sources/               # one module per marketplace + the shared Source contract
│   │   ├── base.py, ebay.py, gumtree.py, facebook.py, rss.py, links.py
│   ├── alerts/                 # console.py, webhook.py
│   └── web/                    # Flask app.py + templates/*.html
├── tests/                    # pytest suite, one file per subsystem (see Testing)
└── .daemoncore/              # multi-agent orchestration tooling (not part of the product itself)
```

## Design Philosophy

- **Single-writer-agnostic, concurrency via SQLite WAL** rather than any message bus or lock service — deliberately simple for a local single-user tool.
- **Marketplace-agnostic downstream code**: every source normalises into one `Listing` shape; grading, scoring, catalogue matching, storage, and the web UI never branch on which marketplace a listing came from.
- **Config-first extensibility for low-effort sources**: adding a new RSS or manual-link site is pure YAML, no code; only sites needing real API integration (eBay) get a dedicated `Source` subclass.
- **Narrowing-only filters**: source restrictions compose from enabled sources ∩ project sources ∩ item sources — each level can only narrow what's above it, never widen it.
- **Explicit non-inference where trust matters**: the product-suggestion mechanism only trusts eBay's own seller-declared structured fields, never a guess parsed from free text, and confidence never reaches 100%.
- **Never crash a cycle on one failure**: source search failures, alert-send failures, and per-listing lookup failures are all caught and logged, never propagated to kill a `run_once`/`watch` cycle.
- **DB as source of truth once seeded**, YAML as the one-time seed plus the home for settings that are inherently global (postcode, interval, alerts) — avoids a split-brain between two ways of defining the same project data.
- **Additive-only schema migrations** applied idempotently on every connection — no separate migration runner, no down-migrations, consistent with "small, maintainable codebase."

---

## Answers

**1. If Product Finder continues along its current trajectory, what type of software platform is it becoming?**
A personal, local-first **deal-intelligence and market-tracking platform for second-hand goods** — closer to a self-hosted price-tracking/alerting tool (in the vein of a CamelCamelCamel or a Skyscanner-style watcher) than a generic saved-search aggregator, with its own accumulating price-history dataset (via the used-price index and auction-close captures) becoming a genuine asset independent of any single search. The trajectory so far (catalogue → three-tier pricing → auto-discovery of catalogue products → auction-close capture) points toward an increasingly automated, self-improving reference-price database that needs progressively less manual maintenance (MSRP/new price entry today; the README's own "future ideas" — AI-assisted analysis, free-text product discovery — would be the next step in that direction, though none of that is built yet).

**2. What are the three strongest architectural decisions currently in the codebase?**
- The **`Source` contract normalising everything to a plain `Listing` dataclass**, which keeps grading/scoring/storage/web entirely marketplace-agnostic and makes adding a config-only RSS or link source free of code changes.
- **SQLite WAL mode as the concurrency mechanism** between the independent `watch` and `web` processes — a minimal, dependency-free way to get safe concurrent read/write without a client-server database or message queue, appropriate to the single-user local-first scope.
- **The three-tier product pricing model (MSRP / typical new / typical used)** with the used price computed automatically from the app's own observed listings (median over a rolling window, one observation per distinct listing) — this turns the app's own operation into its own price-data source rather than depending on an unavailable sold-price API, and it's what makes the "above typical used price" scoring signal possible at all.

**3. What architectural debt currently exists?**
- **No cross-source or cross-item deduplication** — the same physical item can appear multiple times (same listing matching overlapping item terms; the same item on two different marketplaces), which the README acknowledges but which will increasingly distort scores/rankings as more sources are added.
- **No data lifecycle/pruning** — the SQLite database grows indefinitely (listings, matches, price observations all accumulate with no retention policy), which will eventually affect query performance and the used-price median's relevance if very old rows are never trimmed.
- **Schema migrations are additive-only, applied ad hoc on every connect()** — workable at the current scale, but there's no versioning or down-migration path, so any future need to rename/restructure a column becomes awkward.
- **`typical_new_price` is entirely manual** with no path yet toward automation (explicitly called out in the README as blocked on retailer API/ToS constraints), so the catalogue pricing model's "new" side doesn't benefit from the same self-updating property as the "used" side.
- **The free-text (non-structured-data) product-discovery fallback is unbuilt**, meaning catalogue coverage is entirely dependent on sellers filling in eBay's brand/mpn fields — casual/private-seller listings (arguably where the best bargains live) are structurally excluded from ever seeding a new catalogue product.
