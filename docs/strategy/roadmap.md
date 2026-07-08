# Product Finder Platform Roadmap

This roadmap describes platform evolution, not implementation chronology.

Historical implementation order remains in `docs/implementation-notes/` and the ADRs. This document answers: where should future work belong, and what architectural asset does it strengthen?

## Current Strategic Position

Product Finder has evolved beyond a bargain finder.

Current state:

- local-first single-operator app
- compliant connector model
- global product catalogue
- project-owned intent
- identity and duplicate foundations
- auction and offer intelligence
- source health and coverage telemetry
- optional Ollama extraction into review
- optional SearXNG retailer price candidates
- marketplace outbound gateway with click tracking

The next stage is platform consolidation: keep expanding knowledge without weakening evidence, compliance, or ownership boundaries.

## Foundations

Shipped foundations:

- CLI and Flask local runtime
- SQLite/WAL persistence
- DB-backed projects/items
- connector contract
- source settings overlay
- global catalogue split (`products` + `item_products`)
- outbound gateway
- source telemetry
- import/export v1

Future direction:

- authentication and ownership
- explicit public/private route boundary
- retention and maintenance policies
- stronger migration/audit discipline for shared global data

Deliberately deferred:

- replacing SQLite before scale requires it
- distributed workers before orchestration policies need them
- public deployment before ownership and public-safe filtering exist

## Core Engine

Current state:

- `runner.run_once()` owns the search/match/score/persist/alert cycle
- `SearchOrchestrator` executes work items through an execution policy
- default execution preserves deterministic sequential behaviour
- scoring is objective and explainable

Future direction:

- health-aware connector selection
- retry/backoff policies per source
- per-connector cadence
- concurrency only after write boundaries are designed
- clearer event boundaries if external workers appear

## Catalogue

Current state:

- global products
- item-specific tracking via `item_products`
- deterministic matching by match terms
- structured eBay product suggestions
- optional Ollama fallback into the same review queue
- suspect/accessory/brand-only triage
- knowledge-only product state

Future direction:

- richer product attributes
- categories and variants
- accessory/spare/bundle classification
- compatibility and alternatives
- global product edit/merge audit
- moderation model before multi-user global edits

## Identity Resolution

Current state:

- canonical URL identity v1 for provable source-native ids
- cross-marketplace fuzzy duplicate proposals for human review
- same-marketplace fuzzy duplicate proposals are deliberately not generated
- duplicate suppression uses `is_primary_sighting`, not deletion

Future direction:

- seller identity as connector capabilities mature
- perceptual image matching
- relist and cross-post detection
- duplicate-group modelling if pair decisions become insufficient
- automatic resurfacing when the kept listing ends

## Connectors

Current state:

- eBay official API
- Gumtree/Facebook manual links
- config-defined RSS and links
- capability/risk/knowledge declarations
- health and coverage analytics

Future direction:

- more official/API/feed connectors
- careful Gumtree/Facebook evaluation through the options paper
- Reverb or other official APIs where terms permit
- used-retailer and clearance feeds
- auction house/liquidation feeds
- source-specific enrichment behind generic capabilities

Never:

- silent scraping as ordinary automation
- user-session connector scheduled in the background
- downstream marketplace special cases

## Marketplace Abstraction

Current state:

- inbound connector contract
- outbound marketplace gateway
- query-param affiliate adapter
- click audit table

Future direction:

- source-specific outbound adapters where needed
- affiliate revenue reporting
- public click-through analytics
- per-user click attribution after ownership lands

Deferred:

- manual-search-link click tracking until there is a clear identity/context model
- anonymous session identifiers until public/session architecture exists

## Import And Export

Current state:

- `import-config` for YAML seed merge
- `product-finder/import/v1` for project/item import/export
- two-phase preview/apply validation

Future direction:

- template library
- community templates
- richer validation and previews
- import/export of project intent only unless a separate backup format is designed

Deferred:

- exporting accumulated platform market evidence as project files

## Knowledge Expansion

### Product Intelligence

Current:

- manufacturer/model catalogue
- match terms
- global product dedupe by model key
- triage queues

Future:

- product categories
- variant families
- compatibility relationships
- accessory/spare/consumable distinction
- bundle decomposition

### Market Intelligence

Current:

- typical used price from observations
- auction close observations
- retailer price candidates and refresh
- used-price trend

Future:

- confidence intervals
- source-weighted market estimates
- sold-price integrations where legitimate
- new-price trend scoring
- seasonal and volatility signals

### Seller Intelligence

Current:

- not modelled beyond location and source payloads

Future:

- connector-declared seller identity fields
- seller reputation and consistency
- repeated seller detection
- scam/trust indicators

Deferred:

- trust score before seller data exists

### Listing Intelligence

Current:

- condition grading
- warning flags
- spec/category conflicts
- live auction detection
- offer support
- auction trajectory

Future:

- richer condition parsing
- listing confidence
- image evidence
- stock-photo/reused-image detection
- stale listing lifecycle

### Decision Intelligence

Current:

- deal score
- under-target
- warning surfaces
- auction and offer suggestions
- duplicate/product review decisions

Future:

- saved/ignored/shortlisted decisions
- recommendation explanations
- buy/wait/alternative guidance
- personal priority-aware ranking

### Coverage

Current:

- source coverage metrics
- source health
- catalogue match rates by source
- stale-rate visibility

Future:

- coverage targets by product/category
- market confidence based on source mix
- connector value ranking
- source quarantine/throttling when low-quality

### Trust

Current:

- connector risk model
- warning flags
- compliance declarations

Future:

- listing trust
- source trust
- seller trust
- provenance and contradiction tracking

## Intelligence

Recommendations are the final layer, not a shortcut.

Current intelligence:

- rule-based scoring
- trajectory labels
- offer suggestions
- triage/review queues

Future intelligence:

- recommendations: buy, wait, ignore, compare
- compatibility: works with this project/context
- alternatives: equivalent or better product
- bundles: decompose and price multiple items
- trends: rising/falling market
- pricing: confidence and fair-value ranges
- forecasting: likely auction close or future price direction

Rule:

Recommendation logic should consume catalogue, identity, market, trust, coverage, and project signals. It should not reimplement or bypass them.

## Experience

Current:

- local dashboard
- project detail pages
- sources, catalogue, auctions, offers, manual links
- import/export UI

Future:

- project templates
- calmer review workflows
- public search/browse
- sharing and cloning
- notification routing
- better explanation surfaces

Deferred:

- real-time collaboration
- public exposure before auth/ownership

## Commercial

Current:

- outbound gateway and affiliate parameter injection
- click audit table

Planned:

- Authentik/OIDC authentication
- user ownership
- public read-only discovery
- saved signed-in projects
- sharing/invites/cloning

Future:

- subscriptions
- affiliate reporting
- API access
- entitlements

Rule:

Commercial layers must not distort market knowledge. They should attach to the platform through ownership, outbound, and public/private boundaries.

## Ecosystem

Future candidates:

- mobile companion
- browser extension
- webhook API
- third-party integrations
- community templates
- public catalogue
- public product pages

These belong after the knowledge and ownership layers are stable enough to expose.

## Principles For Sequencing

Prefer work that:

- improves evidence quality
- increases compliant coverage
- reduces duplicate/noisy knowledge
- clarifies ownership boundaries
- makes future features additive
- preserves current local-first usefulness

Defer work that:

- requires public exposure before authorization exists
- automates high-risk sources without explicit opt-in
- adds intelligence without evidence
- adds commercial mechanics before domain knowledge supports them
- introduces infrastructure complexity without measured pressure
