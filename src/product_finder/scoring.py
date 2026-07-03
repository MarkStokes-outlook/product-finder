"""Warning flags, margins and deal scoring."""

from __future__ import annotations

import re

from . import grading, price_trend
from .catalogue import Product
from .config import ItemConfig
from .models import Evaluation, Listing

# Phrases that make a cheap listing a likely false bargain.
_WARNING_TERMS = {
    "spares or repairs": ["spares or repairs", "spares or repair", "spares/repairs", "for spares", "for parts", "parts only"],
    "faulty": ["faulty", "fault", "defective"],
    "not working": ["not working", "doesn't work", "does not work", "won't turn on", "wont turn on", "no power", "dead"],
    "broken": ["broken", "cracked", "cracks", "snapped"],
    "cosmetic damage": ["scratch", "scratched", "scratches", "damage", "damaged"],
    "untested": ["untested", "unable to test", "can't test", "cannot test", "sold as seen"],
    "missing battery": ["missing battery", "no battery", "no batteries", "without battery", "bare unit", "body only"],
    "no charger": ["no charger", "without charger", "missing charger", "charger not included"],
    "incomplete": ["missing parts", "incomplete", "missing motor", "missing blade", "missing lead", "no box or accessories"],
}

_GRADE_ADJUST = {
    grading.GRADE_A: 10.0,
    grading.GRADE_B: 5.0,
    grading.GRADE_C: -10.0,
    grading.SPARES: -40.0,
    grading.UNKNOWN: -5.0,
}

_PRIORITY_ADJUST = {"high": 10.0, "normal": 0.0, "low": -5.0}


def warning_flags(text: str) -> list[str]:
    """Return the list of false-bargain warning flags present in the text.
    Uses grading.phrase_present() so a negated claim ("no dead pixels", "not
    scratched") is correctly read as ruling a fault out, not reporting one —
    same negation rules grading.classify() uses, one shared primitive."""
    text = (text or "").lower()
    flags = []
    for flag, phrases in _WARNING_TERMS.items():
        if any(grading.phrase_present(text, phrase) for phrase in phrases):
            flags.append(flag)
    return flags


def margins(price: float, normal_price: float | None) -> tuple[float, float]:
    """Return (absolute margin, percentage below normal). Zero if no normal price."""
    if not normal_price or normal_price <= 0:
        return 0.0, 0.0
    margin_abs = normal_price - price
    margin_pct = (margin_abs / normal_price) * 100.0
    return round(margin_abs, 2), round(margin_pct, 1)


def is_likely_false_bargain(price: float, normal_price: float | None, flags: list[str]) -> bool:
    """Unusually cheap plus negative condition flags = probably not a bargain."""
    if not flags or not normal_price or normal_price <= 0:
        return False
    return price < normal_price * 0.5


# A listing whose *title* spells out a price range ("£95 - £299") or an
# explicit multi-item bundle can't be scored as "this exact item costs £X" —
# the listing's confirmed API price is only one point in an unknown range,
# or covers several distinct products/variants bundled into one listing.
# Title-only, deliberately never `description`: sellers routinely write
# single-item markdown framing there ("was £299, now £95", "reduced from
# £299 to £95") which isn't a real price range and would false-positive
# against a naive range pattern — a genuine multi-item/range listing almost
# always signals it in the title itself, where eBay's character limit
# forces sellers to be explicit about covering several variants/prices. A
# range spelled out only in the description (not the title) is a known,
# accepted false-negative this doesn't attempt to catch.
_PRICE_RANGE_RE = re.compile(
    r"£\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:-|to|–|—)\s*£?\s*\d[\d,]*(?:\.\d{1,2})?"
)
_MULTI_ITEM_TITLE_TERMS = [
    "job lot", "bundle of", "lot of", "multiple items",
    "various models", "various sizes", "choose from", "select from",
    "pick from", "prices from", "priced from",
]


def is_multi_item_or_price_range(listing: Listing) -> bool:
    """True if the listing's title itself signals a price range or several
    bundled items/variants, per the module-level note above."""
    title = (listing.title or "").lower()
    if _PRICE_RANGE_RE.search(title):
        return True
    return any(
        re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", title)
        for term in _MULTI_ITEM_TITLE_TERMS
    )


def is_live_auction(listing: Listing) -> bool:
    """True if `listing.price` may just be a current bid, not a committed
    price — i.e. it's an active eBay-style auction. The final price could be
    much higher (bidding concentrates in the closing seconds/minutes, so
    even an auction ending soon with several bids isn't a reliable signal —
    see scoring roadmap notes). Never treat this price as "available now"."""
    return "AUCTION" in listing.buying_options


def deal_score(
    price: float,
    normal_price: float | None,
    target_deal_price: float | None,
    grade: str,
    flags: list[str],
    priority: str = "normal",
    title: str = "",
    typical_used_price: float | None = None,
    price_trend_pct: float | None = None,
    price_trend_confidence: float = 0.0,
) -> float:
    """Score 0-100. Higher = better deal."""
    _, pct_below = margins(price, normal_price)
    score = 40.0  # neutral baseline for a fairly priced listing
    score += max(-20.0, min(pct_below, 60.0)) * 0.6
    if target_deal_price and price <= target_deal_price:
        score += 15.0
    score += _GRADE_ADJUST.get(grade, 0.0)
    score += _PRIORITY_ADJUST.get(priority, 0.0)
    score -= min(len(flags) * 8.0, 30.0)
    if title and len(title.split()) < 3:
        score -= 5.0  # vague title
    if is_likely_false_bargain(price, normal_price, flags):
        score -= 20.0
    if typical_used_price and typical_used_price > 0:
        # A saving vs. the *new* price means nothing if it's still priced
        # above what this product typically goes for used (e.g. new £200,
        # typical used £100 — £150 is a poor deal despite "saving" vs new).
        used_pct_below = (typical_used_price - price) / typical_used_price * 100.0
        if used_pct_below < 0:
            score += max(used_pct_below, -30.0) * 0.4
    # Used-price trend (see price_trend.py) — a small, confidence-scaled,
    # capped nudge on top of everything above; zero whenever there isn't
    # enough observation history to say anything (v1 is used-price only,
    # see docs/strategy/roadmap.md, "Deal accuracy").
    score += price_trend.score_adjustment(price_trend_pct, price_trend_confidence)
    return round(max(0.0, min(score, 100.0)), 1)


def effective_prices(
    item: ItemConfig, product: Product | None
) -> tuple[float | None, float | None, float | None]:
    """The (typical_new_price, target_deal_price, typical_used_price)
    actually used for scoring. typical_new_price cascades: the matched
    product's own typical new price, else its MSRP, else the item's blended
    estimate. target_deal_price prefers the product's, else the item's.
    typical_used_price only ever comes from the product (it's a per-product
    market observation, not something an item-level estimate can stand in
    for) and is None when there's no product or no observations yet."""
    normal_price = (
        (product.typical_new_price or product.msrp) if product else None
    ) or item.normal_price
    target_deal_price = (
        product.target_deal_price if product and product.target_deal_price else item.target_deal_price
    )
    typical_used_price = product.typical_used_price if product else None
    return normal_price, target_deal_price, typical_used_price


def evaluate(listing: Listing, item: ItemConfig, product: Product | None = None) -> Evaluation:
    """Full evaluation of a listing against a wanted item.

    `product` is the catalogue entry (see `catalogue.match()`) the listing
    resolved to, if any — its price overrides the item's blended figures so
    a £600 Makita and a £50 own-brand tool aren't judged against the same
    "normal" price just because they share a search term.
    """
    normal_price, target_deal_price, typical_used_price = effective_prices(item, product)
    grade = grading.classify(listing.text)
    flags = warning_flags(listing.text)
    if is_live_auction(listing):
        flags = flags + ["live auction"]
    elif typical_used_price and typical_used_price > 0 and listing.price > typical_used_price * 1.1:
        # >10% above the typical used price — not just noise around the median.
        flags = flags + ["above typical used price"]
    multi_item = is_multi_item_or_price_range(listing)
    if multi_item:
        flags = flags + ["multiple items / price range"]
    margin_abs, margin_pct = margins(listing.price, normal_price)
    # Never a confirmed "target met" off an ambiguous range/bundle price —
    # same reasoning as never treating a live auction's current bid as a
    # committed price (see is_live_auction): we don't know which item in
    # the bundle, or which end of the range, the price actually applies to.
    under_target = bool(
        target_deal_price and listing.price <= target_deal_price and not multi_item
    )
    score = deal_score(
        price=listing.price,
        normal_price=normal_price,
        target_deal_price=target_deal_price,
        grade=grade,
        flags=flags,
        priority=item.priority,
        title=listing.title,
        typical_used_price=typical_used_price,
        price_trend_pct=product.price_trend_pct if product else None,
        price_trend_confidence=product.price_trend_confidence if product else 0.0,
    )
    return Evaluation(
        grade=grade,
        flags=flags,
        margin_abs=margin_abs,
        margin_pct=margin_pct,
        under_target=under_target,
        deal_score=score,
    )


def excluded(listing: Listing, item: ItemConfig) -> bool:
    """True if the listing title trips any of the item's exclude terms."""
    title = listing.title.lower()
    return any(term.lower() in title for term in item.exclude_terms)
