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


---

# ADDENDUM — Observed staging response discrepancy (2026-08)

Staging access is now working. `POST /token` succeeds from both Mac and Windows
using the issued API key and signing key. **HMAC authentication is confirmed
correct and was not modified.**

## The discrepancy

| | Published YAML | Observed staging |
|---|---|---|
| HTTP status | `201` | `201` ✅ matches |
| Token field | `enrollment_token` | **`token`** |
| Next-step field | `next_step` | **`next_step_url`** |
| Expiry field | `expires_at` (**`required`**) | **absent entirely** |

All three fields are marked `required` in the YAML, so this is not an optional
field being omitted — **staging and the specification disagree.** Most likely
staging is running a build that predates the published spec, but that is a
hypothesis; either could be the one that changes.

## How we handle it

`PerchHTTPClient._parse_token_response()` accepts **either** shape and
normalizes to the documented names, so nothing downstream needs to know which
arrived. The result carries `response_shape` (`"documented"` | `"staging_alias"`)
so the discrepancy stays visible in the audit trail rather than being silently
smoothed over.

If neither key is present the error names both accepted spellings and lists the
keys actually received — so a third variant fails loudly and diagnosably.

## Token expiry when `expires_at` is absent

**We do not fabricate an API-returned expiry.**

`perch_tokens.expires_at` is `NOT NULL` and drives *proactive* refresh, so a
value is required. When Perch omits it we derive one locally
(`issued_at + TOKEN_TTL_SECONDS`, the YAML-documented 1 hour) and record
**`expires_at_source = 'derived'`** (migration `005`).

Why this is safe:

1. **It is never presented as Perch's.** The column, `token_status()`, and the
   admin diagnostics all distinguish `'api'` from `'derived'`, and expose
   `expires_at_is_authoritative`.
2. **Correctness does not depend on it.** The authoritative expiry signal is a
   **403** from Perch, which triggers the already-tested refresh-and-retry path.
   The derived value only schedules *early* refresh — an optimization.
3. **Both error directions are benign.** Estimate too long → the 403 path
   catches it. Too short → one wasted refresh call.
4. **An unparseable `expires_at` is treated as absent**, not trusted.

## Files changed in this addendum

| File | Change |
|---|---|
| `services/perch/client.py` | `_parse_token_response()` accepts both shapes; adds `response_shape` |
| `services/perch/token_manager.py` | `_server_expiry()` → `_resolve_expiry()` returning `(datetime, source)`; `_store()` persists provenance; `token_status()` exposes it |
| `db/migrations/005_token_expiry_provenance.sql` | **New.** `perch_tokens.expires_at_source` |
| `test/test_perch_milestone2.py` | Two new sections, 21 assertions |
| `scripts/verify_token_live.py` | **New.** Read-only live `/token` check through the normal client |

**Unchanged:** `services/perch/hmac_auth.py` (verified byte-identical), API
key/signing-key handling, base URLs, duplicate/in-progress-email handling.

## Question for Perch

Which is correct — the spec (`enrollment_token` / `expires_at` / `next_step`) or
staging (`token` / `next_step_url`, no expiry)? We support both, but production
should not be a third variant. Specifically: **does the enrollment token
actually expire in 1 hour**, given staging returns no `expires_at`?


---

# ADDENDUM 2 — Observed staging `/capacity` discrepancy (2026-08)

Live enrollment-scoped `POST /capacity` verified against staging with
`zip_code=12202`, `utility_name=national-grid-ny`, using the normal
`PerchClient` and a token obtained through the normal `/token` path.

## The discrepancy

Identical alias pattern to `POST /token`:

| | Published YAML | Observed staging |
|---|---|---|
| Envelope | `{"project_details": {...}, "next_step": "<url>"}` | `{"project_details": {...}, "next_step_url": "<url>"}` |
| `project_details` | six required fields | **all six present** ✅ |
| Next-step key | `next_step` | **`next_step_url`** |

**`project_details` matched the spec exactly** — this is purely an envelope key
name. Observed values (`lmi_capacity_available=True`,
`proof_documents_required=True`, `residential_capacity_available=False`,
`small_commercial_capacity_available=False`, both savings rates `25.0`) are
plausible and internally consistent: an LMI-only project requiring proof docs.

Two endpoints now confirmed using `*_url` suffixes where the YAML does not, so
this is a **systematic staging/spec divergence**, not a one-off.

## How we handle it

New `normalize_capacity_response()` in `services/perch/client.py`, mirroring
`_parse_token_response()`:

* accepts `next_step` (documented) or `next_step_url` (staging)
* **prefers `next_step`** when both are present
* passes `project_details` through **completely unchanged**
* preserves the untouched Perch response under `raw`
* tags `response_shape` as `documented` / `staging_alias` / `no_next_step`

`no_next_step` exists so a response with neither key fails visibly instead of
silently producing `None`.

The adapter now writes `response["raw"]` to `perch_capacity_checks.raw_response_json`
and to the API-call audit log, so the audit trail stores exactly what Perch sent
rather than our normalized wrapper.

## Files changed in this addendum

| File | Change |
|---|---|
| `services/perch/client.py` | Added `normalize_capacity_response()`; live `check_capacity()` returns it |
| `services/perch/mock_client.py` | Returns the same normalized shape; added `emit_staging_alias_shape` test hook |
| `services/perch/adapter.py` | Persists the genuine Perch envelope (`raw`) to audit records |
| `test/test_perch_milestone2.py` | 19 assertions across two new sections |
| `scripts/verify_capacity_live.py` | Validates the normalized path; also checks the workflow engine resolves the returned URL |

**Unchanged:** `hmac_auth.py`, token handling, base URLs, request payloads.

## Remaining staging-vs-spec discrepancies

1. **`token` vs `enrollment_token`** (POST /token) — normalized.
2. **`next_step_url` vs `next_step`** (POST /token) — normalized.
3. **`next_step_url` vs `next_step`** (POST /capacity) — normalized.
4. **`expires_at` absent entirely** (POST /token) — derived locally, provenance recorded.

All four are fields the YAML marks `required`. **Hypothesis:** staging runs a
build predating the published spec. Unconfirmed.

**Unknown until tested:** whether `/enroll`, `/status`, `/lmi/*`, and
`/contracts` use the same `*_url` convention. Given two of two endpoints so far
do, assume they will and verify each before building against it.

## Questions for Perch

1. Which spelling is authoritative for production — the YAML or staging?
2. Does the enrollment token actually expire in 1 hour, given staging returns no `expires_at`?
3. Do the remaining endpoints also use `*_url` suffixes?
