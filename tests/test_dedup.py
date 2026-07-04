from product_finder import db
from product_finder.models import Evaluation, Listing


def make_listing(external_id="e1", price=250.0):
    return Listing(
        source="ebay",
        external_id=external_id,
        title="Makita track saw",
        price=price,
        url=f"https://example.com/{external_id}",
    )


def make_evaluation():
    return Evaluation(
        grade="B", flags=[], margin_abs=250.0, margin_pct=50.0,
        under_target=True, deal_score=80.0,
    )


def test_listing_dedup(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    id1, new1 = db.upsert_listing(conn, make_listing())
    id2, new2 = db.upsert_listing(conn, make_listing(price=240.0))
    assert new1 is True
    assert new2 is False
    assert id1 == id2
    # price updated on re-sight
    row = conn.execute("SELECT price FROM listings WHERE id = ?", (id1,)).fetchone()
    assert row["price"] == 240.0


def test_image_url_stored_refreshed_and_never_blanked(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    first = make_listing()
    first.image_url = "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    listing_id, _ = db.upsert_listing(conn, first)
    row = db.get_listing(conn, listing_id)
    assert row["image_url"] == "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"

    # Re-sight without an image (e.g. a proxy source): keeps the existing one.
    db.upsert_listing(conn, make_listing())
    assert db.get_listing(conn, listing_id)["image_url"].endswith("s-l1600.jpg")

    # Re-sight with a different image: refreshed.
    updated = make_listing()
    updated.image_url = "https://i.ebayimg.com/images/g/xyz/s-l1600.jpg"
    db.upsert_listing(conn, updated)
    assert db.get_listing(conn, listing_id)["image_url"].endswith("g/xyz/s-l1600.jpg")


def test_different_sources_not_deduped(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    _, new1 = db.upsert_listing(conn, make_listing())
    other = make_listing()
    other.source = "gumtree"
    _, new2 = db.upsert_listing(conn, other)
    assert new1 is True and new2 is True


def test_match_only_alerts_once(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    conn.execute("INSERT INTO projects (slug, name) VALUES ('p', 'P')")
    conn.execute(
        "INSERT INTO items (project_id, name) VALUES "
        "((SELECT id FROM projects WHERE slug='p'), 'Track Saw')"
    )
    item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
    listing_id, _ = db.upsert_listing(conn, make_listing())

    match_id, is_new = db.record_match(conn, listing_id, item_id, make_evaluation())
    assert is_new is True
    _, is_new_again = db.record_match(conn, listing_id, item_id, make_evaluation())
    assert is_new_again is False

    assert db.mark_alerted(conn, match_id, "console") is True
    assert db.mark_alerted(conn, match_id, "console") is False
    assert db.mark_alerted(conn, match_id, "webhook") is True
