# Implementation notes — deal_score recalibration

**Date:** 2026-07-04
**Commit:** `6037a9f`
**Scope:** `src/product_finder/scoring.py`, `tests/test_scoring.py`, README Known Limitations
**Status:** shipped, 316 tests passing. Explicitly an **interim** calibration — see
"Architectural direction" at the end.

## Problem

The deal score had saturated into uselessness as a ranking signal: on the real
database, 2,851 of 4,251 clean (unflagged) matches scored exactly 100, and 95%
scored ≥ 70. Every listing looked "hot"; the spotlight and sort orders carried
no information.

## Root cause (measured, not assumed)

Term-by-term decomposition of every clean match against the live DB found two
stacked causes:

1. **Formula headroom.** The old additive maximum was 111 against a clamp of
   100 (40 baseline + 36 margin cap + 15 target bonus + 10 grade A + 10 high
   priority). 43% of clean matches sat at exactly 111 pre-clamp. Worse, the
   positive terms were strongly correlated, so they added no discrimination:
   among 100-scorers the target bonus fired 100% of the time (anything 90%
   below "normal" is trivially under target), grade A covered 84% (73% of the
   entire clean population is grade A), and high priority covered 84%.

2. **Accessory pollution — the dominant *data* cause, and not what it first
   looked like.** The working hypothesis was inflated `normal_price`
   references. Wrong: the normals are plausible for the wanted products. The
   real issue is that only **5.4%** of clean matches resolve to a catalogue
   product, so 95% are scored against the item's blended `normal_price` — and
   the cheap "perfect deals" are overwhelmingly accessories and spare parts
   caught by the item's search terms. Measured examples: a £1.17 saw-blade
   screw, £4 dust-hose adaptors, and vape pods (sharing the "SP6000" model
   string) all scored 100 against £500–600 item normals, because "99% below
   normal" pinned the margin term to its cap. Median discount among
   100-scorers was 90% — these are not bargains, they are identity errors.

## What shipped

All thresholds are **named constants at the top of `scoring.py`** so they can
be re-tuned against real data without archaeology.

### 1. Inverted-U margin term (`margin_term()`)

The margin reward now rises linearly (×0.6/pct) to **+30 at 50% below** the
reference price, plateaus to 70%, then — for **unverified** matches only —
decays to +10 at 85% and on down to a floor of −10. Rationale: past ~70% off,
an unverified discount is more likely a wrong-product match than a bargain, so
deeper should score *lower*, not higher.

**"Verified" = the listing resolved to a catalogue product** (`catalogue.match()`),
meaning the reference price genuinely describes this product. Verified matches
keep the plateau at any depth — a trusted deep discount is not punished.
(Whether a 95%-off *verified* listing is a scam is a trust-layer question, per
the roadmap, deliberately not conflated with deal quality here.)

### 2. Implausible-price gate (`is_price_implausible()` + warning flag)

Unverified and priced below **12% of the reference price** → `evaluate()` adds
a `price implausible for item` warning flag. Consequences, all via existing
mechanisms rather than new plumbing:

- Excluded from spotlight/hero selection (existing `flagged=False` filter).
- `under_target` forced `False` — same reasoning as the multi-item/price-range
  flag: this price almost certainly isn't *this item's* price, so it can't
  meet the item's target.
- No target bonus in the score; the ordinary flag penalty and the existing
  false-bargain penalty then sink these listings to ~0 naturally.

### 3. Bonus deflation

Baseline 40 → 35, target bonus 15 → 10, grade A +10 → +5, grade B +5 → +2
(C/spares/unknown unchanged). The additive maximum is now 88 (with maximum
favourable trend), so 100 is unreachable and the top of the range
discriminates again. A regression test pins this.

### 4. Priority removed from the score entirely

Architectural decision (Mark's): **deal score answers "how objectively good is
this deal?"; priority answers "how much do I care about this item?"** — two
different questions. Baking priority into the score would poison any future
cross-item ranking, spotlight selection, or personalised opportunity scoring
that wants to weigh them independently. `deal_score()` no longer takes a
priority parameter; `ItemConfig.priority`, its DB column, and its UI are
untouched and remain available to the future recommendation layer.

### Unchanged by design

Flags penalty, false-bargain heuristic, above-typical-used-price penalty, and
the used-price trend adjustment (`price_trend.py`) are all exactly as before.

## Verification (real data, not synthetic)

Re-ran the new `evaluate()` over all 4,587 primary matches in the live DB
(read-only):

| Metric (clean cohort) | Before | After |
|---|---|---|
| Scores of exactly 100 | 2,851 | 0 |
| Maximum | 100 | 80 |
| Median | 100 | 60 |
| Share ≥ 70 | 95% | ~30% |

1,583 matches now carry the implausible-price flag (median score 0). The top
of the ranking changed from indistinguishable accessories to genuine deals: a
£120 Herman Miller Aeron, real CPUs at ~60% off, real sliding mitre saws at
~half price.

## Knock-on notes for downstream consumers

- **Stored scores rewrite lazily**: `listing_matches.deal_score` refreshes as
  listings re-match each watch cycle. The running `watch` process must be
  restarted to load the new code; the DB then converges within a cycle.
- **"Hot" thresholds not retuned**: `HOT_DEAL_SCORE = 70` (web) and the
  `deal_score >= 70` dashboard stat now mean "top ~30% of clean matches".
  Sane semantics, but worth revisiting once real scores settle.
- `deal_score()` gained a `verified: bool` keyword and lost `priority` — any
  future direct caller should pass `verified=bool(product)`.

## Architectural direction (why this is interim)

The formula changes treat the *symptom* well enough to make ranking useful
today, but the measured root cause is **catalogue identity and
classification**: 95% of matches can't be verified because the catalogue
doesn't cover them, and the system cannot distinguish a complete product from
an accessory, spare part, or bundle for it. The inverted-U is, honestly
stated, a statistical prior standing in for missing identity knowledge — a
genuine once-in-a-blue-moon 90%-off unverified listing will be under-scored
until the catalogue covers it (documented in README Known Limitations).

The long-term fixes remain the roadmap's existing directions: catalogue
coverage (discovery from unstructured text), product-vs-accessory/bundle
classification in listing understanding, and identity resolution. Scoring
should not grow further cleverness that hides those gaps.

Next queued piece of work (unrelated to scoring): fuzzy cross-marketplace
identity grouping with a confirm/dismiss review UI, mirroring
`product_suggestions`' pending/approved/dismissed pattern.
