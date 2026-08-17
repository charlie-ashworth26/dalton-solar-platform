# Dalton — Private Staging Deployment (Render)

> **FAKE / TEST DATA ONLY.** This environment is not approved for real customer
> enrollment. It talks to the **Perch STAGING API**, and every tester sees a red
> `TEST ENVIRONMENT — DO NOT ENTER REAL CUSTOMER INFORMATION` banner.

Target: 2–3 coworkers testing simultaneously.

---

## Architecture

```
staging.cleanenergyenrollment.org
  -> Render web service (Python 3.12.0)
     -> Gunicorn, 1 worker / 4 threads
        -> Dalton Flask app
           -> SQLite on a Render persistent disk   /var/data/dalton_solar.db
           -> uploaded + generated files            /var/data/uploads, /var/data/storage
           -> Perch STAGING API
```

### Why exactly one worker

Not a performance oversight. Two reasons, both structural:

1. A Render **persistent disk attaches to one instance only**.
2. Contract-review capability tokens live in a **process-local dict**, so a
   second worker would reject links minted by the first.

**One worker does not mean one user.** The 4 threads serve several coworkers
concurrently; SQLite runs in WAL mode with a 5-second busy timeout so concurrent
readers don't collide with the writer.

---

## 1. Create the service

1. Render dashboard → **New** → **Web Service**
2. Connect the GitHub repo, branch **`phase4a-rep-visibility-auth`**
3. Runtime **Python 3**
4. Plan **Starter or higher** — the free plan has **no persistent disk**, and
   without one the database and every uploaded document are destroyed on each
   deploy

## 2. Build and start commands

**Build command**
```
pip install -r requirements.txt
```

**Start command**
```
gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
```

## 3. Python version

`.python-version` pins **3.12.0**. Also set `PYTHON_VERSION=3.12.0` as an
environment variable — Render honours it explicitly.

## 4. Persistent disk (required)

Render → service → **Disks** → **Add Disk**

| Setting | Value |
|---|---|
| Name | `dalton-data` |
| Mount path | `/var/data` |
| Size | 1 GB |

Everything Dalton must keep lives here: the SQLite database, uploaded
originals, generated documents and submission packages. **Without the disk, all
of it is wiped on every deploy and restart** and document links start returning
"file missing on disk".

## 5. Health check

Render → **Settings** → **Health Check Path**:

```
/api/health
```

Unauthenticated and returns `{"ok": true}`, so Render can poll it.

## 6. Environment variables

### Non-secret configuration — safe to commit / paste

| Key | Value |
|---|---|
| `PYTHON_VERSION` | `3.12.0` |
| `DALTON_ENV` | `staging` |
| `DALTON_DB_PATH` | `/var/data/dalton_solar.db` |
| `DALTON_DATA_DIR` | `/var/data` |
| `DALTON_TRUSTED_PROXY_COUNT` | `1` |
| `DALTON_LOG_LEVEL` | `INFO` |
| `PERCH_API_MODE` | `live` |
| `PERCH_ENROLLMENT_BASE_URL` | `https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments` |
| `PERCH_MARKETS_BASE_URL` | `https://staging.api.perchenergy.com/affiliate_partners/v1/markets` |

> **`PERCH_API_MODE=live` means real HTTP instead of mock fixtures. It does NOT
> mean Perch production.** The staging hosts above are what decide that. Never
> point these at `api.perchenergy.com` for this environment.

### SECRETS — set in the Render dashboard only, never in git

| Key | Where it comes from |
|---|---|
| `JWT_SECRET` | **Generate a NEW one** (below). Do not reuse your local value. |
| `PERCH_API_KEY` | Copy from your local `.env` |
| `PERCH_SECRET_KEY` | Copy from your local `.env` |

Generate the JWT secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The app **refuses to boot** in hosted mode if `JWT_SECRET` is missing, blank,
still the public development default, or shorter than 32 characters. That is
deliberate: the development fallback is committed to this repository, so a
deploy using it would have forgeable session tokens.

### What to copy from your local `.env`

Only these two: **`PERCH_API_KEY`** and **`PERCH_SECRET_KEY`**.
Everything else is either listed above or newly generated.

## 7. First boot

1. Deploy and open **Logs**
2. Confirm the startup banner shows:
   - `Perch mode : LIVE`
   - `Environment : staging`
   - `Enrollment host : staging.api.perchenergy.com`
   - `API key : configured` / `Signing key : configured`
3. If the service exits with a `JWT_SECRET` error, set it and redeploy — that
   guard is working as intended.

### Seed the first admin

The database starts empty. Render → **Shell**:

```
python seed.py
```

Then log in as the seeded admin, open **Reps**, create real rep accounts for
your coworkers, and **change the seeded admin password immediately** via
Rep management → Reset password.

## 8. Test the temporary URL

Render gives you `https://dalton-staging-XXXX.onrender.com`.

- [ ] Red `TEST ENVIRONMENT` banner is visible **on the login page**
- [ ] Log in as admin
- [ ] Create a rep in **Reps**; log in as that rep in a second browser
- [ ] Run one full fake enrollment through to contracts
- [ ] Upload a multi-page bill; reopen it and click a filename — it opens
- [ ] **Redeploy**, then confirm the enrollment and its documents still exist
      (this is the real test that the persistent disk is working)
- [ ] Two coworkers use it at the same time without errors

## 9. Attach staging.cleanenergyenrollment.org

1. Render → service → **Settings** → **Custom Domains** → **Add**
2. Enter `staging.cleanenergyenrollment.org`
3. At your DNS provider add the **CNAME** Render shows, pointing at the
   `.onrender.com` hostname
4. Wait for verification; Render issues the TLS certificate automatically
5. Re-run the checks in step 8 against the custom domain

---

## Notes and limits

- **SQLite is deliberate for this milestone.** PostgreSQL is a separate
  milestone required before any real customer data.
- **Back up before redeploying** anything risky: Render → Shell →
  `cp /var/data/dalton_solar.db /var/data/backup-$(date +%F).db`
- **The URL is public.** There is no signup, so accounts are admin-created only,
  but the login page is reachable from the internet. Consider Render's IP
  allowlist if you want it locked down further.
- **Local development is unchanged.** With none of these variables set, Dalton
  uses the repo-local SQLite file and repo-local upload paths exactly as before.

