from unittest import mock

import requests

from product_finder import db, retailer_price, runner
from product_finder.config import AppConfig, ItemConfig, SearxngConfig
from product_finder.web.app import create_app

JSON_LD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Makita LS0815FL",
 "offers": {"@type": "Offer", "price": "319.99", "priceCurrency": "GBP"}}
</script>
</head><body></body></html>
"""

JSON_LD_GRAPH_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "BreadcrumbList"},
  {"@type": "Product", "name": "Makita LS0815FL",
   "offers": [{"@type": "Offer", "price": 319.99, "priceCurrency": "GBP"}]}
]}
</script>
</head><body></body></html>
"""

MICRODATA_HTML = """
<html><body>
<span itemprop="price" content="414.98" />
<meta itemprop="priceCurrency" content="GBP" />
</body></html>
"""

USD_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "offers": {"price": "399.00", "priceCurrency": "USD"}}
</script>
</head></html>
"""


def _cfg(**overrides):
    return SearxngConfig(enabled=True, base_url="http://searxng.test", **overrides)


def _resp(status_code=200, text=""):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    return resp


# --- fetch_price: JSON-LD / microdata / currency filtering ----------------------


def test_fetch_price_parses_json_ld():
    with mock.patch("product_finder.retailer_price.requests.get", return_value=_resp(text=JSON_LD_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 319.99, "currency": "GBP"}


def test_fetch_price_parses_json_ld_graph_wrapper():
    with mock.patch(
        "product_finder.retailer_price.requests.get", return_value=_resp(text=JSON_LD_GRAPH_HTML)
    ):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 319.99, "currency": "GBP"}


def test_fetch_price_falls_back_to_microdata():
    with mock.patch("product_finder.retailer_price.requests.get", return_value=_resp(text=MICRODATA_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 414.98, "currency": "GBP"}


def test_fetch_price_rejects_non_gbp_currency():
    with mock.patch("product_finder.retailer_price.requests.get", return_value=_resp(text=USD_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


def test_fetch_price_returns_none_on_non_200():
    with mock.patch("product_finder.retailer_price.requests.get", return_value=_resp(status_code=503)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


def test_fetch_price_returns_none_when_unreachable(caplog):
    with mock.patch(
        "product_finder.retailer_price.requests.get", side_effect=requests.ConnectionError("refused")
    ):
        with caplog.at_level("WARNING"):
            result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None
    assert "failed" in caplog.text.lower()


def test_fetch_price_returns_none_when_no_structured_data():
    with mock.patch(
        "product_finder.retailer_price.requests.get", return_value=_resp(text="<html>no data here</html>")
    ):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


# --- search_candidates -----------------------------------------------------------


def test_search_candidates_disabled_makes_no_request():
    with mock.patch("product_finder.retailer_price.requests.get") as get:
        result = retailer_price.search_candidates("Makita", "LS0815FL", SearxngConfig(enabled=False))
    assert result == []
    get.assert_not_called()


def test_search_candidates_unavailable_returns_empty(caplog):
    with mock.patch(
        "product_finder.retailer_price.requests.get", side_effect=requests.ConnectionError("refused")
    ):
        with caplog.at_level("WARNING"):
            result = retailer_price.search_candidates("Makita", "LS0815FL", _cfg())
    assert result == []
    assert "unavailable" in caplog.text.lower()


def test_search_candidates_returns_ranked_priced_results():
    search_resp = mock.Mock()
    search_resp.raise_for_status = mock.Mock()
    search_resp.json = mock.Mock(return_value={"results": [
        {"url": "https://screwfix.com/p/makita-ls0815fl", "title": "Makita LS0815FL mitre saw"},
        {"url": "https://randomblog.test/review", "title": "some review, no price data"},
        {"url": "https://axminstertools.com/makita-ls0815fl-216mm", "title": "Makita LS0815FL"},
    ]})

    def fake_get(url, headers=None, params=None, timeout=None):
        if params is not None:  # the SearXNG search call itself
            return search_resp
        if "screwfix" in url:
            return _resp(text=JSON_LD_HTML)
        if "axminstertools" in url:
            return _resp(text=MICRODATA_HTML)
        return _resp(text="<html>no price here</html>")

    with mock.patch("product_finder.retailer_price.requests.get", side_effect=fake_get):
        candidates = retailer_price.search_candidates("Makita", "LS0815FL", _cfg())

    assert len(candidates) == 2  # the unparseable blog result is dropped
    domains = {c["domain"] for c in candidates}
    assert domains == {"screwfix.com", "axminstertools.com"}
    # Both mention the model in the URL/title and are known UK retailers,
    # so both should rank highly — sorted descending.
    assert candidates[0]["confidence"] >= candidates[1]["confidence"]


def test_confidence_scores_model_and_known_domain_higher():
    known = retailer_price._confidence("Makita", "LS0815FL", "https://screwfix.com/p/ls0815fl", "Makita LS0815FL")
    unknown = retailer_price._confidence("Makita", "LS0815FL", "https://randomshop.test/x", "some tool")
    assert known > unknown


# --- db.py: candidate storage / approval / refresh -------------------------------


def _setup_product(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(name="Mitre Saw", terms=["mitre saw"]))
    product_id = db.create_product(conn, item_id, "Makita", "LS0815FL", ["Makita LS0815FL"], None, None, None)
    return cfg, conn, product_id


def test_record_and_list_price_candidates(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://screwfix.com/p/1", "domain": "screwfix.com", "price": 319.99,
         "currency": "GBP", "confidence": 90.0},
    ])
    rows = db.list_price_candidates(conn, product_id)
    assert len(rows) == 1
    assert rows[0]["domain"] == "screwfix.com"
    assert db.get_product(conn, product_id)["price_search_checked"] == 1


def test_recording_candidates_replaces_previous_batch(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://a.test/1", "domain": "a.test", "price": 100.0, "currency": "GBP", "confidence": 50.0},
    ])
    db.record_price_candidates(conn, product_id, [
        {"url": "https://b.test/1", "domain": "b.test", "price": 200.0, "currency": "GBP", "confidence": 80.0},
    ])
    rows = db.list_price_candidates(conn, product_id)
    assert len(rows) == 1
    assert rows[0]["domain"] == "b.test"


def test_products_needing_price_search_excludes_already_checked_or_approved(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    assert [r["id"] for r in db.list_products_needing_price_search(conn)] == [product_id]
    db.record_price_candidates(conn, product_id, [])
    assert db.list_products_needing_price_search(conn) == []


def test_approve_price_candidate_sets_canonical_url_and_price(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://screwfix.com/p/1", "domain": "screwfix.com", "price": 319.99,
         "currency": "GBP", "confidence": 90.0},
    ])
    candidate_id = db.list_price_candidates(conn, product_id)[0]["id"]

    db.approve_price_candidate(conn, candidate_id, {"price": 309.99, "currency": "GBP"})

    product = db.get_product(conn, product_id)
    assert product["canonical_price_url"] == "https://screwfix.com/p/1"
    assert product["typical_new_price"] == 309.99  # fresh refetch price used, not the stale candidate price
    assert product["last_price_check_ok"] == 1
    assert db.list_price_candidates(conn, product_id) == []


def test_approve_price_candidate_falls_back_to_candidate_price_on_refresh_failure(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://screwfix.com/p/1", "domain": "screwfix.com", "price": 319.99,
         "currency": "GBP", "confidence": 90.0},
    ])
    candidate_id = db.list_price_candidates(conn, product_id)[0]["id"]

    db.approve_price_candidate(conn, candidate_id, None)  # refetch-on-approve failed

    product = db.get_product(conn, product_id)
    assert product["typical_new_price"] == 319.99  # falls back to the candidate's own price
    assert product["last_price_check_ok"] == 0


def test_clear_price_candidates_does_not_reset_checked_flag(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://a.test/1", "domain": "a.test", "price": 100.0, "currency": "GBP", "confidence": 50.0},
    ])
    db.clear_price_candidates(conn, product_id)
    assert db.list_price_candidates(conn, product_id) == []
    assert db.get_product(conn, product_id)["price_search_checked"] == 1


def test_refresh_due_list_and_record_refresh_keeps_last_price_on_failure(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.update_product(conn, product_id, "Makita", "LS0815FL", ["Makita LS0815FL"], None, 319.99, None)
    conn.execute(
        "UPDATE products SET canonical_price_url = ? WHERE id = ?",
        ("https://screwfix.com/p/1", product_id),
    )
    conn.commit()

    assert [r["id"] for r in db.list_products_due_for_price_refresh(conn, 24)] == [product_id]

    db.record_price_refresh(conn, product_id, None)  # refresh failed
    product = db.get_product(conn, product_id)
    assert product["typical_new_price"] == 319.99  # unchanged
    assert product["last_price_check_ok"] == 0
    assert db.list_products_due_for_price_refresh(conn, 24) == []  # just-checked, not due again

    db.record_price_refresh(conn, product_id, {"price": 299.99, "currency": "GBP"})
    product = db.get_product(conn, product_id)
    assert product["typical_new_price"] == 299.99
    assert product["last_price_check_ok"] == 1


# --- runner.py wiring --------------------------------------------------------------


def test_run_once_triggers_discovery_for_unsearched_products(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    with mock.patch(
        "product_finder.retailer_price.search_candidates",
        return_value=[{"url": "https://screwfix.com/p/1", "domain": "screwfix.com",
                       "price": 319.99, "currency": "GBP", "confidence": 90.0}],
    ) as search, mock.patch("product_finder.sources.build_registry", return_value={}):
        runner.run_once(cfg, conn)

    search.assert_called_once_with("Makita", "LS0815FL", cfg.searxng)
    assert len(db.list_price_candidates(conn, product_id)) == 1


def test_run_once_skips_discovery_when_searxng_disabled(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)  # searxng.enabled defaults to False
    with mock.patch("product_finder.retailer_price.search_candidates") as search, mock.patch(
        "product_finder.sources.build_registry", return_value={}
    ):
        runner.run_once(cfg, conn)
    search.assert_not_called()


def test_run_once_refreshes_stale_canonical_prices(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    db.record_price_candidates(conn, product_id, [])  # mark as already searched
    conn.execute(
        "UPDATE products SET canonical_price_url = ? WHERE id = ?",
        ("https://screwfix.com/p/1", product_id),
    )
    conn.commit()

    with mock.patch(
        "product_finder.retailer_price.fetch_price", return_value={"price": 299.99, "currency": "GBP"}
    ) as fetch, mock.patch("product_finder.sources.build_registry", return_value={}):
        runner.run_once(cfg, conn)

    fetch.assert_called_once_with("https://screwfix.com/p/1", cfg.searxng.timeout)
    assert db.get_product(conn, product_id)["typical_new_price"] == 299.99


# --- web routes ---------------------------------------------------------------------


def test_product_edit_page_shows_search_button_when_no_canonical_url(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    app = create_app(cfg)
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get(f"/products/{product_id}/edit")
    assert resp.status_code == 200
    assert b"Search for retailer price" in resp.data


def test_price_candidate_approve_route_sets_canonical_url(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    db.record_price_candidates(conn, product_id, [
        {"url": "https://screwfix.com/p/1", "domain": "screwfix.com", "price": 319.99,
         "currency": "GBP", "confidence": 90.0},
    ])
    candidate_id = db.list_price_candidates(conn, product_id)[0]["id"]
    conn.close()

    app = create_app(cfg)
    app.config["TESTING"] = True
    client = app.test_client()
    with mock.patch("product_finder.web.app.retailer_price.fetch_price", return_value=None):
        resp = client.post(f"/price-candidates/{candidate_id}/approve", follow_redirects=True)
    assert resp.status_code == 200

    conn2 = db.connect(cfg.db_path)
    assert db.get_product(conn2, product_id)["canonical_price_url"] == "https://screwfix.com/p/1"
