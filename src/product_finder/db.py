"""SQLite storage: projects, items, listings, matches, alerts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig
from .models import Evaluation, Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def sync_config(conn: sqlite3.Connection, cfg: AppConfig) -> dict[tuple[str, str], int]:
    """Upsert projects/items from config. Returns {(project_slug, item_name): item_id}."""
    item_ids: dict[tuple[str, str], int] = {}
    for project in cfg.projects:
        conn.execute(
            "INSERT INTO projects (slug, name) VALUES (?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name = excluded.name",
            (project.slug, project.name),
        )
        project_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (project.slug,)
        ).fetchone()["id"]
        for item in project.items:
            conn.execute(
                "INSERT INTO items (project_id, name, priority, max_price, normal_price, target_deal_price, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, name) DO UPDATE SET "
                "priority = excluded.priority, max_price = excluded.max_price, "
                "normal_price = excluded.normal_price, target_deal_price = excluded.target_deal_price, "
                "notes = excluded.notes",
                (
                    project_id,
                    item.name,
                    item.priority,
                    item.max_price,
                    item.normal_price,
                    item.target_deal_price,
                    item.notes,
                ),
            )
            item_id = conn.execute(
                "SELECT id FROM items WHERE project_id = ? AND name = ?",
                (project_id, item.name),
            ).fetchone()["id"]
            item_ids[(project.slug, item.name)] = item_id
    conn.commit()
    return item_ids


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


def report_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All matches joined with listings/items/projects, best deals first."""
    return conn.execute(
        """
        SELECT p.name AS project_name, p.slug AS project_slug,
               i.name AS item_name, i.normal_price, i.target_deal_price, i.priority,
               l.title, l.price, l.currency, l.url, l.source, l.location, l.first_seen,
               m.grade, m.deal_score, m.margin_abs, m.margin_pct, m.under_target, m.flags
        FROM listing_matches m
        JOIN listings l ON l.id = m.listing_id
        JOIN items i ON i.id = m.item_id
        JOIN projects p ON p.id = i.project_id
        ORDER BY p.name, i.name, m.deal_score DESC
        """
    ).fetchall()
