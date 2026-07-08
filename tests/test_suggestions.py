import json
from unittest import mock

from product_finder import auction_watch, db, runner, sources  # noqa: F401 (auction_watch unused, kept for parity)
from product_finder.config import AppConfig, EbayConfig, ItemConfig, SourcesConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities


def _setup(tmp_path):
    cfg = AppConfig(
        db_path=str(tmp_path / "t.db"),
        sources=SourcesConfig(ebay=EbayConfig(app_id="id", cert_id="secret")),
    )
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(name="Mitre Saw", terms=["mitre saw"]))
    return cfg, conn, item_id


# --- db.record_suggestion_sighting / approve / dismiss --------------------------


def test_first_sighting_creates_pending_suggestion(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    assert row["status"] == "pending"
    assert row["sighting_count"] == 1
    assert row["confidence"] == 70.0


def test_repeated_sighting_corroborates_and_raises_confidence(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/2")
    assert row["sighting_count"] == 2
    assert row["confidence"] == 78.0
    assert row["example_url"] == "https://x/2"  # most recent sighting


def test_dismissed_suggestion_is_never_reopened(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    db.dismiss_suggestion(conn, row["id"])
    again = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/2")
    assert again["status"] == "dismissed"
    assert again["sighting_count"] == 1  # untouched, not corroborated further


def test_auto_approve_threshold_promotes_suggestion_to_product(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.set_auto_approve_threshold(conn, 75.0)
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    assert row["status"] == "pending"  # confidence 70 < 75
    row2 = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/2")
    assert row2["status"] == "approved"  # confidence 78 >= 75
    products = db.list_products(conn, item_id)
    assert len(products) == 1
    assert products[0]["manufacturer"] == "Makita"


def test_no_auto_approve_by_default(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    for i in range(10):
        row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", f"https://x/{i}")
    assert row["status"] == "pending"
    assert row["confidence"] == 99.0
    assert db.list_products(conn, item_id) == []


def test_approve_suggestion_creates_product_with_match_terms(tmp_path):
    # Approving a suggestion creates/attaches a *global* product (see
    # docs/adr/0007-catalogue-globalization.md); match_terms live on this
    # item's item_products row, not the global products row.
    cfg, conn, item_id = _setup(tmp_path)
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    product_id = db.approve_suggestion(conn, row["id"])
    global_product = db.get_product(conn, product_id)
    assert global_product["manufacturer"] == "Makita"
    item_product = db.get_item_product(conn, item_id, product_id)
    assert json.loads(item_product["match_terms"]) == ["Makita LS0816F/2", "LS0816F/2"]
    suggestion = db.get_product_suggestion(conn, row["id"])
    assert suggestion["status"] == "approved"


def test_approve_suggestion_without_model_uses_single_match_term(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "", "https://x/1")
    product_id = db.approve_suggestion(conn, row["id"])
    item_product = db.get_item_product(conn, item_id, product_id)
    assert json.loads(item_product["match_terms"]) == ["Makita"]


def test_list_product_suggestions_filters_by_status(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    pending = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    dismissed = db.record_suggestion_sighting(conn, item_id, "DeWalt", "DWS773", "https://x/2")
    db.dismiss_suggestion(conn, dismissed["id"])
    assert [s["id"] for s in db.list_product_suggestions(conn, item_id, "pending")] == [pending["id"]]
    assert [s["id"] for s in db.list_product_suggestions(conn, item_id, "dismissed")] == [dismissed["id"]]


def test_deleting_item_deletes_its_suggestions(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    db.delete_item(conn, item_id)
    assert conn.execute("SELECT COUNT(*) c FROM product_suggestions").fetchone()["c"] == 0


# --- Normalisation wired into record_suggestion_sighting ------------------------


def test_casing_variants_merge_into_one_suggestion(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "WAGNER", "", "https://x/1")
    db.record_suggestion_sighting(conn, item_id, "Wagner", "", "https://x/2")
    row = db.record_suggestion_sighting(conn, item_id, "wagner", "", "https://x/3")

    rows = db.list_product_suggestions(conn, item_id)
    assert len(rows) == 1
    assert rows[0]["manufacturer"] == "Wagner"
    assert rows[0]["sighting_count"] == 3
    assert row["id"] == rows[0]["id"]


def test_raw_samples_track_distinct_variants_seen(tmp_path):
    import json

    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "WAGNER", "", "https://x/1")
    db.record_suggestion_sighting(conn, item_id, "Wagner", "", "https://x/2")
    db.record_suggestion_sighting(conn, item_id, "WAGNER", "", "https://x/3")  # repeat, no new variant

    row = db.list_product_suggestions(conn, item_id)[0]
    raw = json.loads(row["raw_samples"])
    assert {"manufacturer": "WAGNER", "model": ""} in raw
    assert {"manufacturer": "Wagner", "model": ""} in raw
    assert len(raw) == 2  # the repeat didn't add a third entry
    assert row["sighting_count"] == 3  # but still counted for confidence


def test_junk_manufacturer_sighting_is_rejected(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    result = db.record_suggestion_sighting(conn, item_id, "Does Not Apply", "", "https://x/1")
    assert result is None
    assert db.list_product_suggestions(conn, item_id) == []


def test_seller_name_sighting_is_rejected(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    result = db.record_suggestion_sighting(conn, item_id, "Tools Direct Store", "", "https://x/1")
    assert result is None
    assert db.list_product_suggestions(conn, item_id) == []


def test_model_null_variants_merge_together(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "Makita", "-", "https://x/1")
    db.record_suggestion_sighting(conn, item_id, "Makita", "N/A", "https://x/2")
    row = db.record_suggestion_sighting(conn, item_id, "Makita", "", "https://x/3")

    rows = db.list_product_suggestions(conn, item_id)
    assert len(rows) == 1
    assert rows[0]["model"] == ""
    assert rows[0]["sighting_count"] == 3
    assert row["id"] == rows[0]["id"]


# --- renormalize_pending_suggestions (one-time cleanup) --------------------------


def test_renormalize_merges_pre_existing_casing_duplicates(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    # Simulate suggestions created before normalisation existed: insert raw
    # casing-duplicate rows directly, bypassing the (now-normalising) API.
    now = "2026-07-03T00:00:00+00:00"
    for manufacturer, count in [("WAGNER", 2), ("Wagner", 1), ("wagner", 1)]:
        conn.execute(
            "INSERT INTO product_suggestions (item_id, manufacturer, model, confidence, "
            "sighting_count, source, example_url, status, first_seen, last_seen) "
            "VALUES (?, ?, '', 70.0, ?, 'ebay-structured', 'https://x/1', 'pending', ?, ?)",
            (item_id, manufacturer, count, now, now),
        )
    conn.commit()
    assert len(db.list_product_suggestions(conn, item_id)) == 3

    result = db.renormalize_pending_suggestions(conn)

    rows = db.list_product_suggestions(conn, item_id)
    assert len(rows) == 1
    assert rows[0]["manufacturer"] == "Wagner"
    assert rows[0]["sighting_count"] == 4  # 2 + 1 + 1 replayed
    assert result["before"] == 3
    assert result["after"] == 1


def test_renormalize_drops_pre_existing_junk(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    now = "2026-07-03T00:00:00+00:00"
    conn.execute(
        "INSERT INTO product_suggestions (item_id, manufacturer, model, confidence, "
        "sighting_count, source, example_url, status, first_seen, last_seen) "
        "VALUES (?, 'Does Not Apply', '', 70.0, 1, 'ebay-structured', 'https://x/1', 'pending', ?, ?)",
        (item_id, now, now),
    )
    conn.commit()

    result = db.renormalize_pending_suggestions(conn)

    assert db.list_product_suggestions(conn, item_id) == []
    assert result["rejected_outright"] == 1


def test_renormalize_leaves_approved_and_dismissed_alone(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    approved = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    db.approve_suggestion(conn, approved["id"])
    dismissed = db.record_suggestion_sighting(conn, item_id, "Bosch", "GKT55", "https://x/2")
    db.dismiss_suggestion(conn, dismissed["id"])

    db.renormalize_pending_suggestions(conn)

    assert db.get_product_suggestion(conn, approved["id"])["status"] == "approved"
    assert db.get_product_suggestion(conn, dismissed["id"])["status"] == "dismissed"


# --- runner.py wiring -------------------------------------------------------------


class FakeEbaySource(Source):
    name = "ebay"

    def __init__(self, cfg, listings, details=None):
        super().__init__(cfg)
        self._listings = listings
        self._details = details or {}

    def capabilities(self):
        return SourceCapabilities(
            automated=True, compliance="test fake", supports_enrichment=True
        )

    def search(self, term, item):
        return self._listings

    def get_item_details(self, external_id):
        return self._details.get(external_id)

    def manual_links(self, item):
        return []


def _run_with_fake_ebay(cfg, conn, listings, details=None):
    fake = FakeEbaySource(cfg, listings, details)
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: {"ebay": fake}
    try:
        return runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig


def test_unmatched_listing_with_structured_brand_creates_suggestion(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing = Listing(source="ebay", external_id="e1", title="Makita LS0816F/2 mitre saw",
                       price=250.0, url="https://x/e1")
    _run_with_fake_ebay(cfg, conn, [listing], details={"e1": {"brand": "Makita", "model": "LS0816F/2"}})

    suggestions = db.list_product_suggestions(conn, item_id)
    assert len(suggestions) == 1
    assert suggestions[0]["manufacturer"] == "Makita"
    assert suggestions[0]["example_url"] == "https://x/e1"


def test_listing_only_brand_checked_once(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing = Listing(source="ebay", external_id="e1", title="Makita LS0816F/2 mitre saw",
                       price=250.0, url="https://x/e1")
    details = {"e1": {"brand": "Makita", "model": "LS0816F/2"}}
    _run_with_fake_ebay(cfg, conn, [listing], details=details)
    _run_with_fake_ebay(cfg, conn, [listing], details=details)  # rescan

    suggestions = db.list_product_suggestions(conn, item_id)
    assert len(suggestions) == 1
    assert suggestions[0]["sighting_count"] == 1  # not incremented on rescan


def test_no_suggestion_when_no_structured_brand(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    listing = Listing(source="ebay", external_id="e1", title="Mitre saw, no brand info",
                       price=90.0, url="https://x/e1")
    _run_with_fake_ebay(cfg, conn, [listing], details={})

    assert db.list_product_suggestions(conn, item_id) == []
    listing_row = conn.execute("SELECT brand_checked FROM listings WHERE external_id = 'e1'").fetchone()
    assert listing_row["brand_checked"] == 1


def test_ollama_fallback_creates_suggestion_when_no_structured_brand(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    cfg.ollama.enabled = True
    listing = Listing(source="ebay", external_id="e1", title="mitre saw, barely used",
                       price=90.0, url="https://x/e1")
    with mock.patch(
        "product_finder.runner.extraction.extract_brand_model",
        return_value={"brand": "Makita", "model": "LS0816F/2"},
    ) as extract:
        _run_with_fake_ebay(cfg, conn, [listing], details={})

    extract.assert_called_once()
    suggestions = db.list_product_suggestions(conn, item_id)
    assert len(suggestions) == 1
    assert suggestions[0]["manufacturer"] == "Makita"
    assert suggestions[0]["source"] == "ollama"


def test_ollama_fallback_not_used_when_structured_brand_present(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    cfg.ollama.enabled = True
    listing = Listing(source="ebay", external_id="e1", title="Makita LS0816F/2 mitre saw",
                       price=250.0, url="https://x/e1")
    with mock.patch(
        "product_finder.runner.extraction.extract_brand_model"
    ) as extract:
        _run_with_fake_ebay(cfg, conn, [listing], details={"e1": {"brand": "Makita", "model": "LS0816F/2"}})

    extract.assert_not_called()
    suggestions = db.list_product_suggestions(conn, item_id)
    assert len(suggestions) == 1
    assert suggestions[0]["source"] == "ebay-structured"


def test_no_suggestion_when_ollama_extraction_finds_nothing(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    cfg.ollama.enabled = True
    listing = Listing(source="ebay", external_id="e1", title="mystery tool, no branding",
                       price=40.0, url="https://x/e1")
    with mock.patch(
        "product_finder.runner.extraction.extract_brand_model", return_value=None
    ):
        _run_with_fake_ebay(cfg, conn, [listing], details={})

    assert db.list_product_suggestions(conn, item_id) == []


def test_no_suggestion_check_when_listing_already_matches_a_product(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.create_product(conn, item_id, "Makita", "LS0816F/2", ["makita ls0816f/2"], None, None, None)
    listing = Listing(source="ebay", external_id="e1", title="Makita LS0816F/2 mitre saw",
                       price=250.0, url="https://x/e1")
    _run_with_fake_ebay(cfg, conn, [listing], details={"e1": {"brand": "Makita", "model": "LS0816F/2"}})

    # Already resolved via catalogue.match() — no wasted get_item_details call,
    # no duplicate/competing suggestion for a product we already have.
    assert db.list_product_suggestions(conn, item_id) == []
