"""
Take one verified backup of the configured database.

    python scripts/backup_db.py                 # back up $DALTON_DB_PATH
    python scripts/backup_db.py --list          # what is on disk now
    python scripts/backup_db.py --verify-all    # re-check every stored backup
    python scripts/backup_db.py --prune-only    # apply retention, take nothing
    python scripts/backup_db.py --dry-run       # show what retention WOULD drop

This is the command a scheduler runs. It takes a backup, proves it with
PRAGMA integrity_check, and applies the retention policy. Nothing about it runs
on import, on app start, or on deploy. See BACKUP_AND_RECOVERY.md.

Exit codes:  0 success   1 failure (including "refused to use the same disk")
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


def cmd_list(args):
    backups = bk.list_backups(args.backup_dir)
    where = bk.resolve_backup_dir(args.backup_dir)
    if not backups:
        print(f"No backups in {where}")
        return 0
    print(f"{len(backups)} backup(s) in {where}\n")
    for b in backups:
        flag = "" if b["manifest"] else "   (no manifest)"
        print(f"  {b['name']}  {_size(b['size_bytes']):>10}  "
              f"{b['when'].strftime('%Y-%m-%d %H:%M:%SZ')}{flag}")
    keep, delete = bk.select_for_retention(
        backups, keep_hours=args.keep_hours, keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly)
    print(f"\nretention: {len(keep)} kept, {len(delete)} eligible to remove")
    return 0


def cmd_verify_all(args):
    backups = bk.list_backups(args.backup_dir)
    if not backups:
        print(f"No backups in {bk.resolve_backup_dir(args.backup_dir)}")
        return 1
    bad = 0
    for b in backups:
        r = bk.verify_backup(b["path"])
        if r["ok"]:
            print(f"  [OK]   {b['name']}  integrity_check=ok  "
                  f"tables={r['tables']}")
        else:
            bad += 1
            print(f"  [BAD]  {b['name']}  {r['error']}")
    print(f"\n{len(backups) - bad} good, {bad} bad")
    return 1 if bad else 0


def cmd_prune(args):
    removed = bk.prune(args.backup_dir, keep_hours=args.keep_hours,
                       keep_daily=args.keep_daily, keep_weekly=args.keep_weekly,
                       dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    if not removed:
        print("retention: nothing to remove")
        return 0
    print(f"retention: {verb} {len(removed)}")
    for p in removed:
        print(f"  {os.path.basename(p)}")
    return 0


def cmd_backup(args):
    source = os.path.abspath(args.source or db.DB_PATH)
    target_dir = bk.resolve_backup_dir(args.backup_dir)
    sep = bk.separation_report(source, target_dir)

    print(f"source : {source}")
    print(f"backups: {target_dir}")
    if not sep["separate"]:
        print("  NOTE: backups are on the SAME disk as the database - this "
              "survives a bad migration, not a lost disk.")

    result = bk.create_backup(
        source=source, backup_dir=args.backup_dir,
        allow_same_disk=args.allow_same_disk, prune_after=not args.no_prune,
        keep_hours=args.keep_hours, keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly)

    m = result["manifest"]
    print(f"\nwrote  : {os.path.basename(result['path'])}  "
          f"({_size(m['size_bytes'])} in {m['duration_seconds']}s)")
    print(f"sha256 : {m['sha256']}")
    print(f"verify : integrity_check={m['integrity_check'][0]}  "
          f"fk_violations={m['foreign_key_violations']}  tables={m['tables']}")
    counts = m["row_counts"]
    print(f"rows   : enrollments={counts.get('enrollments')} "
          f"customers={counts.get('customers')} documents={counts.get('documents')} "
          f"users={counts.get('users')}")
    if m["source_recovered_from_snapshot"]:
        print("note   : the live -wal needed recovery, so a COPY of the "
              "database was recovered and backed up. The source was not written.")
    if result["pruned"]:
        print(f"pruned : {len(result['pruned'])} older backup(s)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Take a verified, timestamped backup of the UBS database.")
    p.add_argument("--source", help="database to back up (default: DALTON_DB_PATH)")
    p.add_argument("--backup-dir", help=f"where to write (default: ${bk.BACKUP_DIR_ENV} "
                                        f"or <repo>/{bk.DEFAULT_BACKUP_DIRNAME})")
    p.add_argument("--allow-same-disk", action="store_true",
                   help="accept backups on the live database's disk when deployed")
    p.add_argument("--no-prune", action="store_true", help="skip retention")
    p.add_argument("--keep-hours", type=int, default=bk.KEEP_HOURS)
    p.add_argument("--keep-daily", type=int, default=bk.KEEP_DAILY)
    p.add_argument("--keep-weekly", type=int, default=bk.KEEP_WEEKLY)
    p.add_argument("--list", action="store_true", help="list stored backups")
    p.add_argument("--verify-all", action="store_true",
                   help="re-run integrity_check over every stored backup")
    p.add_argument("--prune-only", action="store_true",
                   help="apply retention without taking a backup")
    p.add_argument("--dry-run", action="store_true",
                   help="with --prune-only, show what would be removed")
    args = p.parse_args(argv)

    try:
        if args.list:
            return cmd_list(args)
        if args.verify_all:
            return cmd_verify_all(args)
        if args.prune_only:
            return cmd_prune(args)
        return cmd_backup(args)
    except bk.BackupError as exc:
        print(f"\nBACKUP FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
