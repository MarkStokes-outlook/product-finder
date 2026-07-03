# Roadmap

## Current State

Product Finder has evolved beyond a marketplace watcher into a local-first product intelligence platform.

The current implementation provides:

- Product catalogue
- Marketplace connectors
- Historical used-price observations
- Auction tracking
- Web UI
- Deal scoring
- Product discovery
- Manual catalogue approval

Future work should strengthen the intelligence layer rather than simply adding more marketplaces.

---

# Phase 1 — Catalogue Foundation

## Aliases

Allow canonical products to own aliases for:

- Product names
- Manufacturer spellings
- Model variants
- Common abbreviations

Aliases should improve search generation, product matching and duplicate detection.

---

## Product Merge

Allow multiple discovered products to be merged into a single canonical catalogue entry.

Preserve:

- Price observations
- Listing history
- Suggestions
- Relationships

---

## Catalogue Hygiene

Improve discovery quality by:

- Manufacturer normalisation
- Rejecting junk brands
- Better model validation
- Duplicate suggestion detection

---

# Phase 2 — Intelligence

## Ollama

Use Ollama to assist—not replace—human decisions.

Examples include:

- Manufacturer/model extraction
- Bundle identification
- Product comparison
- Merge suggestions
- Alias suggestions

AI should never directly modify the catalogue.

---

## Bundle Detection

Represent listings containing multiple valuable items.

Examples:

- Tool + batteries
- PC component bundles
- Camera kits

Estimate bundle value separately from the primary product.

---

## Duplicate Detection

Detect when listings represent the same product across:

- Multiple marketplaces
- Multiple search terms
- Slight title variations

---

# Phase 3 — Pricing

## Retail Pricing

Track:

- MSRP
- Typical retail
- Typical used
- Auction close prices

Improve confidence in deal scoring.

---

## Price Intelligence

Learn pricing over time.

Support:

- Historical trends
- Seasonal pricing
- Confidence levels
- Outlier detection

---

# Phase 4 — Product Knowledge

Expand the catalogue beyond pricing.

Products should understand:

- Categories
- Variants
- Accessories
- Consumables
- Replacement parts
- Compatibility

---

# Phase 5 — Recommendations

Move from "watching listings" to making buying recommendations.

Examples:

- Buy now
- Wait
- Better alternative
- Better value bundle
- Rare opportunity

---

# Phase 6 — Automation

Improve long-running operation.

Examples:

- Smarter workers
- Maintenance tasks
- Data retention
- Scheduled summaries
- Notification improvements

---

# Future Ideas

Interesting ideas that are intentionally not scheduled.

- Browser extension
- Mobile app
- Cloud sync
- Multi-user support
- Public API

---

# Guiding Principle

The roadmap guides architectural evolution.

Real-world usage should always take priority over the roadmap.

If using Product Finder reveals a better direction, follow the user experience.