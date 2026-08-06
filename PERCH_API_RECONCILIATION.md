# Perch API Reconciliation Report

**Sources (authoritative, in priority order):**
1. Perch Partner Enrollment API v1 Swagger docs (4 screenshots — actual schemas)
2. Engineering call transcript (Matt, Jordan, Darius / Chris, Charlie)
3. Our prior `ARCHITECTURE_REVIEW.md` §8 assumptions
4. Milestone 1 as actually implemented

Where the screenshots and the transcript disagree, the screenshots win — they're
the shipped contract. Where the transcript adds operational detail the docs don't
cover (token TTL, resubmission process, rep attribution), the transcript is the
source.

---

## 0. The headline finding

**`next_step` is a URL, not an enum string.**

```json
"next_step": "https://api.perchenergy.com/affiliate_partners/v1/enrollments/enroll"
```

And from the LMI section of the docs:

> *"Partners do not select the method — follow the `next_step` URL in each response
> to know which LMI steps (if any) are required."*

This is a hypermedia-driven API. Perch owns the enrollment state machine and tells
us where to go next on every response. We assumed `next_step` was a token like
`"collect_customer_info"` that our client would branch on with a `switch`. It isn't.

That single fact reframes several others below, and it's the thing that should
drive Milestone 2's design. It also means our fixed 5-step wizard
(Project → Bill → Contact → LMI → Agreement) is a *UX* sequence, not a *workflow*
— the actual required steps are discovered at runtime from Perch.

---

## 1. Assumption-by-assumption classification

### Authentication & token management

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| A1 | OAuth2 client-credentials at `POST /oauth/token` | `POST /affiliate_partners/v1/enrollments/token`, returns an `enrollment_token` (UUID form: `550e8400-e29b-41d4-a716-446655440000`) | **Incorrect** |
| A2 | `Authorization: Bearer <token>` header | `X-Enrollment-Token: <uuid>` header | **Incorrect** |
| A3 | Refresh = re-request a token | Dedicated `PATCH /refresh_token` endpoint | **Incorrect** |
| A4 | Token TTL ~3600s (mock assumed) | **30 minutes** (transcript, explicit) | **Incorrect** |
| A5 | Refresh should be fully server-side, transparent to callers | Confirmed — and *more* important than we thought. Transcript: *"you'll just have to build the system to ping back for a new token to submit the proof doc if you do it at a different time."* A rep who uploads a proof doc an hour later has a dead token. | **Confirmed** |
| A6 | One auth scheme | **Two.** Enrollment token for the enrollment flow; **HMAC auth (no enrollment token)** for `GET /markets/capacity` pre-enrollment targeting | **Incorrect (incomplete)** |
| A7 | Expired token surfaces as an error we handle | `403` with an explicit instruction: call `PATCH /refresh_token`, then **retry the original request**. Returned by *every* enrollment-token endpoint. | **Partially confirmed** — we need automatic retry-after-refresh, which we didn't build |

### Pre-enrollment vs enrollment flow

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| B1 | One capacity endpoint under a "pre-enrollment" path | **Two distinct flows with separate base URLs:** enrollment `…/v1/enrollments`, pre-enrollment `…/v1/markets` | **Incorrect** |
| B2 | Capacity is the first call in the rep flow | Correct for the enrollment flow: `POST /token` → `POST /capacity` → `POST /enroll`. Docs: *"Call after POST /token and before POST /enroll."* | **Confirmed** |
| B3 | Pre-enrollment and enrollment are the same call at different times | They are genuinely different endpoints, different auth, different purpose. `GET /markets/capacity` is for *targeting before a signup exists* (marketing, canvassing route planning). We built neither correctly, and didn't build markets at all. | **Incorrect** |

### Capacity API & product selection

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| C1 | Request `{zip_code, utility}` | `{zip_code, utility_name}` — and `utility_name` must be a **slug** (`consolidated-edison-ny`), not a display name | **Partially confirmed** |
| C2 | Response is a **list of products** the rep picks from | Response is a **single `project_details` object**. No product array. No product names. No per-product IDs in the response. | **Incorrect** |
| C3 | Each product carries `available_capacity_kw` | No kW figure anywhere. Capacity is expressed as **three booleans**: `residential_capacity_available`, `small_commercial_capacity_available`, `lmi_capacity_available` | **Incorrect** |
| C4 | Each product carries its own `savings_percentage` | **Two** rates per project: `savings_percent_for_residential_and_commercial_customers` and `savings_percent_for_lmi_customers` | **Incorrect** |
| C5 | `customer_type` is an enum field on a product | The three segments exist, but as availability booleans. Note it's **small commercial**, not "commercial". | **Partially confirmed** |
| C6 | No capacity = empty product list, HTTP 200 | **`503 Service Unavailable`** when no open capacity exists. Docs: *"Partners should not proceed to POST /enroll until this endpoint succeeds."* | **Incorrect** — and consequential; we treat 503 as a transport failure to retry, Perch uses it as a business outcome |
| C7 | Rep selects a product | **Perch assigns projects by priority ranking.** Transcript: *"we assign you guys to projects in our system… there will always be a project. So it's just more of a priority ranking."* Product selection as a rep action does not exist. | **Incorrect** |
| C8 | Capacity is authoritative and must be re-checked before submit | Confirmed emphatically. Docs: *"eligibility and savings rates that Perch will enforce at enroll."* | **Confirmed** |

### Product IDs

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| D1 | Product IDs are returned to us and we reference them | Transcript: *"this capacity check… is really our product ID section"* and *"we have 130 product IDs."* But the capacity **response schema contains no product_id field.** The product ID logic appears to be resolved *internally by Perch* and expressed to us as the derived booleans + savings rates. | **Still unknown** — needs Perch confirmation (Q3 below). Our `perch_products.perch_product_id` column may have nothing to store. |

### Dynamic customer experience

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| E1 | Render the enrollment experience from the API response rather than local rules | **Confirmed, strongly.** Transcript: *"you'll be able to translate that into your own customer experience and say, hey, you may qualify for a 20% discount for LMI qualifying customers because this data point will actually be sent to you."* Maps exactly to `savings_percent_for_lmi_customers: 20`. | **Confirmed** |
| E2 | Dynamic rendering means "render a product list" | Dynamic rendering means: render *segment availability*, *savings rates*, *whether proof docs are required*, and *follow next_step*. Different shape, same principle. | **Partially confirmed** |
| E3 | Fallback when the customer doesn't qualify for the best rate | Confirmed: *"it might reflect that there's other open-resi capacity, maybe at a different percentage… that's to fall back on."* The three booleans + two rates support exactly this. | **Confirmed** |

### LMI logic

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| F1 | Three LMI paths: document upload / self-attestation / N/A, **rep selects** | **Two NY verification methods**, and **Perch selects**: (1) **Empower Zone** — based on service address, *no income documentation required*; (2) **DAC + Self-Attestation** — customer in a DAC zone signs a self-attestation form. Docs: *"The verification method that applies to a given enrollment is determined by Perch Energy based on the project. Partners do not select the method."* | **Incorrect** |
| F2 | LMI requirement is a per-product boolean we read | `proof_documents_required: true` is a **project-level** field. Transcript distinguishes *state/basic LMI* (self-attestation or Empower Zone → **no proof doc**) from the **IRA federal program** (→ **proof doc required**). | **Partially confirmed** |
| F3 | Self-attestation needs a household-size income table we maintain | Source type confirmed: `self_attestation_qualifying_income`. Docs reference *"the applicable income limit for their household size"* but **don't say who supplies the limit.** Charlie raised exactly this on the call and it wasn't resolved. | **Still unknown** |
| F4 | Our hardcoded NY 80% SMI table is correct | Unvalidated. Still unknown whether Perch supplies thresholds or expects us to. | **Still unknown** |

### Proof document handling

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| G1 | Proof docs are a per-product array of required types | Capacity returns a single project-level boolean. Specific accepted documents are a **separate `source_type` vocabulary**, e.g. `proof_doc_free_reduced_school_lunch_letter` | **Partially confirmed** |
| G2 | We invented type strings like `"lmi_proof"` | Real identifiers are `proof_doc_*` / `self_attestation_*` prefixed source types. Transcript: *"if you guys collect a LIHEAP document, you're going to want to send that to us as proof.lyheap, and then that will tag in our system when we ingest it."* | **Incorrect** |
| G3 | Rejected proof docs come back via API | **No.** They come back via **VIPR**. Resubmission interim process is a **SharePoint folder with a strict filename convention** (`FirstName-LastName-AccountNumber-EID` style), scraped weekly by Perch engineering, overwriting prior documents. A **magic-link** customer upload flow is promised for **~early September**. | **Incorrect** — and this materially changes the M4 design |

### Contract retrieval & versioning

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| H1 | Perch owns the contract library; we fetch the PDF | **Confirmed, emphatically.** *"we have 15 different contract forms. This will just — you'll ping it and you'll know exactly, it'll give you the PDF on what to go sign."* … *"It'll just pull the right contract for you guys."* | **Confirmed** |
| H2 | Our local contract generators are obsolete | **Confirmed.** Charlie on the call: *"initially I thought I was going to be building a library and then pulling from my own library… They're basically telling me, oh, we already have the library built."* | **Confirmed** |
| H3 | There's a contract-acceptance step | Confirmed indirectly — the 403 description names **"Submit Contracts Acceptance"** as an enrollment-token endpoint | **Partially confirmed** (endpoint exists; schema unseen) |
| H4 | Contract versioning is handled by the API returning the latest approved version | **Explicitly deferred by Perch on the call.** Matt: *"I would like to say yes, Charlie. I feel like that's something we should just validate with [engineering]."* Known mitigation: Perch **shuts a project down** during contract changes and relaunches it, and **priority stacking** means another project takes over. | **Still unknown** |

### Customer account creation & communications

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| I1 | Dalton creates customer logins and a customer portal | **Incorrect — this is Perch's job.** *"We'll create the account, send out emails, customer comms, all that kind of stuff. So you guys don't need to worry about username and password and all that kind of stuff."* | **Incorrect** |
| I2 | The platform is Arcadia | It's **Perch** (perchenergy.com). Arcadia was the wrong mental model; Matt corrected it on the call. | **Incorrect** |
| I3 | We send the customer a magic link to sign | Signing still happens on our side (we present the Perch contract), but **account creation, credentials, and all customer email comms are Perch's.** | **Partially confirmed** |

### VIPR & rep attribution

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| J1 | VIPR is the downstream system for status, payability, proof-doc issues | **Confirmed.** *"Viper's still where you're gonna be statused and understand payability and hey, the proof doc didn't come in correctly."* | **Confirmed** |
| J2 | The Partner API may not expose an agent/rep ID; Dalton tracks internally | **Confirmed.** *"Charlie, I don't remember seeing an agent ID aspect to the API."* Proposed workaround on the call: Dalton tracks rep IDs internally (`DSM 001`), possibly a weekly upload to Perch, possibly email as unique identifier. Perch flagged it as a candidate API enhancement. | **Confirmed** |
| J3 | Reps get reporting from Dalton | Partially — reps **still log into VIPR** for payability/status reporting. Dalton reporting is additive, not a replacement. | **Partially confirmed** |
| J4 | There's a status API | **`GET /status` exists** (named in the 403 description). Relationship to VIPR unconfirmed. | **Still unknown** |

### OCR, audit, caching, error handling

| # | Our assumption | Reality | Verdict |
|---|---|---|---|
| K1 | Dalton owns OCR | **Confirmed.** Perch expects clean, well-organized submissions; OCR is our quality lever. | **Confirmed** |
| K2 | POD ID is a required secondary identifier for some utilities | **Confirmed *and now specified*:** NYSEG = 15 digits starting `N01`; Central Hudson = 10 digits; Rochester G&E = 15 digits starting `R01`. Transcript confirms Perch rejects a wrong-length POD ID. | **Confirmed** |
| K3 | Store every request/response/identifier/timestamp/error | **Confirmed.** Transcript: *"this is QA/QC as well and auditing stuff, timestamps, signatures, IP addresses, all that good stuff."* Perch also issues an **API key that acts as an identifier** for reconciliation. | **Confirmed** |
| K4 | Treat Perch as authoritative; don't cache products/capacity as truth | **Confirmed — asked and answered directly on the call.** Chris: *"should we be caching all the projects and availabilities locally… should we always trust your paths?"* Matt: *"You should. But you should always inspect what you expect."* | **Confirmed** |
| K5 | Snapshots for audit only, never as a read cache | Confirmed by implication of K4 + C8. | **Confirmed** |
| K6 | Errors are transport failures | Perch uses **HTTP status codes as business semantics**: 503 = no capacity (don't proceed), 403 = token expired (refresh + retry). Validation errors come back as structured rejections (e.g. bad POD ID length). | **Incorrect** |

---

## 2. What the meeting validated

These were correct calls and should be treated as settled:

1. **The adapter/ABC boundary was the right architectural bet.** Nearly every assumption below the boundary turned out wrong — endpoints, auth header, token TTL, response shape, error semantics. Because all of it is contained in `client.py` / `mock_client.py`, correcting it doesn't touch routes, the audit spine, the DB access layer, or the frontend contract. This is the single best decision from Milestone 1 and it just paid for itself.
2. **Dalton Enrollment ID before any Perch call.** Now clearly right: Perch's enrollment token is *session*-scoped and expires in 30 minutes, so it can never be the durable key. Our `ENR-YYYY-NNNNNN` is the only stable identifier across bill upload → signing → proof-doc resubmission → VIPR reconciliation.
3. **Server-side-only token containment.** Vindicated harder than expected — a 30-minute TTL with mandatory refresh-and-retry is exactly the kind of thing you cannot push into a browser.
4. **`perch_api_calls` as the audit spine.** Perch independently described the same requirement (timestamps, signatures, IP addresses) and issues an API key identifier for correlation.
5. **Snapshot-for-audit, never-as-cache.** Asked and answered verbatim on the call.
6. **Not building a local contract library.** Confirmed emphatically; Charlie reached the same conclusion live.
7. **Not hardcoding projects locally.** Confirmed — Perch has ~3,400 NY projects with priority stacking and weekly shutdowns. Any local mirror would be wrong within days.
8. **Rep attribution stays internal to Dalton.** Confirmed as an actual gap in Perch's API, not our misunderstanding.
9. **Dalton owns OCR.**
10. **Migration discipline** (additive migrations, ledger) — unaffected and still correct.

---

## 3. What must change

Ordered by blast radius.

### 3.1 The workflow becomes hypermedia-driven (largest change)

We modeled a **fixed step sequence** with local branching. Perch models a
**server-driven state machine** where each response hands us the next URL.

What this means concretely:
- Introduce a `perch_workflow_state` concept per enrollment: current `next_step`
  URL, last response, what Perch says is required now.
- The adapter gains a `follow_next_step()` primitive rather than a hardcoded
  method per endpoint.
- Our wizard's *screens* stay (reps need a sane UX), but which screens are
  **required** — LMI or not, proof doc or not, which contract — is read from
  Perch, never decided locally.

### 3.2 Rewrite the client to the real contract

| Item | From | To |
|---|---|---|
| Token endpoint | `POST /oauth/token` | `POST /affiliate_partners/v1/enrollments/token` |
| Refresh | re-request token | `PATCH /refresh_token` |
| Auth header | `Authorization: Bearer` | `X-Enrollment-Token` |
| TTL | 3600s | 1800s, with refresh-and-retry on 403 |
| Capacity endpoint | `/v1/pre-enrollment/capacity` | `POST /affiliate_partners/v1/enrollments/capacity` |
| Capacity params | `{zip_code, utility}` | `{zip_code, utility_name}` with **slug** |
| No-capacity | HTTP 200 + empty list | **HTTP 503** = business outcome, block progression to enroll |
| Pre-enrollment | (not built) | `GET /affiliate_partners/v1/markets/capacity`, **HMAC auth** |

### 3.3 Replace `perch_products` with `perch_capacity_checks`

The current table models a product list that does not exist in the response.
Replace with a table matching `project_details`:

```
perch_capacity_checks
  enrollment_id, zip_code, utility_slug,
  residential_capacity_available      BOOLEAN
  small_commercial_capacity_available BOOLEAN
  lmi_capacity_available              BOOLEAN
  proof_documents_required            BOOLEAN
  savings_percent_res_commercial      NUMERIC
  savings_percent_lmi                 NUMERIC
  next_step_url                       TEXT
  raw_response_json                   TEXT
```
Additive migration `002`; `perch_products` gets dropped once nothing reads it.

### 3.4 Add a utility slug registry

Five confirmed mappings so far (`national-grid-ny`, `consolidated-edison-ny`,
`orange-and-rockland`, `central-hudson-gas-electric`, `rochester-gas-electric`),
with more below the screenshot fold. This becomes a real reference table, not a
free-text field — a display-name/slug mismatch silently breaks capacity checks.

### 3.5 Add POD ID with per-utility validation

Now fully specified for three utilities. Validate client-side *and* server-side
before calling Perch, so a rep sees "that looks like 14 digits, NYSEG needs 15"
rather than an opaque API rejection.

### 3.6 Retire the Dalton customer account/portal

Perch creates accounts, issues credentials, and sends all customer
communications. The Phase 1 customer login screen, customer portal, and
rep-sets-customer-password fields are **obsolete and should be removed** — they
would create a second, conflicting account for every customer and confuse the
comms Perch is already sending.

*Signing evidence capture stays* — we still present the Perch contract and
capture the signature. What goes is the account/credential layer.

### 3.7 Rework LMI to be Perch-directed

Remove the rep-facing 3-way selector (document / self-attest / N/A). Replace
with: Perch tells us the method; we render the corresponding collection step.
Add `self_attestation_qualifying_income` and the real `proof_doc_*` source-type
vocabulary. Empower Zone means **no documentation at all** — a path we don't
currently model.

### 3.8 Redesign proof-doc resubmission around the real process

Not an API flow. Rejections surface in **VIPR**; resubmission is a **SharePoint
drop with a strict filename convention**, weekly scraped. Dalton should:
- store the exact filename convention as data, not code;
- generate correctly-named files automatically (this is a genuine differentiator
  — it's where partners will make mistakes);
- track resubmission attempts against the original enrollment without losing
  history;
- be ready to swap to the **magic-link** flow when Perch ships it (~September).

### 3.9 Error handling becomes semantic

403 → refresh + auto-retry once. 503 on capacity → "no capacity in this area,"
a normal UI state, not an error banner. Validation rejections → field-level
messages (the POD ID case is the worked example Perch gave us).

---

## 4. What should NOT change

- The adapter boundary and the mock/live factory. **Do not** collapse it now that
  we have real docs — the mock becomes *more* valuable, since staging isn't up
  yet and production is explicitly not open.
- Dalton Enrollment ID as the immutable spine.
- `perch_api_calls` audit logging (extend it, don't restructure it).
- Snapshot-for-audit semantics.
- Server-side token containment.
- Rep authentication, internal rep IDs, QA workflow, status history, audit logs.
- OCR ownership and the extraction service.
- Migration runner and additive-migration discipline.
- The visual design.
- The three existing test suites as regression cover.

---

## 5. Which Milestone 1 work was correct

**Keep as-is:** migration runner (`db/migrate.py`), `perch_api_calls`,
`perch_capacity_snapshots` (raw storage — shape-agnostic by design, so it
survives), the adapter/route boundary, token containment + audit redaction,
draft-before-Perch-call ordering, RBAC on the Perch routes, the mode-switch
factory, and the credential-containment tests.

**Correct, wrong details:** `token_manager` (right lifecycle, wrong endpoint,
TTL, and refresh mechanism), `PerchHTTPClient` (right structure, every path and
shape wrong), `fetch_capacity` (right orchestration, wrong normalization).

**Discard:** `perch_products` table and normalization, the product-card UI and
`selectPerchProduct()`, the mock's product-array fixtures, and `next_step`-as-enum.

Roughly: **structure survives, contract details don't** — which is what the
adapter layer was built to make survivable. The rewrite is contained to
`client.py`, `mock_client.py`, `adapter.py::_normalize_product`, one migration,
and one frontend screen.

---

## 6. Roadmap — reordered

**Previous plan:** M2 = OCR expansion → M3 = contracts → M4 = submission → M5 = VIPR.

**Why it must change:** M2 as previously scoped (OCR field expansion) builds
*more* on top of an API contract we now know is wrong in its fundamentals. Every
additional layer built on the wrong shapes increases rework. And OCR feeds
submission, which is two milestones out — it's not on the critical path.

| Milestone | Scope | Gate |
|---|---|---|
| **M2 (next)** | **Correct the Perch contract + hypermedia workflow engine.** Real token/refresh/`X-Enrollment-Token`/30-min; real capacity request+response; utility slug registry; 503 & 403 semantics with refresh-and-retry; `next_step`-driven workflow state; rewrite the mock to the documented shapes; migration `002`. | Mock matches documented schemas exactly; then first successful call against **staging** |
| **M3** | **Enrollment submission** (`POST /enroll`) + POD ID validation + customer data mapping. This is where OCR fields actually get consumed — so OCR expansion (POD ID, meter) folds in here rather than standing alone. | Staging accepts a test enrollment |
| **M4** | **Contract retrieval + acceptance.** Fetch the Perch PDF, present, sign, `Submit Contracts Acceptance`. Retire the local contract library. | Staging returns a contract and accepts signature |
| **M5** | **LMI + proof documents.** Perch-directed method, `proof_doc_*` source types, correctly-named bundle generation, SharePoint resubmission workflow with history preservation. | Proof doc accepted end-to-end |
| **M6** | **Reconciliation & reporting.** `GET /status`, VIPR correlation, rep attribution export, drop-off analytics. | — |
| **M7** | **Cleanup + deployment.** Remove customer portal/credentials, drop `projects` and local contract generators, S3 for documents, Postgres, AWS. | — |

**Two things moved earlier than planned:** removing the customer account/portal
(now known to be actively wrong, not just redundant) and the utility slug
registry (a silent-failure risk).

**One thing moved later:** standalone OCR expansion — merged into M3 where the
fields are consumed.

---

## 7. Questions still requiring Perch engineering

Sharply narrowed from twelve. Ordered by what they block.

**Blocking Milestone 2:**
1. **HMAC scheme for `GET /markets/capacity`** — signing algorithm, canonical string, which secret, clock-skew tolerance. Entirely unspecified in what we've seen.
2. **`PATCH /refresh_token` semantics** — does it need the expired token, a separate refresh credential, or the API key? Does the enrollment token survive a refresh (same UUID) or rotate?
3. **Product IDs.** The transcript calls capacity "our product ID section" and cites 130 product IDs, but the response schema exposes none. Is the product ID resolved internally and never surfaced? If it *is* surfaced somewhere, where — and do we need to store it for reconciliation with VIPR?
4. **Full `next_step` URL set.** Every URL the workflow can hand back, so we can build the state machine deliberately rather than discovering endpoints in production.
5. **Complete utility slug list** (the screenshot cut off after five).

**Blocking Milestone 3:**
6. `POST /enroll` request schema — required fields, and which are conditional by utility.
7. **Idempotency** — is there an idempotency key? What happens on a retried submission after a timeout? (This is the failure mode most likely to create duplicate enrollments at 100+/day.)
8. Does `proof_documents_required: true` apply only to LMI enrollments on that project, or to every customer on it?

**Blocking Milestone 4:**
9. **Contract versioning** — the question Matt explicitly deferred. If legal updates a contract mid-week, does the API return the new version automatically? Is there a version identifier we should store with the signature for audit?
10. Contract retrieval: is the PDF pre-filled with customer data, or do we fill it? What does `Submit Contracts Acceptance` expect — a signature image, a hash, an attestation payload?

**Blocking Milestone 5:**
11. Document upload: multipart or base64, size limits, MIME types, and the exact `source_type` vocabulary (we've seen two of what is presumably a longer table).
12. **Who supplies the self-attestation income limits** by household size — Perch, or do we maintain the table? (Charlie raised this live; unresolved.)
13. Exact SharePoint filename convention, and the confirmed scrape cadence.

**Blocking Milestone 6:**
14. `GET /status` — what it returns, and whether it replaces or complements VIPR for status/payability.
15. Rep/agent ID: is it being added to the API? Perch flagged it as a possible enhancement on the call.

**Operational:**
16. Staging credentials + toolkit access (promised Friday), rate limits, and whether the mock's assumptions can be validated against staging before production.
