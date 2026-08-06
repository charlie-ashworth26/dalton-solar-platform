"""
Migration runner.

Phase 1 initialized the database by running schema_sqlite.sql fresh, which is
destructive — fine when there was no real data, not acceptable now. This adds
an additive migration ledger: each file in db/migrations/ runs exactly once,
in filename order, and is recorded in schema_migrations.

Postgres note: the runner itself is dialect-agnostic (it just executes SQL
files and records filenames). Only the migration SQL is dialect-specific.
When migrating to Postgres, point MIGRATIONS_DIR at a postgres/ subdirectory
or maintain per-dialect variants — the ledger mechanism is unchanged.
"""
import os
import sqlite3

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def applied_migrations(conn):
    conn.execute(LEDGER_DDL)
    conn.commit()
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def pending_migrations(conn):
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    done = applied_migrations(conn)
    all_files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    return [f for f in all_files if f not in done]


def run_migrations(db_path, verbose=True):
    """Apply every pending migration in filename order. Safe to call repeatedly.

    Foreign keys are disabled for the duration of each migration and re-enabled
    afterwards. This is standard practice for schema migrations: SQLite cannot
    drop a column in place, so structural changes use the documented
    create-copy-drop-rename rebuild, and that rebuild is impossible with FK
    enforcement on. PRAGMA foreign_key_check runs after each migration so a
    genuine integrity violation still fails loudly rather than passing silently.
    """
    conn = sqlite3.connect(db_path)
    pending = pending_migrations(conn)
    if not pending:
        if verbose:
            print("No pending migrations.")
        conn.close()
        return []

    for filename in pending:
        path = os.path.join(MIGRATIONS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (filename,))
            conn.commit()

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                conn.rollback()
                conn.close()
                raise RuntimeError(
                    f"Migration {filename} left {len(violations)} foreign-key violation(s): "
                    f"{violations[:5]}"
                )
            conn.execute("PRAGMA foreign_keys = ON")
            if verbose:
                print(f"  applied {filename}")
        except Exception as e:
            conn.rollback()
            conn.close()
            raise RuntimeError(f"Migration {filename} failed: {e}") from e

    conn.close()
    return pending


if __name__ == "__main__":
    from db import DB_PATH
    print(f"Running migrations against {DB_PATH}")
    applied = run_migrations(DB_PATH)
    print(f"Done. {len(applied)} migration(s) applied.")
