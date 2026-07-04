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

from . import catalogue, identity, price_trend
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
    item_id INTEGER NOT NULL REFERENCES items(id),
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    match_terms TEXT NOT NULL DEFAULT '[]',
    normal_price REAL,
    target_deal_price REAL,
    archived INTEGER NOT NULL DEFAULT 0
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
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


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
    for table, column, decl in _MIGRATIONS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            if table == "products" and column == "typical_new_price":
                # normal_price predates the msrp/typical_new_price split —
                # carry forward any existing value, since it was
                # functionally "the new price" before the split. Only runs
                # the moment this column is added (i.e. once per database,
                # ever) — this used to run unconditionally on every single
                # connect(), which meant every web request and every watch
                # tick took a write lock for a no-op UPDATE, and enough of
                # them colliding produced "database is locked".
                conn.execute(
                    "UPDATE products SET typical_new_price = normal_price "
                    "WHERE typical_new_price IS NULL AND normal_price IS NOT NULL"
                )
    conn.commit()
    return conn


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
    """Hard delete an item and its matches/alerts/products (listings are kept)."""
    conn.execute(
        "DELETE FROM alerts_sent WHERE match_id IN "
        "(SELECT id FROM listing_matches WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute("DELETE FROM listing_matches WHERE item_id = ?", (item_id,))
    conn.execute(
        "DELETE FROM product_price_observations WHERE product_id IN "
        "(SELECT id FROM products WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute(
        "DELETE FROM product_new_price_history WHERE product_id IN "
        "(SELECT id FROM products WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute("DELETE FROM product_suggestions WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM products WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    if _commit:
        conn.commit()


# --- Product catalogue CRUD (manufacturer/model tracked under one item) ------


def _product_from_row(row: sqlite3.Row) -> catalogue.Product:
    return catalogue.Product(
        id=row["id"],
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
    )


def list_products(
    conn: sqlite3.Connection, item_id: int, include_archived: bool = True
) -> list[sqlite3.Row]:
    where = "item_id = ?" if include_archived else "item_id = ? AND archived = 0"
    return conn.execute(
        f"SELECT * FROM products WHERE {where} ORDER BY archived, manufacturer, model",
        (item_id,),
    ).fetchall()


def list_products_for_matching(conn: sqlite3.Connection, item_id: int) -> list[catalogue.Product]:
    """Active catalogue products for an item, ready for catalogue.match()."""
    return [_product_from_row(r) for r in list_products(conn, item_id, include_archived=False)]


def get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


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
    cur = conn.execute(
        "INSERT INTO products (item_id, manufacturer, model, match_terms, "
        "msrp, typical_new_price, target_deal_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, manufacturer, model, json.dumps(match_terms), msrp, typical_new_price, target_deal_price),
    )
    conn.commit()
    return cur.lastrowid


def update_product(
    conn: sqlite3.Connection,
    product_id: int,
    manufacturer: str,
    model: str,
    match_terms: list[str],
    msrp: float | None,
    typical_new_price: float | None,
    target_deal_price: float | None,
) -> None:
    conn.execute(
        "UPDATE products SET manufacturer = ?, model = ?, match_terms = ?, "
        "msrp = ?, typical_new_price = ?, target_deal_price = ? WHERE id = ?",
        (manufacturer, model, json.dumps(match_terms), msrp, typical_new_price, target_deal_price, product_id),
    )
    conn.commit()


def set_product_archived(conn: sqlite3.Connection, product_id: int, archived: bool) -> None:
    conn.execute("UPDATE products SET archived = ? WHERE id = ?", (int(archived), product_id))
    conn.commit()


def delete_product(conn: sqlite3.Connection, product_id: int) -> None:
    conn.execute(
        "UPDATE listing_matches SET product_id = NULL WHERE product_id = ?", (product_id,)
    )
    conn.execute("DELETE FROM product_price_observations WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM product_new_price_history WHERE product_id = ?", (product_id,))
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
    conn.commit()


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
    """Active products with no canonical retailer URL that haven't had a
    Stage-1 search attempt yet."""
    return conn.execute(
        "SELECT * FROM products WHERE archived = 0 AND canonical_price_url IS NULL "
        "AND price_search_checked = 0"
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
        "SELECT * FROM products WHERE archived = 0 AND canonical_price_url IS NOT NULL "
        "AND (last_price_check_at IS NULL OR last_price_check_at < ?)",
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
    existing = conn.execute(
        "SELECT * FROM product_suggestions WHERE item_id = ? AND manufacturer = ? AND model = ?",
        (item_id, manufacturer, model),
    ).fetchone()
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


def approve_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> int:
    """Create the real catalogue product from a pending suggestion. Returns
    the new product's id."""
    suggestion = get_product_suggestion(conn, suggestion_id)
    combined = f"{suggestion['manufacturer']} {suggestion['model']}".strip()
    match_terms = [combined]
    if suggestion["model"] and suggestion["model"] != combined:
        match_terms.append(suggestion["model"])
    product_id = create_product(
        conn, suggestion["item_id"], suggestion["manufacturer"], suggestion["model"],
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
    """Listings that are candidates for end-of-auction price capture: not yet
    captured, matched to a catalogue product, with a known end time that
    hasn't gone stale (in case the app was offline past its close). Filtered
    further in Python (auction_watch.py) for "is this actually an auction"
    and "is it actually due for a poll right now" — both awkward to express
    over a JSON column and a variable cadence in SQL."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_staleness_days)).isoformat(
        timespec="seconds"
    )
    return conn.execute(
        """
        SELECT DISTINCT l.*, m.product_id
        FROM listings l
        JOIN listing_matches m ON m.listing_id = l.id
        WHERE l.sold_captured = 0
          AND l.end_time IS NOT NULL
          AND l.end_time >= ?
          AND m.product_id IS NOT NULL
        """,
        (cutoff,),
    ).fetchall()


def mark_listing_polled(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET last_poll_at = ? WHERE id = ?", (_now(), listing_id))
    conn.commit()


def mark_sold_captured(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET sold_captured = 1 WHERE id = ?", (listing_id,))
    conn.commit()


# --- Listings, matches, alerts -------------------------------------------------


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> tuple[int, bool]:
    """Insert a listing or touch last_seen. Returns (listing_id, is_new).

    buying_options/bid_count/end_time/image_url are refreshed on every rescan
    too (a Buy It Now can disappear once bidding starts, bid count/price move,
    sellers swap photos) — this is what the auction-close poller
    (auction_watch.py) later reads to know which listings are auctions and
    when they end. image_url only ever overwrites with a real value, so a
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
            "image_url = COALESCE(?, image_url) WHERE id = ?",
            (now, listing.price, listing.title, buying_options, listing.bid_count,
             listing.end_time, listing.image_url, row["id"]),
        )
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO listings (source, external_id, title, price, currency, url, "
        "location, description, condition, first_seen, last_seen, "
        "buying_options, bid_count, end_time, image_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    return cur.lastrowid, True


def get_listing(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


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

    if listing.source == platform and row["primary_source"] != platform:
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
    return cur.lastrowid, True


def mark_alerted(conn: sqlite3.Connection, match_id: int, channel: str) -> bool:
    """Record an alert. Returns False if already sent on this channel."""
    try:
        conn.execute(
            "INSERT INTO alerts_sent (match_id, channel, sent_at) VALUES (?, ?, ?)",
            (match_id, channel, _now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


_MATCH_SELECT = """
SELECT p.name AS project_name, p.slug AS project_slug, p.id AS project_id,
       i.name AS item_name, i.id AS item_id,
       COALESCE(pr.typical_new_price, pr.msrp, i.normal_price) AS normal_price,
       COALESCE(pr.target_deal_price, i.target_deal_price) AS target_deal_price,
       pr.typical_used_price, i.priority,
       pr.manufacturer AS product_manufacturer, pr.model AS product_model,
       pr.price_trend_pct, pr.price_trend_confidence,
       l.title, l.price, l.currency, l.url, l.source, l.location, l.first_seen,
       l.last_seen, l.end_time, l.bid_count, l.buying_options, l.image_url,
       m.grade, m.deal_score, m.margin_abs, m.margin_pct, m.under_target, m.flags
FROM listing_matches m
JOIN listings l ON l.id = m.listing_id
JOIN items i ON i.id = m.item_id
JOIN projects p ON p.id = i.project_id
LEFT JOIN products pr ON pr.id = m.product_id
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
    Ended listings (see _NOT_ENDED) are likewise always excluded."""
    clauses, params = ["l.is_primary_sighting = 1", _NOT_ENDED], []
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
               COUNT(CASE WHEN l.is_primary_sighting = 1 AND {_NOT_ENDED} THEN m.id END) AS match_count,
               MAX(CASE WHEN l.is_primary_sighting = 1 AND {_NOT_ENDED} THEN m.deal_score END) AS best_score
        FROM projects p
        LEFT JOIN items i ON i.project_id = p.id AND i.archived = 0
        LEFT JOIN listing_matches m ON m.item_id = i.id
        LEFT JOIN listings l ON l.id = m.listing_id
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
            WHERE p.archived = 0 AND l.is_primary_sighting = 1
              AND {_NOT_ENDED}
              AND m.flags = '[]' AND m.grade != 'spares/repair'
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["project_id"]: row for row in rows}


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
        WHERE l.is_primary_sighting = 1 AND {_NOT_ENDED}
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
