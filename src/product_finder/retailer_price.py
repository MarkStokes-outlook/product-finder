"""Retailer price discovery via SearXNG — brings a product's
`typical_new_price` up to the same self-updating, externally-verified
standard `typical_used_price` already has (see docs/strategy/roadmap.md,
"Deal accuracy").

Two deliberately separate stages, per real-world testing against Mark's own
SearXNG instance:

Stage 1 (`search_candidates`) — search for retailer pages for a product,
fetch each one, and extract a structured price where available. Matching a
search result to the *correct* product is a genuine identity-resolution
problem (a listing for "Makita LS0815FL" can surface reviews, marketplace
listings, and unrelated products alongside the real retailer page) — this
module doesn't try to solve that automatically. It only ever proposes
ranked candidates; a human always picks the canonical URL via the review
queue (see db.record_price_candidates / db.approve_price_candidate).

Stage 2 (`fetch_price`, reused directly) — once a URL has been
human-approved, refreshing its price is a deterministic re-fetch of a
known-good page. No further searching or matching involved.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import requests

from . import db
from .config import SearxngConfig

log = logging.getLogger(__name__)

# Deterministic ranking boost for domains known to be real UK retailers, as
# opposed to review sites/marketplaces/aggregators that happen to mention a
# price. Small and meant to grow over time — same style as
# catalogue.BRAND_ALIASES. Display/ranking only; never gates a candidate
# outright (an unknown retailer can still be the right answer).
KNOWN_UK_RETAILER_DOMAINS = {
    "screwfix.com", "toolstation.com", "axminstertools.com", "diy.com",
    "wickes.co.uk", "homebase.co.uk", "amazon.co.uk", "argos.co.uk",
    "currys.co.uk", "very.co.uk", "johnlewis.com", "ryobitools.co.uk",
    "machinemart.co.uk",
}

# A plain default `requests` user agent gets a flat block from at least one
# real UK retailer tested during design (diy.com returned 503) — a normal
# browser UA is the difference between "page fetched" and "silently blocked".
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_MICRODATA_PRICE_RE = re.compile(
    r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE
)
_MICRODATA_CURRENCY_RE = re.compile(
    r'itemprop=["\']priceCurrency["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE
)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _price_from_json_ld(html: str) -> tuple[float, str] | None:
    for raw in _JSON_LD_RE.findall(html):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        blocks = data if isinstance(data, list) else [data]
        # @graph is a common wrapper for multiple entities in one block.
        entries = [e for b in blocks if isinstance(b, dict) for e in (b.get("@graph") or [b])]
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("@type") != "Product":
                continue
            offers = entry.get("offers")
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if not isinstance(offers, dict):
                continue
            try:
                return float(offers["price"]), str(offers.get("priceCurrency", ""))
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _price_from_microdata(html: str) -> tuple[float, str] | None:
    price_match = _MICRODATA_PRICE_RE.search(html)
    if not price_match:
        return None
    try:
        price = float(price_match.group(1))
    except ValueError:
        return None
    currency_match = _MICRODATA_CURRENCY_RE.search(html)
    return price, (currency_match.group(1) if currency_match else "")


def fetch_price(url: str, timeout: int) -> dict | None:
    """Fetch a retailer page and extract a structured price — JSON-LD
    first, falling back to schema.org microdata (real UK retailers use
    both; Axminster Tools, for one, only has microdata, no JSON-LD at all).
    Returns None on any failure: unreachable, non-200, no parseable
    structured price, or a currency other than GBP (this app is UK-only,
    same filter the eBay source already applies) — never raises."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("Retailer page fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("Retailer page fetch for %s returned HTTP %s", url, resp.status_code)
        return None
    result = _price_from_json_ld(resp.text) or _price_from_microdata(resp.text)
    if result is None:
        return None
    price, currency = result
    if currency and currency.upper() != "GBP":
        return None
    return {"price": price, "currency": "GBP"}


def _confidence(manufacturer: str, model: str, url: str, title: str) -> float:
    """Deterministic 0-100 ranking score — display/ordering only. Never
    used to auto-select a candidate; a human always makes the final call
    (see module docstring)."""
    haystack = f"{url} {title}".lower()
    score = 30.0
    if model and len(model) >= 3 and model.lower() in haystack:
        score += 40.0
    if manufacturer and manufacturer.lower() in haystack:
        score += 20.0
    if _domain(url) in KNOWN_UK_RETAILER_DOMAINS:
        score += 10.0
    return min(100.0, score)


def search_candidates(manufacturer: str, model: str, cfg: SearxngConfig) -> list[dict]:
    """Search SearXNG for retailer pages for (manufacturer, model), fetch
    each result, and keep only the ones with a parseable GBP price — ranked
    by confidence for human review. Degrades to an empty list (never
    raises) if disabled, SearXNG is unreachable, or nothing parseable
    turned up — a background discovery service should never fail the
    caller because an optional web search came up empty."""
    if not cfg.enabled:
        return []
    query = f"{manufacturer} {model}".strip()
    if not query:
        return []
    try:
        resp = requests.get(
            f"{cfg.base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "categories": "general"},
            headers=_HEADERS,
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("SearXNG search skipped (unavailable): %s", exc)
        return []

    candidates = []
    for result in results[: cfg.max_results]:
        url = result.get("url")
        if not url:
            continue
        priced = fetch_price(url, cfg.timeout)
        if priced is None:
            continue
        candidates.append({
            "url": url,
            "domain": _domain(url),
            "price": priced["price"],
            "currency": priced["currency"],
            "confidence": _confidence(manufacturer, model, url, result.get("title", "")),
        })
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def run_discovery_and_refresh(conn, cfg) -> None:
    """One pass of both stages, called once per `runner.run_once()` cycle —
    cheap when there's nothing to do, since both queries are narrowly
    scoped (products never searched yet, or approved URLs stale beyond
    `refresh_interval_hours`). No-op entirely when `cfg.searxng.enabled` is
    False, so turning the feature off also stops any background fetching,
    not just new discovery."""
    searxng_cfg = cfg.searxng
    if not searxng_cfg.enabled:
        return

    for row in db.list_products_needing_price_search(conn):
        candidates = search_candidates(row["manufacturer"], row["model"] or "", searxng_cfg)
        db.record_price_candidates(conn, row["id"], candidates)

    for row in db.list_products_due_for_price_refresh(conn, searxng_cfg.refresh_interval_hours):
        result = fetch_price(row["canonical_price_url"], searxng_cfg.timeout)
        db.record_price_refresh(conn, row["id"], result, domain=_domain(row["canonical_price_url"]))
