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
from datetime import datetime, timezone
from pathlib import Path

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
"""

# Columns added since the first release; applied to pre-existing databases.
_MIGRATIONS = [
    ("projects", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "sources", "TEXT"),
    ("items", "terms", "TEXT NOT NULL DEFAULT '[]'"),
    ("items", "exclude_terms", "TEXT NOT NULL DEFAULT '[]'"),
    ("items", "sources", "TEXT"),
    ("items", "archived", "INTEGER NOT NULL DEFAULT 0"),
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
    """Hard delete an item and its matches/alerts (listings are kept)."""
    conn.execute(
        "DELETE FROM alerts_sent WHERE match_id IN "
        "(SELECT id FROM listing_matches WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute("DELETE FROM listing_matches WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    if _commit:
        conn.commit()


# --- Listings, matches, alerts -------------------------------------------------


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> tuple[int, bool]:
    """Insert a listing or touch last_seen. Returns (listing_id, is_new)."""
    now = _now()
    row = conn.execute(
        "SELECT id FROM listings WHERE source = ? AND external_id = ?",
        (listing.source, listing.external_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE listings SET last_seen = ?, price = ?, title = ? WHERE id = ?",
            (now, listing.price, listing.title, row["id"]),
        )
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO listings (source, external_id, title, price, currency, url, "
        "location, description, condition, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    return cur.lastrowid, True


def record_match(
    conn: sqlite3.Connection, listing_id: int, item_id: int, evaluation: Evaluation
) -> tuple[int, bool]:
    """Record a listing/item match. Returns (match_id, is_new_match)."""
    row = conn.execute(
        "SELECT id FROM listing_matches WHERE listing_id = ? AND item_id = ?",
        (listing_id, item_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE listing_matches SET grade = ?, deal_score = ?, margin_abs = ?, "
            "margin_pct = ?, under_target = ?, flags = ? WHERE id = ?",
            (
                evaluation.grade,
                evaluation.deal_score,
                evaluation.margin_abs,
                evaluation.margin_pct,
                int(evaluation.under_target),
                json.dumps(evaluation.flags),
                row["id"],
            ),
        )
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO listing_matches (listing_id, item_id, grade, deal_score, "
        "margin_abs, margin_pct, under_target, flags, matched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
       i.normal_price, i.target_deal_price, i.priority,
       l.title, l.price, l.currency, l.url, l.source, l.location, l.first_seen,
       m.grade, m.deal_score, m.margin_abs, m.margin_pct, m.under_target, m.flags
FROM listing_matches m
JOIN listings l ON l.id = m.listing_id
JOIN items i ON i.id = m.item_id
JOIN projects p ON p.id = i.project_id
"""

_SORTS = {
    "score": "m.deal_score DESC",
    "price": "l.price ASC",
    "first_seen": "l.first_seen DESC",
}


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
    or graded spares/repair; flagged=False → clean listings only."""
    clauses, params = [], []
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
    """Per-project counts and best deal score, for the dashboard."""
    return conn.execute(
        """
        SELECT p.id, p.name, p.slug, p.archived,
               COUNT(DISTINCT i.id) AS item_count,
               COUNT(m.id) AS match_count,
               MAX(m.deal_score) AS best_score
        FROM projects p
        LEFT JOIN items i ON i.project_id = p.id AND i.archived = 0
        LEFT JOIN listing_matches m ON m.item_id = i.id
        WHERE p.archived = 0
        GROUP BY p.id ORDER BY p.name
        """
    ).fetchall()


def project_top_picks(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Each active project's single best match, keyed by project id — the
    "here's what stands out" preview shown on the dashboard's project cards."""
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT p.id AS project_id, i.name AS item_name,
                   l.title, l.price, l.currency, l.url, l.source,
                   m.grade, m.deal_score, m.margin_pct, m.under_target,
                   ROW_NUMBER() OVER (PARTITION BY p.id ORDER BY m.deal_score DESC) AS rn
            FROM listing_matches m
            JOIN listings l ON l.id = m.listing_id
            JOIN items i ON i.id = m.item_id
            JOIN projects p ON p.id = i.project_id
            WHERE p.archived = 0
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
