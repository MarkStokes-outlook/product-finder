# Implementation notes — connector framework v1

**Date:** 2026-07-04
**Scope:** `sources/base.py` (contract), all five connector implementations,
`sources/__init__.py`, `runner.py`, `db.py` (new `source_runs` table),
`web/app.py` + `sources.html`, `tests/test_connectors.py` (new)
**Status:** shipped, 332 tests passing (8 new). First deliverable of the
roadmap's "Market coverage and marketplace connectors" phase.

## Intent

Optimise for *adding many compliant connectors over the project's lifetime* —
not for any single marketplace. That translated into three concrete changes:
make every connector declare what it is (capabilities), make the engine
reason over those declarations instead of marketplace identities (no special
cases), and make connector health observable so weak sources can be spotted
and dealt with as coverage grows.

## 1. Declared capabilities (`SourceCapabilities` on the `Source` contract)

Every connector now implements `capabilities()` — it's abstract, so a new
connector *cannot* be added without stating what it is:

- `automated` vs manual-assisted (the two first-class connector classes from
  the roadmap).
- `compliance` — mandatory prose stating the legitimate basis the connector
  operates on ("official eBay Browse API", "open RSS/Atom feed",
  "manual-assisted links only (terms prohibit scraping)"). Rendered on the
  Sources page, and a test asserts it's non-empty for every connector — the
  ToS-compliance constraint is now part of the contract, not tribal
  knowledge.
- `supports_enrichment`, `provides_images`, `provides_end_time`,
  `provides_structured_attributes`, `notes` — deliberately only fields the
  engine or UI actually uses today; the dataclass grows when real uses
  appear, not speculatively.

`is_automated()` remains, defaulting to `capabilities().automated`. eBay
overrides it: *declared class* (automated connector) is static, *operational
readiness* (credentials configured) is not. The Sources page distinguishes
these ("needs credentials" badge).

## 2. No marketplace special cases in the engine

The runner previously did `isinstance(source, EbaySource)` to decide whether
a listing could be detail-enriched for product discovery — exactly the
"special case" the roadmap's new boundary forbids. `get_item_details()` is
now part of the base contract and the runner offers enrichment to any
connector declaring `supports_enrichment`. A future Reverb/retailer connector
with a detail endpoint gets product discovery for free.

Consequence for tests: fakes now declare capabilities instead of overriding
`is_automated`, and the old `mock.patch("runner.EbaySource")` indirection is
gone.

## 3. Connector health recording (`source_runs`)

`runner.run_once()` accumulates per-connector outcomes (searches, listings
returned, errors, last error message) and writes one `source_runs` row per
connector per cycle. `db.source_health()` summarises: last run, last
success, consecutive failing runs, 24-hour ingest volume. The Sources page
grew Class/Compliance and Health columns (ok / failing ×N with the error, or
"not yet run"; manual-assisted connectors show a dash).

Retention is handled where the data is created: rows older than 30 days are
pruned on write (`_SOURCE_RUN_RETENTION_DAYS`), per the roadmap's
"opportunistic health" principle — this table can't become the unbounded
growth problem the roadmap warns about.

This is the seed of the roadmap's coverage metrics (listings/day by source,
active/failing sources, freshness). More metrics (catalogue match rate by
source, suggestion yield by source) can join as queries over data that
already exists — no new collection needed.

## Adding a connector now looks like

1. YAML-only for anything reachable as an RSS/Atom feed or URL-template
   links source (unchanged — still zero code).
2. A new class for anything richer: implement `capabilities()` (compliance
   statement mandatory), `search()` or `manual_links()`, optionally
   `get_item_details()` — and register it in `build_registry()`/
   `build_all()`. Health recording, enrichment offers, Sources-page display,
   and the runner's failure isolation all come from the framework.

## Deliberately not done

- No connector scheduler changes — the watch loop still searches every
  enabled connector each cycle; per-connector cadence/back-off scheduling is
  future "Keeping the system healthy" work once connector count justifies it
  (adaptive rate limiting within a cycle already exists).
- No new marketplaces in this change — the framework was the task. Candidate
  connectors (Reverb, used-retailer feeds, auction houses) each start with a
  ToS check, per the roadmap.
- No seller-identity capability field yet — nothing consumes seller data
  until the trust layer starts; the field joins `SourceCapabilities` when it
  has a reader.

## Operational notes

- `source_runs` is created by the schema on next connect — no manual
  migration step.
- Mark's `watch` process needs a restart to start recording health;
  the `web` process needs one to show the new Sources page columns.
