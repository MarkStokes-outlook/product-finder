# Roadmap

This document exists to answer one question: **if there's a free weekend to
improve Product Finder, which part of the system deserves it, and why?**

It intentionally does not answer "what are the next tasks" — that changes
weekly and belongs in code, commits, and conversation, not here. This
document should still make sense even if every implementation detail below
it has changed.

## What Product Finder actually is

Product Finder looks like a marketplace watcher, but marketplace connectors
are the least valuable part of it — they're replaceable, and mostly already
done. What compounds in value over time, and what this roadmap exists to
protect and grow, is:

- **Curated product knowledge** — the catalogue: which manufacturer/model
  products exist, and how to recognise them in the wild.
- **Accumulated pricing knowledge** — what things actually cost, new and
  used, and how that changes over time.
The long-term goal is not simply to estimate price, but to estimate confidence in that price based on the quality and quantity of supporting evidence.
- **Buying intelligence** — turning the two assets above into a judgement
  about whether a specific listing, right now, is worth acting on.

Every area below is one of these three assets, or something that protects
the system's ability to keep accumulating them honestly. If a future idea
doesn't serve one of these, it's scope creep, however interesting.

---

## Catalogue quality

The catalogue already normalises manufacturer names, rejects placeholder
brands, and discovers new products from eBay's own structured seller data
under human review. That part works and doesn't need revisiting.

What the catalogue can't yet do is **heal itself**. Coverage is entirely
dependent on sellers filling in structured brand/model fields — a private
seller writing a plain-text listing is invisible to discovery no matter how
good the deal is, which is exactly the kind of listing this app exists to
catch. And because discovery has no way to recognise "this is the same
product as something already known, just spelled differently," near-
duplicate products can accumulate with no path to reconcile them back
together.

The direction here isn't more discovery *sources* — it's making discovery
and reconciliation trustworthy enough to stop being the bottleneck: teaching
the system to recognise products from unstructured text, and giving it a way
to merge what it eventually gets wrong, both under the same human-review
discipline the structured-data path already uses.

## Listing understanding

Grading and catalogue matching both treat a listing as one simple claim: one
item, one price, one condition. That assumption is often wrong — a listing
can describe a bundle of several valuable things, a range of prices for
different variants, or a condition claim that a keyword scanner reads
backwards (a listing bragging about *no* faults getting flagged as faulty).

The system doesn't need cleverer keyword lists here — it needs a richer
model of what a listing *is* before grading or pricing tries to reason about
it. Getting this right feeds directly into deal accuracy, since a
mis-modelled listing produces a confidently wrong score.

## Deal accuracy

Deal scoring rests on three reference prices, and only one of them —
typical used price — actually improves itself over time, because it's built
from the system's own observations. The new-price side is static and
manually maintained, and there's still no real sold-price signal beyond
auction closes, so scores are ultimately bounded by how good the person
entering numbers is.

The most valuable thing to do here is bring the new-price side up to the
same self-updating standard the used-price side already has — from an
external, verifiable source, not a guess — and let scoring reason over
accumulated price history rather than a single stored number. A deal score
computed against a trend is a fundamentally better judgement than one
computed against a static estimate, independent of any UI or feature work.

## Identity resolution

**v1 shipped (canonical-URL matching).** This was live debt, not latent —
`rss.py` (generic, config-driven RSS/Atom source) has been automated
(`is_automated() -> True`) alongside eBay for a while, so a listing matching
one item's search terms on both could already be scored and alerted on
twice, quietly, for any project running both sources.

`identity.py`/`db.resolve_identity()` now recognises the one case with a
provably shared identifier: a listing's URL containing a platform's own
native ID (v1: eBay's item ID, e.g. an RSS entry that happens to link
straight to an eBay item page). Cross-source sightings sharing that ID are
linked via `listing_identities`/`listing_identity_members`; only one
("primary" — the platform's own native listing if one exists, else whichever
was seen first) counts toward alerting, price observations, and
`query_matches`/dashboard results. Every sighting still gets its own
`listings` row and `listing_matches` entry — nothing is deleted or
collapsed, so provenance and the ability to unpick a bad merge are both
preserved.

**Deliberately not solved by v1**: the same physical item independently
listed on two marketplaces with *no* shared ID (e.g. the same saw on eBay
and Gumtree) still counts twice — there's no provable identifier to key off,
only title/price similarity, and merging across marketplaces on that alone
risks silently conflating two different real items. Future work, if this
turns out to matter in practice: a fuzzy title/price candidate-grouping
mechanism, surfaced for human confirm/dismiss rather than auto-merged — that
needs its own review UI and a "don't ask again" dismiss/remember workflow
(mirroring `product_suggestions`' pending/approved/dismissed pattern), which
is meaningfully more state than v1's scope covered.

Also still unresolved (separate, smaller issue): a single listing matching
more than one item's search terms still gets scored/alerted once per item —
that's intentional (each item is a genuinely different thing to want), not
an identity bug.

## Product knowledge beyond price

The catalogue currently knows a product's identity and its price. It has no
concept of category, variant relationships, accessories, consumables, or
compatibility — so it can't tell you a listing is really two things you'd
want to track separately, or that a cheaper compatible alternative exists.

This is real long-term value, but it's also the area most likely to turn
into architecture for its own sake if pulled forward too early — it only
pays off once the catalogue underneath it is clean and de-duplicated.
Knowledge layered on a noisy foundation just gives the noise a longer reach.

## Recommendations

Today's "intelligence" is a single number computed at the moment a listing
is seen. It has no memory — it can't say whether now is a *good time* to
buy versus just a plausible number, and it has no concept of an
alternative. A genuine recommendation ("buy now," "wait," "there's a better
option") requires exactly the two things above: real price history to judge
timing against, and product knowledge to know what else could satisfy the
same want. This is the natural endpoint of the other sections, not a
parallel workstream — it has little to build on until they exist.

## Keeping the system healthy

Data accumulates with no retention policy, and the watch loop is a simple
fixed interval with no back-off. Neither is urgent today, but both are the
kind of thing that's cheap to ignore until it suddenly isn't (a slow query,
a database that's awkward to back up or reason about). This isn't a feature
area competing with the others — it's maintenance debt that should get
picked up opportunistically whenever it's touched anyway, so it never
becomes a crisis.

---

## Where AI fits

AI should show up where it can extend what the deterministic system already
does well — reading unstructured text to enrich catalogue discovery,
proposing a possible bundle or duplicate for a human to confirm — never as
a replacement for the deterministic grading, matching, and scoring that
already exists, and never as something that writes to the catalogue or
changes a score unsupervised. The existing product-suggestion queue
(machine proposes, confidence scores, human approves) is the template: every
future use of AI here should look like that pattern, not a new one.

---

## Future ideas (deliberately unscheduled)

Genuinely interesting, not currently justified by the assets above, and not
meant to be planned against:

- Browser extension
- Mobile app
- Cloud sync
- Multi-user support
- Public API
- Notification channels beyond console/webhook

---

## Guiding principle

This document guides direction, not sequence. Real usage always outranks
it — if using Product Finder points somewhere this roadmap doesn't mention,
follow that instead and update this document later, not the other way
round.
