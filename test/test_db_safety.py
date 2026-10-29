"""
UBS C0.5 - DATABASE SAFETY.

The application database holds live enrollment and sales records. One function
can erase it - init_db(reset=True), which unlinks the file and its WAL
sidecars - and 34 suites call it to get a clean fixture. Twenty-seven of them
did not repoint db.DB_PATH first, so a full `pytest` run deleted the real
database. That happened during Phase C.

Four independent protections now stand between a test run and that file, and
this suite proves each of them separately, because a defence that only works
when the others do is one defence, not four:

  1. db.assert_reset_allowed() REFUSES a destructive reset of the application
     database, and refuses unconditionally on a hosting platform.
  2. conftest.py repoints db.DB_PATH at a temp file for the whole pytest
     session, and fails any test that leaves it pointing at the real file.
  3. test/_ubs_test_db.py does the same for a suite run directly with python,
     where conftest never loads.
  4. Startup and schema initialisation are non-destructive: they create what
     is missing and preserve every existing row.

Every check here runs against TEMPORARY databases. This suite never opens the
application database, and asserts as much about itself at the end.

    python test/test_db_safety.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _ubs_test_db  # noqa: F401,E402  - throwaway database before app import
import db            # noqa: E402

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def _fresh_db(dirpath, name="scratch.db"):
    """A real, populated SQLite database standing in for the application's."""
    p = os.path.join(dirpath, name)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE enrollments (id INTEGER PRIMARY KEY, code TEXT)")
    con.executemany("INSERT INTO enrollments (code) VALUES (?)",
                    [("ENR-1",), ("ENR-2",), ("ENR-3",)])
    con.commit()
    con.close()
    return p


def _rows(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
    finally:
        con.close()


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. THE RESET GUARD REFUSES THE APPLICATION DATABASE")

    check("the real database path is captured at import, not read live",
          isinstance(db.APP_DB_PATH, str) and db.APP_DB_PATH)
    check("  ...and this test run is NOT pointed at it",
          not db.is_application_database(db.DB_PATH))
    check("  ...it is pointed at a temporary file",
          os.path.abspath(db.DB_PATH) != os.path.abspath(db.APP_DB_PATH))

    # The guard is exercised directly, against the REAL path, without ever
    # creating or touching that file.
    raised = None
    try:
        db.assert_reset_allowed(db.APP_DB_PATH)
    except db.DatabaseResetRefused as exc:
        raised = exc
    check("resetting the application database is REFUSED", raised is not None)
    check("  ...and the refusal explains what is at stake",
          "enrollment" in str(raised).lower() and "sales" in str(raised).lower())
    check("  ...and names both ways forward",
          "_ubs_test_db" in str(raised) and db.RESET_OPT_IN_ENV in str(raised))

    check("a scratch path is allowed without ceremony",
          db.assert_reset_allowed(os.path.join(tempfile.gettempdir(),
                                               "ubs-not-real.db")) is None)

    section("1b. THE GUARD FAILS CLOSED")
    # A path it cannot reason about must be treated as the real database, not
    # waved through.
    check("an unusable path is treated as the real database",
          db.is_application_database(None) is True)
    # Relative vs absolute vs symlinked spellings of the same file are the
    # same file.
    rel = os.path.relpath(db.APP_DB_PATH, os.getcwd()) \
        if os.path.splitdrive(db.APP_DB_PATH)[0] == os.path.splitdrive(os.getcwd())[0] \
        else db.APP_DB_PATH
    check("a relative spelling of the real path is still recognised",
          db.is_application_database(rel) is True)

    section("1c. A HOSTING PLATFORM CANNOT BE OVERRIDDEN")
    prior = {k: os.environ.get(k) for k in ("RENDER", db.RESET_OPT_IN_ENV)}
    try:
        os.environ["RENDER"] = "true"
        os.environ[db.RESET_OPT_IN_ENV] = "1"      # even WITH the opt-in
        server_refusal = None
        try:
            db.assert_reset_allowed(db.APP_DB_PATH)
        except db.DatabaseResetRefused as exc:
            server_refusal = exc
        check("on a deployed service the opt-in does not unlock a reset",
              server_refusal is not None)
        check("  ...and the refusal says why",
              "hosting platform" in str(server_refusal))
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    section("1d. AN EXPLICIT HUMAN OPT-IN STILL WORKS ON A DEV MACHINE")
    prior_optin = os.environ.get(db.RESET_OPT_IN_ENV)
    try:
        os.environ[db.RESET_OPT_IN_ENV] = "1"
        allowed = True
        try:
            db.assert_reset_allowed(db.APP_DB_PATH)
        except db.DatabaseResetRefused:
            allowed = False
        check("a developer who asks for it explicitly is allowed", allowed)
    finally:
        if prior_optin is None:
            os.environ.pop(db.RESET_OPT_IN_ENV, None)
        else:
            os.environ[db.RESET_OPT_IN_ENV] = prior_optin

    # ═══════════════════════════════════════════════════════════════════
    section("2. init_db(reset=True) CANNOT WIPE THE APPLICATION DATABASE")
    # Driven end to end: a populated stand-in is installed AS the application
    # database, and a reset is attempted exactly as a careless suite would.
    with tempfile.TemporaryDirectory(prefix="ubs-safety-") as tmp:
        standin = _fresh_db(tmp, "pretend_app.db")
        before = _rows(standin)
        saved_app, saved_cur = db.APP_DB_PATH, db.DB_PATH
        try:
            db.APP_DB_PATH = standin      # this file is now "the real one"
            db.DB_PATH = standin          # ...and a suite forgot to repoint
            refused = None
            try:
                db.init_db(reset=True)
            except db.DatabaseResetRefused as exc:
                refused = exc
            check("the reset is refused", refused is not None)
            check("  ...the file still exists", os.path.exists(standin))
            check(f"  ...and still holds its rows ({before})",
                  _rows(standin) == before == 3)
        finally:
            db.APP_DB_PATH, db.DB_PATH = saved_app, saved_cur

    section("2b. A RESET OF A SCRATCH DATABASE STILL WORKS")
    with tempfile.TemporaryDirectory(prefix="ubs-safety-") as tmp:
        saved_cur = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "scratch.db")
            db.init_db(reset=True)
            con = sqlite3.connect(db.DB_PATH)
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            con.close()
            check("a throwaway database is created and reset normally",
                  "enrollments" in tables and "customers" in tables)
        finally:
            db.DB_PATH = saved_cur

    # ═══════════════════════════════════════════════════════════════════
    section("3. SCHEMA INITIALISATION PRESERVES EXISTING RECORDS")
    # The requirement that matters on startup: an installation with real
    # enrollments must keep every one of them.
    with tempfile.TemporaryDirectory(prefix="ubs-safety-") as tmp:
        saved_cur = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "existing.db")
            db.init_db(reset=True)                      # a real schema
            db.execute("INSERT INTO users (email, password_hash, role, full_name) "
                       "VALUES (?,?,?,?)", ("keep@me.com", "x", "admin", "Keep Me"))
            db.close_db()
            con = sqlite3.connect(db.DB_PATH)
            kept_before = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            con.close()

            db.init_db()                                # NON-destructive re-init
            con = sqlite3.connect(db.DB_PATH)
            kept_after = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            still_there = con.execute(
                "SELECT full_name FROM users WHERE email='keep@me.com'").fetchone()
            con.close()
            check(f"init_db() over an existing database keeps its rows "
                  f"({kept_before} -> {kept_after})", kept_after == kept_before == 1)
            check("  ...the exact record survives", still_there
                  and still_there[0] == "Keep Me")
            check("  ...and it did not raise on the existing schema", True)
        finally:
            db.close_db()
            db.DB_PATH = saved_cur

    # ═══════════════════════════════════════════════════════════════════
    section("4. STARTUP NEVER RESETS")
    APPPY = (ROOT / "app.py").read_text(encoding="utf-8")
    check("create_app() calls init_db with no arguments",
          "init_db()" in APPPY)
    check("  ...and never with reset", "init_db(reset" not in APPPY)
    check("  ...only when there is no database yet",
          "if not os.path.exists(DB_PATH):" in APPPY)
    check("  ...otherwise it runs additive migrations",
          "run_migrations(DB_PATH, verbose=False)" in APPPY)
    check("the migration runner is additive, with a ledger",
          "CREATE TABLE IF NOT EXISTS schema_migrations"
          in (ROOT / "db" / "migrate.py").read_text(encoding="utf-8"))
    SCHEMA = (ROOT / "db" / "schema_sqlite.sql").read_text(encoding="utf-8")
    check("the schema file drops nothing",
          "DROP TABLE" not in SCHEMA.upper() and "TRUNCATE" not in SCHEMA.upper())

    # ═══════════════════════════════════════════════════════════════════
    section("5. NO SUITE CAN REACH THE APPLICATION DATABASE")
    suites = sorted(p for p in (ROOT / "test").glob("*.py")
                    if p.name not in ("_ubs_test_db.py",))
    resetting, unprotected = [], []
    for p in suites:
        s = p.read_text(encoding="utf-8")
        if "init_db(reset=True)" not in s:
            continue
        resetting.append(p.name)
        if "_ubs_test_db" not in s and "db.DB_PATH" not in s:
            unprotected.append(p.name)
    check(f"{len(resetting)} suites perform a destructive reset", len(resetting) >= 30)
    check(f"every one of them is isolated first ({len(unprotected)} unprotected)",
          not unprotected)

    CONF = (ROOT / "conftest.py").read_text(encoding="utf-8")
    check("pytest isolates the database for the whole session",
          "scope=\"session\", autouse=True" in CONF and "tempfile.mkdtemp" in CONF)
    check("  ...and fails any test that repoints it at the real file",
          "is_application_database(db.DB_PATH)" in CONF)
    check("the shared isolation module exists for direct `python test/x.py` runs",
          (ROOT / "test" / "_ubs_test_db.py").exists())
    check("  ...and redirects the file roots as well as the database",
          "helpers.BACKEND_ROOT" in (ROOT / "test" / "_ubs_test_db.py").read_text(encoding="utf-8"))

    section("5b. SEEDING CANNOT SILENTLY ERASE A REAL INSTALLATION")
    SEED = (ROOT / "seed.py").read_text(encoding="utf-8")
    check("seed.py refuses the application database by default",
          "is_application_database(_db.DB_PATH) and not args.force" in SEED)
    check("  ...and says what would be lost",
          "every enrollment, customer and sales record" in SEED)
    check("  ...with --force as the explicit way through",
          '"--force"' in SEED and "RESET_OPT_IN_ENV] = \"1\"" in SEED)

    # Proved by running it: a bare `python seed.py` against a stand-in
    # application database must exit non-zero and change nothing.
    with tempfile.TemporaryDirectory(prefix="ubs-safety-") as tmp:
        standin = _fresh_db(tmp, "seed_target.db")
        before = _rows(standin)
        env = dict(os.environ)
        env["DALTON_DB_PATH"] = standin          # now THE application database
        env.pop(db.RESET_OPT_IN_ENV, None)
        proc = subprocess.run([sys.executable, str(ROOT / "seed.py")],
                              capture_output=True, text=True, env=env, timeout=120)
        check(f"`python seed.py` exits non-zero against a real database "
              f"(rc={proc.returncode})", proc.returncode != 0)
        check("  ...and says it is refusing", "Refusing to seed" in proc.stdout)
        check(f"  ...and the records are untouched ({before} rows)",
              _rows(standin) == before == 3)

    # ═══════════════════════════════════════════════════════════════════
    section("6. THIS SUITE NEVER TOUCHED THE APPLICATION DATABASE")
    check("db.DB_PATH is still a temporary file",
          not db.is_application_database(db.DB_PATH))
    check("  ...and the recorded application path was never opened as this run's db",
          os.path.abspath(db.DB_PATH) != os.path.abspath(db.APP_DB_PATH))

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
