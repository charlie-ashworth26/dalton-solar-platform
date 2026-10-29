# UBS — Database Backup and Recovery

The database holds live enrollment and sales history. This document is the
runbook for backing it up and for getting it back.

Two separate protections exist and they answer different questions:

| | Protects against | Lives in |
|---|---|---|
| **C0.5 — reset guard** | a test, seed, startup or deploy *erasing* the database | `db/__init__.py` |
| **C0.6 — backups** *(this document)* | disk loss, a bad migration, a wrong `DELETE`, a lost host | `db/backup.py` |

Neither substitutes for the other. A guard cannot bring back a row somebody
deleted on purpose; a backup cannot stop a test run from deleting the file.

---

## Taking a backup

```bash
python scripts/backup_db.py
```

That backs up whatever `DALTON_DB_PATH` points at, writes a timestamped file
into the backup directory, proves it with `PRAGMA integrity_check`, writes a
manifest beside it, and applies the retention policy.

Other things the command does:

```bash
python scripts/backup_db.py --list            # what is on disk now
python scripts/backup_db.py --verify-all      # re-check every stored backup
python scripts/backup_db.py --prune-only      # apply retention, take nothing
python scripts/backup_db.py --prune-only --dry-run
```

**Nothing takes a backup on its own.** No import, app start, request, migration
or deploy triggers one. A backup happens because a schedule or a person ran
that command.

### Why not `cp dalton_solar.db backup.db`

A plain copy of a database that is being written is a torn read, and SQLite runs
in WAL mode here: committed rows may live in `dalton_solar.db-wal` and not be in
the `.db` file at all. The copy then looks fine and is quietly missing the newest
data — the failure surfaces the day you need it.

`db/backup.py` uses SQLite's online backup API (`sqlite3.Connection.backup`),
which copies pages under a read transaction and restarts if a writer changes the
source mid-copy. The regression suite demonstrates the difference: with 200 rows
committed but still in the WAL, the backup captures all of them and a file copy
captures none.

### The backup never writes to the source

The source is opened **read-only** (`file:…?mode=ro`), so modifying it is
impossible at the SQLite level rather than merely avoided. That also rules out
the subtle case: a read-write connection closing as the last connection
checkpoints the WAL and rewrites the `.db` file — logically identical, byte-wise
different, and it would mean the backup changed the database.

The one case read-only cannot handle is a `-wal` left needing recovery after a
crash, because recovery is a write. Rather than escalate to read-write on the
live file, the tool copies the `.db`/`-wal`/`-shm` triple aside and recovers the
*copy*. The manifest records `source_recovered_from_snapshot: true` when this
happened. The source is still never written.

---

## Where backups are stored

`DALTON_BACKUP_DIR` decides, and the default is `<repo>/backups` (gitignored).

| Environment | Database | Backups | Separate disk? |
|---|---|---|---|
| **Local development** | `<repo>/dalton_solar.db` | `<repo>/backups/` | No — and that is fine here |
| **Hosted staging** (Render) | `/var/data/dalton_solar.db` | `/var/data/backups/` | **No** — see below |
| **Production** | per deployment | a different volume **and** off-host object storage | Required |

### The Render limitation, stated plainly

A Render service gets **one** persistent disk. With the database on `/var/data`,
any backup written to `/var/data/backups` is on that same disk. It protects
against a bad migration, a wrong `DELETE` and an accidental reset — the most
common ways data is actually lost — but **it does not survive losing the disk**,
which is the failure a deployment backup is supposed to cover.

The tool refuses to pretend otherwise: on a deployed service, a same-disk backup
directory is rejected unless `--allow-same-disk` is passed, and when it is, the
manifest records `"separate": false`.

Staging accepts the compromise deliberately (`--allow-same-disk` in the cron
command) because staging is fake data. **Production must not.** Before this runs
with real customer data, one of these must be true:

1. the backup directory is on a **different volume** from the database, or
2. every backup is **copied off-host** promptly after it is written — object
   storage (S3, B2, GCS), a managed Postgres with its own backups, or a pull
   from a machine that is not this one.

A `backup_db.py` run writes one `.db` and one `.json` per backup, so shipping
them onward is a one-line follow-up in the same scheduled command:

```bash
python scripts/backup_db.py --allow-same-disk && \
  rclone copy /var/data/backups remote:ubs-backups --include '*.db' --include '*.json'
```

### Scheduling

**On Render, a disk attaches to exactly one service.** A separate Cron Job
service therefore gets its own empty disk and *cannot see the database at all* —
scheduling the backup as a Render Cron Job looks right and silently backs up
nothing. The backup has to run inside the service that owns the disk, as a
**one-off Job**:

```bash
render jobs create --service <dalton-staging-service-id> \
  --start-command "python scripts/backup_db.py --allow-same-disk"
```

That command inherits the service's environment and its `/var/data` mount.
Trigger it daily from whatever scheduler is available off-platform — a GitHub
Actions `schedule:` workflow calling the Render API is the usual choice. The
same constraint applies to any host where the database lives on a volume only
one service can mount.

Local or any Linux host, via `cron`:

```
17 3 * * *  cd /path/to/backend-milestone2.5 && .venv/bin/python scripts/backup_db.py
```

Windows, via Task Scheduler — run the repo's `.venv\Scripts\python.exe` with
argument `scripts\backup_db.py`, and set *Start in* to the repo directory.

---

## Retention policy

Applied after every backup, and separately via `--prune-only`:

| Rule | Default | Why |
|---|---|---|
| Everything from the last **24 hours** | `--keep-hours 24` | an hourly schedule keeps today's granularity |
| The newest backup of each of the last **7 days** | `--keep-daily 7` | a week of day-level recovery points |
| The newest backup of each of the last **4 ISO weeks** | `--keep-weekly 4` | a month of coarser history |
| **The newest backup, always** | not configurable | no configuration can leave you with nothing |

Pruning only ever deletes files that match the backup naming pattern exactly,
that sit directly inside the backup directory, and that are not the application
database. Those are three separate checks, so a misconfigured backup directory
pointed at the repo root still cannot reach `dalton_solar.db`.

Rough sizing: the database is ~500 KB today, so a full retention set is a few
megabytes. The 1 GB Render disk is not a constraint at this scale.

---

## Integrity verification

Every backup is verified **before it is allowed to become a backup**:

1. the copy is written to a `.part` file;
2. that file is opened read-only and `PRAGMA integrity_check` is run — it must
   return exactly `ok`;
3. `PRAGMA foreign_key_check` is run — it must return no rows;
4. the backup's per-table row counts are compared against the source's;
5. only then is the `.part` renamed into place, atomically.

A file that fails any of these is renamed `.FAILED` and the command exits
non-zero. It never receives a backup's name, so `--list`, `--verify-all` and the
restore tool cannot mistake it for one. The manifest records the
`integrity_check` result, the foreign-key violation count, a SHA-256 of the
file, and every table's row count.

Re-check stored backups at any time with `python scripts/backup_db.py --verify-all`.

---

## Restoring

### Inspecting a backup (safe, changes nothing)

```bash
python scripts/restore_db.py --latest --inspect
```

Verifies the newest backup and prints its row counts. Restores nothing.

### Restoring to a new file (safe, the normal case)

```bash
python scripts/restore_db.py --latest --to /tmp/recovered.db
```

Writes the backup to a **new** path, verifies the result, and leaves the live
database alone. This is what you want for: checking a backup is good, reading a
row that was deleted, comparing counts against the live database, or staging a
file during a real recovery.

To pull one record back, restore to a new file and copy the row across with SQL —
do not replace the whole database to recover one row.

### Replacing the live database (manual, deliberate, service down)

**`restore_db.py` will not do this, and there is no flag that makes it.** A live
database may be held open by a running server; writing underneath it leaves
neither the backup nor the original. Replacing it is a sequence a person
performs with the service stopped:

```bash
# 1. STOP THE SERVICE. Not optional — an open handle makes the rest unsafe.
#    Render: suspend the service, or scale to zero.
#    Local:  stop the Flask process.

# 2. Verify the backup you intend to use, BEFORE touching anything.
python scripts/restore_db.py --backup backups/dalton_solar-20261027T193045Z.db --inspect

# 3. Stage it next to the live database.
python scripts/restore_db.py \
    --backup backups/dalton_solar-20261027T193045Z.db \
    --to /var/data/dalton_solar.RESTORED.db

# 4. Move the CURRENT database aside. Do not delete it — if the backup turns
#    out to be the wrong one, this is the only copy of what you had.
mv /var/data/dalton_solar.db      /var/data/dalton_solar.PRE-RESTORE.db
mv /var/data/dalton_solar.db-wal  /var/data/dalton_solar.PRE-RESTORE.db-wal  2>/dev/null
mv /var/data/dalton_solar.db-shm  /var/data/dalton_solar.PRE-RESTORE.db-shm  2>/dev/null

# 5. Put the restored file in place.
mv /var/data/dalton_solar.RESTORED.db /var/data/dalton_solar.db

# 6. Confirm before starting anything.
python scripts/backup_db.py --list
python -c "import sqlite3; c=sqlite3.connect('file:/var/data/dalton_solar.db?mode=ro',uri=True); \
print(c.execute('PRAGMA integrity_check').fetchone()[0]); \
print('enrollments', c.execute('SELECT COUNT(*) FROM enrollments').fetchone()[0])"

# 7. START THE SERVICE. Migrations run on boot and are additive, so a backup
#    from an older schema is brought forward without losing rows.

# 8. Keep dalton_solar.PRE-RESTORE.db until you are certain. Then take a fresh
#    backup, because the retention set still describes the old timeline.
```

Step 4 is the one people skip. Don't — a restore from the wrong backup is
recoverable only if the database you replaced still exists.

### Testing a restore

Test restores against a throwaway file, never the live database:

```bash
python scripts/restore_db.py --latest --to /tmp/restore-drill.db
sqlite3 /tmp/restore-drill.db "SELECT COUNT(*) FROM enrollments;"
rm /tmp/restore-drill.db
```

Worth doing on a schedule. A backup nobody has ever restored is a hypothesis.

---

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DALTON_DB_PATH` | `<repo>/dalton_solar.db` | the database to back up |
| `DALTON_BACKUP_DIR` | `<repo>/backups` | where backups are written |
| `UBS_ALLOW_DB_RESET` | unset | C0.5 reset opt-in — **unrelated to backups**, and never set it in a deployment |

---

## Known gaps

- **Uploaded documents are not covered here.** `DALTON_DATA_DIR` holds customer
  bills and generated agreements; the database stores relative paths pointing at
  them. A database restored without those files has rows referring to documents
  that are gone. Back up `/var/data/uploads` and `/var/data/storage` on the same
  schedule.
- **Backups are not encrypted at rest.** The file contains customer names,
  addresses, masked account numbers and password hashes. Whatever storage it is
  copied to must be access-controlled, and off-host copies should be encrypted.
- **No restore drill is automated.** The tooling supports one (see above); doing
  it on a schedule is a process decision, not a code one.
