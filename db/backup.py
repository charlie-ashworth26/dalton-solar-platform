"""
UBS C0.6 - DATABASE BACKUP AND RECOVERY.

The database holds live enrollment and sales history. C0.5 made it very hard to
DESTROY; this module makes it possible to GET BACK. They answer different
failure modes and neither substitutes for the other:

    C0.5  a test / seed / deploy erases the file      -> refused
    C0.6  the disk dies, a row is deleted in error,   -> restore from a backup
          a migration goes wrong, the host is lost

WHY NOT `cp dalton_solar.db backup.db`
    A plain copy of a database that is being written is a torn read. SQLite can
    be mid-transaction, and in WAL mode the committed pages may be in
    dalton_solar.db-wal rather than in the .db at all - so the copy is missing
    the newest committed rows, or is corrupt outright. The result LOOKS like a
    backup and fails only when you need it.

    This module uses SQLite's online backup API (sqlite3.Connection.backup),
    which copies pages under a read transaction and restarts if a writer
    changes the source mid-copy. It is the mechanism SQLite provides for
    exactly this, and it is safe against a live, actively-written database.

WHY THE SOURCE IS OPENED READ-ONLY
    A backup must not be able to alter the thing it is protecting. The source
    connection is opened with mode=ro, so modification is impossible at the
    SQLite level, not merely avoided by convention. That also rules out the
    subtle one: a read-write connection closing as the last connection
    CHECKPOINTS the WAL, rewriting the .db file. Logically identical, byte-wise
    different - and it would mean "the backup changed the database", which is
    precisely what must never be true.

    The one case mode=ro cannot handle is a -wal left needing recovery after a
    crash: recovery is a write, and a read-only connection cannot perform it.
    Rather than escalate to read-write on the live file, _snapshot_source()
    copies the .db/-wal/-shm triple aside (pure reads) and recovers THE COPY.
    The source is still never written.

NOTHING HERE RUNS AUTOMATICALLY. No import, app start, request, migration or
deploy triggers a backup or a restore. Backups are a scheduled command;
restores are a human decision. See BACKUP_AND_RECOVERY.md.
"""
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import db as _db

#: Where backups go when DALTON_BACKUP_DIR is unset. Repo-local, gitignored,
#: and fine for development - NOT acceptable for a deployment, where the whole
#: point is to be somewhere the live database is not. See resolve_backup_dir().
DEFAULT_BACKUP_DIRNAME = "backups"

BACKUP_DIR_ENV = "DALTON_BACKUP_DIR"

#: dalton_solar-20261027T193045Z.db - UTC, sortable, and free of the colons
#: that are illegal in Windows filenames.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_NAME_RE = re.compile(r"^dalton_solar-(\d{8}T\d{6}Z)(?:-(\d+))?\.db$")

#: Retention defaults. Overridable per call and via the CLI.
KEEP_HOURS = 24      # everything from the last day, however often it ran
KEEP_DAILY = 7       # the newest backup of each of the last 7 days
KEEP_WEEKLY = 4      # the newest backup of each of the last 4 ISO weeks


class BackupError(RuntimeError):
    """A backup could not be taken, or could not be proved good."""


class RestoreRefused(RuntimeError):
    """A restore was asked for in a way that could damage a live database."""


# ───────────────────────────── paths and environment ─────────────────────────

def _ro_uri(path):
    """A read-only SQLite URI for `path`.

    Built with Path.as_uri() rather than by string concatenation because a
    Windows path is not a valid URI body: a drive letter and backslashes need
    the file:///<drive>:/... form and percent-encoding. Concatenation works on
    POSIX and fails on Windows, so getting it wrong passes CI on Linux and
    breaks on the machine the database actually lives on.
    """
    return pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def resolve_backup_dir(explicit=None):
    """Where backups are written, in precedence order."""
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get(BACKUP_DIR_ENV)
    if env:
        return os.path.abspath(env)
    return os.path.join(_db.BASE_DIR, DEFAULT_BACKUP_DIRNAME)


def _device_of(path):
    """The filesystem device a path lives on, walking up to the first existing
    ancestor so a not-yet-created backup directory still resolves."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def separation_report(source, backup_dir):
    """Is the backup directory meaningfully separate from the live database?

    A backup sharing the live database's disk still protects against the
    failures that are actually most common - a bad migration, a wrong DELETE,
    an accidental reset - but it does NOT survive losing the disk, and a
    deployment is exactly where losing the disk is the scenario worth planning
    for. The caller decides what to do; this only refuses to let it go unsaid.
    """
    src_dev, dst_dev = _device_of(source), _device_of(backup_dir)
    same_device = src_dev is not None and src_dev == dst_dev
    src_dir = os.path.dirname(os.path.abspath(source))
    nested = os.path.abspath(backup_dir).startswith(src_dir + os.sep)
    return {
        "source_dir": src_dir,
        "backup_dir": os.path.abspath(backup_dir),
        "same_device": same_device,
        "inside_source_dir": nested,
        "deployed": _db._looks_like_server_deployment(),
        "separate": not same_device,
    }


# ─────────────────────────────── taking a backup ─────────────────────────────

def _timestamp(now=None):
    return (now or datetime.now(timezone.utc)).strftime(_STAMP_FORMAT)


def _unique_target(backup_dir, stamp):
    """dalton_solar-<stamp>.db, suffixed if that second already has one."""
    base = os.path.join(backup_dir, f"dalton_solar-{stamp}.db")
    if not os.path.exists(base):
        return base
    for n in range(2, 1000):
        candidate = os.path.join(backup_dir, f"dalton_solar-{stamp}-{n}.db")
        if not os.path.exists(candidate):
            return candidate
    raise BackupError(f"too many backups in the same second at {backup_dir}")


def _snapshot_source(source, workdir):
    """Copy the .db/-wal/-shm triple aside, for the crash-recovery case only.

    Reached only when the live -wal needs recovery, which a read-only
    connection cannot do. Every operation here READS the source; the copy is
    what gets recovered when it is opened.
    """
    dest = os.path.join(workdir, "source-snapshot.db")
    shutil.copy2(source, dest)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(source + suffix):
            shutil.copy2(source + suffix, dest + suffix)
    return dest


def _open_source(source, workdir):
    """(connection, recovered) - read-only if at all possible."""
    try:
        conn = sqlite3.connect(_ro_uri(source), uri=True, timeout=30)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, False
    except sqlite3.Error:
        # Almost always: a -wal needing recovery, which read-only cannot do.
        # Recover a COPY instead of writing to the live file.
        snapshot = _snapshot_source(source, workdir)
        conn = sqlite3.connect(snapshot, timeout=30)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, True


def _table_counts(conn):
    tables = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"))
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in tables}


def verify_backup(path):
    """Prove a backup file is a usable database. Opens it READ-ONLY.

    `PRAGMA integrity_check` is the required one and catches structural
    damage - a torn copy, a bad sector, a truncated write.
    `PRAGMA foreign_key_check` is cheap here and catches the referential
    damage a half-applied restore would leave.
    """
    result = {"path": os.path.abspath(path), "ok": False,
              "integrity_check": None, "foreign_key_violations": None,
              "tables": None, "row_counts": None, "error": None}
    if not os.path.exists(path):
        result["error"] = "file does not exist"
        return result
    result["size_bytes"] = os.path.getsize(path)
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=30)
    except sqlite3.Error as exc:
        result["error"] = f"cannot open: {exc}"
        return result
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result["integrity_check"] = [r[0] for r in rows]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        result["foreign_key_violations"] = len(fk)
        counts = _table_counts(conn)
        result["tables"] = len(counts)
        result["row_counts"] = counts
        result["ok"] = (result["integrity_check"] == ["ok"]
                        and result["foreign_key_violations"] == 0)
        if not result["ok"] and result["error"] is None:
            result["error"] = (f"integrity_check={result['integrity_check']} "
                               f"fk_violations={result['foreign_key_violations']}")
    except sqlite3.Error as exc:
        result["error"] = f"verification failed: {exc}"
    finally:
        conn.close()
        _clear_sidecars(path)
    return result


def create_backup(source=None, backup_dir=None, allow_same_disk=False,
                  prune_after=True, keep_hours=KEEP_HOURS, keep_daily=KEEP_DAILY,
                  keep_weekly=KEEP_WEEKLY, now=None):
    """Take one verified, timestamped backup. Returns a result dict.

    The sequence is deliberate: write to a .part file, verify THAT, and only
    then rename it into place. A reader of the backup directory therefore never
    sees a file that has not already passed integrity_check - an unverified
    backup is worse than no backup, because it is trusted.
    """
    source = os.path.abspath(source or _db.DB_PATH)
    if not os.path.exists(source):
        raise BackupError(f"no database to back up at {source}")
    backup_dir = resolve_backup_dir(backup_dir)

    sep = separation_report(source, backup_dir)
    if sep["same_device"] and sep["deployed"] and not allow_same_disk:
        raise BackupError(
            f"Refusing to write backups to the same disk as the live database.\n"
            f"  database: {sep['source_dir']}\n"
            f"  backups:  {sep['backup_dir']}\n"
            "On a deployed service this protects against a bad migration or a "
            "wrong DELETE, but NOT against losing the disk - and losing the "
            "disk is the failure a deployment backup exists for.\n"
            f"  * Point {BACKUP_DIR_ENV} at a different mount, or ship each "
            "backup off-host (see BACKUP_AND_RECOVERY.md).\n"
            "  * Pass --allow-same-disk to accept the limitation deliberately.")

    os.makedirs(backup_dir, exist_ok=True)
    started = datetime.now(timezone.utc)
    target = _unique_target(backup_dir, _timestamp(now or started))
    part = target + ".part"

    workdir = tempfile.mkdtemp(prefix="ubs-backup-")
    src_conn = dest_conn = None
    try:
        src_conn, recovered = _open_source(source, workdir)
        source_counts = _table_counts(src_conn)
        dest_conn = sqlite3.connect(part, timeout=30)
        # THE ONLINE BACKUP API. Copies under a read transaction and restarts
        # automatically if a writer changes the source mid-copy, so this is
        # safe against a live database being written to right now.
        src_conn.backup(dest_conn)
        dest_conn.close()
        dest_conn = None
        src_conn.close()
        src_conn = None

        check = verify_backup(part)
        if not check["ok"]:
            failed = part + ".FAILED"
            os.replace(part, failed)
            raise BackupError(
                f"backup failed verification and was NOT kept as a backup: "
                f"{check['error']} (evidence left at {failed})")
        if check["row_counts"] != source_counts:
            failed = part + ".FAILED"
            os.replace(part, failed)
            raise BackupError(
                "backup row counts do not match the source; not kept "
                f"(evidence left at {failed})")

        _clear_sidecars(part)           # see _clear_sidecars
        os.replace(part, target)        # atomic: appears only once verified
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if src_conn is not None:
            src_conn.close()
        shutil.rmtree(workdir, ignore_errors=True)
        if os.path.exists(part):
            os.remove(part)
        _clear_sidecars(part)

    finished = datetime.now(timezone.utc)
    manifest = {
        "backup": os.path.basename(target),
        "created_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "source_path": source,
        "source_recovered_from_snapshot": recovered,
        "size_bytes": os.path.getsize(target),
        "sha256": _sha256(target),
        "integrity_check": check["integrity_check"],
        "foreign_key_violations": check["foreign_key_violations"],
        "tables": check["tables"],
        "row_counts": check["row_counts"],
        "schema_migrations": check["row_counts"].get("schema_migrations"),
        "separation": sep,
    }
    with open(_manifest_path(target), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    _clear_sidecars(target)       # a stored backup is ONE file plus its manifest

    pruned = []
    if prune_after:
        pruned = prune(backup_dir, keep_hours=keep_hours, keep_daily=keep_daily,
                       keep_weekly=keep_weekly, now=now)
    return {"path": target, "manifest": manifest, "verified": check,
            "pruned": pruned, "separation": sep}


def _clear_sidecars(path):
    """Remove the -wal/-shm files an OPEN of `path` created.

    Opening a WAL database creates these even for a read-only connection, so
    merely verifying a backup leaves two files behind. Two reasons that is not
    cosmetic:

      * the .part ones outlive the file they belong to - it is renamed - and
        nothing would ever clean up a sidecar named after a file that no
        longer exists, so they accumulate one pair per backup, forever;
      * a -wal sitting beside a stored backup is a file SQLite may consult
        when that backup is next opened or copied, which is the stale-sidecar
        hazard restore_backup() clears at the other end.

    Safe because every caller here has already closed its connections and a
    read-only verification committed nothing.
    """
    for suffix in ("-wal", "-shm"):
        sidecar = path + suffix
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError:
                pass        # never fail a good backup over a leftover file


def _manifest_path(backup_path):
    return backup_path[:-3] + ".json"      # .db -> .json


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────── retention ──────────────────────────────────

def list_backups(backup_dir=None):
    """Every well-formed backup in the directory, newest first."""
    backup_dir = resolve_backup_dir(backup_dir)
    if not os.path.isdir(backup_dir):
        return []
    found = []
    for name in os.listdir(backup_dir):
        m = _NAME_RE.match(name)
        if not m:
            continue                       # .part, .FAILED, anything else
        try:
            when = datetime.strptime(m.group(1), _STAMP_FORMAT).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        path = os.path.join(backup_dir, name)
        found.append({"path": path, "name": name, "when": when,
                      "size_bytes": os.path.getsize(path),
                      "manifest": _manifest_path(path)
                      if os.path.exists(_manifest_path(path)) else None})
    found.sort(key=lambda b: (b["when"], b["name"]), reverse=True)
    return found


def select_for_retention(backups, keep_hours=KEEP_HOURS, keep_daily=KEEP_DAILY,
                         keep_weekly=KEEP_WEEKLY, now=None):
    """(keep, delete) - a grandfather-father-son policy.

        * everything from the last `keep_hours` hours, so an hourly schedule
          keeps today's granularity;
        * the newest backup of each of the last `keep_daily` days;
        * the newest backup of each of the last `keep_weekly` ISO weeks;
        * the newest backup overall, unconditionally and always.

    The last rule is the one that matters: no combination of clock skew, empty
    directory, or silly configuration can leave zero backups behind.
    """
    now = now or datetime.now(timezone.utc)
    if not backups:
        return [], []
    ordered = sorted(backups, key=lambda b: b["when"], reverse=True)
    keep_paths = {ordered[0]["path"]}                   # newest, always

    cutoff = now - timedelta(hours=keep_hours)
    for b in ordered:
        if b["when"] >= cutoff:
            keep_paths.add(b["path"])

    seen_days, seen_weeks = {}, {}
    for b in ordered:                                   # newest first
        day = b["when"].date()
        if day not in seen_days:
            seen_days[day] = b
        week = b["when"].isocalendar()[:2]
        if week not in seen_weeks:
            seen_weeks[week] = b
    for day in sorted(seen_days, reverse=True)[:keep_daily]:
        keep_paths.add(seen_days[day]["path"])
    for week in sorted(seen_weeks, reverse=True)[:keep_weekly]:
        keep_paths.add(seen_weeks[week]["path"])

    keep = [b for b in ordered if b["path"] in keep_paths]
    delete = [b for b in ordered if b["path"] not in keep_paths]
    return keep, delete


def prune(backup_dir=None, keep_hours=KEEP_HOURS, keep_daily=KEEP_DAILY,
          keep_weekly=KEEP_WEEKLY, now=None, dry_run=False):
    """Delete backups the retention policy no longer keeps.

    This is the only function here that deletes anything, so it re-checks every
    path immediately before unlinking it: the name must match the backup
    pattern, the file must sit directly inside the backup directory, and it
    must not be the application database.

    list_backups() already filters on the first of those, so in the current
    code the re-checks are a second line rather than the only one - they exist
    because the cost of being wrong here is a deleted database, and because a
    future change to list_backups() should not be able to turn this into a
    delete-anything loop. Do not remove them on the grounds that they are
    currently redundant; that is the point.
    """
    backup_dir = os.path.abspath(resolve_backup_dir(backup_dir))
    keep, delete = select_for_retention(
        list_backups(backup_dir), keep_hours=keep_hours, keep_daily=keep_daily,
        keep_weekly=keep_weekly, now=now)
    removed = []
    for b in delete:
        path = os.path.abspath(b["path"])
        if not _NAME_RE.match(os.path.basename(path)):
            continue
        if os.path.dirname(path) != backup_dir:
            continue
        if _db.is_application_database(path):
            continue                       # unreachable by design; still checked
        if dry_run:
            removed.append(path)
            continue
        os.remove(path)
        _clear_sidecars(path)
        manifest = _manifest_path(path)
        if os.path.exists(manifest):
            os.remove(manifest)
        removed.append(path)
    return removed


# ──────────────────────────────── restoring ──────────────────────────────────

def restore_backup(backup_path, target_path, force=False, verify_first=True):
    """Write a verified backup to `target_path`. NEVER called automatically.

    THE APPLICATION DATABASE CANNOT BE A TARGET, and there is no flag to allow
    it. That is not an oversight. Restoring over a live database while a server
    may be holding it open is how a bad afternoon becomes an unrecoverable one:
    the running process keeps its own file handle and its WAL, and the result is
    neither the backup nor the original. The supported production restore stops
    the service and moves files by hand - BACKUP_AND_RECOVERY.md spells out the
    sequence - and this function exists to restore ALONGSIDE the live database,
    which is what verification, inspection and recovery staging actually need.
    """
    backup_path = os.path.abspath(backup_path)
    target_path = os.path.abspath(target_path)

    if not os.path.exists(backup_path):
        raise RestoreRefused(f"no backup at {backup_path}")
    if _db.is_application_database(target_path):
        raise RestoreRefused(
            f"Refusing to restore over the application database at {target_path}.\n"
            "A live database may be open by a running server; writing underneath "
            "it produces neither the backup nor the original.\n"
            "  * Restore to a NEW path and inspect it first.\n"
            "  * To replace the live database, follow the documented sequence "
            "in BACKUP_AND_RECOVERY.md (stop the service, move the current "
            "file aside, put the backup in place).")
    if os.path.exists(target_path) and not force:
        raise RestoreRefused(
            f"{target_path} already exists; pass force=True to overwrite it.")

    if verify_first:
        check = verify_backup(backup_path)
        if not check["ok"]:
            raise RestoreRefused(
                f"refusing to restore from a backup that fails verification: "
                f"{check['error']}")

    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    # Clear stale sidecars BEFORE the copy. SQLite ignores a -wal whose salt
    # does not match the database it sits beside, so in most cases this changes
    # nothing - but "most cases" is not a property worth relying on when the
    # failure mode is a silently wrong recovery, and a -journal from a
    # non-WAL-mode database is a rollback hint SQLite will act on.
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(target_path + suffix):
            os.remove(target_path + suffix)
    shutil.copy2(backup_path, target_path)

    restored = verify_backup(target_path)
    if not restored["ok"]:
        raise RestoreRefused(
            f"the restored file at {target_path} does not verify: "
            f"{restored['error']}")

    # Verifying opened the file, and opening a WAL database recreates empty
    # -wal/-shm sidecars. Any sidecar here was created by that read - the stale
    # ones were removed above and the copy brought only the .db - and a
    # read-only connection committed nothing to them. Removing them matters
    # because the documented recovery step moves the restored file into place:
    # a restored database should be one clean file, not one file and two
    # orphans left behind in the staging directory.
    for suffix in ("-wal", "-shm"):
        sidecar = target_path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)
    return {"backup": backup_path, "target": target_path, "verified": restored}
