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
from functools import lru_cache
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

    `price_trend_pct`/`price_trend_confidence` are the used-price trend
    (see price_trend.py) cached alongside `typical_used_price` by the same
    call — a signed percent change and a 0-1 confidence, both None/0 until
    there's enough observation history to say anything.
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
    price_trend_pct: float | None = None
    price_trend_confidence: float = 0.0
    # wanted=False = "knowledge only": still matched, so identification and
    # price history keep working, but never alerted or shown as a deal —
    # for products that are real but not what the item is after (e.g. old
    # CPU generations under a current-gen item). Archived stops matching
    # entirely; wanted only stops surfacing.
    wanted: bool = True

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern | None:
    """Compiled word-boundary pattern for a match term, tolerant of
    spacing/punctuation variance inside model numbers: sellers write
    "KGS 216 M", "KGS216M" and "KGS-216M" for the same product, and a
    matcher that treats those as different strings quietly regenerates
    catalogue noise (unmatched listings re-enter suggestion churn and come
    back as near-duplicate products).

    The term is split into letter/digit runs and any separators between
    them become optional: "CT15" matches "CT 15" and vice versa. Outer
    word boundaries are kept, so "CT15" still takes nothing from
    "connect 15" or "CT 150"."""
    tokens = re.findall(r"[^\W\d_]+|\d+", term.lower())
    if not tokens:
        return None
    return re.compile(
        r"(?<!\w)" + r"[\s\-./]*".join(re.escape(t) for t in tokens) + r"(?!\w)"
    )


def _matches(text: str, term: str) -> bool:
    # Word-boundary match, same style as grading._matches_any /
    # scoring.excluded, so "saw" doesn't match "sawdust" — but spacing-
    # insensitive within model numbers (see term_pattern).
    pattern = term_pattern(term.lower())
    return pattern is not None and pattern.search(text) is not None


def model_key(value: str) -> str:
    """Canonical identity key for a manufacturer/model string: lowercase,
    alphanumerics only. "KGS 216 M", "KGS216M" and "kgs-216-m" share one
    key — used for suggestion dedup, the product-create guard, and the
    duplicate-product sweep, so spacing variants can never accumulate as
    separate suggestions or products."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


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


# --- Suggestion normalisation ----------------------------------------------------
#
# eBay's brand/mpn fields are seller-typed free text on many listing flows,
# so the same real brand shows up with different casing ("WAGNER" / "Wagner"
# / "wagner"), and the field is sometimes a placeholder ("Does Not Apply"),
# a generic catch-all ("Unbranded"), or a seller's own store name rather
# than a manufacturer at all. None of this is inferred or AI-assisted —
# plain deterministic rules, same style as grading.py/scoring.py — so a
# suggestion's provenance stays fully explainable.

# Known casing variants -> canonical display form. Deliberately small and
# meant to grow over time as new brands are seen; unknown brands are left
# in whatever casing they arrived in rather than guessed at (blindly
# title-casing would mangle names like "DeWalt" or "iRobot").
BRAND_ALIASES: dict[str, str] = {
    "graco": "Graco",
    "wagner": "Wagner",
    "titan": "Titan",
    "tritech": "TriTech",
}

# Manufacturer values that are placeholders, not real brands. Exact match
# (after trim + lowercase) — a substring check risks false positives.
_JUNK_MANUFACTURERS = {
    "unbranded",
    "unbranded/generic",
    "branded",
    "generic",
    "after market",
    "aftermarket",
    "does not apply",
    "dose not apply",
    "n/a",
    "na",
    "unknown",
    "not specified",
}

# Manufacturer values that would otherwise trip looks_like_seller_name()
# but are genuine brands — add here if a real manufacturer gets falsely
# suppressed. Compared case-insensitively.
MANUFACTURER_ALLOWLIST: set[str] = set()

# Substrings suggesting a storefront/username rather than a manufacturer.
_SELLER_NAME_KEYWORDS = (
    "store", "shop", "outlet", "direct", "trading", "wholesale", "warehouse",
    "supplies", "retail", "seller", "official", "ltd", "limited", "llc", "inc",
)

_MODEL_NULL_VALUES = {
    "", "-", "does not apply", "dose not apply", "n/a", "unknown",
    # Extraction/seller placeholders seen in real suggestion data ("Herman
    # Miller NOT FOUND" had 45 sightings) — a placeholder model must merge
    # into the brand-only suggestion, not stand as a distinct "product".
    "not found", "none", "null", "0", "no model", "various",
    "see description", "see title", "see photos", "see pictures",
}


def normalize_manufacturer(raw: str) -> str:
    """Trim, then canonicalise casing via BRAND_ALIASES for known brands.
    Unrecognised brands are trimmed only — their original casing is kept
    rather than guessed at."""
    trimmed = (raw or "").strip()
    if not trimmed:
        return ""
    return BRAND_ALIASES.get(trimmed.lower(), trimmed)


def is_junk_manufacturer(manufacturer: str) -> bool:
    """True for placeholder/non-brand values like "Does Not Apply" or
    "Unbranded" — never worth suggesting as a catalogue product."""
    return manufacturer.strip().lower() in _JUNK_MANUFACTURERS


def looks_like_seller_name(manufacturer: str) -> bool:
    """Heuristic, deterministic check for "this is a storefront/username,
    not a brand" — conservative on purpose, since a false positive here
    silently suppresses a real manufacturer. Skipped entirely for anything
    in MANUFACTURER_ALLOWLIST."""
    value = manufacturer.strip().lower()
    if not value or value in {a.lower() for a in MANUFACTURER_ALLOWLIST}:
        return False
    if any(keyword in value for keyword in _SELLER_NAME_KEYWORDS):
        return True
    if "_" in value:
        return True
    if len(value) > 6 and any(ch.isdigit() for ch in value):
        return True
    return False


def normalize_model(raw: str | None) -> str:
    """Trim, and collapse placeholder values ("-", "N/A", "Unknown", etc.)
    to '' — treated as "no model" throughout. Meaningful model numbers are
    preserved as-is."""
    trimmed = (raw or "").strip()
    return "" if trimmed.lower() in _MODEL_NULL_VALUES else trimmed


def normalize_suggestion(manufacturer: str, model: str | None) -> tuple[str, str] | None:
    """Normalise a raw (manufacturer, model) sighting for suggestion
    storage. Returns None if the manufacturer should be rejected outright —
    a junk placeholder or something that looks like a seller's store name
    rather than a manufacturer — so it never becomes a suggestion at all."""
    manufacturer = normalize_manufacturer(manufacturer)
    if not manufacturer or is_junk_manufacturer(manufacturer) or looks_like_seller_name(manufacturer):
        return None
    return manufacturer, normalize_model(model)


# --- Accessory / spare-part detection (suspect products) --------------------------
#
# Sellers put *part numbers* in the model field for accessories (dust bags,
# spray tips, repair kits), so "approve anything with a manufacturer and
# model" quietly fills the catalogue with consumables. These signals expose
# them from the evidence their own matched listings provide. Deterministic
# and explainable, same style as grading.py — no inference.

# Words that, appearing in a matched listing's title, suggest the "product"
# is really an accessory/consumable/spare for the wanted item. Word-boundary
# matched (so "tip" doesn't fire on "tipped").
ACCESSORY_KEYWORDS = (
    "bag", "bags", "filter", "filters", "tip", "tips", "nozzle", "nozzles",
    "hose", "seal", "gasket", "kit", "spare", "spares", "replacement",
    "attachment", "adapter", "adaptor", "cable", "bracket", "mount",
    "cover", "lid", "brush", "brushes", "pad", "pads", "belt", "blade",
    "blades", "wheel", "castor", "detector", "stand", "tripod", "battery",
    "charger", "case", "sticker", "manual",
)

_PART_NUMBER_PATTERNS = (
    re.compile(r"^\d{6,}$"),                # bare EAN/article number: 2371069
    re.compile(r"^\d\.\d{3}-\d{3}\.\d$"),   # Kärcher part style: 2.863-314.0
)


def looks_like_part_number(model: str) -> bool:
    """True when a model string is shaped like a parts-catalogue number
    rather than a product model. Supporting signal only — some brands
    (Wagner) use bare article numbers for real products too, so this must
    never condemn a product on its own."""
    return any(p.match(model.strip()) for p in _PART_NUMBER_PATTERNS)


def accessory_title_share(titles: Sequence[str]) -> float:
    """Fraction of listing titles containing at least one accessory
    keyword. Titles are what sellers write to be found, so an accessory
    listing almost always names itself one."""
    if not titles:
        return 0.0
    hits = sum(
        1 for title in titles
        if any(_matches(title.lower(), kw) for kw in ACCESSORY_KEYWORDS)
    )
    return hits / len(titles)


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
