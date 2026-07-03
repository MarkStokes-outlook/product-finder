"""Adaptive per-source request pacing.

Learns, per Source instance, how much space to leave between requests: backs
off hard on a 429 (respecting a Retry-After header when the API sends one),
and gently eases back down after a run of clean requests — probing for the
fastest pace that still stays under the limit, rather than staying as
cautious as the last failure made it forever.

Scoped per Source instance (constructed once in __init__), not process-wide
— runner.run_once() builds the source registry once per cycle and reuses the
same instances for every term/item searched that cycle, so state already
persists across the whole burst of calls one watch tick makes. It doesn't
need to (and doesn't) survive across separate cycles — an hour's gap between
`watch` ticks is long enough for any real rate-limit window to reset, so
starting back at the fast/optimistic end each cycle is the right call, not a
gap in the design.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

BACKOFF_MULTIPLIER = 2.0
RECOVERY_MULTIPLIER = 0.9
SUCCESSES_BEFORE_RECOVERY = 5
DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class RateLimitState:
    current_delay: float
    consecutive_successes: int = 0


def on_success(state: RateLimitState, min_delay: float) -> RateLimitState:
    """A clean request. After a long enough streak, ease the delay back down
    a little — probing for the fastest pace that still stays under the
    limit, rather than staying permanently as cautious as the last failure
    made it."""
    successes = state.consecutive_successes + 1
    delay = state.current_delay
    if successes >= SUCCESSES_BEFORE_RECOVERY:
        delay = max(min_delay, delay * RECOVERY_MULTIPLIER)
        successes = 0
    return RateLimitState(current_delay=delay, consecutive_successes=successes)


def on_rate_limited(
    state: RateLimitState, retry_after: float | None, max_delay: float
) -> RateLimitState:
    """A 429. Back off hard and reset the success streak — a fresh burst of
    clean requests has to re-earn any speed-up. `retry_after`, when the API
    sends one, is respected as a floor: never wait less than the server
    explicitly asked for, even if our own multiplier would suggest less."""
    delay = min(max_delay, state.current_delay * BACKOFF_MULTIPLIER)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return RateLimitState(current_delay=delay, consecutive_successes=0)


def _parse_retry_after(value: str | None) -> float | None:
    """Only the delay-seconds form of Retry-After is handled — the
    alternative HTTP-date form is valid per spec but hasn't been observed
    from eBay or Reddit in practice, so it's not worth the parsing surface
    until it actually shows up."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class RateLimiter:
    """Wall-clock pacing for one source. One instance per Source object —
    see the module docstring for why per-instance (not process-wide) scope
    is the right lifetime here."""

    def __init__(self, min_delay: float, max_delay: float):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._state = RateLimitState(current_delay=min_delay)
        self._last_request_at = 0.0

    @property
    def current_delay(self) -> float:
        return self._state.current_delay

    def wait(self) -> None:
        remaining = (self._last_request_at + self._state.current_delay) - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def record_success(self) -> None:
        self._state = on_success(self._state, self.min_delay)

    def record_rate_limited(self, retry_after: float | None = None) -> None:
        self._state = on_rate_limited(self._state, retry_after, self.max_delay)


def request_with_backoff(limiter: RateLimiter, make_request, source_name: str,
                          max_attempts: int = DEFAULT_MAX_ATTEMPTS):
    """Pace and run `make_request()` (a zero-arg callable performing one
    HTTP request and calling raise_for_status()), retrying a 429 up to
    `max_attempts` times with the limiter's own growing backoff before
    giving up. The final attempt's exception propagates — same as any other
    request failure, which callers already treat as "this term failed this
    cycle" (see runner.py's per-term try/except)."""
    last_exc: requests.HTTPError | None = None
    for attempt in range(max_attempts):
        limiter.wait()
        try:
            response = make_request()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                limiter.record_rate_limited(retry_after)
                log.warning(
                    "%s: rate limited (attempt %d/%d) — backing off to %.1fs between requests",
                    source_name, attempt + 1, max_attempts, limiter.current_delay,
                )
                last_exc = exc
                continue
            raise
        else:
            limiter.record_success()
            return response
    raise last_exc
