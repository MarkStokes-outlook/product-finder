# Product Finder Vision

Product Finder exists to help people make better buying decisions in messy second-hand markets.

It started as a practical tool for watching marketplaces for renovation tools, but the product has moved beyond saved searches and simple bargain alerts. Its real purpose is to build a growing body of market knowledge: what products exist, how to recognise them, what they usually cost, where they appear, and when a listing is genuinely worth acting on.

## The Problem

Second-hand marketplaces are noisy.

A normal saved search can tell you that a keyword appeared. It cannot reliably tell you:

- whether the listing is the product you actually want
- whether the price is good for that specific product
- whether the price is low because the item is faulty, incomplete, stale, or ambiguous
- whether the same opportunity has already appeared somewhere else
- whether an auction is still worth watching
- whether a source is trustworthy, fresh, or useful

The buyer is left to repeat the same judgement manually across marketplaces, products, and projects.

Product Finder turns those repeated judgements into reusable knowledge.

## Who It Is For

Current state:

Product Finder is a local-first, single-operator tool for a technically capable user who wants a personal buying radar across second-hand and clearance markets.

Future direction:

The same architecture can support public discovery and signed-in saved projects, but that is not the current product. Authentication, user ownership, public search, sharing, subscriptions, and API access are future platform layers, not present-day assumptions.

## What Makes It Different

Product Finder is not just a marketplace aggregator.

It combines:

- user intent, through projects and wanted items
- marketplace coverage, through compliant connectors
- product knowledge, through a shared manufacturer/model catalogue
- identity knowledge, through canonical and reviewed duplicate detection
- price knowledge, through observed used prices, retailer candidates, auction closes, and trends
- decision knowledge, through scoring, warnings, auction trajectory, and offer suggestions

The system prefers evidence over guesswork. Deterministic rules, source-declared capabilities, human review queues, and explicit provenance matter more than opaque automation.

AI may assist with unstructured extraction, but it does not own decisions. It proposes into review flows.

## What It Should Become

Product Finder should become a market knowledge and buying-intelligence platform.

The enduring product direction is:

- see more of the market through more compliant connectors
- understand products better through a global catalogue
- accumulate reusable price and identity evidence
- separate objective deal quality from personal priority
- explain why a listing is, or is not, worth attention
- support public discovery and personal saved projects without compromising the ownership boundary

The long-term asset is not a single source integration. It is the platform's accumulated knowledge about products, prices, listings, sources, and buying decisions.

## Explicitly Out Of Scope

Product Finder should not become:

- a scraping-first system that bypasses login walls, CAPTCHAs, or marketplace terms
- a black-box AI recommender whose reasoning cannot be inspected
- a marketplace itself
- a payment processor
- an automated purchasing or seller-messaging bot
- a general inventory-management or ecommerce platform
- a speculative SaaS rewrite that discards the working local-first engine
- a feature pile where every marketplace gets bespoke downstream logic

Future commercial features must serve the knowledge platform. They should not become the reason the architecture exists.
