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

Compliance is a hard constraint: every connector declares the legitimate
basis it operates on (`SourceCapabilities.compliance`), and an integration
that would require scraping, login bypass, or bot-protection evasion does
not get built.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink


@dataclass(frozen=True)
class SourceCapabilities:
    """What a connector can legitimately do, declared not inferred.

    The engine and UI reason over these instead of special-casing
    marketplaces (e.g. the runner offers detail-enrichment to any connector
    with supports_enrichment, not to "eBay"). Add fields as the engine
    grows real uses for them — this is deliberately not a wishlist."""

    #: True = search() does the work; False = manual_links() only.
    automated: bool
    #: The legitimate basis this connector operates on — shown on the
    #: Sources page, e.g. "official eBay Browse API" or "manual links only
    #: (terms prohibit automated access)".
    compliance: str
    #: get_item_details() can fetch structured per-listing detail.
    supports_enrichment: bool = False
    #: Listings carry an image URL (even best-effort).
    provides_images: bool = False
    #: Listings carry end/expiry semantics (auction end, listing expiry).
    provides_end_time: bool = False
    #: Structured product attributes (brand/MPN etc.) are available.
    provides_structured_attributes: bool = False
    #: Human-readable quirks/provenance notes for the Sources page.
    notes: str = ""


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
