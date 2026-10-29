"""
UBS C0.6 - BACKUP AND RECOVERY.

C0.5 proved the database cannot be erased by accident. This proves it can be
got back when something that is not an accident happens: a disk failure, a bad
migration, a wrong DELETE run against the right database.

The properties that matter, and that this suite holds to:

  1. A backup NEVER modifies the database it is backing up - proved by hash,
     size and mtime, including while a writer holds the database open with
     committed rows still sitting in the WAL.
  2. The backup captures those WAL-resident rows. A plain file copy does not,
     and this suite demonstrates the difference rather than asserting it.
  3. Every backup is verified with PRAGMA integrity_check before it is allowed
     to become a backup, and a file that fails is never given a backup's name.
  4. Retention keeps a sensible set and, whatever it is asked to do, always
     leaves the newest backup in place and can never reach a live database.
  5. A restore cannot overwrite the application database, with no flag to
     permit it.
  6. Nothing runs automatically.

Everything here runs against TEMPORARY databases built in temp directories.
The application database is never opened, and the suite asserts that about
itself at the end.

    python test/test_db_backup.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _ubs_test_db  # noqa: F401,E402  - throwaway database before app import
import db            # noqa: E402
from db import backup as bk  # noqa: E402

_results = []
_tmpdirs = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def tmpdir(prefix="ubs-backup-test-"):
    d = tempfile.mkdtemp(prefix=prefix)
    _tmpdirs.append(d)
    return d


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path):
    return (sha256(path), os.path.getsize(path), os.path.getmtime(path))


def make_db(path, customers=3, enrollments=2):
    """A small but REAL database: two tables with a foreign key between them,
    so PRAGMA foreign_key_check has something to check."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)")
    con.execute("CREATE TABLE enrollments (id INTEGER PRIMARY KEY, code TEXT, "
                "customer_id INTEGER REFERENCES customers(id))")
    con.executemany("INSERT INTO customers (email) VALUES (?)",
                    [(f"c{i}@x.com",) for i in range(customers)])
    con.executemany("INSERT INTO enrollments (code, customer_id) VALUES (?,?)",
                    [(f"ENR-{i:04d}", 1) for i in range(enrollments)])
    con.commit()
    con.close()
    return path


def counts(path):
    con = sqlite3.connect(bk._ro_uri(path), uri=True)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("customers", "enrollments")}
    finally:
        con.close()


def synth_backup(backup_dir, when, rows=1):
    """A real (tiny) database filed under a chosen timestamp, for retention
    tests that must span weeks without waiting weeks."""
    os.makedirs(backup_dir, exist_ok=True)
    name = f"dalton_solar-{when.strftime(bk._STAMP_FORMAT)}.db"
    path = os.path.join(backup_dir, name)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    con.executemany("INSERT INTO t (id) VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()
    with open(path[:-3] + ".json", "w", encoding="utf-8") as f:
        json.dump({"backup": name}, f)
    return path


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. A BACKUP NEVER MODIFIES THE SOURCE")
    d = tmpdir()
    live = make_db(os.path.join(d, "live", "dalton_solar.db"))
    backups = os.path.join(d, "backups")
    before = fingerprint(live)

    result = bk.create_backup(source=live, backup_dir=backups, prune_after=False)
    after = fingerprint(live)

    check("source sha256 is unchanged", before[0] == after[0])
    check("source size is unchanged", before[1] == after[1])
    check("source mtime is unchanged", before[2] == after[2])
    check("the backup exists", os.path.exists(result["path"]))
    check("  ...and is a different file from the source",
          os.path.abspath(result["path"]) != os.path.abspath(live))

    section("1b. ...EVEN WITH A WRITER HOLDING IT OPEN, MID-WAL")
    # The case a naive implementation gets wrong. 200 rows are committed but
    # still live in the -wal, not in the .db file.
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executemany("INSERT INTO customers (email) VALUES (?)",
                       [(f"wal{i}@x.com",) for i in range(200)])
    writer.commit()
    expected = writer.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    wal_size = os.path.getsize(live + "-wal") if os.path.exists(live + "-wal") else 0
    check(f"the WAL holds committed pages ({wal_size} bytes)", wal_size > 0)

    before = fingerprint(live)
    live_backup = bk.create_backup(source=live, backup_dir=backups,
                                   prune_after=False)
    after = fingerprint(live)
    check("source sha256 is unchanged while a writer holds it", before[0] == after[0])
    check("source mtime is unchanged", before[2] == after[2])
    check(f"the backup captured every committed row ({expected})",
          counts(live_backup["path"])["customers"] == expected)

    # The contrast, demonstrated rather than claimed.
    naive = os.path.join(d, "naive-copy.db")
    shutil.copyfile(live, naive)
    naive_customers = counts(naive)["customers"]
    check(f"a plain file copy would have missed them ({naive_customers} of "
          f"{expected})", naive_customers < expected)
    writer.close()

    section("1c. THE BACKUP IS A USABLE DATABASE, NOT JUST BYTES")
    con = sqlite3.connect(live_backup["path"])
    rows = con.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
    joined = con.execute("SELECT c.email FROM enrollments e "
                         "JOIN customers c ON c.id = e.customer_id "
                         "LIMIT 1").fetchone()
    con.close()
    check("it can be opened and queried", rows == 2)
    check("  ...and its foreign keys still resolve", joined is not None)

    # ═══════════════════════════════════════════════════════════════════
    section("2. EVERY BACKUP IS VERIFIED BEFORE IT COUNTS AS ONE")
    m = live_backup["manifest"]
    check("integrity_check ran and returned ok", m["integrity_check"] == ["ok"])
    check("foreign_key_check ran and found nothing",
          m["foreign_key_violations"] == 0)
    check("the manifest records a sha256", len(m["sha256"]) == 64)
    check("  ...matching the file on disk", m["sha256"] == sha256(live_backup["path"]))
    check("the manifest records the source path", m["source_path"] == os.path.abspath(live))
    check("the manifest records row counts", m["row_counts"]["customers"] == expected)
    check("a manifest file sits beside the backup",
          os.path.exists(bk._manifest_path(live_backup["path"])))

    section("2a. A STORED BACKUP IS ONE FILE PLUS ITS MANIFEST")
    # Opening a WAL database creates -wal/-shm, so merely VERIFYING a backup
    # leaves two files behind - including a pair named after the temporary
    # .part file, which is then renamed away and leaves orphans nothing would
    # ever clean up. One pair per backup, accumulating forever, next to the
    # thing they could be replayed into.
    listing = sorted(os.listdir(backups))
    check(f"no leftovers in the backup directory ({listing})",
          all(f.endswith((".db", ".json")) for f in listing))
    check("  ...no -wal or -shm beside a stored backup",
          not any(f.endswith(("-wal", "-shm")) for f in listing))
    check("  ...and nothing named after the temporary .part file",
          not any(".part" in f for f in listing))
    bk.verify_backup(live_backup["path"])
    check("  ...still true after verifying one again",
          sorted(os.listdir(backups)) == listing)

    section("2b. A CORRUPT BACKUP IS REPORTED AS CORRUPT")
    corrupt = os.path.join(d, "corrupt.db")
    shutil.copyfile(live_backup["path"], corrupt)
    with open(corrupt, "r+b") as f:
        f.seek(4096)                       # past the header, into page data
        f.write(b"\xde\xad\xbe\xef" * 512)
    v = bk.verify_backup(corrupt)
    check("integrity_check rejects a damaged file", v["ok"] is False)
    check("  ...and says what was wrong", bool(v["error"]))

    section("2c. A FILE THAT FAILS VERIFICATION NEVER BECOMES A BACKUP")
    # The source is made unreadable partway through by truncating it, so the
    # copy cannot be a valid database.
    broken_dir = os.path.join(d, "broken")
    broken = make_db(os.path.join(broken_dir, "src.db"))
    with open(broken, "r+b") as f:
        f.truncate(2048)                   # a torn, half-page database
    out = os.path.join(d, "broken-backups")
    raised = None
    try:
        bk.create_backup(source=broken, backup_dir=out, prune_after=False)
    except Exception as exc:               # BackupError or sqlite3.DatabaseError
        raised = exc
    check("the backup attempt fails loudly", raised is not None)
    survivors = [f for f in os.listdir(out)] if os.path.isdir(out) else []
    promoted = [f for f in survivors if bk._NAME_RE.match(f)]
    check(f"no backup-named file was left behind ({survivors})", not promoted)

    section("2d. THE VERIFICATION GATE IS WIRED IN, NOT JUST AVAILABLE")
    # 2c proves a broken SOURCE fails. It does not prove that a backup which
    # copied cleanly but verifies BADLY is rejected, because the backup API
    # does not produce such a file on demand. Forcing verify_backup to report
    # failure tests the gate itself: the raise, and the refusal to promote.
    gate_dir = os.path.join(d, "gate")
    real_verify = bk.verify_backup
    try:
        bk.verify_backup = lambda path: {
            "path": path, "ok": False, "error": "forced failure for this test",
            "integrity_check": ["*** in database main"],
            "foreign_key_violations": 0, "tables": 0, "row_counts": {}}
        raised = None
        try:
            bk.create_backup(source=live, backup_dir=gate_dir, prune_after=False)
        except bk.BackupError as exc:
            raised = exc
        check("a backup that fails verification raises", raised is not None)
        check("  ...saying it was not kept", "NOT kept" in str(raised))
        present = os.listdir(gate_dir) if os.path.isdir(gate_dir) else []
        check(f"  ...and no backup-named file appears ({present})",
              not any(bk._NAME_RE.match(f) for f in present))
        check("  ...the evidence is left behind as .FAILED",
              any(f.endswith(".FAILED") for f in present))
    finally:
        bk.verify_backup = real_verify

    section("2e. ROW COUNTS MUST MATCH THE SOURCE")
    # A copy can pass integrity_check and still be the wrong database.
    count_dir = os.path.join(d, "counts")
    real_counts = bk._table_counts
    try:
        # Only the SOURCE reading is falsified - create_backup counts the
        # source, verify_backup counts the copy, and the mismatch between them
        # is the thing under test.
        calls = {"n": 0}

        def _first_call_lies(conn):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"customers": 10 ** 9}
            return real_counts(conn)

        bk._table_counts = _first_call_lies
        raised = None
        try:
            bk.create_backup(source=live, backup_dir=count_dir, prune_after=False)
        except bk.BackupError as exc:
            raised = exc
        check("a row-count mismatch is caught", raised is not None)
        check("  ...and that file is not kept either",
              not any(bk._NAME_RE.match(f)
                      for f in (os.listdir(count_dir)
                                if os.path.isdir(count_dir) else [])))
    finally:
        bk._table_counts = real_counts

    section("2f. THE SOURCE CONNECTION PHYSICALLY CANNOT WRITE")
    # Not "we are careful not to write" - the connection is opened read-only,
    # so an attempted write fails at the SQLite level.
    work = tmpdir()
    src_conn, recovered = bk._open_source(live, work)
    try:
        denied = None
        try:
            src_conn.execute("INSERT INTO customers (email) VALUES ('no@x.com')")
            src_conn.commit()
        except sqlite3.OperationalError as exc:
            denied = exc
        check("writing through the backup's source connection is denied",
              denied is not None)
        check(f"  ...because the database is read-only ({denied})",
              "readonly" in str(denied).lower())
        check("  ...and no snapshot fallback was needed here", recovered is False)
    finally:
        src_conn.close()

    section("2g. THE CRASH-RECOVERY FALLBACK RECOVERS A COPY, NOT THE SOURCE")
    # When a -wal needs recovery, read-only cannot open it. The fallback copies
    # the triple aside and recovers THAT. Exercised directly, since a genuine
    # mid-crash -wal cannot be conjured reliably.
    snap_work = tmpdir()
    fp = fingerprint(live)
    snapshot = bk._snapshot_source(live, snap_work)
    check("the snapshot is a different file", os.path.abspath(snapshot)
          != os.path.abspath(live))
    check("  ...and the source is unchanged by taking it", fingerprint(live) == fp)
    sc = sqlite3.connect(snapshot)
    snap_rows = sc.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    sc.close()
    check(f"  ...and it carries the WAL-resident rows ({snap_rows})",
          snap_rows == expected)

    # ═══════════════════════════════════════════════════════════════════
    section("3. TIMESTAMPED, SORTABLE, AND SAFE ON WINDOWS")
    names = sorted(os.path.basename(b["path"]) for b in bk.list_backups(backups))
    check(f"backups are named dalton_solar-<UTC>.db ({len(names)} present)",
          all(bk._NAME_RE.match(n) for n in names))
    check("  ...with no colons, which Windows forbids in filenames",
          not any(":" in n for n in names))
    check("  ...and sort chronologically as plain strings",
          names == sorted(names) and
          [b["name"] for b in bk.list_backups(backups)] == sorted(names, reverse=True))

    stamped = bk.create_backup(source=live, backup_dir=backups, prune_after=False)
    again = bk.create_backup(source=live, backup_dir=backups, prune_after=False)
    check("two backups in the same second do not collide",
          stamped["path"] != again["path"] and os.path.exists(stamped["path"]))

    section("3b. THE READ-ONLY URI IS BUILT FOR WINDOWS PATHS TOO")
    # Str-concatenating a URI works on POSIX and breaks on "C:\...", which is
    # where this actually runs. Path.as_uri() is why it does not.
    uri = bk._ro_uri(live)
    check("the source URI is a file:// URI", uri.startswith("file://"))
    check("  ...with mode=ro", uri.endswith("?mode=ro"))
    check("  ...and no raw backslashes", "\\" not in uri)

    # ═══════════════════════════════════════════════════════════════════
    section("4. RETENTION: DAILY AND WEEKLY, NEWEST ALWAYS KEPT")
    rd = os.path.join(tmpdir(), "retention")
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    # Four today (an hourly schedule), then one a day back 40 days.
    for h in (1, 3, 6, 11):
        synth_backup(rd, now - timedelta(hours=h))
    for day in range(1, 41):
        synth_backup(rd, now - timedelta(days=day, hours=2))
    total = len(bk.list_backups(rd))
    check(f"{total} backups to choose from", total == 44)

    keep, delete = bk.select_for_retention(bk.list_backups(rd), now=now)
    kept_names = {os.path.basename(b["path"]) for b in keep}
    check(f"{len(keep)} kept, {len(delete)} dropped", len(keep) < total and delete)
    check("today's hourly backups are all kept",
          sum(1 for b in keep if (now - b["when"]) <= timedelta(hours=24)) == 4)
    newest = max(bk.list_backups(rd), key=lambda b: b["when"])
    check("the newest backup is kept", os.path.basename(newest["path"]) in kept_names)
    oldest = min(bk.list_backups(rd), key=lambda b: b["when"])
    check("a 40-day-old backup is not", os.path.basename(oldest["path"]) not in kept_names)
    days_covered = len({b["when"].date() for b in keep})
    check(f"at least a week of daily granularity ({days_covered} distinct days)",
          days_covered >= 7)
    weeks_covered = len({b["when"].isocalendar()[:2] for b in keep})
    check(f"and about a month of weekly ({weeks_covered} distinct ISO weeks)",
          weeks_covered >= 4)

    # A sidecar left beside a pruned backup would outlive it.
    doomed = delete[0]["path"]
    for suffix in ("-wal", "-shm"):
        with open(doomed + suffix, "wb") as f:
            f.write(b"")
    removed = bk.prune(rd, now=now)
    check(f"prune removed exactly those {len(delete)}", len(removed) == len(delete))
    check("  ...and their manifests went too",
          not any(os.path.exists(bk._manifest_path(p)) for p in removed))
    check("  ...and their sidecars did not survive them",
          not os.path.exists(doomed + "-wal") and not os.path.exists(doomed + "-shm"))
    check("  ...leaving the kept ones on disk", len(bk.list_backups(rd)) == len(keep))

    section("4b. RETENTION CAN NEVER LEAVE YOU WITH NOTHING")
    solo = os.path.join(tmpdir(), "solo")
    synth_backup(solo, now - timedelta(days=9999))
    check("even a single ancient backup survives pruning",
          not bk.prune(solo, keep_hours=0, keep_daily=0, keep_weekly=0, now=now)
          and len(bk.list_backups(solo)) == 1)

    section("4c. PRUNE CANNOT REACH ANYTHING THAT IS NOT A BACKUP")
    mixed = os.path.join(tmpdir(), "mixed")
    synth_backup(mixed, now - timedelta(days=500))
    synth_backup(mixed, now)
    for stray in ("dalton_solar.db", "notes.txt", "dalton_solar-nope.db",
                  "dalton_solar-20261027T120000Z.db.part"):
        with open(os.path.join(mixed, stray), "w") as f:
            f.write("not a backup managed by this tool")
    bk.prune(mixed, now=now)
    left = set(os.listdir(mixed))
    check("a live-looking dalton_solar.db is untouched", "dalton_solar.db" in left)
    check("an unrelated file is untouched", "notes.txt" in left)
    check("a malformed name is untouched", "dalton_solar-nope.db" in left)
    check("a .part file is untouched",
          "dalton_solar-20261027T120000Z.db.part" in left)

    section("4d. PRUNE REFUSES THE APPLICATION DATABASE EXPLICITLY")
    # Even configured to point at the application database's own directory,
    # with the application database named like a backup.
    trap = os.path.join(tmpdir(), "trap")
    os.makedirs(trap, exist_ok=True)
    decoy = synth_backup(trap, now - timedelta(days=900))
    synth_backup(trap, now)
    saved = db.APP_DB_PATH
    try:
        db.APP_DB_PATH = decoy             # the app database IS that old file
        kept_despite_age = bk.prune(trap, now=now)
        check("a file that is the application database is never deleted",
              decoy not in kept_despite_age and os.path.exists(decoy))
    finally:
        db.APP_DB_PATH = saved

    # ═══════════════════════════════════════════════════════════════════
    section("5. RESTORE CANNOT OVERWRITE THE APPLICATION DATABASE")
    saved = db.APP_DB_PATH
    try:
        db.APP_DB_PATH = live              # `live` is now "the real one"
        fp = fingerprint(live)
        refused = None
        try:
            bk.restore_backup(live_backup["path"], live, force=True)
        except bk.RestoreRefused as exc:
            refused = exc
        check("restoring onto it is refused", refused is not None)
        check("  ...even with force=True", refused is not None)
        check("  ...and the file is untouched", fingerprint(live) == fp)
        check("  ...the refusal points at the documented procedure",
              "BACKUP_AND_RECOVERY.md" in str(refused))
    finally:
        db.APP_DB_PATH = saved

    section("5b. RESTORE TO A NEW PATH WORKS AND IS VERIFIED")
    target = os.path.join(tmpdir(), "restored", "check.db")
    r = bk.restore_backup(live_backup["path"], target)
    check("the restored file exists", os.path.exists(target))
    check("  ...passes integrity_check", r["verified"]["integrity_check"] == ["ok"])
    check(f"  ...and holds every row ({expected} customers)",
          counts(target)["customers"] == expected)
    check("  ...byte-identical to the backup",
          sha256(target) == sha256(live_backup["path"]))

    section("5c. RESTORE REFUSES THE UNSAFE CASES")
    refused = None
    try:
        bk.restore_backup(live_backup["path"], target)      # exists, no force
    except bk.RestoreRefused as exc:
        refused = exc
    check("overwriting an existing target needs force", refused is not None)
    r2 = bk.restore_backup(live_backup["path"], target, force=True)
    check("  ...and works with it", r2["verified"]["ok"])

    refused = None
    try:
        bk.restore_backup(corrupt, os.path.join(tmpdir(), "from-corrupt.db"))
    except bk.RestoreRefused as exc:
        refused = exc
    check("restoring FROM a corrupt backup is refused", refused is not None)

    refused = None
    try:
        bk.restore_backup(os.path.join(d, "no-such-backup.db"),
                          os.path.join(tmpdir(), "x.db"))
    except bk.RestoreRefused as exc:
        refused = exc
    check("restoring from a missing file is refused", refused is not None)

    section("5d. STALE SIDECARS AT THE TARGET ARE CLEARED")
    # A -wal left from an earlier database at that path would be replayed over
    # the restored file, silently corrupting what was just recovered.
    t2 = os.path.join(tmpdir(), "sidecars", "db.db")
    os.makedirs(os.path.dirname(t2), exist_ok=True)
    for suffix in ("-wal", "-shm"):
        with open(t2 + suffix, "wb") as f:
            f.write(b"stale bytes from a previous life")
    bk.restore_backup(live_backup["path"], t2)
    check("a stale -wal is gone", not os.path.exists(t2 + "-wal"))
    check("a stale -shm is gone", not os.path.exists(t2 + "-shm"))
    check("  ...so the restored database is ONE file to move into place",
          sorted(os.listdir(os.path.dirname(t2))) == ["db.db"])
    check("  ...and none of the stale bytes reached it",
          counts(t2)["customers"] == expected)

    # ═══════════════════════════════════════════════════════════════════
    section("6. ENVIRONMENT: LOCAL, HOSTED STAGING, PRODUCTION")
    prior = {k: os.environ.get(k) for k in (bk.BACKUP_DIR_ENV, "RENDER")}
    try:
        os.environ.pop(bk.BACKUP_DIR_ENV, None)
        os.environ.pop("RENDER", None)
        default = bk.resolve_backup_dir()
        check("local default is <repo>/backups",
              default == os.path.join(db.BASE_DIR, "backups"))
        check("  ...which is NOT the data directory uploads live in",
              os.path.basename(default) == "backups")

        os.environ[bk.BACKUP_DIR_ENV] = "/var/backups/ubs"
        check(f"{bk.BACKUP_DIR_ENV} overrides it",
              bk.resolve_backup_dir() == os.path.abspath("/var/backups/ubs"))
        check("  ...and an explicit argument overrides that",
              bk.resolve_backup_dir("/elsewhere") == os.path.abspath("/elsewhere"))

        section("6b. HOSTED: SAME-DISK BACKUPS ARE REFUSED, NOT SILENTLY TAKEN")
        # Render mounts ONE persistent disk at /var/data and puts the database
        # on it. Backups written next to it do not survive losing that disk.
        hosted = tmpdir()
        hosted_db = make_db(os.path.join(hosted, "var-data", "dalton_solar.db"))
        same_disk = os.path.join(hosted, "var-data", "backups")
        os.environ["RENDER"] = "true"
        rep = bk.separation_report(hosted_db, same_disk)
        check("the same-disk condition is detected", rep["same_device"] is True)
        check("  ...and reported as not separate", rep["separate"] is False)
        check("  ...and the process knows it is deployed", rep["deployed"] is True)
        refused = None
        try:
            bk.create_backup(source=hosted_db, backup_dir=same_disk,
                             prune_after=False)
        except bk.BackupError as exc:
            refused = exc
        check("a deployed same-disk backup is refused", refused is not None)
        check("  ...and the message names the limitation",
              "losing the disk" in str(refused))
        check("  ...and offers the deliberate override",
              "--allow-same-disk" in str(refused))
        ok = bk.create_backup(source=hosted_db, backup_dir=same_disk,
                              allow_same_disk=True, prune_after=False)
        check("  ...which then works, recording the compromise in the manifest",
              ok["manifest"]["separation"]["separate"] is False)

        section("6c. LOCAL DEVELOPMENT IS NOT NAGGED")
        os.environ.pop("RENDER", None)
        local_db = make_db(os.path.join(tmpdir(), "repo", "dalton_solar.db"))
        local_out = os.path.join(os.path.dirname(local_db), "backups")
        fine = bk.create_backup(source=local_db, backup_dir=local_out,
                                prune_after=False)
        check("a developer backing up beside the repo is not blocked",
              os.path.exists(fine["path"]))
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    section("6d. PRODUCTION DEPLOYMENT USES THE SAME MECHANISM")
    RENDER = (ROOT / "render.yaml").read_text(encoding="utf-8")
    check("the blueprint still puts the database on the persistent disk",
          "DALTON_DB_PATH" in RENDER and "/var/data/dalton_solar.db" in RENDER)
    check("  ...and declares a backup directory",
          "DALTON_BACKUP_DIR" in RENDER)
    DOC = (ROOT / "BACKUP_AND_RECOVERY.md").read_text(encoding="utf-8")
    for topic in ("Local development", "Hosted staging", "Production"):
        check(f"the runbook covers: {topic}", topic in DOC)

    # ═══════════════════════════════════════════════════════════════════
    section("7. NOTHING RUNS AUTOMATICALLY")
    APPPY = (ROOT / "app.py").read_text(encoding="utf-8")
    check("app.py does not import the backup module", "backup" not in APPPY)
    check("create_app() takes no backup", "create_backup" not in APPPY)
    for mod in ("db/__init__.py", "db/migrate.py", "seed.py", "conftest.py"):
        src = (ROOT / mod).read_text(encoding="utf-8")
        check(f"{mod} never calls create_backup()", "create_backup(" not in src)
        check(f"{mod} never calls restore_backup()", "restore_backup(" not in src)
    BK = (ROOT / "db" / "backup.py").read_text(encoding="utf-8")
    body = BK.split('if __name__')[0]
    check("db/backup.py does no work at import time",
          "\ncreate_backup(" not in body and "\nprune(" not in body)

    # ═══════════════════════════════════════════════════════════════════
    section("8. THE C0.5 PROTECTIONS ARE STILL IN FORCE")
    check("the reset guard still exists", hasattr(db, "assert_reset_allowed"))
    check("  ...and still refuses the application database",
          _refuses(lambda: db.assert_reset_allowed(db.APP_DB_PATH)))
    check("  ...and still fails closed on an unusable path",
          db.is_application_database(None) is True)
    check("the backup module reuses that same identity check, rather than "
          "inventing its own", "is_application_database" in BK)
    check("seed.py still refuses to erase the application database",
          "is_application_database(_db.DB_PATH) and not args.force"
          in (ROOT / "seed.py").read_text(encoding="utf-8"))

    # ═══════════════════════════════════════════════════════════════════
    section("9. THE COMMANDS WORK END TO END")
    cli_dir = tmpdir()
    cli_db = make_db(os.path.join(cli_dir, "live", "dalton_solar.db"))
    cli_out = os.path.join(cli_dir, "backups")
    env = dict(os.environ)
    env["DALTON_DB_PATH"] = cli_db
    env["DALTON_BACKUP_DIR"] = cli_out
    env.pop("RENDER", None)

    def run(script, *argv):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *argv],
                              capture_output=True, text=True, env=env, timeout=180)

    fp = fingerprint(cli_db)
    r = run("backup_db.py")
    check(f"scripts/backup_db.py succeeds (rc={r.returncode})", r.returncode == 0)
    check("  ...and reports integrity_check=ok", "integrity_check=ok" in r.stdout)
    check("  ...without touching the source", fingerprint(cli_db) == fp)

    r = run("backup_db.py", "--list")
    check("--list shows the backup", "1 backup(s)" in r.stdout)
    r = run("backup_db.py", "--verify-all")
    check(f"--verify-all passes (rc={r.returncode})",
          r.returncode == 0 and "1 good, 0 bad" in r.stdout)

    r = run("restore_db.py", "--latest", "--inspect")
    check(f"restore_db.py --inspect works (rc={r.returncode})", r.returncode == 0)
    check("  ...and restores nothing", "nothing was restored" in r.stdout)

    dest = os.path.join(cli_dir, "restored.db")
    r = run("restore_db.py", "--latest", "--to", dest)
    check(f"restore_db.py --to a new path works (rc={r.returncode})",
          r.returncode == 0 and os.path.exists(dest))
    check("  ...and says the live database was untouched",
          "was not touched" in r.stdout)
    check("  ...and the source really is untouched", fingerprint(cli_db) == fp)

    r = run("restore_db.py", "--latest", "--to", cli_db, "--force")
    check(f"restore_db.py REFUSES the live database (rc={r.returncode})",
          r.returncode == 1 and "RESTORE REFUSED" in r.stderr)
    check("  ...and the live database is still untouched", fingerprint(cli_db) == fp)

    # ═══════════════════════════════════════════════════════════════════
    section("10. THIS SUITE NEVER TOUCHED THE APPLICATION DATABASE")
    check("db.DB_PATH is still a temporary file",
          not db.is_application_database(db.DB_PATH))
    check("the application database was never created by this run",
          not os.path.exists(db.APP_DB_PATH) or
          os.path.abspath(db.APP_DB_PATH) != os.path.abspath(db.DB_PATH))

    for t in _tmpdirs:
        shutil.rmtree(t, ignore_errors=True)

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


def _refuses(fn):
    try:
        fn()
    except db.DatabaseResetRefused:
        return True
    return False


if __name__ == "__main__":
    main()
