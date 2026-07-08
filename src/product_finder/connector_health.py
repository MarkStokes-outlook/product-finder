"""Connector health status — explainable, rule-based, not a black-box
score. Built entirely from telemetry Phase A/B already persist
(db.source_health, db.source_coverage_analytics); this module adds no new
data collection, only interpretation.

Pure functions only, same style as auction_trajectory.py/price_trend.py:
no DB or network access, no class state. Every rule below independently
decides whether it's triggered and, if so, at what severity — the overall
status is simply the most severe rule triggered, and every triggered
rule's reason is reported, not just the worst one. There is deliberately
no blended/weighted score: two connectors both landing on "Warning" for
completely different reasons should read as two different sentences, not
the same number.

Some signals the roadmap asked this module to consider are honestly
unavailable given what's persisted today — see UNAVAILABLE_SIGNALS. They
are documented, not faked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Statuses, most to least severe ---------------------------------------

HEALTHY = "healthy"
WARNING = "warning"
DEGRADED = "degraded"
OFFLINE = "offline"

_SEVERITY = {HEALTHY: 0, WARNING: 1, DEGRADED: 2, OFFLINE: 3}

STATUS_LABELS = {HEALTHY: "Healthy", WARNING: "Warning", DEGRADED: "Degraded", OFFLINE: "Offline"}

# --- Signals this module was asked to consider but cannot honestly compute -

#: db.record_source_run's `errors` counter is a single tally of every
#: exception search() raised in a cycle — network failures, auth failures,
#: and response-parsing/schema failures are all caught identically
#: (runner.run_once's per-term try/except) and none is classified
#: separately. There is no way to say "this connector is failing because
#: its parser broke" specifically, only "this connector is failing" (see
#: the consecutive-failure and success-rate checks below, which do cover
#: the general case). Would need per-exception-type tagging in
#: db.record_source_run to answer this honestly.
UNAVAILABLE_SIGNALS = (
    "Schema/parsing-specific failure detection: only generic run failures "
    "are tracked (any exception, uncategorised) — not whether a failure "
    "was specifically a parsing/schema error vs. a network or auth error.",
)

# --- Tuning constants (named, not inline mystery numbers) ------------------

#: 3+ failing runs in a row is more than one flaky cycle.
DEGRADED_CONSECUTIVE_FAILURES = 3
#: 6+ in a row (roughly a full day of hourly cycles with zero success) —
#: not a bad run, just not working.
OFFLINE_CONSECUTIVE_FAILURES = 6

#: Success rate thresholds (% of runs in the retained window that were
#: clean). Only evaluated once there's enough runs to trust a percentage —
#: see MIN_RUNS_FOR_SUCCESS_RATE_SIGNAL.
WARNING_SUCCESS_RATE_PCT = 90
DEGRADED_SUCCESS_RATE_PCT = 70
OFFLINE_SUCCESS_RATE_PCT = 40
#: Below this many runs, a success-rate percentage is one or two flukes
#: away from looking catastrophic — too small a sample to act on alone.
MIN_RUNS_FOR_SUCCESS_RATE_SIGNAL = 3

#: How long since the last clean run before that alone is worth flagging —
#: independent of consecutive_failures, since a connector that's simply
#: stopped running (rather than actively erroring) still has
#: consecutive_failures=0.
WARNING_LAST_SUCCESS_AGE_HOURS = 6.0
DEGRADED_LAST_SUCCESS_AGE_HOURS = 24.0
OFFLINE_LAST_SUCCESS_AGE_HOURS = 72.0

#: A connector needs at least this many total runs before "recent vs.
#: baseline" comparisons (listings-drop, runtime-increase, below) are
#: meaningful — otherwise "recent" and "baseline" overlap too much to mean
#: anything.
MIN_TOTAL_RUNS_FOR_TREND_SIGNAL = 8

#: recent_avg_listings_found <= this fraction of avg_listings_found.
WARNING_LISTINGS_DROP_RATIO = 0.5
DEGRADED_LISTINGS_DROP_RATIO = 0.15
#: Only meaningful once the baseline itself finds a non-trivial number of
#: listings per run — a connector that normally finds ~1/run swinging to 0
#: isn't a meaningful "100% drop", it's noise at a small sample size.
MIN_BASELINE_LISTINGS_FOR_DROP_SIGNAL = 2.0

#: recent_avg_duration_ms >= this multiple of avg_duration_ms.
WARNING_RUNTIME_INCREASE_RATIO = 2.0
DEGRADED_RUNTIME_INCREASE_RATIO = 4.0
#: Below this, timing noise on an already-fast connector could look like a
#: "300% slower" swing despite being a handful of milliseconds either way.
MIN_BASELINE_DURATION_MS_FOR_LATENCY_SIGNAL = 200

#: A high stale rate (source_coverage_analytics' stale_rate_pct) is often
#: normal marketplace churn — items sell, listings expire — not a
#: connector fault. It can only ever push to Warning, never
#: Degraded/Offline, on its own.
WARNING_STALE_RATE_PCT = 60


@dataclass(frozen=True)
class ConnectorHealthReport:
    """status is the single most severe rule triggered (or HEALTHY if
    none were). summary is that rule's own reason, ready to show inline.
    reasons lists every triggered rule's reason, most severe first — the
    expanded/detail view. reasons is empty exactly when status is HEALTHY;
    there's nothing to elaborate on."""

    status: str
    summary: str
    reasons: list[str] = field(default_factory=list)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt_age(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)} minutes"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _check_consecutive_failures(health: dict) -> tuple[str, str] | None:
    n = health["consecutive_failures"]
    if n >= OFFLINE_CONSECUTIVE_FAILURES:
        return OFFLINE, f"{n} consecutive failures"
    if n >= DEGRADED_CONSECUTIVE_FAILURES:
        return DEGRADED, f"{n} consecutive failures"
    return None


def _check_success_rate(health: dict) -> tuple[str, str] | None:
    rate = health["success_rate"]
    total = health["total_runs"]
    if rate is None or total < MIN_RUNS_FOR_SUCCESS_RATE_SIGNAL:
        return None
    reason = f"success rate {rate}% over last {total} runs"
    if rate < OFFLINE_SUCCESS_RATE_PCT:
        return OFFLINE, reason
    if rate < DEGRADED_SUCCESS_RATE_PCT:
        return DEGRADED, reason
    if rate < WARNING_SUCCESS_RATE_PCT:
        return WARNING, reason
    return None


def _check_last_success_age(health: dict, now: datetime) -> tuple[str, str] | None:
    last_success = health["last_success_at"]
    if last_success is None:
        # Covered by consecutive_failures/success_rate already firing for
        # a source with no clean run at all in the retained window — not
        # worth a second, redundant reason.
        return None
    age_hours = (now - _parse(last_success)).total_seconds() / 3600
    reason = f"last successful run {_fmt_age(age_hours)} ago"
    if age_hours >= OFFLINE_LAST_SUCCESS_AGE_HOURS:
        return OFFLINE, reason
    if age_hours >= DEGRADED_LAST_SUCCESS_AGE_HOURS:
        return DEGRADED, reason
    if age_hours >= WARNING_LAST_SUCCESS_AGE_HOURS:
        return WARNING, reason
    return None


def _check_listings_vs_baseline(health: dict) -> tuple[str, str] | None:
    recent = health["recent_avg_listings_found"]
    baseline = health["avg_listings_found"]
    if (
        recent is None or not baseline
        or health["total_runs"] < MIN_TOTAL_RUNS_FOR_TREND_SIGNAL
        or baseline < MIN_BASELINE_LISTINGS_FOR_DROP_SIGNAL
    ):
        return None
    ratio = recent / baseline
    reason = (
        f"listings found dropped to {recent:g}/run over the last "
        f"{health['recent_run_count']} runs (baseline {baseline:g}/run)"
    )
    if ratio <= DEGRADED_LISTINGS_DROP_RATIO:
        return DEGRADED, reason
    if ratio <= WARNING_LISTINGS_DROP_RATIO:
        return WARNING, reason
    return None


def _check_runtime_vs_baseline(health: dict) -> tuple[str, str] | None:
    recent = health["recent_avg_duration_ms"]
    baseline = health["avg_duration_ms"]
    if (
        recent is None or not baseline
        or health["total_runs"] < MIN_TOTAL_RUNS_FOR_TREND_SIGNAL
        or baseline < MIN_BASELINE_DURATION_MS_FOR_LATENCY_SIGNAL
    ):
        return None
    ratio = recent / baseline
    reason = (
        f"runtime {recent}ms/run over the last {health['recent_run_count']} runs, "
        f"{ratio:.1f}x the {baseline}ms baseline"
    )
    if ratio >= DEGRADED_RUNTIME_INCREASE_RATIO:
        return DEGRADED, reason
    if ratio >= WARNING_RUNTIME_INCREASE_RATIO:
        return WARNING, reason
    return None


def _check_stale_rate(analytics: dict | None) -> tuple[str, str] | None:
    if not analytics:
        return None
    rate = analytics.get("stale_rate_pct")
    if rate is None or rate < WARNING_STALE_RATE_PCT:
        return None
    return WARNING, f"{rate}% of listings have gone stale (unseen 48h+)"


_CHECKS = (
    _check_consecutive_failures,
    _check_success_rate,
    _check_listings_vs_baseline,
    _check_runtime_vs_baseline,
)


def evaluate(
    health: dict,
    analytics: dict | None = None,
    *,
    now: datetime | None = None,
) -> ConnectorHealthReport:
    """health: one source's entry from db.source_health() (must be present
    — callers should only invoke this for a source with at least one
    recorded run; a source with none is "not yet run", a UI state this
    module doesn't need to know about). analytics: the matching entry from
    db.source_coverage_analytics(), if available — only used for the
    stale-rate signal, entirely optional."""
    now = now or datetime.now(timezone.utc)
    triggered = [c(health) for c in _CHECKS]
    triggered.append(_check_last_success_age(health, now))
    triggered.append(_check_stale_rate(analytics))
    triggered = [t for t in triggered if t is not None]

    if not triggered:
        return ConnectorHealthReport(status=HEALTHY, summary=STATUS_LABELS[HEALTHY], reasons=[])

    triggered.sort(key=lambda t: _SEVERITY[t[0]], reverse=True)
    status = triggered[0][0]
    reasons = [f"{STATUS_LABELS[sev]}: {reason}" for sev, reason in triggered]
    return ConnectorHealthReport(status=status, summary=reasons[0], reasons=reasons)
