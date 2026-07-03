import json

import pytest

from product_finder import db
from product_finder.config import AppConfig
from product_finder.models import Evaluation, Listing
from product_finder.web.app import create_app


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def client(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def seed_match(cfg, flags=None, grade="A", score=85.0):
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Coachhouse Tools")
    from product_finder.config import ItemConfig

    item_id = db.create_item(
        conn,
        project_id,
        ItemConfig(name="Track Saw", terms=["track saw"], normal_price=500,
                   target_deal_price=300, priority="high"),
    )
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="E1", title="Makita SP6000 saw",
                price=245.0, url="https://example.com/1"),
    )
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade=grade, flags=flags or [], margin_abs=255.0,
                   margin_pct=51.0, under_target=True, deal_score=score),
    )
    conn.commit()
    conn.close()
    return project_id, item_id


# --- Dashboard ---------------------------------------------------------------


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No projects yet" in resp.data


def test_dashboard_with_data(cfg, client):
    seed_match(cfg)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Coachhouse Tools" in resp.data
    assert b"Makita SP6000 saw" in resp.data
    assert b"under target" in resp.data


def test_dashboard_warning_section(cfg, client):
    seed_match(cfg, flags=["faulty"], grade="spares/repair", score=20.0)
    resp = client.get("/")
    assert b"faulty" in resp.data


def test_dashboard_hero_shows_best_deal(cfg, client):
    seed_match(cfg, score=90.0)
    resp = client.get("/")
    assert b"Best deals right now" in resp.data
    assert b"deal-card" in resp.data
    assert b"Makita SP6000 saw" in resp.data


def test_dashboard_project_card_shows_top_pick_preview(cfg, client):
    seed_match(cfg, score=90.0)
    resp = client.get("/")
    assert b"project-pick" in resp.data
    assert b"Still watching" not in resp.data  # has a match, not idle


def test_dashboard_project_card_idle_state_when_no_matches(cfg, client):
    conn = db.connect(cfg.db_path)
    db.create_project(conn, "Quiet Project")
    conn.commit()
    resp = client.get("/")
    assert b"Still watching" in resp.data


# --- Dashboard live refresh (no manual trigger, no full page reload) -----------


def test_no_manual_run_trigger_in_ui(client):
    resp = client.get("/")
    assert b"Run search now" not in resp.data
    assert client.get("/run").status_code == 404


def test_api_status_no_activity_yet(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"last_activity": None}


def test_api_status_reflects_latest_listing_activity(cfg, client):
    seed_match(cfg)
    resp = client.get("/api/status")
    data = resp.get_json()
    assert data["last_activity"] is not None


def test_dashboard_live_fragment_has_no_layout(cfg, client):
    seed_match(cfg)
    resp = client.get("/dashboard/live")
    assert resp.status_code == 200
    assert b"Makita SP6000 saw" in resp.data
    assert b"<nav>" not in resp.data  # fragment only, not the full page shell
    assert b"<html" not in resp.data


def test_dashboard_page_embeds_live_fragment_and_polls(cfg, client):
    seed_match(cfg)
    resp = client.get("/")
    assert b'id="dashboard-live"' in resp.data
    assert b"Makita SP6000 saw" in resp.data  # fragment included on first load too
    assert b"setInterval" in resp.data
    assert b"/api/status" in resp.data
    assert b"/dashboard/live" in resp.data


# --- Sources page ---------------------------------------------------------------


def test_sources_page_lists_builtin_and_extra(cfg, client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert b"eBay" in resp.data
    assert b"Gumtree" in resp.data
    assert b"Facebook Marketplace" in resp.data


def test_sources_page_shows_new_extra_source_no_import_needed(tmp_path, client):
    from product_finder.config import ExtraSourceConfig, SourcesConfig
    from product_finder.web.app import create_app

    cfg = AppConfig(
        db_path=str(tmp_path / "test.db"),
        sources=SourcesConfig(extra=[
            ExtraSourceConfig(name="newsite", type="links", url="https://n.example/?q={term}",
                               label="New Site"),
        ]),
    )
    app = create_app(cfg)
    app.config["TESTING"] = True
    resp = app.test_client().get("/sources")
    assert b"New Site" in resp.data


def test_source_toggle_disables_and_re_enables(cfg, client):
    resp = client.post("/sources/gumtree/toggle", follow_redirects=True)
    assert b"disabled" in resp.data

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT enabled FROM source_settings WHERE name='gumtree'").fetchone()
    assert row["enabled"] == 0

    # Reflected immediately on the dashboard's source list — no restart needed.
    resp = client.get("/")
    assert b"gumtree" not in resp.data

    client.post("/sources/gumtree/toggle")
    row = conn.execute("SELECT enabled FROM source_settings WHERE name='gumtree'").fetchone()
    assert row["enabled"] == 1


def test_source_toggle_unknown_name_404s(client):
    resp = client.post("/sources/not-a-real-source/toggle")
    assert resp.status_code == 404


def test_source_ebay_keys_save_and_prefill(cfg, client):
    resp = client.post(
        "/sources/ebay/keys",
        data={"app_id": "app123", "cert_id": "cert456", "env": "sandbox"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"app123" in resp.data
    assert b"sandbox" in resp.data

    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM source_settings WHERE name='ebay'").fetchone()
    assert row["ebay_app_id"] == "app123"
    assert row["ebay_cert_id"] == "cert456"
    assert row["ebay_env"] == "sandbox"


# --- Project detail (live per-project dashboard, replaces the HTML report) -----


def test_project_detail_shows_items_grouped_with_matches(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    assert b"Track Saw" in resp.data
    assert b"Makita SP6000 saw" in resp.data
    assert b"Target deal price" in resp.data or b"Target deal price:" in resp.data


def test_project_detail_shows_item_with_no_matches_yet(cfg, client):
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Empty Project")
    from product_finder.config import ItemConfig
    db.create_item(conn, project_id, ItemConfig(name="Widget", terms=["widget"]))
    conn.commit()
    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    assert b"Widget" in resp.data
    assert b"Nothing here yet" in resp.data


def test_project_detail_404_for_unknown_project(client):
    assert client.get("/projects/999").status_code == 404
    assert client.get("/projects/999/live").status_code == 404


def test_project_detail_live_fragment_has_no_layout(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get(f"/projects/{project_id}/live")
    assert resp.status_code == 200
    assert b"Makita SP6000 saw" in resp.data
    assert b"<nav>" not in resp.data
    assert b"<html" not in resp.data


def test_project_detail_page_polls_for_live_updates(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get(f"/projects/{project_id}")
    assert b'id="project-live"' in resp.data
    assert b"setInterval" in resp.data
    assert f"/projects/{project_id}/live".encode() in resp.data


def test_project_detail_includes_manual_links_for_non_automated_sources(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get(f"/projects/{project_id}")
    assert b"Manual searches" in resp.data
    assert b"Gumtree" in resp.data


def test_dashboard_project_card_links_to_project_detail(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get("/")
    assert f'/projects/{project_id}"'.encode() in resp.data


def test_projects_page_name_links_to_project_detail(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get("/projects")
    assert f'/projects/{project_id}"'.encode() in resp.data


# --- Report exports removed (superseded by the live project dashboard) --------


def test_report_routes_gone(cfg, client):
    assert client.get("/reports/html").status_code == 404
    assert client.get("/reports/md").status_code == 404


def test_no_report_mentions_anywhere(cfg, client):
    project_id, _ = seed_match(cfg)
    for url in ("/", f"/projects/{project_id}"):
        resp = client.get(url)
        assert b"HTML report" not in resp.data
        assert b"Markdown report" not in resp.data


# --- Items/Listings folded into the project page — no standalone pages --------


def test_items_and_listings_nav_removed(client):
    resp = client.get("/")
    assert b'>Items<' not in resp.data
    assert b'>Listings<' not in resp.data


def test_items_and_listings_routes_gone(client):
    assert client.get("/items").status_code == 404
    assert client.get("/listings").status_code == 404


# --- Project CRUD --------------------------------------------------------------


def test_project_create(cfg, client):
    resp = client.post("/projects/new", data={"name": "Homelab"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Homelab" in resp.data
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM projects WHERE name = 'Homelab'").fetchone()
    assert row is not None
    assert row["slug"] == "homelab"


def test_project_create_requires_name(client):
    resp = client.post("/projects/new", data={"name": ""}, follow_redirects=True)
    assert b"required" in resp.data


def test_project_edit(cfg, client):
    project_id, _ = seed_match(cfg)
    client.post(f"/projects/{project_id}/edit", data={"name": "Renamed"})
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()["name"] == "Renamed"


def test_project_create_with_restricted_sources(cfg, client):
    resp = client.post(
        "/projects/new",
        data={"name": "Power Tools", "source_ebay": "1", "source_gumtree": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM projects WHERE name = 'Power Tools'").fetchone()
    assert json.loads(row["sources"]) == ["ebay", "gumtree"]


def test_project_edit_restricts_sources_and_narrows_item_search(cfg, client):
    project_id, item_id = seed_match(cfg)
    client.post(
        f"/projects/{project_id}/edit",
        data={"name": "Coachhouse Tools", "source_ebay": "1"},
    )
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT sources FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert json.loads(row["sources"]) == ["ebay"]

    from product_finder import runner

    project_cfg = next(p for p in db.load_project_configs(conn) if p.id == project_id)
    item_cfg = project_cfg.items[0]
    eff_cfg = db.effective_config(conn, cfg)
    assert runner.item_sources(item_cfg, eff_cfg, project_cfg) == ["ebay"]


def test_project_form_shows_existing_source_restriction(cfg, client):
    project_id, _ = seed_match(cfg)
    client.post(f"/projects/{project_id}/edit", data={"name": "Coachhouse Tools", "source_ebay": "1"})
    resp = client.get(f"/projects/{project_id}/edit")
    text = resp.data.decode()
    ebay_block = text.split('name="source_ebay"')[1].split(">")[0]
    gumtree_block = text.split('name="source_gumtree"')[1].split(">")[0]
    assert "checked" in ebay_block
    assert "checked" not in gumtree_block


def test_project_archive_and_delete(cfg, client):
    project_id, item_id = seed_match(cfg)
    client.post(f"/projects/{project_id}/archive")
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT archived FROM projects WHERE id = ?", (project_id,)
    ).fetchone()["archived"] == 1
    conn.close()

    client.post(f"/projects/{project_id}/delete")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM listing_matches").fetchone()["c"] == 0


# --- Item CRUD -----------------------------------------------------------------


def test_item_create(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.post(
        "/items/new",
        data={
            "project_id": project_id,
            "name": "Mitre Saw",
            "terms": "sliding mitre saw\nDeWalt mitre saw",
            "exclude_terms": "toy",
            "max_price": "300",
            "normal_price": "350",
            "target_deal_price": "200",
            "priority": "high",
            "notes": "prefer DeWalt",
            "source_ebay": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM items WHERE name = 'Mitre Saw'").fetchone()
    assert row is not None
    item = db._item_from_row(row)
    assert item.terms == ["sliding mitre saw", "DeWalt mitre saw"]
    assert item.exclude_terms == ["toy"]
    assert item.max_price == 300
    assert item.sources == ["ebay"]  # only ebay ticked


def test_item_create_requires_terms(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.post(
        "/items/new",
        data={"project_id": project_id, "name": "Widget", "terms": ""},
        follow_redirects=True,
    )
    assert b"search term" in resp.data
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT COUNT(*) c FROM items WHERE name = 'Widget'"
    ).fetchone()["c"] == 0


def test_item_edit(cfg, client):
    _, item_id = seed_match(cfg)
    client.post(
        f"/items/{item_id}/edit",
        data={"name": "Track Saw", "terms": "plunge saw", "priority": "low",
              "normal_price": "450"},
    )
    conn = db.connect(cfg.db_path)
    item = db._item_from_row(db.get_item(conn, item_id))
    assert item.terms == ["plunge saw"]
    assert item.priority == "low"
    assert item.normal_price == 450
    assert item.sources is None  # no boxes ticked = all sources


def test_item_archive_and_delete(cfg, client):
    _, item_id = seed_match(cfg)
    client.post(f"/items/{item_id}/archive")
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT archived FROM items WHERE id = ?", (item_id,)
    ).fetchone()["archived"] == 1
    conn.close()

    client.post(f"/items/{item_id}/delete")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM listing_matches").fetchone()["c"] == 0
    # listings themselves are kept
    assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 1


# --- Product catalogue ---------------------------------------------------------


def test_item_edit_page_shows_products_section(cfg, client):
    _, item_id = seed_match(cfg)
    resp = client.get(f"/items/{item_id}/edit")
    assert b"Known products" in resp.data
    assert b"No known products yet" in resp.data


def test_item_new_page_has_no_products_section(cfg, client):
    project_id, _ = seed_match(cfg)
    resp = client.get(f"/items/new?project_id={project_id}")
    assert b"Known products" not in resp.data


def test_product_create(cfg, client):
    _, item_id = seed_match(cfg)
    resp = client.post(
        f"/items/{item_id}/products/new",
        data={
            "manufacturer": "Makita",
            "model": "SP6000",
            "match_terms": "makita sp6000\nsp6000",
            "msrp": "550",
            "typical_new_price": "500",
            "target_deal_price": "350",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM products WHERE manufacturer = 'Makita'").fetchone()
    assert row is not None
    product = db._product_from_row(row)
    assert product.match_terms == ["makita sp6000", "sp6000"]
    assert product.msrp == 550
    assert product.typical_new_price == 500


def test_product_create_requires_manufacturer_and_match_terms(cfg, client):
    _, item_id = seed_match(cfg)
    resp = client.post(
        f"/items/{item_id}/products/new",
        data={"manufacturer": "", "match_terms": ""},
        follow_redirects=True,
    )
    assert b"Manufacturer is required" in resp.data
    assert b"match term is required" in resp.data
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0


def test_product_edit(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    product_id = db.create_product(conn, item_id, "Makita", "SP6000", ["makita sp6000"], 550, 500, 350)
    conn.close()

    client.post(
        f"/products/{product_id}/edit",
        data={
            "manufacturer": "Makita",
            "model": "SP6000",
            "match_terms": "makita sp6000",
            "msrp": "550",
            "typical_new_price": "480",
            "target_deal_price": "320",
        },
    )
    conn = db.connect(cfg.db_path)
    product = db._product_from_row(db.get_product(conn, product_id))
    assert product.typical_new_price == 480
    assert product.target_deal_price == 320


def test_product_archive_and_delete(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    product_id = db.create_product(conn, item_id, "Makita", "SP6000", ["makita sp6000"], 550, 500, 350)
    conn.close()

    client.post(f"/products/{product_id}/archive")
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT archived FROM products WHERE id = ?", (product_id,)
    ).fetchone()["archived"] == 1
    conn.close()

    client.post(f"/products/{product_id}/delete")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0


def test_deleting_item_also_deletes_its_products(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    db.create_product(conn, item_id, "Makita", "SP6000", ["makita sp6000"], 550, 500, 350)
    conn.close()

    client.post(f"/items/{item_id}/delete")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0


def test_matched_product_shown_on_project_detail(cfg, client):
    # seed_match's listing title is "Makita SP6000 saw" against the "Track
    # Saw" item — add a catalogue product that matches it and re-run the
    # match via the scoring pipeline (record_match) to attach product_id.
    project_id, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    product_id = db.create_product(
        conn, item_id, "Makita", "SP6000", ["makita sp6000"], 550, 500, 350
    )
    match_row = conn.execute("SELECT id FROM listing_matches WHERE item_id = ?", (item_id,)).fetchone()
    conn.execute("UPDATE listing_matches SET product_id = ? WHERE id = ?", (product_id, match_row["id"]))
    conn.commit()
    conn.close()

    resp = client.get(f"/projects/{project_id}")
    assert b"Makita SP6000</span>" in resp.data


# --- Product suggestions ---------------------------------------------------------


def test_item_edit_shows_pending_suggestions(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://example.com/x")
    conn.close()

    resp = client.get(f"/items/{item_id}/edit")
    assert b"Suggested products" in resp.data
    assert b"Makita" in resp.data
    assert b"70%" in resp.data


def test_item_edit_hides_suggestions_section_when_none_pending(cfg, client):
    _, item_id = seed_match(cfg)
    resp = client.get(f"/items/{item_id}/edit")
    assert b"Suggested products" not in resp.data


def test_suggestion_approve_creates_product(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    suggestion = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://example.com/x")
    conn.close()

    resp = client.post(f"/suggestions/{suggestion['id']}/approve", follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    products = db.list_products(conn, item_id)
    assert len(products) == 1
    assert products[0]["manufacturer"] == "Makita"
    assert db.get_product_suggestion(conn, suggestion["id"])["status"] == "approved"


def test_suggestion_dismiss(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    suggestion = db.record_suggestion_sighting(conn, item_id, "Makita", "LS0816F/2", "https://example.com/x")
    conn.close()

    client.post(f"/suggestions/{suggestion['id']}/dismiss")
    conn = db.connect(cfg.db_path)
    assert db.get_product_suggestion(conn, suggestion["id"])["status"] == "dismissed"
    assert db.list_products(conn, item_id) == []


def test_catalogue_settings_updates_threshold(cfg, client):
    _, item_id = seed_match(cfg)
    resp = client.post(
        "/catalogue-settings", data={"auto_approve_threshold": "85"},
        headers={"Referer": f"/items/{item_id}/edit"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    assert db.get_auto_approve_threshold(conn) == 85.0


def test_catalogue_settings_blank_disables_auto_approve(cfg, client):
    _, item_id = seed_match(cfg)
    conn = db.connect(cfg.db_path)
    db.set_auto_approve_threshold(conn, 85.0)
    conn.close()

    client.post(
        "/catalogue-settings", data={"auto_approve_threshold": ""},
        headers={"Referer": f"/items/{item_id}/edit"},
    )
    conn = db.connect(cfg.db_path)
    assert db.get_auto_approve_threshold(conn) is None


def test_project_hero_excludes_flagged_listings_even_if_top_scored(cfg, client):
    # A flagged listing (e.g. a live auction — see scoring.is_live_auction)
    # can still score highest numerically, but must never headline the hero
    # "grab this now" callout — only a genuinely clean listing should.
    project_id, item_id = seed_match(cfg, flags=None, grade="A", score=85.0)
    conn = db.connect(cfg.db_path)
    listing_id, _ = db.upsert_listing(
        conn,
        Listing(source="ebay", external_id="E2", title="Suspiciously cheap live auction saw",
                price=5.0, url="https://example.com/2"),
    )
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="A", flags=["live auction"], margin_abs=495.0,
                   margin_pct=99.0, under_target=True, deal_score=99.0),
    )
    conn.commit()
    conn.close()

    resp = client.get(f"/projects/{project_id}")
    assert b"Makita SP6000 saw" in resp.data  # the clean, lower-scoring listing
    assert b"Suspiciously cheap live auction saw" in resp.data  # still visible in the table
    # The hero card only ever links to the clean listing's URL.
    hero_section = resp.data.split(b"Items &amp; listings")[0]
    assert b"example.com/1" in hero_section
    assert b"example.com/2" not in hero_section


# --- Listings filters, now on the project detail page ------------------------


def test_project_detail_listing_filters(cfg, client):
    project_id, item_id = seed_match(cfg)
    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    assert b"Makita SP6000 saw" in resp.data

    resp = client.get(f"/projects/{project_id}?grade=A&flagged=no&sort=price")
    assert b"Makita SP6000 saw" in resp.data

    # The hero callout always shows the true best deal regardless of the
    # listing filters below it, so check the filtered *table* is empty
    # rather than asserting the title vanishes from the whole page.
    resp = client.get(f"/projects/{project_id}?grade=spares/repair")
    assert b"Nothing here yet" in resp.data

    resp = client.get(f"/projects/{project_id}?item_id={item_id}")
    assert b"Makita SP6000 saw" in resp.data


# --- Manual pages -------------------------------------------------------------


def test_manual_page(cfg, client):
    seed_match(cfg)
    resp = client.get("/manual")
    assert resp.status_code == 200
    assert b"Gumtree" in resp.data
    assert b"Facebook Marketplace" in resp.data


def test_archived_project_excluded_from_manual(cfg, client):
    project_id, _ = seed_match(cfg)
    client.post(f"/projects/{project_id}/archive")
    resp = client.get("/manual")
    assert b"Track Saw" not in resp.data
