from datetime import datetime, timedelta, timezone
from unittest import mock

from product_finder import auction_watch, db
from product_finder.config import AppConfig, EbayConfig, ItemConfig, SourcesConfig
from product_finder.models import AuctionSnapshot, Listing


def _setup(tmp_path):
    cfg = AppConfig(
        db_path=str(tmp_path / "t.db"),
        sources=SourcesConfig(ebay=EbayConfig(app_id="id", cert_id="secret")),
    )
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(
        conn, project_id, ItemConfig(name="Golf Watch", terms=["golf gps watch"])
    )
    product_id = db.create_product(
        conn, item_id, "Shot Scope", "V2", ["shot scope v2"], None, None, None
    )
    return cfg, conn, item_id, product_id


def _seed_auction_listing(conn, item_id, end_time: str, external_id="e1", price=20.0):
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(
            source="ebay", external_id=external_id, title="Shot Scope V2 Golf GPS Watch",
            price=price, url=f"https://x/{external_id}",
            buying_options=["AUCTION"], bid_count=3, end_time=end_time,
        ),
    )
    from product_finder.models import Evaluation

    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="B", flags=["live auction"], margin_abs=0, margin_pct=0,
                   under_target=False, deal_score=50.0),
    )
    return listing_id


def _attach_product(conn, listing_id, item_id, product_id):
    match_id = conn.execute(
        "SELECT id FROM listing_matches WHERE listing_id = ? AND item_id = ?",
        (listing_id, item_id),
    ).fetchone()["id"]
    conn.execute("UPDATE listing_matches SET product_id = ? WHERE id = ?", (product_id, match_id))
    conn.commit()


# --- upsert_listing persistence -------------------------------------------------


def test_upsert_listing_persists_auction_fields(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = "2026-07-03T20:00:00.000Z"
    listing_id = _seed_auction_listing(conn, item_id, end)
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["end_time"] == end
    assert row["bid_count"] == 3
    assert row["buying_options"] == '["AUCTION"]'


def test_upsert_listing_refreshes_auction_fields_on_rescan(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = "2026-07-03T20:00:00.000Z"
    _seed_auction_listing(conn, item_id, end, price=20.0)
    listing_id, is_new = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e1", title="Shot Scope V2 Golf GPS Watch",
                price=25.0, url="https://x/e1", buying_options=["AUCTION"], bid_count=5,
                end_time=end),
    )
    assert is_new is False
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["bid_count"] == 5
    assert row["price"] == 25.0


def test_upsert_listing_persists_current_bid_and_bin_distinctly(tmp_path):
    """Bug fix (2026-07-08): current_bid_price/buy_it_now_price must be
    stored distinctly from price (the BIN-preferring fallback) — this is
    what closes the gap where a freshly-seen BIN+auction listing had
    nothing but the BIN price recorded anywhere."""
    cfg, conn, item_id, product_id = _setup(tmp_path)
    listing_id, is_new = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e9", title="PSU, auction with BIN",
                price=31.00, url="https://x/e9", buying_options=["AUCTION", "FIXED_PRICE"],
                bid_count=0, current_bid_price=9.68, buy_it_now_price=31.00),
    )
    assert is_new is True
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["price"] == 31.00
    assert row["current_bid_price"] == 9.68
    assert row["buy_it_now_price"] == 31.00

    # Refresh on rescan — bid climbs, BIN unchanged.
    db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e9", title="PSU, auction with BIN",
                price=31.00, url="https://x/e9", buying_options=["AUCTION", "FIXED_PRICE"],
                bid_count=2, current_bid_price=15.40, buy_it_now_price=31.00),
    )
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["current_bid_price"] == 15.40
    assert row["buy_it_now_price"] == 31.00


# --- cadence tiers ---------------------------------------------------------------


def test_due_for_poll_always_true_when_never_polled():
    now = datetime.now(timezone.utc)
    assert auction_watch.due_for_poll(now + timedelta(hours=1), None, now) is True


def test_due_for_poll_far_out_uses_slow_cadence():
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=1)
    just_polled = now - timedelta(minutes=1)
    assert auction_watch.due_for_poll(end, just_polled, now) is False
    long_ago = now - timedelta(minutes=6)
    assert auction_watch.due_for_poll(end, long_ago, now) is True


def test_due_for_poll_closing_soon_uses_fast_cadence():
    now = datetime.now(timezone.utc)
    end = now + timedelta(seconds=90)
    recently_polled = now - timedelta(seconds=10)
    assert auction_watch.due_for_poll(end, recently_polled, now) is False
    a_bit_ago = now - timedelta(seconds=25)
    assert auction_watch.due_for_poll(end, a_bit_ago, now) is True


# --- poll_and_capture -------------------------------------------------------------


def test_poll_and_capture_records_observation_when_ended(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    _attach_product(conn, listing_id, item_id, product_id)

    with mock.patch(
        "product_finder.sources.ebay.EbaySource.get_item",
        return_value=AuctionSnapshot(price=22.0, bid_count=5, ended=True),
    ):
        captured = auction_watch.poll_and_capture(cfg, conn)

    assert captured == 1
    rows = db.list_price_observations(conn, product_id)
    assert len(rows) == 1
    assert rows[0]["price"] == 22.0
    assert rows[0]["source"] == "ebay-close"
    listing = conn.execute("SELECT sold_captured FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert listing["sold_captured"] == 1


def test_poll_and_capture_skips_when_not_yet_due(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    _attach_product(conn, listing_id, item_id, product_id)
    db.mark_listing_polled(conn, listing_id)  # just polled -> far-out cadence not due yet

    with mock.patch("product_finder.sources.ebay.EbaySource.get_item") as get_item:
        captured = auction_watch.poll_and_capture(cfg, conn)

    get_item.assert_not_called()
    assert captured == 0


def test_poll_and_capture_ignores_non_auction_listings(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e2", title="Shot Scope V2 fixed price",
                price=30.0, url="https://x/e2", buying_options=["FIXED_PRICE"], end_time=end),
    )
    from product_finder.models import Evaluation
    db.record_match(conn, listing_id, item_id, Evaluation(
        grade="B", flags=[], margin_abs=0, margin_pct=0, under_target=False, deal_score=50.0))
    _attach_product(conn, listing_id, item_id, product_id)

    with mock.patch("product_finder.sources.ebay.EbaySource.get_item") as get_item:
        auction_watch.poll_and_capture(cfg, conn)

    get_item.assert_not_called()


def test_poll_and_capture_gives_up_after_grace_period(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    _attach_product(conn, listing_id, item_id, product_id)

    with mock.patch(
        "product_finder.sources.ebay.EbaySource.get_item",
        return_value=AuctionSnapshot(price=22.0, bid_count=5, ended=False),
    ):
        captured = auction_watch.poll_and_capture(cfg, conn)

    assert captured == 0
    assert db.list_price_observations(conn, product_id) == []
    listing = conn.execute("SELECT sold_captured FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert listing["sold_captured"] == 1  # gave up, stopped tracking


def test_poll_and_capture_noop_when_ebay_not_configured(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))  # no ebay credentials
    conn = db.connect(cfg.db_path)
    assert auction_watch.poll_and_capture(cfg, conn) == 0


# --- snapshot history (Coverage phase) --------------------------------------------


def test_poll_and_capture_records_snapshot_even_when_not_yet_ended(tmp_path):
    """The core new behaviour: every due poll is recorded as an observation,
    not just the final closing one."""
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    _attach_product(conn, listing_id, item_id, product_id)

    with mock.patch(
        "product_finder.sources.ebay.EbaySource.get_item",
        return_value=AuctionSnapshot(price=22.0, current_bid=22.0, bid_count=5, ended=False, buy_it_now_price=None),
    ):
        captured = auction_watch.poll_and_capture(cfg, conn)

    assert captured == 0  # not a close, still in progress
    rows = db.list_auction_snapshots(conn, listing_id)
    assert len(rows) == 1
    assert rows[0]["current_bid_price"] == 22.0
    assert rows[0]["bid_count"] == 5
    # No close yet, so the product's used-price observations are untouched.
    assert db.list_price_observations(conn, product_id) == []


def test_poll_and_capture_builds_history_across_multiple_polls(tmp_path):
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    _attach_product(conn, listing_id, item_id, product_id)

    for bid in (20.0, 25.0, 31.0):
        with mock.patch(
            "product_finder.sources.ebay.EbaySource.get_item",
            return_value=AuctionSnapshot(price=bid, current_bid=bid, bid_count=1, ended=False),
        ):
            auction_watch.poll_and_capture(cfg, conn)
        conn.execute("UPDATE listings SET last_poll_at = NULL WHERE id = ?", (listing_id,))
        conn.commit()  # force each iteration to be "due" regardless of cadence

    rows = db.list_auction_snapshots(conn, listing_id)
    assert [r["current_bid_price"] for r in rows] == [20.0, 25.0, 31.0]  # nothing overwritten


def test_poll_and_capture_tracks_auctions_with_no_catalogue_match(tmp_path):
    """Snapshot history no longer requires a catalogue-product match — only
    the product-specific used-price observation feed does."""
    cfg, conn, item_id, product_id = _setup(tmp_path)
    end = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing_id = _seed_auction_listing(conn, item_id, end)
    # deliberately not calling _attach_product — this listing has no product match

    with mock.patch(
        "product_finder.sources.ebay.EbaySource.get_item",
        return_value=AuctionSnapshot(price=40.0, current_bid=40.0, bid_count=2, ended=True),
    ):
        captured = auction_watch.poll_and_capture(cfg, conn)

    assert captured == 1
    rows = db.list_auction_snapshots(conn, listing_id)
    assert len(rows) == 1
    assert rows[0]["current_bid_price"] == 40.0
    # No product to attribute a used-price observation to.
    assert db.list_price_observations(conn, product_id) == []
    listing = conn.execute("SELECT sold_captured FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert listing["sold_captured"] == 1
