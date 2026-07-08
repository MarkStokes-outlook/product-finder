"""Search orchestration layer — schedules and executes connector search
calls, separate from what the caller does with the results.

Motivation (roadmap Phase F, "Search Aggregation Foundation"): "the watch
loop iterates connectors" doesn't scale to real scheduling, retry,
concurrency, or health-aware execution as the connector count grows. This
module is the seam future work plugs into. It deliberately does not
implement any of those *behaviours* yet — this phase preserves today's
exact sequential, single-attempt, always-run semantics (see
DefaultExecutionPolicy) — only the architecture that makes adding them
later a policy change, not a rewrite of runner.py.

Division of responsibility, kept strict:
- A connector (Source.search()) only knows how to fetch listings for one
  term. It has no idea whether it's being retried, rate-limited against
  its neighbours, or (in future) run concurrently with anything else.
- SearchOrchestrator knows *how* work is executed: which connectors are
  eligible right now, what order to run them in, how many attempts a
  failure gets and with what backoff. It has no idea what a Listing means,
  how it gets matched to a catalogue, or what an alert is — runner.py
  still owns all of that, unchanged, over whatever SearchOutcomes it
  receives.

ExecutionPolicy is the pluggable seam. DefaultExecutionPolicy reproduces
today's behaviour exactly: every candidate selected, given order
preserved, zero retries, sequential. Swapping in a different policy (for
priority ordering, disabled/maintenance-mode exclusion, health-aware
skipping, retry-with-backoff, etc. — see class docstrings below for
exactly where each plugs in) requires no change to SearchOrchestrator or
runner.py, only a new ExecutionPolicy.

Not addressed here (future, not this phase):
- Concurrent/parallel execution — ExecutionPolicy.concurrency() is
  declared so a policy can *state* its intent, but SearchOrchestrator.run()
  always executes sequentially; nothing today reads that value.
- Distributed/remote-worker execution — WorkItem/SearchOutcome are plain,
  simply-typed dataclasses on purpose (no live objects beyond the config
  types runner.py already threads everywhere), so a future queue-based
  executor could serialise them without a data-model change. No queue,
  worker protocol, or transport exists yet.
- Global/cross-connector rate-limit coordination — each connector already
  self-throttles via rate_limit.py's per-instance RateLimiter; this module
  doesn't (yet) coordinate across connectors. backoff_seconds() is the
  seam for orchestrator-level backoff between *retries* of the same
  connector, which is a different thing.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from .config import ItemConfig
from .models import Listing
from .sources.base import Source

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    """One unit of orchestrated work: search `source_name` for `term`,
    scoped to `item`. Plain data on purpose — see module docstring's
    "distributed/remote-worker execution" note."""

    source_name: str
    term: str
    item: ItemConfig


@dataclass
class SearchOutcome:
    """Result of executing one WorkItem. `error` is None on success —
    check that, not `listings`, to tell success from failure: a clean
    search with zero results is not a failure. `duration_ms` sums every
    attempt (so a retried call's reported duration is real wall-clock
    time spent, not just the last attempt's)."""

    source_name: str
    term: str
    item: ItemConfig
    listings: list[Listing] = field(default_factory=list)
    error: Exception | None = None
    duration_ms: int = 0
    attempts: int = 1


class ExecutionPolicy(ABC):
    """How work gets executed — the seam every future scheduling
    capability plugs into, without SearchOrchestrator itself changing:

    - priority / disabled / maintenance-mode / manual-only exclusion →
      select() and/or order()
    - health-aware execution (e.g. skip a connector connector_health.py
      reports Offline) → select(), using the `health` mapping it's given
    - retry policy → max_retries()
    - backoff → backoff_seconds()
    - future concurrent execution → concurrency() (declared, not yet
      consumed — see module docstring)

    high-risk opt-in connectors and manual-only connectors already have a
    home upstream of this module (sources.build_registry()'s risk gate and
    Source.is_automated() respectively) and don't need re-solving here —
    an ExecutionPolicy only ever sees names runner.py has already decided
    are automated and risk-allowed.
    """

    @abstractmethod
    def select(self, names: Sequence[str], health: Mapping[str, dict]) -> list[str]:
        """Which of the candidate connector names should run this cycle.
        `health` is whatever the caller passes — today, runner.run_once
        doesn't fetch db.source_health() at all, so it's always {} and no
        policy can act on it yet. A future caller passing real health data
        needs no change here or in SearchOrchestrator, only a policy that
        reads it."""

    @abstractmethod
    def order(self, items: Sequence[WorkItem]) -> list[WorkItem]:
        """Execution order for the work items belonging to select()'s
        chosen connectors."""

    @abstractmethod
    def max_retries(self, source_name: str) -> int:
        """Additional attempts after the first failure, for this source."""

    @abstractmethod
    def backoff_seconds(self, source_name: str, attempt: int) -> float:
        """Delay before retry number `attempt` (1-indexed: the delay
        before the *second* overall attempt is backoff_seconds(name, 1)).
        Only consulted when max_retries() makes a retry happen at all."""

    @abstractmethod
    def concurrency(self, source_name: str) -> int:
        """Declared parallelism budget for this source. Not yet consumed
        by SearchOrchestrator.run() (see module docstring) — exists now so
        a future concurrent executor can read a policy's intent without
        the policy interface changing again."""


class DefaultExecutionPolicy(ExecutionPolicy):
    """Reproduces today's behaviour exactly: every candidate selected, in
    the order given; zero retries; concurrency=1 (sequential, and
    SearchOrchestrator.run() only ever executes sequentially regardless).
    This is what makes introducing SearchOrchestrator behaviour-preserving
    — every other policy is opt-in."""

    def select(self, names: Sequence[str], health: Mapping[str, dict]) -> list[str]:
        return list(names)

    def order(self, items: Sequence[WorkItem]) -> list[WorkItem]:
        return list(items)

    def max_retries(self, source_name: str) -> int:
        return 0

    def backoff_seconds(self, source_name: str, attempt: int) -> float:
        return 0.0

    def concurrency(self, source_name: str) -> int:
        return 1


class SearchOrchestrator:
    """Executes WorkItems against a connector registry, per an
    ExecutionPolicy. See module docstring for the division of
    responsibility this preserves relative to the caller (runner.py) and
    the connectors themselves."""

    def __init__(
        self,
        registry: Mapping[str, Source],
        policy: ExecutionPolicy | None = None,
        health: Mapping[str, dict] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or DefaultExecutionPolicy()
        self.health = health or {}

    def run(self, work_items: Sequence[WorkItem]) -> Iterator[SearchOutcome]:
        """Executes work_items sequentially (concurrency is declared on
        the policy but not yet consumed — see module docstring), honouring
        policy.select() (over the distinct source names present, order-
        preserving) and policy.order() (over the resulting work items),
        with per-item retry/backoff per policy. Yields one SearchOutcome
        per surviving work item, in execution order.

        Never raises for a connector failure — the same "a source failure
        must never crash the run" guarantee runner.run_once always had. A
        retry-exhausted failure is a SearchOutcome with `error` set, not
        an exception escaping this method."""
        candidate_names = list(dict.fromkeys(w.source_name for w in work_items))
        selected = set(self.policy.select(candidate_names, self.health))
        eligible = [w for w in work_items if w.source_name in selected]
        for work_item in self.policy.order(eligible):
            yield self._execute(work_item)

    def _execute(self, work_item: WorkItem) -> SearchOutcome:
        source = self.registry[work_item.source_name]
        max_attempts = self.policy.max_retries(work_item.source_name) + 1
        total_duration_ms = 0
        last_error: Exception | None = None
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            if attempts > 1:
                delay = self.policy.backoff_seconds(work_item.source_name, attempts - 1)
                if delay:
                    time.sleep(delay)
            started = time.perf_counter()
            try:
                listings = source.search(work_item.term, work_item.item)
            except Exception as exc:
                # A connector failure must never crash the run.
                total_duration_ms += round((time.perf_counter() - started) * 1000)
                last_error = exc
                log.warning(
                    "%s search failed for %r: %s", work_item.source_name, work_item.term, exc
                )
                continue
            total_duration_ms += round((time.perf_counter() - started) * 1000)
            return SearchOutcome(
                source_name=work_item.source_name,
                term=work_item.term,
                item=work_item.item,
                listings=listings,
                error=None,
                duration_ms=total_duration_ms,
                attempts=attempts,
            )
        return SearchOutcome(
            source_name=work_item.source_name,
            term=work_item.term,
            item=work_item.item,
            listings=[],
            error=last_error,
            duration_ms=total_duration_ms,
            attempts=attempts,
        )
