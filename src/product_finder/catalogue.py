"""Manufacturer/model product catalogue.

An item's search terms (e.g. "mitre saw") can match wildly different
products at wildly different price points, so scoring every match against
one blended `normal_price`/`target_deal_price` skews the deal score. This
module resolves a listing to a specific tracked product (if any) so it can
be scored against that product's own price instead.

`match()` is intentionally the only entry point and knows nothing about
scoring, the database, or Flask — it takes text and a list of candidate
products and returns the best match. Today that's a plain keyword lookup,
but nothing else in the pipeline depends on that: a future AI-assisted
matcher can replace or wrap this function without touching runner.py or
scoring.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class Product:
    """A known manufacturer/model tracked under one item.

    Three distinct reference prices, because "normal price" turned out to
    hide real differences:
    - `msrp` — the manufacturer's list price. Informational only; often
      stale (retailers rarely sell at MSRP for long either way).
    - `typical_new_price` — what it actually costs to buy new right now.
      This is what scoring treats as "the new price" — manually maintained
      for now (see catalogue-pricing roadmap for automating it).
    - `typical_used_price` — a rolling median of observed second-hand
      asking prices for this product, maintained automatically by
      `db.record_price_observation()`. Never set by hand.
    """

    id: int
    item_id: int
    manufacturer: str
    model: str = ""
    match_terms: list[str] = field(default_factory=list)
    msrp: float | None = None
    typical_new_price: float | None = None
    typical_used_price: float | None = None
    target_deal_price: float | None = None
    archived: bool = False

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()


def _matches(text: str, term: str) -> bool:
    # Word-boundary match, same style as grading._matches_any /
    # scoring.excluded, so "saw" doesn't match "sawdust".
    return re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", text) is not None


# Confidence for a catalogue *suggestion* sourced from structured,
# seller-declared data (eBay's brand/mpn item specifics) rather than free
# text — starts fairly high since it's not inferred, then climbs as more
# independent listings corroborate the same manufacturer/model. Starting
# points, not calibrated against real usage yet — tune once there's data.
SUGGESTION_BASE_CONFIDENCE = 70.0
SUGGESTION_CORROBORATION_STEP = 8.0
SUGGESTION_MAX_CONFIDENCE = 99.0


def suggestion_confidence(sighting_count: int) -> float:
    """Confidence score (0-100) for a pending product suggestion, given how
    many independent listings have produced the same manufacturer/model.
    Never reaches 100 — even seller-declared fields can be wrong."""
    return min(
        SUGGESTION_MAX_CONFIDENCE,
        SUGGESTION_BASE_CONFIDENCE + max(0, sighting_count - 1) * SUGGESTION_CORROBORATION_STEP,
    )


def match(text: str, products: Sequence[Product]) -> Product | None:
    """Resolve listing text (title + description) to the most specific
    matching catalogue product, or None if nothing matches.

    "Most specific" = longest matching term, so a full model/SKU term (e.g.
    "LS1019L") wins over a bare manufacturer term (e.g. "Makita") when a
    listing contains both. Ties keep whichever product was checked first.
    Archived products are never matched.
    """
    text = (text or "").lower()
    best: Product | None = None
    best_len = -1
    for product in products:
        if product.archived:
            continue
        for term in product.match_terms:
            term = term.strip()
            if term and len(term) > best_len and _matches(text, term):
                best, best_len = product, len(term)
    return best
