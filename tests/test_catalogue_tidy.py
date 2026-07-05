"""Catalogue tidy-up: case-insensitive suggestion merging, junk-model
normalisation, duplicate-product prevention and merging, the catalogue-tidy
CLI command, and the global /catalogue review page.
"""

import json

import pytest

from product_finder import catalogue, cli, db
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Evaluation, Listing
from product_finder.web.app import create_app


def _setup(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(name="Mitre Saw", terms=["mitre saw"]))
    return cfg, conn, item_id


# --- Suggestion normalisation -------------------------------------------------


def test_unknown_brand_casing_variants_merge_case_insensitively(tmp_path):
    # DEWALT isn't in BRAND_ALIASES — the case-insensitive lookup must merge
    # variants anyway, keeping the first-recorded casing.
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "DEWALT", "DWS774", "https://x/1")
    row = db.record_suggestion_sighting(conn, item_id, "DeWalt", "dws774", "https://x/2")
    assert row["sighting_count"] == 2
    assert row["manufacturer"] == "DEWALT"  # first-seen casing adopted
    assert row["model"] == "DWS774"
    pending = db.list_product_suggestions(conn, item_id)
    assert len(pending) == 1


def test_placeholder_models_collapse_to_brand_only():
    assert catalogue.normalize_model("NOT FOUND") == ""
    assert catalogue.normalize_model("None") == ""
    assert catalogue.normalize_model("see description") == ""
    assert catalogue.normalize_model("DWS774") == "DWS774"  # real models untouched


def test_placeholder_model_sighting_corroborates_brand_only_suggestion(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    db.record_suggestion_sighting(conn, item_id, "Herman Miller", "", "https://x/1")
    row = db.record_suggestion_sighting(conn, item_id, "Herman Miller", "NOT FOUND", "https://x/2")
    assert row["sighting_count"] == 2
    assert row["model"] == ""


# --- Duplicate products: prevention -------------------------------------------


def test_create_product_returns_existing_on_case_insensitive_duplicate(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    first = db.create_product(conn, item_id, "DEWALT", "DW088K-XJ", ["dw088k"], None, None, None)
    second = db.create_product(conn, item_id, "DeWalt", "dw088k-xj", ["other"], None, None, None)
    assert second == first
    assert len(db.list_products(conn, item_id)) == 1


def test_create_product_allows_same_model_on_different_item(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    other_item = db.create_item(
        conn, db.create_project(conn, "Other"), ItemConfig(name="Laser", terms=["laser"])
    )
    a = db.create_product(conn, item_id, "DEWALT", "DW088K", ["dw088k"], None, None, None)
    b = db.create_product(conn, other_item, "DEWALT", "DW088K", ["dw088k"], None, None, None)
    assert a != b


# --- Duplicate products: merging ----------------------------------------------


def _match(conn, listing_id, item_id, product_id):
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="A", flags=[], margin_abs=100.0, margin_pct=40.0,
                   under_target=False, deal_score=55.0),
        product_id=product_id,
    )


def test_merge_products_repoints_everything_and_unions_terms(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    keep = db.create_product(conn, item_id, "DEWALT", "DW088K", ["dw088k"], None, None, None)
    dup = db.create_product(conn, item_id, "DEWALT", "DW088K-XJ", ["dw088k", "dw088k-xj"],
                            None, 120.0, 60.0)

    listing_id, _ = db.upsert_listing(conn, Listing(
        source="ebay", external_id="E1", title="DeWalt DW088K-XJ laser",
        price=45.0, url="https://x/e1"))
    _match(conn, listing_id, item_id, dup)
    db.record_price_observation(conn, dup, 45.0, "ebay")

    db.merge_products(conn, keep, dup)

    assert db.get_product(conn, dup) is None
    kept = db.get_product(conn, keep)
    assert json.loads(kept["match_terms"]) == ["dw088k", "dw088k-xj"]  # union, no dupes
    assert kept["typical_new_price"] == 120.0  # NULL filled from duplicate
    assert kept["target_deal_price"] == 60.0
    assert kept["typical_used_price"] == 45.0  # recomputed over moved observations
    match = conn.execute("SELECT product_id FROM listing_matches WHERE listing_id = ?",
                         (listing_id,)).fetchone()
    assert match["product_id"] == keep
    obs = conn.execute("SELECT product_id FROM product_price_observations").fetchall()
    assert all(o["product_id"] == keep for o in obs)


def test_merge_products_keeps_existing_prices_over_duplicates(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    keep = db.create_product(conn, item_id, "NILFISK", "128500724", [], None, 150.0, 80.0)
    dup_id = conn.execute(
        "INSERT INTO products (item_id, manufacturer, model, match_terms, typical_new_price) "
        "VALUES (?, 'Nilfisk', '128500724', '[]', 999.0)", (item_id,)
    ).lastrowid
    db.merge_products(conn, keep, dup_id)
    kept = db.get_product(conn, keep)
    assert kept["typical_new_price"] == 150.0  # keeper's value wins


def test_merge_products_rejects_self_or_missing(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    keep = db.create_product(conn, item_id, "A", "B", [], None, None, None)
    with pytest.raises(ValueError):
        db.merge_products(conn, keep, keep)
    with pytest.raises(ValueError):
        db.merge_products(conn, keep, 9999)


def test_dedupe_products_sweeps_pre_guard_duplicates(tmp_path):
    # Simulate a pre-guard database: insert duplicates directly.
    cfg, conn, item_id = _setup(tmp_path)
    for manufacturer in ("DEWALT", "DeWalt", "Dewalt"):
        conn.execute(
            "INSERT INTO products (item_id, manufacturer, model, match_terms) "
            "VALUES (?, ?, 'DW088K-XJ', '[]')", (item_id, manufacturer),
        )
    conn.commit()
    assert db.dedupe_products(conn) == 2
    products = db.list_products(conn, item_id)
    assert len(products) == 1
    assert products[0]["manufacturer"] == "DEWALT"  # oldest row kept
    assert db.dedupe_products(conn) == 0  # idempotent


def test_catalogue_tidy_cli(tmp_path, capsys):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute(
        "INSERT INTO products (item_id, manufacturer, model, match_terms) "
        "VALUES (?, 'HARIBO', '465137', '[]'), (?, 'Haribo', '465137', '[]')",
        (item_id, item_id),
    )
    conn.commit()
    conn.close()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"db_path: {cfg.db_path}\nprojects: []\n")
    assert cli.main(["-c", str(config_path), "catalogue-tidy"]) == 0
    out = capsys.readouterr().out
    assert "1 exact duplicate(s) folded away" in out


# --- Suspect products (accessories approved as products) -------------------------


def _listing_match(conn, item_id, product_id, external_id, title, price):
    listing_id, _ = db.upsert_listing(conn, Listing(
        source="ebay", external_id=external_id, title=title, price=price,
        url=f"https://x/{external_id}"))
    _match(conn, listing_id, item_id, product_id)
    return listing_id


def test_part_number_shapes():
    assert catalogue.looks_like_part_number("2371069")        # Wagner article no.
    assert catalogue.looks_like_part_number("2.863-314.0")    # Kärcher part style
    assert not catalogue.looks_like_part_number("DWS774")
    assert not catalogue.looks_like_part_number("i7-4790s")
    assert not catalogue.looks_like_part_number("VC3012M")


def test_accessory_title_share_word_boundaries():
    assert catalogue.accessory_title_share(["Festool dust bags x5"]) == 1.0
    # "tipped" must not fire the "tip" keyword.
    assert catalogue.accessory_title_share(["Carbide tipped mitre saw"]) == 0.0


def test_accessory_priced_product_is_suspect(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Festool", "204308", ["204308"], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Festool 204308 spares", 12.0)
    _listing_match(conn, item_id, product, "E2", "204308 for Festool CT MINI", 15.0)
    suspects = db.find_suspect_products(conn)
    assert len(suspects) == 1
    assert suspects[0]["id"] == product
    assert any("average" in r for r in suspects[0]["reasons"])


def test_accessory_titled_product_is_suspect_even_at_normal_price(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 100 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Makita", "W107418353", [], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Makita filter bags pack", 60.0)
    _listing_match(conn, item_id, product, "E2", "Makita replacement hose", 70.0)
    suspects = db.find_suspect_products(conn)
    assert len(suspects) == 1
    assert any("accessory" in r for r in suspects[0]["reasons"])


def test_real_product_is_not_suspect(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 450 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Makita", "LS1019L", ["ls1019l"], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Makita LS1019L sliding mitre saw", 320.0)
    _listing_match(conn, item_id, product, "E2", "Makita LS1019L saw, great condition", 300.0)
    assert db.find_suspect_products(conn) == []


def test_single_match_is_never_accused(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Festool", "204308", [], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Festool dust bags", 12.0)
    assert db.find_suspect_products(conn) == []


def test_archived_product_not_listed(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Festool", "204308", [], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Festool dust bags", 12.0)
    _listing_match(conn, item_id, product, "E2", "Festool dust bags again", 14.0)
    db.set_product_archived(conn, product, True)
    assert db.find_suspect_products(conn) == []


# --- Suggestion triage (evidence-based verdicts on the pending queue) ------------


def _suggest(conn, item_id, manufacturer, model, sightings=2):
    row = None
    for i in range(sightings):
        row = db.record_suggestion_sighting(conn, item_id, manufacturer, model, f"https://x/{i}")
    return row


def _verdicts(conn):
    return {
        (s["manufacturer"], s["model"]): s["verdict"]
        for s in db.triage_pending_suggestions(conn)
    }


def test_triage_strong_when_model_priced_like_the_item(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 450 WHERE id = ?", (item_id,))
    _listing_match(conn, item_id, None, "E1", "Makita LS1019L sliding mitre saw", 320.0)
    _listing_match(conn, item_id, None, "E2", "Makita LS1019L mitre saw excellent", 300.0)
    _suggest(conn, item_id, "Makita", "LS1019L")
    assert _verdicts(conn)[("Makita", "LS1019L")] == db.TRIAGE_STRONG


def test_triage_accessory_by_wording_and_by_price(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    # Accessory wording at accessory prices.
    _listing_match(conn, item_id, None, "E1", "Dust bags 204308 for Festool CT MINI", 12.0)
    _listing_match(conn, item_id, None, "E2", "Festool 204308 dust bags pack", 15.0)
    _suggest(conn, item_id, "Festool", "204308")
    # Neutral wording but part-level prices.
    _listing_match(conn, item_id, None, "E3", "Makita W107418353 genuine", 20.0)
    _listing_match(conn, item_id, None, "E4", "Makita W107418353 new", 22.0)
    _suggest(conn, item_id, "Makita", "W107418353")
    verdicts = _verdicts(conn)
    assert verdicts[("Festool", "204308")] == db.TRIAGE_ACCESSORY
    assert verdicts[("Makita", "W107418353")] == db.TRIAGE_ACCESSORY


def test_triage_unclear_without_evidence_and_brand_only(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    _suggest(conn, item_id, "Bosch", "GCM800")  # no listings mention it
    _suggest(conn, item_id, "DEWALT", "")       # brand only
    verdicts = _verdicts(conn)
    assert verdicts[("Bosch", "GCM800")] == db.TRIAGE_UNCLEAR
    assert verdicts[("DEWALT", "")] == db.TRIAGE_BRAND_ONLY


def test_triage_needs_two_evidence_listings(tmp_path):
    # One cheap listing must not condemn a suggestion — same "no evidence,
    # no accusation" rule as find_suspect_products.
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    _listing_match(conn, item_id, None, "E1", "Festool 204308 dust bags", 12.0)
    _suggest(conn, item_id, "Festool", "204308")
    assert _verdicts(conn)[("Festool", "204308")] == db.TRIAGE_UNCLEAR


def test_triage_word_boundary_on_model(tmp_path):
    # Model "774" must not take evidence from "DWS774" titles.
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    _listing_match(conn, item_id, None, "E1", "DEWALT DWS774 mitre saw", 250.0)
    _listing_match(conn, item_id, None, "E2", "DEWALT DWS774 saw boxed", 260.0)
    _suggest(conn, item_id, "DEWALT", "774")
    _suggest(conn, item_id, "DEWALT", "DWS774")
    verdicts = _verdicts(conn)
    assert verdicts[("DEWALT", "774")] == db.TRIAGE_UNCLEAR       # no true mentions
    assert verdicts[("DEWALT", "DWS774")] == db.TRIAGE_STRONG


# --- Knowledge-only products (wanted=0: identified, priced, never surfaced) ------


def test_unwanted_product_match_hidden_from_all_deal_surfaces(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    product = db.create_product(conn, item_id, "Intel", "i7-4790K", ["i7-4790k"], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Intel i7-4790K CPU", 59.0)
    assert len(db.query_matches(conn)) == 1

    db.set_product_wanted(conn, product, False)
    # Read-time gating: effect is immediate, no rescan required.
    assert db.query_matches(conn) == []
    assert db.project_top_picks(conn) == {}
    assert db.dashboard_stats(conn)["clean_matches"] == 0
    assert db.project_summaries(conn)[0]["match_count"] == 0

    db.set_product_wanted(conn, product, True)
    assert len(db.query_matches(conn)) == 1  # fully reversible


def test_unwanted_product_still_matches_and_accumulates_price_history(tmp_path):
    # The whole point: identification and pricing knowledge continue.
    cfg, conn, item_id = _setup(tmp_path)
    product = db.create_product(conn, item_id, "Intel", "i7-4790K", ["i7-4790k"], None, None, None)
    db.set_product_wanted(conn, product, False)

    products = db.list_products_for_matching(conn, item_id)
    assert len(products) == 1  # unlike archived, still offered to catalogue.match
    assert products[0].wanted is False
    assert catalogue.match("intel i7-4790k for sale", products) is not None

    db.record_price_observation(conn, product, 55.0, "ebay")
    assert db.get_product(conn, product)["typical_used_price"] == 55.0


def test_runner_skips_alert_for_unwanted_product(tmp_path, monkeypatch):
    from product_finder import runner, sources
    from product_finder.config import ExtraSourceConfig
    from product_finder.sources.base import Source, SourceCapabilities

    class Fake(Source):
        def capabilities(self):
            return SourceCapabilities(automated=True, compliance="test fake")

        def search(self, term, item):
            return [Listing(source="fake", external_id="F1", title="Intel i7-4790K CPU",
                            price=59.0, url="https://x/f1")]

    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    cfg.sources.extra = [ExtraSourceConfig(name="fake", type="rss", url="https://x/{term}")]
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Gaming PC")
    item_id = db.create_item(conn, project_id, ItemConfig(name="CPU", terms=["cpu"],
                                                          normal_price=250))
    product = db.create_product(conn, item_id, "Intel", "i7-4790K", ["i7-4790k"], None, None, None)
    db.set_product_wanted(conn, product, False)
    monkeypatch.setattr(sources, "build_registry", lambda eff_cfg: {"fake": Fake(cfg)})

    alerts = runner.run_once(cfg, conn)
    assert alerts == []  # identified, priced — but never alerted
    match = conn.execute("SELECT product_id FROM listing_matches").fetchone()
    assert match["product_id"] == product  # match recorded with identity intact
    obs = conn.execute("SELECT COUNT(*) n FROM product_price_observations").fetchone()
    assert obs["n"] == 1  # price history still accumulated


def test_unwanted_product_no_longer_accused_as_suspect(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Intel", "SR00B", [], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Intel SR00B CPU", 17.0)
    _listing_match(conn, item_id, product, "E2", "Intel SR00B processor", 18.0)
    assert len(db.find_suspect_products(conn)) == 1
    db.set_product_wanted(conn, product, False)
    assert db.find_suspect_products(conn) == []  # decision made, stop asking


# --- Global /catalogue review page ----------------------------------------------


@pytest.fixture
def web(tmp_path):
    cfg, conn, item_id = _setup(tmp_path)
    app = create_app(cfg)
    app.config["TESTING"] = True
    return cfg, conn, item_id, app.test_client()


def test_catalogue_page_groups_by_verdict(web):
    cfg, conn, item_id, client = web
    db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    db.record_suggestion_sighting(conn, item_id, "DEWALT", "", "https://x/2")
    resp = client.get("/catalogue")
    assert resp.status_code == 200
    assert b"Mitre Saw" in resp.data
    assert b"Workshop" in resp.data
    assert b"LS0816F/2" in resp.data
    assert b"brand only" in resp.data     # model-less rows are labelled
    assert b"Needs a human" in resp.data  # verdict bucket headings
    assert b"Brand only" in resp.data


def test_catalogue_page_empty_state(web):
    cfg, conn, item_id, client = web
    resp = client.get("/catalogue")
    assert b"Nothing waiting for review" in resp.data


def test_bulk_approve_skips_brand_only_suggestions(web):
    cfg, conn, item_id, client = web
    with_model = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    brand_only = db.record_suggestion_sighting(conn, item_id, "DEWALT", "", "https://x/2")
    resp = client.post("/suggestions/bulk-approve", data={
        "suggestion_ids": [str(with_model["id"]), str(brand_only["id"])],
        "next": "/catalogue",
    }, follow_redirects=True)
    assert b"Approved 1 suggestion(s)" in resp.data
    assert b"Skipped 1 brand-only" in resp.data
    products = db.list_products(conn, item_id)
    assert len(products) == 1
    assert products[0]["model"] == "LS0816F/2"
    # The brand-only suggestion is still pending — not silently dismissed.
    assert db.get_product_suggestion(conn, brand_only["id"])["status"] == "pending"


def test_individual_approve_of_brand_only_still_allowed(web):
    cfg, conn, item_id, client = web
    brand_only = db.record_suggestion_sighting(conn, item_id, "DEWALT", "", "https://x/1")
    resp = client.post(f"/suggestions/{brand_only['id']}/approve",
                       data={"next": "/catalogue"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/catalogue")
    assert len(db.list_products(conn, item_id)) == 1


def test_catalogue_page_shows_suspects_and_bulk_archive_works(web):
    cfg, conn, item_id, client = web
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Festool", "204308", ["204308"], None, None, None)
    _listing_match(conn, item_id, product, "E1", "Festool 204308 dust bags", 12.0)
    _listing_match(conn, item_id, product, "E2", "Dust bags for Festool", 15.0)
    conn.commit()
    resp = client.get("/catalogue")
    assert b"Suspect products" in resp.data
    assert b"Festool" in resp.data

    resp = client.post("/products/bulk-archive", data={
        "product_ids": [str(product)], "next": "/catalogue",
    }, follow_redirects=True)
    assert b"Archived 1 product(s)" in resp.data
    assert db.get_product(conn, product)["archived"] == 1
    # Archived: no longer offered to catalogue.match, no longer accused.
    assert db.list_products_for_matching(conn, item_id) == []
    assert b"Suspect products" not in client.get("/catalogue").data


def test_bulk_knowledge_only_and_toggle_route(web):
    cfg, conn, item_id, client = web
    conn.execute("UPDATE items SET normal_price = 400 WHERE id = ?", (item_id,))
    product = db.create_product(conn, item_id, "Intel", "SR00B", [], None, None, None)
    resp = client.post("/products/bulk-knowledge-only", data={
        "product_ids": [str(product)], "next": "/catalogue",
    }, follow_redirects=True)
    assert b"knowledge-only" in resp.data
    assert db.get_product(conn, product)["wanted"] == 0

    resp = client.post(f"/products/{product}/toggle-wanted", follow_redirects=False)
    assert db.get_product(conn, product)["wanted"] == 1
    assert f"/items/{item_id}/edit" in resp.headers["Location"]


def test_item_form_shows_knowledge_only_badge(web):
    cfg, conn, item_id, client = web
    product = db.create_product(conn, item_id, "Intel", "SR00B", [], None, None, None)
    db.set_product_wanted(conn, product, False)
    resp = client.get(f"/items/{item_id}/edit")
    assert b"knowledge only" in resp.data
    assert b"Surface deals" in resp.data  # the un-toggle action


def test_suggestion_redirect_rejects_offsite_next(web):
    cfg, conn, item_id, client = web
    s = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://x/1")
    resp = client.post(f"/suggestions/{s['id']}/dismiss",
                       data={"next": "//evil.example/phish"})
    # Falls back to the item edit page instead of following the bad target.
    assert f"/items/{item_id}/edit" in resp.headers["Location"]
