"""Coverage Analytics (db.source_coverage_analytics) — Phase B of the
"acquisition platform" roadmap: rate-based metrics ("which source actually
finds useful deals") layered on top of the existing source_coverage() raw
counts. Sources-page rendering of this data is tested in test_coverage.py,
alongside the existing Sources/Coverage/Connector Stats table tests.
"""

from datetime import datetime, timedelta, timezone

import pytest

from product_finder import db
from product_finder.config import ItemConfig
from product_finder.models import Evaluation, Listing


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


def _add_match(conn, listing_id, item_id, product_id=None, under_target=True):
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="A", flags=[], margin_abs=400.0, margin_pct=80.0,
                   under_target=under_target, deal_score=60.0),
        product_id=product_id,
    )


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "t.db"))


# --- total_sightings / unique_listings / duplicate_suppression_pct ------------


def test_empty_db_has_no_analytics(conn):
    assert db.source_coverage_analytics(conn) == {}


def test_total_sightings_and_unique_listings(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "e1")
    _add_listing(conn, "ebay", "e2")
    _add_listing(conn, "ebay", "e3", primary=False)  # suppressed cross-source dup
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["total_sightings"] == 3
    assert a["unique_listings"] == 2


def test_duplicate_suppression_pct(conn):
    _seed_item(conn)
    _add_listing(conn, "rss", "r1")
    _add_listing(conn, "rss", "r2", primary=False)
    _add_listing(conn, "rss", "r3", primary=False)
    a = db.source_coverage_analytics(conn)["rss"]
    assert a["duplicate_suppression_pct"] == 67  # 2 of 3


def test_duplicate_suppression_pct_none_with_no_listings_but_price_data(conn):
    # A source that only ever appears via product_price_observations (see
    # source_coverage's own equivalent test) has no listings to compute a
    # rate from - stays None, not a misleading 0%.
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (1, 100, 'auction-close', ?)",
        (_iso(days=1),),
    )
    a = db.source_coverage_analytics(conn)["auction-close"]
    assert a["duplicate_suppression_pct"] is None
    assert a["total_sightings"] == 0


# --- catalogue_match_pct (reused) ----------------------------------------------


def test_catalogue_match_pct_matches_source_coverage(conn):
    item_id = _seed_item(conn)
    a_listing = _add_listing(conn, "ebay", "a")
    b_listing = _add_listing(conn, "ebay", "b")
    _add_match(conn, a_listing, item_id, product_id=1)
    _add_match(conn, b_listing, item_id)  # no catalogue product
    cov = db.source_coverage(conn)["ebay"]
    analytics = db.source_coverage_analytics(conn)["ebay"]
    assert analytics["catalogue_match_pct"] == cov["catalogue_match_pct"] == 50


# --- deal_rate_pct --------------------------------------------------------------


def test_deal_rate_counts_primary_listings_only(conn):
    item_id = _seed_item(conn)
    deal = _add_listing(conn, "ebay", "deal")
    no_deal = _add_listing(conn, "ebay", "no-deal")
    dup_deal = _add_listing(conn, "ebay", "dup-deal", primary=False)
    _add_match(conn, deal, item_id, under_target=True)
    _add_match(conn, no_deal, item_id, under_target=False)
    _add_match(conn, dup_deal, item_id, under_target=True)  # excluded: not primary
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["deal_rate_pct"] == 50  # 1 of 2 primary listings


def test_deal_rate_none_when_nothing_evaluated(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "unevaluated")
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["deal_rate_pct"] is None


# --- stale_rate_pct ---------------------------------------------------------------


def test_stale_rate_pct(conn):
    _seed_item(conn)
    _add_listing(conn, "rss", "fresh", first_seen=_iso(hours=2), last_seen=_iso(hours=1))
    _add_listing(conn, "rss", "lingering", first_seen=_iso(days=6), last_seen=_iso(days=3))
    a = db.source_coverage_analytics(conn)["rss"]
    assert a["stale_rate_pct"] == 50  # 1 of 2


# --- avg_lifetime_days / lifetime_sample_size ------------------------------------


def test_avg_lifetime_uses_end_time_for_ended_listings(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "ended", first_seen=_iso(days=5), end_time=_iso(days=2))
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["avg_lifetime_days"] == pytest.approx(3.0, abs=0.1)
    assert a["lifetime_sample_size"] == 1


def test_avg_lifetime_uses_last_seen_for_stale_listings(conn):
    _seed_item(conn)
    # No end_time, not rescanned in over _STALE_AFTER_HOURS (48h) => stale.
    _add_listing(conn, "ebay", "stale", first_seen=_iso(days=10), last_seen=_iso(days=6))
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["avg_lifetime_days"] == pytest.approx(4.0, abs=0.1)
    assert a["lifetime_sample_size"] == 1


def test_avg_lifetime_excludes_still_live_listings(conn):
    _seed_item(conn)
    # Fresh, still being rescanned - lifetime hasn't concluded.
    _add_listing(conn, "ebay", "active", first_seen=_iso(days=10), last_seen=_iso(minutes=5))
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["avg_lifetime_days"] is None
    assert a["lifetime_sample_size"] == 0


def test_avg_lifetime_blends_ended_and_stale_but_not_active(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "ended", first_seen=_iso(days=5), end_time=_iso(days=3))  # 2d
    _add_listing(conn, "ebay", "stale", first_seen=_iso(days=10), last_seen=_iso(days=6))  # 4d
    _add_listing(conn, "ebay", "active", first_seen=_iso(days=1), last_seen=_iso(minutes=1))
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["lifetime_sample_size"] == 2
    assert a["avg_lifetime_days"] == pytest.approx(3.0, abs=0.1)


# --- price_history_coverage_pct --------------------------------------------------


def test_price_history_coverage_pct(conn):
    item_id = _seed_item(conn)
    a_listing = _add_listing(conn, "ebay", "a")
    b_listing = _add_listing(conn, "ebay", "b")
    _add_match(conn, a_listing, item_id, product_id=1)
    _add_match(conn, b_listing, item_id, product_id=1)
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (1, 100, 'ebay', ?)",
        (_iso(days=45),),  # deliberately outside the 30d window used elsewhere
    )
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["price_history_coverage_pct"] == 50  # 1 observation / 2 catalogued matches


def test_price_history_coverage_pct_none_without_catalogue_matches(conn):
    _seed_item(conn)
    _add_listing(conn, "ebay", "unmatched")
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["price_history_coverage_pct"] is None


# --- time_to_first_match: honestly unavailable -----------------------------------


def test_time_to_first_match_is_unavailable_with_explanation(conn):
    item_id = _seed_item(conn)
    listing_id = _add_listing(conn, "ebay", "a")
    _add_match(conn, listing_id, item_id, product_id=1)
    a = db.source_coverage_analytics(conn)["ebay"]
    assert a["time_to_first_match"] is None
    assert a["time_to_first_match_unavailable_reason"] == db.TIME_TO_FIRST_MATCH_UNAVAILABLE
    assert a["time_to_first_match_unavailable_reason"]  # non-empty prose, not just a flag
