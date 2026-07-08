"""Marketplace Outbound Gateway — the single place the app turns a stored
listing into an outbound navigation. See ARCHITECTURE.md ("Marketplace
outbound gateway") and docs/adr/0002-affiliate-link-redirect-and-tracking.md.

Nothing outside this module (and the GET /out/<listing_id> route in
web/app.py that drives it) should ever construct a marketplace URL for a
template to render. Every outbound click flows:

    Listing -> MarketplaceOutboundService -> MarketplaceAdapter -> redirect URL -> Marketplace

MarketplaceAdapter is the extension point: each marketplace decides, on its
own, whether affiliate parameters are supported, how its destination URL is
built, and how its own failures are handled. The core application never
inspects *how* an adapter does this — it only ever calls resolve() and reads
the result. Today every real adapter is one of the two generic classes below
(no marketplace here needs bespoke logic yet); a future marketplace whose
affiliate programme needs something more exotic than query-param injection
(a wrapping/cloaked redirect URL, a signed link, etc.) gets its own
MarketplaceAdapter subclass without touching MarketplaceOutboundService or
the redirect route at all.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import AppConfig

log = logging.getLogger(__name__)

#: Allowed schemes for an outbound redirect destination — defence against
#: open-redirect/scheme-smuggling (javascript:, data:, etc.). Every input to
#: this module is server-controlled (a stored listings.url plus server-side
#: affiliate config, never a request parameter), but the redirect route
#: checks every resolved destination against this regardless, so a bad data
#: row or a broken adapter can never produce an unsafe redirect.
_SAFE_SCHEMES = ("http", "https")


def is_safe_redirect_url(url: str) -> bool:
    """True if `url` is safe to issue as a 302 Location header: an absolute
    http(s) URL with a network location. Rejects everything else (empty
    strings, relative paths, javascript:/data: schemes) — the redirect
    endpoint refuses to send a browser anywhere that fails this check,
    recording a failed click instead."""
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme in _SAFE_SCHEMES and bool(parts.netloc)


@dataclass(frozen=True)
class OutboundResolution:
    """What a MarketplaceAdapter (or the service's own passthrough) decided
    for one listing click. `affiliate_applied` is recorded on the click
    event (see db.record_listing_click) — analytics-visible, but never the
    parameters themselves."""

    url: str
    affiliate_applied: bool = False


class MarketplaceAdapter(ABC):
    """One marketplace's outbound-URL policy — deliberately mirrors
    sources.base.Source's per-connector contract style rather than
    inventing a new pattern.

    An adapter owns, entirely on its own:
    - whether affiliate parameters are supported (resolve() either injects
      them or returns the URL unchanged)
    - how the destination URL is constructed
    - how its own failures are handled — resolve() should not raise for a
      normal listing URL; catch what it can and fall back to returning the
      URL unchanged rather than letting a broken adapter block navigation
      (MarketplaceOutboundService also catches anything that escapes, as a
      second line of defence, but a well-behaved adapter shouldn't rely on
      that)."""

    #: Matches Listing.source / Source.name (sources/base.py) — the key
    #: MarketplaceOutboundService dispatches on.
    name: str

    @abstractmethod
    def resolve(self, listing_url: str) -> OutboundResolution:
        """Return the destination for one listing's stored, unmodified URL.
        `listings.url` itself is never touched by this feature — this is a
        read, not a rewrite."""


class PassthroughAdapter(MarketplaceAdapter):
    """The default for any source with no affiliate programme configured —
    redirects to the original URL unchanged. Tracking and the redirect hop
    still apply uniformly (see ADR-0002): this is not a bypass of the
    gateway, just an adapter with nothing to add."""

    def __init__(self, name: str):
        self.name = name

    def resolve(self, listing_url: str) -> OutboundResolution:
        return OutboundResolution(url=listing_url, affiliate_applied=False)


class QueryParamAffiliateAdapter(MarketplaceAdapter):
    """Config-driven affiliate parameter injection — covers every affiliate
    programme that only needs extra query-string parameters added to the
    existing listing URL (eBay Partner Network and most others work this
    way). A marketplace whose programme needs something more exotic gets
    its own MarketplaceAdapter subclass instead; this class is the common
    case, not the only one the interface allows."""

    def __init__(self, name: str, params: dict[str, str]):
        self.name = name
        self._params = dict(params)

    def resolve(self, listing_url: str) -> OutboundResolution:
        if not self._params:
            return OutboundResolution(url=listing_url, affiliate_applied=False)
        try:
            parts = urlsplit(listing_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query.update(self._params)  # ours always wins over any same-named param already present
            new_url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
            return OutboundResolution(url=new_url, affiliate_applied=True)
        except Exception:
            log.warning(
                "Affiliate param injection failed for source %r; passing through", self.name,
                exc_info=True,
            )
            return OutboundResolution(url=listing_url, affiliate_applied=False)


class MarketplaceOutboundService:
    """The single entry point for turning a stored listing into an outbound
    redirect destination — everything downstream of a listing_id flows
    through here (see GET /out/<listing_id> in web/app.py). Nothing outside
    this module knows or cares which marketplaces have affiliate
    programmes, what their parameters are, or how they're injected.

    Built once per effective config (affiliate config is server-side only —
    see config.OutboundConfig — and, like source enable/disable, may be
    overlaid by the DB per request, so this is rebuilt per-request the same
    way _effective_cfg() is, not cached at app start)."""

    def __init__(self, cfg: AppConfig):
        self._adapters: dict[str, MarketplaceAdapter] = {}
        for name in cfg.sources.all_names():
            params = cfg.outbound.affiliate_params.get(name)
            self._adapters[name] = (
                QueryParamAffiliateAdapter(name, params) if params else PassthroughAdapter(name)
            )

    def resolve(self, source: str, listing_url: str) -> OutboundResolution:
        """Resolve one listing's outbound destination. Never raises: an
        unknown source (stale data, or a source since removed from config)
        and an adapter that misbehaves both fail safe to the original URL
        unchanged, rather than blocking the redirect — see ADR-0002's "fail
        safely" requirement. The redirect route still validates the result
        with is_safe_redirect_url() before ever issuing it."""
        adapter = self._adapters.get(source)
        if adapter is None:
            log.info("No marketplace adapter registered for source %r; passing through", source)
            return OutboundResolution(url=listing_url, affiliate_applied=False)
        try:
            return adapter.resolve(listing_url)
        except Exception:
            log.warning("Marketplace adapter %r raised; passing through", source, exc_info=True)
            return OutboundResolution(url=listing_url, affiliate_applied=False)
