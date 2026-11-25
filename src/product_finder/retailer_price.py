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

import ipaddress
import json
import logging
import re
import socket
from math import isfinite
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlsplit

import urllib3

import requests

from . import db
from .config import SearxngConfig
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

# Deterministic ranking boost for domains known to be real UK retailers, as
# opposed to review sites/marketplaces/aggregators that happen to mention a
# price. Small and meant to grow over time — same style as
# catalogue.BRAND_ALIASES. Also the deliberately narrow network allowlist
# for this pilot. Adding a retailer requires review; no wildcard subdomains.
KNOWN_UK_RETAILER_DOMAINS = {
    "screwfix.com", "toolstation.com", "axminstertools.com", "diy.com",
    "wickes.co.uk", "homebase.co.uk", "amazon.co.uk", "argos.co.uk",
    "currys.co.uk", "very.co.uk", "johnlewis.com", "ryobitools.co.uk",
    "machinemart.co.uk",
}

# Respect blocks rather than impersonating a browser or following redirects.
_HEADERS = {"User-Agent": "ProductFinder/1.0 (retailer price discovery)"}
DISCOVERY_LIMIT = 3
REFRESH_LIMIT = 8
MAX_RESULTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
SOURCE_NAME = "retailer-discovery"
_search_limiter = RateLimiter(2, 60)
_retailer_limiter = RateLimiter(2, 60)


def _approved_host(url):
    """Exact allowlist, HTTPS and default port only; no credentials."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.netloc.lower() not in (host, host + ":443")):
            return None
        if host.removeprefix("www.") not in KNOWN_UK_RETAILER_DOMAINS:
            return None
        return host
    except (TypeError, ValueError):
        return None


def _public_address(host):
    """Reject mixed public/private answers; connect to the validated IP."""
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(not address.is_global or address.is_multicast
                            or address.is_reserved for address in addresses):
        raise ValueError("Non-public retailer destination")
    return str(addresses[0])


def _retailer_get(url, timeout):
    host = _approved_host(url)
    if not host:
        raise ValueError("Retailer destination not approved")
    address = _public_address(host)
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    # Connect to the resolved IP, retaining original TLS SNI and certificate
    # verification. No proxy, second DNS lookup or redirect to arbitrary hosts.
    pool = urllib3.HTTPSConnectionPool(
        address, port=443, server_hostname=host, assert_hostname=host,
        cert_reqs="CERT_REQUIRED", ca_certs=requests.certs.where(),
        timeout=urllib3.Timeout(total=min(max(timeout, 1), 15)), retries=False,
    )
    response = None
    try:
        _retailer_limiter.wait()
        response = pool.urlopen("GET", target, headers={**_HEADERS, "Host": host},
                                redirect=False, preload_content=False)
        if response.status == 429:
            _retailer_limiter.record_rate_limited()
        elif response.status == 200:
            _retailer_limiter.record_success()
        if response.status != 200:
            return response.status, ""
        body = response.read(MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("Retailer response exceeds size limit")
        return response.status, body.decode("utf-8", errors="replace")
    finally:
        if response is not None:
            response.close()
        pool.close()


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


# New-retail observations must not silently absorb refurbished/used prices.
_NON_NEW = re.compile(r"\b(?:refurb(?:ished)?|reconditioned|pre[- ]?owned|used|second[- ]?hand|open[- ]?box)\b", re.I)


def _non_new(value):
    text = str(value or "")
    return bool(_NON_NEW.search(text) or text.lower().endswith(("usedcondition", "refurbishedcondition", "damagedcondition")))


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
            if _non_new(entry.get("name")) or _non_new(entry.get("itemCondition")) or _non_new(offers.get("itemCondition")):
                raise ValueError("Non-new product condition")
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
        if _non_new(urlsplit(url).path):
            return None
        status, html = _retailer_get(url, timeout)
    except (urllib3.exceptions.HTTPError, OSError, ValueError) as exc:
        log.warning("Retailer page fetch failed for %s: %s", url, exc)
        return None
    if status != 200:
        log.warning("Retailer page fetch for %s returned HTTP %s", url, status)
        return None
    try:
        result = _price_from_json_ld(html) or _price_from_microdata(html)
    except ValueError:
        return None
    if result is None:
        return None
    price, currency = result
    if currency.upper() != "GBP" or not isfinite(price) or price <= 0:
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


def search_candidates(manufacturer: str, model: str, cfg: SearxngConfig) -> list[dict] | None:
    """None means upstream failure (retry later); [] means a completed search.

    Candidates are review evidence, never verified product identity or a deal.
    """
    if not cfg.enabled:
        return []
    query = f"{manufacturer} {model}".strip()
    if not query or not model:
        return []
    try:
        _search_limiter.wait()
        resp = requests.get(
            f"{cfg.base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "categories": "general"},
            headers=_HEADERS, timeout=min(max(cfg.timeout, 1), 15),
            allow_redirects=False,
        )
        if resp.status_code == 429:
            _search_limiter.record_rate_limited()
        resp.raise_for_status()
        if resp.status_code != 200:
            raise ValueError("Unexpected search response")
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Invalid search response")
        results = data["results"]
        if not results and data.get("unresponsive_engines"):
            raise ValueError("Search engines unavailable")
        _search_limiter.record_success()
    except (requests.RequestException, ValueError) as exc:
        log.warning("SearXNG search skipped (unavailable): %s", exc)
        return None

    candidates = []
    seen = set()
    for result in results[:min(max(cfg.max_results, 0), MAX_RESULTS)]:
        if not isinstance(result, dict):
            continue
        url = result.get("url")
        title = result.get("title")
        if (not isinstance(url, str) or not isinstance(title, str)
                or url in seen or not _approved_host(url) or _non_new(title) or _non_new(urlsplit(url).path)):
            continue
        # Exact model token in the result title; still requires human review.
        if not re.search(r"(?<![a-z0-9])" + re.escape(model.lower()) + r"(?![a-z0-9])", title.lower()):
            continue
        seen.add(url)
        priced = fetch_price(url, cfg.timeout)
        if priced is None:
            continue
        candidates.append({
            "url": url, "domain": _domain(url),
            "price": priced["price"], "currency": priced["currency"],
            "confidence": _confidence(manufacturer, model, url, title),
        })
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def _discovery_backoff(conn):
    recent = conn.execute(
        "SELECT ok, run_at FROM source_runs WHERE source = ? ORDER BY id DESC LIMIT 4",
        (SOURCE_NAME,),
    ).fetchall()
    failures = 0
    for row in recent:
        if row["ok"]:
            break
        failures += 1
    if not failures:
        return False
    last = datetime.fromisoformat(recent[0]["run_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delay = timedelta(hours=min(2 ** (failures - 1), 6))
    return datetime.now(timezone.utc) - last < delay


def run_discovery_and_refresh(conn, cfg) -> None:
    """Bounded per-cycle discovery and refresh, with durable source backoff."""
    searxng_cfg = cfg.searxng
    if not searxng_cfg.enabled:
        return

    if not _discovery_backoff(conn):
        attempted = 0
        failed = False
        for row in db.list_products_needing_price_search(conn, limit=DISCOVERY_LIMIT):
            attempted += 1
            candidates = search_candidates(row["manufacturer"], row["model"] or "", searxng_cfg)
            if candidates is None:
                failed = True
                break  # don't exhaust the remaining budget against a failing source
            db.record_price_candidates(conn, row["id"], candidates)
        if attempted:
            db.record_source_run(conn, SOURCE_NAME, searches=attempted, errors=int(failed),
                                 last_error="Retailer search unavailable; retry deferred" if failed else None)

    for row in db.list_products_due_for_price_refresh(conn, searxng_cfg.refresh_interval_hours,
                                                       limit=REFRESH_LIMIT):
        result = fetch_price(row["canonical_price_url"], searxng_cfg.timeout)
        db.record_price_refresh(conn, row["id"], result, domain=_domain(row["canonical_price_url"]))
