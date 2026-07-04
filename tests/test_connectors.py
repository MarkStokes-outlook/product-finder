"""Connector-framework contract: declared capabilities, health recording,
and capability-driven behaviour in the runner (no marketplace special cases).
"""

from datetime import datetime, timedelta, timezone

from product_finder import db, runner, sources
from product_finder.config import AppConfig, EbayConfig, ExtraSourceConfig, ItemConfig, SourcesConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities


def _cfg(tmp_path, extra=None):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    if extra:
        cfg.sources.extra = extra
    return cfg


# --- Capability declaration -------------------------------------------------------


def test_every_connector_declares_capabilities(tmp_path):
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="hukd", type="rss", url="https://example.com/rss?q={term}"),
        ExtraSourceConfig(name="johnpye", type="links", url="https://example.com/?s={term}"),
    ])
    connectors = sources.build_all(cfg)
    assert set(connectors) == {"ebay", "gumtree", "facebook", "hukd", "johnpye"}
    for name, connector in connectors.items():
        caps = connector.capabilities()
        assert isinstance(caps, SourceCapabilities), name
        # Compliance basis is mandatory prose, not an empty string — every
        # integration states what legitimately allows it to exist.
        assert caps.compliance.strip(), name


def test_manual_assisted_connectors_are_never_automated(tmp_path):
    connectors = sources.build_all(_cfg(tmp_path))
    for name in ("gumtree", "facebook"):
        assert connectors[name].capabilities().automated is False
        assert connectors[name].is_automated() is False


def test_ebay_automated_capability_vs_credential_readiness(tmp_path):
    # Declared class (automated connector) is static; operational readiness
    # depends on credentials being configured.
    without_keys = sources.build_all(_cfg(tmp_path))["ebay"]
    assert without_keys.capabilities().automated is True
    assert without_keys.is_automated() is False

    cfg = _cfg(tmp_path)
    cfg.sources = SourcesConfig(ebay=EbayConfig(app_id="id", cert_id="secret"))
    with_keys = sources.build_all(cfg)["ebay"]
    assert with_keys.is_automated() is True


# --- Health recording -------------------------------------------------------------


class HealthyFake(Source):
    def __init__(self, cfg, name, listings):
        super().__init__(cfg)
        self.name = name
        self._listings = listings

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        return self._listings


class FailingFake(HealthyFake):
    def search(self, term, item):
        raise RuntimeError("boom 429")


def _seed_item(conn):
    project_id = db.create_project(conn, "Workshop")
    db.create_item(
        conn, project_id,
        ItemConfig(name="Track Saw", terms=["track saw"], normal_price=350,
                   target_deal_price=200),
    )


def _run_with(cfg, conn, registry):
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: registry
    try:
        return runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig


def test_run_once_records_health_for_success_and_failure(tmp_path):
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="good", type="rss", url="https://x/{term}"),
        ExtraSourceConfig(name="bad", type="rss", url="https://x/{term}"),
    ])
    conn = db.connect(cfg.db_path)
    _seed_item(conn)
    listing = Listing(source="good", external_id="g1", title="Makita track saw",
                      price=180.0, url="https://x/g1")
    _run_with(cfg, conn, {
        "good": HealthyFake(cfg, "good", [listing]),
        "bad": FailingFake(cfg, "bad", []),
    })
    health = db.source_health(conn)
    assert health["good"]["last_ok"] is True
    assert health["good"]["consecutive_failures"] == 0
    assert health["good"]["listings_24h"] == 1
    assert health["bad"]["last_ok"] is False
    assert health["bad"]["consecutive_failures"] == 1
    assert "boom 429" in health["bad"]["last_error"]


def test_consecutive_failures_reset_by_a_clean_run(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.record_source_run(conn, "s", searches=1, errors=1, last_error="x")
    db.record_source_run(conn, "s", searches=1, errors=1, last_error="y")
    assert db.source_health(conn)["s"]["consecutive_failures"] == 2
    db.record_source_run(conn, "s", searches=1, listings=5)
    h = db.source_health(conn)["s"]
    assert h["consecutive_failures"] == 0
    assert h["last_ok"] is True
    assert h["last_success_at"] is not None


def test_source_runs_pruned_beyond_retention(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    ancient = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO source_runs (source, run_at, ok) VALUES ('s', ?, 1)", (ancient,)
    )
    db.record_source_run(conn, "s", searches=1)
    rows = conn.execute("SELECT COUNT(*) AS n FROM source_runs").fetchone()
    assert rows["n"] == 1  # the 40-day-old row is gone


# --- Capability-driven enrichment (no marketplace special cases) -------------------


def test_enrichment_not_attempted_for_connector_without_capability(tmp_path):
    # A connector that doesn't declare supports_enrichment never gets a
    # get_item_details() call — brand_checked stays untouched, so if the
    # connector later gains enrichment the listing is still eligible.
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="plainrss", type="rss", url="https://x/{term}")
    ])
    conn = db.connect(cfg.db_path)
    _seed_item(conn)
    listing = Listing(source="plainrss", external_id="r1", title="Unbranded track saw",
                      price=100.0, url="https://x/r1")
    _run_with(cfg, conn, {"plainrss": HealthyFake(cfg, "plainrss", [listing])})
    row = conn.execute("SELECT brand_checked FROM listings WHERE external_id='r1'").fetchone()
    assert row["brand_checked"] == 0
