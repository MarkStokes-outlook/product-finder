# Connector Capability Explorer — Phase C of the "acquisition platform" roadmap

**Date:** 2026-07-08 ~21:15
**Tests:** 586 passing (583 prior + 3 net new page tests, plus 5 new unit
tests in `test_connectors.py`)
**Trigger:** Continuation of [[acquisition_platform_roadmap]] — Phases A
(connector maturity) and B (coverage analytics) shipped earlier the same
session; this is Phase C, scoped by Mark as: surface `SourceCapabilities`
directly (no duplicated hard-coded UI logic), show supported/unsupported/
unknown clearly, across 16 named capability areas, readable (not a
20-column table), fix connector metadata only where genuinely incomplete
or misleading, no new connectors, no scheduling changes.

## Design decisions made before writing code

**"Unknown" isn't a real third state — reframed as "na" with a stated
reason.** `SourceCapabilities`' own docstring is explicit: "What a
connector can legitimately do, declared not inferred." Every boolean field
already has a concrete default (`False`) — there's no nullable/unknown
sentinel anywhere in the dataclass, by design. Introducing a fabricated
"we don't know" bucket would have contradicted that design and reintroduced
exactly the kind of unprovenanced approximation Phase B's rules explicitly
ruled out. But there *is* a real, structural third state hiding in the
data: for a manual-assisted connector (`automated=False`), `search()` is
never called and no `Listing` is ever produced — only `ManualLink`
(`source`/`label`/`url`, see `models.py`). So asking "does Gumtree provide
images" isn't a false claim, it's a category error: there's no listing to
have an image. `capability_checklist()` reports exactly the 9
listing-shape fields (`provides_images`, `provides_end_time`, etc., plus
`supports_enrichment`) as `"na"` for any connector with `automated=False`,
and real `"supported"`/`"unsupported"` everywhere else. Confirmed this
distinction is genuine, not invented, by checking `ManualLink`'s actual
fields before writing the logic.

**One manifest, one method, not duplicated UI logic.** `_CAPABILITY_FIELDS`
(`sources/base.py`) is an ordered `(label, field_name)` tuple list — the
only place the 16 capability areas and their display order are named.
`SourceCapabilities.capability_checklist()` reads it and returns `(label,
status)` pairs by `getattr`-ing the live instance. The template calls
`s.caps.capability_checklist()` directly (Jinja supports zero-arg method
calls) — no `app.py` route change needed at all, and no risk of the
template's label text drifting from the dataclass's actual field. Adding a
17th capability later means editing exactly one tuple.

## Metadata gap found and fixed

Audited every connector's `capabilities()` call (`ebay.py`, `gumtree.py`,
`facebook.py`, `rss.py`, `links.py`) against Mark's 16-item list before
writing any UI. Found one real gap: `provides_auction_snapshot` (time-series
bid *history*, wired up in the previous coverage-phase session) was the
only auction-related field — there was no field for the more basic "can
this source's listings even be auction-type at all" claim (buying_options/
bid_count/current_bid_price meaningfully populated). For eBay, both
questions happen to have the same answer today (True), which is exactly
why the gap was easy to miss — but they're genuinely different claims,
and Mark's brief listed "Auctions" and "Auction snapshots" as two separate
rows, which would have shown the same underlying boolean twice under two
different labels: technically not wrong, but presented as if they were two
independently-verified facts when they were one. Added `provides_auctions`
to the dataclass, set `True` only for eBay. No other connector's
declarations needed correcting — gumtree/facebook/rss/links were all
already accurate against the full 16-field list (double-checked each
`capabilities()` call directly rather than assuming).

## UI

Reused the existing `details.listings` collapsible pattern (same class,
same arrow-icon CSS already in `base.html`) rather than inventing a new
disclosure widget — one folded `<details>` per connector, closed by
default, containing a `.cap-grid` (CSS grid, `auto-fit` columns) of
16 compact rows rather than a wide table. Colour is deliberately neutral:
a health badge uses green/red because failing is bad, but a capability
being "unsupported" often isn't (e.g. `requires_user_auth: unsupported` is
a *good* thing) — using red there would have implied the wrong polarity.
Used a muted/subdued style for both "unsupported" and "na" and a brighter
check only for "supported", so the eye reads "what's actually there"
rather than "what's wrong".

**Test-writing note:** two page-rendering tests initially failed on
string-search overlap — `data.find("Gumtree")` picked up the *Sources*
table's row (which lists "Gumtree" first) rather than the Capabilities
section further down the page, and a fixed 4000-character slice from
"eBay" overran into the next connector's `<details>` block. Fixed by
scoping searches to the `<h2>Capabilities</h2>`...`<h2>Coverage</h2>`
region first, then bounding each connector's block at its own
`</details>` tag rather than a magic-number character offset.

## What's deliberately not done

- `provides_auctions` is purely declarative/display — not wired into any
  runtime gating (auction polling, scheduling, etc.). Confirmed nothing
  else in the codebase reads it before adding it; no behavioural change,
  per this phase's explicit constraint.
- No changes to `account_risk`/`compliance_mode`/`rate_limit_class`/
  `recommended_schedule`/`freshness` display — those already surface via
  the existing Sources table's "Class" column (`caps.compliance`) and
  weren't part of Mark's 16-item capability list for this phase.
- Phases D (health score/status), E (source roadmap metadata), F
  (orchestration layer) not started.
