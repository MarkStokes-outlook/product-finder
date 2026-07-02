# Project Memory

This file captures long-lived, repo-specific conventions that agents should respect.

## Stable Conventions
- Layout: `src/` layout Python package (`src/product_finder/`), installed editable via `pip install -e ".[dev]"`.
- Dependencies: minimal by design — PyYAML + requests only. Do not add heavyweight frameworks.
- CLI-first: argparse subcommands in `cli.py`; orchestration lives in `runner.py` so it stays testable.
- Config: single YAML file (`config.yaml`, gitignored; `config.example.yaml` is the committed reference).

## Domain Concepts
- `project` — a group of wanted items (e.g. Coachhouse tools); `item` — one wanted product with search terms and prices.
- `normal_price` — operator's estimate of market value; `target_deal_price` — at/under this = deal; `max_price` — hard filter.
- `grade` — rule-based condition class: A / B / C / spares/repair / unknown (`grading.py`). Spares keywords always win.
- `warning flags` — false-bargain signals (faulty, untested, no charger…) in `scoring.py`.
- `deal_score` — 0–100 heuristic combining margin, target, grade, priority, flags.
- Sources: eBay automated via official Browse API (needs free dev keys in config); Gumtree and Facebook are manual-assisted link generators only (ToS compliance — never scrape).
- Dedup: listings unique on `(source, external_id)`; alerts fire once per `(listing, item)` match per channel (`alerts_sent`).

## Testing & Quality
- Run: `pytest` (or `.venv/bin/pytest`). 22 tests cover grading, scoring, dedup, config loading.
- Good enough = pragmatic MVP: correctness of scoring/dedup matters; polish does not.

## Agent Usage Notes
- 2026-07-02: MVP built from `prompts/1-initial-prompt.md` (Claude, developer role). Full package, tests, README usage docs. Not yet committed.
- Do not add web UI, accounts, or plugin architecture — explicitly out of scope.
- Compliance is a hard constraint: no login bypass, no CAPTCHA/bot-protection evasion, official APIs or manual links only.
