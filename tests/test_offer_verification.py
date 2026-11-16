from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from product_finder import auction_watch, db, relevance, runner, sources
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing, SearchPage
from product_finder.identity import derive_canonical_key
from product_finder.sources.base import Source, SourceCapabilities
from product_finder.sources import ebay
from product_finder.sources.ebay import _verify_fixed_price
from product_finder.web.app import create_app


ITEM = Listing("ebay", "v1|1|0", "Makita SP6000 track saw", 150, "https://example.test/1",
               buying_options=["FIXED_PRICE"])


@pytest.fixture
def offer_details():
    return {
        "itemId": ITEM.external_id,
        "title": ITEM.title,
        "price": {"value": "150.00", "currency": "GBP"},
        "buyingOptions": ["FIXED_PRICE"],
        "estimatedAvailabilities": [{
            "deliveryOptions": ["SHIP_TO_HOME"], "estimatedAvailabilityStatus": "IN_STOCK",
        }],
        "shippingOptions": [{
            "shippingCost": {"value": "20.00", "currency": "GBP"},
            "shipToLocationUsedForEstimate": {"country": "GB", "postalCode": "SW1A 1AA"},
            "quantityUsedForEstimate": 1,
        }],
    }


def test_fixed_price_verification_checks_the_destination_and_total(offer_details):
    check = _verify_fixed_price(ITEM, offer_details, "sw1a1aa")
    assert check.total == 170
    assert _verify_fixed_price(ITEM, offer_details, "EC1A 1BB").total is None
    offer_details["shippingOptions"][0]["shippingCost"]["value"] = "0"
    assert _verify_fixed_price(ITEM, offer_details, "SW1A 1AA").total == 150


def test_ebay_uses_a_postcode_scoped_read_for_the_exact_item(monkeypatch, offer_details):
    connector = ebay.EbaySource(AppConfig(postcode="SW1A 1AA"))
    monkeypatch.setattr(connector, "_get_token", lambda: "fixture")

    class Response:
        status_code = 200

        def json(self):
            return offer_details

    def get(url, *, headers, timeout):
        assert url.endswith("/v1%7C1%7C0")
        assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
        assert headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DGB%2Czip%3DSW1A%201AA"
        assert timeout == 8
        return Response()

    monkeypatch.setattr(ebay.requests, "get", get)
    assert connector.verify_offer(ITEM).total == 170


@pytest.mark.parametrize(("change", "value", "expected"), [
    ("itemId", "v1|1|2", "review"),
    ("title", "Makita accessory", "review"),
    ("itemGroupHref", "https://example.test/variants", "review"),
    ("buyingOptions", ["AUCTION", "FIXED_PRICE"], "review"),
    ("shippingOptions", [], "review"),
    ("estimatedAvailabilities", [], "review"),
    ("estimatedAvailabilities", [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}], "unavailable"),
    ("itemEndDate", "2020-01-01T00:00:00Z", "unavailable"),
    ("price", {"value": "160", "currency": "GBP"}, "review"),
    ("price", {"value": "150", "currency": "USD"}, "review"),
])
def test_unconfirmed_offers_do_not_return_a_payable_total(offer_details, change, value, expected):
    offer_details[change] = value
    check = _verify_fixed_price(ITEM, offer_details, "SW1A 1AA")
    assert check.status == expected
    assert check.total is None


def test_collection_only_lists_the_location_without_a_delivered_total(offer_details):
    offer_details["estimatedAvailabilities"][0]["deliveryOptions"] = ["SELLER_ARRANGED_LOCAL_PICKUP"]
    offer_details["itemLocation"] = {"city": "Bristol"}
    check = _verify_fixed_price(ITEM, offer_details)
    assert check.total is None
    assert "Collection only in Bristol" in check.reason


def test_changed_condition_requires_review(offer_details):
    listing = replace(ITEM, condition="Used")
    offer_details["condition"] = "For parts or not working"
    check = _verify_fixed_price(listing, offer_details, "SW1A 1AA")
    assert check.total is None
    assert "different condition" in check.reason


def test_watch_verifies_buy_it_now_and_withdraws_it_when_auction_changes(tmp_path, monkeypatch, offer_details):
    cfg = AppConfig(db_path=str(tmp_path / "test.db"), postcode="SW1A 1AA")
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(
        "Track Saw", ["track saw"], max_price=180, target_deal_price=175, normal_price=300,
    ))
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    hybrid = replace(ITEM, buying_options=["AUCTION", "FIXED_PRICE"], end_time=end_time,
                     buy_it_now_price=150, current_bid_price=99, bid_count=0)
    bid_only = replace(hybrid, external_id="v1|2|0", url="https://example.test/2",
                       buying_options=["AUCTION"], buy_it_now_price=None, price=99)
    parts_only = replace(ITEM, external_id="v1|3|0", url="https://example.test/3",
                         condition="Parts only, no power")
    offer_details.update(buyingOptions=["FIXED_PRICE", "AUCTION"], itemEndDate=end_time,
                         currentBidPrice={"value": "99", "currency": "GBP"})
    details = {
        hybrid.external_id: offer_details,
        bid_only.external_id: dict(offer_details, itemId=bid_only.external_id, buyingOptions=["AUCTION"]),
    }
    connector = ebay.EbaySource(cfg)
    detail_checks = []

    def fetch_item(external_id, *, delivery_context=False):
        if delivery_context:
            detail_checks.append(external_id)
        return details[external_id]

    monkeypatch.setattr(connector, "is_automated", lambda: True)
    monkeypatch.setattr(connector, "search_page", lambda term, item: SearchPage([parts_only, bid_only, hybrid], 3))
    monkeypatch.setattr(connector, "_fetch_item", fetch_item)
    monkeypatch.setattr(sources, "build_registry", lambda _: {"ebay": connector})
    delivered = []
    monkeypatch.setattr(runner.console_alerts, "send", delivered.append)

    [alert] = runner.run_once(cfg, conn)
    assert detail_checks == [hybrid.external_id]
    assert alert.listing.price == 170
    assert alert.listing.current_bid_price == 99
    assert alert.evaluation.under_target
    assert alert.extras["purchase_mode"] == "buy_it_now"
    assert delivered == [alert]
    [recommendation] = db.query_matches(conn, item_id=item_id, verified_only=True)
    assert recommendation["offer_total"] == 170
    assert db.project_top_picks(conn)[project_id]["price"] == 170

    with create_app(cfg).test_client() as client:
        assert b"Buy It Now" in client.get("/").data
        auctions = client.get("/auctions").data
        assert b"99.00" in auctions
        assert b"170.00 delivered" in auctions

        # The item endpoint can withdraw Buy It Now before the next search
        # even when the numeric bid happens to equal the old purchase price.
        details[hybrid.external_id] = dict(offer_details, buyingOptions=["AUCTION"],
                                           currentBidPrice=offer_details["price"])
        assert auction_watch.poll_and_capture(cfg, conn) == 0
        assert not db.query_matches(conn, item_id=item_id, verified_only=True)
        assert project_id not in db.project_top_picks(conn)
        assert b"170.00 delivered" not in client.get("/auctions").data
        assert b"150.00" in client.get("/auctions").data

    # A previously verified response cannot replace the new bid-only state.
    db.store_offer_check(conn, recommendation["listing_id"], hybrid,
                         _verify_fixed_price(hybrid, offer_details, cfg.postcode), postcode=cfg.postcode)
    assert not db.query_matches(conn, item_id=item_id, verified_only=True)
    assert runner.run_once(cfg, conn) == []
    assert delivered == [alert]


def test_watch_prices_and_links_the_exact_nvme_variant(tmp_path, monkeypatch, offer_details):
    cfg = AppConfig(db_path=str(tmp_path / "t.db"), postcode="SW1A 1AA")
    conn = db.connect(cfg.db_path)
    item_id = db.create_item(conn, db.create_project(conn, "Computer"), ItemConfig(
        "4TB NVMe SSD", ["4TB NVMe SSD"], max_price=550, normal_price=600,
    ))
    title = "Fikwot 1TB 2TB 4TB M.2 NVMe SSD for PS5 PC"
    group_url = "https://www.ebay.co.uk/itm/123456789012"
    group = Listing("ebay", "v1|123456789012|0", title, 149.99, group_url,
                    buying_options=["FIXED_PRICE"], variation_group=True, variation_group_id="123456789012")

    def variant(size, ident, price, link_id=None):
        return dict(offer_details, itemGroupType="SELLER_DEFINED_VARIATIONS",
                    itemId=f"v1|123456789012|{ident}", title=title, price={"value": price, "currency": "GBP"},
                    itemWebUrl=f"{group_url}?var={link_id or ident}",
                    localizedAspects=[{"name": "Storage Capacity", "value": size},
                                      {"name": "Interface", "value": "M.2, NVME"}])

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [
                variant("1TB", "111111111111", "149.99"),
                variant("4TB", "222222222222", "100", link_id="111111111111"),
                variant("4 TB", "444444444444", "489.99"),
            ]}

    calls = []

    def get(url, *, params, headers, timeout):
        assert url.endswith("/get_items_by_item_group")
        assert params == {"item_group_id": "123456789012"}
        calls.append(url)
        return Response()

    connector = ebay.EbaySource(cfg)
    monkeypatch.setattr(connector, "_get_token", lambda: "fixture")
    monkeypatch.setattr(connector, "is_automated", lambda: True)
    monkeypatch.setattr(connector, "search_page", lambda term, item: SearchPage([group], 1))
    monkeypatch.setattr(ebay.requests, "get", get)
    monkeypatch.setattr(sources, "build_registry", lambda _: {"ebay": connector})
    cfg.alerts.console = False

    matches = runner.run_once(cfg, conn)
    assert [match.listing.price for match in matches] == [509.99]
    [recommended] = db.query_matches(conn, item_id=item_id, verified_only=True)
    assert recommended["variant_capacity_gb"] == 4096
    assert recommended["offer_total"] == 509.99
    assert derive_canonical_key(recommended["url"]) == "ebay:123456789012:444444444444"
    assert derive_canonical_key(group_url) != derive_canonical_key(recommended["url"])
    selected = Listing.from_row(db.get_listing(conn, recommended["listing_id"]))
    assert not relevance.assess(selected, ItemConfig("Samsung 4TB NVMe SSD", ["4TB NVMe SSD"]), [])[0].eligible
    assert not relevance.assess(
        selected, ItemConfig("4TB NVMe SSD", ["4TB NVMe SSD"], required_terms=["1TB"]), [],
    )[0].eligible
    assert db.query_matches(conn, item_id=item_id, review=True)[0]["price"] == 149.99
    assert runner.run_once(cfg, conn) == []
    assert len(calls) == 1


def test_checked_total_controls_deals_alerts_and_review(tmp_path, monkeypatch, offer_details):
    cfg = AppConfig(db_path=str(tmp_path / "test.db"), postcode="SW1A 1AA")
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(conn, project_id, ItemConfig(
        "Track Saw", ["track saw"], max_price=180, target_deal_price=160, normal_price=300,
    ))

    class EbayFixture(Source):
        name = "ebay"
        checks = 0

        def capabilities(self):
            return SourceCapabilities(automated=True, compliance="test", supports_offer_verification=True)

        def search(self, term, item):
            return [ITEM]

        def verify_offer(self, listing):
            self.checks += 1
            return _verify_fixed_price(listing, offer_details, cfg.postcode)

    connector = EbayFixture(cfg)
    monkeypatch.setattr(sources, "build_registry", lambda _: {"ebay": connector})
    monkeypatch.setattr(runner.console_alerts, "send", lambda _: None)

    alerts = runner.run_once(cfg, conn)
    assert [alert.listing.price for alert in alerts] == [170]
    assert not alerts[0].evaluation.under_target
    assert runner.run_once(cfg, conn) == []
    assert connector.checks == 1
    with create_app(cfg).test_client() as client:
        assert b"170.00 delivered" in client.get("/").data
        assert b'class="badge target">' not in client.get("/").data
        db.set_offer_destination(conn, "EC1A 1BB")
        assert b"170.00 delivered" not in client.get("/").data
        db.set_offer_destination(conn, "sw1a1aa")
        assert b"170.00 delivered" in client.get("/").data

        # A later search reports a different price, invalidating the cached total.
        changed = deepcopy(ITEM)
        changed.price = 165
        db.upsert_listing(conn, changed)
        assert not db.query_matches(conn, verified_only=True)
        assert not db.mark_alerted(conn, alerts[0].extras["match_id"], "webhook")
        assert b"Price and availability still need checking" in client.get(f"/projects/{project_id}?view=review").data

        db.upsert_listing(conn, ITEM)
        assert not db.query_matches(conn, verified_only=True)
        check = _verify_fixed_price(ITEM, offer_details, cfg.postcode)
        listing_id = db.query_matches(conn, item_id=item_id)[0]["listing_id"]
        db.store_offer_check(conn, listing_id, ITEM, check, postcode=cfg.postcode)
        assert db.query_matches(conn, verified_only=True)
        conn.execute("UPDATE listings SET offer_checked_at = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        assert not db.query_matches(conn, verified_only=True)
        assert db.query_matches(conn, item_id=item_id, review=True)
