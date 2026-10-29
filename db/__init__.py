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

# ─────────────────────── DESTRUCTIVE-RESET PROTECTION ───────────────────────
#
# THE INCIDENT THIS PREVENTS. init_db(reset=True) deletes the database file and
# its WAL sidecars outright. Test suites call it to get a clean fixture, and a
# suite that forgets to repoint DB_PATH first deletes the REAL application
# database instead - the one holding live enrollment and sales records. That is
# not hypothetical: a full `pytest` run did exactly this during the Phase C
# work, and 27 of the 34 suites that reset were relying on nothing but habit.
#
# APP_DB_PATH is captured HERE, at import, before any test module can reassign
# DB_PATH. It is therefore a stable record of "the database this installation
# actually serves", and the guard below compares against it rather than against
# whatever DB_PATH happens to point at by the time a reset is attempted.
APP_DB_PATH = DB_PATH

#: Set to "1" to permit a destructive reset of the APPLICATION database. It is
#: deliberately awkward: a human typing it is making a decision, which is the
#: only context in which erasing real records is legitimate. It has NO effect
#: on a scratch/temp database (those never needed permission) and NO effect on
#: a server deployment (see _looks_like_server_deployment).
RESET_OPT_IN_ENV = "UBS_ALLOW_DB_RESET"

#: Environment markers set by hosting platforms. Their presence means this
#: process is serving, not developing, and no opt-in can unlock a reset.
_SERVER_ENV_MARKERS = ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID",
                       "DYNO", "KUBERNETES_SERVICE_HOST", "WEBSITE_INSTANCE_ID")


class DatabaseResetRefused(RuntimeError):
    """Raised instead of deleting a database that holds real records."""


def _looks_like_server_deployment():
    return any(os.environ.get(v) for v in _SERVER_ENV_MARKERS)


def is_application_database(path):
    """True when `path` is the database this installation serves."""
    try:
        return os.path.realpath(os.path.abspath(path)) == \
               os.path.realpath(os.path.abspath(APP_DB_PATH))
    except Exception:
        # FAIL CLOSED. If the comparison itself cannot be made, treat the
        # target as the real database rather than assuming it is scratch.
        return True


def assert_reset_allowed(path):
    """Permit a destructive reset, or refuse with an explanation.

    FAIL-CLOSED by construction: the only path that returns quietly is one
    proved NOT to be the application database, or an explicit human opt-in on a
    developer machine. Everything else raises.
    """
    target = os.path.abspath(path)
    if not is_application_database(target):
        return                      # scratch or temp file: nothing real to lose
    if _looks_like_server_deployment():
        raise DatabaseResetRefused(
            f"Refusing to reset the application database at {target}: this "
            "process is running on a hosting platform. A destructive reset is "
            "never performed on a deployed service.")
    if os.environ.get(RESET_OPT_IN_ENV) == "1":
        return                      # a human asked for this, on their own machine
    raise DatabaseResetRefused(
        f"Refusing to erase the application database at {target}.\n"
        "This file holds live enrollment and sales records.\n"
        "  * Tests must point db.DB_PATH at a temporary file first - "
        "import test/_ubs_test_db.py, which does it for you.\n"
        f"  * To reset your own development database on purpose, set "
        f"{RESET_OPT_IN_ENV}=1 for that one command.")

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


def _has_schema(conn):
    """True when this database already carries the application's tables."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='enrollments'").fetchone()
    return bool(row and row[0])


def init_db(reset=False):
    """Create the database from schema_sqlite.sql, then apply any pending
    migrations from db/migrations/. If reset=True, drop and recreate first."""
    # RELEASE THIS THREAD'S CONNECTION FIRST.
    #
    # get_db() caches a connection on threading.local. Flask closes it via
    # teardown_appcontext after every request, so a SERVING process never holds
    # one open. But any query made OUTSIDE an app context - which is how the
    # test suites read the database directly - creates a connection that no
    # teardown ever closes.
    #
    # On POSIX an open handle does not block unlink, so this was invisible.
    # On Windows os.remove() then fails with WinError 32 (file in use), and WAL
    # makes it worse by holding -wal and -shm open as well. The first suite to
    # leave a bare-call connection open broke every later suite that resets.
    #
    # Closing here is the correct lifecycle, not a workaround: after a reset the
    # cached connection points at a DELETED file, so continuing to use it would
    # be a bug in its own right.
    close_db()

    if reset:
        # THE GUARD COMES FIRST, before anything is unlinked. It raises rather
        # than returning a value, so there is no path on which a refusal is
        # ignored and the deletion below runs anyway.
        assert_reset_allowed(DB_PATH)
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
    # EXISTING DATA IS NEVER RE-SCHEMA'D.
    #
    # schema_sqlite.sql is 18 bare CREATE TABLE statements, so running it over
    # a populated database raises partway through ("table already exists"),
    # leaving the script half-applied. Detecting the populated case and going
    # straight to migrations makes startup idempotent AND data-preserving: an
    # installation with real enrollments receives new migrations and keeps
    # every row it had.
    if _has_schema(conn):
        conn.close()
    else:
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
