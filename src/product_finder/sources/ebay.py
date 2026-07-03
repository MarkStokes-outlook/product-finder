"""eBay UK source.

Automated via the official eBay Browse API when app credentials are configured
(https://developer.ebay.com — free developer account, client-credentials OAuth).
Falls back to manual-assisted search links otherwise. No scraping.
"""

from __future__ import annotations

import base64
import logging
import time
from urllib.parse import quote, urlencode

import requests

from .. import rate_limit
from ..config import ItemConfig
from ..models import AuctionSnapshot, Listing, ManualLink
from .base import Source

log = logging.getLogger(__name__)

# Starting pace for the search endpoint — see rate_limit.py. Low floor since
# a handful of terms back-to-back was fine before this existed; the ceiling
# just needs to be high enough that a bad run still makes *some* progress
# rather than stalling a whole watch cycle.
_MIN_DELAY = 0.5
_MAX_DELAY = 60.0

_ENDPOINTS = {
    "production": {
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.ebay.com/buy/browse/v1/item_summary/search",
        "item": "https://api.ebay.com/buy/browse/v1/item",
    },
    "sandbox": {
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "search": "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search",
        "item": "https://api.sandbox.ebay.com/buy/browse/v1/item",
    },
}


def _price_value(data: dict) -> tuple[float, str] | None:
    """A pure auction (no Buy It Now) has price=null and the current bid
    under currentBidPrice instead — without this fallback those listings
    silently vanish (float(None) raises). Same field shape on both the
    search (item_summary) and single-item (getItem) endpoints."""
    price_info = data.get("price") or data.get("currentBidPrice") or {}
    try:
        return float(price_info["value"]), str(price_info.get("currency", "GBP"))
    except (TypeError, ValueError, KeyError):
        return None


class EbaySource(Source):
    name = "ebay"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._limiter = rate_limit.RateLimiter(_MIN_DELAY, _MAX_DELAY)

    def is_automated(self) -> bool:
        ebay = self.cfg.sources.ebay
        return bool(ebay.app_id and ebay.cert_id)

    def _get_token(self) -> str:
        if self._token and self._token_expires > time.time() + 60:
            return self._token
        ebay = self.cfg.sources.ebay
        creds = base64.b64encode(f"{ebay.app_id}:{ebay.cert_id}".encode()).decode()
        resp = requests.post(
            _ENDPOINTS[ebay.env]["token"],
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
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def search(self, term: str, item: ItemConfig) -> list[Listing]:
        filters = ["itemLocationCountry:GB", "priceCurrency:GBP"]
        if item.max_price:
            filters.append(f"price:[..{item.max_price:g}]")

        def _do_request():
            resp = requests.get(
                _ENDPOINTS[self.cfg.sources.ebay.env]["search"],
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                },
                params={"q": term, "limit": "50", "filter": ",".join(filters)},
                timeout=30,
            )
            resp.raise_for_status()
            return resp

        resp = rate_limit.request_with_backoff(self._limiter, _do_request, self.name)
        listings = []
        for summary in resp.json().get("itemSummaries", []):
            priced = _price_value(summary)
            if priced is None:
                continue
            price, currency = priced
            location = summary.get("itemLocation") or {}
            listings.append(
                Listing(
                    source=self.name,
                    external_id=str(summary.get("itemId", "")),
                    title=str(summary.get("title", "")),
                    price=price,
                    currency=currency,
                    url=str(summary.get("itemWebUrl", "")),
                    location=", ".join(
                        p for p in (location.get("city"), location.get("postalCode")) if p
                    ),
                    description=str(summary.get("shortDescription", "") or ""),
                    condition=str(summary.get("condition", "") or ""),
                    buying_options=list(summary.get("buyingOptions") or []),
                    bid_count=summary.get("bidCount"),
                    end_time=summary.get("itemEndDate"),
                )
            )
        return listings

    def _fetch_item(self, external_id: str) -> dict | None:
        """Raw single-item lookup (Browse API getItem) — the shared fetch
        behind get_item() and get_item_details(). None if the item can no
        longer be fetched (e.g. eBay has since removed it)."""
        resp = requests.get(
            f"{_ENDPOINTS[self.cfg.sources.ebay.env]['item']}/{quote(external_id, safe='')}",
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        return resp.json()

    def get_item(self, external_id: str) -> AuctionSnapshot | None:
        """Single-item lookup for tracking an auction toward its close (see
        auction_watch.py) — `external_id` is the same eBay itemId stored on
        Listing.external_id from search()."""
        data = self._fetch_item(external_id)
        if data is None:
            return None
        priced = _price_value(data)
        if priced is None:
            return None
        price, currency = priced
        ended = any(
            a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
            for a in data.get("estimatedAvailabilities", [])
        )
        return AuctionSnapshot(price=price, currency=currency, bid_count=data.get("bidCount"), ended=ended)

    def get_item_details(self, external_id: str) -> dict | None:
        """Seller-declared brand/model, when eBay's own structured item
        specifics are filled in — a much more reliable signal for the
        catalogue than inferring anything from free text (see
        suggestions in catalogue.py). Returns None if unavailable (not
        every listing has these fields filled in, and casual/private
        sellers often skip them) or the item can't be fetched."""
        data = self._fetch_item(external_id)
        if data is None:
            return None
        brand = data.get("brand")
        if not brand:
            return None
        return {"brand": str(brand), "model": str(data.get("mpn") or "")}

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        links = []
        for term in item.terms:
            params = {"_nkw": term}
            if item.max_price:
                params["_udhi"] = f"{item.max_price:g}"
            if self.cfg.postcode:
                params["_stpos"] = self.cfg.postcode
                params["_sadis"] = str(self.cfg.radius_miles)
            links.append(
                ManualLink(
                    source=self.name,
                    label=f"eBay UK: {term}",
                    url=f"https://www.ebay.co.uk/sch/i.html?{urlencode(params)}",
                )
            )
        return links
