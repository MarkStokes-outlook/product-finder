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
from .base import Source, SourceCapabilities

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


def _current_bid_and_bin(data: dict) -> tuple[float | None, float | None]:
    """currentBidPrice and (when FIXED_PRICE is also present) the Buy It Now
    price, kept as two distinct values — never merged, unlike _price_value()'s
    fallback. Real captures confirm both are present and independent when a
    listing has both AUCTION and FIXED_PRICE (see
    tests/fixtures/ebay/README.md), and that currentBidPrice is present even
    at zero bids (equal to minimumPriceToBid then) — never absent — so there
    is no separate "starting price" case to handle. Same field shape on both
    the search (item_summary) and single-item (getItem) endpoints, used by
    both search() and get_item()."""
    current_bid = None
    bid_info = data.get("currentBidPrice")
    if bid_info:
        try:
            current_bid = float(bid_info["value"])
        except (TypeError, ValueError, KeyError):
            current_bid = None

    buy_it_now_price = None
    if "FIXED_PRICE" in (data.get("buyingOptions") or []):
        bin_info = data.get("price") or {}
        try:
            buy_it_now_price = float(bin_info["value"])
        except (TypeError, ValueError, KeyError):
            buy_it_now_price = None

    return current_bid, buy_it_now_price


class EbaySource(Source):
    name = "ebay"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._limiter = rate_limit.RateLimiter(_MIN_DELAY, _MAX_DELAY)

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            automated=True,
            compliance="official eBay Browse API (application token)",
            account_risk="none",
            compliance_mode="official",
            can_run_unattended=True,
            requires_user_auth=False,
            requires_manual_input=False,
            is_official_api=True,
            rate_limit_class="official-api-standard",
            recommended_schedule="every watch cycle",
            freshness="realtime",
            supports_enrichment=True,
            provides_images=True,
            provides_end_time=True,
            provides_structured_attributes=True,
            provides_auctions=True,
            provides_auction_snapshot=True,
            provides_offers=True,
            provides_seller_identity=False,  # seller data exists in raw payloads, not mapped yet
            provides_location=True,
            notes="Auction current bids are captured but never treated as "
                  "committed prices; getItem enrichment supplies brand/MPN.",
        )

    def is_automated(self) -> bool:
        # Declared automated, but only operable once API credentials exist —
        # readiness is config-dependent, the capability class is not.
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
            # Counterintuitively, thumbnailImages[0] is the LARGE render
            # (~1200-1600px) and `image` the 225px one — verified live against
            # the production Browse API. Prefer the big one; cards scale down.
            thumbs = summary.get("thumbnailImages") or []
            image_url = (thumbs[0].get("imageUrl") if thumbs else None) or (
                summary.get("image") or {}
            ).get("imageUrl")
            current_bid, buy_it_now_price = _current_bid_and_bin(summary)
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
                    image_url=image_url,
                    current_bid_price=current_bid,
                    buy_it_now_price=buy_it_now_price,
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
        auction_watch.py) and, now, for recording each poll as a snapshot
        observation — `external_id` is the same eBay itemId stored on
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
        current_bid, buy_it_now_price = _current_bid_and_bin(data)
        shipping_price = None
        shipping_opts = data.get("shippingOptions") or []
        if shipping_opts:
            cost = shipping_opts[0].get("shippingCost") or {}
            try:
                shipping_price = float(cost["value"])
            except (TypeError, ValueError, KeyError):
                shipping_price = None
        return AuctionSnapshot(
            price=price,
            currency=currency,
            bid_count=data.get("bidCount"),
            ended=ended,
            current_bid=current_bid,
            buy_it_now_price=buy_it_now_price,
            shipping_price=shipping_price,
            # Not exposed by the Browse API on any endpoint we have access to
            # — confirmed absent from real captures, not guessed as missing.
            watch_count=None,
            view_count=None,
            raw=data,
        )

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
