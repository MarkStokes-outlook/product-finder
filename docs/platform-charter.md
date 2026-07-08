# Product Finder Platform Charter

This charter is the architecture contract for Product Finder. It defines the principles future features must satisfy.

The implementation remains the source of truth for current behaviour. This charter defines how that implementation should continue to evolve.

Product Finder's architectural flow is:

```text
Evidence -> Knowledge -> Intelligence -> Decision -> Action
```

The platform collects evidence, compounds it into reusable knowledge, evaluates it with explainable intelligence, records or proposes decisions, and only then presents or enables action. Future features should strengthen that chain rather than bypass it.

## Platform Invariants

These are properties the platform should not intentionally violate:

- Marketplace evidence is preserved as source evidence. Stored `listings.url` values are not rewritten for affiliate logic; outbound adapters add parameters at navigation time.
- Shared market knowledge is platform-owned. Products, listings, price observations, source telemetry, identity links, auction snapshots, and connector declarations are not duplicated per project as ordinary design.
- Project data expresses intent. Projects, items, item-product tracking, listing matches, alerts, and future saved/ignored feedback interpret shared facts in context.
- Provenance is retained wherever practical. Review decisions, telemetry, duplicate status, price observations, and click audit records should be traceable back to the source evidence that caused them.
- Uncertain automation enters review. AI extraction, duplicate candidates, retailer price candidates, product suggestions, and suspect-product judgements must not silently mutate trusted knowledge when the evidence is weak.
- Objective deal score remains independent of personal preference. Priority and future user taste can rank or filter opportunities, but they must not redefine the objective quality score.
- Connector behaviour is declared through `SourceCapabilities` and `ConnectorKnowledge`. Downstream code should reason over these contracts, not hard-coded marketplace names.
- Marketplace-specific logic belongs in connectors or outbound adapters. Scoring, matching, identity, persistence, and UI query paths should consume normalised facts.
- Search execution stays outside web requests. The web UI reads and mutates state; the watch/run cycle acquires marketplace evidence.
- Database writes must complete before network boundaries. Long API waits, rate-limit backoff, or webhook calls must not hold SQLite writer locks open.

## 1. Evidence Before AI

The platform should prefer direct evidence over inference.

Examples of evidence:

- official API fields
- observed listings
- observed auction snapshots
- human-approved product catalogue entries
- human-approved retailer price candidates
- connector-declared capabilities and limitations
- persisted source health and coverage telemetry

AI may propose evidence candidates, especially from unstructured text, but AI output must enter the same review and provenance model as other uncertain signals. It must not write directly to the catalogue, mutate scores, or hide uncertainty.

## 2. Knowledge Compounds

Every search cycle should improve the platform where possible.

Listings, products, price observations, source runs, auction snapshots, duplicate decisions, and retailer candidates are not incidental by-products. They are the platform's accumulated knowledge.

A future feature should enrich at least one knowledge layer or protect the quality of existing knowledge.

## 3. Platform Owns Market Knowledge

The platform owns shared market facts:

- listings
- global products
- used and new price observations
- source coverage and health
- identity links and duplicate decisions
- auction snapshots
- outbound click audit facts

These facts should not be duplicated per project or user unless there is a clear privacy or correctness reason.

## 4. Projects Own User Intent

Projects and items represent what a person cares about, not what the market is.

Project-owned data includes:

- projects
- watched items
- item-specific source restrictions
- search and exclude terms
- target and maximum prices
- item-specific product tracking through `item_products`
- matches between listings and watched items
- future saved/ignored/feedback decisions

The platform may know a product globally. A project decides whether that product matters in context.

## 5. Explainability Over Black Boxes

A user should be able to understand why a listing was surfaced.

Scores, warnings, health status, auction labels, offer suggestions, and catalogue triage should be built from named rules and visible evidence. Where a confidence score exists, it should be a cue for review, not a hidden authority.

## 6. Compliance Before Convenience

Connector convenience never outranks marketplace compliance and account safety.

Official APIs, authorised feeds, open RSS/Atom sources, and manual-assisted links are preferred. Scraping, user-session automation, and third-party scraping providers may only exist as explicitly modelled risk, never as default behaviour and never disguised as ordinary automation.

High-risk sources require explicit opt-in before scheduled execution.

## 7. Configuration Over Special Cases

Generic RSS and link sources should remain config-defined where possible.

When code is necessary, source-specific behaviour belongs inside connector or outbound adapter contracts. It should not leak into scoring, matching, dashboard queries, or downstream domain logic.

## 8. Connectors Are Plugins

Connectors own acquisition. They fetch or generate source-specific data and emit normalised listings or manual links.

They must declare:

- capability
- compliance basis
- operational risk
- maturity
- known limitations
- knowledge and future notes

The engine should reason over these declarations, not over marketplace names.

## 9. Marketplaces Are Interchangeable Downstream

Downstream code should consume normalised facts:

- `Listing`
- `ManualLink`
- `SourceCapabilities`
- `ConnectorKnowledge`
- `SearchOutcome`

Scoring, matching, identity, storage, and UI surfaces must not become a collection of marketplace branches.

## 10. Incremental Evolution

Product Finder should evolve through independently shippable layers.

Major future capabilities such as authentication, ownership, public discovery, sharing, subscriptions, and API access must land additively. They should not require replacing SQLite, the connector contract, the catalogue model, or the scoring pipeline unless the implementation itself proves those foundations insufficient.

## 11. Architecture Before Optimisation

Do not optimise before ownership, boundaries, and evidence flow are clear.

Performance work should follow measured pressure: source count, query latency, data volume, API rate limits, or UI response time. Optimisation must not obscure the domain model.

## 12. Domain Knowledge Before Commercial Features

Commercial features should sit on top of product and market knowledge.

Affiliate redirects, subscriptions, public search, public API access, templates, or third-party integrations should not distort the core model. They should use the same evidence, connector, catalogue, identity, and decision layers as the local product.

## 13. Human Review Is A Product Primitive

The platform should not pretend uncertain automation is certain.

Human review queues are already part of the architecture:

- product suggestions
- suspect products
- duplicate listings
- retailer price candidates

Future uncertain intelligence should follow this pattern unless there is strong evidence that automatic action is safe.

## 14. Keep Simple Things Simple

The current platform benefits from boring infrastructure:

- Python package
- Flask server-rendered UI
- SQLite with WAL
- deterministic pure functions
- argparse CLI
- pytest

These should remain until real scale, deployment, or collaboration requirements force a change.
