# Architecture Review — Refactoring Around the Perch Partner Enrollment API

> **SUPERSEDED IN PART — read `PERCH_API_RECONCILIATION.md` first.**
>
> Sections 1-7 of this document (what stays, what goes, adapter design, file
> structure) remain accurate and were validated by the Perch engineering call.
>
> **Section 8 (assumptions and open questions) is obsolete.** It was written
> before we had the API docs, and most of its guesses about endpoints, auth,
> token TTL, and response shapes turned out wrong. The reconciliation report
> classifies every one of those assumptions against the real contract and
> replaces the question list.
>
> The roadmap in §7 has also been reordered — see the reconciliation report §6.

Written against the repository as it stands after Phase 2 (2,550 lines of
Python across 24 modules, 1,829 lines of frontend). Every claim below is
grounded in an actual inspection of the code, not from memory.

---

## 1. Which existing code should remain

**Keep unchanged — this is Dalton-owned and unaffected by Perch:**

| Component | Files | Why it stays |
|---|---|---|
| Rep authentication | `auth.py`, `routes/auth_routes.py` | Dalton owns rep identity. Perch never sees a Dalton rep login. |
| Internal enrollment IDs | `helpers.py::next_enrollment_code` | `ENR-YYYY-NNNNNN` is the immutable Dalton key that everything else reconciles against. This becomes *more* important, not less. |
| Status machine | `services/status_machine.py` | Dalton owns enrollment UX state. Needs new Perch-aware statuses (§3) but the mechanism is right. |
| Audit logging | `services/audit.py`, `audit_logs` table | Required by the new architecture ("store every request, response, identifier, timestamp, and error"). |
| Status history | `status_history` table | Rep/QA attribution stays in Dalton. |
| Utility bill OCR | `services/extraction.py` | Dalton owns OCR. Needs field additions (POD ID) — see §3. |
| LMI OCR + validation assist | `services/lmi_validation.py` | Dalton owns validation *assistance*; Perch owns the actual proof requirement. Keep the classifier, stop treating its output as the eligibility decision. |
| QA workflow | `routes/qa_routes.py`, `qa_reviews` | Dalton owns QA. Unaffected. |
| Reporting | `routes/report_routes.py` | Dalton owns reporting. |
| Document storage | `documents` table, `routes/document_routes.py` | Dalton stores the source documents (bills, LMI proofs) and forwards them to Perch. |
| Signature certificate + audit trail | `services/documents.py::generate_signature_certificate`, `signatures`, `signature_events` | Dalton-side signing evidence remains valuable for QA and dispute resolution even when the *contract* comes from Perch. |
| Frontend shell, auth, dashboard | `static/js/app.js` Phase 2 work, `templates/index.html`, `static/css/app.css` | Visual design preserved per instruction. |
| Both test suites | `test/e2e_scenario.py`, `test/verify_frontend_integration.py` | Regression safety net during the refactor. |

---

## 2. Which code should be removed or refactored

### Obsolete — Perch now owns this

| Code | Current behavior | Disposition |
|---|---|---|
| `projects` table as source of truth | Hardcoded Cobblestone Ridge / Birchfield Commons / Otter Creek with local `savings_pct`, `capacity_pct_full`, `spots_left`, `lmi_required` | **Demote, don't delete.** Referenced in 9 places (`enrollment_routes`, `agreement_routes`, `qa_routes`, `developer_routes`, `signing_routes`, `submission_routes`, `project_routes`, `seed.py`). Deleting it now breaks QA and developer flows that are already tested and working. Correct move: stop *writing* to it as authority, add Perch-backed tables alongside, migrate call sites one at a time, drop it in a later milestone. Ripping it out in Milestone 1 would be a large, untested, all-at-once change — exactly the risk the incremental instruction is meant to avoid. |
| `seed.py` project seeding | Seeds 3 fake NY projects | Refactor to seed **users only**. Projects now come from Perch. (Doing this in Milestone 1.) |
| `routes/project_routes.py` | `GET /api/projects` from local table | Superseded by `GET /api/perch/capacity`. Keep the old endpoint alive during transition so the existing dashboard doesn't break; deprecate once the wizard is fully Perch-driven. |
| `services/documents.py` contract generators — `generate_subscription_agreement`, `generate_cdg_disclosure`, `generate_income_survey`, `generate_consent_doc` | Generates Dalton's own PDFs of the Perch/Solstice contracts using reportlab | **This is the local contract library and it is now obsolete.** Perch returns the correct contract PDF. Keep the functions in the tree but stop calling them once contract-fetch lands (Milestone 3) — they're the fallback if Perch contract retrieval is unavailable. Delete after Perch contract fetch is proven in staging. |
| `routes/agreement_routes.py::DOC_SPECS` | Hardcoded 5–6 document packet, with `income_survey` inserted based on the local `projects.lmi_required` flag | Obsolete. Contract selection is a Perch decision driven by product + utility + LMI path. Replace with a Perch contract-fetch call in Milestone 3. |
| `lmi_validation.py::AMI_TABLE` | Hardcoded NY 80% State Median Income table | **Verify with Perch before trusting.** If Perch's pre-enrollment response returns income thresholds, use theirs. If not, this stays but needs an annual-update owner. Flagged in §8. |

### Refactor — right idea, wrong shape for the new architecture

- **`enrollments` table** needs Perch correlation columns (§3). Currently has no way to answer "what is this enrollment in Perch's system?"
- **`services/extraction.py`** — add POD ID extraction (required by the new workflow, absent today).
- **`status_machine.py`** — the current 17 statuses model a Dalton-only lifecycle. Needs Perch-interaction states.

---

## 3. Database schema changes

**New tables (Milestone 1):**

```
perch_tokens              Server-side only. Token, expiry, scope. Never leaves the backend.
perch_api_calls           Every request/response/error against an enrollment. The audit spine.
perch_capacity_snapshots  Raw API response, stored for audit. Never used as live truth.
perch_products            Normalized products from a snapshot, so the UI can render them
                          and an enrollment can reference a selection.
```

**Altered (Milestone 1):**

```
enrollments
  + service_zip                TEXT
  + utility_name               TEXT
  + selected_perch_product_id  INTEGER FK -> perch_products(id)
  + perch_enrollment_ref       TEXT   (Perch's own ID, once they issue one)
  + perch_customer_ref         TEXT   (Perch's customer ID, once created)
```

**Deferred to later milestones (documented now so the shape is agreed):**

```
utility_accounts  + pod_id TEXT                    (Milestone 2, OCR)
perch_contracts    (enrollment_id, perch_contract_id,
                    document_id, fetched_at)        (Milestone 3)
vipr_reconciliation (enrollment_id, vipr_status,
                     payable, last_synced_at)       (Milestone 5)
```

All new DDL is written for both SQLite and PostgreSQL. Postgres compatibility
is preserved — see `db/migrations/`.

**Migration mechanism:** the repo had no migration runner (Phase 1 just ran
`schema_sqlite.sql` fresh, which is destructive). That's not acceptable once
real enrollment data exists. Milestone 1 adds a proper additive migration
runner with a `schema_migrations` ledger.

---

## 4. New API adapter/service architecture for Perch

```
services/perch/
  config.py         Env-driven mode selection (mock | live), base URL, credentials.
  client.py         PerchClient ABC + PerchHTTPClient (real, uses requests).
  mock_client.py    PerchMockClient — realistic fixtures, same interface.
  token_manager.py  get_valid_token(): returns a cached unexpired token or
                    requests+stores a new one. Refresh is fully server-side.
  adapter.py        The ONLY thing routes talk to. Handles: token acquisition,
                    client dispatch, API-call persistence, response
                    normalization, error translation.
  errors.py         PerchError hierarchy so routes handle failures uniformly.
```

**The contract that makes mock→live a config change, not a code change:**

`get_perch_client()` in `config.py` returns either implementation based on
`PERCH_API_MODE`. Both satisfy the same ABC. `adapter.py` — and therefore
every route, the entire frontend, and all workflow logic — is written against
the ABC and never knows which is in play. Swapping to Perch staging is
setting two env vars.

**Credential containment:** `PERCH_CLIENT_ID` / `PERCH_CLIENT_SECRET` are read
only in `config.py`, used only in `token_manager.py`, and the token is stored
only in `perch_tokens`. No Perch credential or token is ever serialized into
any API response. There is a test asserting this.

---

## 5. Which local project and contract logic is now obsolete

**Obsolete now (Milestone 1 addresses):**
- Hardcoded project array in `seed.py` — Perch owns products.
- `projects.savings_pct`, `.capacity_pct_full`, `.spots_left`, `.lmi_required` as authority — all four are Perch-owned facts.
- Frontend `loadProjects()` reading `/api/projects` for the *enrollment* path (dashboard display can keep it during transition).

**Obsolete at Milestone 3 (contract fetch):**
- `DOC_SPECS` packet definition.
- All four contract-generating functions in `services/documents.py`.
- `agreements.template_version` as a Dalton-managed version — becomes Perch's contract identifier.

**NOT obsolete, despite superficial similarity:**
- `generate_cover_sheet` / `generate_signature_certificate` — these are Dalton
  audit artifacts, not contracts. Keep.
- `packaging.py` ZIP builder — still needed for QA export and internal records.

---

## 6. Proposed production file structure

```
backend/
  app.py
  config.py                    ← NEW: centralized env config (currently scattered)
  auth.py
  helpers.py
  db/
    __init__.py
    schema_sqlite.sql
    schema_postgres.sql
    migrate.py                 ← NEW: migration runner
    migrations/
      001_perch_integration.sql
  services/
    audit.py
    status_machine.py
    extraction.py              (Dalton OCR)
    lmi_validation.py          (Dalton assist)
    documents.py               (audit artifacts; contract gens deprecated M3)
    packaging.py
    perch/                     ← NEW
      __init__.py
      config.py
      errors.py
      client.py
      mock_client.py
      token_manager.py
      adapter.py
  routes/
    auth_routes.py
    enrollment_routes.py
    document_routes.py
    perch_routes.py            ← NEW
    qa_routes.py
    developer_routes.py
    report_routes.py
    signing_routes.py          (M3: Perch contracts)
    submission_routes.py       (M4: Perch submission)
    agreement_routes.py        (deprecated M3)
    project_routes.py          (deprecated once wizard is Perch-driven)
  static/ templates/ test/
```

---

## 7. Recommended implementation sequence

| Milestone | Scope | Gate before proceeding |
|---|---|---|
| **1 (this iteration)** | Draft creation, rep association, token management + refresh, ZIP/utility entry, adapter layer, mocked capacity endpoint, response persistence, dynamic rendering | Mock capacity round-trips end-to-end; tests green |
| 2 | OCR field expansion (POD ID, meter), rep review/confirm screen, Perch validation-rule handling | Real bill extraction produces every field Perch requires |
| 3 | Perch contract fetch + present + sign; retire local contract library | Perch returns a contract PDF in staging |
| 4 | Final enrollment submission + document upload to Perch; error recovery; LMI resubmission without history loss | Perch staging accepts a full enrollment |
| 5 | VIPR reconciliation, payment reporting, rep attribution matching | VIPR access + schema confirmed |

**Sequencing rationale:** each milestone leaves the system in a working,
tested state, and no milestone requires rewriting the previous one. The
adapter boundary is built first (M1) precisely so M2–M4 don't touch frontend
or workflow code when the real API arrives.

---

## 8. Risks, assumptions, and open questions for Perch engineering

### Assumptions made in the mock (must be confirmed before Milestone 2)

1. **Auth**: OAuth2 client-credentials, `POST /oauth/token`, returns
   `{access_token, expires_in, token_type}`. Mock assumes 3600s expiry.
2. **Capacity**: `POST /v1/pre-enrollment/capacity` with `{zip_code, utility}`
   returns a list of products with `product_id`, `name`, `customer_type`,
   `savings_percentage`, `available_capacity_kw`, `lmi_required`,
   `proof_documents_required[]`, `next_step`.
3. **Customer types** are an enum (`residential` / `commercial` / `lmi`) —
   real values unknown.
4. **`next_step`** is a string the client branches on. Vocabulary unknown.

### Open questions — blocking Milestone 2+

| # | Question | Blocks |
|---|---|---|
| 1 | Exact OAuth endpoint, grant type, scopes, token TTL, and whether refresh tokens exist or it's re-auth each time | Token refresh correctness (M1 works either way; correctness needs this) |
| 2 | Capacity endpoint path, request schema, response schema, pagination | M2 |
| 3 | Complete `next_step` vocabulary and the state machine it implies | M2 — this drives dynamic rendering |
| 4 | Complete `customer_type` and `proof_document_type` enums | M2 |
| 5 | Is capacity a reservation or a read? If a read, what's the race condition on final submit, and what error is returned when capacity is gone? | M4 — "always revalidate before final submission" needs to know what failure looks like |
| 6 | Contract retrieval: endpoint, is the PDF pre-filled with customer data or does Dalton fill it, and what signing methods are supported (embedded? redirect? Dalton-side with signature upload?) | M3 — **highest-uncertainty item** |
| 7 | Document upload: multipart or base64, size limits, accepted MIME types, per-document-type endpoints? | M4 |
| 8 | Does the Partner API expose an agent/rep ID field? (Instruction #16 assumes not — confirming would let us drop the internal-only workaround) | M5 reconciliation |
| 9 | Idempotency: is there an idempotency key? What happens on a retried submission after a network timeout? | M4 error recovery |
| 10 | LMI proof rejection: is it a webhook, a polled status, or VIPR-only? What identifiers correlate it back to our enrollment? | M4 resubmission flow |
| 11 | Does Perch return income thresholds for LMI self-attestation, or do we keep maintaining the NY SMI table locally? | M2 |
| 12 | Rate limits, and staging environment availability/credentials | All |

### Risks

- **Contract signing (Q6) is the largest unknown.** If Perch requires a
  redirect or hosted signing flow, the Dalton-side signature UI built in
  Phase 1 becomes evidence-capture only, not the signing mechanism. Sequencing
  it at M3 rather than earlier limits the blast radius.
- **Capacity race on submission (Q5)** — an enrollment can be fully signed and
  then fail at submit because capacity vanished. Needs a defined recovery UX.
- **Dual-write during transition** — while `projects` still exists alongside
  `perch_products`, there's a window where two things describe "the project."
  Mitigated by making `projects` read-only-legacy immediately (seed.py stops
  populating it in M1) and deleting it at M4.
- **Token refresh under concurrency** — 40 reps hitting an expired token
  simultaneously could stampede the token endpoint. M1 implementation
  serializes refresh with a DB-level guard; revisit if Perch rate-limits.
