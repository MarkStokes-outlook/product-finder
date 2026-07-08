# Search Aggregation Foundation — Phase F of the "acquisition platform" roadmap

**Date:** 2026-07-09 ~00:30
**Tests:** 654 passing (633 prior, all unmodified + 21 new in
`test_orchestrator.py`)
**Trigger:** Final phase of [[acquisition_platform_roadmap]] — Phases
A-E shipped earlier the same session. Phase F, scoped by Mark as: an
orchestration layer responsible for scheduling, connector selection,
execution ordering, concurrency, retry policy, backoff, health-aware
execution, and rate-limit awareness — but explicitly *architectural seams
only*, no new behaviour, no concurrency actually implemented yet, no
connector changes, "preserve existing behaviour" and "preserve
determinism" as hard constraints.

## The core design decision

Mark's own framing was the spec: *"The orchestrator should know how work
is executed. Connectors should only know how to search their source. Keep
those responsibilities separate."* Read this as ruling out a third,
tempting option — folding scheduling logic into `runner.py` alongside the
domain processing (catalogue matching, persistence, alerting) it already
does. Kept three responsibilities in three places with no overlap:

- `Source.search()` — unchanged. Still only knows how to fetch listings
  for one term. Doesn't know it's being retried, timed, or (later)
  parallelised.
- `orchestrator.py` (new) — knows *how* work is executed: which
  connectors run, in what order, how many attempts, what backoff. Has
  zero knowledge of Listings, catalogue matching, or alerts.
- `runner.py` — unchanged in what it *does* (identical stats accounting,
  identical per-listing processing), changed only in *how it gets its
  input* (SearchOutcomes from an orchestrator, not raw `search()` calls it
  makes itself).

## Why `ExecutionPolicy` and not, e.g., a bag of parameters

Every "future capability" in Mark's brief (priority connectors, disabled
connectors, maintenance mode, health-aware execution, retry, backoff,
future concurrency) reduces to "the orchestrator asks a policy a
question, gets an answer, acts on it" — so `ExecutionPolicy` is a small
ABC (5 abstract methods: `select`, `order`, `max_retries`,
`backoff_seconds`, `concurrency`), matching this codebase's existing
convention (`Source` is an ABC too) rather than introducing
`typing.Protocol`, which isn't used anywhere else in the project.
`DefaultExecutionPolicy` implements all five as the identity/no-op/
single-attempt/sequential answer — this is what makes wiring the
orchestrator into `runner.py` a behaviour-preserving refactor rather than
a behaviour change: every other policy is opt-in, and nothing opts in yet.

**Two of the five hooks are honestly still just declarations**, not real
mechanisms:

- `select(names, health)` *can* filter connectors based on `health`
  today — the hook genuinely works and is tested — but `runner.run_once()`
  passes `health={}` (it doesn't call `db.source_health()` at all), so no
  policy can act on real data yet without a `runner.py` change. This was
  a deliberate choice to keep this phase from quietly adding a new
  per-cycle DB query for a feature nobody asked to actually turn on yet.
- `concurrency(source_name)` is declared on the interface and callable,
  but `SearchOrchestrator.run()` never reads it — execution is always
  strictly sequential. Building a real concurrent executor now would have
  meant reasoning about thread-safety of the shared `sqlite3.Connection`
  the runner's per-listing processing writes to inline as it consumes
  results — explicitly out of scope ("do not implement those features
  yet"), and risky to half-build without real test coverage of concurrent
  behaviour. The seam exists so a future concurrent executor can read a
  policy's stated intent without another interface change.

## Retry/backoff *is* a real, working mechanism (just defaulted off)

Unlike selection and concurrency, retry was worth actually implementing
end-to-end rather than only declaring — it's a self-contained loop inside
`SearchOrchestrator._execute()` with no DB/threading implications, and
"preserve existing behaviour" is trivially satisfied by
`DefaultExecutionPolicy.max_retries() == 0` (loop runs exactly once,
identical to today). Verified with real tests, not just a signature: a
fake connector that fails N times then succeeds
(`test_retries_on_failure_then_succeeds`), a policy that exhausts retries
and reports the last error (`test_gives_up_after_max_retries_exhausted`),
and that `backoff_seconds()` is consulted before each retry but never
before the first attempt (`test_backoff_seconds_consulted_between_retries_not_before_first_attempt`,
using `unittest.mock.patch` on `time.sleep` so the test suite doesn't
actually sleep).

## The runner.py refactor: how "no behavioural change" was actually verified

Not asserted — checked. The refactor moves the `for term in item.terms: ...
try: source.search(...) except: ...` block out of `runner.run_once()` and
into `SearchOrchestrator._execute()`, keeping every other structural
detail identical: `products` is still fetched once per item (not
per-search), the nested project→item→source→term order is preserved
exactly (work items are built via the same nested comprehension order,
and `DefaultExecutionPolicy.select()`/`order()` are both order-preserving
identity operations), and every stat (`searches`, `listings`, `errors`,
`duration_ms`, `new_listings`, `duplicates`, `catalogue_matches`,
`deals_found`) is computed from the same signals in the same places, just
sourced from a `SearchOutcome` instead of a local variable.

The actual evidence: **all 633 pre-existing tests, several of which
exercise this exact code path in detail (`test_connectors.py`'s health-
recording tests, `test_identity.py`'s cross-source duplicate tests,
`test_suggestions.py`'s enrichment tests, `test_price_history.py`'s
observation tests, `test_duplicates.py`, `test_catalogue_tidy.py`,
`test_locking.py`'s transaction-boundary test), pass completely
unmodified** — not one assertion needed changing. That's stronger
evidence than manual reasoning about the diff, since several of those
tests pin exact stats values, exact alert ordering, and exact DB row
counts that a subtle behavioural regression would have broken silently.

## The watch loop: verified, not assumed, to need no changes

Checked `cli.py` before claiming "the watch loop is now an orchestrator
client" — `cmd_watch()` only ever calls `runner.run_once(cfg, conn)`; the
only other `sources.build_registry()` call in the file is in
`_print_run_summary()`, a read-only post-run reporting helper (which
automated sources exist, how many manual links were generated) that
doesn't execute any search and was already unrelated to this phase's
scope. The watch loop never directly touched connectors before this
change and doesn't need to now — it was already only a client of
`runner.run_once()`, which is what makes it transitively an orchestrator
client with zero lines of `cli.py` changed.

## What's deliberately not done

- No actual concurrent, distributed, or remote-worker execution —
  `concurrency()` is declared, `WorkItem`/`SearchOutcome` are plain,
  simply-typed dataclasses (no live objects) specifically so a future
  queue-based executor *could* serialise them, but no queue, worker
  protocol, or transport exists.
- No cross-connector/global rate-limit coordination — each connector
  still self-throttles via its own `rate_limit.RateLimiter` instance,
  unchanged; `backoff_seconds()` is about delay between *retries of the
  same connector*, a different concern.
- `runner.run_once()` doesn't fetch `db.source_health()` or pass real
  health data to the orchestrator — the `select()` hook can use it, but
  nothing supplies it yet, so no policy today has anything to act on.
- `collect_manual_links()` (manual-assisted connectors' link generation)
  was not routed through the orchestrator — it's a fast, local,
  non-network, non-failure-prone call; the orchestration concerns
  (retry/backoff/concurrency/rate-limit/health-aware execution) that
  motivate this phase don't apply to it.
- This closes the six-phase "acquisition platform" roadmap
  ([[acquisition_platform_roadmap]]) opened at the start of this session.
  Any future connector-count growth work should start from
  `ExecutionPolicy` rather than re-adding ad-hoc scheduling logic to
  `runner.py`.
