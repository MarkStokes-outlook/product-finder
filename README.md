# Product Finder

## Vision

Product Finder is a lightweight application that helps users discover genuine bargains across second-hand marketplaces.

Unlike traditional saved searches, Product Finder understands projects, expected market value and product condition, allowing it to distinguish between a true bargain and a listing that only appears cheap because it is faulty or incomplete.

The initial focus is renovation tools for **The Coachhouse**, but the application is designed to support any collection of wanted items.

---

## Goals

- Monitor multiple marketplaces from a single application.
- Organise searches into projects.
- Alert only on new, relevant listings.
- Estimate whether a listing represents good value.
- Identify likely false bargains.
- Keep configuration simple.
- Run locally with minimal setup.

---

## Core Concepts

### Projects

Projects group related wanted items.

Examples:

- The Coachhouse
- Spain Villa
- Homelab
- Camera Gear
- Car Parts

### Wanted Items

Each item defines:

- Search terms
- Maximum purchase price
- Expected market value
- Target deal price
- Preferred sources
- Search radius
- Priority

### Deal Intelligence

For every matching listing the application should estimate:

- Expected market value
- Saving (£)
- Saving (%)
- Deal score
- Condition grade
- Warning flags

---

## Condition Classification

Listings should be automatically classified where possible:

- Grade A — Excellent or nearly new.
- Grade B — Good used condition.
- Grade C — Heavy wear but serviceable.
- Spares / Repair — Faulty, incomplete or non-working.
- Unknown — Insufficient information.

Listings that appear inexpensive because they are faulty should receive a poor deal score.

---

## MVP Scope

The first version should:

- Support multiple projects.
- Search eBay automatically where practical.
- Support Gumtree where practical.
- Generate manual-assisted searches for Facebook Marketplace if automation is not appropriate.
- Store seen listings locally.
- Produce Markdown reports.
- Provide console alerts.
- Calculate a simple deal score.

The objective is a useful tool, not a polished product.

---

## Future Ideas

Potential future enhancements include:

- AI-assisted listing analysis.
- Historical pricing trends.
- Image analysis for condition.
- Automatic duplicate detection across marketplaces.
- Browser UI.
- Mobile notifications.
- Seller reputation scoring.
- Team-shared projects.

These ideas are intentionally out of scope for the MVP.

---

## Design Principles

- Working software over perfect software.
- Simple configuration.
- Local-first.
- Compliant with marketplace terms of service.
- Small, maintainable codebase.
- Easy to extend.

---

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml
# edit config.yaml — postcode, projects, items, prices
```

### eBay API keys (optional but recommended)

Automated eBay search uses the official
[eBay Browse API](https://developer.ebay.com). Create a free developer
account, generate production keys, and add them to `config.yaml`:

```yaml
sources:
  ebay:
    enabled: true
    app_id: "YourAppID"
    cert_id: "YourCertID"
```

Without keys, eBay falls back to manual-assisted search links, the same as
Gumtree and Facebook Marketplace.

---

## Configuration

See `config.example.yaml` for a complete annotated example. The essentials:

```yaml
postcode: "BL0"
radius_miles: 30
interval_minutes: 60

projects:
  - name: "The Coachhouse Tools"
    slug: "coachhouse-tools"
    items:
      - name: "Track Saw"
        terms: ["track saw", "Makita SP6000"]
        max_price: 400          # hard filter — listings above are ignored
        normal_price: 500       # expected market value, used for margin
        target_deal_price: 300  # at or below this = deal
        priority: high          # high | normal | low
        exclude_terms: ["toy"]  # drop listings whose title contains these
        # sources: [ebay]       # optional per-item source filter
```

---

## Commands

```bash
python -m product_finder run-once        # one search cycle, alert on new matches
python -m product_finder watch           # run continuously at interval_minutes
python -m product_finder report          # regenerate reports/latest.md from stored data
python -m product_finder report-html     # regenerate reports/latest.html from stored data
python -m product_finder list-projects   # show configured projects
python -m product_finder list-items      # show configured items
```

All commands accept `-c path/to/config.yaml` (default `config.yaml`) and `-v`
for debug logging.

---

## Source Limitations

| Source | Mode | Notes |
|---|---|---|
| eBay UK | Automated (Browse API) | Needs free developer keys; GB-located listings only; distance filtering is not applied by the API (postcode is used in manual links). |
| Gumtree UK | Manual-assisted | No official public API and scraping is against their terms. Pre-filtered search links are generated instead. |
| Facebook Marketplace | Manual-assisted | Login-walled, no public API. Search links are generated instead. |

No source bypasses logins, CAPTCHAs or bot protection. Source failures are
logged and never crash a run.

## Grading Limitations

Grading is keyword-based on title + condition + short description:

- Sellers describing condition vaguely (or not at all) grade as **Unknown**.
- Sarcasm, typos and unusual phrasing are not understood.
- "Spares/repair" keywords always win — a "like new but faulty" listing is
  graded spares/repair.
- eBay short descriptions are truncated, so some condition detail is missed.

Treat the grade as a triage hint, not a verdict — always read the listing.

---

## Example Report

`reports/latest.md` groups matches by project, then item, best deals first:

```markdown
## The Coachhouse Tools

### Track Saw

Normal price: £500 · Target deal price: £300 · Priority: high

| Score | Title | Price | Margin | % below | Grade | Flags | Source | First seen |
|---|---|---|---|---|---|---|---|---|
| 100 | [Makita SP6000 track saw, boxed](https://…) | £245 ✅ | £255 | 51% | A | — | ebay | 2026-07-02 |
| 25 | [Festool TS55 — faulty, spares](https://…) | £90 ✅ | £410 | 82% | spares/repair | spares or repairs, faulty | ebay | 2026-07-02 |
```

✅ = at or under target deal price. A **Manual searches** section at the end
lists pre-filtered links for the non-automated sources.

An HTML version (`reports/latest.html`) is generated alongside the Markdown
report on every run — same data, with colour highlighting: green rows for
excellent deals (score ≥ 70, no warnings), red rows for spares/repair or
flagged listings, and an "under target" badge on prices at or below the
target deal price. Open it in a browser; there is no server and no
JavaScript.

---

## Known Limitations

- One listing can match multiple items if their search terms overlap.
- `normal_price` is your estimate, not market data — margins are only as good
  as the estimate.
- Deal scores are heuristic; a vague title or missing description skews them.
- No de-duplication across sources (the same saw on eBay and Gumtree counts
  twice).
- Watch mode is a simple loop — no scheduling, back-off, or rate limiting
  beyond the configured interval.
- SQLite database (`data/product_finder.db`) grows indefinitely; no pruning
  yet.

---

## Tests

```bash
pytest
```

Covers grading, scoring, deduplication and config loading.