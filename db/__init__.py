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
# Local development: unset -> the existing repo-local file, unchanged.
# Hosted staging:    DALTON_DB_PATH=/var/data/dalton_solar.db on the persistent
#                    disk, because the container filesystem is wiped on deploy.
DB_PATH = os.environ.get("DALTON_DB_PATH") or os.path.join(BASE_DIR, "dalton_solar.db")

# Several coworkers share one Gunicorn worker with 4 threads, so multiple
# threads hit SQLite concurrently.
#   WAL         readers do not block the writer (default DELETE mode blocks both)
#   busy_timeout wait for a held lock instead of failing instantly with
#               "database is locked"
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("DALTON_SQLITE_BUSY_TIMEOUT_MS", "5000"))


def _configure_connection(conn):
    """Applied to EVERY connection. WAL is a persistent database property, but
    busy_timeout and foreign_keys are per-connection and must be re-applied."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        # Some filesystems refuse WAL. Degrade rather than fail to boot.
        pass
    return conn
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_sqlite.sql")

_local = threading.local()


def get_db():
    """Return a connection for the current thread (Flask request), creating one if needed."""
    if not hasattr(_local, "conn"):
        # check_same_thread=False is safe here: each thread gets its OWN
        # connection via threading.local, so no connection is shared.
        conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _configure_connection(conn)
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
    if reset:
        # WAL mode keeps committed pages in a SIDECAR file. Deleting only the
        # .db leaves dalton_solar.db-wal behind, and SQLite REPLAYS it on the
        # next connect - resurrecting rows the reset was supposed to remove.
        # This surfaced as tests seeing stale document rows whose files had
        # been cleaned up. Remove the sidecars too.
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _configure_connection(conn)
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


def transaction():
    """Group several writes into ONE all-or-nothing unit.

        with transaction() as tx:
            tx.execute(...)
            tx.execute(...)

    execute() above commits on EVERY call, so two consecutive execute() calls
    are not atomic: if the second fails, the first is already durable. Creating
    a rep writes both a `users` row and a `sales_reps` row, and a half-created
    rep (user with no rep row) would be unusable and invisible to rep listings.

    Commits on clean exit, rolls back on ANY exception, and re-raises so the
    caller can translate it into an HTTP response.
    """
    return _Transaction(get_db())


class _Transaction:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        # sqlite3 opens a transaction implicitly on the first write; make sure
        # nothing earlier in this connection is left pending.
        self._conn.commit()
        return self

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False   # never swallow the exception


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
