"""Coverage metrics (db.source_coverage): per-source ingest, freshness,
catalogue match rate, duplicate suppression, and price-history contribution —
the roadmap's "coverage should become measurable" layer, and its rendering
on the Sources page.
"""

from datetime import datetime, timedelta, timezone

import pytest

from product_finder import db
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Evaluation, Listing
from product_finder.web.app import create_app


def _iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


def _seed_item(conn):
    project_id = db.create_project(conn, "Workshop")
    return db.create_item(
        conn, project_id,
        ItemConfig(name="Track Saw", terms=["track saw"], normal_price=500,
                   target_deal_price=300),
    )


def _add_listing(conn, source, external_id, *, first_seen=None, last_seen=None,
                 end_time=None, primary=True):
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(source=source, external_id=external_id,
                title=f"Listing {external_id}", price=100.0,
                url=f"https://example.com/{external_id}", end_time=end_time),
    )
    conn.execute(
        "UPDATE listings SET first_seen = ?, last_seen = ?, is_primary_sighting = ? "
        "WHERE id = ?",
        (first_seen or _iso(hours=1), last_seen or _iso(hours=1),
         1 if primary else 0, listing_id),
    )
    return listing_id


def _add_match(conn, listing_id, item_id, product_id=None):
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="A", flags=[], margin_abs=400.0, margin_pct=80.0,
                   under_target=True, deal_score=60.0),
        product_id=product_id,
    )


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "t.db"))


def test_empty_db_has_no_coverage(conn):
    assert db.source_coverage(conn) == {}


def test_listing_counts_live_new_and_stale(conn):
    _seed_item(conn)
    # Fresh, no end_time: live, new in both windows.
    _add_listing(conn, "ebay", "fresh", first_seen=_iso(hours=2), last_seen=_iso(hours=1))
    # Seen 3 days ago, still rescanned recently: not new in 24h, new in 7d.
    _add_listing(conn, "ebay", "older", first_seen=_iso(days=3), last_seen=_iso(hours=1))
    # Ended auction: not live, not stale (its absence is explained).
    _add_listing(conn, "ebay", "ended", first_seen=_iso(days=2),
                 last_seen=_iso(days=1), end_time=_iso(days=1))
    # No end_time and not seen for 3 days: live by the read-path rule, but stale.
    _add_listing(conn, "ebay", "lingering", first_seen=_iso(days=6), last_seen=_iso(days=3))

    cov = db.source_coverage(conn)["ebay"]
    assert cov["listings_total"] == 4
    assert cov["listings_live"] == 3  # ended one excluded
    assert cov["new_24h"] == 1
    assert cov["new_7d"] == 4
    assert cov["stale"] == 1  # only "lingering": no end_time, unseen 48h+


def test_sources_are_isolated_from_each_other(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "e1")
    _add_listing(conn, "hardwareswapuk", "h1")
    _add_listing(conn, "hardwareswapuk", "h2")
    cov = db.source_coverage(conn)
    assert cov["ebay"]["listings_total"] == 1
    assert cov["hardwareswapuk"]["listings_total"] == 2


def test_catalogue_match_rate_per_source(conn):
    item_id = _seed_item(conn)
    a = _add_listing(conn, "ebay", "a")
    b = _add_listing(conn, "ebay", "b")
    c = _add_listing(conn, "ebay", "c")
    d = _add_listing(conn, "rssfeed", "d")
    _add_match(conn, a, item_id, product_id=1)
    _add_match(conn, b, item_id, product_id=1)
    _add_match(conn, c, item_id)  # matched the item, no catalogue product
    _add_match(conn, d, item_id)

    cov = db.source_coverage(conn)
    assert cov["ebay"]["matches_total"] == 3
    assert cov["ebay"]["matches_catalogued"] == 2
    assert cov["ebay"]["catalogue_match_pct"] == 67
    assert cov["rssfeed"]["matches_total"] == 1
    assert cov["rssfeed"]["catalogue_match_pct"] == 0


def test_match_pct_none_when_source_has_listings_but_no_matches(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "unmatched")
    cov = db.source_coverage(conn)["ebay"]
    assert cov["matches_total"] == 0
    assert cov["catalogue_match_pct"] is None


def test_hidden_duplicates_counted(conn):
    _seed_item(conn)
    _add_listing(conn, "rssfeed", "r1", primary=False)  # suppressed by identity
    _add_listing(conn, "rssfeed", "r2")
    cov = db.source_coverage(conn)["rssfeed"]
    assert cov["hidden_duplicates"] == 1
    assert cov["listings_total"] == 2


def test_price_observations_windowed_to_30_days(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "e1")
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (1, 100, 'ebay', ?), (1, 110, 'ebay', ?), (1, 90, 'ebay', ?)",
        (_iso(days=1), _iso(days=10), _iso(days=45)),
    )
    cov = db.source_coverage(conn)
    assert cov["ebay"]["price_observations_30d"] == 2  # 45-day-old one excluded


def test_price_observations_from_source_with_no_listing_rows(conn):
    # Observation sources shouldn't KeyError even if no listings remain for
    # that source name — they get their own coverage entry.
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (1, 100, 'auction-close', ?)",
        (_iso(days=1),),
    )
    cov = db.source_coverage(conn)
    assert cov["auction-close"]["price_observations_30d"] == 1
    assert cov["auction-close"]["listings_total"] == 0


# --- Sources page rendering ---------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(db_path=str(tmp_path / "t.db"))


@pytest.fixture
def client(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def test_sources_page_renders_coverage_table(cfg, client):
    conn = db.connect(cfg.db_path)
    item_id = _seed_item(conn)
    listing_id = _add_listing(conn, "ebay", "e1")
    _add_match(conn, listing_id, item_id, product_id=1)
    conn.commit()
    conn.close()
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert b"Coverage" in resp.data
    assert b"1 of 1" in resp.data  # catalogue match count
    assert b"(100%)" in resp.data


def test_sources_page_coverage_empty_state(cfg, client):
    resp = client.get("/sources")
    # Automated-class connectors appear with a quiet empty state; manual-
    # assisted ones (gumtree/facebook) never ingest, so no row at all.
    assert b"no listings yet" in resp.data


# --- Connector Stats table (roadmap Phase A: connector maturity) --------------


def test_sources_page_renders_connector_stats_table(cfg, client):
    conn = db.connect(cfg.db_path)
    db.record_source_run(
        conn, "ebay", searches=1, listings=4, duration_ms=250,
        new_listings=2, duplicates=1, catalogue_matches=1, deals_found=1,
    )
    conn.close()
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert b"Connector Stats" in resp.data
    assert b"100%" in resp.data  # success rate, single clean run
    assert b"250ms" in resp.data


def test_sources_page_connector_stats_empty_state_for_unrun_source(cfg, client):
    resp = client.get("/sources")
    assert b"not yet run" in resp.data


def test_sources_page_renders_coverage_analytics_table(cfg, client):
    conn = db.connect(cfg.db_path)
    item_id = _seed_item(conn)
    listing_id = _add_listing(conn, "ebay", "a")
    _add_match(conn, listing_id, item_id, product_id=1)  # under_target=True by default
    conn.close()
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert b"Coverage Analytics" in resp.data
    assert b"100%" in resp.data  # deal rate: the one primary listing was a deal


def test_sources_page_explains_time_to_first_match_unavailable(cfg, client):
    conn = db.connect(cfg.db_path)
    item_id = _seed_item(conn)
    listing_id = _add_listing(conn, "ebay", "a")
    _add_match(conn, listing_id, item_id, product_id=1)
    conn.close()
    resp = client.get("/sources")
    assert b"not tracked" in resp.data.lower()
    assert db.TIME_TO_FIRST_MATCH_UNAVAILABLE.encode() in resp.data


def test_sources_page_coverage_analytics_empty_state(cfg, client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert b"Coverage Analytics" in resp.data
    assert b"no listings yet" in resp.data


def test_sources_page_connector_stats_no_health_status_shown(cfg, client):
    # Phase A ships raw metrics only; the Healthy/Warning/Degraded/Offline
    # status model is Phase D. The hint text may explain that in prose, but
    # no connector should be tagged with a status value yet.
    conn = db.connect(cfg.db_path)
    db.record_source_run(conn, "ebay", searches=1, listings=1)
    conn.close()
    resp = client.get("/sources")
    for term in (b"Degraded", b"Offline"):
        assert term not in resp.data
