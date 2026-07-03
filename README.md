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
python -m product_finder web             # local web UI at http://127.0.0.1:8765
python -m product_finder report          # regenerate reports/latest.md from stored data
python -m product_finder import-config   # merge YAML projects/items into the database
python -m product_finder list-projects   # show projects
python -m product_finder list-items      # show items
```

All commands accept `-c path/to/config.yaml` (default `config.yaml`) and `-v`
for debug logging. `web` also accepts `--port`.

### Running on a schedule

`watch` and `web` are independent processes — searches must not depend on the
UI being open, and shouldn't block a page load. Run `watch` in the background
alongside `web`:

```bash
nohup python -m product_finder watch > watch.log 2>&1 &
disown
```

Both processes read/write the same SQLite DB concurrently (WAL mode handles
this safely). There's no manual "run now" trigger in the web UI — search only
ever happens in `watch` (or a one-off `run-once`), never inside the web
process; the dashboard just polls for and displays whatever `watch` finds.

For something that survives reboots/logout, wrap the `watch` command in a
launchd LaunchAgent (macOS) or systemd unit (Linux) instead of `nohup`.

---

## Web UI

```bash
python -m product_finder web
```

A local, server-rendered UI at `http://127.0.0.1:8765` — localhost only, no
accounts, no cloud. Pages:

- **Dashboard** — project summaries, best current deals, a warnings/false
  bargains section, and a link to the latest report. Polls every 15s for
  new results (from the background `watch` process) and swaps in fresh data
  without a full page reload. Click a project to open its detail page.
- **Project detail** (click any project) — that project's items, each with
  its own matched-listings table and price/priority context, plus its manual
  search links. Same live auto-refresh as the dashboard.
- **Projects** — create, rename, archive/unarchive, delete.
- **Items** — full editing of every field (terms, exclude terms, prices,
  priority, notes, per-item source filters), grouped by project.
- **Listings** — browse discovered listings; filter by project, item, source,
  grade, and warning flags; sort by deal score, price, or first seen.
- **Manual searches** — the Gumtree/Facebook (and keyless eBay) links grouped
  by project and item.
- **Sources** — enable/disable any source (built-in or config-defined) and
  set eBay API credentials, without touching YAML or restarting anything.

### Where projects and items live

Once seeded, **the database is the source of truth for projects and items** —
edit them in the web UI, not the YAML. The YAML remains the place for
settings: postcode, radius, interval, and alerts.

- On first run (CLI or web) an empty database is seeded from the YAML
  `projects:` section automatically.
- To re-import after editing the YAML by hand, run
  `python -m product_finder import-config` or use the **Import from YAML**
  button on the Projects page. Import merges by project slug and item name,
  overwriting those items' fields.
- Archived projects/items are kept but excluded from searches, reports, and
  manual links.

### Where sources live

Unlike projects/items, source *definitions* (URL templates, type) always come
from `sources.extra` in YAML — no import step, no duplication into the DB.
Add a new endpoint to `config.yaml` and it appears on the Sources page
immediately. Only two things can be overridden in the DB, via the Sources
page: whether a source is **enabled**, and **eBay API credentials**
(`app_id`/`cert_id`/`env`) — both take effect on the very next search, in any
process (`web`, `watch`, `run-once`), no restart needed.

---

## Adding Sources

Every source implements one small contract (`src/product_finder/sources/base.py`):
`name`, `is_automated()`, `search(term, item)` and `manual_links(item)`. All
downstream logic (grading, scoring, dedup, alerts, reports, web UI) only sees
normalised `Listing` objects, so it never knows or cares where they came from.

Most new sites need **no code at all** — add them under `sources.extra` in
`config.yaml`:

```yaml
sources:
  extra:
    - name: hukd                # automated: RSS/Atom feed per search term
      type: rss
      label: HotUKDeals
      url: "https://www.hotukdeals.com/rss/search?q={term}"
    - name: johnpye             # manual-assisted: pre-filtered search links
      type: links
      label: John Pye Auctions
      url: "https://www.johnpye.co.uk/?s={term}"
```

Templates may use `{term}`, `{max_price}`, `{postcode}` and `{radius}`.
RSS entries must mention a `£` price in the title or description; entries
without one are skipped (scoring needs a price). Items can target extra
sources by name in their `sources:` filter, and they appear in the web UI
like any built-in.

Sites needing a real API integration (like eBay) get a subclass of `Source`
registered in `sources/__init__.py` — one file plus one registry line.

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

There's no separate HTML report file — the **project detail page** in the web
UI (click a project anywhere in the UI) shows the same thing live: each
item grouped with its matched listings, colour-highlighted the same way
(green for excellent deals, red for spares/repair or flagged listings), plus
that project's manual search links. It updates itself automatically as
`watch` finds new results — no regenerating, no opening a file.

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