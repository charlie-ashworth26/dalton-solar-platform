# Dalton Solar Enrollment Platform — Backend (Phase 1)

This is the persistence + API layer for the enrollment platform, built on top
of the existing static prototype. It does **not** touch the existing
enrollment flow, utility-bill extraction, LMI logic, project data, document
packet content, or signing UX design that were already working — it gives
all of that a real database and a real server behind it, per the instruction
to build persistence before changing the signature/developer workflows.

One piece of the signature workflow *was* built this phase anyway
(`templates/signing_session.html`) because there was nothing to test the
signing API against otherwise — it's a plain HTML/JS page, no framework,
that talks to the real endpoints below.

---

## 1. Summary of what was implemented

- **Full relational schema** — all 17 tables from the spec, normalized (no
  enrollment-as-one-JSON-blob). One additional table, `signing_sessions`,
  was added beyond the spec's list because "expiring signing session"
  (requirement #6) needs somewhere to live — see *Known limitations* for
  every other place I diverged from or extended the spec.
- **A real Flask REST API** — JWT auth, role-based access control, all
  endpoints from the spec plus a few supporting ones (project listing,
  correction-reason list, `/me`).
- **Real utility-bill extraction** (pdfplumber + regex), tuned against and
  verified against the actual National Grid bill you supplied. Every
  extracted field carries a confidence score; the rep can correct any value
  without losing the original extraction (`documents.extracted_data_json`
  vs `documents.corrected_data_json`).
- **A rule-based LMI document classifier** — matches uploaded documents
  against the 9 NY-accepted types from your reference sheet, checks the
  date is within 12 months, cross-checks the name, and flags the DAC
  requirement for Medicaid/Lifeline/SLIP. This is explicitly **not** a real
  AI model — see *Known limitations*.
- **Real PDF generation** (reportlab) for all 6 packet documents, populated
  from the actual enrollment record — verified by extracting text back out
  of a generated PDF and confirming the real customer name, address, and
  project savings rate appear.
- **A real e-signature workflow** — session tokens with expiry, one required
  field per applicable document, typed/drawn signature capture, a signature
  certificate PDF, and a full audit trail via `signature_events`.
- **Real packet assembly** — a merged PDF (cover sheet + all generated docs +
  certificate) and a ZIP in the exact folder structure you specified, both
  verified to actually open (`%PDF` / `PK` magic bytes checked, and the CDG
  disclosure PDF's text was re-extracted to confirm it's not garbled).
- **QA and developer portals as APIs** — both fully functional and tested;
  see *Known limitations* for what's UI vs. API-only right now.
- **Full audit logging and status history** — every status change and every
  document access writes a row; verified populated after the test run (33
  audit log rows, 14 status history rows from one enrollment's lifecycle).

Everything above is **verified**, not just written — see section 7.

---

## 2. Database schema

Two files, identical shape:

- `db/schema_sqlite.sql` — what the prototype actually runs on.
- `db/schema_postgres.sql` — ready to run against Postgres as-is when you
  migrate. Same tables, same columns, same constraints; only dialect
  mechanics differ (`SERIAL` vs `AUTOINCREMENT`, `TIMESTAMPTZ` vs `TEXT`,
  `JSONB` vs `TEXT` for the metadata columns).

Tables: `users`, `sales_reps`, `customers`, `service_addresses`,
`utility_accounts`, `projects`, `enrollments`, `lmi_qualifications`,
`documents`, `agreements`, `signatures`, `signature_events`,
`validation_results`, `qa_reviews`, `submissions`, `status_history`,
`audit_logs`, plus `signing_sessions` (addition, see above).

### Migrating to Postgres later

1. Provision a Postgres database (Neon, Supabase, RDS, whatever).
2. `psql $DATABASE_URL -f db/schema_postgres.sql`
3. Swap `db/__init__.py`'s sqlite3 connection for `psycopg2` (or move to
   SQLAlchemy/Knex if you want an ORM) — the query shapes barely change
   since parameter binding style (`?` → `%s`) is the main difference.
   The `datetime('now')` calls in application code become `NOW()`.

---

## 3. API endpoints

All routes are prefixed `/api` except the signing page itself (`/sign/<token>`).
Auth: `Authorization: Bearer <token>` header, JWT issued by `/api/auth/login`.
Signing-session routes use the session token in the URL instead — customers
never get a staff account.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/login` | — | Staff login, returns JWT |
| GET | `/api/auth/me` | any | Current user |
| POST | `/api/enrollments` | rep, admin | Create draft enrollment |
| GET | `/api/enrollments` | any (rep sees own only) | List, filterable by status/project/date |
| GET | `/api/enrollments/:id` | any | Full detail incl. documents, agreements, signatures, status history, QA, submission |
| PATCH | `/api/enrollments/:id` | rep, admin | Update customer/address/utility/project |
| POST | `/api/enrollments/:id/status` | any | Status transition (validated against the state machine) |
| POST | `/api/enrollments/:id/lmi` | rep, admin | Record LMI qualification (any of the 3 paths) |
| POST | `/api/enrollments/:id/documents` | rep, admin | Upload + auto-extract (bill or LMI doc) |
| POST | `/api/enrollments/:id/documents/:id/correct` | rep, admin | Store a correction to extracted fields |
| GET | `/api/enrollments/:id/documents/:id/download` | any | Protected file download |
| POST | `/api/enrollments/:id/agreements/generate` | rep, admin | Generate the dynamic document packet |
| POST | `/api/enrollments/:id/signing-session` | rep, admin | Create a signing link (72h expiry) |
| GET | `/api/signing-sessions/:token` | session token | Fetch packet + required fields |
| GET | `/api/signing-sessions/:token/documents/:agreementId` | session token | View one document PDF |
| POST | `/api/signing-sessions/:token/fields/:fieldKey` | session token | Submit one signature/initial |
| POST | `/api/signing-sessions/:token/complete` | session token | Finalize — generates signature certificate |
| GET | `/api/qa/queue` | qa, admin | Enrollments awaiting review |
| GET | `/api/qa/correction-reasons` | any | Standardized reason codes |
| POST | `/api/qa/enrollments/:id/review` | qa, admin | Approve / reject / needs-work |
| POST | `/api/enrollments/:id/submit` | rep, qa, admin | Build merged PDF + ZIP, create submission |
| GET | `/api/enrollments/:id/package` | any | Download links for the package |
| GET | `/api/submissions/:id` | any | Submission record |
| GET | `/api/submissions/:id/status` | any | Just the developer_status |
| PATCH | `/api/submissions/:id/status` | developer, admin | Accept / reject / needs-work |
| GET | `/api/developer/submissions` | developer, admin | Developer queue, filterable |
| GET | `/api/developer/submissions/:id` | developer, admin | Full record for review |
| POST | `/api/developer/submissions/:id/assign-project` | developer, admin | Assign project → status Project Assigned |
| POST | `/api/developer/submissions/:id/activate` | developer, admin | Final status → Active |
| GET | `/api/reports/summary` | admin, qa, developer | Counts, filterable by rep/project/date/LMI |
| GET | `/api/projects` | — | Project list |
| GET | `/api/health` | — | Liveness check |
| GET | `/sign/:token` | — | The actual signing page (HTML) |

---

## 4. Test credentials

Created by `seed.py`:

| Role | Email | Password |
|---|---|---|
| Admin | `admin@daltonsolar.com` | `AdminPass1!` |
| Sales rep (Charlie Mren) | `charlie@daltonsolar.com` | `RepPass1!` |
| QA reviewer | `qa@daltonsolar.com` | `QaPass1!` |
| Developer (Arcadia/Perch reviewer) | `developer@perchenergy.com` | `DevPass1!` |

---

## 5. Local setup

Requires Python 3.10+ and `tesseract-ocr` installed on the system (for photo
LMI/bill uploads — PDFs don't need it).

```bash
cd backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# macOS: brew install tesseract
# Ubuntu/Debian: sudo apt install tesseract-ocr

python3 seed.py                   # creates dalton_solar.db, seeds users + projects
python3 app.py                    # runs on http://localhost:5000
```

Sanity check: `curl http://localhost:5000/api/health` → `{"ok": true}`

## 6. Database initialization and seed instructions

`python3 seed.py` is destructive by design for this prototype — it drops and
recreates the database every time (`init_db(reset=True)`), then seeds the 4
test users and the same 3 projects the client-side app uses (Cobblestone
Ridge, Birchfield Commons, Otter Creek Solar), with `lmi_required` set on
two of them so you can exercise both the LMI and non-LMI paths.

To initialize without wiping (e.g. after the first run, when you have real
data): call `db.init_db()` (no `reset=True`) — it's idempotent, `CREATE TABLE`
without `IF NOT EXISTS` will just fail loudly if the DB already exists, which
is the intended guardrail against accidentally wiping real data.

---

## 7. Full test scenario — verified, not simulated

`test/e2e_scenario.py` runs the entire flow you asked for against the real
app and real database (via Flask's test client — no separate server process
needed), using the actual National Grid bill and a real sample document for
the LMI upload path. Run it yourself:

```bash
cd backend
python3 test/e2e_scenario.py
```

Last run: **42/42 checks passed**, from a completely fresh database. Full
status history produced by that run:

```
             Draft -> Information Needed    (Enrollment created)
Information Needed -> Utility Bill Uploaded  (Bill file received)
Utility Bill Uploaded -> Utility Validation  (Bill extracted and reviewed)
Utility Validation -> LMI Review             (Project requires LMI qualification)
          LMI Review -> Agreement Ready      (Document packet generated)
     Agreement Ready -> Signature Pending    (Signing session created)
   Signature Pending -> Signed               (Customer completed signing session)
              Signed -> Internal Review      (Signed, routing to QA)
     Internal Review -> Verified             (QA approved)
            Verified -> Submitted            (Package generated)
           Submitted -> Developer Review     (Delivered to developer queue)
    Developer Review -> Accepted             (Looks complete.)
            Accepted -> Project Assigned     (Assigned by developer)
    Project Assigned -> Active               (Enrollment activated)
```

Along the way it verifies: real field extraction from the actual bill
(account holder, address, account number, meter number, usage all correct),
the dynamic packet correctly including the income survey only because that
project requires LMI, all 6 required signature/initial fields gating the
"complete" button until every one is signed, the merged PDF and ZIP both
downloading with correct file signatures, and the developer seeing exactly
6 signature records with the right document.

**To also try the real signing page in a browser** (not just the API): run
`app.py`, then in Python (or via the API with curl) create an enrollment
through to the point of having a signing session token, and open
`http://localhost:5000/sign/<token>`.

---

## 8. Exact files changed / created

Everything under `backend/` is new — nothing in the existing client-side
prototype (`dalton-solar-portal.html`) was modified.

```
backend/
  app.py                          Flask app factory, blueprint registration
  auth.py                         Password hashing, JWT, RBAC decorators
  helpers.py                      Masking, enrollment codes, path resolution, upload validation
  seed.py                         Test users + sample projects
  requirements.txt
  db/
    __init__.py                   SQLite connection layer
    schema_sqlite.sql             Prototype schema
    schema_postgres.sql           Migration-ready schema
  services/
    extraction.py                 Utility-bill text extraction + field parsing
    lmi_validation.py              LMI document classifier (rule-based)
    documents.py                   PDF generation (reportlab) + merging (pypdf)
    packaging.py                   ZIP packaging
    status_machine.py              Status transitions + history + audit
    audit.py                       Audit log writer
  routes/
    auth_routes.py
    enrollment_routes.py
    document_routes.py
    agreement_routes.py
    signing_routes.py
    qa_routes.py
    developer_routes.py
    submission_routes.py
    report_routes.py
    project_routes.py
  templates/
    signing_session.html          Real signing UI (see "what's not built yet" below)
  test/
    e2e_scenario.py               The verified end-to-end test
    sample_utility_bill.pdf       Copy of the real bill you supplied, for testing
    sample_lmi_doc.pdf            Sample LMI document, for testing
  uploads/                        Created at runtime — original uploaded files
  storage/generated/              Created at runtime — generated PDFs per enrollment
  storage/packages/               Created at runtime — final ZIPs
```

---

## 9. Known limitations

**Honesty about "AI-assisted" (#5):** the LMI validator is a rule-based
keyword/date classifier, not a call to a real vision or language model. It's
built to the exact contract a model-backed version would expose
(`classify → confidence, reasons, missing_info, mismatch_warnings`), so
swapping in a real model later means rewriting the inside of
`services/lmi_validation.py::validate_lmi_document()` — nothing else in the
system needs to change. It never makes the final eligibility call on its
own, per the spec.

**No frontend for QA/developer/rep portals yet.** Every workflow they need
is a real, tested API endpoint (`/api/qa/*`, `/api/developer/*`,
`/api/enrollments/*`) — what doesn't exist yet is a UI wired to them. Given
the explicit instruction to build persistence *before* changing the
signature/developer-submission workflow, this was the deliberate scope line
for this phase. The one piece of UI that does exist,
`templates/signing_session.html`, was necessary to actually exercise the
signing API — everything else was verified via the test client and is ready
to have UI built against it next.

**Extraction is tuned to National Grid's layout.** The regex patterns in
`extraction.py` were built and debugged against the actual bill you
supplied — including a real bug where the two-column layout was
interleaving the billing-period text into the customer's name and address,
which I found and fixed. Other utilities' bills will need their own
patterns added; nothing about the architecture prevents that (it's a
service-layer function, not baked into the schema).

**No rate limiting, HTTPS, or CORS configuration** — fine for local
development, not fine for anything public-facing.

**Signature drawn-image storage is local disk**, same as uploaded documents.
For real deployment this should move to object storage (S3/GCS) — the
`stored_path` column already stores a relative reference rather than
embedding bytes, so this is a swap in `document_routes.py`/`signing_routes.py`,
not a schema change.

**No email/SMS delivery.** Signing links are returned as JSON (`token`,
`expires_at`) rather than actually sent — wiring up delivery is
straightforward once you pick a provider, but wasn't in scope for the
persistence-layer phase.

**Single-server, single-process.** No background job queue for PDF
generation — it happens synchronously in the request. Fine at this volume;
would want to move to a queue before this handles real traffic.

**Reporting is counts only** — no time-series/charting, and the "signature
links created" metric counts sessions created, not links actually opened.

---

## 10. Phase 2 — Frontend integration (steps 1–9 of 18)

Connects the existing PowerMarket-style HTML prototype to this backend.
Per the phase-2 instructions, this stops after step 9 (dashboard/projects/
enrollments live) — the enrollment wizard itself (steps 10-15) is not yet
connected. Everything below is real and tested; nothing is a mockup.

### What changed

| Files | Why |
|---|---|
| `templates/index.html` (new) | The frontend HTML, moved here from the standalone prototype so Flask can serve it. Two ids added (`login-error`, `login-submit-btn`, `sidebar-role-label`) to support real auth error display and live user info — no visual/layout changes. |
| `static/css/app.css` (new) | The prototype's `<style>` block, extracted verbatim. One new rule added: `.status-danger`, using the existing `--danger`/`--danger-tint` tokens, for Rejected/Needs Work status pills that didn't exist in the old fake status vocabulary. |
| `static/js/app.js` (new) | The prototype's app logic, extracted and then modified — see below. |
| `app.py` | Added `GET /` rendering `templates/index.html`. |
| `routes/enrollment_routes.py` | **Security fix, found while testing this phase**: `_serialize_enrollment()` was doing `SELECT *` on `customers` and returning the row as-is — every enrollment API response included the customer's `password_hash`. Fixed to select only the safe columns. This is a Phase 1 bug, only caught because Phase 2 was the first time an API response was actually inspected end-to-end for frontend consumption. |
| `test/verify_frontend_integration.py` (new) | Proves the API contract the new JS depends on — see "How to test" below. |

### app.js changes in detail

- **New:** `AuthStore` (sessionStorage-backed token storage), `apiFetch()` (fetch wrapper — attaches the bearer token, handles 401 by clearing the session and returning to login), `applyCurrentUserToUI()`, `tryRestoreSession()` (runs on page load, restores a session if a valid token is already in sessionStorage).
- **Replaced:** `doLogin()` — now calls `POST /api/auth/login` for real, shows the actual error message on failure (wrong password no longer works — previously any non-empty input logged in), shows a loading state on the button.
- **Replaced:** `renderDashboard()`, `renderProjects()`'s data source, `renderCustomers()` — now load from `GET /api/projects` and `GET /api/enrollments` instead of hardcoded arrays. The 5 dashboard stat cards keep their exact original layout but now group the real 18-status state machine into 5 buckets (`DASH_BUCKETS` — In Progress / Signature Pending / In Review / Submitted / Active) instead of the old 5 fake statuses.
- **Removed:** the hardcoded `projects` array (3 fake projects), `mkSeed()`, and the 5 fake seeded customers (Maria Castillo, Derek Whitfield, etc.) — these only existed to make the old disconnected prototype look populated. A fresh database now correctly shows zero enrollments instead of fake demo data.
- **Fixed:** a real type-mismatch bug this migration would have introduced — project ids are now real numeric database ids instead of string slugs (`'cobblestone-ridge'`), but `startWizardForProject()` receives the id as a string (it comes from an HTML `onclick` attribute). Fixed the comparison to be type-safe.
- **Untouched, deliberately:** the enrollment wizard (`state`, `submitBill`, `submitContact`, `submitLmi`, etc.), `doCustomerLogin()`, and `resumeCustomer()` still read/write the local `customers` array exactly as before. They're not connected yet — that's steps 10-15. `customers` now starts empty instead of pre-seeded, so the wizard's "Resume" and the customer-login demo won't find anything until that phase.

### How to test

Automated (proves the contract without a browser):
```bash
cd backend
python3 test/verify_frontend_integration.py   # Phase 2 checks — 45 assertions
python3 test/e2e_scenario.py                  # Phase 1 regression — 42 assertions, confirms nothing broke
```

Manual (the real browser click-through — do this yourself, I can't drive a browser from here):
```bash
python3 app.py
# open http://localhost:5000
```
1. Try logging in with a wrong password → should show a red error message inline, not log you in.
2. Log in with `charlie@daltonsolar.com` / `RepPass1!` → dashboard loads, sidebar shows "Charlie Mren" / "Role: sales rep" (not the old hardcoded "Charlie Mren" / "Roles: Agent" — same name here coincidentally, but now it's real).
3. Dashboard stat cards should all read 0 (fresh database, no fake data).
4. Projects tab should show the 3 real seeded projects (Cobblestone Ridge, Birchfield Commons, Otter Creek Solar) — same names as before, but now loaded from the database.
5. Refresh the page — you should stay logged in (session restore via `tryRestoreSession()`).
6. Click Logout, then try navigating back — you should be at the login screen, and the browser's sessionStorage should no longer have a token (check DevTools → Application → Session Storage).
7. To see a real enrollment on the dashboard: run `python3 test/e2e_scenario.py` first (creates one real enrollment), then log in — the recent-enrollments table and stat cards should reflect it.

### What remains disconnected (steps 10–18, not started)

- The enrollment wizard (project select → customer → bill upload → LMI → agreement → send) still runs entirely in local browser memory. Clicking "+ New enrollment" does not create a real backend enrollment yet.
- Utility bill upload in the wizard does not call the real `/documents` endpoint or real extraction — still the old client-side PDF.js/Tesseract.js simulation.
- LMI workflow in the wizard doesn't call `/api/enrollments/:id/lmi`.
- Agreement generation, signing, QA, developer, and reporting screens are all still the original disconnected prototype UI.
- The customer-facing signing page (`/sign/<token>`, `templates/signing_session.html`) is real and connected — but nothing in the rep wizard can create a real signing session yet, since the wizard doesn't create real enrollments.


---

## 11. Perch refactor — Milestone 1

Full architecture review is in **`ARCHITECTURE_REVIEW.md`** (8 sections, as
requested). This section covers the Milestone 1 implementation only.

### Files modified / created

**New:**
| File | Purpose |
|---|---|
| `ARCHITECTURE_REVIEW.md` | The 8-section review: what stays, what goes, schema changes, adapter design, obsolete logic, file structure, sequencing, open questions for Perch. |
| `db/migrate.py` | Additive migration runner with a `schema_migrations` ledger. Phase 1 had none — `init_db()` wiped the database, unacceptable once real enrollments exist. |
| `db/migrations/001_perch_integration.sql` | Perch tables + enrollment correlation columns. |
| `services/perch/config.py` | Env-driven client factory. **The only place Perch credentials are read.** |
| `services/perch/client.py` | `PerchClient` ABC + `PerchHTTPClient` (real, untested against live). |
| `services/perch/mock_client.py` | `PerchMockClient` — realistic fixtures, same interface. |
| `services/perch/token_manager.py` | Server-side token lifecycle + automatic refresh. |
| `services/perch/adapter.py` | The only module routes may import. Token, dispatch, persistence, normalization. |
| `services/perch/errors.py` | Typed failure hierarchy. |
| `routes/perch_routes.py` | Browser-facing Perch endpoints. |
| `test/test_perch_milestone1.py` | 99 assertions covering every deliverable. |

**Modified:**
| File | Change |
|---|---|
| `app.py` | Registered the Perch blueprint. |
| `db/__init__.py` | `init_db()` now runs migrations after the base schema. |
| `seed.py` | **Stopped seeding invented projects** — Perch owns products. Legacy seeding moved to `seed_legacy_projects()`, called only by the pre-Perch e2e test. |
| `templates/index.html` | Project-picker step replaced with the ZIP/utility + dynamic product screen. No other visual change. |
| `static/css/app.css` | Appended styles for the capacity screen, using existing design tokens only — no new colors. |
| `static/js/app.js` | `startWizardFresh()` now creates a real draft then calls Perch. `buildProjectPicker()`/`selectProject()` removed. |
| `test/e2e_scenario.py`, `test/verify_frontend_integration.py` | Updated to reflect that fake projects no longer exist. |

### Database migrations

`001_perch_integration.sql` — additive only, nothing dropped:
- **New tables:** `perch_tokens`, `perch_api_calls`, `perch_capacity_snapshots`, `perch_products`
- **`enrollments` gains:** `service_zip`, `utility_name`, `selected_perch_product_id`, `perch_enrollment_ref`, `perch_customer_ref`

Apply: `python3 -m db.migrate` (or just `python3 seed.py`, which runs them).
Idempotent — safe to re-run; there's a test asserting that.

### API endpoints added

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/api/perch/drafts` | rep, admin | Create draft + issue Dalton Enrollment ID **before any Perch call** |
| GET | `/api/perch/utilities` | any | Utility options (**mocked**) |
| POST | `/api/perch/enrollments/:id/capacity` | rep, admin | ZIP + utility → Perch capacity; persists everything |
| GET | `/api/perch/enrollments/:id/capacity` | any | Last snapshot, flagged `stale: true` |
| GET | `/api/perch/enrollments/:id/api-calls` | any | Perch interaction history (QA/audit) |
| GET | `/api/perch/token-status` | **admin only** | Expiry + mode. Never the token value. |

### How to run

```bash
cd backend
pip install -r requirements.txt
python3 seed.py        # runs migrations + seeds users (no projects any more)
python3 app.py         # http://localhost:5000
```
Defaults to `PERCH_API_MODE=mock`. To point at Perch staging later — **no code changes**:
```bash
export PERCH_API_MODE=live
export PERCH_BASE_URL=https://staging.api.perchenergy.com
export PERCH_CLIENT_ID=...
export PERCH_CLIENT_SECRET=...
```

### How to test

```bash
python3 test/test_perch_milestone1.py      # 99 assertions — Milestone 1
python3 test/e2e_scenario.py               # 42 — Phase 1 regression
python3 test/verify_frontend_integration.py # 46 — Phase 2 regression
```
All three green as of this commit.

Manual: log in as `charlie@daltonsolar.com` / `RepPass1!` → **+ New enrollment**.
An `ENR-…` code appears immediately (draft created before any Perch call). Enter
ZIP `13348` + National Grid → two products render with live-from-response
customer type, savings, capacity, proof docs, and next step. Try `11550` +
PSEG Long Island (different utility/rules), `99999` (no capacity), `00000`
(upstream failure handling).

### What remains mocked

- **All Perch responses.** `PerchMockClient` returns fixtures. Endpoint paths, request/response schemas, and every enum value are **assumptions** — see `ARCHITECTURE_REVIEW.md` §8.
- **The utility list** (`/api/perch/utilities`) comes from the mock catalog; it should come from Perch.
- **`PerchHTTPClient` has never run against a live endpoint.** Its shapes encode our guesses.

### Blocking questions for Perch before Milestone 2

Full list in `ARCHITECTURE_REVIEW.md` §8. The four that gate Milestone 2:
1. **OAuth details** — endpoint, grant type, scopes, TTL, refresh-token vs re-auth.
2. **Capacity endpoint** — exact path, request schema, response schema.
3. **`next_step` vocabulary** — this drives all dynamic rendering; we're branching on a guessed string.
4. **`customer_type` / `proof_document_type` enums** — real values.

Highest-risk unknown overall is **contract retrieval and signing** (§8 Q6) — deliberately sequenced at Milestone 3 to contain the blast radius.

### Not implemented (correctly out of scope)

Contract signing and final enrollment submission, per the stop instruction. Also still on the legacy path: OCR field expansion (POD ID), QA/developer/signing/submission routes reading the legacy `projects` table, and VIPR reconciliation.

---

## 12. Roadmap (updated after the Perch engineering call)

The Perch API docs and engineering call reclassified most of our original API
assumptions. Full analysis: **`PERCH_API_RECONCILIATION.md`**.
`ARCHITECTURE_REVIEW.md` §8 is superseded and marked as such.

| Milestone | Status | Scope |
|---|---|---|
| M1 | **Done** | Draft creation, rep association, token lifecycle, adapter layer, mocked capacity, persistence, dynamic rendering |
| **M2** | **Next** | **Correct the Perch contract + hypermedia workflow engine.** Real `/token` + `PATCH /refresh_token` + `X-Enrollment-Token` + 30-min TTL; real capacity request/response; utility slug registry; 403 refresh-and-retry and 503 no-capacity semantics; `next_step`-URL-driven workflow state; mock rewritten to documented schemas; migration `002` |
| M3 | Planned | `POST /enroll` submission, POD ID validation, customer data mapping. **OCR field expansion folds in here** (previously a standalone M2) |
| M4 | Planned | Contract retrieval + `Submit Contracts Acceptance`; retire local contract generators |
| M5 | Planned | LMI (Perch-directed method) + proof documents + SharePoint resubmission workflow |
| M6 | Planned | `GET /status`, VIPR reconciliation, rep attribution export, funnel analytics |
| M7 | Planned | Remove customer portal/credentials, drop `projects`, S3, Postgres, AWS |

### Key corrections driving the reorder

- **`next_step` is a URL, not an enum.** The workflow is hypermedia-driven — Perch owns the state machine. Our fixed step sequence is UX only.
- **Capacity returns no product list.** It returns one `project_details` object with three availability booleans and two savings rates. Rep-side product selection does not exist; Perch assigns projects by priority.
- **Auth is `X-Enrollment-Token` (UUID, 30-min TTL) with a dedicated `PATCH /refresh_token`** — not OAuth2 Bearer. A second scheme (HMAC) covers pre-enrollment `GET /markets/capacity`.
- **Perch creates customer accounts and sends all customer comms.** Our customer portal/login is obsolete and scheduled for removal.
- **Partners do not select the LMI method** — Perch determines it per project.
- **503 means "no capacity"** (a business outcome), not a transport error.

### What was validated

The adapter/ABC boundary, Dalton-Enrollment-ID-first, server-side token containment, the `perch_api_calls` audit spine, snapshot-for-audit-never-as-cache, no local contract library, no local project mirror, internal rep attribution, and Dalton-owned OCR. Structure survived; contract details didn't — which is what the adapter layer existed to make survivable.

---

## 13. Milestone 2 — the documented Perch contract

Companion docs: **`PERCH_API_RECONCILIATION.md`** (why this was needed),
**`PERCH_OPEN_ITEMS.md`** (what's still unresolved),
**`WALKTHROUGH_MILESTONE2.md`** (what a rep experiences, call by call).

### Architectural decisions

**1. The frontend became a renderer, not a page sequence.**
Perch's API is hypermedia-driven — every response carries a `next_step` URL, and
the docs are explicit that partners follow it rather than deciding for themselves.
So `templates/index.html` now contains one empty `<div id="workflow-root">`. The
backend resolves a *step descriptor* (fields, validations, panels, actions) and
`app.js` renders it generically. Adding Milestone 3's enroll step is a backend
change — a new builder in `services/perch/workflow.py` — with zero frontend edits.

**2. Unrecognized `next_step` URLs fail loudly.**
We've only ever seen one value. An unknown URL is audit-logged, surfaced in the
UI, and listed in `GET /api/perch/diagnostics` — rather than silently skipping a
step Perch expects.

**3. HTTP status codes carry business meaning.**
503 on capacity means "no capacity here" — we return **200** with
`capacity_available: false`, because it's a business outcome, not a failure.
403 means "token expired" — we refresh and retry once, per Perch's guidance. A
genuine 5xx stays an error and is distinguishable from either.

**4. Tokens are per-enrollment, not global.**
Perch's enrollment token is session-scoped with a 1-hour TTL, so it can't be
a singleton. Refresh is both proactive (2-minute skew) and reactive (on 403).

**5. Utility slugs are reference data, not free text.**
A display-name/slug mismatch fails silently at Perch. `perch_utilities` holds
the published mapping; dropdown values are always slugs; inferred slugs are
flagged `slug_confirmed = 0` and surfaced in diagnostics.

**6. We refuse to guess.** `GET /markets/capacity` raises
`PerchNotImplementedError` because the HMAC scheme is unpublished.

### Files changed

**New:** `db/migrations/002_perch_documented_contract.sql`,
`services/perch/utilities.py`, `services/perch/workflow.py`,
`test/test_perch_milestone2.py`, `PERCH_OPEN_ITEMS.md`, `WALKTHROUGH_MILESTONE2.md`

**Rewritten:** `services/perch/client.py`, `mock_client.py`, `token_manager.py`,
`adapter.py`, `config.py`, `errors.py`, `routes/perch_routes.py`; the workflow
section of `static/js/app.js`; the wizard's first step in `templates/index.html`

**Modified:** `db/migrate.py` (FK-safe migrations + `foreign_key_check`),
`static/css/app.css` (generic renderer styles, existing tokens only)

**Retired:** `test/test_perch_milestone1.py` — its assertions encoded the guessed
contract. Every still-valid behaviour it covered was ported into the M2 suite;
the original is kept as `_superseded_test_perch_milestone1.py.txt` so the
assumption-vs-reality comparison stays auditable.

### Migration 002

Adds `perch_utilities` (seeded from the published slug mapping and POD ID table),
`perch_capacity_checks` (the documented `project_details` shape),
`perch_workflow_state`, and per-enrollment token columns. **Drops `perch_products`**
and rebuilds `enrollments` to remove the orphaned `selected_perch_product_id` FK.

> A real bug found during this work: dropping `perch_products` while
> `enrollments` still had a foreign key to it made every subsequent enrollment
> INSERT fail with `no such table`. Fixed with the documented SQLite
> create-copy-drop-rename rebuild, and `db/migrate.py` now disables FK
> enforcement during migrations and runs `PRAGMA foreign_key_check` afterwards.

### API endpoints

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/api/perch/drafts` | rep, admin | Dalton Enrollment ID before any Perch call |
| GET | `/api/perch/utilities` | any | Slugs + POD ID rules |
| GET | `/api/perch/enrollments/:id/workflow` | any | **Current step descriptor** |
| POST | `/api/perch/enrollments/:id/capacity` | rep, admin | `POST /capacity` + next step |
| GET | `/api/perch/enrollments/:id/capacity` | any | Last check (flagged `stale`) |
| GET | `/api/perch/enrollments/:id/api-calls` | any | Perch audit trail |
| GET | `/api/perch/enrollments/:id/token-status` | **admin** | Expiry only, never the token |
| GET | `/api/perch/diagnostics` | **admin** | Inferred values + known next_step paths |

### How to test

```bash
python3 test/test_perch_milestone2.py       # 142 assertions
python3 test/e2e_scenario.py                # 42 — Phase 1 regression
python3 test/verify_frontend_integration.py # 46 — Phase 2 regression
```
All green. Manual walkthrough: `WALKTHROUGH_MILESTONE2.md`.

### What remains mocked

Every Perch response. No call has been made against staging. Most likely
first-contact failures: the API key header name (Q2) and the NYSEG slug (Q5).

### Not started (later milestones)

OCR expansion, contract signing, enrollment submission, VIPR sync, customer
communications, magic links, LMI resubmission.


---

## 14. OpenAPI spec alignment (pre-staging)

Perch supplied the official OpenAPI specification. The codebase was audited
against it and low-risk required corrections were applied.

**Read:** `PERCH_STAGING_READINESS.md` (audit) and
`FRIDAY_STAGING_TEST_PLAN.md` (validation order).

### Running tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest                            # all suites, no special flags
python test/test_perch_milestone2.py        # standalone (188 assertions)
python test/e2e_scenario.py                 # standalone (42)
python test/verify_frontend_integration.py  # standalone (46)
```

### Fixes applied

| Area | Change |
|---|---|
| HMAC-SHA256 | New `services/perch/hmac_auth.py`; verified against `openssl` |
| `POST /token` | Now HMAC-signed with `{"email": ...}` |
| `PATCH /refresh_token` | HMAC-signed and email-keyed (was token-keyed) |
| Token expiry | Uses Perch's `expires_at`, not our clock |
| Headers | `X-API-Key` (was `X-Api-Key`) |
| Utilities | Added `pse-g-ny`; `nyseg` confirmed; account-number lengths added |
| Uploads | 4 MB limit (Perch returns 413 above) |
| Reference data | 9 `source_type` values recorded |
| Status codes | 401 / 403 / 404 / 413 / 422 handled distinctly |
| Workflow | `email` collected first (required by `/token`) |

### Developer-experience fixes

- `requirements-dev.txt` with pytest
- UTF-8 on all text reads — `seed.py` and the suites previously crashed on Windows without `-X utf8`
- Retired M1 stub removed from pytest discovery (its `sys.exit(0)` broke collection)
- `conftest.py`, `pytest.ini`, `test/test_suites.py` so `python -m pytest` works with no flags

### Environment for staging (do not commit)

```bash
export PERCH_API_MODE=live
export PERCH_ENROLLMENT_BASE_URL=https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments
export PERCH_MARKETS_BASE_URL=https://staging.api.perchenergy.com/affiliate_partners/v1/markets
export PERCH_API_KEY=...
export PERCH_SECRET_KEY=...
```
