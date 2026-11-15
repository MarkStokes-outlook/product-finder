from datetime import datetime, timedelta, timezone

from product_finder import db, runner, sources
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities


def _setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(
        conn, project_id,
        ItemConfig(name="Mitre Saw", terms=["mitre saw"], normal_price=350, target_deal_price=200),
    )
    product_id = db.create_product(
        conn, item_id, "Makita", "LS1019L", ["makita ls1019l"], 900, None, 700
    )
    return conn, item_id, product_id


# --- db.record_price_observation / rolling median -----------------------------


def test_record_price_observation_computes_median(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    for price in (100, 200, 300):
        db.record_price_observation(conn, product_id, price, "ebay")
    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.typical_used_price == 200


def test_record_price_observation_ignores_stale_observations(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (product_id, 1000.0, "ebay", stale),
    )
    conn.commit()
    db.record_price_observation(conn, product_id, 150, "ebay")
    product = db._product_from_row(db.get_product(conn, product_id))
    # The 200-day-old £1000 observation must not drag the median up.
    assert product.typical_used_price == 150


def test_list_price_observations(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    db.record_price_observation(conn, product_id, 100, "ebay")
    db.record_price_observation(conn, product_id, 110, "gumtree")
    rows = db.list_price_observations(conn, product_id)
    assert [r["price"] for r in rows] == [100, 110]
    assert [r["source"] for r in rows] == ["ebay", "gumtree"]


def test_expired_comparables_disappear_until_remaining_evidence_is_refreshed(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    db.record_price_observation(conn, product_id, 100, "ebay")
    db.record_price_observation(conn, product_id, 200, "ebay")
    assert db.list_products_for_matching(conn, item_id)[0].typical_used_price == 150
    conn.execute("UPDATE product_price_observations SET observed_at = '2020-01-01T00:00:00+00:00' WHERE price = 100")
    conn.execute("UPDATE products SET used_price_expires_at = '2020-04-01T00:00:00+00:00'")
    conn.commit()

    assert db.list_products_for_matching(conn, item_id)[0].typical_used_price is None
    db.refresh_market_references(conn)
    row = db.list_products(conn, item_id)[0]
    assert row["typical_used_price"] == 200
    assert row["used_price_samples"] == 1
    assert len(db.list_price_observations(conn, product_id)) == 2


def test_auction_close_comparables_are_kept_separate_from_asking_prices(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    db.record_price_observation(conn, product_id, 250, "ebay")
    for price in (100, 110):
        db.record_price_observation(conn, product_id, price, "ebay-close")
    assert db.list_products(conn, item_id)[0]["typical_used_price"] == 250
    db.record_price_observation(conn, product_id, 120, "ebay-close")
    row = db.list_products(conn, item_id)[0]
    assert row["typical_used_price"] == 110
    assert row["used_price_samples"] == 3
    assert row["used_price_basis"] == "Auction closes"


def test_observations_track_and_deduplicate_the_same_listing(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    listing = Listing("ebay", "known", "Makita LS1019L", 150, "https://example.com/known")
    listing_id, _ = db.upsert_listing(conn, listing)
    db.record_price_observation(conn, product_id, 150, "ebay", listing_id=listing_id)
    db.record_price_observation(conn, product_id, 100, "ebay", listing_id=listing_id)
    [row] = db.list_price_observations(conn, product_id)
    assert row["listing_id"] == listing_id
    assert row["price"] == 150


def test_old_retailer_reference_falls_back_to_msrp(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    conn.execute(
        "UPDATE products SET typical_new_price = 500, canonical_price_url = 'https://example.com/p', "
        "last_price_check_ok = 1, last_price_check_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (product_id,),
    )
    conn.commit()
    product = db.list_products_for_matching(conn, item_id)[0]
    assert product.typical_new_price is None
    assert product.msrp == 900


def test_deleting_product_deletes_its_observations(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    db.record_price_observation(conn, product_id, 100, "ebay")
    db.delete_product(conn, product_id)
    assert conn.execute("SELECT COUNT(*) c FROM product_price_observations").fetchone()["c"] == 0


# --- db.record_price_observation / cached used-price trend (see price_trend.py) --


def _insert_observation(conn, product_id, days_ago, price, source="ebay"):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (product_id, price, source, ts),
    )
    conn.commit()


def test_insufficient_history_leaves_trend_uncached(tmp_path):
    conn, _, product_id = _setup(tmp_path)
    db.record_price_observation(conn, product_id, 100, "ebay")
    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.price_trend_pct is None
    assert product.price_trend_confidence == 0.0


def test_trend_cached_only_changes_when_new_observation_recorded(tmp_path):
    conn, _, product_id = _setup(tmp_path)

    # Prior window (30-60 days ago): median 100.
    for days_ago, price in [(35, 100), (45, 102), (55, 98)]:
        _insert_observation(conn, product_id, days_ago, price)
    # Recent window (last 30 days): median 90 once the final observation
    # below is recorded through record_price_observation (the only call
    # that (re)computes and caches the trend).
    _insert_observation(conn, product_id, 15, 92)
    db.record_price_observation(conn, product_id, 88, "ebay")

    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.price_trend_pct is not None
    assert product.price_trend_confidence > 0
    cached_pct = product.price_trend_pct
    cached_confidence = product.price_trend_confidence

    # Re-reading the product (no new observation in between) must not move
    # the cached figures — there is no per-read recomputation.
    for _ in range(3):
        reread = db._product_from_row(db.get_product(conn, product_id))
        assert reread.price_trend_pct == cached_pct
        assert reread.price_trend_confidence == cached_confidence

    # A genuinely new observation is the only thing allowed to move it —
    # a confirmed close at a much lower price shifts the recent window hard.
    db.record_price_observation(conn, product_id, 60, "ebay-close")
    updated = db._product_from_row(db.get_product(conn, product_id))
    assert updated.price_trend_pct != cached_pct


# --- runner.py wiring -----------------------------------------------------------


class FakeSource(Source):
    name = "ebay"

    def __init__(self, cfg, listings):
        super().__init__(cfg)
        self._listings = listings

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        return self._listings

    def manual_links(self, item):
        return []


def _run_with_listings(cfg, conn, listings):
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: {"ebay": FakeSource(eff_cfg, listings)}
    try:
        return runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig


def test_runner_logs_one_observation_for_a_matched_fixed_price_listing(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    listing = Listing(
        source="ebay", external_id="1", title="Makita LS1019L mitre saw, boxed", price=650.0,
        url="https://x/1", buying_options=["FIXED_PRICE"],
    )
    _run_with_listings(cfg, conn, [listing])

    rows = db.list_price_observations(conn, product_id)
    assert len(rows) == 1
    assert rows[0]["price"] == 650.0
    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.typical_used_price == 650.0


def test_runner_does_not_log_duplicate_observation_on_rescan(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    listing = Listing(
        source="ebay", external_id="1", title="Makita LS1019L mitre saw, boxed", price=650.0,
        url="https://x/1", buying_options=["FIXED_PRICE"],
    )
    _run_with_listings(cfg, conn, [listing])
    _run_with_listings(cfg, conn, [listing])  # simulate a later watch cycle re-seeing it

    rows = db.list_price_observations(conn, product_id)
    assert len(rows) == 1


def test_runner_does_not_log_observation_for_live_auction(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    listing = Listing(
        source="ebay", external_id="1", title="Makita LS1019L mitre saw", price=5.0,
        url="https://x/1", buying_options=["AUCTION"],
    )
    _run_with_listings(cfg, conn, [listing])

    assert db.list_price_observations(conn, product_id) == []
    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.typical_used_price is None


def test_runner_does_not_log_observation_for_unmatched_listing(tmp_path):
    conn, item_id, product_id = _setup(tmp_path)
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    listing = Listing(
        source="ebay", external_id="1", title="Own Brand mitre saw", price=90.0,
        url="https://x/1", buying_options=["FIXED_PRICE"],
    )
    _run_with_listings(cfg, conn, [listing])

    assert db.list_price_observations(conn, product_id) == []
