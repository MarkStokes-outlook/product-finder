"""Offer intelligence for Buy It Now / Best Offer listings — suggested safe/
normal/cheeky offer prices, with a plain-English explanation. Pure functions
only, same style as price_trend.py/auction_trajectory.py: no DB access, no
class state, no black-box scoring.

This never submits, automates, or otherwise acts on an offer — it only
suggests numbers for a human to use. Nothing here calls any marketplace API
to place an offer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Listing

# --- Tuning constants (provisional — same practice as price_trend.py's
# tuning constants; revisit once there's real accepted-offer data to check
# these against). Discounts are off the *asking* price by default; anchored
# toward the reference price instead when the seller is pricing above market.
SAFE_DISCOUNT_PCT = 10.0
NORMAL_DISCOUNT_PCT = 15.0
CHEEKY_DISCOUNT_PCT = 22.5
#: When evidence is thin (no reference price, or an unclear condition grade),
#: cap how aggressive the cheeky offer is allowed to be — an aggressive low
#: offer needs real evidence behind it, not just a flat percentage.
CONSERVATIVE_CHEEKY_DISCOUNT_PCT = NORMAL_DISCOUNT_PCT
#: A grade this vague means "we don't actually know the condition" — same
#: values grading.classify() can return.
UNCLEAR_GRADES = (None, "", "unknown")


@dataclass
class OfferSuggestion:
    supports_offers: bool
    safe_offer: float | None
    normal_offer: float | None
    cheeky_offer: float | None
    confidence: str  # "low" | "medium" | "high"
    explanation: str


def detect_offer_support(listing: Listing) -> bool:
    """Source-agnostic: any connector that populates buying_options with
    BEST_OFFER is treated the same way, never special-cased by source name
    (see sources/base.py's design philosophy). Real eBay evidence for this
    field: tests/fixtures/ebay/search_best_offer.json."""
    return "BEST_OFFER" in listing.buying_options


def _confidence(
    reference_price: float | None, grade: str | None, verified: bool,
    seller_confidence: float | None, source_confidence: float | None,
) -> str:
    if not reference_price:
        return "low"
    if grade in UNCLEAR_GRADES:
        return "low"
    confidence = "high" if (verified and grade in ("A", "B")) else "medium"
    # seller_confidence/source_confidence aren't supplied by any connector
    # today (no source declares seller identity or a confidence score yet —
    # see SourceCapabilities) — accepted here for forward compatibility, but
    # None is treated as neutral, never guessed at or defaulted to a number.
    if seller_confidence is not None and seller_confidence < 0.5:
        confidence = "low"
    if source_confidence is not None and source_confidence < 0.5:
        confidence = "low"
    return confidence


def suggest_offers(
    *,
    listing_price: float,
    reference_price: float | None,
    reference_label: str = "typical used price",
    grade: str | None = None,
    verified: bool = False,
    supports_offers: bool = True,
    seller_confidence: float | None = None,
    source_confidence: float | None = None,
) -> OfferSuggestion:
    """`reference_price` should be the best available fair-value estimate
    (prefer a product's typical_used_price; fall back to the item's blended
    normal_price) — resolve via scoring.effective_prices(), same convention
    as auction_trajectory.evaluate(). `grade` is grading.classify()'s output."""
    if not supports_offers:
        return OfferSuggestion(
            supports_offers=False, safe_offer=None, normal_offer=None, cheeky_offer=None,
            confidence="n/a", explanation="This listing does not support offers.",
        )

    safe = listing_price * (1 - SAFE_DISCOUNT_PCT / 100)
    normal = listing_price * (1 - NORMAL_DISCOUNT_PCT / 100)
    cheeky = listing_price * (1 - CHEEKY_DISCOUNT_PCT / 100)

    above_market = bool(reference_price) and listing_price > reference_price
    if above_market:
        # Seller is pricing above the market — anchor toward/below the
        # reference price instead of a flat % off an inflated asking price.
        normal = min(normal, reference_price)
        cheeky = min(cheeky, reference_price * 0.9)
        safe = min(safe, reference_price * 1.05)

    confidence = _confidence(reference_price, grade, verified, seller_confidence, source_confidence)
    if confidence == "low":
        # Thin evidence — don't let the cheeky offer be more aggressive than
        # a cautious flat discount off asking, regardless of what the
        # reference-anchoring above computed.
        cap = listing_price * (1 - CONSERVATIVE_CHEEKY_DISCOUNT_PCT / 100)
        cheeky = max(cheeky, cap)

    safe, normal, cheeky = round(safe, 2), round(normal, 2), round(cheeky, 2)

    if reference_price:
        base = f"Listed at £{listing_price:.0f}; {reference_label} is £{reference_price:.0f}."
        if above_market:
            explanation = f"{base} Seller is above market; cheeky offer suggested (£{cheeky:.0f})."
        else:
            explanation = f"{base} Try £{cheeky:.0f}–£{normal:.0f}."
    else:
        explanation = (
            f"Listed at £{listing_price:.0f}. No reference price yet — try a cautious "
            f"£{normal:.0f} (normal) up to £{safe:.0f} (safe)."
        )

    return OfferSuggestion(
        supports_offers=True, safe_offer=safe, normal_offer=normal, cheeky_offer=cheeky,
        confidence=confidence, explanation=explanation,
    )
