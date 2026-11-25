from datetime import date, datetime, timezone

from product_finder import black_friday, db
from product_finder.config import AppConfig, ItemConfig
from product_finder.web.app import create_app

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def row(**kwargs):
    return dict(canonical_price_url="https://example.com/product", typical_new_price=75,
                last_price_check_at=NOW.isoformat(), last_price_check_ok=1,
                max_price=100, target_deal_price=80) | kwargs


def history():
    return [{"observed_at": f"2026-11-{day:02}T12:00:00+00:00",
             "domain": "example.com", "price": 100} for day in (1, 5, 10)]


def test_event_dates():
    assert black_friday.event_dates(2026) == (
        date(2026, 11, 27), date(2026, 11, 20), date(2026, 11, 30))


def test_event_dates_when_november_has_five_fridays():
    assert black_friday.event_dates(2029) == (
        date(2029, 11, 23), date(2029, 11, 16), date(2029, 11, 26))


def test_observed_reduction_ignores_event_prices_other_retailers_and_duplicate_days():
    observations = history() + [
        {"observed_at": "2026-11-25T10:00:00+00:00", "domain": "example.com", "price": 500},
        {"observed_at": "2026-11-05T10:00:00+00:00", "domain": "other.example", "price": 999},
        {"observed_at": "2026-11-01T10:00:00+00:00", "domain": "example.com", "price": 300},
    ]
    result = black_friday.assess(row(), observations, NOW)
    assert result["baseline"] == 100
    assert result["percent"] == 25
    assert result["sample_days"] == 3
    assert result["target_met"]
    assert result["verdict"] == "Strong observed reduction"


def test_missing_history_and_failed_stale_future_check():
    assert black_friday.assess(row(), history()[:2], NOW)["percent"] is None
    for patch in ({"last_price_check_ok": 0}, {"last_price_check_at": "2026-11-23T00:00:00Z"},
                  {"last_price_check_at": "2026-11-26T00:00:00Z"},
                  {"typical_new_price": float("nan")}):
        result = black_friday.assess(row(**patch), history(), NOW)
        assert result["current"] is None
        assert result["percent"] is None
        assert not result["target_met"]


def test_budget_and_no_rrp_discount():
    result = black_friday.assess(row(max_price=70), history(), NOW)
    assert result["verdict"] == "Above budget"
    assert black_friday.assess(row(), [], NOW)["verdict"] == "Insufficient history"
    assert black_friday.assess(row(typical_new_price=110), history(), NOW)["verdict"] == "Above budget"


def test_watchlist_scope_and_route(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "test.db"))
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    project = db.create_project(conn, "Workshop")
    item = db.create_item(conn, project, ItemConfig(name="Saw", terms=["saw"]))
    pid = db.create_product(conn, item, "Makita", "WantedSaw", ["saw"], 500, None, 100)
    db.create_product(conn, item, "Makita", "NotWantedSaw", ["other"], 500, None, 100)
    conn.execute("UPDATE item_products SET wanted = 0 WHERE product_id != ?", (pid,))
    conn.commit()
    assert [d["model"] for d in black_friday.dashboard(conn, NOW)["deals"]] == ["WantedSaw"]
    db.create_item(conn, project, ItemConfig(name="UnresolvedWatch", terms=["drill"], priority="high"))
    unresolved = black_friday.dashboard(conn, NOW)["unresolved"]
    assert [w["item"] for w in unresolved] == ["UnresolvedWatch"]
    conn.close()
    response = app.test_client().get("/black-friday")
    assert response.status_code == 200
    assert b"WantedSaw" in response.data
    assert b"UnresolvedWatch" in response.data
    assert b"Watches needing product identification" in response.data
    assert b"NotWantedSaw" not in response.data
    assert b"Needs retailer approval" in response.data
    assert b"/black-friday" in app.test_client().get("/").data


def test_marketplace_target_selection(monkeypatch):
    base = dict(listing_id=1, match_id=1, offer_total=80, target_deal_price=100, priority="normal")
    rows = [base, dict(base, match_id=2, priority="high", target_deal_price=90),
            dict(base, listing_id=2, target_deal_price=70),
            dict(base, listing_id=3, target_deal_price=None),
            dict(base, listing_id=4, offer_total=None)]
    def query(conn, **kwargs):
        assert kwargs == {"verified_only": True, "sort": "price"}
        return rows
    monkeypatch.setattr(db, "query_matches", query)
    [result] = black_friday.marketplace_targets(None)
    assert result["match_id"] == 2
    assert result["below_target"] == 10
