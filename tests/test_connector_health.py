"""Connector Health (connector_health.py) — explainable Healthy/Warning/
Degraded/Offline classification, roadmap Phase D. Pure-function tests: no
DB, just health/analytics dicts shaped like db.source_health()/
db.source_coverage_analytics() output.
"""

from datetime import datetime, timedelta, timezone

import pytest

from product_finder import connector_health as ch


def _iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


def _health(**overrides):
    base = {
        "consecutive_failures": 0,
        "success_rate": 100,
        "total_runs": 10,
        "last_success_at": _iso(minutes=5),
        "avg_duration_ms": 500,
        "recent_avg_duration_ms": 500,
        "avg_listings_found": 5.0,
        "recent_avg_listings_found": 5.0,
        "recent_run_count": 5,
    }
    base.update(overrides)
    return base


# --- Healthy baseline ---------------------------------------------------------


def test_no_issues_is_healthy():
    report = ch.evaluate(_health())
    assert report.status == ch.HEALTHY
    assert report.summary == "Healthy"
    assert report.reasons == []


# --- Consecutive failures ------------------------------------------------------


def test_degraded_at_threshold_consecutive_failures():
    report = ch.evaluate(_health(consecutive_failures=ch.DEGRADED_CONSECUTIVE_FAILURES))
    assert report.status == ch.DEGRADED
    assert "3 consecutive failures" in report.summary
    assert report.summary.startswith("Degraded:")


def test_below_threshold_consecutive_failures_stays_healthy():
    report = ch.evaluate(_health(consecutive_failures=ch.DEGRADED_CONSECUTIVE_FAILURES - 1))
    assert report.status == ch.HEALTHY


def test_offline_at_threshold_consecutive_failures():
    report = ch.evaluate(_health(consecutive_failures=ch.OFFLINE_CONSECUTIVE_FAILURES))
    assert report.status == ch.OFFLINE
    assert report.summary.startswith("Offline:")


# --- Success rate ----------------------------------------------------------------


def test_warning_success_rate():
    report = ch.evaluate(_health(success_rate=ch.WARNING_SUCCESS_RATE_PCT - 1, total_runs=20))
    assert report.status == ch.WARNING
    assert "success rate" in report.summary
    assert "over last 20 runs" in report.summary


def test_degraded_success_rate():
    report = ch.evaluate(_health(success_rate=ch.DEGRADED_SUCCESS_RATE_PCT - 1, total_runs=20))
    assert report.status == ch.DEGRADED


def test_offline_success_rate():
    report = ch.evaluate(_health(success_rate=ch.OFFLINE_SUCCESS_RATE_PCT - 1, total_runs=20))
    assert report.status == ch.OFFLINE


def test_success_rate_ignored_below_minimum_sample_size():
    # A single failing run out of 2 is a 50% success rate but far too small
    # a sample to call "Degraded" - the rule should not fire at all.
    report = ch.evaluate(_health(
        success_rate=50, total_runs=ch.MIN_RUNS_FOR_SUCCESS_RATE_SIGNAL - 1,
    ))
    assert report.status == ch.HEALTHY


# --- Last successful run age ------------------------------------------------------


def test_warning_last_success_age():
    report = ch.evaluate(_health(last_success_at=_iso(hours=ch.WARNING_LAST_SUCCESS_AGE_HOURS + 1)))
    assert report.status == ch.WARNING
    assert "last successful run" in report.summary


def test_degraded_last_success_age():
    report = ch.evaluate(_health(last_success_at=_iso(hours=ch.DEGRADED_LAST_SUCCESS_AGE_HOURS + 1)))
    assert report.status == ch.DEGRADED


def test_offline_last_success_age():
    report = ch.evaluate(_health(last_success_at=_iso(hours=ch.OFFLINE_LAST_SUCCESS_AGE_HOURS + 1)))
    assert report.status == ch.OFFLINE


def test_recent_last_success_no_reason():
    report = ch.evaluate(_health(last_success_at=_iso(minutes=1)))
    assert report.status == ch.HEALTHY


def test_no_last_success_at_all_does_not_double_report():
    # No successful run in the retained window at all - consecutive_failures
    # (or success_rate) already explains it; last_success_at=None shouldn't
    # add a second, redundant reason.
    report = ch.evaluate(_health(
        last_success_at=None, consecutive_failures=ch.OFFLINE_CONSECUTIVE_FAILURES,
    ))
    assert report.status == ch.OFFLINE
    assert len(report.reasons) == 1
    assert "consecutive failures" in report.reasons[0]


# --- Listings vs. baseline ------------------------------------------------------


def test_warning_listings_drop():
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_listings_found=10.0,
        recent_avg_listings_found=10.0 * ch.WARNING_LISTINGS_DROP_RATIO - 0.1,
    ))
    assert report.status == ch.WARNING
    assert "listings found dropped" in report.summary


def test_degraded_listings_drop():
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_listings_found=10.0,
        recent_avg_listings_found=10.0 * ch.DEGRADED_LISTINGS_DROP_RATIO - 0.1,
    ))
    assert report.status == ch.DEGRADED


def test_listings_drop_ignored_below_minimum_total_runs():
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL - 1,
        avg_listings_found=10.0, recent_avg_listings_found=0.0,
    ))
    assert report.status == ch.HEALTHY


def test_listings_drop_ignored_when_baseline_too_small():
    # Baseline of ~1/run swinging to 0 isn't a meaningful drop signal.
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_listings_found=ch.MIN_BASELINE_LISTINGS_FOR_DROP_SIGNAL - 0.5,
        recent_avg_listings_found=0.0,
    ))
    assert report.status == ch.HEALTHY


# --- Runtime vs. baseline --------------------------------------------------------


def test_warning_runtime_increase():
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_duration_ms=1000,
        recent_avg_duration_ms=int(1000 * ch.WARNING_RUNTIME_INCREASE_RATIO) + 1,
    ))
    assert report.status == ch.WARNING
    assert "runtime" in report.summary


def test_degraded_runtime_increase():
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_duration_ms=1000,
        recent_avg_duration_ms=int(1000 * ch.DEGRADED_RUNTIME_INCREASE_RATIO) + 1,
    ))
    assert report.status == ch.DEGRADED


def test_runtime_increase_ignored_when_baseline_too_fast():
    # A jump from 50ms to 400ms is "8x" but both numbers are timing noise.
    report = ch.evaluate(_health(
        total_runs=ch.MIN_TOTAL_RUNS_FOR_TREND_SIGNAL,
        avg_duration_ms=ch.MIN_BASELINE_DURATION_MS_FOR_LATENCY_SIGNAL - 50,
        recent_avg_duration_ms=400,
    ))
    assert report.status == ch.HEALTHY


# --- Stale rate (from source_coverage_analytics) ----------------------------------


def test_stale_rate_triggers_warning():
    report = ch.evaluate(_health(), {"stale_rate_pct": ch.WARNING_STALE_RATE_PCT})
    assert report.status == ch.WARNING
    assert "stale" in report.summary


def test_stale_rate_below_threshold_no_reason():
    report = ch.evaluate(_health(), {"stale_rate_pct": ch.WARNING_STALE_RATE_PCT - 1})
    assert report.status == ch.HEALTHY


def test_stale_rate_alone_never_exceeds_warning():
    # Even a 100% stale rate can't push past Warning by itself - it's
    # often normal marketplace churn, not a connector fault.
    report = ch.evaluate(_health(), {"stale_rate_pct": 100})
    assert report.status == ch.WARNING


def test_missing_analytics_does_not_crash():
    report = ch.evaluate(_health(), None)
    assert report.status == ch.HEALTHY


def test_analytics_without_stale_rate_key_does_not_crash():
    report = ch.evaluate(_health(), {})
    assert report.status == ch.HEALTHY


# --- Multiple reasons, severity ordering -------------------------------------------


def test_multiple_reasons_worst_status_wins_and_all_are_listed():
    report = ch.evaluate(
        _health(consecutive_failures=ch.DEGRADED_CONSECUTIVE_FAILURES),
        {"stale_rate_pct": ch.WARNING_STALE_RATE_PCT},
    )
    assert report.status == ch.DEGRADED  # worse of the two triggered
    assert len(report.reasons) == 2
    assert report.reasons[0].startswith("Degraded:")  # most severe first
    assert report.reasons[1].startswith("Warning:")
    assert report.summary == report.reasons[0]


def test_offline_outranks_degraded_and_warning_together():
    report = ch.evaluate(
        _health(
            consecutive_failures=ch.OFFLINE_CONSECUTIVE_FAILURES,
            success_rate=ch.DEGRADED_SUCCESS_RATE_PCT - 1, total_runs=20,
        ),
        {"stale_rate_pct": 100},
    )
    assert report.status == ch.OFFLINE
    assert len(report.reasons) == 3
    assert report.reasons[0].startswith("Offline:")


# --- Honesty about unavailable signals ----------------------------------------------


def test_unavailable_signals_documents_schema_parsing_gap():
    assert ch.UNAVAILABLE_SIGNALS
    assert any("parsing" in s.lower() or "schema" in s.lower() for s in ch.UNAVAILABLE_SIGNALS)
