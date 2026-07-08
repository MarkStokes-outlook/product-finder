"""SQLite storage: projects, items, listings, matches, alerts.

Projects and items are editable in the database (via the web UI); the YAML
config seeds them when the database is empty and can be re-imported with
`import-config`. postcode/alerts stay config-driven.

Sources are a special case: their definitions (URL templates, type) always
come from YAML — no duplication into the DB, so editing config.yaml always
takes effect immediately. Only per-source *overrides* (enabled, eBay API
keys) live in the `source_settings` table, overlaid onto the YAML config at
runtime by `effective_config()`. A source with no override row just uses its
YAML defaults untouched.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from . import catalogue, duplicates, identity, price_trend
from .config import AppConfig, ItemConfig, ProjectConfig
from .models import Evaluation, Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    sources TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    max_price REAL,
    normal_price REAL,
    target_deal_price REAL,
    notes TEXT DEFAULT '',
    terms TEXT NOT NULL DEFAULT '[]',
    exclude_terms TEXT NOT NULL DEFAULT '[]',
    sources TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT ''
);
-- item_products is the join/context table between a project's item and a
-- global catalogue product (see docs/adr/0007-catalogue-globalization.md):
-- match_terms/target_deal_price/archived/wanted are this item's own
-- tracking of the product, never the product's own identity or market
-- data. A product may be tracked by many items across many projects;
-- each gets its own row here rather than its own copy of the product.
CREATE TABLE IF NOT EXISTS item_products (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    match_terms TEXT NOT NULL DEFAULT '[]',
    target_deal_price REAL,
    archived INTEGER NOT NULL DEFAULT 0,
    wanted INTEGER NOT NULL DEFAULT 1,
    UNIQUE(item_id, product_id)
);
CREATE TABLE IF NOT EXISTS product_price_observations (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price REAL NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_new_price_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price REAL NOT NULL,
    domain TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_price_candidates (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    confidence REAL NOT NULL,
    found_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_suggestions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    sighting_count INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'ebay-structured',
    example_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(item_id, manufacturer, model)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT DEFAULT 'GBP',
    url TEXT NOT NULL,
    location TEXT DEFAULT '',
    description TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS listing_matches (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    grade TEXT,
    deal_score REAL,
    margin_abs REAL,
    margin_pct REAL,
    under_target INTEGER DEFAULT 0,
    flags TEXT DEFAULT '[]',
    matched_at TEXT NOT NULL,
    UNIQUE(listing_id, item_id)
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES listing_matches(id),
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(match_id, channel)
);
CREATE TABLE IF NOT EXISTS source_settings (
    name TEXT PRIMARY KEY,
    enabled INTEGER,               -- NULL = inherit the YAML default
    ebay_app_id TEXT DEFAULT '',
    ebay_cert_id TEXT DEFAULT '',
    ebay_env TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS listing_identities (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT UNIQUE NOT NULL,
    primary_listing_id INTEGER NOT NULL REFERENCES listings(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_identity_members (
    id INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES listing_identities(id),
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    status TEXT NOT NULL DEFAULT 'confirmed',   -- v1 only ever writes 'confirmed'
    matched_by TEXT NOT NULL DEFAULT 'canonical_url',
    created_at TEXT NOT NULL,
    UNIQUE(identity_id, listing_id)
);
CREATE TABLE IF NOT EXISTS listing_duplicates (
    id INTEGER PRIMARY KEY,
    listing_a INTEGER NOT NULL REFERENCES listings(id),
    listing_b INTEGER NOT NULL REFERENCES listings(id),  -- listing_a < listing_b, always
    item_id INTEGER NOT NULL REFERENCES items(id),       -- match scope it was detected in
    confidence REAL NOT NULL,                             -- 0-100, display/ranking only
    signals TEXT NOT NULL DEFAULT '{}',                   -- JSON, see duplicates.evaluate_pair
    status TEXT NOT NULL DEFAULT 'pending',               -- pending | confirmed | dismissed
    kept_listing_id INTEGER REFERENCES listings(id),      -- set on confirm
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(listing_a, listing_b)
);
CREATE TABLE IF NOT EXISTS auction_snapshots (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    current_bid_price REAL,
    currency TEXT DEFAULT 'GBP',
    bid_count INTEGER,
    buy_it_now_price REAL,
    shipping_price REAL,
    end_time TEXT,
    watch_count INTEGER,
    view_count INTEGER,
    raw_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_auction_snapshots_listing ON auction_snapshots(listing_id, observed_at);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    run_at TEXT NOT NULL,
    ok INTEGER NOT NULL,            -- 1 = cycle completed with zero errors
    searches INTEGER NOT NULL DEFAULT 0,
    listings INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source, run_at);
-- One row per GET /out/<listing_id> redirect attempt (Marketplace Outbound
-- Gateway — see outbound.py, ARCHITECTURE.md "Marketplace outbound
-- gateway", docs/adr/0002-affiliate-link-redirect-and-tracking.md).
-- `source` echoes listings.source at click time (not a join), so a click
-- record stays meaningful even if a listing's source were ever to change.
-- `project_id` is nullable: set when the click originated from a
-- project-scoped surface, NULL for dashboard/auctions/offers clicks.
-- `user_id` is nullable and always NULL until Phase 3 (EPIC-103) starts
-- writing it — the column exists now so that phase doesn't need a second
-- migration on this table.
CREATE TABLE IF NOT EXISTS listing_clicks (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    project_id INTEGER REFERENCES projects(id),
    source TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'success',
    affiliate_applied INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER,
    clicked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_clicks_listing ON listing_clicks(listing_id, clicked_at);
"""

# Columns added since the first release; applied to pre-existing databases.
_MIGRATIONS = [
    ("projects", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "sources", "TEXT"),
    ("items", "terms", "TEXT NOT NULL DEFAULT '[]'"),
    ("items", "exclude_terms", "TEXT NOT NULL DEFAULT '[]'"),
    ("items", "sources", "TEXT"),
    ("items", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("listing_matches", "product_id", "INTEGER REFERENCES products(id)"),
    ("products", "msrp", "REAL"),
    ("products", "typical_new_price", "REAL"),
    ("products", "typical_used_price", "REAL"),
    ("listings", "buying_options", "TEXT DEFAULT '[]'"),
    ("listings", "bid_count", "INTEGER"),
    ("listings", "end_time", "TEXT"),
    ("listings", "last_poll_at", "TEXT"),
    ("listings", "sold_captured", "INTEGER NOT NULL DEFAULT 0"),
    ("listings", "brand_checked", "INTEGER NOT NULL DEFAULT 0"),
    ("product_suggestions", "raw_samples", "TEXT NOT NULL DEFAULT '[]'"),
    ("products", "canonical_price_url", "TEXT"),
    ("products", "price_search_checked", "INTEGER NOT NULL DEFAULT 0"),
    ("products", "last_price_check_at", "TEXT"),
    ("products", "last_price_check_ok", "INTEGER"),
    ("products", "price_trend_pct", "REAL"),
    ("products", "price_trend_confidence", "REAL NOT NULL DEFAULT 0"),
    ("listings", "is_primary_sighting", "INTEGER NOT NULL DEFAULT 1"),
    ("listings", "image_url", "TEXT"),
    # wanted=0 -> "knowledge only": still matched (identification, price
    # history, keeps listings out of suggestion churn) but never alerted or
    # shown as a deal. Distinct from archived, which stops matching entirely.
    ("products", "wanted", "INTEGER NOT NULL DEFAULT 1"),
    # Distinct from `price`'s BIN-preferring fallback — see Listing.current_bid_price
    # docstring. Bug fix (2026-07-08): a BIN+AUCTION listing was displaying its
    # Buy It Now price labelled as "current bid" because nothing persisted
    # currentBidPrice separately until the auction-close poller reached it.
    ("listings", "current_bid_price", "REAL"),
    ("listings", "buy_it_now_price", "REAL"),
    # Connector Maturity phase (roadmap: "Become the best acquisition
    # platform..." Phase A) — per-run stats beyond the original
    # searches/listings/errors, so the Sources page can report averages and
    # per-run yield (new listings, cross-source duplicates suppressed,
    # catalogue matches, deals found) rather than only pass/fail health.
    ("source_runs", "duration_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("source_runs", "new_listings", "INTEGER NOT NULL DEFAULT 0"),
    ("source_runs", "duplicates", "INTEGER NOT NULL DEFAULT 0"),
    ("source_runs", "catalogue_matches", "INTEGER NOT NULL DEFAULT 0"),
    ("source_runs", "deals_found", "INTEGER NOT NULL DEFAULT 0"),
    # Durable per-source metadata that must survive source_runs' 30-day
    # retention pruning — set once on a source's first recorded run, never
    # overwritten (see record_source_run).
    ("source_settings", "first_seen", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _pending_migrations(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """_MIGRATIONS entries not yet applied, as of right now. A plain read —
    callers decide whether/when to act on it, and must re-derive this
    fresh after acquiring any lock rather than reusing an earlier result
    (see connect()'s race-safety comment)."""
    pending = []
    for table, column, decl in _MIGRATIONS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if table == "products" and column == "wanted" and "item_id" not in cols:
            # wanted moved to item_products under catalogue globalization
            # (see docs/adr/0007-catalogue-globalization.md) — only add it
            # back to `products` for a database that still has `item_id`
            # (pre-migration), where _migrate_catalogue_globalization's
            # backfill needs to read it. A fresh database, or one already
            # rebuilt, must never regain this column here.
            continue
        if column not in cols:
            pending.append((table, column, decl))
    return pending


def _apply_migrations(conn: sqlite3.Connection, pending: list[tuple[str, str, str]]) -> None:
    for table, column, decl in pending:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if column in cols:
            continue  # a racing connection already added this one — see connect()
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if table == "products" and column == "typical_new_price" and "normal_price" in cols:
            # normal_price predates the msrp/typical_new_price split —
            # carry forward any existing value, since it was functionally
            # "the new price" before the split. Only runs the moment this
            # column is added (i.e. once per database, ever) — this used
            # to run unconditionally on every single connect(), which
            # meant every web request and every watch tick took a write
            # lock for a no-op UPDATE, and enough of them colliding
            # produced "database is locked". `normal_price` itself was
            # dropped from `products` by the catalogue globalization
            # rebuild (see _migrate_catalogue_globalization) — a database
            # created after that ADR never had the column at all, so this
            # guard keeps that (now purely historical) path a no-op
            # instead of erroring on a missing column.
            conn.execute(
                "UPDATE products SET typical_new_price = normal_price "
                "WHERE typical_new_price IS NULL AND normal_price IS NOT NULL"
            )


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path) if db_path != ":memory:" else db_path
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the web UI (reader) and a background `watch`/run-once process
    # (writer) hit the DB concurrently without "database is locked" errors.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    # Cheap, lock-free check first — the common case (already-migrated
    # database, i.e. every connect() after the very first one ever) must
    # stay lock-free, per the "database is locked" incident recorded in
    # _apply_migrations' comment. Only escalate to a write lock when there
    # is actually something to do.
    if _pending_migrations(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-derive pending migrations *after* acquiring the write
            # lock, not reuse the pre-lock check above — a second
            # connection can reach this same "something's pending" branch
            # at nearly the same moment (real production incident,
            # 2026-07-08: `watch` and `web` both connecting within moments
            # of each other); the loser blocks on BEGIN IMMEDIATE until
            # the winner commits, then must only ALTER what's *still*
            # actually missing, not blindly repeat the pre-lock list —
            # otherwise it hits "duplicate column name" for every column
            # the winner already added.
            _apply_migrations(conn, _pending_migrations(conn))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _migrate_catalogue_globalization(conn)
    return conn


def _migrate_catalogue_globalization(conn: sqlite3.Connection) -> None:
    """One-time, idempotent migration to the catalogue-globalization schema
    (see docs/adr/0007-catalogue-globalization.md): products stops being
    item-scoped and becomes a shared/global catalogue, with item_products as
    the new join/context table.

    No-ops immediately (cheap PRAGMA check, no write) once already applied —
    detected by `products` no longer having an `item_id` column, which only
    the final rebuild step below removes. Safe to call on every connect().

    Three steps, run in one transaction so a crash never leaves a
    half-migrated database:

    1. Backfill: one item_products row per pre-existing product, exactly
       reproducing today's tracking relationship (zero behaviour change).
    2. Global dedupe: fold cross-item duplicate products (same manufacturer/
       model identity key) into one, reconciling item_products so no item
       loses its own match_terms/target_deal_price/archived/wanted state and
       no item ends up with two rows for the same product.
    3. Rebuild `products` without `item_id` (and the long-dead `normal_price`
       column) — SQLite has no in-place DROP COLUMN for a column with a
       REFERENCES clause, so this is the first non-additive migration in
       this codebase. Gated on step 1/2 having already run in this same
       transaction, so a failure here still leaves item_products fully
       populated and rollback-safe.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
    if "item_id" not in cols:
        return  # already migrated

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check after acquiring the write lock, not just before it. Two
        # connections can both see "item_id present" and both decide to
        # migrate before either has taken the lock — real production
        # scenario: `watch` and `web` both connecting to a not-yet-migrated
        # database within moments of each other. The loser blocks here on
        # BEGIN IMMEDIATE until the winner commits, then must not proceed:
        # without this re-check it would run step 1's SELECT against a
        # `products` table the winner already rebuilt without `item_id`,
        # crashing every caller with "no such column: item_id" (real
        # incident, 2026-07-08 — see docs/implementation-notes/).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
        if "item_id" not in cols:
            conn.execute("ROLLBACK")
            return
        # Step 1: backfill — idempotent via the NOT IN guard, so re-running
        # after a partial failure never duplicates a row.
        conn.execute(
            "INSERT INTO item_products (item_id, product_id, match_terms, "
            "target_deal_price, archived, wanted) "
            "SELECT item_id, id, match_terms, target_deal_price, archived, wanted "
            "FROM products WHERE id NOT IN (SELECT product_id FROM item_products)"
        )

        # Step 2: global dedupe — same grouping catalogue.model_key already
        # provides for per-item dedupe (find_duplicate_products), just
        # without item_id in the key.
        rows = conn.execute("SELECT id, manufacturer, model FROM products ORDER BY id").fetchall()
        groups: dict[tuple, list[int]] = {}
        for row in rows:
            key = (catalogue.model_key(row["manufacturer"]), catalogue.model_key(row["model"]))
            groups.setdefault(key, []).append(row["id"])
        for ids in groups.values():
            if len(ids) < 2:
                continue
            keep_id, *dup_ids = ids  # oldest (lowest id) kept, as elsewhere
            for dup_id in dup_ids:
                _merge_products_locked(conn, keep_id, dup_id)

        # Step 3: rebuild products without item_id/normal_price. Every
        # column below already exists on the pre-migration table (added by
        # _MIGRATIONS over time) — this only changes which columns survive.
        conn.execute(
            """
            CREATE TABLE products_new (
                id INTEGER PRIMARY KEY,
                manufacturer TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                msrp REAL,
                typical_new_price REAL,
                typical_used_price REAL,
                canonical_price_url TEXT,
                price_search_checked INTEGER NOT NULL DEFAULT 0,
                last_price_check_at TEXT,
                last_price_check_ok INTEGER,
                price_trend_pct REAL,
                price_trend_confidence REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO products_new (id, manufacturer, model, msrp, typical_new_price, "
            "typical_used_price, canonical_price_url, price_search_checked, "
            "last_price_check_at, last_price_check_ok, price_trend_pct, price_trend_confidence) "
            "SELECT id, manufacturer, model, msrp, typical_new_price, typical_used_price, "
            "canonical_price_url, price_search_checked, last_price_check_at, last_price_check_ok, "
            "price_trend_pct, price_trend_confidence FROM products"
        )
        conn.execute("DROP TABLE products")
        conn.execute("ALTER TABLE products_new RENAME TO products")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- Config import / DB-backed project & item loading -----------------------


def import_config(conn: sqlite3.Connection, cfg: AppConfig) -> int:
    """Upsert projects/items from the YAML config. Returns items written."""
    count = 0
    for project in cfg.projects:
        conn.execute(
            "INSERT INTO projects (slug, name, sources) VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name = excluded.name, sources = excluded.sources",
            (
                project.slug,
                project.name,
                json.dumps(project.sources) if project.sources is not None else None,
            ),
        )
        project_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (project.slug,)
        ).fetchone()["id"]
        for item in project.items:
            conn.execute(
                "INSERT INTO items (project_id, name, priority, max_price, normal_price, "
                "target_deal_price, notes, terms, exclude_terms, sources) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, name) DO UPDATE SET "
                "priority = excluded.priority, max_price = excluded.max_price, "
                "normal_price = excluded.normal_price, target_deal_price = excluded.target_deal_price, "
                "notes = excluded.notes, terms = excluded.terms, "
                "exclude_terms = excluded.exclude_terms, sources = excluded.sources",
                (
                    project_id,
                    item.name,
                    item.priority,
                    item.max_price,
                    item.normal_price,
                    item.target_deal_price,
                    item.notes,
                    json.dumps(item.terms),
                    json.dumps(item.exclude_terms),
                    json.dumps(item.sources) if item.sources is not None else None,
                ),
            )
            count += 1
    conn.commit()
    return count


def seed_from_config_if_empty(conn: sqlite3.Connection, cfg: AppConfig) -> bool:
    """Seed the DB from YAML if it holds no projects, or only term-less items
    (a database created before terms moved into SQLite)."""
    has_projects = conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] > 0
    if has_projects:
        any_terms = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE terms != '[]'"
        ).fetchone()["c"] > 0
        if any_terms:
            return False
    if not cfg.projects:
        return False
    import_config(conn, cfg)
    return True


# --- Source settings (enable/disable + API key overrides on top of YAML) -------


def set_source_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO source_settings (name, enabled) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
        (name, int(enabled)),
    )
    conn.commit()


def set_ebay_credentials(conn: sqlite3.Connection, app_id: str, cert_id: str, env: str) -> None:
    conn.execute(
        "INSERT INTO source_settings (name, ebay_app_id, ebay_cert_id, ebay_env) "
        "VALUES ('ebay', ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET ebay_app_id = excluded.ebay_app_id, "
        "ebay_cert_id = excluded.ebay_cert_id, ebay_env = excluded.ebay_env",
        (app_id, cert_id, env),
    )
    conn.commit()


def _source_overrides(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute("SELECT * FROM source_settings")}


def effective_sources_config(conn: sqlite3.Connection, cfg: AppConfig):
    """YAML-defined sources with DB-stored enable/API-key overrides applied.

    Source *definitions* (url, type, label) always come from YAML — only
    enabled-state and eBay credentials can be overridden here, so editing
    config.yaml always takes effect without needing an import step.
    """
    overrides = _source_overrides(conn)

    def _enabled(name: str, default: bool) -> bool:
        row = overrides.get(name)
        return bool(row["enabled"]) if row is not None and row["enabled"] is not None else default

    sc = cfg.sources
    ebay = sc.ebay
    row = overrides.get("ebay")
    if row is not None:
        ebay = replace(
            ebay,
            enabled=_enabled("ebay", ebay.enabled),
            app_id=row["ebay_app_id"] or ebay.app_id,
            cert_id=row["ebay_cert_id"] or ebay.cert_id,
            env=row["ebay_env"] or ebay.env,
        )
    extra = [replace(e, enabled=_enabled(e.name, e.enabled)) for e in sc.extra]
    return replace(
        sc,
        ebay=ebay,
        gumtree_enabled=_enabled("gumtree", sc.gumtree_enabled),
        facebook_enabled=_enabled("facebook", sc.facebook_enabled),
        extra=extra,
    )


def effective_config(conn: sqlite3.Connection, cfg: AppConfig) -> AppConfig:
    """cfg with source overrides from the DB applied — the entry point every
    caller that builds a source registry or checks enabled sources should use."""
    return replace(cfg, sources=effective_sources_config(conn, cfg))


def _item_from_row(row: sqlite3.Row) -> ItemConfig:
    sources = row["sources"]
    return ItemConfig(
        name=row["name"],
        terms=json.loads(row["terms"] or "[]"),
        max_price=row["max_price"],
        normal_price=row["normal_price"],
        target_deal_price=row["target_deal_price"],
        priority=row["priority"] or "normal",
        notes=row["notes"] or "",
        exclude_terms=json.loads(row["exclude_terms"] or "[]"),
        sources=json.loads(sources) if sources else None,
        id=row["id"],
    )


def load_project_configs(conn: sqlite3.Connection) -> list[ProjectConfig]:
    """Active (non-archived) projects and items, as config dataclasses."""
    projects = []
    for prow in conn.execute(
        "SELECT * FROM projects WHERE archived = 0 ORDER BY name"
    ):
        items = [
            _item_from_row(irow)
            for irow in conn.execute(
                "SELECT * FROM items WHERE project_id = ? AND archived = 0 ORDER BY name",
                (prow["id"],),
            )
        ]
        projects.append(
            ProjectConfig(
                name=prow["name"],
                slug=prow["slug"],
                items=items,
                sources=json.loads(prow["sources"]) if prow["sources"] else None,
                id=prow["id"],
            )
        )
    return projects


# --- Project CRUD ------------------------------------------------------------


def list_projects(conn: sqlite3.Connection, include_archived: bool = True) -> list[sqlite3.Row]:
    where = "" if include_archived else "WHERE p.archived = 0"
    return conn.execute(
        f"""
        SELECT p.*, COUNT(i.id) AS item_count
        FROM projects p LEFT JOIN items i ON i.project_id = p.id AND i.archived = 0
        {where}
        GROUP BY p.id ORDER BY p.archived, p.name
        """
    ).fetchall()


def get_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def create_project(conn: sqlite3.Connection, name: str, sources: list[str] | None = None) -> int:
    base = slugify(name)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    cur = conn.execute(
        "INSERT INTO projects (slug, name, sources) VALUES (?, ?, ?)",
        (slug, name, json.dumps(sources) if sources is not None else None),
    )
    conn.commit()
    return cur.lastrowid


def update_project(
    conn: sqlite3.Connection, project_id: int, name: str, sources: list[str] | None = None
) -> None:
    conn.execute(
        "UPDATE projects SET name = ?, sources = ? WHERE id = ?",
        (name, json.dumps(sources) if sources is not None else None, project_id),
    )
    conn.commit()


def set_project_archived(conn: sqlite3.Connection, project_id: int, archived: bool) -> None:
    conn.execute(
        "UPDATE projects SET archived = ? WHERE id = ?", (int(archived), project_id)
    )
    conn.commit()


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    """Hard delete a project, its items, and their matches/alerts."""
    item_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE project_id = ?", (project_id,)
    )]
    for item_id in item_ids:
        delete_item(conn, item_id, _commit=False)
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()


# --- Item CRUD ----------------------------------------------------------------


def list_items(
    conn: sqlite3.Connection, project_id: int | None = None, include_archived: bool = True
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if project_id is not None:
        clauses.append("i.project_id = ?")
        params.append(project_id)
    if not include_archived:
        clauses.append("i.archived = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT i.*, p.name AS project_name, p.slug AS project_slug
        FROM items i JOIN projects p ON p.id = i.project_id
        {where}
        ORDER BY p.name, i.archived, i.name
        """,
        params,
    ).fetchall()


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT i.*, p.name AS project_name FROM items i "
        "JOIN projects p ON p.id = i.project_id WHERE i.id = ?",
        (item_id,),
    ).fetchone()


def _item_params(item: ItemConfig) -> tuple:
    return (
        item.name,
        item.priority,
        item.max_price,
        item.normal_price,
        item.target_deal_price,
        item.notes,
        json.dumps(item.terms),
        json.dumps(item.exclude_terms),
        json.dumps(item.sources) if item.sources is not None else None,
    )


def create_item(conn: sqlite3.Connection, project_id: int, item: ItemConfig) -> int:
    cur = conn.execute(
        "INSERT INTO items (project_id, name, priority, max_price, normal_price, "
        "target_deal_price, notes, terms, exclude_terms, sources) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, *_item_params(item)),
    )
    conn.commit()
    return cur.lastrowid


def update_item(conn: sqlite3.Connection, item_id: int, item: ItemConfig) -> None:
    conn.execute(
        "UPDATE items SET name = ?, priority = ?, max_price = ?, normal_price = ?, "
        "target_deal_price = ?, notes = ?, terms = ?, exclude_terms = ?, sources = ? "
        "WHERE id = ?",
        (*_item_params(item), item_id),
    )
    conn.commit()


def set_item_archived(conn: sqlite3.Connection, item_id: int, archived: bool) -> None:
    conn.execute("UPDATE items SET archived = ? WHERE id = ?", (int(archived), item_id))
    conn.commit()


def delete_item(conn: sqlite3.Connection, item_id: int, _commit: bool = True) -> None:
    """Hard delete an item and its matches/alerts/catalogue tracking
    (listings, and the global products/price history they resolve to, are
    kept — see docs/adr/0007-catalogue-globalization.md: a product may be
    tracked by other items, and even when it isn't any more, its
    accumulated price history stays as platform evidence rather than being
    destroyed by one item's deletion)."""
    conn.execute(
        "DELETE FROM alerts_sent WHERE match_id IN "
        "(SELECT id FROM listing_matches WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute("DELETE FROM listing_matches WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM item_products WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM product_suggestions WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    if _commit:
        conn.commit()


# --- Product catalogue CRUD ---------------------------------------------------
#
# products is the global/platform-owned catalogue (see
# docs/adr/0007-catalogue-globalization.md): manufacturer/model identity and
# market prices (msrp, typical new/used price, retailer URL, price trend),
# shared across every item/project that tracks it. item_products is the
# join/context table holding one item's own tracking of a product: its
# match terms, its target-deal-price override, and whether it's still
# archived/wanted *for that item* — never global state.

_ITEM_PRODUCT_SELECT = """
SELECT ip.id AS id, ip.item_id AS item_id, p.id AS product_id,
       p.manufacturer AS manufacturer, p.model AS model,
       ip.match_terms AS match_terms,
       p.msrp AS msrp, p.typical_new_price AS typical_new_price,
       p.typical_used_price AS typical_used_price,
       ip.target_deal_price AS target_deal_price,
       ip.archived AS archived, ip.wanted AS wanted,
       p.price_trend_pct AS price_trend_pct,
       p.price_trend_confidence AS price_trend_confidence,
       p.canonical_price_url AS canonical_price_url,
       p.price_search_checked AS price_search_checked,
       p.last_price_check_at AS last_price_check_at,
       p.last_price_check_ok AS last_price_check_ok
FROM item_products ip
JOIN products p ON p.id = ip.product_id
"""


def _product_from_row(row: sqlite3.Row) -> catalogue.Product:
    """Build a Product from a *bare* global `products` row (see get_product)
    — no item context, so the item-scoped fields (match_terms,
    target_deal_price, archived, wanted) take their neutral defaults. Used
    where only global market data is needed (price history, price trend)."""
    return catalogue.Product(
        id=row["id"],
        global_product_id=row["id"],
        item_product_id=None,
        item_id=None,
        manufacturer=row["manufacturer"],
        model=row["model"] or "",
        match_terms=[],
        msrp=row["msrp"],
        typical_new_price=row["typical_new_price"],
        typical_used_price=row["typical_used_price"],
        target_deal_price=None,
        archived=False,
        price_trend_pct=row["price_trend_pct"],
        price_trend_confidence=row["price_trend_confidence"],
        wanted=True,
    )


def _item_product_from_row(row: sqlite3.Row) -> catalogue.Product:
    """Build a Product from a joined item_products+products row (see
    _ITEM_PRODUCT_SELECT) — the effective, item-contextualised view:
    global identity/market fields from products, tracking fields
    (match_terms/target_deal_price/archived/wanted) from item_products.
    `.id` is the *global* products.id (what listing_matches/price
    observations must reference); `.item_product_id` is this item's own
    tracking row, needed by anything that mutates match_terms/target_deal_price/
    archived/wanted for this item specifically."""
    return catalogue.Product(
        id=row["product_id"],
        global_product_id=row["product_id"],
        item_product_id=row["id"],
        item_id=row["item_id"],
        manufacturer=row["manufacturer"],
        model=row["model"] or "",
        match_terms=json.loads(row["match_terms"] or "[]"),
        msrp=row["msrp"],
        typical_new_price=row["typical_new_price"],
        typical_used_price=row["typical_used_price"],
        target_deal_price=row["target_deal_price"],
        archived=bool(row["archived"]),
        price_trend_pct=row["price_trend_pct"],
        price_trend_confidence=row["price_trend_confidence"],
        wanted=bool(row["wanted"]),
    )


def list_products(
    conn: sqlite3.Connection, item_id: int, include_archived: bool = True
) -> list[sqlite3.Row]:
    """This item's tracked catalogue products (joined view — see
    _ITEM_PRODUCT_SELECT). `row['id']` is this item's item_products row id
    (what the web UI's edit/archive/delete/toggle-wanted routes act on);
    `row['product_id']` is the shared global product id."""
    where = "ip.item_id = ?" if include_archived else "ip.item_id = ? AND ip.archived = 0"
    return conn.execute(
        f"{_ITEM_PRODUCT_SELECT} WHERE {where} ORDER BY ip.archived, p.manufacturer, p.model",
        (item_id,),
    ).fetchall()


def list_products_for_matching(conn: sqlite3.Connection, item_id: int) -> list[catalogue.Product]:
    """Active catalogue products for an item, ready for catalogue.match()."""
    return [_item_product_from_row(r) for r in list_products(conn, item_id, include_archived=False)]


def get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    """Bare global product row (identity + market data only — no
    match_terms/target_deal_price/archived/wanted, which are item-scoped;
    see get_item_product for the combined view)."""
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def get_item_product(conn: sqlite3.Connection, item_id: int, product_id: int) -> sqlite3.Row | None:
    """One item's tracking of one global product, joined (see
    _ITEM_PRODUCT_SELECT) — the combined view most callers actually want."""
    return conn.execute(
        f"{_ITEM_PRODUCT_SELECT} WHERE ip.item_id = ? AND ip.product_id = ?",
        (item_id, product_id),
    ).fetchone()


def get_item_product_by_id(conn: sqlite3.Connection, item_product_id: int) -> sqlite3.Row | None:
    """Same joined view as get_item_product, looked up by the item_products
    row's own id — what the web UI's per-item product routes receive."""
    return conn.execute(
        f"{_ITEM_PRODUCT_SELECT} WHERE ip.id = ?", (item_product_id,)
    ).fetchone()


def _find_global_product(conn: sqlite3.Connection, manufacturer: str, model: str) -> int | None:
    """Identity-key lookup (casing/spacing/punctuation insensitive — see
    catalogue.model_key) across the *global* catalogue, not scoped to any
    one item — the heart of catalogue globalization: two items naming "the
    same" product converge on one products row instead of each minting
    their own."""
    mkey, kkey = catalogue.model_key(manufacturer), catalogue.model_key(model)
    for row in conn.execute("SELECT id, manufacturer, model FROM products"):
        if (catalogue.model_key(row["manufacturer"]) == mkey
                and catalogue.model_key(row["model"]) == kkey):
            return row["id"]
    return None


def create_product(
    conn: sqlite3.Connection,
    item_id: int,
    manufacturer: str,
    model: str,
    match_terms: list[str],
    msrp: float | None,
    typical_new_price: float | None,
    target_deal_price: float | None,
) -> int:
    """Attach an item to a catalogue product, returning the *global*
    product id — creating the global product first if this manufacturer/
    model doesn't already exist anywhere on the platform (see
    _find_global_product), and creating (or updating, if the item already
    tracks it) this item's own item_products row for match_terms/
    target_deal_price. msrp/typical_new_price are only ever set at genuine
    global creation — a second item attaching to an already-known product
    never overwrites its established market data."""
    product_id = _find_global_product(conn, manufacturer, model)
    if product_id is None:
        cur = conn.execute(
            "INSERT INTO products (manufacturer, model, msrp, typical_new_price) "
            "VALUES (?, ?, ?, ?)",
            (manufacturer, model, msrp, typical_new_price),
        )
        product_id = cur.lastrowid

    existing = conn.execute(
        "SELECT id FROM item_products WHERE item_id = ? AND product_id = ?",
        (item_id, product_id),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO item_products (item_id, product_id, match_terms, target_deal_price) "
            "VALUES (?, ?, ?, ?)",
            (item_id, product_id, json.dumps(match_terms), target_deal_price),
        )
    else:
        conn.execute(
            "UPDATE item_products SET match_terms = ?, target_deal_price = ? WHERE id = ?",
            (json.dumps(match_terms), target_deal_price, existing["id"]),
        )
    conn.commit()
    return product_id


def update_product(
    conn: sqlite3.Connection,
    item_product_id: int,
    manufacturer: str,
    model: str,
    match_terms: list[str],
    msrp: float | None,
    typical_new_price: float | None,
    target_deal_price: float | None,
) -> None:
    """Edit an item's tracked product: manufacturer/model/msrp/
    typical_new_price are global market fields (affect every item tracking
    this product — see docs/adr/0007-catalogue-globalization.md, "who can
    edit shared global fields" is an explicitly open question, not solved
    here); match_terms/target_deal_price are this item's own override."""
    row = get_item_product_by_id(conn, item_product_id)
    if row is None:
        return
    conn.execute(
        "UPDATE products SET manufacturer = ?, model = ?, msrp = ?, typical_new_price = ? "
        "WHERE id = ?",
        (manufacturer, model, msrp, typical_new_price, row["product_id"]),
    )
    conn.execute(
        "UPDATE item_products SET match_terms = ?, target_deal_price = ? WHERE id = ?",
        (json.dumps(match_terms), target_deal_price, item_product_id),
    )
    conn.commit()


def set_product_archived(conn: sqlite3.Connection, item_product_id: int, archived: bool) -> None:
    """Archive/unarchive *this item's* tracking of a product — never
    affects other items tracking the same global product."""
    conn.execute("UPDATE item_products SET archived = ? WHERE id = ?", (int(archived), item_product_id))
    conn.commit()


def set_product_wanted(conn: sqlite3.Connection, item_product_id: int, wanted: bool) -> None:
    """Toggle deal surfacing for *this item's* tracking of a product.
    wanted=False = knowledge only: the catalogue keeps identifying its
    listings and collecting price history, but matches never alert or
    appear on deal surfaces for this item (read-time gating via _WANTED —
    effect is immediate, no rescan needed). For products that are real and
    worth knowing about, just not wanted by this item (old CPU generations
    under a current-gen item). Never affects any other item tracking the
    same global product."""
    conn.execute("UPDATE item_products SET wanted = ? WHERE id = ?", (int(wanted), item_product_id))
    conn.commit()


def _merge_products_impl(conn: sqlite3.Connection, keep_id: int, dup_id: int) -> None:
    """Fold a duplicate global product into the one being kept — no commit
    (see merge_products / _merge_products_locked, the two public/internal
    callers that control the transaction boundary). Everything the
    duplicate accumulated changes owner: listing matches, price
    observations, new-price history, price candidates, and every item's
    own item_products tracking row. Global fields (msrp/typical_new_price/
    canonical_price_url) are filled from the duplicate only where the
    keeper's are NULL. The used-price cache is recomputed over the
    combined observations, and the duplicate row is deleted. Nothing else
    is lost."""
    keep = get_product(conn, keep_id)
    dup = get_product(conn, dup_id)
    if keep is None or dup is None or keep_id == dup_id:
        raise ValueError("merge_products needs two distinct existing products")

    for table in ("listing_matches", "product_price_observations",
                  "product_new_price_history", "product_price_candidates"):
        conn.execute(
            f"UPDATE {table} SET product_id = ? WHERE product_id = ?",  # noqa: S608 — fixed table names
            (keep_id, dup_id),
        )

    # Reconcile item_products: an item that only tracked the duplicate is
    # simply repointed at the keeper. An item that (unusually) already
    # tracked *both* — the same-item double-tracking edge case dedupe must
    # also handle — has its two rows merged into one, unioning match_terms
    # and coalescing the override fields, mirroring the global-field merge
    # below, then the now-redundant row is dropped.
    for dup_ip in conn.execute(
        "SELECT * FROM item_products WHERE product_id = ?", (dup_id,)
    ).fetchall():
        keep_ip = conn.execute(
            "SELECT * FROM item_products WHERE item_id = ? AND product_id = ?",
            (dup_ip["item_id"], keep_id),
        ).fetchone()
        if keep_ip is None:
            conn.execute(
                "UPDATE item_products SET product_id = ? WHERE id = ?",
                (keep_id, dup_ip["id"]),
            )
            continue
        terms = json.loads(keep_ip["match_terms"])
        seen = {t.strip().lower() for t in terms}
        for term in json.loads(dup_ip["match_terms"]):
            if term.strip().lower() not in seen:
                terms.append(term)
                seen.add(term.strip().lower())
        conn.execute(
            "UPDATE item_products SET match_terms = ?, "
            "target_deal_price = COALESCE(target_deal_price, ?), "
            "archived = ?, wanted = ? WHERE id = ?",
            (json.dumps(terms), dup_ip["target_deal_price"],
             int(bool(keep_ip["archived"]) and bool(dup_ip["archived"])),
             int(bool(keep_ip["wanted"]) or bool(dup_ip["wanted"])),
             keep_ip["id"]),
        )
        conn.execute("DELETE FROM item_products WHERE id = ?", (dup_ip["id"],))

    conn.execute(
        "UPDATE products SET "
        "msrp = COALESCE(msrp, ?), "
        "typical_new_price = COALESCE(typical_new_price, ?), "
        "canonical_price_url = COALESCE(canonical_price_url, ?) "
        "WHERE id = ?",
        (dup["msrp"], dup["typical_new_price"], dup["canonical_price_url"], keep_id),
    )
    conn.execute("DELETE FROM products WHERE id = ?", (dup_id,))
    _recompute_used_price(conn, keep_id)


def _merge_products_locked(conn: sqlite3.Connection, keep_id: int, dup_id: int) -> None:
    """merge_products without its own commit — for callers (the
    globalization migration) already managing their own transaction."""
    _merge_products_impl(conn, keep_id, dup_id)


def merge_products(conn: sqlite3.Connection, keep_id: int, dup_id: int) -> None:
    _merge_products_impl(conn, keep_id, dup_id)
    conn.commit()


def find_duplicate_products(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """Groups of *global* products that are the same manufacturer/model by
    identity key (casing/spacing/punctuation insensitive, see
    catalogue.model_key) — the duplicates create_product now prevents,
    found so pre-guard databases can be swept (see cli catalogue-tidy).
    Each group is ordered oldest first (the natural keeper). Global since
    catalogue globalization (see docs/adr/0007-catalogue-globalization.md)
    — two items' products with the same identity are duplicates regardless
    of which items track them."""
    rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for row in rows:
        key = (catalogue.model_key(row["manufacturer"]), catalogue.model_key(row["model"]))
        groups.setdefault(key, []).append(row)
    return [group for group in groups.values() if len(group) > 1]


# A product whose matched listings average under this fraction of the item's
# normal price is priced like an accessory, not the wanted product.
_SUSPECT_PRICE_RATIO = 0.25
# ...and one where most matched titles name an accessory probably is one.
_SUSPECT_TITLE_SHARE = 0.5
# Both signals need at least this many matches before accusing anything.
_SUSPECT_MIN_MATCHES = 2


def find_suspect_products(conn: sqlite3.Connection) -> list[dict]:
    """Active item_products entries whose own matched listings suggest
    they're an accessory, consumable or spare part rather than the wanted
    product — approved from seller "model" fields that were really part
    numbers. Evaluated per (item, product) tracking, not per global
    product: the evidence (this item's own matched listings, this item's
    own normal price) is inherently item-scoped, even though the product
    identity itself is shared.

    Evidence-based and read-only: a product is only accused on what its
    matches actually show (average price far below the item's normal
    price, or most matched titles naming an accessory), never on model
    shape alone — some brands use bare article numbers for real products.
    Entries with fewer than _SUSPECT_MIN_MATCHES matches are never listed:
    no evidence, no accusation. Archiving is the human's call (see
    /catalogue); an archived entry stops matching and its old matches lose
    their product_id on the next rescan of each listing.

    `id` in each result is the item_products row id (what the web UI's
    archive/knowledge-only bulk actions act on); `product_id` is the
    shared global product id."""
    rows = conn.execute(
        """
        SELECT ip.id, ip.item_id, ip.product_id, p.manufacturer, p.model,
               i.name AS item_name, i.normal_price AS item_normal,
               COUNT(m.id) AS match_count, AVG(l.price) AS avg_price
        FROM item_products ip
        JOIN products p ON p.id = ip.product_id
        JOIN items i ON i.id = ip.item_id
        JOIN listing_matches m ON m.product_id = ip.product_id AND m.item_id = ip.item_id
        JOIN listings l ON l.id = m.listing_id
        WHERE ip.archived = 0 AND ip.wanted = 1
        GROUP BY ip.id
        HAVING match_count >= ?
        ORDER BY avg_price
        """,
        (_SUSPECT_MIN_MATCHES,),
    ).fetchall()
    suspects = []
    for row in rows:
        reasons = []
        if row["item_normal"] and row["avg_price"] < row["item_normal"] * _SUSPECT_PRICE_RATIO:
            reasons.append(
                f"matches average £{row['avg_price']:.0f} against a "
                f"£{row['item_normal']:.0f} item"
            )
        titles = [
            r["title"] for r in conn.execute(
                "SELECT l.title FROM listing_matches m JOIN listings l ON l.id = m.listing_id "
                "WHERE m.product_id = ? AND m.item_id = ?", (row["product_id"], row["item_id"]),
            )
        ]
        share = catalogue.accessory_title_share(titles)
        if share >= _SUSPECT_TITLE_SHARE:
            reasons.append(
                f"{share:.0%} of matched titles name an accessory"
            )
        if not reasons:
            continue
        if catalogue.looks_like_part_number(row["model"]):
            reasons.append("model is shaped like a part number")
        suspects.append({
            "id": row["id"],
            "product_id": row["product_id"],
            "item_id": row["item_id"],
            "item_name": row["item_name"],
            "manufacturer": row["manufacturer"],
            "model": row["model"],
            "match_count": row["match_count"],
            "avg_price": row["avg_price"],
            "item_normal": row["item_normal"],
            "reasons": reasons,
            "sample_title": titles[0] if titles else "",
        })
    return suspects


def dedupe_products(conn: sqlite3.Connection) -> int:
    """Merge every exact-duplicate global product group into its oldest
    member. Returns how many duplicate rows were folded away. Idempotent."""
    merged = 0
    for group in find_duplicate_products(conn):
        keep, *dups = group
        for dup in dups:
            merge_products(conn, keep["id"], dup["id"])
            merged += 1
    return merged


def delete_item_product(conn: sqlite3.Connection, item_product_id: int) -> None:
    """Stop this item tracking a product — removes only this item's
    item_products row and nulls this item's own listing_matches rows for
    it. The shared global product, its price history, and any other
    item's tracking of it are deliberately left untouched (see
    docs/adr/0007-catalogue-globalization.md: an item deleting its own
    tracking must never destroy platform-wide evidence). This is the
    action the web UI's per-item "Delete" button performs."""
    row = get_item_product_by_id(conn, item_product_id)
    if row is None:
        return
    conn.execute(
        "UPDATE listing_matches SET product_id = NULL WHERE product_id = ? AND item_id = ?",
        (row["product_id"], row["item_id"]),
    )
    conn.execute("DELETE FROM item_products WHERE id = ?", (item_product_id,))
    conn.commit()


def delete_product(conn: sqlite3.Connection, product_id: int) -> None:
    """Purge a global catalogue product entirely: every item's tracking of
    it, its price observations/history/candidates, and its listing_matches
    everywhere (across every item, not just one). A genuinely destructive,
    platform-wide action — use delete_item_product for the everyday "this
    item doesn't want this entry any more" case, which preserves shared
    evidence. Intended for real garbage entries (e.g. a merge mistake),
    not routine per-item cleanup."""
    conn.execute(
        "UPDATE listing_matches SET product_id = NULL WHERE product_id = ?", (product_id,)
    )
    conn.execute("DELETE FROM item_products WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM product_price_observations WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM product_new_price_history WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM product_price_candidates WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()


# Only observations from the last N days feed the rolling "typical used
# price" — old asking prices shouldn't anchor today's market.
_PRICE_HISTORY_WINDOW_DAYS = 90


def record_price_observation(conn: sqlite3.Connection, product_id: int, price: float, source: str) -> None:
    """Log one used-market price sighting for a product and recompute its
    rolling `typical_used_price` (median of observations from the last
    `_PRICE_HISTORY_WINDOW_DAYS` days) plus its used-price trend (see
    price_trend.py) — both cached on `products`, never recomputed at
    scoring time. Call once per distinct listing a product resolves
    against — not on every rescan of an already-seen listing, or a single
    stale unsold listing would dominate the average."""
    conn.execute(
        "INSERT INTO product_price_observations (product_id, price, source, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (product_id, price, source, _now()),
    )
    _recompute_used_price(conn, product_id)
    conn.commit()


def _recompute_used_price(conn: sqlite3.Connection, product_id: int) -> None:
    """Recompute the cached typical_used_price + trend from the observation
    window. Shared by record_price_observation (every new sighting) and
    merge_products (observations just changed owner). Caller commits."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_PRICE_HISTORY_WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        "SELECT price, source, observed_at FROM product_price_observations "
        "WHERE product_id = ? AND observed_at >= ?",
        (product_id, cutoff),
    ).fetchall()
    prices = sorted(r["price"] for r in rows)
    typical = median(prices) if prices else None
    # _PRICE_HISTORY_WINDOW_DAYS (90) comfortably covers the trend module's
    # own two-window lookback (2 * price_trend.WINDOW_DAYS = 60), so this
    # reuses the same rows already fetched above rather than a second query.
    trend = price_trend.compute_trend(
        [(r["observed_at"], r["price"], r["source"]) for r in rows]
    )
    conn.execute(
        "UPDATE products SET typical_used_price = ?, price_trend_pct = ?, "
        "price_trend_confidence = ? WHERE id = ?",
        (typical, trend.pct, trend.confidence, product_id),
    )


def list_price_observations(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM product_price_observations WHERE product_id = ? ORDER BY observed_at",
        (product_id,),
    ).fetchall()


# --- New-price history (collection only — not yet read by scoring) -----------
#
# Mirrors product_price_observations for the new-price side: every canonical
# retailer price (initial approval, and every Stage 2 refresh) is logged here
# so there's real history to validate a new-price trend against later (see
# docs/strategy/roadmap.md, "Deal accuracy"). Deliberately not consumed by
# price_trend.py or scoring.py yet — collected from day one precisely so it
# isn't empty by the time that work starts.


def record_new_price_history(conn: sqlite3.Connection, product_id: int, price: float, domain: str) -> None:
    conn.execute(
        "INSERT INTO product_new_price_history (product_id, price, domain, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (product_id, price, domain, _now()),
    )
    conn.commit()


def list_new_price_history(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM product_new_price_history WHERE product_id = ? ORDER BY observed_at",
        (product_id,),
    ).fetchall()


# --- Retailer price discovery (see retailer_price.py) ------------------------
#
# Stage 1 produces candidates a human must approve before any canonical URL
# exists; Stage 2 only ever refreshes a URL that's already been approved.
# `price_search_checked` is a one-shot flag, same pattern as
# `listings.brand_checked` — a search that finds nothing still counts as
# "tried"; the product edit page's manual "Search again" action is the only
# way back in, not automatic retries.


def record_price_candidates(conn: sqlite3.Connection, product_id: int, candidates: list[dict]) -> None:
    """Replace a product's pending price candidates with a fresh search
    batch (never accumulated across searches) and mark it as searched."""
    conn.execute("DELETE FROM product_price_candidates WHERE product_id = ?", (product_id,))
    now = _now()
    for c in candidates:
        conn.execute(
            "INSERT INTO product_price_candidates "
            "(product_id, url, domain, price, currency, confidence, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_id, c["url"], c["domain"], c["price"], c["currency"], c["confidence"], now),
        )
    conn.execute("UPDATE products SET price_search_checked = 1 WHERE id = ?", (product_id,))
    conn.commit()


def list_price_candidates(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM product_price_candidates WHERE product_id = ? ORDER BY confidence DESC",
        (product_id,),
    ).fetchall()


def get_price_candidate(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM product_price_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()


def clear_price_candidates(conn: sqlite3.Connection, product_id: int) -> None:
    """Discard every candidate for a product without approving any —
    'none of these are right', not a decision that should block a future
    manual re-search."""
    conn.execute("DELETE FROM product_price_candidates WHERE product_id = ?", (product_id,))
    conn.commit()


def list_products_needing_price_search(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Global products still actively tracked by at least one item
    (archived is now per-item — see item_products), with no canonical
    retailer URL, that haven't had a Stage-1 search attempt yet."""
    return conn.execute(
        "SELECT * FROM products WHERE canonical_price_url IS NULL "
        "AND price_search_checked = 0 "
        "AND EXISTS (SELECT 1 FROM item_products ip WHERE ip.product_id = products.id AND ip.archived = 0)"
    ).fetchall()


def approve_price_candidate(
    conn: sqlite3.Connection, candidate_id: int, refreshed: dict | None
) -> None:
    """Adopt a candidate as the product's canonical retailer URL. `refreshed`
    is a freshly re-fetched {"price", ...} at approval time (see
    retailer_price.fetch_price) if that succeeded — falling back to the
    candidate's own already-extracted price if a single refetch has a
    transient hiccup, rather than leaving the product with nothing."""
    candidate = get_price_candidate(conn, candidate_id)
    if candidate is None:
        return
    price = refreshed["price"] if refreshed else candidate["price"]
    conn.execute(
        "UPDATE products SET canonical_price_url = ?, typical_new_price = ?, "
        "last_price_check_at = ?, last_price_check_ok = ? WHERE id = ?",
        (candidate["url"], price, _now(), int(refreshed is not None), candidate["product_id"]),
    )
    conn.execute(
        "DELETE FROM product_price_candidates WHERE product_id = ?", (candidate["product_id"],)
    )
    conn.commit()
    record_new_price_history(conn, candidate["product_id"], price, candidate["domain"])


def list_products_due_for_price_refresh(
    conn: sqlite3.Connection, max_staleness_hours: int
) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_staleness_hours)).isoformat(
        timespec="seconds"
    )
    return conn.execute(
        "SELECT * FROM products WHERE canonical_price_url IS NOT NULL "
        "AND (last_price_check_at IS NULL OR last_price_check_at < ?) "
        "AND EXISTS (SELECT 1 FROM item_products ip WHERE ip.product_id = products.id AND ip.archived = 0)",
        (cutoff,),
    ).fetchall()


def record_price_refresh(
    conn: sqlite3.Connection, product_id: int, result: dict | None, domain: str = ""
) -> None:
    """Stage 2: apply a refetch of a product's already-approved canonical
    URL. On failure, keep the last known typical_new_price — a dead or
    unparseable page is a reason to stop trusting *future* updates, not to
    discard the last real number observed. `domain` is the canonical URL's
    domain (see retailer_price._domain) — only needed on success, to log
    the new-price history row."""
    if result is not None:
        conn.execute(
            "UPDATE products SET typical_new_price = ?, last_price_check_at = ?, "
            "last_price_check_ok = 1 WHERE id = ?",
            (result["price"], _now(), product_id),
        )
        conn.commit()
        record_new_price_history(conn, product_id, result["price"], domain)
    else:
        conn.execute(
            "UPDATE products SET last_price_check_at = ?, last_price_check_ok = 0 WHERE id = ?",
            (_now(), product_id),
        )
        conn.commit()


# --- App-wide settings (key/value; small enough not to need dedicated columns) -


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


_AUTO_APPROVE_THRESHOLD_KEY = "catalogue_auto_approve_threshold"


def get_auto_approve_threshold(conn: sqlite3.Connection) -> float | None:
    """Confidence (0-100) at or above which a product suggestion is
    auto-approved instead of waiting for review. None (the default) means
    everything requires manual approval."""
    raw = get_setting(conn, _AUTO_APPROVE_THRESHOLD_KEY)
    return float(raw) if raw else None


def set_auto_approve_threshold(conn: sqlite3.Connection, value: float | None) -> None:
    set_setting(conn, _AUTO_APPROVE_THRESHOLD_KEY, str(value) if value is not None else "")


# --- Product suggestions (candidates awaiting review — see catalogue.py) ------


def get_product_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM product_suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()


def list_product_suggestions(
    conn: sqlite3.Connection, item_id: int, status: str = "pending"
) -> list[sqlite3.Row]:
    # Ordered by manufacturer first so the web UI can group suggestions —
    # confidence DESC within each manufacturer keeps the most-corroborated
    # model at the top of its group.
    return conn.execute(
        "SELECT * FROM product_suggestions WHERE item_id = ? AND status = ? "
        "ORDER BY manufacturer, confidence DESC",
        (item_id, status),
    ).fetchall()


def list_all_pending_suggestions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every pending suggestion across all active items, with item/project
    names for grouping — the global catalogue review queue. Ordered so the
    template can groupby item: busiest items first (most pending), then
    strongest suggestions first within an item."""
    return conn.execute(
        """
        SELECT s.*, i.name AS item_name, i.normal_price AS item_normal,
               p.name AS project_name,
               COUNT(*) OVER (PARTITION BY s.item_id) AS item_pending
        FROM product_suggestions s
        JOIN items i ON i.id = s.item_id AND i.archived = 0
        JOIN projects p ON p.id = i.project_id AND p.archived = 0
        WHERE s.status = 'pending'
        ORDER BY item_pending DESC, s.item_id,
                 (s.model != '') DESC, s.confidence DESC, s.manufacturer
        """
    ).fetchall()


# Triage verdict thresholds. Evidence = the item's own listings whose title
# mentions the suggested model (word-boundary). Same discipline as
# find_suspect_products: below the minimum, no verdict is offered at all.
_TRIAGE_MIN_EVIDENCE = 2
_TRIAGE_ACCESSORY_TITLE_SHARE = 0.5   # most evidence titles name an accessory
_TRIAGE_ACCESSORY_PRICE_RATIO = 0.25  # evidence priced like a part, not the item
_TRIAGE_STRONG_PRICE_RATIO = 0.4      # evidence priced like the actual item
_TRIAGE_STRONG_TITLE_SHARE = 0.25     # ...and mostly not accessory-worded

# Verdicts, in review-priority order (the UI groups by these).
TRIAGE_STRONG = "strong"          # approve with confidence
TRIAGE_ACCESSORY = "accessory"    # dismiss with confidence
TRIAGE_UNCLEAR = "unclear"        # genuinely needs a human
TRIAGE_BRAND_ONLY = "brand-only"  # no model — can't be a product yet


def triage_pending_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """The pending queue with an evidence-based verdict per suggestion, so
    a human arbitrates buckets instead of individually judging every row
    (the operator's standing direction: evidence gates, humans arbitrate
    small evidence-rich lists — a 1,600-row blind queue produced a
    polluted catalogue).

    Evidence for a suggestion is the item's own listings whose titles
    mention the suggested model: their price level against the item's
    normal price, and how often they're worded as accessories
    ("bags for <model>"). Verdicts are proposals only — nothing here
    approves, dismisses, or writes anything."""
    rows = list_all_pending_suggestions(conn)

    # One title/price sweep per item, not per suggestion.
    item_listings: dict[int, list[sqlite3.Row]] = {}
    for item_id in {r["item_id"] for r in rows}:
        item_listings[item_id] = conn.execute(
            "SELECT DISTINCT l.title, l.price FROM listing_matches m "
            "JOIN listings l ON l.id = m.listing_id WHERE m.item_id = ?",
            (item_id,),
        ).fetchall()

    triaged = []
    for row in rows:
        suggestion = dict(row)
        model = row["model"]
        verdict, evidence_note = TRIAGE_UNCLEAR, "no listings mention this model yet"
        evidence_count, avg_price, accessory_share = 0, None, 0.0
        if not model:
            verdict, evidence_note = TRIAGE_BRAND_ONLY, "no model — not approvable in bulk"
        else:
            # Spacing-insensitive (catalogue.term_pattern): evidence for
            # "KGS 216 M" includes listings titled "KGS216M" and vice versa.
            pattern = catalogue.term_pattern(model.lower())
            evidence = [
                l for l in item_listings.get(row["item_id"], ())
                if pattern is not None and pattern.search(l["title"].lower())
            ] if pattern else []
            evidence_count = len(evidence)
            if evidence_count >= _TRIAGE_MIN_EVIDENCE:
                avg_price = sum(l["price"] for l in evidence) / evidence_count
                accessory_share = catalogue.accessory_title_share(
                    [l["title"] for l in evidence]
                )
                normal = row["item_normal"]
                ratio = (avg_price / normal) if normal else None
                if accessory_share >= _TRIAGE_ACCESSORY_TITLE_SHARE or (
                    ratio is not None and ratio < _TRIAGE_ACCESSORY_PRICE_RATIO
                ):
                    verdict = TRIAGE_ACCESSORY
                    evidence_note = (
                        f"{evidence_count} listings avg £{avg_price:.0f}"
                        + (f" vs £{normal:.0f} item" if normal else "")
                        + (f"; {accessory_share:.0%} accessory-worded"
                           if accessory_share else "")
                    )
                elif accessory_share <= _TRIAGE_STRONG_TITLE_SHARE and (
                    ratio is None or ratio >= _TRIAGE_STRONG_PRICE_RATIO
                ):
                    verdict = TRIAGE_STRONG
                    evidence_note = (
                        f"{evidence_count} listings avg £{avg_price:.0f}"
                        + (f" vs £{normal:.0f} item" if normal else "")
                    )
                else:
                    evidence_note = (
                        f"mixed evidence: {evidence_count} listings avg "
                        f"£{avg_price:.0f}, {accessory_share:.0%} accessory-worded"
                    )
        suggestion["verdict"] = verdict
        suggestion["evidence_note"] = evidence_note
        suggestion["evidence_count"] = evidence_count
        suggestion["part_number_shaped"] = bool(model) and catalogue.looks_like_part_number(model)
        triaged.append(suggestion)

    # Review-effort order: easiest/most-trustworthy decisions first, the
    # "needs more evidence" pile last (it resolves itself as listings
    # accumulate — it should never bury the actionable buckets).
    order = {TRIAGE_STRONG: 0, TRIAGE_ACCESSORY: 1, TRIAGE_BRAND_ONLY: 2, TRIAGE_UNCLEAR: 3}
    triaged.sort(key=lambda s: (order[s["verdict"]], s["item_name"], -s["confidence"]))
    return triaged


_MAX_RAW_SAMPLES = 10


def record_suggestion_sighting(
    conn: sqlite3.Connection,
    item_id: int,
    manufacturer: str,
    model: str,
    example_url: str,
    source: str = "ebay-structured",
) -> sqlite3.Row | None:
    """Create or corroborate a pending suggestion for (item, manufacturer,
    model), after deterministic normalisation (catalogue.normalize_suggestion)
    — casing variants like "WAGNER"/"Wagner"/"wagner" merge into one
    suggestion, and junk/placeholder/seller-name-like manufacturers are
    rejected outright and never become a suggestion at all (returns None).

    If it clears the auto-approve threshold, it's promoted straight to a
    real catalogue product. Once a suggestion has been approved or
    dismissed, further sightings are ignored — dismissal is a deliberate
    "no", not something a few more listings should silently override."""
    raw_sample = {"manufacturer": (manufacturer or "").strip(), "model": (model or "").strip()}
    normalized = catalogue.normalize_suggestion(manufacturer, model)
    if normalized is None:
        return None
    manufacturer, model = normalized
    now = _now()
    # Identity-key match: "DEWALT"/"DeWalt" and "KGS 216 M"/"KGS216M"
    # corroborate one suggestion instead of splitting, without
    # BRAND_ALIASES having to know every brand in advance (see
    # catalogue.model_key — casing, spacing and punctuation insensitive).
    # First-recorded form wins and is adopted here, so the
    # UNIQUE(item_id, manufacturer, model) row is reused.
    existing = None
    for row in conn.execute(
        "SELECT * FROM product_suggestions WHERE item_id = ?", (item_id,)
    ):
        if (catalogue.model_key(row["manufacturer"]) == catalogue.model_key(manufacturer)
                and catalogue.model_key(row["model"]) == catalogue.model_key(model)):
            existing = row
            break
    if existing:
        manufacturer, model = existing["manufacturer"], existing["model"]
    if existing and existing["status"] != "pending":
        return existing

    raw_samples = json.loads(existing["raw_samples"]) if existing else []
    if raw_sample not in raw_samples and len(raw_samples) < _MAX_RAW_SAMPLES:
        raw_samples.append(raw_sample)

    sighting_count = (existing["sighting_count"] + 1) if existing else 1
    confidence = catalogue.suggestion_confidence(sighting_count)
    if existing:
        conn.execute(
            "UPDATE product_suggestions SET sighting_count = ?, confidence = ?, "
            "last_seen = ?, example_url = ?, raw_samples = ? WHERE id = ?",
            (sighting_count, confidence, now, example_url, json.dumps(raw_samples), existing["id"]),
        )
        suggestion_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO product_suggestions (item_id, manufacturer, model, confidence, "
            "sighting_count, source, example_url, raw_samples, status, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (item_id, manufacturer, model, confidence, sighting_count, source, example_url,
             json.dumps(raw_samples), now, now),
        )
        suggestion_id = cur.lastrowid
    conn.commit()

    threshold = get_auto_approve_threshold(conn)
    if threshold is not None and confidence >= threshold:
        approve_suggestion(conn, suggestion_id)
    return get_product_suggestion(conn, suggestion_id)


def renormalize_pending_suggestions(conn: sqlite3.Connection) -> dict:
    """One-time cleanup for suggestions created before normalisation
    existed: rebuilds every *pending* suggestion by replaying its raw
    sightings through the current rules, so casing-duplicates merge and
    now-rejected junk disappears. Approved/dismissed suggestions (already
    decided) are left untouched. Call explicitly (e.g. a maintenance
    script) — this must never run automatically inside connect(), for the
    exact reason a past migration bug caused "database is locked": a write
    on every single connection is not something to run unconditionally."""
    pending = conn.execute("SELECT * FROM product_suggestions WHERE status = 'pending'").fetchall()
    conn.execute("DELETE FROM product_suggestions WHERE status = 'pending'")
    conn.commit()

    rejected = 0
    for row in pending:
        raw_samples = json.loads(row["raw_samples"] or "[]") or [
            {"manufacturer": row["manufacturer"], "model": row["model"]}
        ]
        # Replay whichever raw forms we captured, repeated to preserve the
        # original corroboration count (confidence depends on sighting
        # count, not just the number of distinct raw variants seen).
        replay = (raw_samples * row["sighting_count"])[: row["sighting_count"]]
        result = None
        for sample in replay:
            result = record_suggestion_sighting(
                conn, row["item_id"], sample["manufacturer"], sample["model"],
                row["example_url"], row["source"],
            )
        if result is None:
            rejected += 1
    after = conn.execute(
        "SELECT COUNT(*) c FROM product_suggestions WHERE status = 'pending'"
    ).fetchone()["c"]
    return {"before": len(pending), "after": after, "rejected_outright": rejected}


def approve_suggestion(
    conn: sqlite3.Connection, suggestion_id: int, model: str | None = None
) -> int:
    """Create the real catalogue product from a pending suggestion. Returns
    the new product's id.

    `model` corrects the recorded model at approval time — sellers'
    structured fields often carry an article/order number (Metabo
    "613216380") where the human model name is "KGS 216 M". The product
    gets the corrected model, and the originally-sighted string is kept as
    an extra match term (sellers quote article numbers too, so it still
    earns matches). The suggestion row keeps its *raw* model — that's the
    dedup key that stops the same raw sighting reopening as a new
    suggestion; a later suggestion of the corrected model converges onto
    this same product via create_product's case-insensitive guard."""
    suggestion = get_product_suggestion(conn, suggestion_id)
    raw_model = suggestion["model"]
    final_model = (model or "").strip() or raw_model
    combined = f"{suggestion['manufacturer']} {final_model}".strip()
    match_terms = [combined]
    if final_model and final_model != combined:
        match_terms.append(final_model)
    if raw_model and raw_model.lower() not in {t.lower() for t in match_terms}:
        match_terms.append(raw_model)
    product_id = create_product(
        conn, suggestion["item_id"], suggestion["manufacturer"], final_model,
        match_terms, None, None, None,
    )
    conn.execute("UPDATE product_suggestions SET status = 'approved' WHERE id = ?", (suggestion_id,))
    conn.commit()
    return product_id


def dismiss_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> None:
    conn.execute("UPDATE product_suggestions SET status = 'dismissed' WHERE id = ?", (suggestion_id,))
    conn.commit()


# --- Auction close tracking (see auction_watch.py) -----------------------------


def list_tracked_auctions(conn: sqlite3.Connection, max_staleness_days: int = 1) -> list[sqlite3.Row]:
    """Listings that are candidates for auction polling: not yet closed, with
    a known end time that hasn't gone stale (in case the app was offline past
    its close). A catalogue-product match is no longer required — every live
    auction now gets its snapshot history recorded (see
    record_auction_snapshot), not only ones resolved to a product; `m.product_id`
    is still exposed (NULL when unmatched) so callers can decide whether to
    also feed the product's used-price observations on close. Filtered further
    in Python (auction_watch.py) for "is this actually an auction" and "is it
    actually due for a poll right now" — both awkward to express over a JSON
    column and a variable cadence in SQL."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_staleness_days)).isoformat(
        timespec="seconds"
    )
    return conn.execute(
        """
        SELECT DISTINCT l.*, m.product_id
        FROM listings l
        LEFT JOIN listing_matches m ON m.listing_id = l.id
        WHERE l.sold_captured = 0
          AND l.end_time IS NOT NULL
          AND l.end_time >= ?
        """,
        (cutoff,),
    ).fetchall()


def mark_listing_polled(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET last_poll_at = ? WHERE id = ?", (_now(), listing_id))
    conn.commit()


def mark_sold_captured(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET sold_captured = 1 WHERE id = ?", (listing_id,))
    conn.commit()


def record_auction_snapshot(
    conn: sqlite3.Connection,
    listing_id: int,
    *,
    source: str,
    current_bid_price: float | None = None,
    currency: str = "GBP",
    bid_count: int | None = None,
    buy_it_now_price: float | None = None,
    shipping_price: float | None = None,
    end_time: str | None = None,
    watch_count: int | None = None,
    view_count: int | None = None,
    raw_payload: dict | None = None,
) -> int:
    """Append one point-in-time auction observation. Never overwrites a prior
    observation — this is a history, not a cache — so bid velocity and
    trajectory scoring (see auction_trajectory.py) have real data to work
    from. The `listings` row itself still tracks only the latest known state
    for simple display; this table is what remembers everything in between."""
    cur = conn.execute(
        "INSERT INTO auction_snapshots (listing_id, source, observed_at, "
        "current_bid_price, currency, bid_count, buy_it_now_price, "
        "shipping_price, end_time, watch_count, view_count, raw_payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            listing_id,
            source,
            _now(),
            current_bid_price,
            currency,
            bid_count,
            buy_it_now_price,
            shipping_price,
            end_time,
            watch_count,
            view_count,
            json.dumps(raw_payload) if raw_payload is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_auction_snapshots(conn: sqlite3.Connection, listing_id: int) -> list[sqlite3.Row]:
    """Full observation history for one listing, oldest first — the input to
    bid-velocity/trajectory scoring."""
    return conn.execute(
        "SELECT * FROM auction_snapshots WHERE listing_id = ? ORDER BY observed_at ASC",
        (listing_id,),
    ).fetchall()


# --- Listings, matches, alerts -------------------------------------------------


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> tuple[int, bool]:
    """Insert a listing or touch last_seen. Returns (listing_id, is_new).

    buying_options/bid_count/end_time/image_url/current_bid_price/
    buy_it_now_price are refreshed on every rescan too (a Buy It Now can
    disappear once bidding starts, bid count/price move, sellers swap
    photos) — this is what the auction-close poller (auction_watch.py)
    later reads to know which listings are auctions and when they end, and
    what the Active Auctions page reads for a correct current-bid display
    before any poll has happened yet (see the bug this fixed, 2026-07-08:
    current_bid_price/buy_it_now_price used to not exist at all, so a
    freshly-seen BIN+auction listing had nothing but the BIN-preferring
    `price` to show). image_url only ever overwrites with a real value, so a
    source that stops sending one doesn't blank an image we already have."""
    now = _now()
    buying_options = json.dumps(listing.buying_options)
    row = conn.execute(
        "SELECT id FROM listings WHERE source = ? AND external_id = ?",
        (listing.source, listing.external_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE listings SET last_seen = ?, price = ?, title = ?, "
            "buying_options = ?, bid_count = ?, end_time = ?, "
            "current_bid_price = ?, buy_it_now_price = ?, "
            "image_url = COALESCE(?, image_url) WHERE id = ?",
            (now, listing.price, listing.title, buying_options, listing.bid_count,
             listing.end_time, listing.current_bid_price, listing.buy_it_now_price,
             listing.image_url, row["id"]),
        )
        # Commit immediately (as every db.py write function must): the watch
        # loop calls this between network requests, and an uncommitted write
        # holds the WAL writer lock through every rate-limit sleep that
        # follows — which is exactly what made the web UI 500 with
        # "database is locked" while watch waited out a 429 backoff.
        conn.commit()
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO listings (source, external_id, title, price, currency, url, "
        "location, description, condition, first_seen, last_seen, "
        "buying_options, bid_count, end_time, image_url, "
        "current_bid_price, buy_it_now_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            listing.source,
            listing.external_id,
            listing.title,
            listing.price,
            listing.currency,
            listing.url,
            listing.location,
            listing.description,
            listing.condition,
            now,
            now,
            buying_options,
            listing.bid_count,
            listing.end_time,
            listing.image_url,
            listing.current_bid_price,
            listing.buy_it_now_price,
        ),
    )
    conn.commit()
    return cur.lastrowid, True


def get_listing(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def record_listing_click(
    conn: sqlite3.Connection,
    listing_id: int,
    source: str,
    context: str,
    outcome: str = "success",
    affiliate_applied: bool = False,
    project_id: int | None = None,
) -> None:
    """One row per GET /out/<listing_id> redirect attempt — the click
    audit/analytics trail for the Marketplace Outbound Gateway (see
    outbound.py). Raises on a genuine DB error like any other write here;
    the caller (web/app.py's listing_out route) is responsible for making
    sure a failed write never blocks the redirect itself — analytics must
    never hold up the user's navigation."""
    conn.execute(
        "INSERT INTO listing_clicks (listing_id, project_id, source, context, "
        "outcome, affiliate_applied, clicked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (listing_id, project_id, source, context, outcome, int(affiliate_applied), _now()),
    )
    conn.commit()


def mark_brand_checked(conn: sqlite3.Connection, listing_id: int) -> None:
    """Record that we've already looked this listing up for structured
    brand/model data (see suggestions below) — regardless of whether it had
    any, so we don't keep re-fetching a listing that simply has none."""
    conn.execute("UPDATE listings SET brand_checked = 1 WHERE id = ?", (listing_id,))
    conn.commit()


def _add_identity_member(conn: sqlite3.Connection, identity_id: int, listing_id: int, now: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO listing_identity_members "
        "(identity_id, listing_id, status, matched_by, created_at) "
        "VALUES (?, ?, 'confirmed', 'canonical_url', ?)",
        (identity_id, listing_id, now),
    )


def resolve_identity(conn: sqlite3.Connection, listing_id: int, listing: Listing) -> tuple[int | None, bool]:
    """Cross-source identity resolution — v1: canonical-URL matching only
    (see identity.py). Returns (identity_id, is_primary).

    Most listings have no recognisable canonical key at all (nothing but
    eBay is patterned yet), in which case this is a cheap no-op and the
    listing is trivially its own identity (is_primary=True, identity_id=None).

    When a canonical key *is* recoverable and this is the first sighting of
    it, this listing becomes the identity's primary. A later sighting (any
    source) sharing the same key is linked as a confirmed member; it becomes
    non-primary — `listings.is_primary_sighting` is set to 0, so alerting,
    price observations and match listings skip it — *unless* it's the
    canonical platform's own native listing arriving after an earlier proxy
    (e.g. an RSS entry that merely linked to the same eBay item, seen before
    eBay's own API surfaced it): the native row is promoted to primary
    instead, since it carries materially richer structured data (condition,
    buying_options, brand/mpn) that the proxy never has.

    Full provenance is preserved either way — every listing row and its own
    listing_matches entry stay untouched; only which one counts as the
    "current" sighting for scoring surfaces changes."""
    canonical_key = identity.derive_canonical_key(listing.url)
    if canonical_key is None:
        return None, True

    platform = canonical_key.split(":", 1)[0]
    now = _now()
    row = conn.execute(
        "SELECT li.id AS identity_id, li.primary_listing_id, l2.source AS primary_source "
        "FROM listing_identities li JOIN listings l2 ON l2.id = li.primary_listing_id "
        "WHERE li.canonical_key = ?",
        (canonical_key,),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            "INSERT INTO listing_identities (canonical_key, primary_listing_id, created_at) "
            "VALUES (?, ?, ?)",
            (canonical_key, listing_id, now),
        )
        identity_id = cur.lastrowid
        _add_identity_member(conn, identity_id, listing_id, now)
        conn.commit()
        return identity_id, True

    identity_id = row["identity_id"]
    _add_identity_member(conn, identity_id, listing_id, now)

    if listing_id == row["primary_listing_id"]:
        conn.commit()
        return identity_id, True

    if (
        listing.source == platform
        and row["primary_source"] != platform
        and not _is_hidden_duplicate(conn, listing_id)
    ):
        conn.execute(
            "UPDATE listing_identities SET primary_listing_id = ? WHERE id = ?",
            (listing_id, identity_id),
        )
        conn.execute("UPDATE listings SET is_primary_sighting = 1 WHERE id = ?", (listing_id,))
        conn.execute(
            "UPDATE listings SET is_primary_sighting = 0 WHERE id = ?",
            (row["primary_listing_id"],),
        )
        conn.commit()
        return identity_id, True

    conn.execute("UPDATE listings SET is_primary_sighting = 0 WHERE id = ?", (listing_id,))
    conn.commit()
    return identity_id, False


# --- Fuzzy duplicate candidates (identity v2 — see duplicates.py) --------------


def _is_hidden_duplicate(conn: sqlite3.Connection, listing_id: int) -> bool:
    """True when a human confirmed this listing as the non-kept side of a
    duplicate pair. Guards resolve_identity()'s promotion branch — a native
    platform row arriving after a proxy must not get is_primary_sighting
    set back to 1 if a person already decided it duplicates another listing."""
    return conn.execute(
        "SELECT 1 FROM listing_duplicates WHERE status = 'confirmed' "
        "AND kept_listing_id != ? AND (listing_a = ? OR listing_b = ?)",
        (listing_id, listing_id, listing_id),
    ).fetchone() is not None


def scan_duplicate_candidates(conn: sqlite3.Connection) -> int:
    """One generation pass: propose probable same-physical-item pairs for
    human review (never merging anything — see duplicates.py). Returns how
    many new pending pairs were recorded.

    Candidates come from live, primary listings matched to the same item —
    item scope is both what bounds the pairwise comparison and where
    double-counting actually hurts (same item, two alerts, two dashboard
    rows). A pair already recorded in *any* status is never re-proposed:
    that uniqueness is the "don't ask again" memory, the same discipline as
    record_suggestion_sighting() ignoring already-decided suggestions."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT m.item_id, l.id, l.title, l.price, l.source, l.location, l.image_url
        FROM listing_matches m
        JOIN listings l ON l.id = m.listing_id
        WHERE l.is_primary_sighting = 1 AND {_NOT_ENDED}
        ORDER BY m.item_id, l.id
        """
    ).fetchall()
    by_item: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_item.setdefault(row["item_id"], []).append(row)

    existing_pairs: set[tuple[int, int]] = set()
    pending_per_item: dict[int, int] = {}
    for row in conn.execute(
        "SELECT listing_a, listing_b, item_id, status FROM listing_duplicates"
    ):
        existing_pairs.add((row["listing_a"], row["listing_b"]))
        if row["status"] == "pending":
            pending_per_item[row["item_id"]] = pending_per_item.get(row["item_id"], 0) + 1

    now = _now()
    created = 0
    for item_id, listings in by_item.items():
        proposals = []
        for idx, a in enumerate(listings):
            for b in listings[idx + 1:]:
                key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if key in existing_pairs:
                    conn.execute(
                        "UPDATE listing_duplicates SET last_seen = ? "
                        "WHERE listing_a = ? AND listing_b = ? AND status = 'pending'",
                        (now, key[0], key[1]),
                    )
                    continue
                result = duplicates.evaluate_pair(a, b)
                if result is not None:
                    proposals.append((key, *result))
        # Highest confidence first, capped so one noisy item can't flood
        # the review queue.
        proposals.sort(key=lambda p: p[1], reverse=True)
        budget = duplicates.MAX_PENDING_PER_ITEM - pending_per_item.get(item_id, 0)
        for key, confidence, signals in proposals[: max(budget, 0)]:
            conn.execute(
                "INSERT INTO listing_duplicates (listing_a, listing_b, item_id, "
                "confidence, signals, status, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (key[0], key[1], item_id, confidence, json.dumps(signals), now, now),
            )
            existing_pairs.add(key)
            created += 1
    conn.commit()
    return created


# Both sides of a pair, aliased a_/b_, for the review UI's side-by-side cards.
_DUPLICATE_SELECT = f"""
SELECT d.*, i.name AS item_name, i.project_id,
       la.title AS a_title, la.price AS a_price, la.source AS a_source,
       la.condition AS a_condition, la.location AS a_location,
       la.first_seen AS a_first_seen, la.image_url AS a_image_url,
       la.url AS a_url, la.end_time AS a_end_time,
       lb.title AS b_title, lb.price AS b_price, lb.source AS b_source,
       lb.condition AS b_condition, lb.location AS b_location,
       lb.first_seen AS b_first_seen, lb.image_url AS b_image_url,
       lb.url AS b_url, lb.end_time AS b_end_time
FROM listing_duplicates d
JOIN items i ON i.id = d.item_id
JOIN listings la ON la.id = d.listing_a
JOIN listings lb ON lb.id = d.listing_b
"""


def list_duplicate_candidates(
    conn: sqlite3.Connection,
    project_id: int | None = None,
    status: str = "pending",
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Duplicate pairs for review. Pending pairs are only shown while both
    sides are still live and primary — once one side has ended or been
    hidden by a canonical merge, there's nothing left to double-count, so
    reviewing the pair is pointless (the row stays, harmlessly pending, and
    is never re-proposed). Decided pairs are shown regardless, so a past
    decision can always be found and reverted."""
    clauses = ["d.status = ?"]
    params: list = [status]
    if status == "pending":
        clauses.append("la.is_primary_sighting = 1 AND lb.is_primary_sighting = 1")
        for alias in ("la", "lb"):
            clauses.append(_NOT_ENDED.replace("l.", f"{alias}."))
    if project_id is not None:
        clauses.append("i.project_id = ?")
        params.append(project_id)
    # Pending pairs surface most-confident first (each card carries its item
    # label, and the web UI caps how many render) — decided pairs, newest
    # decision first.
    order = "d.decided_at DESC" if status != "pending" else "d.confidence DESC, d.id"
    tail = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"{_DUPLICATE_SELECT} WHERE {' AND '.join(clauses)} ORDER BY {order}{tail}",
        params,
    ).fetchall()


def get_duplicate(conn: sqlite3.Connection, dup_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"{_DUPLICATE_SELECT} WHERE d.id = ?", (dup_id,)
    ).fetchone()


def confirm_duplicate(
    conn: sqlite3.Connection, dup_id: int, kept_listing_id: int | None = None
) -> int:
    """Human decision: the pair is the same physical item. Hides the non-kept
    listing from every browsing/alerting surface via is_primary_sighting —
    the same suppression mechanism canonical identity uses; no rows are
    deleted, so a wrong call can always be reverted. Returns the kept
    listing id.

    kept_listing_id=None auto-picks: the live listing if only one still is,
    else the cheaper (used by bulk confirm)."""
    dup = get_duplicate(conn, dup_id)
    if dup is None or dup["status"] != "pending":
        raise ValueError(f"No pending duplicate pair {dup_id}")
    if kept_listing_id is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        a_live = dup["a_end_time"] is None or dup["a_end_time"] > now
        b_live = dup["b_end_time"] is None or dup["b_end_time"] > now
        if a_live != b_live:
            kept_listing_id = dup["listing_a"] if a_live else dup["listing_b"]
        else:
            kept_listing_id = (
                dup["listing_a"] if dup["a_price"] <= dup["b_price"] else dup["listing_b"]
            )
    if kept_listing_id not in (dup["listing_a"], dup["listing_b"]):
        raise ValueError(f"Listing {kept_listing_id} is not part of pair {dup_id}")
    hidden = dup["listing_b"] if kept_listing_id == dup["listing_a"] else dup["listing_a"]
    conn.execute(
        "UPDATE listing_duplicates SET status = 'confirmed', kept_listing_id = ?, "
        "decided_at = ? WHERE id = ?",
        (kept_listing_id, _now(), dup_id),
    )
    conn.execute("UPDATE listings SET is_primary_sighting = 0 WHERE id = ?", (hidden,))
    conn.commit()
    return kept_listing_id


def dismiss_duplicate(conn: sqlite3.Connection, dup_id: int) -> None:
    """Human decision: two different items. Remembered forever — the pair is
    never proposed again (see scan_duplicate_candidates)."""
    conn.execute(
        "UPDATE listing_duplicates SET status = 'dismissed', decided_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), dup_id),
    )
    conn.commit()


def revert_duplicate(conn: sqlite3.Connection, dup_id: int) -> None:
    """Undo a confirm or dismiss, back to pending. Restoring the hidden
    side's is_primary_sighting to 1 is safe even when canonical identity
    disagrees — resolve_identity() re-demotes it on the next watch cycle."""
    dup = get_duplicate(conn, dup_id)
    if dup is None or dup["status"] == "pending":
        return
    if dup["status"] == "confirmed" and dup["kept_listing_id"] is not None:
        hidden = (
            dup["listing_b"] if dup["kept_listing_id"] == dup["listing_a"] else dup["listing_a"]
        )
        conn.execute("UPDATE listings SET is_primary_sighting = 1 WHERE id = ?", (hidden,))
    conn.execute(
        "UPDATE listing_duplicates SET status = 'pending', kept_listing_id = NULL, "
        "decided_at = NULL WHERE id = ?",
        (dup_id,),
    )
    conn.commit()


def pending_duplicate_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Reviewable pending pairs per project id (same both-live-and-primary
    visibility rule as list_duplicate_candidates), for the dashboard's
    per-project "possible duplicates" note."""
    not_ended_a = _NOT_ENDED.replace("l.", "la.")
    not_ended_b = _NOT_ENDED.replace("l.", "lb.")
    rows = conn.execute(
        f"""
        SELECT i.project_id, COUNT(*) AS c
        FROM listing_duplicates d
        JOIN items i ON i.id = d.item_id
        JOIN listings la ON la.id = d.listing_a
        JOIN listings lb ON lb.id = d.listing_b
        WHERE d.status = 'pending'
          AND la.is_primary_sighting = 1 AND lb.is_primary_sighting = 1
          AND {not_ended_a} AND {not_ended_b}
        GROUP BY i.project_id
        """
    ).fetchall()
    return {row["project_id"]: row["c"] for row in rows}


def record_match(
    conn: sqlite3.Connection,
    listing_id: int,
    item_id: int,
    evaluation: Evaluation,
    product_id: int | None = None,
) -> tuple[int, bool]:
    """Record a listing/item match. Returns (match_id, is_new_match).

    `product_id` is the catalogue product (if any) the listing resolved to —
    see `catalogue.match()`. None means it was scored against the item's own
    blended price."""
    row = conn.execute(
        "SELECT id FROM listing_matches WHERE listing_id = ? AND item_id = ?",
        (listing_id, item_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE listing_matches SET grade = ?, deal_score = ?, margin_abs = ?, "
            "margin_pct = ?, under_target = ?, flags = ?, product_id = ? WHERE id = ?",
            (
                evaluation.grade,
                evaluation.deal_score,
                evaluation.margin_abs,
                evaluation.margin_pct,
                int(evaluation.under_target),
                json.dumps(evaluation.flags),
                product_id,
                row["id"],
            ),
        )
        # Committed immediately — the watch loop's next action after a
        # match is often another network call (next term's search, or an
        # enrichment fetch), and an uncommitted write would hold the WAL
        # writer lock through it (and through any rate-limit backoff).
        conn.commit()
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, grade, deal_score, "
        "margin_abs, margin_pct, under_target, flags, matched_at, product_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            listing_id,
            item_id,
            evaluation.grade,
            evaluation.deal_score,
            evaluation.margin_abs,
            evaluation.margin_pct,
            int(evaluation.under_target),
            json.dumps(evaluation.flags),
            _now(),
            product_id,
        ),
    )
    conn.commit()
    return cur.lastrowid, True


def mark_alerted(conn: sqlite3.Connection, match_id: int, channel: str) -> bool:
    """Record an alert. Returns False if already sent on this channel."""
    try:
        conn.execute(
            "INSERT INTO alerts_sent (match_id, channel, sent_at) VALUES (?, ?, ?)",
            (match_id, channel, _now()),
        )
        # Committed before the caller fires the (possibly slow) webhook —
        # never hold the writer lock across network I/O.
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


_MATCH_SELECT = """
SELECT p.name AS project_name, p.slug AS project_slug, p.id AS project_id,
       i.name AS item_name, i.id AS item_id,
       COALESCE(pr.typical_new_price, pr.msrp, i.normal_price) AS normal_price,
       COALESCE(ip.target_deal_price, i.target_deal_price) AS target_deal_price,
       pr.typical_used_price, i.priority,
       pr.manufacturer AS product_manufacturer, pr.model AS product_model,
       pr.price_trend_pct, pr.price_trend_confidence,
       l.id AS listing_id, l.title, l.price, l.currency, l.url, l.source, l.location,
       l.first_seen, l.last_seen, l.end_time, l.bid_count, l.buying_options, l.image_url,
       l.current_bid_price, l.buy_it_now_price,
       m.grade, m.deal_score, m.margin_abs, m.margin_pct, m.under_target, m.flags
FROM listing_matches m
JOIN listings l ON l.id = m.listing_id
JOIN items i ON i.id = m.item_id
JOIN projects p ON p.id = i.project_id
LEFT JOIN products pr ON pr.id = m.product_id
LEFT JOIN item_products ip ON ip.product_id = m.product_id AND ip.item_id = m.item_id
"""

_SORTS = {
    "score": "m.deal_score DESC",
    "price": "l.price ASC",
    "first_seen": "l.first_seen DESC",
}

# An ended listing (auction finished, or a fixed-price listing whose end date
# has passed) isn't buyable at any price, so it never belongs on a browsing
# or preview surface — it should vanish the moment the clock passes, not a
# watch cycle later. eBay end_time strings ("2026-07-06T20:51:35.000Z", UTC)
# compare lexically against SQLite's UTC 'now' rendered in the same
# YYYY-MM-DDTHH:MM:SS prefix format. Rows stay in the DB for provenance and
# price history, and auction_watch deliberately keeps its own listing
# queries — it must keep polling briefly *past* end time to capture the
# closing price (see AuctionSnapshot.ended).
_NOT_ENDED = "(l.end_time IS NULL OR l.end_time > strftime('%Y-%m-%dT%H:%M:%S', 'now'))"

# A match against a knowledge-only tracking (item_products.wanted = 0 — this
# item's own decision, not a global product flag) is identification, not
# endorsement: it keeps price history and identity working but never
# belongs on a deal surface or in an alert. Requires item_products joined
# as `ip` (all deal-surface queries join it, keyed by item_id+product_id).
_WANTED = "(m.product_id IS NULL OR ip.wanted = 1)"


def query_matches(
    conn: sqlite3.Connection,
    project_id: int | None = None,
    item_id: int | None = None,
    source: str | None = None,
    grade: str | None = None,
    flagged: bool | None = None,
    sort: str = "score",
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Browse matches with optional filters. flagged=True → has warning flags
    or graded spares/repair; flagged=False → clean listings only.

    Always excludes non-primary sightings (see resolve_identity()) — a
    listing that's a confirmed duplicate of another, already-surfaced one
    stays in the database for provenance but never appears in results.
    Ended listings (see _NOT_ENDED) and matches against knowledge-only
    products (see _WANTED) are likewise always excluded."""
    clauses, params = ["l.is_primary_sighting = 1", _NOT_ENDED, _WANTED], []
    if project_id is not None:
        clauses.append("p.id = ?")
        params.append(project_id)
    if item_id is not None:
        clauses.append("i.id = ?")
        params.append(item_id)
    if source:
        clauses.append("l.source = ?")
        params.append(source)
    if grade:
        clauses.append("m.grade = ?")
        params.append(grade)
    if flagged is True:
        clauses.append("(m.flags != '[]' OR m.grade = 'spares/repair')")
    elif flagged is False:
        clauses.append("m.flags = '[]' AND m.grade != 'spares/repair'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = _SORTS.get(sort, _SORTS["score"])
    tail = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"{_MATCH_SELECT} {where} ORDER BY {order}{tail}", params
    ).fetchall()


def list_active_auctions(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Live auctions across every project, soonest-ending first — for the
    Active Auctions view (see auction_trajectory.py for the per-row scoring
    the web layer builds on top of this). Same exclusions as query_matches
    (primary sighting, not ended, wanted); buying_options is a small fixed
    set of values from real captures (FIXED_PRICE/AUCTION/BEST_OFFER/
    CLASSIFIED_AD — see tests/fixtures/ebay/), so a LIKE match on the JSON
    text is safe here without needing a join/subquery."""
    tail = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"{_MATCH_SELECT} WHERE l.is_primary_sighting = 1 AND {_NOT_ENDED} AND {_WANTED} "
        f"AND l.buying_options LIKE '%AUCTION%' "
        f"ORDER BY (l.end_time IS NULL), l.end_time ASC{tail}"
    ).fetchall()


def list_offer_listings(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Fixed-price listings that support Best Offer, most recently seen
    first — for the Offers view (see offers.py)."""
    tail = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"{_MATCH_SELECT} WHERE l.is_primary_sighting = 1 AND {_NOT_ENDED} AND {_WANTED} "
        f"AND l.buying_options LIKE '%BEST_OFFER%' "
        f"ORDER BY l.last_seen DESC{tail}"
    ).fetchall()


def project_summaries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per-project counts and best deal score, for the dashboard.

    match_count/best_score exclude non-primary sightings (see
    resolve_identity()) via the CASE guards below, so a confirmed
    cross-source duplicate doesn't inflate the count or surface a stale
    score — the LEFT JOINs are kept as LEFT so item_count (which doesn't
    depend on matches existing at all) is unaffected. Ended listings (see
    _NOT_ENDED) are excluded the same way."""
    return conn.execute(
        f"""
        SELECT p.id, p.name, p.slug, p.archived,
               COUNT(DISTINCT i.id) AS item_count,
               COUNT(CASE WHEN l.is_primary_sighting = 1 AND {_NOT_ENDED} AND {_WANTED} THEN m.id END) AS match_count,
               MAX(CASE WHEN l.is_primary_sighting = 1 AND {_NOT_ENDED} AND {_WANTED} THEN m.deal_score END) AS best_score
        FROM projects p
        LEFT JOIN items i ON i.project_id = p.id AND i.archived = 0
        LEFT JOIN listing_matches m ON m.item_id = i.id
        LEFT JOIN listings l ON l.id = m.listing_id
        LEFT JOIN item_products ip ON ip.product_id = m.product_id AND ip.item_id = m.item_id
        WHERE p.archived = 0
        GROUP BY p.id ORDER BY p.name
        """
    ).fetchall()


def project_top_picks(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Each active project's single best *clean* match, keyed by project id —
    the "here's what stands out" preview shown on the dashboard's project
    cards. Excludes non-primary sightings (see resolve_identity()) and, like
    the dashboard hero, anything flagged or graded spares/repair (same
    predicate as query_matches(flagged=False)): a preview card is a "grab
    this" surface, so the best clean deal beats a higher-scoring warned one.
    A project whose matches are all flagged shows the idle state instead."""
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT p.id AS project_id, i.name AS item_name,
                   l.title, l.price, l.currency, l.url, l.source, l.image_url,
                   m.grade, m.deal_score, m.margin_pct, m.under_target,
                   ROW_NUMBER() OVER (PARTITION BY p.id ORDER BY m.deal_score DESC) AS rn
            FROM listing_matches m
            JOIN listings l ON l.id = m.listing_id
            JOIN items i ON i.id = m.item_id
            JOIN projects p ON p.id = i.project_id
            LEFT JOIN item_products ip ON ip.product_id = m.product_id AND ip.item_id = m.item_id
            WHERE p.archived = 0 AND l.is_primary_sighting = 1
              AND {_NOT_ENDED} AND {_WANTED}
              AND m.flags = '[]' AND m.grade != 'spares/repair'
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["project_id"]: row for row in rows}


_SOURCE_RUN_RETENTION_DAYS = 30


def record_source_run(
    conn: sqlite3.Connection,
    source: str,
    searches: int = 0,
    listings: int = 0,
    errors: int = 0,
    last_error: str | None = None,
    duration_ms: int = 0,
    new_listings: int = 0,
    duplicates: int = 0,
    catalogue_matches: int = 0,
    deals_found: int = 0,
) -> None:
    """One connector's outcome for one search cycle (see runner.run_once) —
    the raw material for the Sources page health column and the roadmap's
    coverage metrics. Rows older than the retention window are pruned on
    write so the table can't grow unboundedly (roadmap: "Keeping the system
    healthy" — retention handled opportunistically where data is created).

    duration_ms is wall-clock time spent inside this connector's search()
    calls only (not the DB/matching work per listing) — the number an
    orchestrator would actually want for scheduling/back-off decisions.
    new_listings/duplicates/catalogue_matches/deals_found are this cycle's
    counts of, respectively: listings not seen before (db.upsert_listing),
    listings resolved as a non-primary cross-source sighting of a listing
    already known (db.resolve_identity), listings that matched a catalogue
    product, and listings that met evaluation.under_target."""
    conn.execute(
        "INSERT INTO source_runs (source, run_at, ok, searches, listings, errors, "
        "last_error, duration_ms, new_listings, duplicates, catalogue_matches, deals_found) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, _now(), 1 if errors == 0 else 0, searches, listings, errors, last_error,
         duration_ms, new_listings, duplicates, catalogue_matches, deals_found),
    )
    # first_seen is durable per-source metadata, not run telemetry — it must
    # survive the retention pruning below, so it lives in source_settings
    # (see _MIGRATIONS) and is set once, on this source's first-ever run.
    conn.execute(
        "INSERT INTO source_settings (name, first_seen) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "first_seen = COALESCE(source_settings.first_seen, excluded.first_seen)",
        (source, _now()),
    )
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_SOURCE_RUN_RETENTION_DAYS)
    ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM source_runs WHERE run_at < ?", (cutoff,))
    # Committed here because run_once calls retailer_price (network) next.
    conn.commit()


#: How many of a source's most recent runs feed recent_avg_duration_ms/
#: recent_avg_listings_found (source_health, below) — a sampling parameter
#: for what counts as "recent" (owned here, alongside the query that
#: produces it), not a health-model threshold (those live in
#: connector_health.py and are expressed purely in terms of the numbers
#: this module publishes, so neither module needs to import constants from
#: the other).
_RECENT_RUN_SAMPLE_SIZE = 5


def source_health(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-connector health, keyed by source name.

    Two different kinds of number here, deliberately not blended:
    - snapshot facts: last run, whether it was clean, first_seen, last
      success/failure, consecutive failing runs (0 for a healthy source),
      24-hour ingest volume.
    - averages/rates over every run still inside the retention window (see
      _SOURCE_RUN_RETENTION_DAYS) — success_rate and the average_* fields.
      Bounded by the same 30-day window as last_success_at always was; a
      connector with no runs in the window has no history to average, same
      as it already had no last_success_at.

    No health score or status here — raw telemetry only. Turning this into
    an explainable Healthy/Warning/Degraded/Offline model
    (connector_health.py, roadmap Phase D) is built on top of these
    numbers rather than duplicating them — including recent_avg_duration_ms/
    recent_avg_listings_found/recent_run_count below, which exist
    specifically so Phase D can compare a connector against its *own*
    recent history rather than an arbitrary cross-connector number (an
    RSS feed and the eBay API have very different normal latencies, so an
    absolute latency threshold would be unfair to one or meaningless for
    the other). recent_* covers the most recent _RECENT_RUN_SAMPLE_SIZE
    runs still in the retention window (fewer if the source doesn't have
    that many yet) — free to compute in the same pass as everything else
    above, since rows already arrive ordered newest-first per source.

    Sources with no recorded runs simply aren't present — the UI shows them
    as "not yet run"."""
    rows = conn.execute(
        "SELECT source, run_at, ok, listings, errors, last_error, duration_ms, "
        "new_listings, duplicates, catalogue_matches, deals_found "
        "FROM source_runs ORDER BY source, run_at DESC, id DESC"
    ).fetchall()
    first_seen = {
        row["name"]: row["first_seen"]
        for row in conn.execute(
            "SELECT name, first_seen FROM source_settings WHERE first_seen IS NOT NULL"
        )
    }
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    health: dict[str, dict] = {}
    for row in rows:
        h = health.setdefault(
            row["source"],
            {
                "first_seen": first_seen.get(row["source"]),
                "last_run_at": row["run_at"],
                "last_ok": bool(row["ok"]),
                "last_error": row["last_error"],
                "last_success_at": None,
                "last_failed_at": None,
                "consecutive_failures": 0,
                "listings_24h": 0,
                "errors_24h": 0,
                "total_runs": 0,
                "ok_runs": 0,
                "success_rate": None,
                "avg_duration_ms": None,
                "avg_listings_found": None,
                "avg_new_listings": None,
                "avg_duplicates": None,
                "avg_catalogue_matches": None,
                "avg_deals_found": None,
                "recent_avg_duration_ms": None,
                "recent_avg_listings_found": None,
                "recent_run_count": 0,
                "_streak_open": True,
                "_sum_duration_ms": 0,
                "_sum_listings": 0,
                "_sum_new_listings": 0,
                "_sum_duplicates": 0,
                "_sum_catalogue_matches": 0,
                "_sum_deals_found": 0,
                "_recent_duration_ms": [],
                "_recent_listings": [],
            },
        )
        h["total_runs"] += 1
        if row["ok"]:
            h["ok_runs"] += 1
            if h["last_success_at"] is None:
                h["last_success_at"] = row["run_at"]
        elif h["last_failed_at"] is None:
            h["last_failed_at"] = row["run_at"]
        if h["_streak_open"]:
            if row["ok"]:
                h["_streak_open"] = False
            else:
                h["consecutive_failures"] += 1
        if row["run_at"] >= cutoff_24h:
            h["listings_24h"] += row["listings"]
            h["errors_24h"] += row["errors"]
        h["_sum_duration_ms"] += row["duration_ms"]
        h["_sum_listings"] += row["listings"]
        h["_sum_new_listings"] += row["new_listings"]
        h["_sum_duplicates"] += row["duplicates"]
        h["_sum_catalogue_matches"] += row["catalogue_matches"]
        h["_sum_deals_found"] += row["deals_found"]
        if len(h["_recent_duration_ms"]) < _RECENT_RUN_SAMPLE_SIZE:
            h["_recent_duration_ms"].append(row["duration_ms"])
            h["_recent_listings"].append(row["listings"])
    for h in health.values():
        del h["_streak_open"]
        n = h["total_runs"]
        h["success_rate"] = round(100 * h["ok_runs"] / n) if n else None
        h["avg_duration_ms"] = round(h["_sum_duration_ms"] / n) if n else None
        h["avg_listings_found"] = round(h["_sum_listings"] / n, 1) if n else None
        h["avg_new_listings"] = round(h["_sum_new_listings"] / n, 1) if n else None
        h["avg_duplicates"] = round(h["_sum_duplicates"] / n, 1) if n else None
        h["avg_catalogue_matches"] = round(h["_sum_catalogue_matches"] / n, 1) if n else None
        h["avg_deals_found"] = round(h["_sum_deals_found"] / n, 1) if n else None
        recent_n = len(h["_recent_duration_ms"])
        h["recent_run_count"] = recent_n
        if recent_n:
            h["recent_avg_duration_ms"] = round(sum(h["_recent_duration_ms"]) / recent_n)
            h["recent_avg_listings_found"] = round(sum(h["_recent_listings"]) / recent_n, 1)
        for key in ("_sum_duration_ms", "_sum_listings", "_sum_new_listings",
                    "_sum_duplicates", "_sum_catalogue_matches", "_sum_deals_found",
                    "_recent_duration_ms", "_recent_listings"):
            del h[key]
    return health


# A listing with no end_time can't expire on its own — if the source stops
# returning it (sold, delisted, filtered out) it just lingers. Not rescanned
# for this long = probably gone; the roadmap's "source freshness /
# stale-listing rate" metric counts exactly these.
_STALE_AFTER_HOURS = 48


def source_coverage(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-source data coverage, keyed by source name — the roadmap's
    "coverage should become measurable" metrics, computed from what each
    source has actually contributed over time. Complements source_health(),
    which only says whether recent runs succeeded, not whether they were
    worth anything.

    Per source: total/live listing counts, ingest rate (new in 24h / 7d),
    stale count (no end_time and not seen for _STALE_AFTER_HOURS — likely
    sold/delisted but impossible to know), hidden duplicate count (sightings
    suppressed by identity v1/v2 — a high share means the source mostly
    re-shows things already seen elsewhere), catalogue match counts/rate,
    and price observations contributed in the last 30 days.

    Not measurable yet (data isn't attributed per marketplace): product
    suggestion yield (product_suggestions.source records the *discovery
    mechanism*, e.g. 'ebay-structured'/'ollama') and enrichment success
    rate (only the attempt is recorded, via listings.brand_checked)."""
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    cutoff_7d = (now - timedelta(days=7)).isoformat(timespec="seconds")
    cutoff_stale = (now - timedelta(hours=_STALE_AFTER_HOURS)).isoformat(timespec="seconds")
    cutoff_30d = (now - timedelta(days=30)).isoformat(timespec="seconds")

    coverage: dict[str, dict] = {}

    def entry(source: str) -> dict:
        return coverage.setdefault(source, {
            "listings_total": 0, "listings_live": 0,
            "new_24h": 0, "new_7d": 0, "stale": 0,
            "hidden_duplicates": 0,
            "matches_total": 0, "matches_catalogued": 0,
            "catalogue_match_pct": None,
            "price_observations_30d": 0,
        })

    for row in conn.execute(
        f"""
        SELECT l.source,
               COUNT(*) AS listings_total,
               SUM(CASE WHEN {_NOT_ENDED} THEN 1 ELSE 0 END) AS listings_live,
               SUM(CASE WHEN l.first_seen >= ? THEN 1 ELSE 0 END) AS new_24h,
               SUM(CASE WHEN l.first_seen >= ? THEN 1 ELSE 0 END) AS new_7d,
               SUM(CASE WHEN l.end_time IS NULL AND l.last_seen < ? THEN 1 ELSE 0 END) AS stale,
               SUM(CASE WHEN l.is_primary_sighting = 0 THEN 1 ELSE 0 END) AS hidden_duplicates
        FROM listings l
        GROUP BY l.source
        """,
        (cutoff_24h, cutoff_7d, cutoff_stale),
    ):
        e = entry(row["source"])
        for key in ("listings_total", "listings_live", "new_24h", "new_7d",
                    "stale", "hidden_duplicates"):
            e[key] = row[key]

    for row in conn.execute(
        """
        SELECT l.source,
               COUNT(*) AS matches_total,
               COUNT(m.product_id) AS matches_catalogued
        FROM listing_matches m
        JOIN listings l ON l.id = m.listing_id
        GROUP BY l.source
        """
    ):
        e = entry(row["source"])
        e["matches_total"] = row["matches_total"]
        e["matches_catalogued"] = row["matches_catalogued"]
        if row["matches_total"]:
            e["catalogue_match_pct"] = round(
                100 * row["matches_catalogued"] / row["matches_total"]
            )

    for row in conn.execute(
        "SELECT source, COUNT(*) AS n FROM product_price_observations "
        "WHERE observed_at >= ? GROUP BY source",
        (cutoff_30d,),
    ):
        entry(row["source"])["price_observations_30d"] = row["n"]

    return coverage


# Why this isn't computed: listing_matches.matched_at is stamped once, at
# INSERT time — a listing's very first scan, which under the current
# synchronous match-on-ingest pipeline (runner.run_once calls
# db.record_match on every listing every cycle) always coincides with
# first_seen. product_id, by contrast, IS overwritten on every rescan
# (db.record_match's UPDATE path) — so a listing that starts unmatched and
# later resolves to a catalogue product (e.g. once the catalogue grows)
# silently gains a product_id with no record of *when* that happened.
# There is no honest way to answer "how long after first being seen did
# this source's listings typically get catalogued" from what's persisted
# today. Would need a separate catalogue_matched_at column on
# listing_matches, set once when product_id first transitions from NULL to
# non-NULL, to compute this without guessing.
TIME_TO_FIRST_MATCH_UNAVAILABLE = (
    "Not tracked: matched_at is stamped once at first scan (always ~equal "
    "to first_seen), and product_id is silently overwritten on every "
    "rescan with no timestamp for when a catalogue match first became "
    "true. Needs a dedicated catalogue_matched_at column, set once, to "
    "compute honestly."
)


def source_coverage_analytics(conn: sqlite3.Connection) -> dict[str, dict]:
    """Phase B ("Coverage Analytics") — rate-based metrics answering "which
    source actually finds useful deals", not just "which returns the most
    listings". Layered on top of source_coverage() rather than duplicating
    its counts: two new lightweight GROUP BY queries (deal counts, resolved
    -listing lifetimes, all-time price-observation counts) plus arithmetic
    on numbers source_coverage() already computed. No per-listing scans, no
    N+1 — safe for dashboard load.

    Per source:
    - total_sightings: every listing row ever recorded for this source,
      before cross-source dedup (same figure as source_coverage's
      listings_total, exposed under a name that matches what it measures
      here).
    - unique_listings: the subset that are the *primary* sighting of a
      real-world item (is_primary_sighting=1) — total_sightings minus
      source_coverage's hidden_duplicates.
    - duplicate_suppression_pct: hidden_duplicates / total_sightings — how
      much of what this source shows us turns out to be something another
      sighting (same source or not) already covered.
    - catalogue_match_pct: reused directly from source_coverage.
    - deal_rate_pct: of this source's *primary* listings that were ever
      evaluated against an item, the share whose most recent evaluation
      met evaluation.under_target (listing_matches.under_target) — primary
      only, so a deal isn't counted twice via a cross-source duplicate.
    - stale_rate_pct: source_coverage's stale / total_sightings.
    - avg_lifetime_days / lifetime_sample_size: mean days between
      first_seen and a *resolved* end — end_time for a listing that's
      actually ended, or last_seen for one that's gone stale (no end_time,
      not rescanned in _STALE_AFTER_HOURS — the same "probably gone"
      definition source_coverage's stale count uses). Still-live listings
      being rescanned every cycle are excluded on purpose: their lifetime
      hasn't concluded, so including "time since first seen" for them
      would understate true lifetime and drift the average every cycle.
      lifetime_sample_size is always reported alongside the average so a
      figure backed by one resolved listing doesn't look as solid as one
      backed by fifty. None (not 0) when no listing has resolved yet.
    - price_history_coverage_pct: all-time product_price_observations for
      this source, divided by matches_catalogued (source_coverage). This
      is a ratio of aggregate counts, not a verified per-listing join —
      observations are keyed by (product_id, source, observed_at), not by
      listing_id, so it answers "roughly how much of what we catalogued
      from this source ever produced a price data point", not an exact
      per-listing figure.
    - time_to_first_match: always None — see TIME_TO_FIRST_MATCH_UNAVAILABLE.
    """
    coverage = source_coverage(conn)
    now = datetime.now(timezone.utc)
    cutoff_stale = (now - timedelta(hours=_STALE_AFTER_HOURS)).isoformat(timespec="seconds")

    analytics: dict[str, dict] = {}

    def entry(source: str) -> dict:
        return analytics.setdefault(source, {
            "total_sightings": 0,
            "unique_listings": 0,
            "duplicate_suppression_pct": None,
            "catalogue_match_pct": None,
            "deal_rate_pct": None,
            "stale_rate_pct": None,
            "avg_lifetime_days": None,
            "lifetime_sample_size": 0,
            "price_history_coverage_pct": None,
            "time_to_first_match": None,
            "time_to_first_match_unavailable_reason": TIME_TO_FIRST_MATCH_UNAVAILABLE,
        })

    for source, cov in coverage.items():
        e = entry(source)
        e["total_sightings"] = cov["listings_total"]
        e["unique_listings"] = cov["listings_total"] - cov["hidden_duplicates"]
        if cov["listings_total"]:
            e["duplicate_suppression_pct"] = round(
                100 * cov["hidden_duplicates"] / cov["listings_total"]
            )
            e["stale_rate_pct"] = round(100 * cov["stale"] / cov["listings_total"])
        e["catalogue_match_pct"] = cov["catalogue_match_pct"]

    for row in conn.execute(
        """
        SELECT l.source AS source,
               COUNT(*) AS evaluated,
               SUM(CASE WHEN m.under_target = 1 THEN 1 ELSE 0 END) AS deals
        FROM listing_matches m
        JOIN listings l ON l.id = m.listing_id
        WHERE l.is_primary_sighting = 1
        GROUP BY l.source
        """
    ):
        if row["evaluated"]:
            entry(row["source"])["deal_rate_pct"] = round(
                100 * row["deals"] / row["evaluated"]
            )

    for row in conn.execute(
        """
        SELECT source, AVG(lifetime_days) AS avg_days, COUNT(*) AS n
        FROM (
            SELECT l.source AS source,
                   CASE
                       WHEN l.end_time IS NOT NULL
                            AND l.end_time <= strftime('%Y-%m-%dT%H:%M:%S', 'now')
                       THEN julianday(l.end_time) - julianday(l.first_seen)
                       WHEN l.end_time IS NULL AND l.last_seen < ?
                       THEN julianday(l.last_seen) - julianday(l.first_seen)
                   END AS lifetime_days
            FROM listings l
        )
        WHERE lifetime_days IS NOT NULL
        GROUP BY source
        """,
        (cutoff_stale,),
    ):
        e = entry(row["source"])
        e["avg_lifetime_days"] = round(row["avg_days"], 1)
        e["lifetime_sample_size"] = row["n"]

    for row in conn.execute(
        "SELECT source, COUNT(*) AS n FROM product_price_observations GROUP BY source"
    ):
        e = entry(row["source"])
        matches_catalogued = coverage.get(row["source"], {}).get("matches_catalogued")
        if matches_catalogued:
            e["price_history_coverage_pct"] = round(100 * row["n"] / matches_catalogued)

    return analytics


def latest_activity(conn: sqlite3.Connection) -> str | None:
    """Latest listing timestamp seen by `watch`/`run-once` — a cheap "did a
    search just run" signal for the dashboard's live-polling JS. Every
    listing touched in a cycle gets its last_seen bumped, whether new or
    not, so this changes on any cycle that fetched at least one listing."""
    row = conn.execute("SELECT MAX(last_seen) AS ts FROM listings").fetchone()
    return row["ts"] if row else None


def dashboard_stats(conn: sqlite3.Connection) -> dict:
    """Headline counts for the dashboard stat strip. Same visibility rules as
    query_matches: primary sightings only, "clean" means no warning flags and
    not graded spares/repair, "hot" matches the score >= 70 band used for the
    green "hi" badge throughout the UI."""
    new_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    row = conn.execute(
        f"""
        SELECT
          COUNT(CASE WHEN m.flags = '[]' AND m.grade != 'spares/repair'
                     THEN m.id END) AS clean_matches,
          COUNT(CASE WHEN m.flags = '[]' AND m.grade != 'spares/repair'
                          AND m.deal_score >= 70
                     THEN m.id END) AS hot_deals,
          COUNT(CASE WHEN m.flags = '[]' AND m.grade != 'spares/repair'
                          AND l.first_seen >= ?
                     THEN m.id END) AS new_today
        FROM listing_matches m
        JOIN listings l ON l.id = m.listing_id
        JOIN items i ON i.id = m.item_id AND i.archived = 0
        JOIN projects p ON p.id = i.project_id AND p.archived = 0
        LEFT JOIN item_products ip ON ip.product_id = m.product_id AND ip.item_id = m.item_id
        WHERE l.is_primary_sighting = 1 AND {_NOT_ENDED} AND {_WANTED}
        """,
        (new_cutoff,),
    ).fetchone()
    projects = conn.execute(
        "SELECT COUNT(*) AS n FROM projects WHERE archived = 0"
    ).fetchone()
    return {
        "clean_matches": row["clean_matches"],
        "hot_deals": row["hot_deals"],
        "new_today": row["new_today"],
        "projects": projects["n"],
    }
