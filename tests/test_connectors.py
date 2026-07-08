"""Connector-framework contract: declared capabilities, health recording,
and capability-driven behaviour in the runner (no marketplace special cases).
"""

from datetime import datetime, timedelta, timezone

import pytest

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


# --- Connector maturity: run-level stats and aggregation ---------------------------


def test_record_source_run_persists_new_stats_fields(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.record_source_run(
        conn, "s", searches=2, listings=5, duration_ms=1234,
        new_listings=3, duplicates=1, catalogue_matches=2, deals_found=1,
    )
    row = conn.execute("SELECT * FROM source_runs WHERE source = 's'").fetchone()
    assert row["duration_ms"] == 1234
    assert row["new_listings"] == 3
    assert row["duplicates"] == 1
    assert row["catalogue_matches"] == 2
    assert row["deals_found"] == 1


def test_first_seen_set_on_first_run_and_never_overwritten(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO source_settings (name, first_seen) VALUES ('s', '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    db.record_source_run(conn, "s", searches=1)
    row = conn.execute("SELECT first_seen FROM source_settings WHERE name = 's'").fetchone()
    assert row["first_seen"] == "2020-01-01T00:00:00+00:00"
    assert db.source_health(conn)["s"]["first_seen"] == "2020-01-01T00:00:00+00:00"


def test_first_seen_recorded_for_source_with_no_prior_settings_row(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.record_source_run(conn, "s", searches=1)
    row = conn.execute("SELECT first_seen FROM source_settings WHERE name = 's'").fetchone()
    assert row["first_seen"] is not None


def test_source_health_reports_success_rate_and_averages(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.record_source_run(
        conn, "s", searches=1, listings=4, duration_ms=100,
        new_listings=2, duplicates=1, catalogue_matches=1, deals_found=1,
    )
    db.record_source_run(
        conn, "s", searches=1, listings=6, errors=1, last_error="boom", duration_ms=300,
        new_listings=0, duplicates=1, catalogue_matches=1, deals_found=0,
    )
    h = db.source_health(conn)["s"]
    assert h["total_runs"] == 2
    assert h["ok_runs"] == 1
    assert h["success_rate"] == 50
    assert h["avg_duration_ms"] == 200
    assert h["avg_listings_found"] == 5.0
    assert h["avg_new_listings"] == 1.0
    assert h["avg_duplicates"] == 1.0
    assert h["avg_catalogue_matches"] == 1.0
    assert h["avg_deals_found"] == 0.5
    assert h["last_failed_at"] is not None


def test_source_health_has_no_score_or_status_field(tmp_path):
    # Phase A is raw metrics only — health scoring/status is a separate,
    # explainable model (roadmap Phase D), not decided here.
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.record_source_run(conn, "s", searches=1, listings=1)
    h = db.source_health(conn)["s"]
    assert "health_score" not in h
    assert "status" not in h


def test_run_once_records_new_listings_catalogue_matches_and_deals(tmp_path):
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="good", type="rss", url="https://x/{term}"),
    ])
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(
        conn, project_id,
        ItemConfig(name="Track Saw", terms=["track saw"], normal_price=350,
                   target_deal_price=200),
    )
    db.create_product(
        conn, item_id, "Makita", "LS1019L", ["Makita LS1019L"],
        msrp=None, typical_new_price=None, target_deal_price=200,
    )
    listing = Listing(source="good", external_id="g1", title="Makita LS1019L track saw",
                      price=150.0, url="https://x/g1")
    _run_with(cfg, conn, {"good": HealthyFake(cfg, "good", [listing])})
    run_row = conn.execute(
        "SELECT * FROM source_runs WHERE source = 'good'"
    ).fetchone()
    assert run_row["new_listings"] == 1
    assert run_row["catalogue_matches"] == 1
    assert run_row["deals_found"] == 1  # 150 <= target_deal_price 200

    # Rescanning the same listing again: no longer "new".
    _run_with(cfg, conn, {"good": HealthyFake(cfg, "good", [listing])})
    second_run_row = conn.execute(
        "SELECT * FROM source_runs WHERE source = 'good' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert second_run_row["new_listings"] == 0


def test_run_once_records_duplicates_for_secondary_cross_source_sighting(tmp_path):
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="rss", type="rss", url="https://x/{term}"),
    ])
    conn = db.connect(cfg.db_path)
    _seed_item(conn)
    url = "https://www.ebay.co.uk/itm/195012345678"
    ebay_listing = Listing(source="ebay", external_id="195012345678",
                           title="Makita track saw", price=250.0, url=url)
    rss_listing = Listing(source="rss", external_id="rss-guid-1",
                          title="Makita track saw (RSS)", price=245.0, url=url)
    _run_with(cfg, conn, {
        "ebay": HealthyFake(cfg, "ebay", [ebay_listing]),
        "rss": HealthyFake(cfg, "rss", [rss_listing]),
    })
    health = db.source_health(conn)
    # eBay is the native platform for this URL and is processed first
    # (built-in before extras) - it stays primary. The RSS proxy sighting
    # is the confirmed duplicate.
    assert health["ebay"]["avg_duplicates"] == 0.0
    assert health["rss"]["avg_duplicates"] == 1.0


# --- Connector Capability Explorer: capability_checklist() -------------------------


def test_ebay_capability_checklist_reflects_declared_fields(tmp_path):
    caps = sources.build_all(_cfg(tmp_path))["ebay"].capabilities()
    checklist = dict(caps.capability_checklist())
    # eBay is automated, so listing-shape fields are real supported/
    # unsupported claims, never "na".
    assert checklist["Auctions"] == "supported"
    assert checklist["Auction snapshots"] == "supported"
    assert checklist["Offers"] == "supported"
    assert checklist["Images"] == "supported"
    assert checklist["Seller identity"] == "unsupported"  # declared False today
    assert checklist["Official API"] == "supported"
    assert checklist["Scraping based"] == "unsupported"
    assert checklist["Requires user auth"] == "unsupported"


def test_manual_assisted_connector_reports_na_for_listing_shape_fields(tmp_path):
    caps = sources.build_all(_cfg(tmp_path))["gumtree"].capabilities()
    checklist = dict(caps.capability_checklist())
    # Gumtree only ever produces ManualLink objects (see models.ManualLink),
    # never a Listing - "does it provide images" is a category error, not a
    # false claim, so these must be "na" rather than "unsupported".
    for label in ("Images", "Auctions", "Auction snapshots", "Offers",
                  "Seller identity", "Location", "End time",
                  "Structured attributes", "Enrichment support"):
        assert checklist[label] == "na", label
    # Operating-model fields are still real declared values, not "na".
    assert checklist["Requires manual input"] == "supported"
    assert checklist["Unattended / background capable"] == "unsupported"


def test_automated_non_ebay_connector_reports_unsupported_not_na(tmp_path):
    # An automated connector (RSS) that simply doesn't declare a field is a
    # real "unsupported" claim (it does produce Listings), not "na".
    cfg = _cfg(tmp_path, extra=[
        ExtraSourceConfig(name="hukd", type="rss", url="https://example.com/rss?q={term}"),
    ])
    caps = sources.build_all(cfg)["hukd"].capabilities()
    checklist = dict(caps.capability_checklist())
    assert checklist["Images"] == "supported"
    assert checklist["Auctions"] == "unsupported"
    assert checklist["Seller identity"] == "unsupported"


def test_capability_checklist_covers_every_requested_capability_area(tmp_path):
    caps = sources.build_all(_cfg(tmp_path))["ebay"].capabilities()
    labels = {label for label, _ in caps.capability_checklist()}
    assert labels == {
        "Unattended / background capable", "Requires user auth",
        "Requires manual input", "Official API", "Indexed search",
        "Scraping based", "Third-party provider", "Images", "Auctions",
        "Auction snapshots", "Offers", "Seller identity", "Location",
        "End time", "Structured attributes", "Enrichment support",
    }


def test_capability_checklist_status_values_are_always_one_of_three(tmp_path):
    for connector in sources.build_all(_cfg(tmp_path)).values():
        for label, status in connector.capabilities().capability_checklist():
            assert status in ("supported", "unsupported", "na"), (connector.name, label)


# --- Capability-driven enrichment (no marketplace special cases) -------------------


# --- Risk / compliance model (Coverage phase) -------------------------------------


def test_all_builtin_connectors_declare_none_risk_today(tmp_path):
    # No connector in this repo is scraping-based or user-session-based
    # today - this pins that down so a future PR can't silently regress it.
    for name, connector in sources.build_all(_cfg(tmp_path)).items():
        caps = connector.capabilities()
        assert caps.account_risk == "none", name
        assert caps.is_scraping_based is False, name
        assert caps.requires_user_auth is False, name


def test_capabilities_declare_can_run_unattended_consistently_with_automated(tmp_path):
    connectors = sources.build_all(_cfg(tmp_path))
    # ebay + rss-type extras are automated and can run unattended; the two
    # manual-assisted built-ins cannot.
    assert connectors["ebay"].capabilities().can_run_unattended is True
    assert connectors["gumtree"].capabilities().can_run_unattended is False
    assert connectors["facebook"].capabilities().can_run_unattended is False


def test_invalid_account_risk_rejected():
    with pytest.raises(ValueError):
        SourceCapabilities(automated=True, compliance="x", account_risk="extreme")


def test_invalid_compliance_mode_rejected():
    with pytest.raises(ValueError):
        SourceCapabilities(automated=True, compliance="x", compliance_mode="telepathy")


def test_scraping_based_cannot_claim_low_risk():
    with pytest.raises(ValueError):
        SourceCapabilities(
            automated=True, compliance="x", is_scraping_based=True, account_risk="low",
        )


def test_scraping_based_can_claim_medium_or_high_risk():
    # This must NOT raise - it's exactly the case the risk model exists to allow.
    SourceCapabilities(
        automated=True, compliance="x", is_scraping_based=True, account_risk="medium",
        compliance_mode="scraping",
    )
    SourceCapabilities(
        automated=True, compliance="x", is_scraping_based=True, account_risk="high",
        compliance_mode="scraping",
    )


def test_requires_user_auth_cannot_claim_none_risk():
    with pytest.raises(ValueError):
        SourceCapabilities(automated=True, compliance="x", requires_user_auth=True, account_risk="none")


def test_requires_user_auth_can_claim_low_risk():
    SourceCapabilities(
        automated=True, compliance="x", requires_user_auth=True, account_risk="low",
        compliance_mode="user_session",
    )


# --- Scheduler risk gate ------------------------------------------------------------


class _RiskyFake(Source):
    def __init__(self, cfg, name, risk):
        super().__init__(cfg)
        self.name = name
        self._risk = risk

    def capabilities(self):
        return SourceCapabilities(
            automated=True, compliance="test fake", account_risk=self._risk,
            compliance_mode="scraping" if self._risk in ("medium", "high") else "manual",
            is_scraping_based=self._risk in ("medium", "high"),
        )

    def search(self, term, item):
        return []


def test_build_registry_excludes_medium_and_high_risk_by_default(tmp_path):
    # No built-in/config-defined source can declare risk today, so this
    # exercises the gate function directly against fake capabilities -
    # what build_registry() itself calls per candidate.
    cfg = _cfg(tmp_path)
    assert sources._risk_allowed(cfg, "anything", _RiskyFake(cfg, "x", "none").capabilities())
    assert sources._risk_allowed(cfg, "anything", _RiskyFake(cfg, "x", "low").capabilities())
    assert not sources._risk_allowed(cfg, "anything", _RiskyFake(cfg, "x", "medium").capabilities())
    assert not sources._risk_allowed(cfg, "anything", _RiskyFake(cfg, "x", "high").capabilities())


def test_build_registry_includes_medium_risk_when_explicitly_acknowledged(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.sources.risk_acknowledged = ["scary-source"]
    caps = _RiskyFake(cfg, "scary-source", "medium").capabilities()
    assert sources._risk_allowed(cfg, "scary-source", caps) is True
    # A *different* unacknowledged source of the same risk level is still excluded.
    assert sources._risk_allowed(cfg, "other-source", caps) is False


def test_build_registry_includes_high_risk_only_when_explicitly_acknowledged(tmp_path):
    cfg = _cfg(tmp_path)
    caps = _RiskyFake(cfg, "scary-source", "high").capabilities()
    assert sources._risk_allowed(cfg, "scary-source", caps) is False  # never silent
    cfg.sources.risk_acknowledged = ["scary-source"]
    assert sources._risk_allowed(cfg, "scary-source", caps) is True


def test_risk_acknowledged_loaded_from_config_yaml(tmp_path):
    from product_finder.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "sources:\n"
        "  risk_acknowledged:\n"
        "    - Scary-Source\n"
        "projects:\n"
        "  - name: P\n"
        "    items:\n"
        "      - name: Widget\n"
        "        terms: [widget]\n"
    )
    cfg = load_config(path)
    assert cfg.sources.risk_acknowledged == ["scary-source"]  # normalised lowercase


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
