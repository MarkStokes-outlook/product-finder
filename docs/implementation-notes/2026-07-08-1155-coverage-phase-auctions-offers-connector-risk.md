# Coverage phase: auction snapshots, trajectory/offer intelligence, connector risk model, Active Auctions/Offers UI

**Date:** 2026-07-08 ~11:55
**Tests:** 545 passing (488 prior + 57 new across `test_auction_snapshots.py`,
`test_auction_trajectory.py`, `test_offers.py`, extensions to
`test_sources.py`/`test_auction_watch.py`/`test_connectors.py`/`test_web.py`)
**Trigger:** Mark called the deal-scoring work "useful but not the
bottleneck" — the real gap is source coverage and auction/offer
intelligence. Scoped as an 8-item brief (audit eBay's auction fields, add
an auction snapshot model, auction trajectory scoring, offer intelligence,
a connector capability model, Facebook/Gumtree expansion, and UI). Delivered
as 6 phases in 6 commits; Facebook/Gumtree expansion became an options paper
(see below) rather than code, per the compliance-model discussion.

## Reality check before building anything

Before writing a line of code, audited what the brief assumed was missing
against the actual codebase (memory + fresh grep/read of `sources/ebay.py`,
`scoring.py`, `auction_watch.py`, `sources/base.py`). Several items were
already shipped or partially shipped:

- eBay `currentBidPrice`/`bidCount`/`buyingOptions`/`itemEndDate` mapping:
  already done (2026-07-03 session). No `fieldgroups` param needed or used —
  confirmed live, not assumed.
- Auction tracking: existed, but only as an *end-of-auction close price*
  capture, and only for listings already matched to a catalogue product
  (`list_tracked_auctions` required `product_id`). No time-series of
  in-flight bid/price observations existed at all — that was the real gap.
- A connector capability abstraction (`sources/base.py`) already existed,
  close to what the brief asked for — needed extending, not building.
- Auction trajectory labelling and offer intelligence: genuinely didn't
  exist anywhere.

This reset scope before any design work started, rather than rebuilding
things that were already there.

## Phase 1 — eBay evidence capture (commit `3aace5e`)

Prior test coverage for the auction-field mapping used hand-written inline
dicts, not real API shapes. Per Mark's explicit "do not guess" instruction,
captured real (sanitised) Browse API responses using this project's own
stored dev credentials — a pure auction, a fixed-price listing, and (in a
follow-up capture during Phase 2) a listing with both AUCTION and
FIXED_PRICE, plus a BEST_OFFER listing for later use. Sanitised seller
username/feedback/image URLs/item IDs/city before committing; documented
provenance per-file in `tests/fixtures/ebay/README.md`, including which
parts are real captures vs. one deliberately-labelled synthetic derivative
(the OUT_OF_STOCK-flip "ended" fixture — the real item's close was ~5.5h
away, too far to wait for in-session).

**Real finding worth remembering:** the `getItem` endpoint returns a
non-null `price` field even for a pure auction with no Buy It Now (mirroring
`currentBidPrice`) — unlike the search endpoint, which returns `price: null`
for the same case. Different endpoints, different null behaviour for the
same listing state.

## Phase 2 — Auction snapshot history (commit `7d4943e`)

New `auction_snapshots` table (append-only — `db.record_auction_snapshot`/
`list_auction_snapshots`): listing_id, source, observed_at,
current_bid_price, bid_count, buy_it_now_price, shipping_price, end_time,
watch_count, view_count, raw_payload. Broadened `list_tracked_auctions` to
a LEFT JOIN so any live auction gets snapshot history recorded, not only
catalogue-matched ones — the product-specific used-price observation on
close still only fires when a product match exists (unchanged behaviour
there, just no longer a *prerequisite* for tracking at all).

**Real-data-driven design correction found while wiring this up:** a
BIN+auction listing returns `price` (BIN, 229.50) and `currentBidPrice`
(current bid, 156.70) simultaneously and distinctly. `AuctionSnapshot.price`
already had fallback semantics (BIN-over-bid) relied on elsewhere
(`auction_watch.py`'s close capture) — reusing it for the snapshot table's
"current bid" column would have silently recorded the BIN price as the
"current bid" whenever a BIN existed, hiding exactly the
bid-climbing-toward-BIN signal trajectory scoring needs. Added a separate
`current_bid` field (always `currentBidPrice`, never falls back) rather than
changing the existing field's meaning. `watch_count`/`view_count` are always
`None` — confirmed absent from every real capture (checked the full key
set, not just expected fields), recorded as unknown with provenance, not
guessed.

## Phase 3 — Auction trajectory labelling (commit `6728d74`)

New `auction_trajectory.py`, pure functions, no DB/class state (same style
as `price_trend.py`). Produces one of Mark's five labels (Early watch /
Potential deal / Likely bargain if it stays under £X / Getting too hot / No
longer a deal) plus a plain-English explanation and a suggested bid ceiling
— from headroom % against a reference price (prefers a product's
`typical_used_price`, falls back to the item's blended estimate) and,
when ≥3 bid-bearing snapshots span ≥5 minutes, whether the bid is
accelerating vs. its earlier pace. Below that data threshold, "accelerating"
is `None` — not guessed as False — same confidence-gating philosophy as
`price_trend.py`. Deliberately kept separate from `scoring.deal_score()`,
which already treats a live bid as never a committed price; this answers a
different question ("is this still worth watching") rather than folding
into the main score.

## Phase 4 — Offer intelligence (commit `77a8def`)

New `offers.py`. `detect_offer_support()` reads `BEST_OFFER` from
`buying_options` — already captured, never previously read anywhere.
`suggest_offers()` produces safe/normal/cheeky prices, anchored to the
reference price (not just a flat % off asking) when a seller is pricing
above market, with a confidence tier (low/medium/high) that degrades
without a reference price or a clear condition grade — capping how
aggressive the cheeky offer is allowed to be on thin evidence.
`seller_confidence`/`source_confidence` are accepted as optional inputs for
forward compatibility (no connector supplies them today — default to
neutral, never guessed). Suggestions only; nothing here submits an offer.

## Phase 5 — Connector risk model (commit `7901e67`)

This phase changed mid-flight after a real back-and-forth with Mark about
compliance policy (see conversation, not repeated here in full):

1. First asked to design the connector model around a full
   background-plugin abstraction (unattended/auth/manual/official/
   indexed/compliance-risk/freshness/fields-provided) — explicitly ruling
   out a browser-extension or always-open-session architecture as the
   *core* design.
2. Then Mark deliberately lifted this project's previous absolute
   "compliance is a hard constraint, scraping never gets built" stance —
   but only if risk is modelled explicitly, never hidden behind
   `automated=True`, and scheduling stays safe by default.

`SourceCapabilities` (`sources/base.py`) now carries the full model:
`account_risk` (none/low/medium/high), `compliance_mode` (official/indexed/
manual/user_session/scraping/licensed_provider), `can_run_unattended`,
`requires_user_auth`, `requires_manual_input`, `is_official_api`,
`is_indexed_search_based`, `is_scraping_based`, `is_third_party_provider`,
`rate_limit_class`, `recommended_schedule`, `freshness`, plus the
auction/offer/seller/location "what fields can this provide" flags this
phase needed. `__post_init__` enforces the model can't lie:
`is_scraping_based=True` cannot claim `account_risk` none/low;
`requires_user_auth=True` cannot claim `account_risk="none"`.

`sources.build_registry()` (the scheduled-run path) now risk-gates every
candidate: none/low included by default, medium/high require the source's
name to appear in the new `sources.risk_acknowledged` config list — being
"enabled" is never enough on its own, and there's no accept-everything
switch. All 5 existing connectors declare `account_risk="none"` today —
nothing about current behaviour changed; this is scaffolding for connectors
that don't exist yet.

## Phase 6 — Active Auctions / Offers UI (commit `d041fa4`)

New `/auctions` and `/offers` routes and templates. Active Auctions: every
live auction across all projects, soonest-ending first, current bid
(preferring the latest `auction_snapshots` row over the possibly-stale
`listings.price`), trajectory badge/explanation, suggested bid ceiling.
Offers: fixed-price Best-Offer listings with safe/normal/cheeky suggestions
and a confidence badge. Both reuse the existing `_MATCH_SELECT` join
(extended to expose `listing_id`) rather than a parallel query path.

**Scope call made without re-asking:** went with dedicated pages rather
than retrofitting badges onto every existing listing card
(dashboard/project-detail) — those pages already have a live-polling
mechanism (`dashboard_live`/`_dashboard_live.html`) computing per-row
scoring on every poll tick; adding offer-suggestion computation to every
row of an already-hot path risked a real performance regression I wasn't
asked to introduce. A card-badge version can still be added later.
Live-polling was likewise not added to the two new pages (they render
fresh per page load) — same reasoning, kept as a smaller first cut; the
existing `[data-ends]` countdown JS in `base.html` already ticks on a plain
page load with no extra wiring needed.

## Phase 7 — Facebook/Gumtree options paper (no code)

`docs/strategy/facebook-gumtree-connector-options.md`: six options (official
API, SearXNG-indexed search, manual-assisted, third-party provider,
user-session/browser automation, direct scraping), each scored on data
available / unattended-capability / auth / account risk / freshness /
fragility / complexity / recommended schedule / default-enablement.
Explicitly flagged which parts are live-verified vs. reasoned from each
platform's publicly documented access model (Meta's Content Library and
Apify were not tested against real accounts in this session — no such
credentials exist in this project). Recommendation: keep manual-assisted as
default for both; a small SearXNG experiment is worth trying for Gumtree
specifically (not Facebook, which isn't meaningfully indexed); third-party
provider (Apify-style) is the most realistic path to real Facebook coverage
without risking Mark's own account, worth scoping as its own future piece
of work under explicit opt-in; user-session/browser automation must never
be scheduled even if built later, per Mark's explicit instruction, not just
a current-priority call.

## What's deliberately not done

- No fixture exists yet for the eBay evidence capturing a real close event
  via live polling across a long wait — the "ended" fixture is a labelled
  synthetic derivative of a real mid-auction capture, not a fresh live close.
- Offers/trajectory are not surfaced on the existing dashboard/project-detail
  cards, only on the two new dedicated pages.
- No live-polling refresh on `/auctions` or `/offers` (static per page load).
- Facebook/Gumtree: no code changed at all — options paper only, per this
  phase's explicit scope boundary ("do not build scraping immediately").
- Broadening auction tracking to all live auctions (not just catalogue-
  matched) increases eBay `getItem` polling volume — bounded by the existing
  tiered cadence and per-instance rate limiter, but worth watching on a real
  account with many concurrent auctions.
