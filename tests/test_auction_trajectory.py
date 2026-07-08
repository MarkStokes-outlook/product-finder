from datetime import timedelta

from product_finder import auction_trajectory as at


def _snap(observed_at: str, bid: float | None) -> dict:
    return {"observed_at": observed_at, "current_bid_price": bid}


# --- evaluate(): reference price gate ---------------------------------------------


def test_no_reference_price_is_insufficient_data():
    result = at.evaluate(
        current_bid=50.0, bid_count=3, remaining=timedelta(hours=1), reference_price=None
    )
    assert result.label == at.LABEL_INSUFFICIENT_DATA
    assert result.headroom_pct is None
    assert result.bid_ceiling is None


# --- evaluate(): no bids yet ------------------------------------------------------


def test_no_bids_yet_is_early_watch():
    result = at.evaluate(
        current_bid=None, bid_count=0, remaining=timedelta(hours=2), reference_price=200.0
    )
    assert result.label == at.LABEL_EARLY_WATCH
    assert result.headroom_pct is None
    assert result.bid_ceiling == 180.0  # 200 * 0.9
    assert "No bids yet" in result.explanation


# --- evaluate(): headroom-based labelling -----------------------------------------


def test_bid_at_or_above_reference_is_no_longer_a_deal():
    result = at.evaluate(
        current_bid=210.0, bid_count=5, remaining=timedelta(hours=1), reference_price=200.0
    )
    assert result.label == at.LABEL_NO_LONGER_DEAL
    assert result.headroom_pct <= 0


def test_moderate_headroom_is_likely_bargain_with_ceiling():
    # 200 reference, bid 160 -> 20% headroom (<=25%), no snapshot history so
    # never "accelerating" -> falls to LIKELY_BARGAIN, not GETTING_HOT.
    result = at.evaluate(
        current_bid=160.0, bid_count=4, remaining=timedelta(hours=3), reference_price=200.0
    )
    assert result.label == at.LABEL_LIKELY_BARGAIN
    assert result.headroom_pct == 20.0
    assert result.bid_ceiling == 180.0
    assert "£180" in result.explanation


def test_high_headroom_with_lots_of_time_is_early_watch():
    # 200 reference, bid 90 -> 55% headroom (>=50%), 8 hours left (>=6h).
    result = at.evaluate(
        current_bid=90.0, bid_count=2, remaining=timedelta(hours=8), reference_price=200.0
    )
    assert result.label == at.LABEL_EARLY_WATCH
    assert result.headroom_pct == 55.0


def test_high_headroom_but_short_time_is_potential_deal():
    # Same 55% headroom, but ending soon -> not "too early" any more, just a deal.
    result = at.evaluate(
        current_bid=90.0, bid_count=2, remaining=timedelta(minutes=30), reference_price=200.0
    )
    assert result.label == at.LABEL_POTENTIAL_DEAL


def test_mid_headroom_is_potential_deal():
    # 200 reference, bid 130 -> 35% headroom: above DEAL_MIN (25) and below
    # EARLY_WATCH_MIN (50) -> plain "potential deal" band.
    result = at.evaluate(
        current_bid=130.0, bid_count=3, remaining=timedelta(hours=2), reference_price=200.0
    )
    assert result.label == at.LABEL_POTENTIAL_DEAL


# --- evaluate(): accelerating bid velocity pushes into "getting too hot" ---------


def test_accelerating_bid_near_reference_is_getting_too_hot():
    # Steady £2/hr for the first two gaps, then £10 in the last 20 minutes —
    # a clear acceleration versus the earlier pace.
    snapshots = [
        _snap("2026-07-08T10:00:00.000Z", 100.0),
        _snap("2026-07-08T11:00:00.000Z", 102.0),
        _snap("2026-07-08T12:00:00.000Z", 104.0),
        _snap("2026-07-08T12:20:00.000Z", 114.0),
    ]
    result = at.evaluate(
        current_bid=180.0, bid_count=10, remaining=timedelta(minutes=15),
        reference_price=200.0, snapshots=snapshots,
    )
    assert result.headroom_pct == 10.0  # within HOT_MAX_HEADROOM_PCT
    assert result.accelerating is True
    assert result.label == at.LABEL_GETTING_HOT


def test_steady_bid_near_reference_without_history_is_likely_bargain_not_hot():
    # Same headroom band as above, but no snapshot history at all -> can't
    # claim acceleration, so it must not be labelled "too hot".
    result = at.evaluate(
        current_bid=180.0, bid_count=10, remaining=timedelta(minutes=15), reference_price=200.0
    )
    assert result.accelerating is None
    assert result.label == at.LABEL_LIKELY_BARGAIN


# --- bid_velocity_accelerating(): confidence gating -------------------------------


def test_velocity_none_with_fewer_than_three_snapshots():
    snapshots = [
        _snap("2026-07-08T10:00:00.000Z", 100.0),
        _snap("2026-07-08T11:00:00.000Z", 110.0),
    ]
    assert at.bid_velocity_accelerating(snapshots) is None


def test_velocity_none_when_span_too_short():
    snapshots = [
        _snap("2026-07-08T10:00:00.000Z", 100.0),
        _snap("2026-07-08T10:01:00.000Z", 101.0),
        _snap("2026-07-08T10:02:00.000Z", 102.0),
    ]
    assert at.bid_velocity_accelerating(snapshots) is None  # < MIN_SPAN_FOR_VELOCITY


def test_velocity_ignores_snapshots_with_no_bid_yet():
    snapshots = [
        _snap("2026-07-08T09:00:00.000Z", None),  # no bid yet at this point
        _snap("2026-07-08T10:00:00.000Z", 100.0),
        _snap("2026-07-08T11:00:00.000Z", 102.0),
        _snap("2026-07-08T12:00:00.000Z", 104.0),
        _snap("2026-07-08T12:30:00.000Z", 120.0),
    ]
    # 4 real bid points span 2.5h, well past the minimum -> should resolve
    # to a real answer (accelerating), not bail out due to the None entry.
    assert at.bid_velocity_accelerating(snapshots) is True


def test_velocity_steady_pace_is_not_accelerating():
    snapshots = [
        _snap("2026-07-08T10:00:00.000Z", 100.0),
        _snap("2026-07-08T11:00:00.000Z", 105.0),
        _snap("2026-07-08T12:00:00.000Z", 110.0),
        _snap("2026-07-08T13:00:00.000Z", 115.0),
    ]
    assert at.bid_velocity_accelerating(snapshots) is False
