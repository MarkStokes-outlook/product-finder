"""Fuzzy duplicate detection (identity v2): duplicates.py scoring, candidate
generation, review-state transitions, and runner wiring."""

from product_finder import db, duplicates, runner, sources
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Evaluation, Listing
from product_finder.sources.base import Source, SourceCapabilities


# --- duplicates.evaluate_pair (pure) -------------------------------------------


def _row(title, price, source="ebay", location="", image_url=None):
    return {
        "title": title, "price": price, "source": source,
        "location": location, "image_url": image_url,
    }


def test_normalize_title_strips_punctuation_and_case():
    assert duplicates.normalize_title("  Makita SP6000/J1 — Track-Saw!  ") == (
        "makita sp6000 j1 track saw"
    )


def test_same_marketplace_pair_never_queues_even_with_every_signal_matching():
    # This is the pair the feature was originally built around — identical
    # titles, same eBay seller location, ~20% price apart. It turned out to
    # be exactly the false-positive pattern Mark flagged: a seller with
    # multiple stock units listed under different IDs, free to sell or
    # reprice independently. Different listing ID, same marketplace, must
    # never queue however strong the other signals are.
    a = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 83.89, location="EN6***",
             image_url="https://i.ebayimg.com/images/g/774/s-l1600.jpg")
    b = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 69.99, location="EN6***",
             image_url="https://i.ebayimg.com/images/g/774/s-l1600.jpg")
    assert duplicates.evaluate_pair(a, b) is None


def test_same_source_without_location_or_image_is_rejected():
    a = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 83.89, location="EN6***")
    b = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 69.99, location="LS1***")
    assert duplicates.evaluate_pair(a, b) is None


def test_cross_marketplace_reference_pair_queues():
    # Same title/price/location as the reference pair above, but genuinely
    # cross-marketplace (a seller cross-posting) — this is the case v2
    # exists for, and the only shape it now scores.
    a = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 83.89, source="ebay",
             location="EN6***", image_url="https://i.ebayimg.com/images/g/774/s-l1600.jpg")
    b = _row("VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz", 69.99, source="gumtree",
             location="EN6***", image_url="https://i.ebayimg.com/images/g/8F4/s-l1600.jpg")
    result = duplicates.evaluate_pair(a, b)
    assert result is not None
    confidence, signals = result
    assert 78 < confidence < 80
    assert signals["title_sim"] == 1.0
    assert signals["same_location"] is True
    assert signals["same_image"] is False
    assert signals["cross_source"] is True


def test_cross_source_pair_scores_without_seller_evidence():
    # Cross-marketplace pairs never need a location/image match (formats
    # differ across marketplaces) — title/price similarity is enough to
    # queue on its own; a matching location just adds confidence on top.
    bare = duplicates.evaluate_pair(
        _row("Makita SP6000 track saw", 250.0, source="ebay"),
        _row("Makita SP6000 track saw", 250.0, source="gumtree"),
    )
    with_location = duplicates.evaluate_pair(
        _row("Makita SP6000 track saw", 250.0, source="ebay", location="EN6***"),
        _row("Makita SP6000 track saw", 250.0, source="gumtree", location="EN6***"),
    )
    assert bare is not None and with_location is not None
    assert bare[1]["cross_source"] is True
    assert with_location[0] > bare[0]


def test_dissimilar_titles_rejected():
    a = _row("Makita SP6000 track saw", 250.0, source="ebay", location="EN6***")
    b = _row("Bosch GTS 10 XC table saw", 240.0, source="gumtree", location="EN6***")
    assert duplicates.evaluate_pair(a, b) is None


def test_price_delta_beyond_cap_rejected():
    a = _row("Makita SP6000 track saw", 100.0, source="ebay", location="EN6***")
    b = _row("Makita SP6000 track saw", 200.0, source="gumtree", location="EN6***")  # 100% apart
    assert duplicates.evaluate_pair(a, b) is None


def test_zero_price_never_divides():
    a = _row("Makita SP6000 track saw", 0.0, source="ebay", location="EN6***")
    b = _row("Makita SP6000 track saw", 50.0, source="gumtree", location="EN6***")
    assert duplicates.evaluate_pair(a, b) is None


def test_confidence_never_reaches_100():
    img = "https://i.ebayimg.com/x.jpg"
    a = _row("Makita SP6000 track saw", 250.0, source="ebay", location="EN6***", image_url=img)
    b = _row("Makita SP6000 track saw", 250.0, source="gumtree", location="EN6***", image_url=img)
    confidence, _ = duplicates.evaluate_pair(a, b)
    assert confidence == duplicates.CONFIDENCE_CAP


# --- db.scan_duplicate_candidates ----------------------------------------------


def _seed_item(conn, name="Monitor"):
    project_id = db.create_project(conn, f"{name} project")
    item_id = db.create_item(
        conn, project_id,
        ItemConfig(name=name, terms=[name.lower()], normal_price=300, target_deal_price=150),
    )
    return project_id, item_id


def _seed_listing(conn, item_id, external_id, title, price, location="EN6***",
                  source="ebay", end_time=None, image_url=None):
    listing_id, _ = db.upsert_listing(conn, Listing(
        source=source, external_id=external_id, title=title, price=price,
        url=f"https://example.com/{external_id}", location=location,
        end_time=end_time, image_url=image_url,
    ))
    db.record_match(conn, listing_id, item_id, Evaluation(
        grade="A", flags=[], margin_abs=0.0, margin_pct=0.0,
        under_target=False, deal_score=50.0,
    ))
    return listing_id


_TITLE = "VIEWEDGE C2712FDA-P Monitor 27 inch FHD 144hz"


def _seed_pair(conn, item_id, price_a=83.89, price_b=69.99,
                source_a="ebay", source_b="gumtree", **kwargs):
    a = _seed_listing(conn, item_id, "L1", _TITLE, price_a, source=source_a, **kwargs)
    b = _seed_listing(conn, item_id, "L2", _TITLE, price_b, source=source_b, **kwargs)
    return a, b


def test_scan_records_pending_pair(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    a, b = _seed_pair(conn, item_id)
    assert db.scan_duplicate_candidates(conn) == 1
    row = conn.execute("SELECT * FROM listing_duplicates").fetchone()
    assert row["status"] == "pending"
    assert {row["listing_a"], row["listing_b"]} == {a, b}
    assert row["listing_a"] < row["listing_b"]
    assert row["item_id"] == item_id


def test_scan_skips_same_marketplace_pairs_even_with_matching_location(tmp_path):
    # Different listing ID, same marketplace, same seller location — the
    # exact false-positive pattern (multiple stock units, different IDs)
    # must never be proposed, however strong the seller-proxy signal.
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    _seed_listing(conn, item_id, "L1", _TITLE, 83.89, source="ebay", location="EN6***")
    _seed_listing(conn, item_id, "L2", _TITLE, 69.99, source="ebay", location="EN6***")
    assert db.scan_duplicate_candidates(conn) == 0


def test_scan_skips_ended_and_non_primary_listings(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    a = _seed_listing(conn, item_id, "L1", _TITLE, 83.89, source="ebay")
    _seed_listing(conn, item_id, "L2", _TITLE, 69.99, source="gumtree",
                   end_time="2020-01-01T00:00:00.000Z")
    c = _seed_listing(conn, item_id, "L3", _TITLE, 75.00, source="facebook")
    conn.execute("UPDATE listings SET is_primary_sighting = 0 WHERE id = ?", (c,))
    conn.commit()
    assert db.scan_duplicate_candidates(conn) == 0
    assert a  # only the live, primary listing remains — nothing to pair with


def test_scan_never_reproposes_decided_pairs(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    dup_id = conn.execute("SELECT id FROM listing_duplicates").fetchone()["id"]
    db.dismiss_duplicate(conn, dup_id)
    assert db.scan_duplicate_candidates(conn) == 0
    rows = conn.execute("SELECT status FROM listing_duplicates").fetchall()
    assert [r["status"] for r in rows] == ["dismissed"]


def test_scan_refreshes_last_seen_on_pending(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    conn.execute("UPDATE listing_duplicates SET last_seen = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert db.scan_duplicate_candidates(conn) == 0
    row = conn.execute("SELECT last_seen FROM listing_duplicates").fetchone()
    assert row["last_seen"] > "2020-01-01"


def test_scan_respects_per_item_pending_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(duplicates, "MAX_PENDING_PER_ITEM", 1)
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    _seed_listing(conn, item_id, "L1", _TITLE, 80.0, source="ebay")
    _seed_listing(conn, item_id, "L2", _TITLE, 79.0, source="gumtree")
    _seed_listing(conn, item_id, "L3", _TITLE, 78.0, source="facebook")
    assert db.scan_duplicate_candidates(conn) == 1


# --- confirm / dismiss / revert -------------------------------------------------


def test_confirm_hides_non_kept_listing_from_query_matches(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id, item_id = _seed_item(conn)
    a, b = _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    dup_id = conn.execute("SELECT id FROM listing_duplicates").fetchone()["id"]

    kept = db.confirm_duplicate(conn, dup_id, kept_listing_id=b)
    assert kept == b
    row = conn.execute("SELECT * FROM listing_duplicates WHERE id = ?", (dup_id,)).fetchone()
    assert row["status"] == "confirmed"
    assert row["kept_listing_id"] == b
    assert row["decided_at"] is not None
    hidden = conn.execute("SELECT is_primary_sighting FROM listings WHERE id = ?", (a,)).fetchone()
    assert hidden["is_primary_sighting"] == 0
    matches = db.query_matches(conn, project_id=project_id)
    assert len(matches) == 1
    assert matches[0]["price"] == 69.99


def test_confirm_auto_pick_keeps_cheaper(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    a, b = _seed_pair(conn, item_id, price_a=83.89, price_b=69.99)
    db.scan_duplicate_candidates(conn)
    dup_id = conn.execute("SELECT id FROM listing_duplicates").fetchone()["id"]
    assert db.confirm_duplicate(conn, dup_id) == b


def test_confirm_rejects_listing_outside_pair(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    dup_id = conn.execute("SELECT id FROM listing_duplicates").fetchone()["id"]
    try:
        db.confirm_duplicate(conn, dup_id, kept_listing_id=99999)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_revert_confirmed_pair_restores_visibility(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id, item_id = _seed_item(conn)
    a, b = _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    dup_id = conn.execute("SELECT id FROM listing_duplicates").fetchone()["id"]
    db.confirm_duplicate(conn, dup_id, kept_listing_id=b)

    db.revert_duplicate(conn, dup_id)
    row = conn.execute("SELECT * FROM listing_duplicates WHERE id = ?", (dup_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["kept_listing_id"] is None
    restored = conn.execute("SELECT is_primary_sighting FROM listings WHERE id = ?", (a,)).fetchone()
    assert restored["is_primary_sighting"] == 1
    assert len(db.query_matches(conn, project_id=project_id)) == 2


def test_pending_counts_by_project(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id, item_id = _seed_item(conn)
    _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    assert db.pending_duplicate_counts(conn) == {project_id: 1}


def test_pending_list_hides_pair_once_one_side_ends(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id, item_id = _seed_item(conn)
    a, _ = _seed_pair(conn, item_id)
    db.scan_duplicate_candidates(conn)
    assert len(db.list_duplicate_candidates(conn, project_id=project_id)) == 1
    conn.execute("UPDATE listings SET end_time = '2020-01-01T00:00:00.000Z' WHERE id = ?", (a,))
    conn.commit()
    # One side ended → nothing left to double-count → nothing to review.
    assert db.list_duplicate_candidates(conn, project_id=project_id) == []
    assert db.pending_duplicate_counts(conn) == {}


# --- resolve_identity() promotion guard ----------------------------------------


def test_canonical_promotion_never_resurrects_confirmed_duplicate(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _, item_id = _seed_item(conn)
    url = "https://www.ebay.co.uk/itm/195012345678"

    # An RSS proxy for an eBay item is seen first and becomes the canonical
    # identity's primary.
    proxy = Listing(source="rss", external_id="guid-1", title=_TITLE, price=70.0, url=url)
    proxy_id, _ = db.upsert_listing(conn, proxy)
    db.resolve_identity(conn, proxy_id, proxy)

    # The native eBay row arrives; before its identity is resolved, a human
    # confirms it as a duplicate of some other listing, keeping the other.
    native = Listing(source="ebay", external_id="195012345678", title=_TITLE,
                     price=70.0, url=url, location="EN6***")
    native_id, _ = db.upsert_listing(conn, native)
    other_id = _seed_listing(conn, item_id, "OTHER", _TITLE, 69.99, source="gumtree")
    db.record_match(conn, native_id, item_id, Evaluation(
        grade="A", flags=[], margin_abs=0.0, margin_pct=0.0,
        under_target=False, deal_score=50.0,
    ))
    db.scan_duplicate_candidates(conn)
    dup = conn.execute(
        "SELECT id FROM listing_duplicates WHERE ? IN (listing_a, listing_b)", (native_id,)
    ).fetchone()
    db.confirm_duplicate(conn, dup["id"], kept_listing_id=other_id)

    # Canonical resolution would normally promote the native row over the
    # proxy — the human decision must win instead.
    _, is_primary = db.resolve_identity(conn, native_id, native)
    assert is_primary is False
    row = conn.execute(
        "SELECT is_primary_sighting FROM listings WHERE id = ?", (native_id,)
    ).fetchone()
    assert row["is_primary_sighting"] == 0


# --- runner wiring ---------------------------------------------------------------


class FakeSource(Source):
    def __init__(self, cfg, name, listings):
        super().__init__(cfg)
        self.name = name
        self._listings = listings

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        return self._listings

    def manual_links(self, item):
        return []


def test_run_once_generates_candidates(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id = db.create_project(conn, "Gaming")
    db.create_item(conn, project_id, ItemConfig(
        name="Monitor", terms=["monitor"], normal_price=300, target_deal_price=150,
    ))
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    listings = [
        Listing(source="ebay", external_id="L1", title=_TITLE, price=83.89,
                url="https://example.com/L1", location="EN6***"),
        Listing(source="gumtree", external_id="L2", title=_TITLE, price=69.99,
                url="https://example.com/L2", location="EN6***"),
    ]
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: {"ebay": FakeSource(eff_cfg, "ebay", listings)}
    try:
        runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig
    row = conn.execute("SELECT COUNT(*) c FROM listing_duplicates WHERE status = 'pending'").fetchone()
    assert row["c"] == 1
