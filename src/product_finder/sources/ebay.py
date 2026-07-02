"""eBay UK source.

Automated via the official eBay Browse API when app credentials are configured
(https://developer.ebay.com — free developer account, client-credentials OAuth).
Falls back to manual-assisted search links otherwise. No scraping.
"""

from __future__ import annotations

import base64
import logging
import time
from urllib.parse import urlencode

import requests

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink

NAME = "ebay"
log = logging.getLogger(__name__)

_ENDPOINTS = {
    "production": {
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.ebay.com/buy/browse/v1/item_summary/search",
    },
    "sandbox": {
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search",
    },
}

_token_cache: dict[str, tuple[str, float]] = {}


def is_automated(cfg: AppConfig) -> bool:
    return bool(cfg.sources.ebay.app_id and cfg.sources.ebay.cert_id)


def _get_token(cfg: AppConfig) -> str:
    ebay_cfg = cfg.sources.ebay
    cache_key = ebay_cfg.app_id
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    creds = base64.b64encode(f"{ebay_cfg.app_id}:{ebay_cfg.cert_id}".encode()).decode()
    resp = requests.post(
        _ENDPOINTS[ebay_cfg.env]["token"],
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    _token_cache[cache_key] = (token, time.time() + int(payload.get("expires_in", 7200)))
    return token


def search(term: str, item: ItemConfig, cfg: AppConfig) -> list[Listing]:
    """Search eBay UK via the Browse API. Raises on network/auth errors."""
    token = _get_token(cfg)
    filters = ["itemLocationCountry:GB", "priceCurrency:GBP"]
    if item.max_price:
        filters.append(f"price:[..{item.max_price:g}]")
    resp = requests.get(
        _ENDPOINTS[cfg.sources.ebay.env]["search"],
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        },
        params={"q": term, "limit": "50", "filter": ",".join(filters)},
        timeout=30,
    )
    resp.raise_for_status()
    listings = []
    for summary in resp.json().get("itemSummaries", []):
        price_info = summary.get("price") or {}
        try:
            price = float(price_info.get("value"))
        except (TypeError, ValueError):
            continue
        location = summary.get("itemLocation") or {}
        listings.append(
            Listing(
                source=NAME,
                external_id=str(summary.get("itemId", "")),
                title=str(summary.get("title", "")),
                price=price,
                currency=str(price_info.get("currency", "GBP")),
                url=str(summary.get("itemWebUrl", "")),
                location=", ".join(
                    p for p in (location.get("city"), location.get("postalCode")) if p
                ),
                description=str(summary.get("shortDescription", "") or ""),
                condition=str(summary.get("condition", "") or ""),
            )
        )
    return listings


def manual_links(item: ItemConfig, cfg: AppConfig) -> list[ManualLink]:
    links = []
    for term in item.terms:
        params = {"_nkw": term}
        if item.max_price:
            params["_udhi"] = f"{item.max_price:g}"
        if cfg.postcode:
            params["_stpos"] = cfg.postcode
            params["_sadis"] = str(cfg.radius_miles)
        links.append(
            ManualLink(
                source=NAME,
                label=f"eBay UK: {term}",
                url=f"https://www.ebay.co.uk/sch/i.html?{urlencode(params)}",
            )
        )
    return links
