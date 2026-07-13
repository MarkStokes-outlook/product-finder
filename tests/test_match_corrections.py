from product_finder import catalogue, db, runner, sources
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities
from product_finder.web.app import create_app


def _setup(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "AI Server")
    item = ItemConfig(name="1000W PSU", terms=["1000w psu"], normal_price=140, target_deal_price=100)
    item_id = db.create_item(conn, project_id, item)
    return cfg, conn, item_id


def _seed_match(conn, item_id, title, price, external_id="e1"):
    listing = Listing(source="ebay", external_id=external_id, title=title, price=price,
                       url=f"https://example.com/{external_id}")
    listing_id, _ = db.upsert_listing(conn, listing)
    item = db._item_from_row(db.get_item(conn, item_id))
    from product_finder import scoring
    evaluation = scoring.evaluate(listing, item)
    match_id, _ = db.record_match(conn, listing_id, item_id, evaluation)
    return listing_id, match_id


# --- db.exclude_listing_from_item / listing_excluded_from_item ------------------


def test_exclude_listing_from_item_removes_the_match(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, match_id = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)

    db.exclude_listing_from_item(conn, listing_id, item_id)

    assert db.get_listing_match(conn, match_id) is None
    assert db.listing_excluded_from_item(conn, item_id, "ebay", "e1") is True


def test_listing_excluded_from_item_false_by_default(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, _ = _seed_match(conn, item_id, "Corsair RM1000x PSU", 100.0)
    assert db.listing_excluded_from_item(conn, item_id, "ebay", "e1") is False


def test_exclude_listing_from_item_is_idempotent(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, _ = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)
    db.exclude_listing_from_item(conn, listing_id, item_id)
    db.exclude_listing_from_item(conn, listing_id, item_id)  # no error, no dup
    assert db.listing_excluded_from_item(conn, item_id, "ebay", "e1") is True


def test_exclude_listing_from_item_only_affects_that_item(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    project_id = db.create_project(conn, "Other")
    other_item_id = db.create_item(conn, project_id, ItemConfig(name="Other item", terms=["x"]))
    listing_id, _ = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)
    db.exclude_listing_from_item(conn, listing_id, item_id)
    assert db.listing_excluded_from_item(conn, other_item_id, "ebay", "e1") is False


# --- runner.run_once honours the exclusion --------------------------------------


class FakeEbaySource(Source):
    name = "ebay"

    def __init__(self, cfg, listings):
        super().__init__(cfg)
        self._listings = listings

    def capabilities(self):
        return SourceCapabilities(automated=True, compliance="test fake")

    def search(self, term, item):
        return self._listings

    def get_item_details(self, external_id):
        return None

    def manual_links(self, item):
        return []


def _run_with_fake_ebay(cfg, conn, listings):
    fake = FakeEbaySource(cfg, listings)
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: {"ebay": fake}
    try:
        return runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig


def test_run_once_does_not_recreate_an_excluded_match(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, _ = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)
    db.exclude_listing_from_item(conn, listing_id, item_id)

    listing = Listing(source="ebay", external_id="e1", title="Corsair Type 4 HX Cables",
                       price=52.70, url="https://example.com/e1")
    _run_with_fake_ebay(cfg, conn, [listing])

    assert conn.execute(
        "SELECT COUNT(*) c FROM listing_matches WHERE item_id = ?", (item_id,)
    ).fetchone()["c"] == 0


def test_run_once_still_matches_a_non_excluded_listing(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing = Listing(source="ebay", external_id="e2", title="be quiet! 1000W PSU",
                       price=90.0, url="https://example.com/e2")
    _run_with_fake_ebay(cfg, conn, [listing])

    assert conn.execute(
        "SELECT COUNT(*) c FROM listing_matches WHERE item_id = ?", (item_id,)
    ).fetchone()["c"] == 1


# --- runner.reassess_item_matches also honours it -------------------------------


def test_reassess_removes_a_match_flagged_not_a_match(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, match_id = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)
    # Recreate the match directly (bypassing exclude_listing_from_item's own
    # deletion) to simulate the edge case reassess is meant to catch: an
    # exclusion recorded after the match, not through the same action.
    conn.execute(
        "INSERT INTO listing_match_exclusions (listing_id, item_id, created_at) VALUES (?, ?, ?)",
        (listing_id, item_id, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    item = db._item_from_row(db.get_item(conn, item_id))

    result = runner.reassess_item_matches(conn, item_id, item)

    assert result == {"rescored": 0, "excluded": 1}


# --- Web routes ------------------------------------------------------------------


def _client(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def test_unmatch_route_removes_match_and_redirects(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing_id, match_id = _seed_match(conn, item_id, "Corsair Type 4 HX Cables", 52.70)
    conn.close()
    client = _client(cfg)

    resp = client.post(f"/matches/{match_id}/unmatch", data={"next": "/"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"will no longer be matched" in resp.data
    conn = db.connect(cfg.db_path)
    assert db.get_listing_match(conn, match_id) is None
    assert db.listing_excluded_from_item(conn, item_id, "ebay", "e1") is True


def test_unmatch_route_404s_for_unknown_match(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.close()
    client = _client(cfg)
    resp = client.post("/matches/999/unmatch", data={"next": "/"})
    assert resp.status_code == 404


def test_exclude_term_route_adds_term_and_removes_matching_listing(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    _seed_match(conn, item_id, "Corsair Type 4 HX Power Supply Cables", 52.70, external_id="e1")
    _seed_match(conn, item_id, "be quiet! 1000W PSU", 90.0, external_id="e2")
    conn.close()
    client = _client(cfg)

    resp = client.post(
        f"/items/{item_id}/exclude-term", data={"term": "cables", "next": "/"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Added exclude term" in resp.data
    conn = db.connect(cfg.db_path)
    item = db._item_from_row(db.get_item(conn, item_id))
    assert "cables" in item.exclude_terms
    remaining = conn.execute(
        "SELECT l.title FROM listing_matches m JOIN listings l ON l.id = m.listing_id "
        "WHERE m.item_id = ?", (item_id,),
    ).fetchall()
    assert [r["title"] for r in remaining] == ["be quiet! 1000W PSU"]


def test_exclude_term_route_requires_a_term(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.close()
    client = _client(cfg)
    resp = client.post(
        f"/items/{item_id}/exclude-term", data={"term": "", "next": "/"},
        follow_redirects=True,
    )
    assert b"No exclude term given" in resp.data
