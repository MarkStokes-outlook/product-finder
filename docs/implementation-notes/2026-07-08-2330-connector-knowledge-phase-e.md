# Connector Knowledge — Phase E of the "acquisition platform" roadmap

**Date:** 2026-07-08 ~23:30
**Tests:** 633 passing (629 prior + 4 net new page tests, plus 8 unit
tests in `test_connectors.py`)
**Trigger:** Continuation of [[acquisition_platform_roadmap]] — Phases A
(connector maturity), B (coverage analytics), C (capability explorer), D
(connector health) shipped earlier the same session; this is Phase E,
broadened by Mark from the original "Source Roadmap" framing to
"Connector Knowledge": identity (display name/description/implementation
type/maturity), current functionality (listing types/marketplaces/search
features), operational characteristics (reusing `SourceCapabilities`
where possible), and future notes (planned/intentionally-unsupported/
investigation items) — declared close to each connector, no duplicated UI
text, no behavioural changes, no new connectors.

## Design decisions made before writing code

**Field-by-field audit against `SourceCapabilities` before adding
anything.** Went through Mark's full field list and classified each one:
already exists on `SourceCapabilities` (polling recommendation →
`recommended_schedule`, expected freshness → `freshness`, rate-limit
guidance → `rate_limit_class`, account/compliance risk → `account_risk`/
`compliance_mode`) vs. genuinely new (everything under Identity, Current
functionality, and Future notes, plus `known_limitations`). The "already
exists" fields are **not** re-declared on the new `ConnectorKnowledge`
dataclass at all — the Sources page reads them from `capabilities()`
directly, same object it already had. Authentication requirements is a
readable sentence, not a new field either: derived in the template
straight from the existing `requires_user_auth`/`is_official_api`/
`requires_manual_input` booleans. This was the most direct way to satisfy
"reuse `SourceCapabilities` where possible" — not a policy statement, an
actual field-by-field decision recorded before writing the dataclass.

**`Source.knowledge()` is concrete, not abstract — reversed mid-implementation.**
First pass mirrored `capabilities()`'s `@abstractmethod` pattern exactly,
since that's this codebase's established "declared not inferred"
convention (Phase C's `capability_checklist()` work). Ran the full test
suite immediately after and got 24 failures — every `Source` subclass used
as a test fake across a dozen unrelated files (`test_connectors.py`'s
`HealthyFake`/`FailingFake`/`_RiskyFake`, `test_identity.py`'s
`FakeSource`, `test_suggestions.py`'s `FakeEbaySource`, plus fakes in
`test_duplicates.py`/`test_price_history.py`/`test_catalogue_tidy.py`/
`test_locking.py`) stopped constructing, because none of them had a
reason to declare a full identity/roadmap write-up. Reverting to abstract
and hand-writing plausible `knowledge()` stubs into a dozen test fakes
would have been a large, unrelated diff for a phase whose brief explicitly
said "no behavioural changes" — those tests aren't testing connector
metadata, they're testing runner/dedup/pricing/locking behaviour using a
fake connector as scaffolding. Made `knowledge()` a concrete method
instead, with a default that's honest about being a default (`"No
connector-specific description declared yet."`, `maturity="experimental"`)
rather than one that invents plausible-looking detail. All five real
connectors override it; nothing else needed to change. Caught this by
running the full suite immediately after the abstract-method version
rather than assuming it was fine because the connector tests passed.

**Maturity is a judgement call, made and written down, not left implicit.**
eBay/Gumtree/Facebook/`links.py`'s generic connector are all declared
`"production"` — each mechanism (official API client, or a link generator
with zero fetch/parse surface) has been live and stable. `rss.py`'s
generic connector is `"beta"` — the parsing mechanism itself is equally
stable and shared across every configured feed, but *any specific
configured feed's* real-world reliability and continued compliance is
unverified per-instance, which `"production"` would have overstated.
Documented this reasoning directly in `rss.py`'s `knowledge()` docstring
rather than leaving the distinction unexplained.

## Fact-checking before declaring anything (not guessed)

Read the actual `search()`/`manual_links()` implementation of every
connector before writing its `known_limitations`/`supported_*` fields,
rather than inferring them from the existing `capabilities()` declaration
alone:

- **eBay**: confirmed `X-EBAY-C-MARKETPLACE-ID: "EBAY_GB"` is hardcoded
  (`sources/ebay.py:160,215`) → `supported_marketplaces=("eBay UK
  (EBAY_GB)",)`, not a guessed "eBay" label. Confirmed `search()` makes a
  single request with `limit=50` and no pagination loop → the "only the
  first 50 results per term" limitation is a checked fact, not hedging.
  Cross-referenced the already-parked HTML-enrichment idea in
  `docs/strategy/roadmap.md` (`intentionally_unsupported`) rather than
  re-describing it from memory — read the actual doc section first to get
  the risk classification (`compliance_mode="scraping"`, medium-high risk)
  right.
- **Gumtree/Facebook**: re-read `manual_links()` to confirm exactly which
  query params each generates — Facebook's genuinely has no postcode/
  radius param (unlike Gumtree's), which became an explicit
  `known_limitations` entry rather than an assumption they're equivalent.
- **RSS/links generics**: confirmed via `models.ManualLink`'s fields (only
  `source`/`label`/`url`) that manual-assisted connectors structurally
  cannot report a `supported_listing_types` value — same reasoning Phase
  C already established for `capability_checklist()`'s "na" status, reused
  here rather than re-derived.

## UI

New `<h2>Connector Knowledge</h2>` section, structurally parallel to
Phase C's Capabilities section (one collapsed `details.listings` fold per
connector, same pattern) but kept separate from it rather than merged into
the same fold — Capabilities is a compact ✓/✗/na grid, Connector Knowledge
is prose-heavy; combining them would have cluttered both. Empty
sub-sections (e.g. a connector with no `planned_work`) are omitted
entirely via Jinja `{% if %}` guards, not shown as an empty heading.

**Deliberately not done despite being tempting:** the existing Sources/
Coverage/Connector Stats/Capabilities tables' row `label` values stay
exactly as they were — still assembled in `app.py`'s `source_list()`
(hard-coded for built-ins, `e.label or e.name` for extras) rather than
switched to `knowledge().display_name`. Checked this before doing it:
`rss.py`'s `knowledge()` falls back to the raw configured name when no
label is set, `links.py`'s falls back to a `.title()`-cased name, and
`app.py`'s existing fallback is the raw name — three different fallback
behaviours that happen to agree only when a label *is* set. Retrofitting
every existing row label to source from `knowledge()` would have silently
changed the displayed text for any unlabelled `links`-type extra, which
the phase's explicit "no behavioural changes" constraint rules out. The
new Connector Knowledge section's own heading uses
`knowledge().display_name` directly — that's the one place in this phase
where it actually needed to.

**Test-writing note:** `test_sources_page_connector_knowledge_omits_empty_sections`
initially failed for the same class of bug as Phase D's CSS false-positive
— `section.find("Gumtree")` matched eBay's own `known_limitations` text
first ("...unlike Gumtree/Facebook's postcode+radius"), which appears
*before* Gumtree's actual `<details>` block in the section, over-including
the slice. Fixed by anchoring on the rendered summary heading
(`<strong>Gumtree</strong>`) instead of a bare word — the same lesson as
Phase D: a substring search across a whole HTML fragment needs an anchor
specific enough that the thing under test can't accidentally mention
itself somewhere else on the page.

## What's deliberately not done

- No change to existing row labels elsewhere on the page (see above).
- No new connectors, no scheduling/behavioural changes — every edit is
  either a new dataclass, a new method with a safe default, or template
  rendering of already-declared data.
- Phase F (search aggregation / orchestration foundation) not started —
  the final phase of the roadmap.
