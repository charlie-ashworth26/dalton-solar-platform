# PERCH_STAGING_READINESS.md

Audit of the entire codebase against Perch's **official OpenAPI specification**
(`openapi.yaml`, Partner Enrollment API in NY v1, OAS 3.0).

**The spec is treated as the source of truth throughout.** Where our
implementation disagreed with it, our implementation is wrong.

No connection to staging was made. No credentials were used. No environment
variables were changed.

**Test status after this audit:** 188 / 42 / 46 assertions passing across the
three active suites.

---

## ✅ Already Correct

Confirmed correct against the spec — no change required.

| Area | Verified against |
|---|---|
| **`POST /capacity` request shape** — `{"zip_code", "utility_name"}`, slug value | `capacityRequest`, endpoint description |
| **`POST /capacity` response shape** — the six `project_details` fields, exact names, plus `next_step` | `enrollmentCapacityResponse` + example |
| **`next_step` is a URL, not an enum** — our workflow engine resolves it by path suffix | Spec: *"Follow the `next_step` URL in each API response"* |
| **`X-Enrollment-Token` header** for enrollment-session endpoints | `enrollment_token_auth` security scheme |
| **Enrollment-session calls send NO HMAC headers** | Spec: *"no HMAC headers are needed for these calls"* |
| **30-minute token expiry** | `enrollmentTokenResponse.expires_at` description |
| **403 → refresh → retry once** on session endpoints | `ForbiddenEnrollmentSession` |
| **503 on `/capacity` = no capacity**, treated as a business outcome, blocks progression to enroll | Endpoint description: *"Partners should not proceed to `POST /enroll` until this endpoint succeeds"* |
| **Utility slug mapping** — all 5 originally-known slugs correct | Slug mapping table |
| **POD ID rules** — NYSEG 15/`N01`, Central Hudson 10, Rochester 15/`R01` | Secondary identifier validation table |
| **Base URLs** — enrollment vs markets split | `servers` + info block |
| **Two capacity endpoints serve different purposes** | Capacity Endpoints table |
| **Perch owns customer accounts, comms, and contracts** | Enrollment flow description |
| **Draft-before-Perch-call ordering** (Dalton Enrollment ID is the durable key) | Implied by 30-min session token |
| **Audit spine** (`perch_api_calls`) records every request/response/error | Our requirement; unaffected |
| **Workflow engine + renderer-first frontend** | Validated — the new `email` field required **zero** frontend changes |

**Notable:** our *inferred* `nyseg` slug was **confirmed correct** by the spec.

---

## ⚠ Needs Updating

### FIXED IN THIS PASS

---

**M-1. HMAC-SHA256 authentication was entirely missing** 🔴 **CRITICAL**

- **File / function:** `services/perch/client.py` → `_auth_headers()`, `request_token()`, `refresh_token()`
- **Current behavior (before):** sent `X-Api-Key` only. No signature, no timestamp.
- **Required behavior:** `POST /token`, `PATCH /refresh_token`, and `GET /markets/capacity` require `X-API-Key`, `X-HMAC-Signature` (lowercase hex SHA-256, 64 chars), and `X-HMAC-Timestamp` (Unix, ±5 min). Signed payload is `timestamp + "\n" + body`.
- **Severity:** **Critical** — the first call of the flow would have failed 401.
- **Fix applied:** new `services/perch/hmac_auth.py`. Cross-verified against `openssl dgst -sha256 -hmac` using the spec's own procedure; both the JSON-body and canonical-query-string variants match byte-for-byte.

---

**M-2. `POST /token` requires the customer's email** 🔴 **CRITICAL**

- **File / function:** `services/perch/client.py::request_token()`, `token_manager.issue_new_token()`, `workflow._step_service_area()`
- **Current behavior (before):** posted an empty body `{}`; email was not collected until a later step.
- **Required behavior:** body is `{"email": "..."}`; 422 if invalid.
- **Severity:** **Critical**, and *architectural* — the email must be captured **before any Perch call**, which reorders the workflow.
- **Fix applied:** `email` is now the first field of the `service_area` step, persisted to `enrollments.perch_token_email` before the token call. The renderer required no changes — the field appeared from the descriptor alone.

---

**M-3. `PATCH /refresh_token` used the wrong auth and wrong key** 🔴 **CRITICAL**

- **File / function:** `client.py::refresh_token()`, `token_manager.refresh_existing_token()`
- **Current behavior (before):** sent the *old enrollment token* in `X-Enrollment-Token` and an empty body.
- **Required behavior:** **HMAC-authenticated** (not token-authenticated), body `{"email": ...}`. Returns a token for *"the most recent in-progress enrollment associated with the given email"*. 404 when none exists.
- **Severity:** **Critical** — refresh would have failed 401, cascading into total session loss.
- **Fix applied:** refresh is HMAC-signed and email-keyed; `PerchNotFoundError` added for 404.

---

**M-4. `expires_at` was computed locally instead of read from the response** 🟠

- **File / function:** `token_manager._store()`
- **Current behavior (before):** `now + 30 minutes`.
- **Required behavior:** `enrollmentTokenResponse.expires_at` is authoritative (ISO 8601).
- **Severity:** **Medium** — clock skew between our host and Perch would cause spurious 403s.
- **Fix applied:** `_server_expiry()` parses Perch's value, falling back to local only if absent.

---

**M-5. `next_step` on token responses was discarded** 🟠

- **File / function:** `client._parse_token_response()`
- **Required behavior:** both `/token` and `/refresh_token` return `next_step`; on refresh it is *how an interrupted enrollment resumes*.
- **Severity:** **Medium** — blocks the documented resume path.
- **Fix applied:** parsed and returned. **Not yet wired into workflow resume** — see High Risk R-4.

---

**M-6. Header capitalization** 🟡

- **Current:** `X-Api-Key` → **Required:** `X-API-Key`. Fixed. (HTTP headers are case-insensitive per RFC 7230, so this was likely harmless, but there is no reason to differ from the spec.)

---

**M-7. Missing utility `pse-g-ny`; account number formats absent** 🟠

- **File:** `db/migrations/002_...sql` seeded 6 of 7 utilities; account number lengths were not modeled at all.
- **Required:** 7 utilities; account numbers are 10 or 11 digits depending on utility.
- **Severity:** **Medium** — a PSE&G customer could not be enrolled; bad account numbers would surface as an opaque 422.
- **Fix applied:** migration `003` adds `pse-g-ny`, adds `account_number_length`, and confirms the `nyseg` slug.

---

**M-8. Upload size limit was 15 MB** 🟠

- **File / function:** `helpers.py::MAX_UPLOAD_BYTES`
- **Required:** 4 MB — Perch returns **413** above it.
- **Severity:** **Medium** — a 10 MB bill would be accepted locally and rejected by Perch mid-enrollment.
- **Fix applied:** 4 MB, with a local pre-check.

---

**M-9. Proof-document `source_type` vocabulary unknown** 🟡

- **Required:** 7 `proof_doc_*` values plus 2 `self_attestation_*` values.
- **Fix applied:** `perch_proof_doc_types` reference table (migration `003`). **Consumed in Milestone 5** — not wired into any flow yet.

---

**M-10. Status codes were coarsely mapped** 🟠

- **Current (before):** everything `>= 400` collapsed into two error types.
- **Required:** 401 (HMAC failure), 403 (session expiry *or* API-key permission, depending on endpoint), 404, 413, 422, 500, 503 all have distinct meanings.
- **Fix applied:** `_raise_for_hmac_status()` plus explicit 422/404 handling. Perch's `{error, message}` envelope is now parsed into our messages.

---

### DOCUMENTED, NOT IMPLEMENTED (later milestones — deliberately out of scope)

| # | Gap | Milestone | Note |
|---|---|---|---|
| D-1 | `POST /enroll` — multipart, `utility_accounts[][...]` repeated-array encoding, PDF-only bills | M3 | Exact field names now captured in the Friday plan |
| D-2 | `customer_type` values are `Residential` / `Business` / `LMI` (**capitalized**) | M3 | Our mock used lowercase; not yet used in a real call |
| D-3 | Business customers require `business_name`, `business_title`, `business_phone`, `home_address[*]` | M3 | Not modeled |
| D-4 | `phone_number` is 10 digits, **no country code** | M3 | Not validated |
| D-5 | `billing_address` vs `home_address` vs per-account `service_address` are three distinct addresses | M3 | Our schema has one |
| D-6 | `GET /status` — completed/remaining steps, `completed`, `next_step` | M6 → **recommend moving to M3** | Cheap, and the documented way to resume |
| D-7 | `POST /lmi/proof_docs` — two-part multipart: JSON metadata part + `documents[N][file]` parts | M5 | JPEG/PNG/HEIC/PDF |
| D-8 | `POST /lmi/self_attestation` — `occupancy`, `county`, `lmi_source_selection` | M5 | Answers open item Q16: **Perch links to the HUD income-limit dataset; the partner determines above/below** |
| D-9 | `POST /lmi/self_attestation/accept` — requires `metadata{timestamp, ip_address, user_agent}` | M5 | We capture IP/UA already for signatures |
| D-10 | `POST /contracts` and `POST /contracts/accept` — Perch generates; partner does **not** upload contract files; acceptance needs `metadata{}` | M4 | Confirms the local contract library is dead |
| D-11 | All-or-nothing validation on `/enroll` | M3 | Any bad account rejects the whole request |

---

## 🟢 Safe Refactors

Applied — no functional change:

1. **UTF-8 on every text read** (`db/__init__.py`, `db/migrate.py`, test suite). Our SQL files contain box-drawing characters; on Windows (cp1252 default) `seed.py` and the test suite crashed without `-X utf8`. This would have broken Friday's validation on a Windows machine.
2. **`requirements-dev.txt`** with pytest, plus install command in the README.
3. **Retired M1 stub removed from test discovery.** Its module-level `sys.exit(0)` crashed `pytest` during collection. Historical copy kept as `test/superseded_milestone1_suite.py.txt` (`.txt`, so never imported or collected).
4. **`conftest.py` + `pytest.ini` + `test/test_suites.py`** so `python -m pytest` runs the real suites with no flags.
5. **Perch error envelope parsing** — `{error, message}` surfaced in our messages instead of raw truncated text.

Deliberately **not** done (would be speculative):
- Rewriting the wizard for the multi-address model (M3, needs the real `/enroll` shape)
- Refactoring `lmi_validation.py` toward the new source types (M5)
- Consolidating `projects` removal (already scheduled)

---

## 🔴 High Risk Areas for Friday

Ordered by likelihood of failure.

**R-1. HMAC signature computation** — *highest risk*
Every failure mode returns the same generic `"Authentication failed"`, so a
mistake is invisible. Specific traps:
- **Body bytes must be exactly what was signed.** We send `data=body.encode("utf-8")`, not `json=payload`, because `requests` would re-serialize with spaces and invalidate the signature.
- **Compact JSON** — one stray space breaks it.
- **Clock skew** — ±5 minutes. If the host clock drifts, every call fails.
- *Mitigated:* cross-verified against `openssl` for both variants. Still unverified against Perch.

**R-2. `POST /token` email semantics**
We now send the customer's email. **Unknown:** whether calling `/token` twice
for the same email creates two sessions or collides with the existing
in-progress enrollment. Perch's refresh doc says it returns *"the most recent
in-progress enrollment"* for an email — implying email is a session key. If a
rep starts two enrollments for the same customer, behavior is undefined.
→ Test explicitly (Friday plan §2.4).

**R-3. `X-API-Key` header name**
Now matches the spec. Was `X-Api-Key`. Case-insensitive per RFC, but if Perch's
gateway does an exact-match lookup this was a silent 401.

**R-4. Resume via `next_step` on refresh — not wired**
We parse `next_step` from the refresh response but do not yet route the
workflow to it. A resumed enrollment will land on our locally-derived step
rather than Perch's. Harmless in M2 (only capacity exists) but **must be wired
before M3**.

**R-5. 503 ambiguity**
The global status table says 503 = *"No open solar projects currently
available, **or** service is temporarily down."* On `/capacity` we treat it as
no-capacity. If Perch returns 503 for a genuine outage, we would tell a rep
"no capacity in this ZIP" when the truth is "Perch is down."
→ Verify by checking whether the 503 body's `error` code distinguishes them.

**R-6. Multipart encoding for `/enroll`** *(M3, not yet built)*
`utility_accounts[][utility_bills][]=@file;type=application/pdf` with repeated
bracket keys is easy to get wrong in `requests`. Needs its own test harness
before M3.

**R-7. Timestamp format inconsistency in the spec**
`expires_at` is ISO 8601 (`2026-07-09T16:12:00Z`), but self-attestation
`metadata.timestamp` examples use `2026-07-03 15:20:36.67175` — space-separated
with microseconds. Do not assume one format works everywhere. *(M5)*

**R-8. Bills must be PDF**
`/enroll` accepts `utility_bills[]` as **PDF only**, while proof docs accept
JPEG/PNG/HEIC/PDF. Our uploader currently accepts images for bills — a photo of
a bill will be rejected at enroll. **Needs an OCR-pipeline decision in M3:**
either restrict bill uploads to PDF, or convert images to PDF before submission.

---

## Open items now CLOSED by the spec

| Was | Now |
|---|---|
| Q1 HMAC scheme unknown | **Closed** — fully specified and implemented |
| Q2 API key header name | **Closed** — `X-API-Key` |
| Q4 `next_step` vocabulary | **Closed** — 8 endpoints enumerated |
| Q5 NYSEG slug | **Closed** — `nyseg` confirmed; 7 utilities total |
| Q6 `/token` credentials | **Closed** — HMAC + `{"email"}` |
| Q9 proof-doc scope | **Partially** — project-level flag; per-account submission |
| Q14 upload mechanics | **Closed** — multipart, 4 MB, formats specified |
| Q15 `source_type` vocabulary | **Closed** — 9 values |
| Q16 income limits | **Closed** — Perch links the HUD dataset; partner determines above/below |
| Q19 `GET /status` | **Closed** — schema published |

**Still open:** Q3 (product IDs never surfaced), Q8 (idempotency — *not addressed
anywhere in the spec*, and the highest-volume risk at 100+/day), Q10 (agent ID),
Q12 (contract versioning), Q17/Q18 (SharePoint resubmission, magic link),
Q20 (VIPR correlation keys).
