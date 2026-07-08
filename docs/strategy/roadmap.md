# Roadmap

This document exists to answer one question: **if there's a free weekend to
improve Product Finder, which part of the system deserves it, and why?**

It intentionally does not answer "what are the next tasks" — that changes
weekly and belongs in code, commits, and conversation, not here. This
document should still make sense even if every implementation detail below
it has changed.

## Current strategic phase

**Current phase: Market coverage and product understanding.**

The deal engine is now useful enough to act as a ranking signal: scoring has
been recalibrated, auctions are treated as uncommittable prices while live,
ended listings disappear from browsing surfaces, product suggestions exist,
used-price history is collected, auction-close prices are captured, and the
first canonical-URL identity resolver is live.

The bottleneck has shifted. Product Finder no longer needs a slightly cleverer
score nearly as much as it needs **many more high-quality listings** and a
better understanding of what those listings actually describe.

Near-term focus:

1. **Market coverage** — add many more listing sources through a connector model.
2. **Connector quality** — normalise capabilities, source health, rate limits,
   enrichment, and provenance instead of treating each source as a one-off.
3. **Product understanding** — improve catalogue growth from unstructured text,
   and distinguish products from accessories, spares, bundles, and variants.
4. **Cross-source identity** — prevent the same opportunity appearing as several
   unrelated deals as coverage grows.

---

## What Product Finder actually is

Product Finder looks like a marketplace watcher, but the long-term asset is not
one marketplace integration. What compounds in value over time, and what this
roadmap exists to protect and grow, is:

- **Curated product knowledge** — the catalogue: which manufacturer/model
  products exist, and how to recognise them in the wild.
- **Accumulated pricing knowledge** — what things actually cost, new and
  used, and how that changes over time. The long-term goal is not simply to
  estimate price, but to estimate confidence in that price based on the quality
  and quantity of supporting evidence.
- **Market coverage** — enough current listings, from enough different places,
  that Product Finder can discover real opportunities rather than merely rank
  the small slice of the market it already sees.
- **Buying intelligence** — turning the assets above into a judgement about
  whether a specific listing, right now, is worth acting on.

Every area below is one of these assets, or something that protects the
system's ability to keep accumulating them honestly. If a future idea doesn't
serve one of these, it's scope creep, however interesting.

---

## Recently shipped foundation

The following areas are now part of the foundation rather than open strategic
questions:

- Product catalogue and human-reviewed product suggestions.
- Structured eBay brand/model discovery.
- Local-model (Ollama) brand/model extraction from unstructured listing text,
  feeding the same human-reviewed suggestion queue — never writing to the
  catalogue directly. Off by default until the confidence signal earns trust.
- Product-aware deal scoring.
- Used-price observations and trend-aware scoring.
- Auction capture for end-of-auction price observations.
- Live-auction handling: live bids are not treated as committed buy prices.
- Deal-score recalibration to avoid score saturation.
- Implausible-price handling for unverified extreme discounts.
- Clean project top-picks and dashboard surfaces.
- Product images on listing cards.
- Canonical-URL identity resolution v1.
- Ended listing lifecycle: known-ended listings are hidden immediately on read.

This does not mean these areas are finished forever. It means they are good
enough for the next bottleneck to be elsewhere: coverage, connector quality,
and listing understanding.

---

## Market coverage and marketplace connectors

The current source set is too narrow. eBay plus a handful of RSS-style feeds is
not enough to make Product Finder feel like it sees the market. The next major
leap is not another scoring tweak; it is feeding the engine **many more
listings**.

The direction here is a marketplace connector model, not a growing pile of
source-specific shortcuts. A connector owns the messy details of one market or
feed type and emits the same normalised listing shape to the rest of the
system.

Connectors come in two classes, and both are first-class citizens of the model:

- **Automated connectors** — official APIs, authorised feeds, or genuinely
  open RSS/Atom endpoints that permit programmatic search. These feed the
  engine directly.
- **Manual-assisted connectors** — marketplaces whose terms of service do not
  permit automated access (Facebook Marketplace and Gumtree today). These
  generate pre-filled search links for a human to follow; they never scrape,
  bypass logins, or evade bot protection.

Compliance is a hard constraint, not a preference. An integration that can
only exist by violating a marketplace's terms does not get built, however
valuable its listings would be. A marketplace moves from manual-assisted to
automated only when a legitimate route appears — an official API, an
authorised feed, or an explicit licence — never because scraping got easier.

Every connector should declare, where possible:

- Search capability and supported query shape.
- Pagination/incremental update behaviour.
- Rate limits and back-off strategy.
- Listing freshness and expiry semantics.
- Image availability and quality.
- Seller identity/reputation fields.
- Structured product attributes, if available.
- Enrichment support, if a listing-detail fetch exists.
- Health/status reporting.
- Provenance and confidence notes.

Near-term connector candidates (class depends on what each marketplace's
terms legitimately allow, verified before building):

- Facebook Marketplace (manual-assisted — no compliant automated route today).
- Gumtree (manual-assisted — same).
- Vinted.
- Craigslist or local classifieds where relevant.
- Reverb for music gear (has an official API).
- Cash Converters / CEX / BackMarket-style used retailers.
- Retail clearance and warehouse/outlet feeds.
- Local auction houses, liquidation feeds, and estate-sale sources.

The goal is not just "more sources". The goal is more **credible opportunities**.
A weak source that produces stale, duplicated, or untrustworthy listings should
not pollute the engine simply because it is easy to scrape.

Coverage should become measurable. Useful metrics include:

- Listings ingested per day.
- Listings by source.
- Active and failing sources.
- New listings per hour.
- Duplicate/cross-source identity rate.
- Catalogue match rate by source.
- Product-suggestion yield by source.
- Enrichment success rate.
- Source freshness / stale-listing rate.
- Price-history contribution by source.

These metrics should steer connector work. If one marketplace produces 90% of
useful listings, improve it. If another source produces mostly stale junk,
quarantine, throttle, or drop it.

---

## Catalogue quality

The catalogue already normalises manufacturer names, rejects placeholder
brands, and discovers new products from eBay's own structured seller data
under human review. That part works and doesn't need revisiting.

What the catalogue can't yet do is **heal itself**. Coverage is still too
dependent on sellers filling in structured brand/model fields — a private
seller writing a plain-text listing can be invisible to discovery no matter how
good the deal is, which is exactly the kind of listing this app exists to
catch. And because discovery has no way to recognise "this is the same product
as something already known, just spelled differently," near-duplicate products
can accumulate with no path to reconcile them back together.

The direction here is making discovery and reconciliation trustworthy enough to
stop being the bottleneck: teaching the system to recognise products from
unstructured text, and giving it a way to merge what it eventually gets wrong,
both under the same human-review discipline the structured-data path already
uses.

The free-text extraction fallback now exists (local-model, feeding the
suggestion queue under the same review discipline as structured discovery).
The next catalogue objective is quality rather than existence: measure and
improve extraction yield and precision against real listings, and teach
extraction to carry **richer product understanding** — variant/size/capacity
distinctions, accessory and spare-part signals, bundle awareness — so that
what reaches the suggestion queue describes what a listing actually is, not
just the first brand and model string found in it. That same understanding
should eventually power reconciliation: recognising that a newly extracted
product is a different spelling of one already known, and proposing the merge
for review instead of accumulating near-duplicates.

## Listing understanding

Grading and catalogue matching both treat a listing as one simple claim: one
item, one price, one condition. That assumption is often wrong — a listing can
describe a bundle of several valuable things, a range of prices for different
variants, a spare part/accessory rather than the wanted product, or a condition
claim that a keyword scanner reads backwards.

The system doesn't need cleverer keyword lists here — it needs a richer model
of what a listing *is* before grading or pricing tries to reason about it.
Getting this right feeds directly into deal accuracy, since a mis-modelled
listing produces a confidently wrong score.

Important near-term concepts:

- Complete product vs accessory/consumable/spare.
- Bundle, kit, set, job lot, and multi-item listings.
- Variant/size/capacity differences.
- Condition claims with negation and context.
- Seller-written ambiguity that should reduce confidence rather than force a
  binary decision.

## Deal accuracy

Deal scoring now has a healthier distribution and is useful again as a ranking
signal. The recalibration deliberately treated extreme unverified discounts as
suspicious instead of automatically excellent, removed item priority from the
score, and kept "objective deal quality" separate from "how much the user cares
about this item".

The remaining limitation is not primarily the formula. It is evidence quality.
A score is only as good as the product identity, source trust, and reference
prices beneath it.

The most valuable next work here is to keep improving the evidence layer:

- More catalogue-backed matches.
- More reliable used-price observations from real marketplace activity.
- More new-price history from trusted retailer sources.
- More closing-price/sold-price proxies where APIs do not expose sold data.
- Clear confidence semantics so the score can express uncertainty rather than
  hiding it.

A deal score computed against verified product identity and accumulated price
history is fundamentally better than one computed against a static estimate,
independent of any UI or feature work.

## Sources and trust

Marketplace connectors are only valuable if the information they contribute can
be trusted. As Product Finder expands beyond eBay into retailers, Facebook
Marketplace, Gumtree, RSS feeds and other sources, every connector should emit
the same normalised listing model while keeping source-specific quirks isolated
behind the connector.

More sources should never mean lower confidence. Every source should contribute
not only price and product information, but evidence about the listing itself:
seller identity, listing images, condition, provenance, and anything else that
helps judge whether the opportunity is genuine.

Over time this should grow into a dedicated trust model alongside pricing.
Suspicious pricing, unusually prolific sellers, reused images, reverse-image
matches, stock photography, inconsistent locations, and known scam patterns
should reduce confidence in a listing rather than simply lowering its deal
score. Trust and value are different questions, and the system should learn to
answer both independently before making a recommendation.

Affiliate links also belong here. They should be treated as an implementation
detail of the destination resolver rather than the listing itself, allowing the
same buying experience whether a destination is monetised or not.

Affiliate links also change the product shape: Product Finder should eventually
support anonymous discovery and click-through while reserving saved projects,
watched products, alerts, and personal preferences for signed-in users. The
public experience can answer "is there a good deal right now?"; the signed-in
experience can remember what a person cares about and keep watching on their
behalf.

## Identity resolution

**v1 shipped (canonical-URL matching).** This was live debt, not latent —
`rss.py` (generic, config-driven RSS/Atom source) has been automated
(`is_automated() -> True`) alongside eBay for a while, so a listing matching one
item's search terms on both could already be scored and alerted on twice,
quietly, for any project running both sources.

`identity.py`/`db.resolve_identity()` now recognises the one case with a
provably shared identifier: a listing's URL containing a platform's own native
ID (v1: eBay's item ID, e.g. an RSS entry that happens to link straight to an
eBay item page). Cross-source sightings sharing that ID are linked via
`listing_identities`/`listing_identity_members`; only one ("primary" — the
platform's own native listing if one exists, else whichever was seen first)
counts toward alerting, price observations, and `query_matches`/dashboard
results. Every sighting still gets its own `listings` row and `listing_matches`
entry — nothing is deleted or collapsed, so provenance and the ability to unpick
a bad merge are both preserved.

**v2 shipped (fuzzy candidate grouping, human-decided).** The same physical
item listed with *no* shared ID — a seller double-listing on one marketplace,
or cross-posting to another — has no provable identifier to key off, only
title/price/location/image similarity, and merging on that alone risks
silently conflating two different real items. So v2 never merges: it proposes
candidate pairs for human confirm/dismiss ("Possible duplicates" on the
project page), mirroring `product_suggestions`' pending/approved/dismissed
pattern, with a decided pair remembered forever (a dismissal is never
re-asked) and every decision undoable. The key precision rule, learned from
real data rather than assumed: identical titles on the same marketplace
usually mean different sellers selling the same product model — separate real
opportunities — so same-source pairs also require seller evidence (matching
location, or an identical photo) before being proposed. Confirmed duplicates
are hidden through the same `is_primary_sighting` mechanism canonical
identity uses; nothing is deleted.

What identity still can't do: perceptual image matching (only exact photo-URL
equality contributes today), seller-identity evidence beyond location (waiting
on connectors declaring seller fields), and resurfacing a hidden duplicate
automatically if the kept listing ends first.

Also still unresolved: a single listing matching more than one item's search
terms still gets scored/alerted once per item. That's intentional where each
item represents a genuinely different want, but it may need better presentation
once projects become broader.

## Product knowledge beyond price

The catalogue currently knows a product's identity and its price. It has no
concept of category, variant relationships, accessories, consumables, or
compatibility — so it can't tell you a listing is really two things you'd want
to track separately, or that a cheaper compatible alternative exists.

This is real long-term value, but it's also the area most likely to turn into
architecture for its own sake if pulled forward too early. It pays off once the
catalogue underneath it is clean, growing, and de-duplicated.

Over time the catalogue should also become the system's understanding of
*where* a product can be bought. That means recognising trusted retailers,
marketplace listings, affiliate-supported destinations, accessories, compatible
alternatives, and replacement parts as first-class knowledge rather than
scattered links. The aim is not to promote retailers, but to help the buyer
reach the best purchasing option while keeping the decision grounded in the
catalogue's verified understanding of the product.

## Recommendations

Recommendations are the final layer of the system, not an independent
capability. Before Product Finder can confidently tell someone to buy, wait, or
choose an alternative, it first needs trustworthy answers to five separate
questions:

- What is this product? (Catalogue)
- What is it worth? (Pricing)
- Have I already seen it? (Identity)
- Can I trust this listing? (Sources and trust)
- Am I seeing enough of the market? (Coverage)

Only once those foundations are in place should the system form an opinion.
Recommendations should therefore consume the outputs of the catalogue, pricing,
identity, trust, and coverage layers rather than re-implementing their logic.

Today's "intelligence" is still mostly a number computed at the moment a listing
is seen. A genuine recommendation ("buy now," "wait," "there's a better
option") requires market coverage, real price history, product knowledge,
identity, and trust. This is the natural endpoint of the other sections, not a
parallel workstream.

## Users and saved projects

The current system is effectively single-user: projects are configured by the
operator and the application assumes one owner. That is fine for a private tool,
but affiliate-driven public discovery changes the shape of the product. If
other people can arrive, search, click through, and then decide to save their
own watched products, the system needs an explicit account boundary.

The intended product split is:

- Anonymous users can search, browse live deals, and click through to listing or
  retailer destinations.
- Signed-in users can create projects, save watched products, configure alerts,
  preserve preferences, and build their own buying radar over time.

Long term, authentication should be handled by Authentik/OIDC rather than a
bespoke password system. If implementation needs a stepping stone, a minimal
internal user model may be acceptable, but it should be shaped so it can be
replaced or backed by Authentik without rewriting project ownership,
permissions, or saved-state logic.

This is not just a login feature. It is the ownership model for saved projects,
alerts, affiliate attribution, and personal recommendations.

## Keeping the system healthy

Data accumulates with no retention policy, and the watch loop is still a simple
process that will need smarter source scheduling as connector count grows.
Neither is urgent today, but both are the kind of thing that's cheap to ignore
until it suddenly isn't: a slow query, a database that's awkward to back up, a
source getting rate-limited, or one noisy connector drowning out better data.

Operational health should be improved opportunistically whenever it is touched:

- Source back-off and retry policy.
- Per-connector health and last-success reporting.
- Stale listing handling when no end time exists.
- Retention/archive policy for old listings and observations.
- Query/index review as listing volume grows.
- Import/enrichment queues that can scale beyond one marketplace.

---

## Where AI fits

AI should show up where it can extend what the deterministic system already
does well — reading unstructured text to enrich catalogue discovery, proposing a
possible bundle or duplicate for a human to confirm — never as a replacement for
the deterministic grading, matching, and scoring that already exists, and never
as something that writes to the catalogue or changes a score unsupervised.

The existing product-suggestion queue (machine proposes, confidence scores,
human approves) is the template: every future use of AI here should look like
that pattern, not a new one.

---

## Future ideas (deliberately unscheduled)

Genuinely interesting, not currently justified by the assets above, and not
meant to be planned against:

- Affiliate link engine / destination resolver.
- Public anonymous search plus signed-in saved projects.
- Listing trust and scam detection beyond basic source metadata.
- Reverse image search for provenance and fraud detection.
- Browser extension.
- Mobile app.
- Cloud sync.
- Multi-user support.
- Public API.
- Notification channels beyond console/webhook.
- Automatic negotiation / seller messaging.
- **eBay listing-page HTML enrichment** (parked 2026-07-08, see
  `docs/implementation-notes/2026-07-08-1155-coverage-phase-auctions-offers-connector-risk.md`
  and the current-bid/BIN display bug fix in the same period). Real eBay
  page source confirmed to carry `watchCount` and other page-only signals
  (`x-watch-heart`, etc.) that the official Browse API does not expose at
  all — verified against a real saved page, not assumed.
  - **Purpose:** watcher count / other page-only signals, nothing the
    official API can provide.
  - **Source:** the listing's own HTML page, not the Browse API.
  - **Risk:** `compliance_mode="scraping"`, `account_risk` medium-high — this
    is outside eBay's official API and against its user agreement for
    automated access; risks the developer API credentials this project's
    one fully-compliant connector depends on.
  - **Disabled by default. Opt-in only** (per `sources.risk_acknowledged`,
    see `sources/base.py`) if ever built — never silently enabled, never
    scheduled as a default part of the watch cycle.
  - **Not needed for current bid/Buy It Now correctness** — that gap is
    already closed via the official API (`current_bid_price`/
    `buy_it_now_price` on `Listing`, populated by `EbaySource.search()`).
    This item exists only for watcher-count-style signals the API can't
    provide, and is not currently justified by real usage.

---

## Guiding principle

This document guides direction, not sequence. Real usage always outranks it —
if using Product Finder points somewhere this roadmap doesn't mention, follow
that instead and update this document later, not the other way round.

A useful way to think about Product Finder is as six cooperating knowledge
layers:

1. **Catalogue** — What is this?
2. **Pricing** — Is it good value?
3. **Coverage** — Am I seeing enough of the market?
4. **Identity** — Have I already seen it?
5. **Trust** — Can I believe it?
6. **Recommendations** — Should I buy it?

New features should strengthen one of these layers rather than cutting across
several. The recommendation layer should remain the consumer of the others, not
a shortcut around them.

A second boundary matters as soon as the product becomes public: anonymous
discovery and signed-in ownership should stay separate. Searching and
click-through should not require an account; saving projects, alerts,
preferences, and long-running recommendations should.

A third boundary now matters as source count grows: **every marketplace should
be a connector, not a special case**. The rest of the engine should reason over
normalised listings, source capabilities, provenance, and confidence — not over
the accidental quirks of whichever marketplace happened to produce the row.
