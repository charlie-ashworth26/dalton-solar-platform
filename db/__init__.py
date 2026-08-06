"""
Database access layer.

Everything in this module talks to SQLite for the prototype. The schema
(schema_sqlite.sql) is written so the same shape ports cleanly to Postgres
(schema_postgres.sql) — see README.md "Migrating to Postgres".
"""
import sqlite3
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "dalton_solar.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_sqlite.sql")

_local = threading.local()


def get_db():
    """Return a connection for the current thread (Flask request), creating one if needed."""
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return _local.conn


def close_db(exception=None):
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        del _local.conn


def init_db(reset=False):
    """Create the database from schema_sqlite.sql, then apply any pending
    migrations from db/migrations/. If reset=True, drop and recreate first."""
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    # Migrations are additive and idempotent — safe on both a fresh database
    # and an existing one with real enrollment data.
    from db.migrate import run_migrations
    run_migrations(DB_PATH, verbose=False)


def query(sql, params=()):
    """SELECT helper — returns a list of sqlite3.Row (dict-like)."""
    cur = get_db().execute(sql, params)
    return cur.fetchall()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """INSERT/UPDATE/DELETE helper — commits and returns the cursor (for lastrowid)."""
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
