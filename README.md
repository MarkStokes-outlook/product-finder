# Product Finder

Product Finder is a local-first market knowledge and buying-intelligence platform for second-hand and clearance markets.

It watches compliant marketplace sources, stores source evidence, builds reusable product and market knowledge, evaluates listings with explainable rules, and presents the opportunities most worth acting on. The current app is a single-operator Python/Flask/SQLite system; public, authenticated, and commercial layers are future roadmap work.

## Architecture

The platform architecture reached its first stable baseline on 9th July 2026.

Core references:

- Vision
- Platform Charter
- Architecture
- Knowledge Model
- Roadmap

## Canonical Documents

Use this README for setup and day-to-day operation. Use the canonical docs for product and architecture context:

- [VISION.md](VISION.md) — product purpose and boundaries.
- [ARCHITECTURE.md](ARCHITECTURE.md) — current runtime architecture and canonical implementation model.
- [docs/platform-charter.md](docs/platform-charter.md) — platform principles and invariants.
- [docs/knowledge-model.md](docs/knowledge-model.md) — evidence, knowledge, intelligence, and decision model.
- [docs/strategy/roadmap.md](docs/strategy/roadmap.md) — future platform evolution.

Related architecture references:

- [docs/platform-domain-model.md](docs/platform-domain-model.md) — ownership boundaries.
- [docs/connector-architecture.md](docs/connector-architecture.md) — connector contract and source compliance model.
- [docs/documentation-audit.md](docs/documentation-audit.md) — canonical vs historical documentation status.

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
    # sources: [ebay, gumtree]  # optional project-level source filter —
    #                           # restricts every item below, e.g. no point
    #                           # searching CeX for power tools
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

- **Dashboard** — built to answer "what should I grab right now?" at a
  glance, not just list everything found. A hero strip surfaces the best live
  deals as cards (title, price, saving %, "under target") — not buried in a
  table row. Below that, each project card shows a live preview of its
  current best pick, or "still watching — no matches yet" if it hasn't found
  one, so scanning the page tells you what's happening per-project without
  clicking in. Everything else and warnings/false bargains are demoted to
  plain tables further down. Polls every 15s for new results (from the
  background `watch` process) and swaps in fresh data without a full page
  reload.
- **Project detail** (click any project) — the hub for that project: a
  best-deal callout that expands to up to 4 cards when several listings are
  "hot" (score 70+) at once, its items (add/edit/archive/delete inline, with
  terms, prices, priority and source filters), and their matched listings
  with filter/sort controls (source, grade, warnings, sort — previously a
  separate Listings page). Each item's listings sit in a collapsed-by-default
  section (click to expand) showing a one-line preview of its count and best
  price, and are paginated 10 at a time — so a project with hundreds of
  listings per item stays scannable instead of turning into one giant
  scroll. Items and listings live here rather than on separate pages, since
  both only make sense in the context of a project. Same live auto-refresh
  as the dashboard, plus its manual search links.
- **Projects** — create, rename, archive/unarchive, delete, and set which
  sources apply to the whole project.
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
- Archived projects/items are kept but excluded from searches, the dashboard,
  and manual links.

### Where sources live

Unlike projects/items, source *definitions* (URL templates, type) always come
from `sources.extra` in YAML — no import step, no duplication into the DB.
Add a new endpoint to `config.yaml` and it appears on the Sources page
immediately. Only two things can be overridden in the DB, via the Sources
page: whether a source is **enabled**, and **eBay API credentials**
(`app_id`/`cert_id`/`env`) — both take effect on the very next search, in any
process (`web`, `watch`, `run-once`), no restart needed.

Which sources actually get searched for a given item is enabled sources ∩
the item's project's allowed sources (if restricted) ∩ the item's own
allowed sources (if restricted) — each level can only narrow, never widen,
what the level above allows.

---

## Adding Sources

Every source implements one small contract (`src/product_finder/sources/base.py`):
`name`, `is_automated()`, `search(term, item)` and `manual_links(item)`. All
downstream logic (grading, scoring, dedup, alerts, web UI) only sees
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

Automated sources (eBay, RSS/Atom) pace their own requests adaptively (see
`rate_limit.py`): each backs off on a 429, retrying with growing delays a
bounded number of times before giving up on that term for the cycle, and
gradually eases back down after a run of clean requests. Pacing is per
source instance and resets each `watch` cycle — an hour's gap is long enough
for any real rate-limit window to clear, so starting fast again each cycle
is deliberate, not an oversight.

## Grading Limitations

Grading is keyword-based on title + condition + short description:

- Sellers describing condition vaguely (or not at all) grade as **Unknown**.
- Sarcasm, typos and unusual phrasing are not understood.
- "Spares/repair" keywords always win — a "like new but faulty" listing is
  graded spares/repair.
- eBay short descriptions are truncated, so some condition detail is missed.

Treat the grade as a triage hint, not a verdict — always read the listing.

---

## Viewing Results

There's no report file to generate or open — the **web UI** is the report.
The dashboard's hero strip and per-project preview surface the best deals as
they're found; the **project detail page** (click any project) shows every
item grouped with its matched listings, colour-highlighted the same way
(green for excellent deals, red for spares/repair or flagged listings), plus
that project's manual search links. Both update themselves automatically as
`watch` finds new results — no regenerating, no opening a file.

---

## Known Limitations

- One listing can match multiple items if their search terms overlap.
- `normal_price` (item-level or per-product via the catalogue) is your
  estimate, not market data — margins are only as good as the estimate.
- Product catalogue matching is a plain keyword lookup (same style as
  grading) — no fuzzy matching, so typos or unusual phrasing in a listing
  won't resolve to a catalogue product even if match terms are sensible.
- "Typical used price" is mostly built from *asking* prices on active
  listings, not confirmed sold prices — eBay's Marketplace Insights API
  (which would give real sold prices) requires special access this app's
  developer account doesn't have. The one exception: auctions get a genuine
  "sold for" proxy — see below.
- The auction-close poller assumes the last price seen right after
  `estimatedAvailabilities` flips to `OUT_OF_STOCK` is the winning bid. It
  doesn't (and can't, via this API) confirm the sale actually completed —
  e.g. a reserve-not-met auction would still get captured as if it sold.
- "Typical new price" can now auto-refresh from a human-approved retailer
  URL (optional, off by default — see `searxng` config / `retailer_price.py`),
  but discovering that URL is a one-time human approval step, not automated
  end-to-end: matching a search result to the *correct* retailer page is a
  real identity-resolution problem this deliberately doesn't try to solve
  unsupervised. Amazon/Currys/etc. still have no public listing-search API,
  so this only works for whatever retailer pages plain web search can find
  and a human confirms.
- Deal scores are heuristic; a vague title or missing description skews them.
- The deal score deliberately distrusts extreme discounts on listings that
  haven't resolved to a catalogue product: past ~70% below the reference
  price the margin reward decays instead of growing, and below ~12% of the
  reference price the listing is flagged `price implausible for item` (on
  real data these are almost always accessories or spare parts caught by an
  item's search terms — hose adaptors matching "Dust Extractor" — not real
  bargains). A genuine once-in-a-blue-moon 90%-off listing for an item with
  no catalogue product will therefore be under-scored until the catalogue
  covers it. Thresholds are named constants at the top of `scoring.py`. This
  is an interim calibration: the long-term fix is catalogue coverage plus
  distinguishing complete products from accessories/spares/bundles.
- The deal score is priority-blind by design: item priority is "how much do
  I want this", not "how good is this deal", and is intended for ranking or
  spotlight selection downstream rather than being baked into the score.
- De-duplication is two layers, and only the first is automatic.
  Canonical-URL identity (`identity.py`/`db.resolve_identity()`) auto-links
  sightings sharing a platform's own native ID recoverable straight from the
  URL (eBay only so far). Fuzzy duplicate detection (`duplicates.py`) covers
  probable cross-marketplace duplicates with no shared ID, but only ever
  *proposes* pairs for human review ("Possible duplicates" on the project
  page): merging on title/price similarity alone risks conflating two
  different real items, so nothing is hidden without a human confirming it.
  Same-marketplace fuzzy proposals are deliberately not generated at the
  moment; identical titles on one marketplace usually mean different sellers
  selling the same model, not the same opportunity.
- Once a duplicate pair is confirmed, the hidden listing stays hidden even
  if the kept one later ends while the hidden one is still live — the
  opportunity vanishes from view until the pair's decision is undone (the
  "Decided pairs" fold-away on the project page has an Undo).
- Multi-item/price-range detection (`scoring.is_multi_item_or_price_range`)
  only reads the listing *title*, never the description — deliberate, to
  avoid misreading single-item markdown framing like "was £299, now £95" in
  a description as an ambiguous range. A genuine range spelled out only in
  the description is a known miss.
- Negation handling in grading/warning-flag matching (`grading.phrase_present`)
  scopes a "no"/"not" to the current comma-or-sentence-bounded clause. A
  fault covered by one negator across a comma-joined list (e.g. "no
  scratches, dents or cracks" only suppresses "scratches") needs its own
  "no" to be caught — a deliberately accepted false-negative, safer than
  letting a negator leak into an unrelated later clause.
- Watch mode is a simple loop — no scheduling, back-off, or rate limiting
  beyond the configured interval.
- SQLite database (`data/product_finder.db`) grows indefinitely; no pruning
  yet.

---

## Tests

```bash
pytest
```

Covers grading, scoring, deduplication, config loading, and the web UI
(dashboard, project pages, sources, project/item CRUD).
