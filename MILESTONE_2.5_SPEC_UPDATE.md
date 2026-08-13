# Milestone 2.5 — Newest OpenAPI Alignment

Updates the implementation to the newest Perch OpenAPI specification.
Every behavioural change below is traceable to a specific line in the new YAML.

**The staging 403 investigation is deliberately excluded from this work.**
No change was made to the HMAC algorithm, header names, canonical signing
procedure, API-key/signing-key mapping, endpoint paths, or base URLs.
`services/perch/hmac_auth.py` is **byte-identical** to the pre-update version,
and there is now a regression test asserting the signing vector is unchanged.

---

## Old YAML → new YAML differences

| # | Difference | M2.5? | Action |
|---|---|---|---|
| 1 | Token TTL **30 min → 1 hour** | ✅ | Implemented |
| 2 | `POST /token` success **200 → 201** | ✅ | Implemented |
| 3 | `POST /token` 422 now covers **duplicate / in-progress email** | ✅ | Implemented (+ automatic resume) |
| 4 | `next_step` now **`required`** on token responses | ✅ | Asserted |
| 5 | `lmi_source_selection` enum **narrowed** — `self_attestation_qualifying_income_rejected` now returns 422 | ✅ (data) | Migration 004 |
| 6 | New `self_attestation.status` field (`accepted`/`rejected`) replaces the retired source type | ✅ (data) | Migration 004 |
| 7 | Multipart arrays **`[]` → explicit `[n]` indices** | ❌ M3 | Documented |
| 8 | `/lmi/self_attestation/accept` body reshaped (array → object, no `utility_account_number`) | ❌ M5 | Documented |
| 9 | New metadata validation (timestamp parseable, not future, ≤24h; valid IPv4/IPv6) | ❌ M5 | Documented |
| 10 | `/lmi/self_attestation/accept` + `/contracts/accept` **200 → 202** (async) | ❌ M4/M5 | Documented |
| 11 | Contract presigned URLs expire in **1 hour** | ❌ M4 | Documented |
| 12 | `completed_steps` now includes **`capacity_check`** | ❌ M6 | Documented |
| 13 | `CreateenrollmentRequest` → `CreateEnrollmentRequest` | — | Cosmetic |
| 14 | Staging listed first in `servers`; cURL uses `$BASE_URL` vars | — | Cosmetic |
| 15 | **HMAC signing procedure — UNCHANGED** | — | Regression-tested |
| 16 | **All 11 endpoint paths — UNCHANGED** | — | No action |
| 17 | **401/403 semantics — UNCHANGED** | — | No action |

### Not a bug: the Markets base/path split
The new YAML declares `$MARKETS_BASE_URL` as `…/v1` with path `/markets/capacity`;
Perch's email gave the base as `…/v1/markets`. Both resolve to
`…/v1/markets/capacity`, which is what we send. **No change made.**

---

## Files changed

| File | Change |
|---|---|
| `services/perch/client.py` | `TOKEN_TTL_SECONDS` → `60*60`; added `TOKEN_CREATED_STATUS = 201`; added `_is_enrollment_in_progress()`; raise `PerchEnrollmentInProgressError` on the documented 422; docstrings |
| `services/perch/errors.py` | Added `PerchEnrollmentInProgressError` |
| `services/perch/mock_client.py` | 1-hour TTL; second token request for the same email now raises the documented 422 |
| `services/perch/token_manager.py` | Added `_resume_existing_enrollment()`; `issue_new_token()` auto-resumes on 422 |
| `services/perch/config.py` | Refresh-skew rationale (kept at 120s — drift is absolute, not proportional) |
| `db/migrations/004_new_yaml_alignment.sql` | **New.** Retires `self_attestation_qualifying_income_rejected`; adds status vocabulary |
| `test/test_perch_milestone2.py` | Stale TTL assertions corrected; per-enrollment emails; 4 new sections |
| `routes/perch_routes.py`, `README.md`, `WALKTHROUGH_MILESTONE2.md`, `FRIDAY_STAGING_TEST_PLAN.md` | Stale 30-minute references; §2.4 now answered |

---

## Behavioural changes

1. **Tokens are treated as valid for 1 hour** (Perch's `expires_at` remains authoritative; the constant is only the fallback).
2. **Duplicate/in-progress email auto-resumes.** Previously a rep returning to an abandoned customer would have been permanently blocked by a 422. Now the adapter catches it and obtains a token via `PATCH /refresh_token`. Both the 422 and the resume are audit-logged.
3. **`self_attestation_qualifying_income_rejected` is marked inactive** rather than deleted, preserving the audit trail.

---

## Tests

`test/test_perch_milestone2.py` — **210 passed, 0 failed** (was 188).

Corrected as stale (encoded the old contract, not weakened):
- `TOKEN_TTL_SECONDS == 1800` → `== 3600`
- `ttl_seconds == 1800` → `== 3600`

Added:
- 1-hour TTL verified against the **stored** `expires_at`, not just the constant
- `TOKEN_CREATED_STATUS == 201`; `next_step` present and pointing at `/capacity`
- Duplicate email raises the documented 422 with the `/status` guidance
- **Two enrollments sharing an email both succeed**, via `refresh_token`, with the 422-and-resume audit-logged
- Retired source type inactive with a recorded reason; status vocabulary present
- **HMAC regression guard** — signature vector, header names, canonical query string

### One test bug I introduced and fixed
Giving every capacity *call* a unique email broke the refresh-count assertion:
the suite checks capacity more than once per enrollment, and the email is the
Perch **session identity** — varying it mid-enrollment silently re-pointed the
session. Fixed with a stable per-**enrollment** address. The backend behaviour
(overwriting the email when the rep corrects it) is correct and was not changed
to accommodate the test.

---

## Impact on the staging 403 diagnosis: **none**

Nothing in the new YAML explains a correct-HMAC 403. Reviewed specifically:
signing procedure (unchanged), required headers (unchanged), endpoint paths
(unchanged), base URLs (equivalent), 401/403 semantics (unchanged verbatim),
and the absence of any documented header we are not sending.

Your independent tests are consistent with the spec's own semantics:
corrupted HMAC → **401** (`UnauthorizedHmac`), correct HMAC → **403**
(`ForbiddenPartner` = *"your API key lacks permission for this action —
contact Perch Energy"*). Reproducing outside the application via raw
`curl` + `openssl` rules out our client entirely.

**Remaining anomaly:** the observed 403 body is **blank**, while the spec
documents a JSON `ErrorResponse`. That still points at either key provisioning
or an edge/gateway rejection ahead of the application — a question for Perch,
not a code change.

---

## Still ambiguous in Perch's documentation

1. Blank 403 body vs. the documented JSON envelope.
2. Every 422 uses `error: "unprocessable_entity"`, so duplicate-email detection
   must match on **message text** — brittle if Perch rewords it. A distinct
   error code would fix this.
3. Whether `POST /token` 422 for an email with a *completed* enrollment is
   distinguishable from an *in-progress* one (affects whether resume is correct).
4. Idempotency on `POST /enroll` — still unaddressed anywhere.
5. Whether `/markets/capacity` `proof_documents_required` carries the same
   meaning as the enrollment-scoped one (markets returns no `next_step`).
