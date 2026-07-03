import sqlite3
from unittest import mock

from product_finder import db


def _make_pre_pass1_db(path):
    """A products table as it existed before the msrp/typical_new_price/
    typical_used_price split — no data-migration columns yet."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            manufacturer TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            match_terms TEXT NOT NULL DEFAULT '[]',
            normal_price REAL,
            target_deal_price REAL,
            archived INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO products (item_id, manufacturer, model, normal_price) "
        "VALUES (1, 'Makita', 'LS0816F/2', 500)"
    )
    conn.commit()
    conn.close()


def test_backfill_runs_once_when_column_is_newly_added(tmp_path):
    path = tmp_path / "t.db"
    _make_pre_pass1_db(path)
    conn = db.connect(path)
    row = conn.execute("SELECT typical_new_price FROM products").fetchone()
    assert row["typical_new_price"] == 500


def test_backfill_does_not_overwrite_a_value_set_after_migration(tmp_path):
    path = tmp_path / "t.db"
    _make_pre_pass1_db(path)
    conn = db.connect(path)  # first connect: column added, backfilled to 500
    conn.execute("UPDATE products SET typical_new_price = 350")
    conn.commit()
    conn.close()

    conn2 = db.connect(path)  # second connect: column already exists
    row = conn2.execute("SELECT typical_new_price FROM products").fetchone()
    assert row["typical_new_price"] == 350  # untouched, not reset to normal_price


def test_repeat_connect_never_reruns_the_backfill_update(tmp_path):
    """Regression test for the actual bug: this UPDATE used to run
    unconditionally on every connect(), taking a write lock on every single
    web request and every watch tick regardless of whether it matched any
    rows — enough concurrent connections colliding on that lock is what
    produced "database is locked". A zero-row UPDATE still takes the lock,
    so checking total_changes wouldn't catch this — trace the actual SQL
    executed instead."""
    path = tmp_path / "t.db"
    db.connect(path).close()  # first connect: column added, backfill runs once

    executed = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(executed.append)
        return conn

    with mock.patch("product_finder.db.sqlite3.connect", side_effect=spy_connect):
        db.connect(path).close()

    assert not any("typical_new_price = normal_price" in sql for sql in executed)
