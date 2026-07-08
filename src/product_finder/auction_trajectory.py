"""Auction trajectory labelling — explainable, threshold-based, not a
black-box model. Scores a *live* auction's progress toward a reference
price, separately from scoring.deal_score() (which already treats a live
bid as never a committed price — see scoring.is_live_auction()). This
module answers a different question: "given where this bid is now, and how
it's moving, is this auction still worth watching?"

Pure functions only, same style as price_trend.py/grading.py: no DB or
network access, no class state. All thresholds below are named constants,
deliberately provisional — same practice as price_trend.py's tuning
constants — and should be revisited once there's real auction-outcome data
(did "Potential deal" auctions actually close low?) to check them against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# --- Tuning constants (provisional — see module docstring) ---------------

#: Below this % headroom vs. reference price, with a bid velocity that's
#: accelerating, the auction is heating up faster than the discount is worth.
HOT_MAX_HEADROOM_PCT = 25.0
#: Below this % headroom, worth calling out a concrete "stays under £X" ceiling.
DEAL_MIN_HEADROOM_PCT = 25.0
#: At/above this % headroom, with plenty of time left, it's too early to
#: call this anything but "keep watching".
EARLY_WATCH_MIN_HEADROOM_PCT = 50.0
EARLY_WATCH_MIN_REMAINING = timedelta(hours=6)
#: The suggested "don't bid above this" ceiling, as a fraction of the
#: reference price — leaves margin rather than bidding right up to fair value.
BID_CEILING_FACTOR = 0.9
#: A bid rising faster than this multiple of its earlier pace counts as
#: "accelerating".
ACCELERATION_RATIO = 1.5
#: Need at least this many bid-bearing snapshots, spanning at least this
#: long, before velocity is meaningful — otherwise "accelerating" is
#: reported as unknown (None), never guessed.
MIN_SNAPSHOTS_FOR_VELOCITY = 3
MIN_SPAN_FOR_VELOCITY = timedelta(minutes=5)

LABEL_INSUFFICIENT_DATA = "Not enough data yet"
LABEL_EARLY_WATCH = "Early watch"
LABEL_POTENTIAL_DEAL = "Potential deal"
LABEL_LIKELY_BARGAIN = "Likely bargain if it stays under"
LABEL_GETTING_HOT = "Getting too hot"
LABEL_NO_LONGER_DEAL = "No longer a deal"


@dataclass
class AuctionTrajectory:
    """Result of evaluate() — always includes a plain-English explanation,
    never just a label, per this project's "explainable, no black box" rule."""

    label: str
    explanation: str
    headroom_pct: float | None  # None when no bids yet, or no reference price
    bid_ceiling: float | None  # suggested "don't bid above this"
    accelerating: bool | None  # None = not enough snapshot history to tell


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def bid_velocity_accelerating(snapshots) -> bool | None:
    """snapshots: oldest-first sequence of rows/mappings with `observed_at`
    (ISO string) and `current_bid_price`. Compares the most recent bid-price
    gap (£/hour) to the average of earlier gaps. Returns None — not False —
    when there isn't enough history to say either way."""
    points = [
        (_parse(s["observed_at"]), s["current_bid_price"])
        for s in snapshots
        if s["current_bid_price"] is not None
    ]
    if len(points) < MIN_SNAPSHOTS_FOR_VELOCITY:
        return None
    if points[-1][0] - points[0][0] < MIN_SPAN_FOR_VELOCITY:
        return None

    gaps = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        hours = (t1 - t0).total_seconds() / 3600
        if hours <= 0:
            continue
        gaps.append((p1 - p0) / hours)
    if len(gaps) < 2:
        return None

    *earlier, latest = gaps
    avg_earlier = sum(earlier) / len(earlier)
    if avg_earlier <= 0:
        return latest > 0
    return latest > avg_earlier * ACCELERATION_RATIO


def evaluate(
    *,
    current_bid: float | None,
    bid_count: int | None,
    remaining: timedelta | None,
    reference_price: float | None,
    reference_label: str = "typical used price",
    snapshots=(),
) -> AuctionTrajectory:
    """`reference_price` should be the best available fair-value estimate
    (prefer a product's typical_used_price; fall back to the item's blended
    normal_price) — the caller resolves this via scoring.effective_prices(),
    this module doesn't know about products/items at all. `reference_label`
    only affects wording, so the explanation is honest about which kind of
    number it's comparing against."""
    if reference_price is None or reference_price <= 0:
        return AuctionTrajectory(
            label=LABEL_INSUFFICIENT_DATA,
            explanation=f"No {reference_label} available for this item yet.",
            headroom_pct=None,
            bid_ceiling=None,
            accelerating=None,
        )

    bid_ceiling = round(reference_price * BID_CEILING_FACTOR, 2)
    has_bids = bool(bid_count) and current_bid is not None

    if not has_bids:
        return AuctionTrajectory(
            label=LABEL_EARLY_WATCH,
            explanation=f"No bids yet. {reference_label.capitalize()} is around £{reference_price:.0f}.",
            headroom_pct=None,
            bid_ceiling=bid_ceiling,
            accelerating=None,
        )

    headroom_pct = ((reference_price - current_bid) / reference_price) * 100
    accelerating = bid_velocity_accelerating(snapshots)

    if headroom_pct <= 0:
        label = LABEL_NO_LONGER_DEAL
        explanation = (
            f"Current bid £{current_bid:.2f} is at or above {reference_label} "
            f"(£{reference_price:.0f})."
        )
    elif headroom_pct <= HOT_MAX_HEADROOM_PCT and accelerating:
        label = LABEL_GETTING_HOT
        explanation = (
            f"Bid £{current_bid:.2f} is closing in on {reference_label} "
            f"(£{reference_price:.0f}) and rising faster than earlier in the auction."
        )
    elif headroom_pct <= DEAL_MIN_HEADROOM_PCT:
        label = LABEL_LIKELY_BARGAIN
        explanation = (
            f"Currently £{current_bid:.2f}, {headroom_pct:.0f}% below {reference_label} "
            f"(£{reference_price:.0f}). Likely a bargain if it stays under £{bid_ceiling:.0f}."
        )
    elif headroom_pct >= EARLY_WATCH_MIN_HEADROOM_PCT and (
        remaining is None or remaining >= EARLY_WATCH_MIN_REMAINING
    ):
        label = LABEL_EARLY_WATCH
        explanation = (
            f"Still early — £{current_bid:.2f} vs {reference_label} £{reference_price:.0f}, "
            f"plenty of time left. Too soon to call."
        )
    else:
        label = LABEL_POTENTIAL_DEAL
        explanation = (
            f"£{current_bid:.2f} is {headroom_pct:.0f}% below {reference_label} "
            f"(£{reference_price:.0f})."
        )

    return AuctionTrajectory(
        label=label,
        explanation=explanation,
        headroom_pct=round(headroom_pct, 1),
        bid_ceiling=bid_ceiling,
        accelerating=accelerating,
    )
