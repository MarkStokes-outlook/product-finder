from product_finder import db, runner, scoring
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing


def _setup(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item = ItemConfig(
        name="Track Saw", terms=["track saw"],
        max_price=400, normal_price=500, target_deal_price=300,
    )
    item_id = db.create_item(conn, project_id, item)
    return conn, item_id


def _seed_listing_match(conn, item_id, title, price, external_id="e1"):
    listing = Listing(source="ebay", external_id=external_id, title=title, price=price,
                       url=f"https://example.com/{external_id}")
    listing_id, _ = db.upsert_listing(conn, listing)
    item = db._item_from_row(db.get_item(conn, item_id))
    evaluation = scoring.evaluate(listing, item)
    match_id, _ = db.record_match(conn, listing_id, item_id, evaluation)
    return listing_id, match_id


def test_reassess_removes_matches_that_now_fail_a_new_exclude_term(tmp_path):
    conn, item_id = _setup(tmp_path)
    _seed_listing_match(conn, item_id, "Makita track saw, spares or repairs", 150.0, external_id="e1")
    _seed_listing_match(conn, item_id, "Makita track saw, mint condition", 250.0, external_id="e2")

    item = db._item_from_row(db.get_item(conn, item_id))
    item.exclude_terms = ["spares or repairs"]
    db.update_item(conn, item_id, item)

    result = runner.reassess_item_matches(conn, item_id, item)

    assert result == {"rescored": 1, "excluded": 1}
    remaining = conn.execute(
        "SELECT l.title FROM listing_matches m JOIN listings l ON l.id = m.listing_id "
        "WHERE m.item_id = ?", (item_id,),
    ).fetchall()
    assert [r["title"] for r in remaining] == ["Makita track saw, mint condition"]


def test_reassess_removes_matches_over_a_lowered_max_price(tmp_path):
    conn, item_id = _setup(tmp_path)
    _seed_listing_match(conn, item_id, "Makita track saw", 350.0, external_id="e1")
    _seed_listing_match(conn, item_id, "Makita track saw", 150.0, external_id="e2")

    item = db._item_from_row(db.get_item(conn, item_id))
    item.max_price = 200.0
    db.update_item(conn, item_id, item)

    result = runner.reassess_item_matches(conn, item_id, item)

    assert result == {"rescored": 1, "excluded": 1}


def test_reassess_leaves_unaffected_matches_untouched(tmp_path):
    conn, item_id = _setup(tmp_path)
    _seed_listing_match(conn, item_id, "Makita track saw", 250.0)

    item = db._item_from_row(db.get_item(conn, item_id))
    result = runner.reassess_item_matches(conn, item_id, item)

    assert result == {"rescored": 1, "excluded": 0}


def test_reassess_rescoring_reflects_new_prices(tmp_path):
    conn, item_id = _setup(tmp_path)
    # Original target (300) already makes £250 a "deal" — lower it below
    # the listing price first so there's a real before/after to observe.
    item = db._item_from_row(db.get_item(conn, item_id))
    item.target_deal_price = 200.0
    db.update_item(conn, item_id, item)
    _seed_listing_match(conn, item_id, "Makita track saw", 250.0)
    before = conn.execute(
        "SELECT under_target FROM listing_matches WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert before["under_target"] == 0

    item.target_deal_price = 260.0
    db.update_item(conn, item_id, item)
    runner.reassess_item_matches(conn, item_id, item)

    after = conn.execute(
        "SELECT under_target FROM listing_matches WHERE item_id = ?", (item_id,)
    ).fetchone()
    # £250 against a £260 target is now a deal, where it wasn't at the
    # original £200 target.
    assert after["under_target"] == 1


def test_reassess_reattributes_to_a_catalogue_product(tmp_path):
    conn, item_id = _setup(tmp_path)
    _seed_listing_match(conn, item_id, "Makita LS0816F/2 track saw", 250.0)
    suggestion = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    db.approve_suggestion(conn, suggestion["id"])

    item = db._item_from_row(db.get_item(conn, item_id))
    result = runner.reassess_item_matches(conn, item_id, item)

    assert result == {"rescored": 1, "excluded": 0}
    match = conn.execute(
        "SELECT product_id FROM listing_matches WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert match["product_id"] is not None


def test_reassess_returns_zero_counts_with_no_matches(tmp_path):
    conn, item_id = _setup(tmp_path)
    item = db._item_from_row(db.get_item(conn, item_id))
    assert runner.reassess_item_matches(conn, item_id, item) == {"rescored": 0, "excluded": 0}
