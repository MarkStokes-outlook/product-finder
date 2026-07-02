"""Warning flags, margins and deal scoring."""

from __future__ import annotations

import re

from . import grading
from .config import ItemConfig
from .models import Evaluation, Listing

# Phrases that make a cheap listing a likely false bargain.
_WARNING_TERMS = {
    "spares or repairs": ["spares or repairs", "spares or repair", "spares/repairs", "for spares", "for parts", "parts only"],
    "faulty": ["faulty", "fault", "defective"],
    "not working": ["not working", "doesn't work", "does not work", "won't turn on", "wont turn on", "no power", "dead"],
    "broken": ["broken", "cracked", "snapped"],
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
    """Return the list of false-bargain warning flags present in the text."""
    text = (text or "").lower()
    flags = []
    for flag, phrases in _WARNING_TERMS.items():
        for phrase in phrases:
            if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
                flags.append(flag)
                break
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


def deal_score(
    price: float,
    normal_price: float | None,
    target_deal_price: float | None,
    grade: str,
    flags: list[str],
    priority: str = "normal",
    title: str = "",
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
    return round(max(0.0, min(score, 100.0)), 1)


def evaluate(listing: Listing, item: ItemConfig) -> Evaluation:
    """Full evaluation of a listing against a wanted item."""
    grade = grading.classify(listing.text)
    flags = warning_flags(listing.text)
    margin_abs, margin_pct = margins(listing.price, item.normal_price)
    under_target = bool(item.target_deal_price and listing.price <= item.target_deal_price)
    score = deal_score(
        price=listing.price,
        normal_price=item.normal_price,
        target_deal_price=item.target_deal_price,
        grade=grade,
        flags=flags,
        priority=item.priority,
        title=listing.title,
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
