"""The contract every marketplace source implements.

A source is constructed once per run with the app config. It either searches
automatically (returning normalised Listings — everything downstream is
source-agnostic) or generates manual-assisted search links. Nothing outside
this package should care which.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import AppConfig, ItemConfig
from ..models import Listing, ManualLink


class Source(ABC):
    #: Unique key. Used for listing dedup, per-item source filters, and config.
    name: str

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    @abstractmethod
    def is_automated(self) -> bool:
        """True if search() does the work; False for manual-assisted sources."""

    def search(self, term: str, item: ItemConfig) -> list[Listing]:
        """Fetch listings for one search term. Automated sources override this.
        May raise on network/auth errors — the runner catches per-term."""
        return []

    def manual_links(self, item: ItemConfig) -> list[ManualLink]:
        """Pre-filtered search links. Manual-assisted sources override this."""
        return []
