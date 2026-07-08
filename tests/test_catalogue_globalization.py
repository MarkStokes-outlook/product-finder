"""Catalogue globalization (see docs/adr/0007-catalogue-globalization.md):
products becomes a global/platform-owned catalogue, decoupled from a single
item_id, with item_products as the join/context table for each item's own
tracking (match terms, target-deal-price override, archived, wanted).

Covers: the migration itself (backfill, global dedupe, non-additive rebuild
— run against a real historical backup, not just synthetic fixtures),
two items/projects sharing one global product, per-item settings living on
item_products rather than products, listings staying global, price history
aggregating across every item tracking a product, and the data-model
property project cloning (Phase 5, not built yet) will depend on: a shared
reference rather than a duplicated row.
"""

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from product_finder import db
from product_finder.config import ItemConfig
from product_finder.models import Evaluation, Listing

_REAL_BACKUP = Path(__file__).parent.parent / "data" / "product_finder.db.bak.20260705T200501"


def _match(conn, listing_id, item_id, product_id):
    db.record_match(
        conn, listing_id, item_id,
        Evaluation(grade="A", flags=[], margin_abs=100.0, margin_pct=40.0,
                   under_target=False, deal_score=55.0),
        product_id=product_id,
    )


# --- Migration: dry-run against a real historical backup ---------------------
#
# data/product_finder.db.bak.20260705T200501 predates catalogue globalization
# (products.item_id still present, no item_products table) — a real,
# previously-accumulated database, not a synthetic fixture. Copied to
# tmp_path so this test never touches the actual backup file.


def test_migration_against_real_backup_loses_no_data(tmp_path):
    if not _REAL_BACKUP.exists():
        import pytest
        pytest.skip("real backup fixture not present in this checkout")

    copy_path = tmp_path / "backup_copy.db"
    shutil.copy2(_REAL_BACKUP, copy_path)

    pre = sqlite3.connect(copy_path)
    pre.row_factory = sqlite3.Row
    pre_counts = {
        t: pre.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("projects", "items", "products", "listings",
                   "listing_matches", "product_price_observations")
    }
    pre_products = pre.execute(
        "SELECT id, item_id, manufacturer, model, match_terms, target_deal_price, "
        "archived, wanted FROM products"
    ).fetchall()
    pre.close()

    conn = db.connect(copy_path)  # triggers _migrate_catalogue_globalization

    # No data lost across any table the migration touches.
    assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == pre_counts["listings"]
    assert conn.execute("SELECT COUNT(*) c FROM listing_matches").fetchone()["c"] == pre_counts["listing_matches"]
    assert (conn.execute("SELECT COUNT(*) c FROM product_price_observations").fetchone()["c"]
            == pre_counts["product_price_observations"])

    # products no longer carries item_id (the rebuild — see
    # _migrate_catalogue_globalization step 3).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
    assert "item_id" not in cols
    assert "normal_price" not in cols  # long-dead column, dropped alongside

    # Every pre-migration (item, product) tracking relationship is
    # reproduced exactly as an item_products row — global dedupe found
    # nothing to merge in this real dataset (verified separately below),
    # so this should be a clean 1:1 backfill.
    assert conn.execute("SELECT COUNT(*) c FROM item_products").fetchone()["c"] == len(pre_products)
    for p in pre_products:
        ip = db.get_item_product(conn, p["item_id"], p["id"])
        assert ip is not None, f"product {p['id']} (item {p['item_id']}) lost in migration"
        assert json.loads(ip["match_terms"]) == json.loads(p["match_terms"] or "[]")
        assert ip["target_deal_price"] == p["target_deal_price"]
        assert bool(ip["archived"]) == bool(p["archived"])
        assert bool(ip["wanted"]) == bool(p["wanted"])

    # No orphaned foreign keys anywhere.
    orphan_matches = conn.execute(
        "SELECT COUNT(*) c FROM listing_matches m WHERE m.product_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM products p WHERE p.id = m.product_id)"
    ).fetchone()["c"]
    assert orphan_matches == 0
    orphan_ip = conn.execute(
        "SELECT COUNT(*) c FROM item_products ip WHERE "
        "NOT EXISTS (SELECT 1 FROM items i WHERE i.id = ip.item_id) "
        "OR NOT EXISTS (SELECT 1 FROM products p WHERE p.id = ip.product_id)"
    ).fetchone()["c"]
    assert orphan_ip == 0


def test_migration_is_idempotent_on_real_backup(tmp_path):
    if not _REAL_BACKUP.exists():
        import pytest
        pytest.skip("real backup fixture not present in this checkout")
    copy_path = tmp_path / "backup_copy.db"
    shutil.copy2(_REAL_BACKUP, copy_path)

    db.connect(copy_path).close()  # first connect: migration runs
    after_first = sqlite3.connect(copy_path)
    after_first.row_factory = sqlite3.Row
    counts_1 = {
        t: after_first.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("products", "item_products", "listing_matches", "product_price_observations")
    }
    after_first.close()

    db.connect(copy_path).close()  # second connect: no-ops (no item_id column left)
    after_second = sqlite3.connect(copy_path)
    after_second.row_factory = sqlite3.Row
    counts_2 = {
        t: after_second.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("products", "item_products", "listing_matches", "product_price_observations")
    }
    after_second.close()

    assert counts_1 == counts_2


def test_migration_survives_concurrent_connect_race(tmp_path):
    """Regression test for a real production incident (2026-07-08): `watch`
    and `web` both connecting to a not-yet-migrated database within moments
    of each other. Both see `item_id` present (the pre-lock check) and both
    decide to migrate; the loser blocks on BEGIN IMMEDIATE until the winner
    commits, then — without the post-lock re-check this test guards —
    would run step 1's SELECT against a `products` table the winner has
    already rebuilt without `item_id`, crashing with "no such column:
    item_id" on every subsequent watch tick (next_full_run never advances
    past an exception, so it retried forever). Two real threads, each with
    its own connection to the same file, racing as tightly as a Barrier
    can make them — not just two sequential connect() calls, which
    wouldn't exercise the lock-wait path at all.

    A plain "database is locked" from the loser losing the BEGIN IMMEDIATE
    race is tolerated and retried here (a handful of times, like a real
    caller reconnecting on the next watch tick would) — that's pre-existing,
    already-accepted SQLite-under-concurrent-writers behaviour this
    codebase already has busy_timeout for for, and it's self-healing (the
    very next attempt sees the now-completed migration and no-ops). What
    this test must never tolerate is the schema-corruption class of error
    (no such column / duplicate column name) the fix above eliminates —
    that one doesn't self-heal, because next_full_run never advances past
    an exception, so `watch` retries the *same* broken state forever."""
    path = tmp_path / "race.db"
    _make_pre_globalization_db(path)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _connect():
        barrier.wait()
        for attempt in range(5):
            try:
                db.connect(path).close()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 4:
                    errors.append(exc)
                    return
                time.sleep(0.05)
            except BaseException as exc:  # noqa: BLE001 — the whole point is catching this
                errors.append(exc)
                return

    threads = [threading.Thread(target=_connect) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"migration race raised: {errors}"

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
    assert "item_id" not in cols
    # Exactly one migration's worth of data — a second, half-run attempt
    # duplicating the backfill would show up here as extra rows.
    assert conn.execute("SELECT COUNT(*) c FROM item_products").fetchone()["c"] == 2
    conn.close()


# --- Migration: synthetic cross-item duplicate (real backup had none) --------


def _make_pre_globalization_db(path):
    """A products table exactly as it existed before catalogue
    globalization — two items independently created "the same" product
    (same manufacturer/model identity key), the fragmentation this epic
    fixes — plus matches and price observations against each, so the
    merge/reconciliation path (not just the backfill path) is exercised."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, sources TEXT);
        CREATE TABLE items (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
            name TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', max_price REAL,
            normal_price REAL, target_deal_price REAL, notes TEXT DEFAULT '',
            terms TEXT NOT NULL DEFAULT '[]', exclude_terms TEXT NOT NULL DEFAULT '[]',
            sources TEXT, archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE products (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL,
            manufacturer TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            match_terms TEXT NOT NULL DEFAULT '[]', normal_price REAL,
            target_deal_price REAL, archived INTEGER NOT NULL DEFAULT 0,
            msrp REAL, typical_new_price REAL, typical_used_price REAL,
            canonical_price_url TEXT, price_search_checked INTEGER NOT NULL DEFAULT 0,
            last_price_check_at TEXT, last_price_check_ok INTEGER,
            price_trend_pct REAL, price_trend_confidence REAL NOT NULL DEFAULT 0,
            wanted INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE product_price_observations (id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL, price REAL NOT NULL, source TEXT NOT NULL,
            observed_at TEXT NOT NULL);
        CREATE TABLE product_new_price_history (id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL, price REAL NOT NULL, domain TEXT NOT NULL,
            observed_at TEXT NOT NULL);
        CREATE TABLE product_price_candidates (id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL,
            price REAL NOT NULL, currency TEXT NOT NULL, confidence REAL NOT NULL,
            found_at TEXT NOT NULL);
        CREATE TABLE product_suggestions (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL,
            manufacturer TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
            sighting_count INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'ebay-structured',
            example_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw_samples TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE listings (id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            external_id TEXT NOT NULL, title TEXT NOT NULL, price REAL NOT NULL,
            currency TEXT DEFAULT 'GBP', url TEXT NOT NULL, location TEXT DEFAULT '',
            description TEXT DEFAULT '', condition TEXT DEFAULT '', first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, UNIQUE(source, external_id));
        CREATE TABLE listing_matches (id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL, product_id INTEGER, grade TEXT, deal_score REAL,
            margin_abs REAL, margin_pct REAL, under_target INTEGER DEFAULT 0,
            flags TEXT DEFAULT '[]', matched_at TEXT NOT NULL, UNIQUE(listing_id, item_id));
        CREATE TABLE alerts_sent (id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL,
            channel TEXT NOT NULL, sent_at TEXT NOT NULL, UNIQUE(match_id, channel));
        CREATE TABLE source_settings (name TEXT PRIMARY KEY, enabled INTEGER,
            ebay_app_id TEXT DEFAULT '', ebay_cert_id TEXT DEFAULT '', ebay_env TEXT DEFAULT '',
            first_seen TEXT);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE listing_identities (id INTEGER PRIMARY KEY, canonical_key TEXT UNIQUE NOT NULL,
            primary_listing_id INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE listing_identity_members (id INTEGER PRIMARY KEY, identity_id INTEGER NOT NULL,
            listing_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
            matched_by TEXT NOT NULL DEFAULT 'canonical_url', created_at TEXT NOT NULL,
            UNIQUE(identity_id, listing_id));
        CREATE TABLE listing_duplicates (id INTEGER PRIMARY KEY, listing_a INTEGER NOT NULL,
            listing_b INTEGER NOT NULL, item_id INTEGER NOT NULL, confidence REAL NOT NULL,
            signals TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
            kept_listing_id INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            decided_at TEXT, UNIQUE(listing_a, listing_b));
        CREATE TABLE auction_snapshots (id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL,
            source TEXT NOT NULL, observed_at TEXT NOT NULL, current_bid_price REAL,
            currency TEXT DEFAULT 'GBP', bid_count INTEGER, buy_it_now_price REAL,
            shipping_price REAL, end_time TEXT, watch_count INTEGER, view_count INTEGER,
            raw_payload TEXT);
        CREATE TABLE source_runs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, run_at TEXT NOT NULL,
            ok INTEGER NOT NULL, searches INTEGER NOT NULL DEFAULT 0, listings INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0, last_error TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
            new_listings INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
            catalogue_matches INTEGER NOT NULL DEFAULT 0, deals_found INTEGER NOT NULL DEFAULT 0);
        """
    )
    conn.execute("INSERT INTO projects (id, slug, name) VALUES (1, 'workshop', 'Workshop')")
    conn.execute("INSERT INTO projects (id, slug, name) VALUES (2, 'garage', 'Garage')")
    conn.execute(
        "INSERT INTO items (id, project_id, name, terms) VALUES "
        "(1, 1, 'Mitre Saw A', '[\"mitre saw\"]'), (2, 2, 'Mitre Saw B', '[\"mitre saw\"]')"
    )
    # Item 1's own product row for "Makita LS1019L".
    conn.execute(
        "INSERT INTO products (id, item_id, manufacturer, model, match_terms, target_deal_price) "
        "VALUES (1, 1, 'Makita', 'LS1019L', '[\"ls1019l\"]', 300.0)"
    )
    # Item 2 independently created the *same* real product, spelled slightly
    # differently — this is the cross-item duplicate the global dedupe pass
    # (inside _migrate_catalogue_globalization) must fold together.
    conn.execute(
        "INSERT INTO products (id, item_id, manufacturer, model, match_terms, target_deal_price) "
        "VALUES (2, 2, 'makita', 'ls1019l', '[\"makita ls1019l\"]', 280.0)"
    )
    conn.execute(
        "INSERT INTO listings (id, source, external_id, title, price, url, first_seen, last_seen) "
        "VALUES (1, 'ebay', 'E1', 'Makita LS1019L saw', 250.0, 'https://x/1', '2026-01-01T00:00:00', "
        "'2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, product_id, matched_at) "
        "VALUES (1, 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO listings (id, source, external_id, title, price, url, first_seen, last_seen) "
        "VALUES (2, 'ebay', 'E2', 'Makita LS1019L saw #2', 260.0, 'https://x/2', '2026-01-02T00:00:00', "
        "'2026-01-02T00:00:00')"
    )
    conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, product_id, matched_at) "
        "VALUES (2, 2, 2, '2026-01-02T00:00:00')"
    )
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (1, 250.0, 'ebay', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (2, 260.0, 'ebay', '2026-01-02T00:00:00')"
    )
    conn.commit()
    conn.close()


def test_migration_merges_cross_item_duplicates_and_reconciles_item_products(tmp_path):
    path = tmp_path / "t.db"
    _make_pre_globalization_db(path)

    conn = db.connect(path)  # triggers the migration, including global dedupe

    # Exactly one global product survives.
    assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 1
    surviving = conn.execute("SELECT * FROM products").fetchone()
    assert surviving["manufacturer"] == "Makita"  # older row (id 1) kept

    # Both items retain their own independent tracking of the merged product.
    assert conn.execute("SELECT COUNT(*) c FROM item_products").fetchone()["c"] == 2
    item1_product = db.get_item_product(conn, 1, surviving["id"])
    item2_product = db.get_item_product(conn, 2, surviving["id"])
    assert item1_product is not None and item2_product is not None
    assert json.loads(item1_product["match_terms"]) == ["ls1019l"]
    assert json.loads(item2_product["match_terms"]) == ["makita ls1019l"]
    assert item1_product["target_deal_price"] == 300.0  # each item's own override preserved
    assert item2_product["target_deal_price"] == 280.0

    # Both listing_matches now point at the one surviving product; both
    # price observations are preserved and combined.
    match_products = {
        r["product_id"] for r in conn.execute("SELECT product_id FROM listing_matches")
    }
    assert match_products == {surviving["id"]}
    obs_prices = sorted(
        r["price"] for r in conn.execute("SELECT price FROM product_price_observations")
    )
    assert obs_prices == [250.0, 260.0]


# --- Two items/projects sharing one global product (fresh schema) ------------


def _seed(tmp_path, name="t.db"):
    cfg_conn = db.connect(tmp_path / name)
    return cfg_conn


def test_two_items_in_different_projects_share_one_global_product(tmp_path):
    conn = _seed(tmp_path)
    project_a = db.create_project(conn, "Project A")
    project_b = db.create_project(conn, "Project B")
    item_a = db.create_item(conn, project_a, ItemConfig(name="Mitre Saw A", terms=["mitre saw"]))
    item_b = db.create_item(conn, project_b, ItemConfig(name="Mitre Saw B", terms=["mitre saw"]))

    product_a = db.create_product(conn, item_a, "Makita", "LS1019L", ["ls1019l"], 900, 700, 650)
    product_b = db.create_product(conn, item_b, "makita", "ls1019l", ["makita saw"], None, None, 600)

    assert product_a == product_b  # same global product, not two

    # Global market fields are shared and were set at first creation only —
    # the second item's create_product call never overwrote them.
    global_row = db.get_product(conn, product_a)
    assert global_row["msrp"] == 900
    assert global_row["typical_new_price"] == 700

    # Each item's own tracking (match terms, target price) is independent.
    ip_a = db.get_item_product(conn, item_a, product_a)
    ip_b = db.get_item_product(conn, item_b, product_b)
    assert ip_a["id"] != ip_b["id"]
    assert json.loads(ip_a["match_terms"]) == ["ls1019l"]
    assert json.loads(ip_b["match_terms"]) == ["makita saw"]
    assert ip_a["target_deal_price"] == 650
    assert ip_b["target_deal_price"] == 600


# --- Per-item settings live on item_products, not products --------------------


def test_products_table_has_no_item_scoped_columns(tmp_path):
    conn = _seed(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    assert "match_terms" not in cols
    assert "target_deal_price" not in cols
    assert "archived" not in cols
    assert "wanted" not in cols
    assert "item_id" not in cols
    # Market/identity fields remain.
    assert {"manufacturer", "model", "msrp", "typical_new_price", "typical_used_price"} <= cols


def test_item_products_table_has_the_item_scoped_columns(tmp_path):
    conn = _seed(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(item_products)")}
    assert {"item_id", "product_id", "match_terms", "target_deal_price", "archived", "wanted"} <= cols


def test_one_items_archived_toggle_never_affects_another_items_tracking(tmp_path):
    conn = _seed(tmp_path)
    project_id = db.create_project(conn, "Workshop")
    item_a = db.create_item(conn, project_id, ItemConfig(name="Item A", terms=["saw"]))
    item_b = db.create_item(conn, project_id, ItemConfig(name="Item B", terms=["saw"]))
    product_id = db.create_product(conn, item_a, "Makita", "LS1019L", ["ls1019l"], None, None, None)
    db.create_product(conn, item_b, "Makita", "LS1019L", ["ls1019l"], None, None, None)

    ip_a_id = db.get_item_product(conn, item_a, product_id)["id"]
    db.set_product_archived(conn, ip_a_id, True)

    assert db.get_item_product(conn, item_a, product_id)["archived"] == 1
    assert db.get_item_product(conn, item_b, product_id)["archived"] == 0  # untouched
    # The global product itself has no archived concept to disturb.
    assert "archived" not in dict(db.get_product(conn, product_id)).keys()


# --- Listings remain global -----------------------------------------------


def test_listing_data_has_no_item_or_project_scoping(tmp_path):
    conn = _seed(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    assert "item_id" not in cols
    assert "project_id" not in cols


def test_two_items_matching_the_same_listing_share_one_listings_row(tmp_path):
    conn = _seed(tmp_path)
    project_id = db.create_project(conn, "Workshop")
    item_a = db.create_item(conn, project_id, ItemConfig(name="Item A", terms=["saw"]))
    item_b = db.create_item(conn, project_id, ItemConfig(name="Item B", terms=["saw"]))

    listing = Listing(source="ebay", external_id="E1", title="Makita saw", price=100.0, url="https://x/1")
    listing_id_1, is_new_1 = db.upsert_listing(conn, listing)
    listing_id_2, is_new_2 = db.upsert_listing(conn, listing)  # same source+external_id

    assert listing_id_1 == listing_id_2
    assert is_new_1 and not is_new_2
    assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 1

    _match(conn, listing_id_1, item_a, None)
    _match(conn, listing_id_1, item_b, None)
    assert conn.execute(
        "SELECT COUNT(*) c FROM listing_matches WHERE listing_id = ?", (listing_id_1,)
    ).fetchone()["c"] == 2


# --- Price history aggregates across every item tracking a product -----------


def test_price_observations_aggregate_across_items_sharing_a_product(tmp_path):
    conn = _seed(tmp_path)
    project_id = db.create_project(conn, "Workshop")
    item_a = db.create_item(conn, project_id, ItemConfig(name="Item A", terms=["saw"]))
    item_b = db.create_item(conn, project_id, ItemConfig(name="Item B", terms=["saw"]))

    product_a = db.create_product(conn, item_a, "Makita", "LS1019L", ["ls1019l"], None, None, None)
    product_b = db.create_product(conn, item_b, "Makita", "LS1019L", ["ls1019l"], None, None, None)
    assert product_a == product_b

    # Item A alone would compute a median of 100; item B alone a median of
    # 300 — only the combined set gives 200, proving both items' evidence
    # feeds one shared observation history.
    db.record_price_observation(conn, product_a, 100.0, "ebay")
    db.record_price_observation(conn, product_b, 300.0, "ebay")

    combined = db.get_product(conn, product_a)
    assert combined["typical_used_price"] == 200.0


# --- Clone-by-reference data-model property (Phase 5 not built yet) ----------
#
# Project sharing/cloning itself (EPIC-105) is explicitly out of scope for
# this branch. What this proves is the underlying data-model property
# ADR-0006's forward-looking note depends on: a second project referencing
# the same manufacturer/model resolves to the *same* global product rather
# than duplicating it — the precondition for a future clone to reference
# rather than copy.


def test_second_project_referencing_same_product_shares_it_not_duplicates(tmp_path):
    conn = _seed(tmp_path)
    original_project = db.create_project(conn, "Original")
    cloned_project = db.create_project(conn, "Cloned")
    original_item = db.create_item(
        conn, original_project, ItemConfig(name="Mitre Saw", terms=["mitre saw"])
    )
    # Simulates what a Phase 5 clone's item-copy step would do: a new item
    # in a different project, tracking "the same" product by name.
    cloned_item = db.create_item(
        conn, cloned_project, ItemConfig(name="Mitre Saw", terms=["mitre saw"])
    )

    original_product = db.create_product(
        conn, original_item, "Makita", "LS1019L", ["ls1019l"], 900, 700, 650
    )
    cloned_product = db.create_product(
        conn, cloned_item, "Makita", "LS1019L", ["ls1019l"], 900, 700, 650
    )

    assert original_product == cloned_product  # reference, not a duplicate row
    assert conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 1

    # Each project's item still has its own independent item_products
    # tracking, exactly as ADR-0006 requires for a clone (mutating one
    # project's tracking must never affect the other's).
    original_ip = db.get_item_product(conn, original_item, original_product)
    cloned_ip = db.get_item_product(conn, cloned_item, cloned_product)
    assert original_ip["id"] != cloned_ip["id"]
    db.set_product_archived(conn, cloned_ip["id"], True)
    assert db.get_item_product(conn, original_item, original_product)["archived"] == 0
