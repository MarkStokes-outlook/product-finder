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
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from ..config import ExtraSourceConfig, ItemConfig
from ..models import Listing
from .base import Source

log = logging.getLogger(__name__)

USER_AGENT = "product-finder/0.1 (personal local deal tracker)"

_PRICE_RE = re.compile(r"£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
_TAG_RE = re.compile(r"<[^>]+>")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


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
            }
        )
    return entries


# Feed hosts (Reddit especially) rate-limit unauthenticated clients hard;
# space out requests across all RSS sources in the process.
_MIN_REQUEST_GAP_SECONDS = 3.0
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = _MIN_REQUEST_GAP_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


class RssSource(Source):
    def __init__(self, cfg, spec: ExtraSourceConfig):
        super().__init__(cfg)
        self.name = spec.name
        self.spec = spec

    def is_automated(self) -> bool:
        return True

    def search(self, term: str, item: ItemConfig) -> list[Listing]:
        url = format_url(self.spec.url, term, item, self.cfg)
        _throttle()
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
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
                )
            )
        return listings
