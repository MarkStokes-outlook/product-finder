"""Used-price trend estimation from `product_price_observations` history.

Two-window median comparison — the last WINDOW_DAYS vs. the WINDOW_DAYS
immediately before that — deliberately not a regression/slope fit. A
handful of noisy asking-price points will overreact to one outlier under a
fitted slope; comparing two medians is boring and robust, which is what a
small-sample trend signal needs to be.

Every observation is weighted by how much it's worth as evidence: a
confirmed sold price (auction_watch.py logs these with a "-close" source
suffix — see scoring.is_live_auction) is worth more than a plain asking
price, both when building each window's median (leans the figure toward
the more trustworthy price) and when judging whether there's *enough*
evidence to trust a trend at all (see MIN_WEIGHTED_OBSERVATIONS) — a
couple of sold prices can earn confidence that would take many more
asking-price sightings alone.

Recomputed and cached on `products.price_trend_pct` /
`price_trend_confidence` whenever a new observation is recorded (see
db.record_price_observation) — scoring.py only ever reads the cached
value, never recomputes a trend per listing evaluated. This keeps a
listing's score from drifting between two reads of the same data, and
means the trend only ever moves when genuinely new evidence arrives.

Scoped to used-price only for v1 (see docs/strategy/roadmap.md, "Deal
accuracy") — new-price history is being collected (see
db.record_new_price_history) but deliberately not scored yet, since it
hasn't accumulated enough real data to validate a trend against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Sequence

# --- Tuning constants --------------------------------------------------------
# All guesses pending real usage data — not calibrated against outcomes yet.
# Called out here, in one place, specifically so they're easy to find and
# revisit rather than buried in the maths below.

# A closing-auction observation is logged with this source suffix (see
# auction_watch.py) — a confirmed sold price, not just an asking price.
SOLD_SOURCE_SUFFIX = "-close"

# How much more a sold price counts than a plain asking-price sighting, both
# in the window median and the evidence-count gate below.
SOLD_WEIGHT = 3
ASKING_WEIGHT = 1

# Two comparison windows: "recent" = the last WINDOW_DAYS, "prior" = the
# WINDOW_DAYS immediately before that.
WINDOW_DAYS = 30

# Hard gate: below this weighted evidence count, or with the observations in
# scope spanning less than MIN_SPAN_DAYS, no trend is reported at all
# (confidence 0, pct None) — never a guess dressed up as a number.
MIN_WEIGHTED_OBSERVATIONS = 4
MIN_SPAN_DAYS = 21

# Confidence ramps from just-above-0 at the gate up to 1.0 once weighted
# evidence reaches this count — smooths the transition across the gate
# rather than jumping straight from "no adjustment" to "full adjustment".
CONFIDENCE_FULL_AT_WEIGHT = 12

# Score-adjustment shape: a trend magnitude of FULL_ADJUSTMENT_PCT or more
# earns the full MAX_SCORE_ADJUSTMENT; below that it scales linearly (and
# with confidence), clipped rather than extrapolated beyond it.
FULL_ADJUSTMENT_PCT = 20.0
MAX_SCORE_ADJUSTMENT = 8.0


@dataclass
class TrendResult:
    """`pct` is the signed percent change of the recent window's weighted
    median vs. the prior window's (positive = rising, negative = falling).
    Always None when `confidence` is 0 — there's nothing to report."""

    pct: float | None
    confidence: float  # 0.0-1.0


_NO_TREND = TrendResult(pct=None, confidence=0.0)


def _weight(source: str) -> int:
    return SOLD_WEIGHT if source.endswith(SOLD_SOURCE_SUFFIX) else ASKING_WEIGHT


def _weighted_median(rows: Sequence[tuple[float, str]]) -> float | None:
    """Median of prices, each repeated by its evidence weight — the
    simplest way to lean a median toward higher-quality observations
    without a dedicated weighted-median implementation."""
    if not rows:
        return None
    expanded: list[float] = []
    for price, source in rows:
        expanded.extend([price] * _weight(source))
    return median(expanded)


def _parse(observed_at: str) -> datetime:
    return datetime.fromisoformat(observed_at.replace("Z", "+00:00"))


def compute_trend(
    observations: Sequence[tuple[str, float, str]], now: datetime | None = None
) -> TrendResult:
    """observations: (observed_at ISO string, price, source) tuples, any
    order, any age — anything older than 2*WINDOW_DAYS is ignored here
    regardless of what the caller already filtered."""
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=WINDOW_DAYS)
    prior_cutoff = now - timedelta(days=2 * WINDOW_DAYS)

    recent: list[tuple[float, str]] = []
    prior: list[tuple[float, str]] = []
    timestamps: list[datetime] = []
    for observed_at, price, source in observations:
        ts = _parse(observed_at)
        if ts < prior_cutoff:
            continue
        timestamps.append(ts)
        (recent if ts >= recent_cutoff else prior).append((price, source))

    if not recent or not prior:
        return _NO_TREND

    weighted_count = sum(_weight(source) for _, source in recent + prior)
    span_days = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
    if weighted_count < MIN_WEIGHTED_OBSERVATIONS or span_days < MIN_SPAN_DAYS:
        return _NO_TREND

    recent_median = _weighted_median(recent)
    prior_median = _weighted_median(prior)
    if not prior_median:
        return _NO_TREND

    pct = (recent_median - prior_median) / prior_median * 100.0
    confidence = min(1.0, weighted_count / CONFIDENCE_FULL_AT_WEIGHT)
    return TrendResult(pct=round(pct, 1), confidence=round(confidence, 2))


def score_adjustment(pct: float | None, confidence: float) -> float:
    """Signed deal-score adjustment for a cached trend — same sign as pct
    (falling price -> small negative nudge, since waiting may do better;
    rising price -> small positive nudge, since today is as good as it
    gets), scaled by confidence and capped at +/-MAX_SCORE_ADJUSTMENT.
    Zero whenever there's no trend to act on."""
    if pct is None or confidence <= 0:
        return 0.0
    clipped = max(-FULL_ADJUSTMENT_PCT, min(pct, FULL_ADJUSTMENT_PCT))
    return round(clipped / FULL_ADJUSTMENT_PCT * MAX_SCORE_ADJUSTMENT * confidence, 1)
