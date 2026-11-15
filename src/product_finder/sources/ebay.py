"""eBay UK source.

Automated via the official eBay Browse API when app credentials are configured
(https://developer.ebay.com — free developer account, client-credentials OAuth).
Falls back to manual-assisted search links otherwise. No scraping.
"""

from __future__ import annotations

import base64
import logging
import math
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from .. import identity, rate_limit
from ..config import ItemConfig
from ..models import AuctionSnapshot, Listing, ManualLink, OfferCheck, SearchPage
from .base import ConnectorKnowledge, Source, SourceCapabilities

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


def _gbp_amount(amount: dict) -> float | None:
    if amount.get("currency") != "GBP":
        return None
    try:
        value = float(amount["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _group_id(url: str) -> str | None:
    ids = parse_qs(urlparse(url).query).get("item_group_id", [])
    return ids[0] if len(ids) == 1 else None


def _nvme_variant_capacity(data: dict) -> float | None:
    item_id = data.get("itemId") or ""
    parts = item_id.split("|")
    url = data.get("itemWebUrl") or ""
    parsed = urlparse(url)
    if (len(parts) != 3 or parts[0] != "v1" or not parts[2].isdigit() or not parts[2].strip("0")
            or parsed.scheme != "https" or parsed.hostname not in ("www.ebay.co.uk", "www.ebay.com")
            or identity.derive_canonical_key(url) != f"ebay:{parts[1]}:{parts[2]}"):
        return None
    aspects = {row["name"].casefold(): row.get("value") or "" for row in data.get("localizedAspects", [])}
    capacity = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(tb|gb)\s*", aspects.get("storage capacity", ""), re.I)
    if not capacity or not re.search(r"\bnvme\b", aspects.get("interface", ""), re.I):
        return None
    amount, unit = capacity.groups()
    return float(amount) * (1024 if unit.casefold() == "tb" else 1)


def _verify_fixed_price(listing: Listing, data: dict, postcode: str = "") -> OfferCheck:
    if data.get("itemId") != listing.external_id or not data.get("title"):
        return OfferCheck("review", "The exact eBay item could not be confirmed.")
    availability = data.get("estimatedAvailabilities") or []
    if availability and all(row.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" for row in availability):
        return OfferCheck("unavailable", "eBay reports this item is out of stock.")
    if end_time := data.get("itemEndDate"):
        try:
            expiry = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                return OfferCheck("unavailable", "eBay did not confirm this offer is still open.")
        except (AttributeError, ValueError):
            return OfferCheck("review", "Check the listing end time on eBay.")
    if ("FIXED_PRICE" not in (data.get("buyingOptions") or [])
            or "AUCTION" in data["buyingOptions"]):
        return OfferCheck("review", "Check the purchase options on eBay.")
    if listing.variant_capacity_gb:
        if _nvme_variant_capacity(data) != listing.variant_capacity_gb:
            return OfferCheck("review", "eBay did not confirm the selected NVMe capacity and variant link.")
    elif data.get("itemGroupHref") or data.get("itemGroupType") or listing.variation_group:
        return OfferCheck("review", "The price for the exact variant still needs checking.")
    price = _gbp_amount(data.get("price") or {})
    if price != listing.price or data["title"].strip().casefold() != listing.title.strip().casefold():
        return OfferCheck("review", "eBay returned a different title or price. Check again after the next scan.")
    if not any(row.get("estimatedAvailabilityStatus") in ("IN_STOCK", "LIMITED_STOCK")
               and "SHIP_TO_HOME" in (row.get("deliveryOptions") or []) for row in availability):
        if any(row.get("estimatedAvailabilityStatus") in ("IN_STOCK", "LIMITED_STOCK")
               and "SELLER_ARRANGED_LOCAL_PICKUP" in (row.get("deliveryOptions") or []) for row in availability):
            city = (data.get("itemLocation") or {}).get("city")
            place = f" in {city}" if city else ""
            return OfferCheck("review", f"Collection only{place}. Check handover details and total on eBay.")
        return OfferCheck("review", "eBay did not confirm availability for delivery. Check collection terms if offered.")
    postage = [
        cost for shipping in (data.get("shippingOptions") or [])
        if (shipping.get("shipToLocationUsedForEstimate") or {}).get("country") == "GB"
        and (not postcode or "".join(
            (shipping["shipToLocationUsedForEstimate"].get("postalCode") or "").upper().split()
        ) == "".join(postcode.upper().split()))
        and shipping.get("quantityUsedForEstimate", 1) == 1
        and (cost := _gbp_amount(shipping.get("shippingCost") or {})) is not None
    ]
    if not postage:
        return OfferCheck("review", "eBay did not return a confirmed UK delivery charge.")
    if any(data.get(field) for field in ("importCharges", "deliveryFees")):
        return OfferCheck("review", "Check additional purchase fees on eBay.")
    return OfferCheck("verified", "Item price and UK delivery checked on eBay.", price, min(postage))


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
            supports_offer_verification=True,
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

    def knowledge(self) -> ConnectorKnowledge:
        return ConnectorKnowledge(
            display_name="eBay",
            description="Official eBay Browse API client for UK listings - "
                        "the project's primary automated source, covering "
                        "fixed-price, auction, and Best-Offer listings with "
                        "structured brand/model enrichment.",
            implementation_type="Official REST API client (eBay Browse API, "
                                "OAuth client-credentials)",
            maturity="production",
            supported_listing_types=(
                "Fixed price", "Auction", "Auction with Buy It Now", "Best Offer",
            ),
            supported_marketplaces=("eBay UK (EBAY_GB)",),
            supported_search_features=(
                "Free-text keyword search", "Max price filter",
                "GB item location + GBP currency filter (always applied)",
            ),
            known_limitations=(
                "Only the first 50 results per search term per cycle are "
                "fetched - no pagination beyond the Browse API's default "
                "page size.",
                "Seller identity/reputation exists in the raw API payload "
                "but isn't mapped to a Listing field yet "
                "(provides_seller_identity=False).",
                "No location-radius filter - only a hard GB-only item-"
                "location filter, unlike Gumtree/Facebook's postcode+radius.",
            ),
            planned_work=(
                "Map seller identity/reputation fields once a real use for "
                "them exists.",
            ),
            intentionally_unsupported=(
                "Listing-page HTML enrichment for watch-count/view-count "
                "signals the official API doesn't expose - parked "
                "2026-07-08, disabled by default and opt-in only if ever "
                "built (compliance_mode='scraping', account_risk medium-"
                "high). See docs/strategy/roadmap.md, 'Future ideas "
                "(deliberately unscheduled)'.",
            ),
            investigation_items=(),
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
        return self.search_page(term, item).listings

    def search_identity(self, term: str, item: ItemConfig) -> tuple:
        return term, item.max_price

    def search_page(
        self, term: str, item: ItemConfig, *, limit: int = 50, offset: int = 0, sort: str | None = "watch",
    ) -> SearchPage:
        filters = ["itemLocationCountry:GB", "priceCurrency:GBP"]
        if item.max_price:
            filters.append(f"price:[..{item.max_price:g}]")
        params = {"q": term, "limit": str(limit), "filter": ",".join(filters)}
        if offset:
            params["offset"] = str(offset)
        if sort == "watch":
            # The discovery replay found recent sliding mitre saws buried
            # below the Best Match page. Keep model aliases on Best Match.
            sort = "newlyListed" if term.strip().casefold() == "sliding mitre saw" else None
        if sort:
            params["sort"] = sort

        def _do_request():
            resp = requests.get(
                _ENDPOINTS[self.cfg.sources.ebay.env]["search"],
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                },
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            return resp

        resp = rate_limit.request_with_backoff(self._limiter, _do_request, self.name)
        payload = resp.json()
        summaries = payload.get("itemSummaries") or []
        listings = []
        for summary in summaries:
            priced = _price_value(summary)
            external_id = summary.get("itemId")
            if priced is None or not external_id:
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
                    external_id=str(external_id),
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
                    category=" / ".join(
                        category["categoryName"] for category in (summary.get("categories") or [])
                        if category.get("categoryName")
                    ),
                    variation_group=bool(summary.get("itemGroupHref") or summary.get("itemGroupType")),
                    variation_group_id=_group_id(summary.get("itemGroupHref") or ""),
                )
            )
        total = payload.get("total")
        total = total if isinstance(total, int) and total >= 0 else None
        has_more = True if payload.get("next") else (offset + len(summaries) < total if total is not None else None)
        return SearchPage(listings, len(summaries), total, has_more)

    def _item_headers(self, delivery_context: bool = False) -> dict:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        }
        if delivery_context and self.cfg.postcode:
            location = quote(f"country=GB,zip={self.cfg.postcode}", safe="")
            headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation={location}"
        return headers

    def _fetch_item(self, external_id: str, *, delivery_context: bool = False) -> dict | None:
        """Shared Browse API lookup for one concrete item."""
        resp = requests.get(
            f"{_ENDPOINTS[self.cfg.sources.ebay.env]['item']}/{quote(external_id, safe='')}",
            headers=self._item_headers(delivery_context),
            timeout=8 if delivery_context else 20,
        )
        if resp.status_code != 200:
            return None
        return resp.json()

    def verify_offer(self, listing: Listing) -> OfferCheck:
        try:
            data = self._fetch_item(listing.external_id, delivery_context=True)
        except requests.RequestException:
            data = None
        if data is None:
            return OfferCheck("review", "eBay offer verification is temporarily unavailable.")
        return _verify_fixed_price(listing, data, self.cfg.postcode)

    def resolve_nvme_variants(self, group: Listing, capacity_gb: float) -> list[tuple[Listing, OfferCheck]]:
        if not group.variation_group_id:
            return []
        try:
            endpoint = _ENDPOINTS[self.cfg.sources.ebay.env]["item"]
            response = requests.get(
                f"{endpoint}/get_items_by_item_group",
                params={"item_group_id": group.variation_group_id},
                headers=self._item_headers(delivery_context=True),
                timeout=8,
            )
            response.raise_for_status()
            items = response.json().get("items") or []
        except requests.RequestException:
            return []
        result = []
        for data in items:
            price = _gbp_amount(data.get("price") or {})
            if price is None or _nvme_variant_capacity(data) != capacity_gb or not data.get("title"):
                continue
            variant = replace(
                group, external_id=data["itemId"], title=data["title"], price=price, url=data["itemWebUrl"],
                buying_options=list(data.get("buyingOptions") or []), end_time=data.get("itemEndDate"),
                condition=data.get("condition") or "", description="", variation_group=False,
                variant_capacity_gb=capacity_gb,
            )
            result.append((variant, _verify_fixed_price(variant, data, self.cfg.postcode)))
        return result

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
