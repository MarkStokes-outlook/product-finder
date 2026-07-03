"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    """A single marketplace listing, as fetched from a source."""

    source: str
    external_id: str
    title: str
    price: float
    url: str
    currency: str = "GBP"
    location: str = ""
    description: str = ""
    condition: str = ""
    # Auction awareness: buying_options e.g. ["FIXED_PRICE"], ["AUCTION"],
    # ["AUCTION", "FIXED_PRICE"] (has a Buy It Now). When "AUCTION" is
    # present, `price` may just be the current bid, not a committed price —
    # see scoring.is_live_auction().
    buying_options: list[str] = field(default_factory=list)
    bid_count: int | None = None
    end_time: str | None = None  # ISO 8601, auction/listing end — if known

    @property
    def text(self) -> str:
        """Combined text used for grading and warning-flag detection."""
        return " ".join(p for p in (self.title, self.condition, self.description) if p)


@dataclass
class Evaluation:
    """The result of scoring a listing against a wanted item."""

    grade: str
    flags: list[str]
    margin_abs: float
    margin_pct: float
    under_target: bool
    deal_score: float


@dataclass
class ManualLink:
    """A manual-assisted search link for sources we do not automate."""

    source: str
    label: str
    url: str


@dataclass
class MatchAlert:
    """A newly matched listing, ready for alert channels."""

    project_name: str
    item_name: str
    listing: Listing
    evaluation: Evaluation
    normal_price: float | None = None
    target_deal_price: float | None = None
    extras: dict = field(default_factory=dict)
