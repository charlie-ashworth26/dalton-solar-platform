"""pytest configuration.

Puts the backend root on sys.path so test modules can `from app import app`
regardless of the working directory pytest is invoked from. Without this,
`python -m pytest` from the repo root fails to import the application package.

IT ALSO ISOLATES THE DATABASE FOR THE WHOLE SESSION.

    The suites in test/ call init_db(reset=True) to get a clean fixture, and
    that deletes whatever db.DB_PATH points at. Pointed at the application
    database, it deletes live enrollment and sales records - which a full
    `pytest` run did during Phase C, before this fixture existed.

    The fixture below runs before any suite module is imported (pytest.ini
    collects only test_suites.py, and every suite is imported inside a test
    function), so by the time a suite's module-level init_db(reset=True) runs,
    DB_PATH already points into a temp directory.

    This is one of two independent protections. The other lives in
    db.assert_reset_allowed() and refuses a destructive reset of the real
    database outright. This fixture makes the normal path safe; that guard
    makes the unsafe path impossible. Neither relies on the other.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import helpers     # noqa: E402

#: Recorded so the leak check and the safety suite can assert against it.
APP_DB_PATH_AT_START = db.APP_DB_PATH


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    """Every test in this session uses a throwaway database.

    Session-scoped and autouse: no suite has to remember, and no suite can opt
    out. The real database is never opened, let alone deleted.
    """
    sandbox = tempfile.mkdtemp(prefix="ubs-pytest-db-")
    original = (db.DB_PATH, helpers.BACKEND_ROOT, helpers.DATA_ROOT)
    db.DB_PATH = os.path.join(sandbox, "ubs_pytest.db")
    helpers.BACKEND_ROOT = sandbox
    helpers.DATA_ROOT = sandbox
    try:
        yield db.DB_PATH
    finally:
        db.DB_PATH, helpers.BACKEND_ROOT, helpers.DATA_ROOT = original


@pytest.fixture(autouse=True)
def _database_stayed_isolated(isolated_database):
    """Fail the test that repoints the database at the real file.

    A suite is free to make its OWN sandbox - several do - so this asserts the
    property that matters rather than one specific path: whatever DB_PATH ends
    up as, it must not be the application database.
    """
    yield
    assert not db.is_application_database(db.DB_PATH), (
        "a test left db.DB_PATH pointing at the APPLICATION database "
        f"({db.DB_PATH}); it must use a temporary file"
    )
