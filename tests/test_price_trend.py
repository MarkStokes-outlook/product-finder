from datetime import datetime, timedelta, timezone

from product_finder import price_trend

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def obs(days_ago: float, price: float, source: str = "ebay") -> tuple[str, float, str]:
    return (_iso(days_ago), price, source)


# --- Hard confidence gate -------------------------------------------------------


def test_insufficient_observations_gives_zero_confidence_and_no_adjustment():
    # Two asking-price observations (weight 2 total) — below
    # MIN_WEIGHTED_OBSERVATIONS (4), even though both windows are populated.
    observations = [obs(5, 100), obs(40, 120)]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.confidence == 0.0
    assert result.pct is None
    assert price_trend.score_adjustment(result.pct, result.confidence) == 0.0


def test_no_trend_when_only_one_window_has_data():
    # Plenty of observations, but all in the recent window — nothing to
    # compare against, so this must not be treated as "flat" (pct=0).
    observations = [obs(1, 100), obs(2, 100), obs(3, 100), obs(4, 100), obs(5, 100)]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.confidence == 0.0
    assert result.pct is None


def test_short_span_fails_gate_even_with_enough_weighted_observations():
    # Both windows populated and weighted count clears the bar, but all
    # observations cluster right either side of the recent/prior boundary —
    # too little real time span to trust as a trend.
    observations = [
        obs(29, 100), obs(29.5, 101), obs(30.5, 100), obs(31, 99),
    ]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.confidence == 0.0
    assert result.pct is None


# --- Trend direction -------------------------------------------------------------


def test_downward_trend_gives_small_negative_adjustment():
    observations = [
        obs(5, 90), obs(10, 92), obs(15, 88),      # recent window, median 90
        obs(35, 100), obs(45, 102), obs(55, 98),   # prior window, median 100
    ]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.pct < 0
    assert result.confidence > 0

    adjustment = price_trend.score_adjustment(result.pct, result.confidence)
    assert adjustment < 0
    assert adjustment > -price_trend.MAX_SCORE_ADJUSTMENT  # modest trend, not capped


def test_upward_trend_gives_small_positive_adjustment():
    observations = [
        obs(5, 110), obs(10, 112), obs(15, 108),   # recent window, median 110
        obs(35, 100), obs(45, 102), obs(55, 98),   # prior window, median 100
    ]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.pct > 0
    assert result.confidence > 0

    adjustment = price_trend.score_adjustment(result.pct, result.confidence)
    assert adjustment > 0
    assert adjustment < price_trend.MAX_SCORE_ADJUSTMENT


# --- Sold vs. asking weighting ----------------------------------------------------


def test_ebay_close_observations_can_clear_the_gate_when_asking_alone_cannot():
    # Same shape, same raw count (3 observations) either side of the
    # boundary -- the only difference is one prior-window sighting is a
    # confirmed close, not an asking price.
    asking_only = [obs(5, 100), obs(10, 101), obs(40, 100)]
    with_a_close = [obs(5, 100), obs(10, 101), obs(40, 100, "ebay-close")]

    asking_result = price_trend.compute_trend(asking_only, now=NOW)
    close_result = price_trend.compute_trend(with_a_close, now=NOW)

    assert asking_result.confidence == 0.0  # weighted count 3 < MIN_WEIGHTED_OBSERVATIONS
    assert close_result.confidence > 0.0    # weighted count 5 clears the gate


def test_weighted_median_leans_toward_sold_price_over_asking_price():
    # One asking sighting at 100, one confirmed close at 200, in the same
    # window -- an unweighted median/mean would land at 150; the sold
    # price should pull the weighted median further toward 200.
    assert price_trend._weighted_median([(100.0, "ebay"), (200.0, "ebay-close")]) > 150.0


# --- Cap -----------------------------------------------------------------------


def test_score_adjustment_is_capped():
    # Large, high-confidence swing -- recent window well below prior, both
    # backed entirely by confirmed closes (max weight, max confidence).
    observations = [
        obs(5, 40, "ebay-close"), obs(10, 42, "ebay-close"),
        obs(35, 100, "ebay-close"), obs(45, 98, "ebay-close"),
    ]
    result = price_trend.compute_trend(observations, now=NOW)
    assert result.confidence == 1.0

    adjustment = price_trend.score_adjustment(result.pct, result.confidence)
    assert adjustment == -price_trend.MAX_SCORE_ADJUSTMENT

    # Sign-independent: an equally extreme rise caps at the positive side too.
    assert price_trend.score_adjustment(500.0, 1.0) == price_trend.MAX_SCORE_ADJUSTMENT
    assert price_trend.score_adjustment(-500.0, 1.0) == -price_trend.MAX_SCORE_ADJUSTMENT
