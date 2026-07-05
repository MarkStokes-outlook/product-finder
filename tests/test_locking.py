"""Writer-lock hygiene: the watch loop must never hold an uncommitted write
(= the WAL writer lock) across a network call. Uncommitted writes before a
rate-limit backoff sleep were blocking the web UI's own writes for minutes —
"database is locked" 500s while watch waited out a 429.

The invariant tested here: every db.py write function commits before
returning, so conn.in_transaction is False at every point where the runner
performs network I/O (source.search, get_item_details, alert webhooks).
"""

from product_finder import db, runner, sources
from product_finder.config import AppConfig, ExtraSourceConfig, ItemConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities


class BoundaryFake(Source):
    """Records whether a transaction was open at each network entry point."""

    def __init__(self, cfg, name, conn, listings, boundaries, enrich=False):
        super().__init__(cfg)
        self.name = name
        self._conn = conn
        self._listings = listings
        self._boundaries = boundaries
        self._enrich = enrich

    def capabilities(self):
        return SourceCapabilities(
            automated=True, compliance="test fake", supports_enrichment=self._enrich
        )

    def search(self, term, item):
        self._boundaries.append((f"{self.name}.search", self._conn.in_transaction))
        return self._listings

    def get_item_details(self, external_id):
        self._boundaries.append((f"{self.name}.details", self._conn.in_transaction))
        return None


def test_no_open_transaction_at_any_network_boundary(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    cfg.sources.extra = [
        ExtraSourceConfig(name="first", type="rss", url="https://x/{term}"),
        ExtraSourceConfig(name="second", type="rss", url="https://x/{term}"),
    ]
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    db.create_item(conn, project_id, ItemConfig(name="Track Saw", terms=["track saw"],
                                                normal_price=350, target_deal_price=200))

    boundaries: list[tuple[str, bool]] = []
    # "first" returns a listing (which triggers upsert/match/enrichment
    # writes); "second" is searched immediately after those writes — the
    # exact window where the lock used to be held through backoff sleeps.
    listing = Listing(source="first", external_id="F1", title="Makita track saw",
                      price=180.0, url="https://x/f1")
    registry = {
        "first": BoundaryFake(cfg, "first", conn, [listing], boundaries, enrich=True),
        "second": BoundaryFake(cfg, "second", conn, [], boundaries),
    }
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: registry
    try:
        runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig

    assert [name for name, _ in boundaries] == [
        "first.search", "first.details", "second.search",
    ]
    open_at = [name for name, in_txn in boundaries if in_txn]
    assert open_at == [], f"writer lock held entering network call(s): {open_at}"
    # And the cycle must end fully committed too.
    assert conn.in_transaction is False


def test_write_helpers_commit_before_returning(tmp_path):
    # The three historical stragglers (upsert_listing, record_match,
    # record_source_run) plus mark_alerted, pinned individually so a future
    # "optimisation" can't quietly reintroduce the lock-across-network bug.
    from product_finder.models import Evaluation

    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(name="Saw", terms=["saw"]))

    listing_id, _ = db.upsert_listing(conn, Listing(
        source="s", external_id="e1", title="Saw", price=10.0, url="https://x/1"))
    assert conn.in_transaction is False  # insert path

    db.upsert_listing(conn, Listing(
        source="s", external_id="e1", title="Saw", price=11.0, url="https://x/1"))
    assert conn.in_transaction is False  # update path

    match_id, _ = db.record_match(conn, listing_id, item_id, Evaluation(
        grade="A", flags=[], margin_abs=1.0, margin_pct=1.0,
        under_target=False, deal_score=50.0))
    assert conn.in_transaction is False  # insert path

    db.record_match(conn, listing_id, item_id, Evaluation(
        grade="B", flags=[], margin_abs=1.0, margin_pct=1.0,
        under_target=False, deal_score=40.0))
    assert conn.in_transaction is False  # update path

    db.record_source_run(conn, "s", searches=1, listings=1)
    assert conn.in_transaction is False

    assert db.mark_alerted(conn, match_id, "console") is True
    assert conn.in_transaction is False
