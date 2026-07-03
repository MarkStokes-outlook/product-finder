from product_finder import db, identity, runner, sources
from product_finder.config import AppConfig, ExtraSourceConfig, ItemConfig
from product_finder.models import Listing
from product_finder.sources.base import Source


# --- identity.derive_canonical_key -------------------------------------------


def test_ebay_url_with_slug_and_id():
    url = "https://www.ebay.co.uk/itm/Makita-Track-Saw/195012345678"
    assert identity.derive_canonical_key(url) == "ebay:195012345678"


def test_ebay_url_without_slug():
    url = "https://www.ebay.co.uk/itm/195012345678"
    assert identity.derive_canonical_key(url) == "ebay:195012345678"


def test_ebay_url_other_tld():
    url = "https://www.ebay.com/itm/Makita-Track-Saw/195012345678"
    assert identity.derive_canonical_key(url) == "ebay:195012345678"


def test_ebay_url_short_number_not_matched():
    # A short digit run isn't confidently an item ID (min 9 digits).
    url = "https://www.ebay.co.uk/itm/Makita-Track-Saw/12345"
    assert identity.derive_canonical_key(url) is None


def test_non_ebay_host_never_matches_even_with_itm_path():
    url = "https://example.com/itm/195012345678"
    assert identity.derive_canonical_key(url) is None


def test_ebay_substring_in_unrelated_host_not_matched():
    url = "https://not-ebay.example.com/itm/195012345678"
    assert identity.derive_canonical_key(url) is None


def test_unrelated_url_returns_none():
    assert identity.derive_canonical_key("https://www.hotukdeals.com/deals/12345") is None


def test_empty_url_returns_none():
    assert identity.derive_canonical_key("") is None


# --- db.resolve_identity ------------------------------------------------------


def _listing(source, external_id, url, price=100.0):
    return Listing(
        source=source, external_id=external_id, title="Makita track saw",
        price=price, url=url,
    )


def test_no_canonical_key_is_always_primary(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    listing = _listing("rss", "guid-1", "https://www.hotukdeals.com/deals/12345")
    listing_id, _ = db.upsert_listing(conn, listing)
    identity_id, is_primary = db.resolve_identity(conn, listing_id, listing)
    assert identity_id is None
    assert is_primary is True


def test_first_sighting_of_canonical_key_is_primary(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    listing = _listing("ebay", "195012345678", "https://www.ebay.co.uk/itm/195012345678")
    listing_id, _ = db.upsert_listing(conn, listing)
    identity_id, is_primary = db.resolve_identity(conn, listing_id, listing)
    assert identity_id is not None
    assert is_primary is True
    row = conn.execute("SELECT is_primary_sighting FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["is_primary_sighting"] == 1


def test_second_source_same_canonical_key_becomes_non_primary(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    url = "https://www.ebay.co.uk/itm/195012345678"
    first = _listing("rss", "guid-1", url)
    first_id, _ = db.upsert_listing(conn, first)
    db.resolve_identity(conn, first_id, first)

    second = _listing("ebay", "195012345678", url)
    second_id, _ = db.upsert_listing(conn, second)
    identity_id, is_primary = db.resolve_identity(conn, second_id, second)

    # The native eBay row is promoted over the earlier RSS proxy.
    assert is_primary is True
    first_row = conn.execute(
        "SELECT is_primary_sighting FROM listings WHERE id = ?", (first_id,)
    ).fetchone()
    assert first_row["is_primary_sighting"] == 0

    members = conn.execute(
        "SELECT listing_id FROM listing_identity_members WHERE identity_id = ? ORDER BY listing_id",
        (identity_id,),
    ).fetchall()
    assert {m["listing_id"] for m in members} == {first_id, second_id}


def test_native_platform_promoted_over_earlier_proxy(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    url = "https://www.ebay.co.uk/itm/195012345678"
    proxy = _listing("rss", "guid-1", url)
    proxy_id, _ = db.upsert_listing(conn, proxy)
    db.resolve_identity(conn, proxy_id, proxy)

    native = _listing("ebay", "195012345678", url)
    native_id, _ = db.upsert_listing(conn, native)
    identity_id, native_is_primary = db.resolve_identity(conn, native_id, native)

    assert native_is_primary is True
    row = conn.execute(
        "SELECT primary_listing_id FROM listing_identities WHERE id = ?", (identity_id,)
    ).fetchone()
    assert row["primary_listing_id"] == native_id


def test_third_proxy_after_native_stays_non_primary(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    url = "https://www.ebay.co.uk/itm/195012345678"
    native = _listing("ebay", "195012345678", url)
    native_id, _ = db.upsert_listing(conn, native)
    db.resolve_identity(conn, native_id, native)

    proxy = _listing("rss", "guid-2", url)
    proxy_id, _ = db.upsert_listing(conn, proxy)
    _, proxy_is_primary = db.resolve_identity(conn, proxy_id, proxy)

    assert proxy_is_primary is False
    row = conn.execute(
        "SELECT is_primary_sighting FROM listings WHERE id = ?", (proxy_id,)
    ).fetchone()
    assert row["is_primary_sighting"] == 0


def test_rescanning_same_listing_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    listing = _listing("ebay", "195012345678", "https://www.ebay.co.uk/itm/195012345678")
    listing_id, _ = db.upsert_listing(conn, listing)
    db.resolve_identity(conn, listing_id, listing)
    identity_id, is_primary = db.resolve_identity(conn, listing_id, listing)

    assert is_primary is True
    members = conn.execute(
        "SELECT COUNT(*) c FROM listing_identity_members WHERE identity_id = ?", (identity_id,)
    ).fetchone()
    assert members["c"] == 1


# --- runner.py wiring: alerts/observations/query_matches suppression ---------


class FakeSource(Source):
    def __init__(self, cfg, name, listings):
        super().__init__(cfg)
        self.name = name
        self._listings = listings

    def is_automated(self):
        return True

    def search(self, term, item):
        return self._listings

    def manual_links(self, item):
        return []


def _setup_item(conn):
    project_id = db.create_project(conn, "Workshop")
    item_id = db.create_item(
        conn, project_id,
        ItemConfig(name="Track Saw", terms=["track saw"], normal_price=350, target_deal_price=200),
    )
    return project_id, item_id


def _cfg_with_sources(tmp_path, names):
    """AppConfig with each given source name enabled, so item_sources()
    (which filters against cfg.sources.enabled_names()) reaches every
    FakeSource the test registers — "ebay" is enabled by default; anything
    else (e.g. "rss") needs an explicit enabled ExtraSourceConfig entry."""
    cfg = AppConfig(db_path=str(tmp_path / "t.db"))
    cfg.sources.extra = [
        ExtraSourceConfig(name=name, type="rss", url="https://example.com/{term}")
        for name in names
        if name != "ebay"
    ]
    return cfg


def _run_with_sources(cfg, conn, by_name):
    orig = sources.build_registry
    sources.build_registry = lambda eff_cfg: {
        name: FakeSource(eff_cfg, name, listings) for name, listings in by_name.items()
    }
    try:
        return runner.run_once(cfg, conn)
    finally:
        sources.build_registry = orig


def test_cross_source_duplicate_alerts_only_once(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _setup_item(conn)
    cfg = _cfg_with_sources(tmp_path, ["ebay", "rss"])
    url = "https://www.ebay.co.uk/itm/195012345678"
    ebay_listing = Listing(
        source="ebay", external_id="195012345678", title="Makita track saw", price=250.0, url=url,
    )
    rss_listing = Listing(
        source="rss", external_id="rss-guid-1", title="Makita track saw (RSS)", price=245.0, url=url,
    )

    alerts = _run_with_sources(cfg, conn, {"ebay": [ebay_listing], "rss": [rss_listing]})

    assert len(alerts) == 1
    # Both sightings still exist for provenance.
    assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM listing_matches").fetchone()["c"] == 2


def test_cross_source_duplicate_query_matches_suppresses_secondary(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    project_id, item_id = _setup_item(conn)
    cfg = _cfg_with_sources(tmp_path, ["ebay", "rss"])
    url = "https://www.ebay.co.uk/itm/195012345678"
    ebay_listing = Listing(
        source="ebay", external_id="195012345678", title="Makita track saw", price=250.0, url=url,
    )
    rss_listing = Listing(
        source="rss", external_id="rss-guid-1", title="Makita track saw (RSS)", price=245.0, url=url,
    )

    _run_with_sources(cfg, conn, {"ebay": [ebay_listing], "rss": [rss_listing]})

    results = db.query_matches(conn, project_id=project_id)
    assert len(results) == 1
    assert results[0]["source"] == "ebay"


def test_no_canonical_key_both_sources_alert_independently(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _setup_item(conn)
    cfg = _cfg_with_sources(tmp_path, ["ebay", "rss"])
    ebay_listing = Listing(
        source="ebay", external_id="1", title="Makita track saw", price=250.0,
        url="https://www.ebay.co.uk/itm/195012345678",
    )
    rss_listing = Listing(
        source="rss", external_id="2", title="Makita track saw (deal site)", price=245.0,
        url="https://www.hotukdeals.com/deals/999999",
    )

    alerts = _run_with_sources(cfg, conn, {"ebay": [ebay_listing], "rss": [rss_listing]})

    assert len(alerts) == 2
