"""db.record_auction_snapshot / list_auction_snapshots: the append-only
per-listing auction observation history (see auction_watch.poll_and_capture,
which is what calls these in production)."""

from product_finder import db
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing


def _setup(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(name="Drill", terms=["drill"]))
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(
            source="ebay", external_id="e1", title="Drill, auction",
            price=20.0, url="https://x/e1", buying_options=["AUCTION"],
            bid_count=1, end_time="2026-07-09T12:00:00.000Z",
        ),
    )
    return conn, listing_id


def test_record_and_list_single_snapshot(tmp_path):
    conn, listing_id = _setup(tmp_path)
    db.record_auction_snapshot(
        conn, listing_id, source="ebay", current_bid_price=20.0, bid_count=1,
        end_time="2026-07-09T12:00:00.000Z",
    )
    rows = db.list_auction_snapshots(conn, listing_id)
    assert len(rows) == 1
    assert rows[0]["current_bid_price"] == 20.0
    assert rows[0]["bid_count"] == 1


def test_repeated_observations_are_never_overwritten(tmp_path):
    conn, listing_id = _setup(tmp_path)
    db.record_auction_snapshot(conn, listing_id, source="ebay", current_bid_price=20.0, bid_count=1)
    db.record_auction_snapshot(conn, listing_id, source="ebay", current_bid_price=25.0, bid_count=2)
    db.record_auction_snapshot(conn, listing_id, source="ebay", current_bid_price=31.0, bid_count=4)

    rows = db.list_auction_snapshots(conn, listing_id)
    assert len(rows) == 3  # all three preserved, not collapsed to the latest
    assert [r["current_bid_price"] for r in rows] == [20.0, 25.0, 31.0]
    assert [r["bid_count"] for r in rows] == [1, 2, 4]


def test_snapshot_records_optional_fields_and_provenance(tmp_path):
    conn, listing_id = _setup(tmp_path)
    db.record_auction_snapshot(
        conn, listing_id, source="ebay",
        current_bid_price=156.70, bid_count=0,
        buy_it_now_price=229.50, shipping_price=5.88,
        end_time="2026-07-14T20:34:17.000Z",
        watch_count=None, view_count=None,
        raw_payload={"itemId": "v1|1|0", "buyingOptions": ["FIXED_PRICE", "AUCTION"]},
    )
    row = db.list_auction_snapshots(conn, listing_id)[0]
    assert row["buy_it_now_price"] == 229.50
    assert row["shipping_price"] == 5.88
    assert row["watch_count"] is None
    assert row["view_count"] is None
    assert '"itemId": "v1|1|0"' in row["raw_payload"]


def test_snapshots_scoped_to_their_own_listing(tmp_path):
    conn, listing_id = _setup(tmp_path)
    listing_id_2, _ = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e2", title="Different drill, auction",
                price=10.0, url="https://x/e2", buying_options=["AUCTION"]),
    )
    db.record_auction_snapshot(conn, listing_id, source="ebay", current_bid_price=20.0)
    db.record_auction_snapshot(conn, listing_id_2, source="ebay", current_bid_price=99.0)

    assert len(db.list_auction_snapshots(conn, listing_id)) == 1
    assert len(db.list_auction_snapshots(conn, listing_id_2)) == 1
    assert db.list_auction_snapshots(conn, listing_id)[0]["current_bid_price"] == 20.0
    assert db.list_auction_snapshots(conn, listing_id_2)[0]["current_bid_price"] == 99.0


def test_listing_row_keeps_latest_state_independent_of_history(tmp_path):
    """The `listings` row is still just "latest known state" (existing
    upsert_listing behaviour, unchanged) — auction_snapshots is what adds
    history on top, it doesn't change what upsert_listing does."""
    conn, listing_id = _setup(tmp_path)
    db.record_auction_snapshot(conn, listing_id, source="ebay", current_bid_price=20.0, bid_count=1)
    db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="e1", title="Drill, auction",
                price=35.0, url="https://x/e1", buying_options=["AUCTION"], bid_count=6),
    )
    listing_row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert listing_row["price"] == 35.0
    assert listing_row["bid_count"] == 6
    # ...but the earlier observation is still there, not lost.
    assert db.list_auction_snapshots(conn, listing_id)[0]["current_bid_price"] == 20.0
