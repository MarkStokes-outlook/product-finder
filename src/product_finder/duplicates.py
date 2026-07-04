"""Fuzzy duplicate-listing detection — identity v2 candidate generation.

Canonical-URL identity (identity.py) links sightings that provably share a
platform's own ID. This module handles the case with no provable link: the
same physical item listed more than once — a seller double-listing on the
same marketplace, or cross-posting to another one. There is no identifier to
key off, only title/price/location/image similarity, so nothing here ever
merges anything: it proposes candidate pairs for a human to confirm or
dismiss (see db.scan_duplicate_candidates / the "Possible duplicates"
section on the project page). Design: docs/design/2026-07-04-fuzzy-duplicate-grouping.md.

Pure functions over plain listing rows — no sqlite knowledge, same style as
catalogue.py/price_trend.py. All thresholds are named constants below,
provisional and expected to be tuned against the real review queue.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Gates — a pair failing any of these is never proposed at all.
TITLE_SIM_MIN = 0.80          # SequenceMatcher ratio on normalised titles
TOKEN_OVERLAP_MIN = 0.5       # cheap Jaccard prefilter before SequenceMatcher
PRICE_DELTA_MAX_PCT = 50.0    # beyond this, similar titles usually mean different variants

# Confidence blend (0-100, display/ranking only — never triggers any
# automatic action; a pair stays pending forever until a human decides).
BASE_AT_MIN_SIM = 45.0        # title similarity exactly at TITLE_SIM_MIN
BASE_AT_FULL_SIM = 70.0       # identical titles
PRICE_CLOSENESS_MAX_BONUS = 15.0   # at 0% delta, linear to 0 at PRICE_DELTA_MAX_PCT
SAME_IMAGE_BONUS = 20.0       # identical image URL = almost certainly a cross-post
SAME_LOCATION_BONUS = 10.0    # same masked postcode = probably the same seller
CROSS_SOURCE_PENALTY = 10.0   # cross-marketplace pairs skip the seller-proxy gate,
                              # so they start from correspondingly lower confidence
CONFIDENCE_CAP = 99.0         # never 100 — only a human can be certain
MIN_QUEUE_CONFIDENCE = 60.0

# Safety valve: one noisy item can't flood the review queue.
MAX_PENDING_PER_ITEM = 50

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed."""
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


def _tokens(normalized_title: str) -> set[str]:
    return set(normalized_title.split())


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of normalised title tokens — a cheap prefilter so the
    quadratic SequenceMatcher pass only runs on plausible pairs."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def price_delta_pct(price_a: float, price_b: float) -> float:
    """Absolute price difference as a percentage of the cheaper price."""
    low = min(price_a, price_b)
    if low <= 0:
        return float("inf")
    return abs(price_a - price_b) / low * 100.0


def evaluate_pair(a, b) -> tuple[float, dict] | None:
    """Score two listing rows (mappings with title/price/source/location/
    image_url) as a possible same-physical-item pair. Returns (confidence,
    signals) when the pair clears every gate and MIN_QUEUE_CONFIDENCE,
    else None.

    Same-source pairs additionally require a seller proxy — matching
    non-empty location, or an identical image URL. Without one, two
    same-marketplace listings with similar titles are overwhelmingly
    *different sellers selling the same product model*: genuinely distinct
    purchasable items, not duplicates (measured on real data: 716 live
    exact-title pairs, only 167 sharing a location). Cross-source pairs
    skip that gate (location formats differ across marketplaces) but carry
    CROSS_SOURCE_PENALTY instead.
    """
    norm_a, norm_b = normalize_title(a["title"]), normalize_title(b["title"])
    if token_overlap(norm_a, norm_b) < TOKEN_OVERLAP_MIN:
        return None
    title_sim = title_similarity(norm_a, norm_b)
    if title_sim < TITLE_SIM_MIN:
        return None

    delta = price_delta_pct(a["price"], b["price"])
    if delta > PRICE_DELTA_MAX_PCT:
        return None

    same_source = a["source"] == b["source"]
    same_location = bool(a["location"]) and a["location"] == b["location"]
    same_image = bool(a["image_url"]) and a["image_url"] == b["image_url"]
    if same_source and not (same_location or same_image):
        return None

    sim_span = 1.0 - TITLE_SIM_MIN
    base = BASE_AT_MIN_SIM + (BASE_AT_FULL_SIM - BASE_AT_MIN_SIM) * (
        (title_sim - TITLE_SIM_MIN) / sim_span
    )
    confidence = base + PRICE_CLOSENESS_MAX_BONUS * (1.0 - delta / PRICE_DELTA_MAX_PCT)
    if same_image:
        confidence += SAME_IMAGE_BONUS
    if same_location:
        confidence += SAME_LOCATION_BONUS
    if not same_source:
        confidence -= CROSS_SOURCE_PENALTY
    confidence = min(confidence, CONFIDENCE_CAP)

    if confidence < MIN_QUEUE_CONFIDENCE:
        return None
    signals = {
        "title_sim": round(title_sim, 3),
        "price_delta_pct": round(delta, 1),
        "same_location": same_location,
        "same_image": same_image,
        "cross_source": not same_source,
    }
    return confidence, signals
