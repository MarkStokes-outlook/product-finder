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
    # Distinct from `price`'s BIN-preferring fallback (_price_value()) — see
    # the bug this fixed (2026-07-08): a BIN+AUCTION listing was displaying
    # its Buy It Now price labelled as "current bid", because nothing
    # captured currentBidPrice separately until the (much less frequent)
    # auction-close poller happened to reach it. Real captures confirm
    # currentBidPrice is present even at zero bids (equal to
    # minimumPriceToBid there) — never absent — so this is populated
    # whenever "AUCTION" is in buying_options, with no separate
    # "starting price" fallback needed. None when not an auction.
    current_bid_price: float | None = None
    # The Buy It Now price specifically, when "FIXED_PRICE" is also in
    # buying_options — kept distinct from `price` so it can be displayed
    # alongside the current bid rather than instead of it.
    buy_it_now_price: float | None = None
    # Best single product image, if the source provides one. eBay's Browse
    # API always does (thumbnailImages[0] is the large render, ~1200-1600px;
    # `image` is the 225px one); RSS feeds sometimes carry media:thumbnail.
    image_url: str | None = None

    @property
    def text(self) -> str:
        """Combined text used for grading and warning-flag detection."""
        return " ".join(p for p in (self.title, self.condition, self.description) if p)


@dataclass
class AuctionSnapshot:
    """A single-item price check, for tracking an auction toward its close
    (see auction_watch.py) and, since every poll is now recorded (not just
    the closing one), for building a per-listing observation history (see
    db.record_auction_snapshot). `ended` is derived from the source's stock/
    availability status, not just the clock — eBay's Browse API keeps
    reporting the last bid price for a little while after itemEndDate
    passes, so waiting for the availability flip (rather than just the
    timestamp) avoids capturing a split-second-too-early read.

    `price` keeps its original meaning and fallback behaviour (BIN price if
    present, else current bid) — the "what would closing right now cost"
    signal already relied on by auction_watch.py's close-price capture.
    `current_bid` is a distinct, unambiguous field added for snapshot
    history: always `currentBidPrice` specifically, never falling back to
    the BIN price. This distinction matters in practice — real captures
    confirm eBay returns `price` (BIN) and `currentBidPrice` (current bid)
    simultaneously when a listing has both AUCTION and FIXED_PRICE buying
    options, and they can differ a lot (see
    tests/fixtures/ebay/getitem_auction_with_bin.json: BIN 229.50 vs current
    bid 156.70) — collapsing them into one field would hide exactly the
    "bid climbing toward BIN" signal trajectory scoring needs.
    `watch_count`/`view_count` are always None today: confirmed absent from
    every real Browse API response captured for this project (checked the
    full key set, not just the fields expected) — recorded as unknown with
    provenance rather than guessed, per an explicit project rule to never
    fake unsupported source data."""

    price: float
    currency: str = "GBP"
    bid_count: int | None = None
    ended: bool = False
    current_bid: float | None = None
    buy_it_now_price: float | None = None
    shipping_price: float | None = None
    watch_count: int | None = None
    view_count: int | None = None
    raw: dict | None = None


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
