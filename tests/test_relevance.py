import pytest

from product_finder import db, relevance, runner, sources
from product_finder.config import AppConfig, ItemConfig
from product_finder.models import Listing
from product_finder.sources.base import Source, SourceCapabilities
from product_finder.web.app import create_app


@pytest.mark.parametrize(("watch", "title", "status"), [
    ("1000W PSU", "Corsair RM1000x Modular PSU 600W PCIe 5.0 12VHPWR Cable", "excluded"),
    ("NVIDIA RTX 4090", "Mercedes Citan 6 speed manual gearbox TL4090", "excluded"),
    ("NVIDIA RTX 4080", "MSI RTX 4060 Ventus 2X 8GB Graphics Card", "excluded"),
    ("128GB DDR5 RAM", "Dell PowerEdge R720 server with 128GB RAM", "excluded"),
    ("4TB NVMe SSD", "4TB 870 EVO SATA III Solid State Drive", "excluded"),
    ("4TB NVMe SSD", "512GB-4TB SSD M.2 PCIe 4.0 NVMe", "review"),
    ("Sliding Mitre Saw", "DeWalt DE7023 Universal Extendable Mitre Saw Stand", "excluded"),
    ("Sliding Mitre Saw", 'Makita LS0816F Slide Compound Mitre Saw with Leg Stand', "exact"),
    ("NVIDIA RTX 4090", "ASUS GeForce RTX 4090 24GB graphics card", "exact"),
    ("Samsung Odyssey Neo G9 57", 'Samsung Odyssey Neo G9 57in Curved Gaming Monitor', "exact"),
    ("RTX 3080 12GB", "Dell RTX 3080 10GB GDDR6X graphics card", "excluded"),
    ("128GB DDR5 RAM", "Kingston Fury Beast 4x32GB DDR5 RAM", "exact"),
])
def test_labelled_shopping_cases(watch, title, status):
    candidate = Listing("ebay", "case", title, 200, "https://example.test/case")
    decision, _ = relevance.assess(candidate, ItemConfig(watch, [watch]), [])
    assert decision.status == status
    assert decision.eligible is (status == "exact")
    assert decision.reason


def test_alternative_still_requires_the_watch_specifications():
    item = ItemConfig("RTX 4080", ["rtx 4080"], required_terms=["16GB"], alternatives=["RTX 4080 Super"])
    listing = Listing("ebay", "alt", "RTX 4080 Super Graphics Card", 450, "https://example.test/alt")
    assert relevance.assess(listing, item, [])[0].status == "review"
    listing.title = "RTX 4080 Super 16GB Graphics Card"
    assert relevance.assess(listing, item, [])[0].status == "substitute"
    listing.title = "RTX 4080 16GB Graphics Card"
    listing.variation_group = True
    assert relevance.assess(listing, item, [])[0].status == "review"


def test_watch_reassesses_saved_candidates_and_only_alerts_eligible_listings(tmp_path, monkeypatch):
    cfg = AppConfig(db_path=str(tmp_path / "test.db"))
    conn = db.connect(cfg.db_path)
    project_id = db.create_project(conn, "Computer")
    item = ItemConfig("RTX 4090", ["4090"], max_price=1000, target_deal_price=900, normal_price=1200)
    item_id = db.create_item(conn, project_id, item)

    class SavedSearch(Source):
        name = "ebay"

        def capabilities(self):
            return SourceCapabilities(automated=True, compliance="fixture")

        def search(self, term, item):
            return [
                Listing("ebay", "wrong", "Mercedes gearbox TL4090", 200, "https://example.test/wrong"),
                Listing("ebay", "right", "ASUS GeForce RTX 4090 24GB", 800, "https://example.test/right"),
                Listing("ebay", "budget", "PNY RTX 4090 24GB", 1100, "https://example.test/budget"),
            ]

    monkeypatch.setattr(sources, "build_registry", lambda _: {"ebay": SavedSearch(cfg)})
    cfg.alerts.console = False
    assert [a.listing.external_id for a in runner.run_once(cfg, conn)] == ["right"]
    assert runner.run_once(cfg, conn) == []
    assert [r["title"] for r in db.query_matches(conn)] == ["ASUS GeForce RTX 4090 24GB"]
    assert {r["search_term"] for r in db.query_matches(conn, review=True)} == {"4090"}
    assert all(not row["under_target"] for row in db.query_matches(conn, review=True))
    with create_app(cfg).test_client() as client:
        response = client.get(f"/projects/{project_id}?view=review")
        assert response.status_code == 200
        assert b"Mercedes gearbox" in response.data
        assert b"The listing price exceeds the watch budget" in response.data

    item.max_price = 1200
    runner.reassess_item_matches(conn, item_id, item)
    assert len(db.query_matches(conn)) == 2
    item.sources = ["gumtree"]
    runner.reassess_item_matches(conn, item_id, item)
    assert not db.query_matches(conn)
    item.sources = None
    runner.reassess_item_matches(conn, item_id, item)
    assert len(db.query_matches(conn)) == 2
    assert {r["search_term"] for r in db.query_matches(conn)} == {"4090"}
