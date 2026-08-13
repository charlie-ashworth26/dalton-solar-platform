# FRIDAY_STAGING_TEST_PLAN.md

Ordered validation plan for Perch staging.

**Base URL (staging):** `https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments`
**Markets:** `https://staging.api.perchenergy.com/affiliate_partners/v1/markets`

**Rule for the day: stop at the first failure.** These steps are dependent —
a bad signature in step 1 makes every later result meaningless.

**Use one throwaway email per full run** (e.g. `dalton.test+run1@…`). `POST /token`
appears to key sessions on email, so reusing one across runs may collide with an
in-progress enrollment (see §2.4).

---

## Step 0 — Pre-flight (before any API call)

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                     # expect all suites green in MOCK mode
python test/test_perch_milestone2.py # standalone, same result
date                                 # confirm host clock is accurate
```

**Host clock is a hard dependency.** HMAC tolerance is ±5 minutes. If the clock
is off, *every* authenticated call fails with a generic `"Authentication failed"`
and you will waste the morning debugging the signature.

Then set staging env (do **not** commit these):

```bash
export PERCH_API_MODE=live
export PERCH_ENROLLMENT_BASE_URL=https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments
export PERCH_MARKETS_BASE_URL=https://staging.api.perchenergy.com/affiliate_partners/v1/markets
export PERCH_API_KEY=<from Perch>
export PERCH_SECRET_KEY=<from Perch>
```

**Validate the signer against Perch's own example before trusting it:**

```bash
python -c "
from services.perch import hmac_auth as H
b=H.compact_json({'email':'john.doe@example.com'})
print(repr(b))
print(H.compute_signature('YOUR_SECRET_KEY', H.build_signed_payload('1617187200', b)))
"
# expect: '{\"email\":\"john.doe@example.com\"}'
#         78c4cc8f5c02760d16b9911ab8ed5db3076096273f5bdcca14e4ec10772db5b5
```

> ⚠️ If you reproduce the spec's cURL by hand, run it in **bash, not sh**.
> `echo -n` and `$'\n'` are bash-isms; under `dash` they silently produce the
> wrong signature. (This cost us a false alarm during development.)

---

## 1. Authentication (HMAC) — validate in isolation first

**Do not start with `/token`.** Validate HMAC on its own so an auth failure
isn't confused with a business error.

**Request** — `POST /token` with a deliberately invalid email:
```
X-API-Key, X-HMAC-Signature, X-HMAC-Timestamp, Content-Type: application/json
{"email":"not-an-email"}
```

**Expected:** `422 {"error":"unprocessable_entity","message":"Email is invalid"}`

**Why this is the right first test:** a **422 proves HMAC succeeded** — the
request got past authentication and reached validation. A 401 means the
signature is wrong.

**Failure modes**

| Response | Meaning | Recovery |
|---|---|---|
| 401 `Missing X-API-Key header` | Header not sent | Check `PERCH_API_KEY` is set |
| 401 `Authentication failed` | Bad signature, bad key, or expired key | Compare the exact signed bytes; confirm compact JSON; re-run the signer check above |
| 401 `Timestamp out of tolerance` | Clock skew | `ntpdate` / sync the system clock |
| 403 | API key lacks permission | Contact Darius — not a code problem |

**DB/frontend:** none. Do this via curl or a Python one-liner.

---

## 2. Token generation

### 2.1 Happy path
**Request:** `POST /token`, HMAC-signed, `{"email":"dalton.test+run1@example.com"}`

**Expected 200:**
```json
{"enrollment_token":"<uuid>","expires_at":"<ISO8601>","next_step":"<url>"}
```

**Verify:**
- `enrollment_token` parses as a UUID
- `expires_at` is ~1 hour ahead — **compare to Perch's clock, not ours**
- Note the `next_step` value (we expect `/capacity`; confirm)

**Database:** one row in `perch_tokens` (`api_mode='live'`, `enrollment_id` set,
`expires_at` = **Perch's** value). One `perch_api_calls` row, operation
`request_token`, with the token `[REDACTED]`.

**Frontend:** none directly — issued during the capacity call.

### 2.2 Confirm the token is never exposed
```bash
curl -s localhost:5000/api/perch/enrollments/<id>/token-status -H "Authorization: Bearer <jwt>"
```
Must return `expires_at` and `seconds_remaining` — **never the token value**.

### 2.3 Invalid email → 422 (already covered in §1)

### 2.4 Duplicate-email behavior — **now documented; verify it matches**
Call `POST /token` **twice with the same email**.

**Expected (newest YAML):** `422` —
`"An enrollment request already exists for this email. Use the /status endpoint
to check the current status of the enrollment."`
(or `"Email has already been taken"` if the address already has an account).

**Our behaviour:** the adapter catches this and automatically resumes via
`PATCH /refresh_token` rather than failing. Confirm the resume produces a
working token and that the 422-and-resume appears in `perch_api_calls`.

---

## 3. Capacity

**Request:** `POST /capacity`, `X-Enrollment-Token` only (**no HMAC headers**),
`{"zip_code":"10001","utility_name":"consolidated-edison-ny"}`

**Expected 200:** `project_details` with exactly six fields + `next_step`.

**Verify:** field names match the spec **exactly**; `next_step` points at
`/enroll`; sending HMAC headers *as well* does not cause a failure (harmless, but
worth knowing).

**Database:** `perch_capacity_checks` row with the six normalized fields,
`next_step_url`, and the raw JSON. `enrollments.service_zip` / `utility_name`
set. Status `Draft → Information Needed`.

**Frontend:** capacity card renders three availability rows and two savings
figures; the proof-doc notice appears when `proof_documents_required` is true.

**Failure modes**

| Response | Meaning | Recovery |
|---|---|---|
| 403 | Token expired/invalid | Our adapter auto-refreshes and retries once — **watch the logs to confirm it fired** |
| 422 | Bad ZIP or slug | Check the slug against `perch_utilities` |
| 503 | No capacity **or** service down | See §3.1 |

### 3.1 ⚠️ 503 disambiguation — **test deliberately**
Send a ZIP with no capacity. **Capture the response body.**

Our code treats 503 on `/capacity` as "no capacity." The spec's global table
says 503 may also mean "service temporarily down." **Check whether the `error`
code differs between the two.** If it doesn't, we need Perch to tell us how to
distinguish them — otherwise we will show reps "no capacity here" during an
outage.

### 3.2 Test each utility
Run one capacity check per slug — all seven. Confirms our slug table and reveals
any utility Perch does not actually service in staging.

---

## 4. Refresh token

**Request:** `PATCH /refresh_token`, **HMAC-signed** (not token-authenticated),
`{"email":"dalton.test+run1@example.com"}`

**Expected 200:** a **new** `enrollment_token`, new `expires_at`, and a
`next_step` indicating where to resume.

**Verify:**
- The new token differs from the old one
- **The old token is now rejected** — retry §3 with it, expect 403
- `next_step` reflects actual progress (after §3 it should be `/enroll`)

**Database:** old `perch_tokens` row `is_active=0`; new row with
`refresh_count` incremented. A `refresh_token` row in `perch_api_calls`
recorded as `PATCH`.

**Failure modes**

| Response | Meaning | Recovery |
|---|---|---|
| 404 | No in-progress enrollment for that email | Expected for an unknown email — verify this too |
| 401 | HMAC wrong on this endpoint | Note: refresh uses HMAC, **not** the enrollment token |

### 4.1 Forced-expiry test
Wait out the hour (or ask Perch for a short-TTL key), then call
`/capacity`. Confirm the **automatic 403 → refresh → retry** path fires and the
rep sees no error. Confirm `perch_api_calls` contains the 403 **and** the
successful retry.

### 4.2 ⚠️ Known gap — resume is not wired
We parse `next_step` from the refresh response but do **not** yet route the
workflow to it (Readiness R-4). Record the value; do not expect the UI to jump.

---

## 5. Status endpoint

**Request:** `GET /status` with `X-Enrollment-Token`

**Expected 200:**
```json
{"completed_steps":[...],"remaining_steps":[...],"completed":false,"next_step":"<url>"}
```

**Verify:** after §3, `completed_steps` should contain
`generate_enrollment_token`; capture the **exact step-name vocabulary** — it is
the authoritative list of flow steps and we should map our workflow keys to it.

**Database:** not yet implemented (M6 → **recommend pulling into M3**). For
Friday, call it with curl and record the output.

**Note:** safe to poll, no side effects. Use it liberally to check state between
steps rather than guessing.

---

## 6. Enroll — ⚠️ NOT IMPLEMENTED (Milestone 3)

Do **not** attempt through our application. Validate by curl only, to de-risk M3.

**Goals for Friday:**
1. Confirm the repeated-array multipart encoding is accepted:
   `utility_accounts[][utility_account_number]`, `utility_accounts[][utility_bills][]=@bill.pdf;type=application/pdf`
2. Confirm **PDF-only** for bills — try a JPEG, expect rejection. *(This decides whether our OCR pipeline must convert photos to PDF — Readiness R-8.)*
3. Confirm `customer_type` casing (`Residential`, not `residential`)
4. Trigger **all-or-nothing** validation: submit two accounts, one with a bad account number; confirm neither is created
5. Test a POD ID utility (NYSEG) with a 14-digit POD ID; confirm the 422 and capture the message shape
6. Exceed 4 MB; confirm 413
7. Capture the `next_step` for a **non-LMI** vs an **LMI** project — this drives the M3 branch

**Recovery:** none needed — nothing is persisted on our side.

---

## 7–8. Proof documents / Self-attestation — ⚠️ NOT IMPLEMENTED (Milestone 5)

Curl only. Capture:
- The two-part multipart shape for `/lmi/proof_docs` (JSON metadata part + `documents[N][file]`)
- Whether `source_type` values are validated strictly
- The presigned document URL returned by `/lmi/self_attestation`, and its TTL
- The exact `metadata.timestamp` format accepted by `/accept`
  (**Readiness R-7:** the spec shows `2026-07-03 15:20:36.67175` here but ISO 8601 elsewhere — determine which is required)

---

## 9–11. Contracts / Acceptance / Completion — ⚠️ NOT IMPLEMENTED (Milestone 4)

Curl only. Capture:
- Contract response shape — presigned URLs? Is the PDF pre-filled by Perch?
- Whether a contract **version identifier** is returned *(open item Q12 — ask Darius directly)*
- `/contracts/accept` `metadata` requirements
- The terminal response and `GET /status` showing `completed: true`

---

## Priority order if time runs short

1. **§1 HMAC** — nothing else matters until this passes
2. **§2 Token** (incl. §2.4 duplicate-email)
3. **§3 Capacity** (incl. §3.1 503 disambiguation)
4. **§4 Refresh** (incl. §4.1 forced expiry)
5. **§5 Status** — cheap, and unblocks resume design
6. **§6 Enroll via curl** — the biggest M3 de-risk

Steps 1–5 validate everything we have actually built. Step 6 onward is
reconnaissance for future milestones.

---

## Questions to ask Darius on the day

1. Does `POST /token` twice with the same email fork or resume? (§2.4)
2. Does the 503 body distinguish "no capacity" from "service down"? (§3.1)
3. **Is there an idempotency key on `/enroll`?** Not in the spec anywhere, and it's our biggest volume risk at 100+/day.
4. Is a contract version identifier returned? (open item Q12)
5. Is an agent/rep ID field being added? (open item Q10)
6. Which staging ZIP/utility combinations have capacity, and which reliably have none? (Needed to test both branches deliberately.)
