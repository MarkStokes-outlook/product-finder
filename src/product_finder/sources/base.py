"""The contract every marketplace connector implements.

A connector is constructed once per run with the app config. It either
searches automatically (returning normalised Listings — everything downstream
is source-agnostic) or generates manual-assisted search links. Nothing
outside this package should care which — the rest of the engine reasons over
declared capabilities, never over which marketplace produced a row.

Two connector classes, both first-class (see docs/strategy/roadmap.md,
"Market coverage and marketplace connectors"):

- automated: official APIs, authorised feeds, or genuinely open RSS/Atom
  endpoints that permit programmatic search.
- manual-assisted: marketplaces whose terms don't permit automation; these
  only generate pre-filled search links for a human to follow.

Compliance is no longer an absolute build/don't-build gate — it is modelled
explicitly as connector risk (`SourceCapabilities.account_risk`,
`compliance_mode`, `is_scraping_based`, etc.), so a scraping or user-session
connector *can* exist, but never by accident and never hidden behind
`automated=True`. See `sources/__init__.py`'s scheduler-side risk gate for
how that risk is actually enforced (medium/high risk requires explicit
per-source opt-in; nothing risky is ever silently scheduled just because it
is "enabled").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink

#: account_risk values, low to high. Scheduling policy (sources/__init__.py)
#: treats anything above "low" as requiring explicit per-source opt-in.
ACCOUNT_RISK_LEVELS = ("none", "low", "medium", "high")

#: compliance_mode values — the *kind* of legitimate (or not-so-legitimate)
#: basis a connector operates on. Deliberately includes modes ("scraping",
#: "user_session") that this project doesn't build today, so the model
#: doesn't pretend those connectors can never exist — see Phase 7's options
#: paper (docs/strategy/) for when each mode might get used.
COMPLIANCE_MODES = (
    "official",  # an official, authorised API (eBay Browse API today)
    "indexed",  # a search-index/syndication feed (RSS/Atom, SearXNG-style)
    "manual",  # generates links only; a human does the searching/browsing
    "user_session",  # would require a logged-in personal browser session
    "scraping",  # would parse HTML/undocumented endpoints directly
    "licensed_provider",  # a third-party data provider under its own terms
)


@dataclass(frozen=True)
class SourceCapabilities:
    """What a connector can legitimately do, declared not inferred.

    The engine and UI reason over these instead of special-casing
    marketplaces (e.g. the runner offers detail-enrichment to any connector
    with supports_enrichment, not to "eBay"). Add fields as the engine
    grows real uses for them — this is deliberately not a wishlist.

    Risk is a first-class, mandatory-shaped declaration, not an afterthought:
    a connector cannot claim `is_scraping_based=True` while also claiming
    `account_risk` of "none"/"low", and cannot claim `requires_user_auth=True`
    while claiming `account_risk="none"` — see __post_init__. The intent is
    that risk can never be quietly hidden behind `automated=True` the way a
    binary automated/manual split would allow."""

    # --- mechanical dispatch (unchanged) ---
    #: True = search() does the work; False = manual_links() only.
    automated: bool
    #: The legitimate basis this connector operates on — shown on the
    #: Sources page, e.g. "official eBay Browse API" or "manual links only
    #: (terms prohibit automated access)".
    compliance: str

    # --- risk / compliance model ---
    #: none | low | medium | high — see ACCOUNT_RISK_LEVELS. Governs
    #: scheduling eligibility (sources/__init__.py's risk gate), not just
    #: display.
    account_risk: str = "none"
    #: official | indexed | manual | user_session | scraping |
    #: licensed_provider — see COMPLIANCE_MODES.
    compliance_mode: str = "manual"
    #: Can this connector ever run on a schedule with zero human involvement
    #: and no live browser/user session — the architectural claim the
    #: scheduler relies on. Distinct from `automated`, which just says
    #: whether search() does anything today.
    can_run_unattended: bool = False
    #: Needs a logged-in personal session/credentials (not an application/
    #: service API key) to operate.
    requires_user_auth: bool = False
    #: A human must do something each cycle (open a link, paste a URL,
    #: manually run a search) — distinct from requires_user_auth.
    requires_manual_input: bool = False
    is_official_api: bool = False
    #: Discovered via a search index/syndication feed (RSS/Atom, SearXNG)
    #: rather than the marketplace's own native search/API.
    is_indexed_search_based: bool = False
    is_scraping_based: bool = False
    #: Uses a third-party data provider (e.g. an Apify-style scraping
    #: service) rather than this project talking to the marketplace itself.
    is_third_party_provider: bool = False
    #: Free-text tag describing this connector's rate-limit posture, e.g.
    #: "official-api-standard", "third-party-feed-conservative", "n/a" for
    #: manual-assisted connectors that make no automated requests at all.
    rate_limit_class: str = "n/a"
    #: Free-text recommended scheduling cadence for the Sources page, e.g.
    #: "every watch cycle", "hourly", "manual only".
    recommended_schedule: str = "manual only"
    #: realtime | minutes | hours | daily | unknown — how fresh results are
    #: likely to be once fetched.
    freshness: str = "unknown"

    # --- declared fields available ---
    #: get_item_details() can fetch structured per-listing detail.
    supports_enrichment: bool = False
    #: Listings carry an image URL (even best-effort).
    provides_images: bool = False
    #: Listings carry end/expiry semantics (auction end, listing expiry).
    provides_end_time: bool = False
    #: Structured product attributes (brand/MPN etc.) are available.
    provides_structured_attributes: bool = False
    #: Per-listing auction observation history is available (see
    #: db.record_auction_snapshot / auction_watch.py).
    provides_auction_snapshot: bool = False
    #: Offer/Best-Offer detection is meaningful for this source's listings
    #: (see offers.detect_offer_support).
    provides_offers: bool = False
    #: Seller identity/reputation fields are available.
    provides_seller_identity: bool = False
    #: Listing location is available (even approximate).
    provides_location: bool = False

    #: Human-readable quirks/provenance notes for the Sources page.
    notes: str = ""

    def __post_init__(self) -> None:
        if self.account_risk not in ACCOUNT_RISK_LEVELS:
            raise ValueError(
                f"account_risk must be one of {ACCOUNT_RISK_LEVELS}, got {self.account_risk!r}"
            )
        if self.compliance_mode not in COMPLIANCE_MODES:
            raise ValueError(
                f"compliance_mode must be one of {COMPLIANCE_MODES}, got {self.compliance_mode!r}"
            )
        if self.is_scraping_based and self.account_risk in ("none", "low"):
            raise ValueError(
                "is_scraping_based=True cannot be declared with "
                f"account_risk={self.account_risk!r} — scraping is never "
                "risk-free; don't hide it behind a low risk label"
            )
        if self.requires_user_auth and self.account_risk == "none":
            raise ValueError(
                "requires_user_auth=True cannot be declared with "
                "account_risk='none' — using a personal session always "
                "carries some account risk"
            )


class Source(ABC):
    #: Unique key. Used for listing dedup, per-item source filters, and config.
    name: str

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    @abstractmethod
    def capabilities(self) -> SourceCapabilities:
        """Declared capabilities — every connector must state what it is."""

    def is_automated(self) -> bool:
        """True if search() does the work; False for manual-assisted sources."""
        return self.capabilities().automated

    def search(self, term: str, item: ItemConfig) -> list[Listing]:
        """Fetch listings for one search term. Automated sources override this.
        May raise on network/auth errors — the runner catches per-term."""
        return []

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        """Pre-filtered search links. Manual-assisted sources override this."""
        return []

    def get_item_details(self, external_id: str) -> dict | None:
        """Structured detail for one listing (brand/model etc.), for
        connectors with supports_enrichment. Default: nothing available."""
        return None
