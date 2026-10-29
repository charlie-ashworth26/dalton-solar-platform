"""Point this process's database at a throwaway file. Import me first.

WHY THIS EXISTS

    init_db(reset=True) deletes the database file it is pointed at. Every suite
    here calls it to get a clean fixture. A suite that has not repointed
    db.DB_PATH first therefore deletes the REAL application database - the one
    holding live enrollment and sales records - and a full `pytest` run did
    exactly that during Phase C.

    Seven suites repointed DB_PATH by hand and twenty-seven did not. Rather
    than twenty-seven slightly different fixtures, this module does the one
    correct thing once:

        import _ubs_test_db  # noqa: F401  - isolates this run's database

    Import it BEFORE `from app import app`. app.py builds the Flask app at
    import time and that reads db.DB_PATH, so a later redirect would arrive
    after the application had already bound to the real file.

WHAT IT DOES

    * db.DB_PATH            -> a fresh file in a per-process temp directory
    * helpers.BACKEND_ROOT  -> that same directory, so uploaded files, storage
      helpers.DATA_ROOT        and generated packages land in the sandbox too

    It is idempotent: a suite that already redirected DB_PATH itself keeps its
    own choice, and importing this module twice does nothing the second time.

    The temp directory is deliberately NOT deleted at exit. A failed suite is
    usually worth inspecting, the directory is small, and the operating system
    clears it. Nothing in it is ever the real database.

WHAT IT DOES NOT DO

    It does not grant permission to reset the real database. db.assert_reset_allowed()
    refuses that regardless of what this module did, which is why the two
    protections are independent: this one makes the common path safe, and that
    one makes the unsafe path impossible.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db           # noqa: E402
import helpers      # noqa: E402

#: The sandbox this process is using, exported so a suite can assert on it.
TEST_DB_DIR = None
TEST_DB_PATH = None


def isolate():
    """Repoint the database and the file roots at a temp sandbox. Idempotent."""
    global TEST_DB_DIR, TEST_DB_PATH
    if TEST_DB_PATH is not None:
        return TEST_DB_PATH
    if not db.is_application_database(db.DB_PATH):
        # A suite that already made its own sandbox keeps it; recording the
        # path here means the conftest leak-check still sees an isolated run.
        TEST_DB_PATH = db.DB_PATH
        TEST_DB_DIR = os.path.dirname(os.path.abspath(db.DB_PATH))
        return TEST_DB_PATH

    TEST_DB_DIR = tempfile.mkdtemp(prefix="ubs-test-db-")
    TEST_DB_PATH = os.path.join(TEST_DB_DIR, "ubs_test.db")
    db.DB_PATH = TEST_DB_PATH
    # Uploads, generated packages and storage all hang off these two roots.
    # Leaving them pointed at the repo would scatter test artefacts through a
    # real installation even though the database itself was safe.
    helpers.BACKEND_ROOT = TEST_DB_DIR
    helpers.DATA_ROOT = TEST_DB_DIR
    return TEST_DB_PATH


isolate()
