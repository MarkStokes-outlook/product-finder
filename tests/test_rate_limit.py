from unittest import mock

import pytest
import requests

from product_finder import rate_limit
from product_finder.rate_limit import RateLimitState, RateLimiter, request_with_backoff


# --- pure state transitions ---------------------------------------------------


def test_on_success_holds_delay_below_streak_threshold():
    state = RateLimitState(current_delay=4.0, consecutive_successes=0)
    for _ in range(rate_limit.SUCCESSES_BEFORE_RECOVERY - 1):
        state = rate_limit.on_success(state, min_delay=1.0)
    assert state.current_delay == 4.0  # unchanged — streak not long enough yet


def test_on_success_eases_delay_down_after_streak():
    state = RateLimitState(current_delay=4.0, consecutive_successes=0)
    for _ in range(rate_limit.SUCCESSES_BEFORE_RECOVERY):
        state = rate_limit.on_success(state, min_delay=1.0)
    assert state.current_delay == pytest.approx(4.0 * rate_limit.RECOVERY_MULTIPLIER)
    assert state.consecutive_successes == 0  # streak resets after easing


def test_on_success_never_drops_below_min_delay():
    state = RateLimitState(current_delay=1.0, consecutive_successes=0)
    for _ in range(rate_limit.SUCCESSES_BEFORE_RECOVERY):
        state = rate_limit.on_success(state, min_delay=1.0)
    assert state.current_delay == 1.0


def test_on_rate_limited_doubles_delay_and_resets_streak():
    state = RateLimitState(current_delay=2.0, consecutive_successes=3)
    new_state = rate_limit.on_rate_limited(state, retry_after=None, max_delay=60.0)
    assert new_state.current_delay == 4.0
    assert new_state.consecutive_successes == 0


def test_on_rate_limited_capped_at_max_delay():
    state = RateLimitState(current_delay=50.0)
    new_state = rate_limit.on_rate_limited(state, retry_after=None, max_delay=60.0)
    assert new_state.current_delay == 60.0


def test_on_rate_limited_respects_retry_after_as_a_floor():
    state = RateLimitState(current_delay=1.0)
    new_state = rate_limit.on_rate_limited(state, retry_after=30.0, max_delay=60.0)
    assert new_state.current_delay == 30.0  # bigger than 1.0 * 2.0, so the floor wins


def test_on_rate_limited_own_multiplier_wins_when_bigger_than_retry_after():
    state = RateLimitState(current_delay=20.0)
    new_state = rate_limit.on_rate_limited(state, retry_after=5.0, max_delay=60.0)
    assert new_state.current_delay == 40.0  # 20*2 > 5, so retry_after doesn't shrink it


# --- request_with_backoff -----------------------------------------------------


def _http_error(status_code, retry_after=None):
    response = mock.Mock()
    response.status_code = status_code
    response.headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return requests.HTTPError(response=response)


def test_succeeds_first_try_records_success():
    limiter = RateLimiter(min_delay=0.0, max_delay=10.0)
    make_request = mock.Mock(return_value="ok")
    with mock.patch("product_finder.rate_limit.time.sleep"):
        result = request_with_backoff(limiter, make_request, "ebay")
    assert result == "ok"
    assert make_request.call_count == 1
    assert limiter.current_delay == 0.0  # min_delay floor, one success doesn't move it


def test_retries_after_429_then_succeeds():
    limiter = RateLimiter(min_delay=1.0, max_delay=60.0)
    make_request = mock.Mock(side_effect=[_http_error(429), "ok"])
    with mock.patch("product_finder.rate_limit.time.sleep") as sleep:
        result = request_with_backoff(limiter, make_request, "ebay")
    assert result == "ok"
    assert make_request.call_count == 2
    assert sleep.called  # backed off before the retry


def test_gives_up_after_max_attempts_and_raises():
    limiter = RateLimiter(min_delay=0.1, max_delay=60.0)
    make_request = mock.Mock(side_effect=_http_error(429))
    with mock.patch("product_finder.rate_limit.time.sleep"):
        with pytest.raises(requests.HTTPError):
            request_with_backoff(limiter, make_request, "ebay", max_attempts=3)
    assert make_request.call_count == 3


def test_non_429_error_propagates_immediately_without_retry():
    limiter = RateLimiter(min_delay=0.1, max_delay=60.0)
    make_request = mock.Mock(side_effect=_http_error(500))
    with mock.patch("product_finder.rate_limit.time.sleep"):
        with pytest.raises(requests.HTTPError):
            request_with_backoff(limiter, make_request, "ebay")
    assert make_request.call_count == 1  # no retry for a non-rate-limit error


def test_repeated_429s_grow_the_delay_between_attempts():
    limiter = RateLimiter(min_delay=1.0, max_delay=60.0)
    make_request = mock.Mock(side_effect=[_http_error(429), _http_error(429), "ok"])
    with mock.patch("product_finder.rate_limit.time.sleep"):
        request_with_backoff(limiter, make_request, "ebay", max_attempts=3)
    assert limiter.current_delay == pytest.approx(4.0)  # 1.0 -> 2.0 -> 4.0 across two 429s


# --- source wiring -------------------------------------------------------------


def test_ebay_retries_on_429_then_succeeds():
    from product_finder.config import AppConfig, EbayConfig, SourcesConfig, ItemConfig
    from product_finder.sources.ebay import EbaySource

    cfg = AppConfig(sources=SourcesConfig(ebay=EbayConfig(app_id="id", cert_id="secret")))
    source = EbaySource(cfg)

    token_resp = mock.Mock()
    token_resp.raise_for_status = mock.Mock()
    token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200}

    ok_resp = mock.Mock()
    ok_resp.raise_for_status = mock.Mock()
    ok_resp.json.return_value = {"itemSummaries": []}

    with mock.patch("product_finder.sources.ebay.requests.post", return_value=token_resp):
        with mock.patch(
            "product_finder.sources.ebay.requests.get",
            side_effect=[_http_error_response(429), ok_resp],
        ):
            with mock.patch("product_finder.rate_limit.time.sleep"):
                listings = source.search("makita drill", ItemConfig(name="x", terms=["x"]))
    assert listings == []


def _http_error_response(status_code):
    resp = mock.Mock()
    resp.raise_for_status.side_effect = requests.HTTPError(response=mock.Mock(
        status_code=status_code, headers={},
    ))
    return resp


def test_rss_retries_on_429_then_succeeds():
    from product_finder.config import AppConfig, ExtraSourceConfig, ItemConfig
    from product_finder.sources.rss import RssSource

    cfg = AppConfig()
    spec = ExtraSourceConfig(name="hukd", type="rss", url="https://h.example/rss?q={term}")
    source = RssSource(cfg, spec)

    ok_resp = mock.Mock(text="<rss version=\"2.0\"><channel></channel></rss>")
    ok_resp.raise_for_status = mock.Mock()

    with mock.patch(
        "product_finder.sources.rss.requests.get",
        side_effect=[_http_error_response(429), ok_resp],
    ):
        with mock.patch("product_finder.rate_limit.time.sleep"):
            listings = source.search("track saw", ItemConfig(name="x", terms=["x"]))
    assert listings == []
