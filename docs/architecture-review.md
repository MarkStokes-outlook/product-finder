# Architecture Review

This review assesses Product Finder as a platform expected to evolve over the next 3-5 years.

## Exceptionally Strong Decisions

### Connector Contract And Normalised Listings

The `Source` contract is the strongest boundary in the system.

It keeps marketplace-specific acquisition behind connectors and gives the rest of the platform one listing shape to reason over. The later capability, knowledge, risk, and health declarations extend the same idea rather than replacing it.

This decision should not change.

### Global Catalogue Split

Moving product identity and market facts into global `products`, with item-specific context in `item_products`, is the right long-term platform move.

It aligns the implementation with the ownership boundary: the platform owns market knowledge; projects own intent.

This is now a core platform invariant.

### SQLite/WAL Local-First Runtime

For the current stage, SQLite plus WAL is a good fit.

It avoids infrastructure overhead while supporting independent `watch` and `web` processes. It is simple enough for the operator to run and inspect.

Do not replace it until measured scale, deployment, or multi-user operational needs require it.

### Human Review Queues

Product suggestions, duplicate candidates, suspect products, and retailer price candidates all follow the same pattern: machine proposes, human decides, decision persists.

This is a mature architectural instinct. It avoids pretending uncertain extraction is certain.

### Marketplace Outbound Gateway

Centralising outbound listing clicks behind `/out/<listing_id>` is stronger than an affiliate-only patch.

It creates a general extension point for affiliate logic, audit, safety checks, and future attribution without mutating stored listing URLs.

### Explainable Deterministic Intelligence

Scoring, grading, spec conflicts, auction trajectory, offer suggestions, price trends, and connector health are all explainable and testable.

That matters more than a cleverer black-box score at this stage.

## Principles That Should Never Change

- Evidence before AI.
- Compliance before convenience.
- Connectors hide marketplace-specific acquisition.
- Downstream logic consumes normalised facts.
- Products/listings are shared market knowledge.
- Projects/items express user intent.
- Human review is the default for uncertain automation.
- Scores must remain explainable.
- Stored provenance should not be destroyed casually.
- Future commercial layers must not distort the core domain model.

## Architecture That Should Evolve

### Ownership And Authorization

The current local/no-auth model is correct for now, but public or multi-user use requires explicit ownership.

The planned `projects.owner_user_id` boundary is sound. The high-risk part will be route and query auditing, not schema design.

### Connector Scheduling

The `ExecutionPolicy` seam exists, but default execution is still sequential and health-unaware.

As connector count grows, scheduling should evolve through policy objects: backoff, source health, cadence, and eventually concurrency.

### Source Trust

Connector risk and health exist, but listing/source trust is not yet a first-class domain model.

Seller identity, reused images, source freshness, and suspicious pricing should eventually feed a trust layer distinct from deal quality.

### Recommendation Layer

Recommendations should emerge after catalogue, identity, market, trust, coverage, and project signals are mature.

Do not build recommendations as a parallel scoring shortcut.

### Data Lifecycle

Rows are mostly retained indefinitely.

That is acceptable while local and small, but a platform needs a lifecycle policy: stale listings, old source runs, obsolete matches, old price observations, and backups.

## Intentionally Simple And Should Remain Simple

- Flask server-rendered UI.
- Argparse CLI.
- PyYAML/request/Flask dependency set.
- Pure function domain modules.
- Config-defined RSS/link sources.
- Additive migrations where possible.
- Local-first setup.
- Manual-assisted connectors for risky marketplaces.
- Review queues rather than autonomous writes.

## Where Complexity Should Be Resisted

### Premature SaaS Infrastructure

Do not introduce a service mesh, queue system, distributed workers, external database, or frontend framework just because the product is becoming more strategic.

The current bottlenecks are knowledge quality and source coverage, not infrastructure fashion.

### Marketplace-Specific Downstream Branches

If a scoring rule says "if source == ebay", it is probably wrong. Add capabilities or normalised fields instead.

### AI Ownership Of Decisions

AI should not own catalogue writes, deal scores, or recommendations without explainable evidence and review.

### Commercial Features Before Domain Maturity

Subscriptions, APIs, public pages, and sharing are valid future layers, but they should not come before ownership boundaries and knowledge quality.

### Over-Modelling Users Too Early

The future user model should attach ownership to projects. Avoid adding per-user variants of platform-owned facts unless a real product need exists.

## Main Architectural Risks

- Global product edits now have wider blast radius than the UI fully explains.
- Global product merge/delete operations lack audit/undo.
- Public exposure would currently leak too much operational/admin surface.
- Connector count may outgrow sequential search.
- Stale data may eventually distort surfaces and queries.
- Seller/trust data is absent, limiting recommendation confidence.
- Duplicate handling is deliberately conservative and may miss real duplicates.

## Overall Assessment

The architecture is stronger than the exploratory implementation history suggests.

The strongest pattern is consistent separation:

- acquisition vs interpretation
- market knowledge vs project intent
- evidence vs inference
- proposal vs human decision
- inbound connector vs outbound gateway

Future work should protect these separations.
