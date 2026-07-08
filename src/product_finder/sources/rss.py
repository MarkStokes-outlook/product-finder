"""Generic RSS/Atom feed source — automated, defined entirely in config.

Works with any site offering per-search feeds, e.g.:
  - HotUKDeals:  https://www.hotukdeals.com/rss/search?q={term}
  - Reddit subs: https://www.reddit.com/r/hardwareswapuk/search.rss?q={term}&restrict_sr=1

Prices are extracted from entry titles/descriptions (first £ amount found);
entries without a detectable price are skipped, since scoring needs one.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from .. import rate_limit
from ..config import ExtraSourceConfig, ItemConfig
from ..models import Listing
from .base import ConnectorKnowledge, Source, SourceCapabilities

log = logging.getLogger(__name__)

USER_AGENT = "product-finder/0.1 (personal local deal tracker)"

_PRICE_RE = re.compile(r"£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
_TAG_RE = re.compile(r"<[^>]+>")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"


def _entry_image(node) -> str:
    """Best-effort image from a feed entry: media:thumbnail (Reddit link
    posts, many RSS 2.0 feeds) or an image-typed enclosure. Empty string
    when the entry simply has none — most text posts won't."""
    thumb = node.find(f"{_MEDIA_NS}thumbnail")
    if thumb is not None and thumb.get("url"):
        return thumb.get("url").strip()
    enclosure = node.find("enclosure")
    if enclosure is not None and (enclosure.get("type") or "").startswith("image/"):
        return (enclosure.get("url") or "").strip()
    return ""


def format_url(template: str, term: str, item: ItemConfig, cfg) -> str:
    return template.format(
        term=quote_plus(term),
        max_price=f"{item.max_price:g}" if item.max_price else "",
        postcode=quote_plus(cfg.postcode or ""),
        radius=cfg.radius_miles,
    )


def extract_price(text: str) -> float | None:
    match = _PRICE_RE.search(text or "")
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


def _parse_date(text: str | None) -> datetime | None:
    """Parse RSS 2.0 (RFC 822 pubDate) or Atom (ISO 8601) timestamps."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(xml_text: str) -> list[dict]:
    """Parse RSS 2.0 or Atom into [{id, title, url, description, published}]."""
    root = ET.fromstring(xml_text)
    entries = []
    # RSS 2.0: <rss><channel><item>
    for node in root.iter("item"):
        entries.append(
            {
                "id": (node.findtext("guid") or node.findtext("link") or "").strip(),
                "title": (node.findtext("title") or "").strip(),
                "url": (node.findtext("link") or "").strip(),
                "description": _strip_html(node.findtext("description") or ""),
                "published": _parse_date(node.findtext("pubDate")),
                "image_url": _entry_image(node),
            }
        )
    # Atom: <feed><entry>
    for node in root.iter(f"{_ATOM_NS}entry"):
        link = node.find(f"{_ATOM_NS}link")
        published_text = node.findtext(f"{_ATOM_NS}published") or node.findtext(
            f"{_ATOM_NS}updated"
        )
        entries.append(
            {
                "id": (node.findtext(f"{_ATOM_NS}id") or "").strip(),
                "title": (node.findtext(f"{_ATOM_NS}title") or "").strip(),
                "url": (link.get("href", "") if link is not None else "").strip(),
                "description": _strip_html(
                    node.findtext(f"{_ATOM_NS}summary")
                    or node.findtext(f"{_ATOM_NS}content")
                    or ""
                ),
                "published": _parse_date(published_text),
                "image_url": _entry_image(node),
            }
        )
    return entries


# Feed hosts (Reddit especially) rate-limit unauthenticated clients hard.
# Starting floor matches the old fixed gap this replaces; the ceiling gives
# the adaptive backoff (rate_limit.py) room to grow into on a bad run.
_MIN_DELAY = 3.0
_MAX_DELAY = 120.0


class RssSource(Source):
    def __init__(self, cfg, spec: ExtraSourceConfig):
        super().__init__(cfg)
        self.name = spec.name
        self.spec = spec
        # Per-instance, not shared across feeds — each configured RSS
        # source (e.g. a Reddit search vs. a HotUKDeals search) learns its
        # own pace rather than fighting over one global clock.
        self._limiter = rate_limit.RateLimiter(_MIN_DELAY, _MAX_DELAY)

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            automated=True,
            compliance="open RSS/Atom feed intended for syndication",
            account_risk="none",
            compliance_mode="indexed",
            can_run_unattended=True,
            requires_user_auth=False,
            requires_manual_input=False,
            is_indexed_search_based=True,
            rate_limit_class="third-party-feed-conservative",
            recommended_schedule="every watch cycle",
            freshness="minutes",
            provides_images=True,
            notes="Prices parsed from entry text (entries without a £ amount "
                  "are skipped); images best-effort from media:thumbnail or "
                  "image enclosures.",
        )

    def knowledge(self) -> ConnectorKnowledge:
        label = self.spec.label or self.spec.name
        return ConnectorKnowledge(
            display_name=label,
            description=f"Generic RSS/Atom feed connector, configured for "
                        f"{label} ({self.spec.url}). Parses entries directly "
                        f"from the feed - no marketplace-specific API, no "
                        f"structured listing fields beyond what a price-"
                        f"extraction regex can find in the entry text.",
            implementation_type="Generic RSS/Atom feed parser (config-driven, "
                                "no per-site code)",
            # The parsing mechanism is stable and shared across every
            # configured feed, but any *specific* feed's real-world
            # reliability/compliance is unverified per-instance - "beta"
            # reflects that, not a claim the code itself is unfinished.
            maturity="beta",
            supported_listing_types=("Fixed price",),
            supported_marketplaces=(label,),
            supported_search_features=(
                "Feed-defined query templating ({term}/{max_price}/{postcode}"
                "/{radius} substituted into the configured URL - see "
                "format_url()); actual filtering behaviour depends entirely "
                "on what the target feed itself supports.",
            ),
            known_limitations=(
                "No structured price/condition/location fields - price is "
                "regex-extracted from entry title/description text and "
                "entries without a detectable £ amount are silently skipped.",
                "No auction/offer/end-time semantics - RSS entries are "
                "treated as plain fixed-price listings.",
                "Feed reliability (uptime, rate limits, whether it stays "
                "genuinely open for syndication) is entirely dependent on "
                "the configured site and not verified by this connector.",
            ),
            investigation_items=(
                "SearXNG-indexed search as a more structured alternative to "
                "ad-hoc per-site RSS feeds for sites without one - see "
                "docs/strategy/facebook-gumtree-connector-options.md.",
            ),
        )

    def search(self, term: str, item: ItemConfig) -> list[Listing]:
        url = format_url(self.spec.url, term, item, self.cfg)

        def _do_request():
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            return resp

        resp = rate_limit.request_with_backoff(self._limiter, _do_request, self.name)
        cutoff = None
        if self.spec.max_age_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.spec.max_age_days)
        listings = []
        for entry in parse_feed(resp.text):
            if not entry["url"] or not entry["title"]:
                continue
            if cutoff and entry["published"] and entry["published"] < cutoff:
                log.debug("%s: %r too old (%s), skipped", self.name, entry["title"][:60],
                          entry["published"].date())
                continue
            price = extract_price(f"{entry['title']} {entry['description']}")
            if price is None:
                log.debug("%s: no price in %r, skipped", self.name, entry["title"][:60])
                continue
            listings.append(
                Listing(
                    source=self.name,
                    external_id=entry["id"] or entry["url"],
                    title=entry["title"],
                    price=price,
                    url=entry["url"],
                    description=entry["description"][:500],
                    image_url=entry["image_url"] or None,
                )
            )
        return listings
