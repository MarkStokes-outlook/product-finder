# Connector Health — Phase D of the "acquisition platform" roadmap

**Date:** 2026-07-08 ~22:30
**Tests:** 622 passing (616 prior + 6 net new; `test_connector_health.py`
is new with 28 tests, plus 6 page-rendering tests added to
`test_coverage.py`, plus 2 telemetry tests for the recent-window extension)
**Trigger:** Continuation of [[acquisition_platform_roadmap]] — Phases A
(connector maturity), B (coverage analytics), C (capability explorer)
shipped earlier the same session; this is Phase D, scoped by Mark as: an
explainable Healthy/Warning/Degraded/Offline status considering
consecutive failures, recent success rate, last-success age, average
runtime/latency, listings vs. recent baseline, schema/parsing failures if
available, and stale-data indicators if available — no black-box score,
named/configurable thresholds, honest about unavailable signals, don't
touch Phase A/B's existing raw metrics.

## Design decisions made before writing code

**Rule-based, not scored — and no single "worst reason" hides the rest.**
Considered a weighted-sum health score early and rejected it immediately:
Mark's brief explicitly said "avoid a black-box score" and "prefer
explicit reasons over a single magic number." `connector_health.evaluate()`
runs six independent checks, each either silent or returning `(severity,
reason)`. The overall status is the worst severity triggered, but *every*
triggered reason is returned (`reasons`, most severe first), not just the
one that decided the status — so a connector that's both Degraded from
consecutive failures *and* Warning from a rising stale rate shows both
sentences, not just the worse one silently absorbing the other.

**"Recent vs. baseline" needed new telemetry, added as its own small
commit first.** Two of the six signals Mark asked for — "average runtime
compared with recent baseline" and "listings returned compared with recent
baseline" — don't exist yet: Phase A's `avg_duration_ms`/
`avg_listings_found` are single numbers averaged over the *entire* retained
window, with no split between "recent" and "historical". Rather than
inventing an absolute cross-connector threshold (rejected: an RSS feed and
the eBay API have genuinely different normal latencies, so a fixed
"200ms is slow" rule would be unfair to one and meaningless for the
other), extended `db.source_health()` to also track each source's most
recent `_RECENT_RUN_SAMPLE_SIZE` (5) runs' own average, alongside the
existing full-window average — zero new SQL, since the existing query
already returns rows ordered newest-first per source; just a capped list
appended to in the same loop. Committed and tested separately (`9ab3517`)
before writing any health-model logic, so the telemetry layer and the
classification layer could each be verified independently.

**Where do the two modules' constants live?** `db.py` owns
`_RECENT_RUN_SAMPLE_SIZE` (a *sampling* parameter — how many rows to keep
for the recent window) because it's intrinsic to the query/aggregation.
`connector_health.py` owns every *threshold* (what ratio counts as a
"drop", what age counts as "stale success") because those are policy, not
data-shape. Neither module imports constants from the other —
`connector_health.py` just reads whatever `recent_run_count`/
`recent_avg_*` numbers `db.py` publishes and applies its own ratios. Kept
the two concerns cleanly separated rather than one giant module owning
both "what to measure" and "what it means".

## The six rules

1. **Consecutive failures** — Degraded at 3, Offline at 6.
2. **Success rate** — Warning/Degraded/Offline at 90/70/40%, but only once
   `total_runs >= MIN_RUNS_FOR_SUCCESS_RATE_SIGNAL` (3) — a single bad run
   out of 2 is a 50% rate that would otherwise look catastrophic on no
   real evidence.
3. **Last successful run age** — Warning/Degraded/Offline at 6/24/72 hours.
   Deliberately independent of consecutive_failures: a connector that's
   simply *stopped being invoked* (not erroring, just quiet) still has
   `consecutive_failures=0` but a stale `last_success_at` — this is the
   rule that answers Mark's "when a source goes quiet" framing directly.
   Skipped entirely when `last_success_at` is `None` (no successful run in
   the window at all) — that case is already covered by
   consecutive-failures/success-rate, and adding a second reason for the
   same underlying fact would be noise, not information.
4. **Listings vs. baseline** — Warning/Degraded when the last 5 runs'
   average drop to ≤50%/≤15% of the full-window baseline. Gated on
   `total_runs >= 8` (otherwise "recent" and "baseline" overlap too much
   to mean anything) and `avg_listings_found >= 2.0` (a connector that
   normally finds ~1/run swinging to 0 isn't a meaningful "100% drop",
   it's noise at a tiny sample).
5. **Runtime vs. baseline** — Warning/Degraded at 2x/4x the baseline
   duration, gated the same way plus a `>= 200ms` baseline floor (avoids
   "50ms → 400ms" reading as a dramatic "8x slower" when both numbers are
   timing noise on an already-fast connector).
6. **Stale rate** (from Phase B's `source_coverage_analytics`) — Warning
   only, never worse, at ≥60%. Deliberately capped: listings going stale
   is often normal marketplace churn (items sell, get delisted), not
   evidence the connector itself is broken — using it as a Degraded/Offline
   signal would conflate "this connector isn't working" with "this
   connector's market is quiet", two different facts.

## What's honestly unavailable

**Schema/parsing-specific failure classification.** Investigated before
writing any rule: `runner.run_once()`'s per-term `try/except Exception`
catches every failure identically — network errors, auth errors, and
response-parsing/schema errors all increment the same `errors` counter
with no type tag. There's no way to say "this connector is Degraded
*because its parser broke*" specifically; only "this connector is
Degraded" (via the consecutive-failures/success-rate rules, which do
cover the general case). Documented as `connector_health.UNAVAILABLE_SIGNALS`
and disclosed directly in the Sources page hint text ("Failures aren't
classified by cause... only whether a run succeeded") — visible where
Mark will actually read it, not buried in a code comment. Fixing this
honestly would need per-exception-type tagging added to
`db.record_source_run`, out of scope here.

## UI

Replaced the Health column's old binary `ok`/`failing ×N` badge in the
existing Sources table (not a new table) with the four-state status badge
+ concise summary reason, plus an expandable `<details>` fold ("N reasons")
when more than one rule triggered — reusing the existing `details.listings`
collapsible pattern again. A Healthy source shows the pre-existing
`listings_24h` context line instead of a reason (there's nothing to
explain). Sources with zero runs keep the unchanged "not yet run" state —
`app.py` only calls `connector_health.evaluate()` when `row["health"]` is
present. Four new badge colours (green/amber/red-soft/red-soft-bold for
Healthy/Warning/Degraded/Offline) added to `base.html`, following the
existing soft-palette convention rather than introducing solid fills not
used elsewhere on the page.

**Test-writing note:** two page-rendering tests initially failed as false
positives/negatives — checking bare substrings like `b"Degraded"` or
`b"health-healthy"` against the *whole* HTML response matches this
phase's own CSS rules and code comments in `base.html`'s embedded
`<style>` block (e.g. `.badge.health-healthy { ... }` and a comment
mentioning "Degraded" by name), which are present on *every* page load
regardless of whether any badge actually rendered. Fixed by asserting
against the literal rendered markup (`b'class="badge health-degraded">Degraded'`)
instead of a bare word — a stronger, more honest test in any case, since
the loose version would have passed even if the template were broken.

**Also fixed:** a Phase A test (`test_sources_page_connector_stats_no_health_status_shown`)
asserted no health status ever appeared on the page — true when it was
written (Phase A shipped before Phase D existed), now stale now that Phase
D is real. Renamed and re-scoped it to what's actually still true: a
single clean run doesn't get flagged Degraded/Offline (too small a sample
for any rule to fire) — kept the regression coverage, dropped the
now-false premise from the comment.

## What's deliberately not done

- No change to Phase A/B/C's own tables (Connector Stats, Coverage,
  Coverage Analytics, Capabilities) — only the Sources table's Health
  column changed, per the explicit "don't break or replace" constraint.
- No per-exception-type failure tagging — `UNAVAILABLE_SIGNALS` documents
  the gap rather than half-building a fix as a Phase D side-quest, same
  practice as Phase B's `time_to_first_match`.
- Phase E (source roadmap metadata), Phase F (orchestration layer) not
  started.
