# Milestone 2 — Browser Walkthrough

What a rep actually experiences today, which API calls fire, and where each
piece of data comes to rest. Written against the system as built; run it
yourself with `python3 app.py` and `http://localhost:5000`.

`PERCH_API_MODE=mock` is the default. Everything Perch-side below is a mock
response matching the published schemas — see "What is mocked" at the end.

---

## Setup

```bash
cd backend
python3 seed.py     # runs migrations 001 + 002, seeds 4 users, NO projects
python3 app.py
```

Sign in as **charlie@daltonsolar.com / RepPass1!**

Useful test ZIPs (mock fixtures):

| ZIP | Utility slug | What it exercises |
|---|---|---|
| `13348` | `national-grid-ny` | All three segments available, proof docs required, 10% / 20% |
| `12010` | `national-grid-ny` | Residential only — no LMI capacity, 8% |
| `10001` | `consolidated-edison-ny` | LMI available with **no** proof doc, 5% / 25% |
| `12550` | `central-hudson-gas-electric` | A POD ID utility |
| `99999` | any | **503 → no capacity** (business outcome) |
| `00000` | any | Genuine upstream failure (error path) |

---

## Step 1 — Rep clicks "+ New enrollment"

**What the rep sees:** a card headed *Service area* with a ZIP field, a utility
dropdown, and a banner showing an enrollment code like `ENR-2026-000001`.

**What is actually happening — the important part:** none of that card exists in
our HTML. `templates/index.html` contains a single empty `<div id="workflow-root">`.
The card was **built at runtime from a descriptor the backend returned.**

**API calls:**

| # | Call | Purpose |
|---|---|---|
| 1 | `POST /api/perch/drafts` | Creates the Dalton enrollment draft |
| 2 | `GET /api/perch/enrollments/:id/workflow` | Asks "what step am I on?" |

Call 2 returns, in part:

```json
{
  "step": {
    "key": "service_area",
    "title": "Service area",
    "fields": [
      {"name": "zip_code", "type": "text", "required": true, "max_length": 5,
       "validation": {"pattern": "^\\d{5}$", "message": "ZIP code must be exactly 5 digits."}},
      {"name": "utility_name", "type": "select", "required": true,
       "options": [{"value": "national-grid-ny", "label": "National Grid NY"}, ...]}
    ],
    "primary_action": {"label": "Check availability", "operation": "check_capacity"}
  }
}
```

The frontend renders fields, wires the validation regex, and labels the button —
all from that payload. **No Perch call has happened yet.**

**Stored in Dalton:** a row in `enrollments` (status `Draft`, the immutable
`ENR-` code, the rep's ID), a row in `perch_workflow_state`, and an
`enrollment_draft_created` row in `audit_logs`.

**Stored in Perch:** nothing.

**Why the draft comes first:** Perch's enrollment token is session-scoped and
expires in 1 hour. It can never be the durable key for an enrollment that
will later collect documents, a signature, and a VIPR payability record. The
Dalton Enrollment ID is issued before any Perch contact and is what everything
else hangs off.

---

## Step 2 — Rep enters ZIP `13348`, selects National Grid NY, clicks "Check availability"

**What the rep sees:** the button reads *Checking with Perch…*, then the card is
replaced by *Capacity confirmed* showing three availability rows and two savings
figures.

**API call:** `POST /api/perch/enrollments/:id/capacity`
with `{"zip_code": "13348", "utility_name": "national-grid-ny"}`

Note the dropdown's **value** is the slug, never the display name. Perch matches
on slug, and a mismatch fails silently with no capacity found — so the frontend
is never given the opportunity to send a display name. (If a display name *is*
posted, e.g. by an integration, `utilities.resolve_slug()` translates it.)

**What the backend does, in order:**

1. Validates ZIP is 5 digits and the utility resolves to a known slug — **before**
   calling Perch. A bad utility gets a 400 explaining slugs, not an opaque
   Perch failure.
2. `token_manager.get_valid_token(enrollment_id)` — no usable token exists, so:
   → **`POST /token`** to Perch → stores the returned UUID in `perch_tokens`
   with a 1-hour expiry (taken from Perch's `expires_at`), scoped to this enrollment.
3. → **`POST /capacity`** to Perch with header `X-Enrollment-Token: <uuid>`.
4. Persists the raw response, normalizes it, records the `next_step` URL.
5. Re-resolves the workflow and returns the **next step descriptor** alongside
   the result.

**Perch's response (exactly the documented shape):**

```json
{
  "project_details": {
    "small_commercial_capacity_available": true,
    "lmi_capacity_available": true,
    "residential_capacity_available": true,
    "proof_documents_required": true,
    "savings_percent_for_residential_and_commercial_customers": 10,
    "savings_percent_for_lmi_customers": 20
  },
  "next_step": "https://api.perchenergy.com/affiliate_partners/v1/enrollments/enroll"
}
```

**What the rep now sees, and where each value came from:**

| On screen | Source |
|---|---|
| Residential — Available | `residential_capacity_available` |
| Small commercial — Available | `small_commercial_capacity_available` |
| Income qualified (LMI) — Available | `lmi_capacity_available` |
| Residential / commercial savings **10%** | `savings_percent_for_residential_and_commercial_customers` |
| Income-qualified savings **20%** | `savings_percent_for_lmi_customers` |
| ⚠️ "This project requires proof documents…" | `proof_documents_required` |

Every figure is Perch's. Dalton computes none of it. There is no product list
and no product picker, because the API returns neither — Perch assigns projects
by internal priority ranking.

**The Continue button is present but disabled**, reading: *"Perch's next step is
POST /enroll, which is Milestone 3. Capacity has been confirmed and stored."*
That text is generated from the `next_step` URL Perch returned — the backend
resolved `.../enrollments/enroll` to a step it knows about but hasn't built yet.

**Stored in Dalton:**
- `perch_api_calls` — two rows (`request_token`, `check_capacity`) with endpoint,
  method, request body, full response, status, duration, and `api_mode`. The
  token request's response is logged with the token value replaced by
  `[REDACTED]`.
- `perch_capacity_checks` — the normalized six fields, the `next_step` URL, and
  the complete raw JSON.
- `perch_tokens` — the enrollment token, expiry, refresh count. **Server-side only.**
- `enrollments` — `service_zip` and `utility_name` updated; status moves
  `Draft → Information Needed`.
- `perch_workflow_state` — current step and the `next_step` URL Perch handed back.
- `status_history` + `audit_logs` — the transition and the capacity check.

**Stored in Perch:** nothing durable. A capacity check is a read; the enrollment
does not exist at Perch until `POST /enroll` (Milestone 3).

**In VIPR:** nothing. VIPR receives nothing until an enrollment is submitted and
processed.

---

## Step 3 — The token expires mid-session (1 hour)

Realistic scenario from the engineering call: a rep starts an enrollment, gets
pulled away, comes back 40 minutes later and clicks *Check availability* again.

**What the rep sees:** nothing unusual. It works.

**What actually happens** depends on which clock notices first:

- **Proactive path (usual).** Our stored expiry is within the 2-minute skew
  window, so `get_valid_token()` calls **`PATCH /refresh_token`** before making
  the capacity call. One extra API call; the rep sees nothing.
- **Reactive path (clock skew, or Perch expires early).** The capacity call
  returns **403**. Per Perch's documentation we then call `PATCH /refresh_token`
  and **retry the original request exactly once**. If the retry also 403s, we
  stop — retrying forever would mask a real auth problem.

Both paths are tested. The 403 itself is written to `perch_api_calls` with
`status_code = 403` and the message *"403 enrollment_token expired — refreshing
and retrying once"*, so an auditor can see the recovery rather than just a
successful call.

---

## Step 4 — A ZIP with no capacity (`99999`)

**What the rep sees:** *No capacity available*, with an explanation that
enrollment cannot proceed and that capacity changes as projects rotate, so it's
worth re-checking later. The primary button becomes *Try a different ZIP or utility*.

**What happened:** Perch returned **503**. Per the docs, that means *"no open
solar project capacity exists for the given utility and ZIP"* and *"partners
should not proceed to POST /enroll."*

**This is deliberately not treated as an error.** Our API returns **HTTP 200**
to the browser with `capacity_available: false`. A 503 from Perch is a business
outcome the rep needs to act on, not a server failure to show a red banner for.

A genuine upstream failure (`00000`) *is* an error — HTTP 503 with
`perch_error: "PerchUnavailableError"`. The two are distinguishable.

**Stored in Dalton:** the no-capacity result is still persisted to
`perch_capacity_checks` with `capacity_available = 0` and the raw detail — we
keep the evidence that we asked and were told no. The enrollment **stays in
`Draft`** and cannot progress.

---

## Where everything lives — summary

| Data | Dalton | Perch | VIPR |
|---|---|---|---|
| Dalton Enrollment ID (`ENR-…`) | ✅ authoritative | ✗ | ✗ |
| Rep identity and attribution | ✅ authoritative | ✗ *(no agent ID in the API)* | partial, manual |
| ZIP / utility entered | ✅ | ✅ (as request input) | ✗ |
| Available capacity, savings %, proof-doc requirement | cached for audit | ✅ **authoritative** | ✗ |
| Project / product assignment | ✗ | ✅ authoritative (priority ranked) | ✗ |
| Enrollment token | ✅ server-side only | ✅ issuer | ✗ |
| Every request/response/error | ✅ `perch_api_calls` | ✅ their side | ✗ |
| Workflow position | ✅ mirrors Perch's `next_step` | ✅ authoritative | ✗ |
| Customer account, login, emails | ✗ *(deliberately)* | ✅ **authoritative** | ✗ |
| Contracts | ✗ *(library retired)* | ✅ authoritative | ✗ |
| Enrollment status / payability | mirror | ✅ | ✅ **authoritative for payability** |
| Proof-doc rejections | ✗ *(not yet)* | ✅ | ✅ surfaced here |

The line: **Dalton owns the enrollment experience and the audit trail. Perch owns
the business rules, the customer relationship, and the contracts. VIPR owns
payability.**

---

## What is mocked (and what that means for the walkthrough)

Everything Perch-side. `PerchMockClient` returns fixtures that match the
published schemas **exactly** — the six documented `project_details` fields and
nothing else, `next_step` as a URL, UUID tokens, a real 1-hour TTL enforced
by the mock itself, 403 on expiry, and 503 for no capacity.

What is **real** in the walkthrough above: the Dalton Enrollment ID, the audit
trail, token storage and refresh logic, workflow resolution, slug translation,
validation, persistence, status transitions, and every screen the rep sees.

What is **not verified**: that Perch's live API behaves as documented. We have
never made a call against staging. The most likely first-contact failure is the
API key header name (`PERCH_OPEN_ITEMS.md` Q2), followed by the NYSEG slug (Q5).

---

## What a rep *cannot* do yet

Everything after capacity. Customer data entry, POD ID collection, contract
retrieval, signing, document upload, and submission are Milestones 3–5. The
Continue button is visibly disabled with the reason stated, rather than leading
to a dead end.
