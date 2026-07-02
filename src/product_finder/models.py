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
