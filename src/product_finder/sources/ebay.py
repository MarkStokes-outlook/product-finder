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

from ..config import ItemConfig
from ..models import Listing, ManualLink
from .base import Source

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


class EbaySource(Source):
    name = "ebay"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._token: str | None = None
        self._token_expires: float = 0.0

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
                    source=self.name,
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
