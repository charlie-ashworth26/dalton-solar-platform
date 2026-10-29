"""
Restore a backup to a NEW database file, and prove it is good.

    python scripts/restore_db.py --latest --to /tmp/check.db
    python scripts/restore_db.py --backup backups/dalton_solar-20261027T193045Z.db \
                                 --to /tmp/check.db
    python scripts/restore_db.py --inspect --latest      # verify, restore nothing

THIS COMMAND WILL NOT OVERWRITE THE APPLICATION DATABASE, and there is no flag
that makes it. A live database may be held open by a running server; writing
underneath it leaves neither the backup nor the original. Replacing the live
database is a deliberate, service-down sequence performed by a person - it is
written out step by step in BACKUP_AND_RECOVERY.md.

What this command IS for: restoring a backup alongside the live database so you
can verify it, read it, compare row counts, or pull back something that was
deleted - and, during a real recovery, staging the file that a human then moves
into place.

Exit codes:  0 success   1 refused or failed
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                                      # noqa: E402
from db import backup as bk                                    # noqa: E402


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _pick(args):
    if args.backup:
        return os.path.abspath(args.backup)
    backups = bk.list_backups(args.backup_dir)
    if not backups:
        print(f"No backups in {bk.resolve_backup_dir(args.backup_dir)}",
              file=sys.stderr)
        return None
    return backups[0]["path"]


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Restore a UBS database backup to a new file.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--backup", help="the backup file to restore")
    src.add_argument("--latest", action="store_true", help="use the newest backup")
    p.add_argument("--to", help="destination path for the restored database")
    p.add_argument("--backup-dir", help="where backups live")
    p.add_argument("--inspect", action="store_true",
                   help="verify the backup and print its contents; restore nothing")
    p.add_argument("--force", action="store_true",
                   help="overwrite --to if it already exists "
                        "(never permitted for the application database)")
    args = p.parse_args(argv)

    chosen = _pick(args)
    if chosen is None:
        return 1

    check = bk.verify_backup(chosen)
    print(f"backup : {chosen}")
    print(f"size   : {_size(check.get('size_bytes', 0))}")
    print(f"verify : integrity_check="
          f"{(check['integrity_check'] or ['?'])[0]}  "
          f"fk_violations={check['foreign_key_violations']}  "
          f"tables={check['tables']}")
    if not check["ok"]:
        print(f"\nTHIS BACKUP IS NOT USABLE: {check['error']}", file=sys.stderr)
        return 1
    counts = check["row_counts"] or {}
    print(f"rows   : enrollments={counts.get('enrollments')} "
          f"customers={counts.get('customers')} documents={counts.get('documents')} "
          f"users={counts.get('users')} "
          f"schema_migrations={counts.get('schema_migrations')}")

    if args.inspect:
        print("\n(--inspect: nothing was restored)")
        return 0
    if not args.to:
        print("\n--to is required unless you pass --inspect.", file=sys.stderr)
        return 1

    try:
        result = bk.restore_backup(chosen, args.to, force=args.force,
                                   verify_first=False)   # already verified above
    except bk.RestoreRefused as exc:
        print(f"\nRESTORE REFUSED\n{exc}", file=sys.stderr)
        return 1

    print(f"\nrestored to: {result['target']}")
    print(f"verified   : integrity_check="
          f"{result['verified']['integrity_check'][0]}  "
          f"tables={result['verified']['tables']}")
    print(f"\nThe live database at {db.APP_DB_PATH} was not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
