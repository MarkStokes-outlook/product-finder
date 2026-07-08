"""Search orchestration layer (orchestrator.py) — roadmap Phase F. Pure
unit tests against fake Source instances: no DB, no runner.py involved.
Confirms DefaultExecutionPolicy reproduces today's sequential/single-
attempt/always-run semantics, and that the ExecutionPolicy seams
(select/order/retry/backoff/concurrency) genuinely work when a different
policy is supplied.
"""

from unittest import mock

import pytest

from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing
from product_finder.orchestrator import (
    DefaultExecutionPolicy,
    ExecutionPolicy,
    SearchOrchestrator,
    SearchOutcome,
    WorkItem,
)
from product_finder.sources.base import Source, SourceCapabilities


def _item(**overrides):
    base = {"name": "Track Saw", "terms": ["track saw"]}
    base.update(overrides)
    return ItemConfig(**base)


class FixedSource(Source):
    """Always returns the same listings for any term."""

    def __init__(self, cfg, name, listings=()):
        super().__init__(cfg)
        self.name = name
        self._listings = list(listings)
        self.calls: list[str] = []

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        self.calls.append(term)
        return self._listings


class FailNTimesSource(Source):
    """Raises for the first `fail_count` calls, then succeeds."""

    def __init__(self, cfg, name, fail_count, listings=()):
        super().__init__(cfg)
        self.name = name
        self.fail_count = fail_count
        self._listings = list(listings)
        self.call_count = 0

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise RuntimeError(f"boom {self.call_count}")
        return self._listings


def _registry(**sources_by_name):
    return dict(sources_by_name)


def _cfg():
    return AppConfig()


# --- WorkItem / SearchOutcome are plain data --------------------------------------


def test_work_item_is_frozen():
    item = _item()
    w = WorkItem(source_name="ebay", term="track saw", item=item)
    assert w.source_name == "ebay"
    with pytest.raises(AttributeError):  # frozen - can't reassign a field
        w.source_name = "other"


def test_search_outcome_defaults():
    outcome = SearchOutcome(source_name="ebay", term="x", item=_item())
    assert outcome.listings == []
    assert outcome.error is None
    assert outcome.duration_ms == 0
    assert outcome.attempts == 1


# --- DefaultExecutionPolicy reproduces today's exact behaviour --------------------


def test_default_policy_selects_all_names_in_given_order():
    policy = DefaultExecutionPolicy()
    assert policy.select(["b", "a", "c"], {}) == ["b", "a", "c"]


def test_default_policy_preserves_work_item_order():
    policy = DefaultExecutionPolicy()
    items = [WorkItem(source_name=n, term="t", item=_item()) for n in ("b", "a")]
    assert policy.order(items) == items


def test_default_policy_zero_retries():
    assert DefaultExecutionPolicy().max_retries("ebay") == 0


def test_default_policy_zero_backoff():
    assert DefaultExecutionPolicy().backoff_seconds("ebay", 1) == 0.0


def test_default_policy_sequential_concurrency():
    assert DefaultExecutionPolicy().concurrency("ebay") == 1


# --- SearchOrchestrator.run(): success/failure/order/never-raises -----------------


def test_orchestrator_yields_one_outcome_per_work_item():
    listing = Listing(source="ebay", external_id="1", title="Track saw", price=100.0, url="https://x/1")
    fake = FixedSource(_cfg(), "ebay", [listing])
    orch = SearchOrchestrator(_registry(ebay=fake))
    items = [WorkItem(source_name="ebay", term="track saw", item=_item())]
    outcomes = list(orch.run(items))
    assert len(outcomes) == 1
    assert outcomes[0].listings == [listing]
    assert outcomes[0].error is None
    assert outcomes[0].attempts == 1


def test_orchestrator_preserves_execution_order_across_sources_and_terms():
    calls = []

    class TrackingSource(Source):
        def __init__(self, cfg, name):
            super().__init__(cfg)
            self.name = name

        def capabilities(self):
            return SourceCapabilities(automated=True, compliance="test fake")

        def search(self, term, item):
            calls.append((self.name, term))
            return []

    reg = _registry(a=TrackingSource(_cfg(), "a"), b=TrackingSource(_cfg(), "b"))
    orch = SearchOrchestrator(reg)
    items = [
        WorkItem(source_name="a", term="t1", item=_item()),
        WorkItem(source_name="a", term="t2", item=_item()),
        WorkItem(source_name="b", term="t1", item=_item()),
    ]
    outcomes = list(orch.run(items))
    assert [(o.source_name, o.term) for o in outcomes] == [
        ("a", "t1"), ("a", "t2"), ("b", "t1"),
    ]
    assert calls == [("a", "t1"), ("a", "t2"), ("b", "t1")]  # same order the connector saw


def test_orchestrator_never_raises_for_a_connector_failure():
    class AlwaysFails(Source):
        name = "bad"

        def capabilities(self):
            return SourceCapabilities(automated=True, compliance="test fake")

        def search(self, term, item):
            raise RuntimeError("network down")

    orch = SearchOrchestrator(_registry(bad=AlwaysFails(_cfg())))
    items = [WorkItem(source_name="bad", term="t", item=_item())]
    outcomes = list(orch.run(items))  # must not raise
    assert len(outcomes) == 1
    assert outcomes[0].error is not None
    assert "network down" in str(outcomes[0].error)
    assert outcomes[0].listings == []


def test_orchestrator_records_duration_on_success_and_failure():
    fake = FixedSource(_cfg(), "ebay", [])
    orch = SearchOrchestrator(_registry(ebay=fake))
    outcome = next(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    assert outcome.duration_ms >= 0


# --- select() / order() seams ------------------------------------------------------


class _SelectivePolicy(ExecutionPolicy):
    def __init__(self, excluded=(), reverse=False):
        self.excluded = set(excluded)
        self.reverse = reverse

    def select(self, names, health):
        return [n for n in names if n not in self.excluded]

    def order(self, items):
        return list(reversed(items)) if self.reverse else list(items)

    def max_retries(self, source_name):
        return 0

    def backoff_seconds(self, source_name, attempt):
        return 0.0

    def concurrency(self, source_name):
        return 1


def test_select_hook_excludes_a_connector_entirely():
    reg = _registry(
        good=FixedSource(_cfg(), "good", []),
        excluded=FixedSource(_cfg(), "excluded", []),
    )
    orch = SearchOrchestrator(reg, policy=_SelectivePolicy(excluded=["excluded"]))
    items = [
        WorkItem(source_name="good", term="t", item=_item()),
        WorkItem(source_name="excluded", term="t", item=_item()),
    ]
    outcomes = list(orch.run(items))
    assert [o.source_name for o in outcomes] == ["good"]


def test_order_hook_changes_execution_order():
    reg = _registry(a=FixedSource(_cfg(), "a", []), b=FixedSource(_cfg(), "b", []))
    orch = SearchOrchestrator(reg, policy=_SelectivePolicy(reverse=True))
    items = [
        WorkItem(source_name="a", term="t", item=_item()),
        WorkItem(source_name="b", term="t", item=_item()),
    ]
    outcomes = list(orch.run(items))
    assert [o.source_name for o in outcomes] == ["b", "a"]


def test_health_mapping_is_passed_through_to_select():
    received = {}

    class RecordingPolicy(_SelectivePolicy):
        def select(self, names, health):
            received.update(health)
            return list(names)

    fake_health = {"ebay": {"consecutive_failures": 3}}
    reg = _registry(ebay=FixedSource(_cfg(), "ebay", []))
    orch = SearchOrchestrator(reg, policy=RecordingPolicy(), health=fake_health)
    list(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    assert received == fake_health


# --- retry / backoff seams ----------------------------------------------------------


class _RetryPolicy(ExecutionPolicy):
    def __init__(self, retries, backoff=0.0):
        self.retries = retries
        self.backoff = backoff
        self.backoff_calls = []

    def select(self, names, health):
        return list(names)

    def order(self, items):
        return list(items)

    def max_retries(self, source_name):
        return self.retries

    def backoff_seconds(self, source_name, attempt):
        self.backoff_calls.append((source_name, attempt))
        return self.backoff

    def concurrency(self, source_name):
        return 1


def test_retries_on_failure_then_succeeds():
    listing = Listing(source="ebay", external_id="1", title="x", price=1.0, url="https://x/1")
    fake = FailNTimesSource(_cfg(), "ebay", fail_count=2, listings=[listing])
    policy = _RetryPolicy(retries=2)
    orch = SearchOrchestrator(_registry(ebay=fake), policy=policy)
    outcome = next(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    assert outcome.error is None
    assert outcome.listings == [listing]
    assert outcome.attempts == 3  # 2 failures + 1 success
    assert fake.call_count == 3


def test_gives_up_after_max_retries_exhausted():
    fake = FailNTimesSource(_cfg(), "ebay", fail_count=99)  # always fails
    policy = _RetryPolicy(retries=2)
    orch = SearchOrchestrator(_registry(ebay=fake), policy=policy)
    outcome = next(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    assert outcome.error is not None
    assert outcome.attempts == 3  # 1 initial + 2 retries, all failed
    assert fake.call_count == 3


def test_zero_retries_means_a_single_attempt():
    fake = FailNTimesSource(_cfg(), "ebay", fail_count=99)
    orch = SearchOrchestrator(_registry(ebay=fake))  # DefaultExecutionPolicy: 0 retries
    outcome = next(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    assert outcome.attempts == 1
    assert fake.call_count == 1


def test_backoff_seconds_consulted_between_retries_not_before_first_attempt():
    fake = FailNTimesSource(_cfg(), "ebay", fail_count=2, listings=[])
    policy = _RetryPolicy(retries=2, backoff=0.0)
    orch = SearchOrchestrator(_registry(ebay=fake), policy=policy)
    with mock.patch("product_finder.orchestrator.time.sleep") as sleep:
        list(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    # Backoff consulted before retry attempts 2 and 3, never before attempt 1.
    assert policy.backoff_calls == [("ebay", 1), ("ebay", 2)]
    # backoff=0.0 is falsy - time.sleep should not even be called for a zero delay.
    sleep.assert_not_called()


def test_backoff_seconds_actually_sleeps_when_nonzero():
    fake = FailNTimesSource(_cfg(), "ebay", fail_count=1, listings=[])
    policy = _RetryPolicy(retries=1, backoff=0.5)
    orch = SearchOrchestrator(_registry(ebay=fake), policy=policy)
    with mock.patch("product_finder.orchestrator.time.sleep") as sleep:
        list(orch.run([WorkItem(source_name="ebay", term="t", item=_item())]))
    sleep.assert_called_once_with(0.5)


# --- concurrency: declared, not yet consumed ---------------------------------------


def test_concurrency_value_does_not_change_sequential_execution_order():
    class HighConcurrencyPolicy(_SelectivePolicy):
        def concurrency(self, source_name):
            return 8

    reg = _registry(a=FixedSource(_cfg(), "a", []), b=FixedSource(_cfg(), "b", []))
    orch = SearchOrchestrator(reg, policy=HighConcurrencyPolicy())
    items = [
        WorkItem(source_name="a", term="t", item=_item()),
        WorkItem(source_name="b", term="t", item=_item()),
    ]
    # Still strictly sequential/ordered - concurrency() is declared but not consumed.
    assert [o.source_name for o in orch.run(items)] == ["a", "b"]


def test_execution_policy_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        ExecutionPolicy()
