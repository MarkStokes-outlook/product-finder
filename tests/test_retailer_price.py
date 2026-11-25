from unittest import mock

import requests
import pytest

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
    return status_code, text


@pytest.fixture(autouse=True)
def _no_test_pacing(monkeypatch):
    monkeypatch.setattr(retailer_price._search_limiter, "wait", lambda: None)
    monkeypatch.setattr(retailer_price._retailer_limiter, "wait", lambda: None)


# --- fetch_price: JSON-LD / microdata / currency filtering ----------------------


def test_fetch_price_parses_json_ld():
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=_resp(text=JSON_LD_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 319.99, "currency": "GBP"}


def test_fetch_price_parses_json_ld_graph_wrapper():
    with mock.patch(
        "product_finder.retailer_price._retailer_get", return_value=_resp(text=JSON_LD_GRAPH_HTML)
    ):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 319.99, "currency": "GBP"}


def test_fetch_price_falls_back_to_microdata():
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=_resp(text=MICRODATA_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result == {"price": 414.98, "currency": "GBP"}


def test_fetch_price_rejects_non_gbp_currency():
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=_resp(text=USD_HTML)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


def test_fetch_price_returns_none_on_non_200():
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=_resp(status_code=503)):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


def test_fetch_price_returns_none_when_unreachable(caplog):
    with mock.patch(
        "product_finder.retailer_price._retailer_get", side_effect=OSError("refused")
    ):
        with caplog.at_level("WARNING"):
            result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None
    assert "failed" in caplog.text.lower()


def test_fetch_price_returns_none_when_no_structured_data():
    with mock.patch(
        "product_finder.retailer_price._retailer_get", return_value=_resp(text="<html>no data here</html>")
    ):
        result = retailer_price.fetch_price("https://retailer.test/p/1", timeout=10)
    assert result is None


# --- search_candidates -----------------------------------------------------------


def test_search_candidates_disabled_makes_no_request():
    with mock.patch("product_finder.retailer_price.requests.get") as get:
        result = retailer_price.search_candidates("Makita", "LS0815FL", SearxngConfig(enabled=False))
    assert result == []
    get.assert_not_called()


def test_search_candidates_unavailable_returns_none(caplog):
    with mock.patch(
        "product_finder.retailer_price.requests.get", side_effect=requests.ConnectionError("refused")
    ):
        with caplog.at_level("WARNING"):
            result = retailer_price.search_candidates("Makita", "LS0815FL", _cfg())
    assert result is None
    assert "unavailable" in caplog.text.lower()


def test_search_candidates_returns_ranked_priced_results():
    search_resp = mock.Mock()
    search_resp.status_code = 200
    search_resp.raise_for_status = mock.Mock()
    search_resp.json = mock.Mock(return_value={"results": [
        {"url": "https://screwfix.com/p/makita-ls0815fl", "title": "Makita LS0815FL mitre saw"},
        {"url": "https://randomblog.test/review", "title": "some review, no price data"},
        {"url": "https://axminstertools.com/makita-ls0815fl-216mm", "title": "Makita LS0815FL"},
    ]})

    def fake_get(url, timeout):
        if "screwfix" in url:
            return _resp(text=JSON_LD_HTML)
        if "axminstertools" in url:
            return _resp(text=MICRODATA_HTML)
        return _resp(text="<html>no price here</html>")

    with mock.patch("product_finder.retailer_price.requests.get", return_value=search_resp), mock.patch(
        "product_finder.retailer_price._retailer_get", side_effect=fake_get
    ):
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
    assert db.list_new_price_history(conn, product_id) == []


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


# --- New-price history collection (not yet read by scoring — see price_trend.py) -


def test_approve_price_candidate_logs_new_price_history(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.record_price_candidates(conn, product_id, [
        {"url": "https://screwfix.com/p/1", "domain": "screwfix.com", "price": 319.99,
         "currency": "GBP", "confidence": 90.0},
    ])
    candidate_id = db.list_price_candidates(conn, product_id)[0]["id"]

    db.approve_price_candidate(conn, candidate_id, {"price": 309.99, "currency": "GBP"})

    history = db.list_new_price_history(conn, product_id)
    assert len(history) == 1
    assert history[0]["price"] == 309.99
    assert history[0]["domain"] == "screwfix.com"


def test_record_price_refresh_logs_new_price_history_only_on_success(tmp_path):
    cfg, conn, product_id = _setup_product(tmp_path)
    db.update_product(conn, product_id, "Makita", "LS0815FL", ["Makita LS0815FL"], None, 319.99, None)
    conn.execute(
        "UPDATE products SET canonical_price_url = ? WHERE id = ?",
        ("https://screwfix.com/p/1", product_id),
    )
    conn.commit()

    db.record_price_refresh(conn, product_id, None, domain="screwfix.com")
    assert db.list_new_price_history(conn, product_id) == []

    db.record_price_refresh(conn, product_id, {"price": 299.99, "currency": "GBP"}, domain="screwfix.com")
    history = db.list_new_price_history(conn, product_id)
    assert len(history) == 1
    assert history[0]["price"] == 299.99
    assert history[0]["domain"] == "screwfix.com"


def test_run_once_refresh_records_new_price_history(tmp_path):
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
    ), mock.patch("product_finder.sources.build_registry", return_value={}):
        runner.run_once(cfg, conn)

    history = db.list_new_price_history(conn, product_id)
    assert len(history) == 1
    assert history[0]["domain"] == "screwfix.com"


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


@pytest.mark.parametrize("url", [
    "http://screwfix.com/p/a", "https://screwfix.com.evil.test/p/a",
    "https://screwfix.com@127.0.0.1/p/a", "https://127.0.0.1/",
    "https://screwfix.com:8443/p/a", "https://unknown.test/p/a",
    "https://screwfix.com\\@evil.test/a",
])
def test_unsafe_destination_rejected_before_dns(url):
    with mock.patch("product_finder.retailer_price.socket.getaddrinfo") as dns:
        assert retailer_price.fetch_price(url, 1) is None
        dns.assert_not_called()


def test_private_or_mixed_dns_is_rejected():
    answers = [(0, 0, 0, "", ("8.8.8.8", 443)),
               (0, 0, 0, "", ("127.0.0.1", 443))]
    with mock.patch("product_finder.retailer_price.socket.getaddrinfo", return_value=answers), mock.patch(
        "product_finder.retailer_price.urllib3.HTTPSConnectionPool"
    ) as pool:
        assert retailer_price.fetch_price("https://screwfix.com/p/a", 1) is None
        pool.assert_not_called()


def test_public_address_pinned_and_redirect_not_followed():
    answers = [(0, 0, 0, "", ("8.8.8.8", 443))]
    with mock.patch("product_finder.retailer_price.socket.getaddrinfo", return_value=answers), mock.patch(
        "product_finder.retailer_price.urllib3.HTTPSConnectionPool"
    ) as pool:
        response = pool.return_value.urlopen.return_value
        response.status = 302
        assert retailer_price.fetch_price("https://screwfix.com/p/a", 1) is None
        assert pool.call_args.args == ("8.8.8.8",)
        assert pool.call_args.kwargs["assert_hostname"] == "screwfix.com"
        assert pool.call_args.kwargs["server_hostname"] == "screwfix.com"
        assert pool.call_args.kwargs["cert_reqs"] == "CERT_REQUIRED"
        assert pool.return_value.urlopen.call_args.kwargs["redirect"] is False
        pool.return_value.urlopen.assert_called_once()


def test_partial_upstream_outage_without_results_is_failure():
    response = mock.Mock(status_code=200)
    response.json.return_value = {"results": [], "unresponsive_engines": [["google", "blocked"]]}
    with mock.patch("product_finder.retailer_price.requests.get", return_value=response):
        assert retailer_price.search_candidates("Makita", "SP6000", _cfg()) is None
        response.json.return_value = {"results": [], "unresponsive_engines": []}
        assert retailer_price.search_candidates("Makita", "SP6000", _cfg()) == []


def test_failed_search_is_not_marked_checked_and_backoff_survives_cycle(tmp_path):
    cfg, conn, product = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    with mock.patch("product_finder.retailer_price.search_candidates", return_value=None) as search:
        retailer_price.run_discovery_and_refresh(conn, cfg)
        retailer_price.run_discovery_and_refresh(conn, cfg)
        search.assert_called_once()
    assert db.get_product(conn, product)["price_search_checked"] == 0
    assert conn.execute("SELECT ok FROM source_runs WHERE source=?", (retailer_price.SOURCE_NAME,)).fetchone()["ok"] == 0


def test_discovery_is_bounded_prioritised_and_wanted_only(tmp_path):
    cfg, conn, low = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    pr = db.create_project(conn, "Renovation")
    item = db.create_item(conn, pr, ItemConfig(name="High", terms=["tool"], priority="high"))
    for n in range(5):
        db.create_product(conn, item, "Makita", f"Model{n}", [f"model{n}"], None, None, None)
    conn.execute("UPDATE item_products SET wanted=0 WHERE product_id=?", (low,))
    conn.commit()
    with mock.patch("product_finder.retailer_price.search_candidates", return_value=[]) as search:
        retailer_price.run_discovery_and_refresh(conn, cfg)
        assert search.call_count == retailer_price.DISCOVERY_LIMIT
        assert [call.args[1] for call in search.call_args_list] == ["Model0", "Model1", "Model2"]
    assert db.get_product(conn, low)["price_search_checked"] == 0


@pytest.mark.parametrize("price,currency", [("0", "GBP"), ("nan", "GBP"), ("-1", "GBP"), ("100", "")])
def test_rejects_invalid_or_unconfirmed_gbp_prices(price, currency):
    html = JSON_LD_HTML.replace("319.99", price).replace("GBP", currency)
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=(200, html)):
        assert retailer_price.fetch_price("https://screwfix.com/p/a", 1) is None


def test_refresh_budget(tmp_path):
    cfg, conn, _ = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    pr = db.create_project(conn, "More")
    item = db.create_item(conn, pr, ItemConfig(name="High", terms=["tool"]))
    for n in range(10):
        pid = db.create_product(conn, item, "Makita", f"Refresh{n}", [f"refresh{n}"], None, None, None)
        conn.execute("UPDATE products SET canonical_price_url=? WHERE id=?", ("https://screwfix.com/p/a", pid))
    conn.commit()
    with mock.patch("product_finder.retailer_price.search_candidates", return_value=[]), mock.patch(
        "product_finder.retailer_price.fetch_price", return_value=None
    ) as fetch:
        retailer_price.run_discovery_and_refresh(conn, cfg)
        assert fetch.call_count == retailer_price.REFRESH_LIMIT


def test_black_friday_shows_pending_candidate_link_and_discovery_status(tmp_path):
    cfg, conn, pid = _setup_product(tmp_path)
    cfg.searxng.enabled = True
    db.record_price_candidates(conn, pid, [
        {"url": "https://screwfix.com/p/a", "domain": "screwfix.com",
         "price": 100, "currency": "GBP", "confidence": 90},
    ])
    db.record_source_run(conn, retailer_price.SOURCE_NAME, searches=1, errors=1, last_error="unavailable")
    app = create_app(cfg)
    response = app.test_client().get("/black-friday")
    assert response.status_code == 200
    assert b"Review 1 retailer candidates" in response.data
    assert b"Unavailable" in response.data
    assert b"Needs retailer approval" in response.data


def test_refurbished_url_is_not_new_price():
    with mock.patch("product_finder.retailer_price._retailer_get") as fetch:
        assert retailer_price.fetch_price("https://screwfix.com/p/refurb-titan-ttb794msw/374ry", 1) is None
        fetch.assert_not_called()


@pytest.mark.parametrize("condition", ["https://schema.org/UsedCondition", "https://schema.org/RefurbishedCondition"])
def test_structured_non_new_condition_does_not_fall_back_to_microdata(condition):
    html = JSON_LD_HTML.replace('"priceCurrency": "GBP"', '"priceCurrency": "GBP", "itemCondition": "' + condition + '"') + MICRODATA_HTML
    with mock.patch("product_finder.retailer_price._retailer_get", return_value=(200, html)):
        assert retailer_price.fetch_price("https://screwfix.com/p/a", 1) is None
