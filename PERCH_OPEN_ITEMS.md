# PERCH_OPEN_ITEMS.md

Unresolved questions for Perch engineering (Darius, CC Matt and Jordan).

Every item states **what we did in the meantime**, so nothing here is a silent
blocker — the system works today, but some of it works on an assumption that
needs confirming before it carries real enrollments.

Legend for our interim position:
- 🟢 **Implemented to documentation** — we believe this is right
- 🟡 **Implemented on an inference** — works, but unverified
- 🔴 **Deliberately not implemented** — we refused to guess

Last updated: end of Milestone 2.

---

## Blocking Milestone 2 (must resolve before the mock is replaced with staging)

### Q1 — HMAC signing scheme for `GET /markets/capacity` 🔴
The docs say pre-enrollment targeting uses *"HMAC auth, no enrollment token"* but
publish no signing scheme.

**Need:** signing algorithm, canonical string format, which secret signs it,
header name(s), timestamp/nonce requirements, and clock-skew tolerance.

**Interim:** `PerchClient.get_market_capacity()` raises
`PerchNotImplementedError` rather than guessing. The rep-facing enrollment flow
does not need it — it uses `POST /enrollments/capacity`. This blocks
pre-enrollment *targeting* (canvassing route planning, marketing), not enrollment.

### Q2 — API key header name 🟡
The call mentioned *"an API key that'll be an identifier that we can use as well."*
The header name is not published.

**Need:** exact header, and whether it's required on every request or only some.

**Interim:** `PerchHTTPClient` sends `X-Api-Key` when `PERCH_API_KEY` is set.
**This is a guess** and is the single most likely cause of a 401/403 on first
contact with staging.

### Q3 — Are product IDs ever surfaced to partners? 🟡
The call described the capacity check as *"really our product ID section"* and
cited *"130 product IDs"*, but the published capacity response contains **no
product identifier** — only three availability booleans and two savings rates.

**Need:** is the product ID resolved internally and never exposed? If it *is*
exposed somewhere (enroll response? status?), we want to store it, because it's
the natural correlation key against VIPR reporting.

**Interim:** we store none, because none is returned. Our schema has room to add
it without a rewrite.

### Q4 — Complete `next_step` URL vocabulary 🟡
We build the workflow from the `next_step` URL on each response. We have seen
exactly one value: `.../enrollments/enroll`.

**Need:** every URL the workflow can return, and under what conditions.

**Interim:** we map known path suffixes to steps, and an **unrecognized
`next_step` is flagged loudly** (audit-logged, surfaced in the UI, exposed via
`GET /api/perch/diagnostics`) rather than silently ignored. So an unknown URL
degrades to "we don't know what to do next" instead of skipping a required step.

### Q5 — NYSEG's utility slug 🟡
NYSEG appears in the published POD ID table but its row in the slug-mapping
table was below the fold in our screenshot. We also can't confirm the list is
complete — five slugs were visible.

**Need:** the full utility-name→slug table.

**Interim:** we inferred `nyseg` from the naming convention and **flagged it
unconfirmed** in the database (`perch_utilities.slug_confirmed = 0`) and in
`GET /api/perch/diagnostics`. A wrong slug fails silently at Perch's end — no
capacity found, no explanatory error — so this is higher risk than it looks.

### Q6 — Does `POST /token` need any credentials? 🟡
The Swagger UI shows an unlocked padlock on `/token`, implying no auth, but the
call described *"exclusive tokens"* and partner-specific setup.

**Need:** what identifies *us* when requesting a token.

**Interim:** we POST an empty body plus the API key header from Q2.

---

## Blocking Milestone 3 (enrollment submission)

### Q7 — `POST /enroll` request schema
**Need:** required and optional fields; which are conditional per utility;
validation rules and error shapes.

**Known so far:** meter number, account number, name, email (from the call);
POD ID for NYSEG / Central Hudson / Rochester G&E (published formats).

### Q8 — Idempotency
**Need:** is there an idempotency key? What happens if we retry a submission
after a network timeout — duplicate enrollment, or dedupe?

**Why it matters:** at 100+ enrollments/day across 40 reps, a timeout-then-retry
will happen in the first week. Without an idempotency key we have to build
speculative client-side dedupe, which is worse.

### Q9 — Does `proof_documents_required` apply to everyone, or only LMI?
The capacity response has one project-level `proof_documents_required` boolean.
The call distinguished state/basic LMI (no proof doc) from the IRA federal
program (proof doc required).

**Need:** does `true` mean "all customers on this project need proof docs" or
"LMI customers on this project need proof docs"? Our UI wording depends on it.

**Interim:** we display it as a project-level requirement without claiming who
it applies to.

### Q10 — Rep/agent ID field
Flagged on the call as a possible enhancement. **Need:** is it being added? If
so, what field and when?

**Interim:** Dalton tracks rep attribution entirely internally against the Dalton
Enrollment ID. Works, but means reconciliation with VIPR agent-level reporting
requires a manual join.

---

## Blocking Milestone 4 (contracts)

### Q11 — Contract retrieval mechanics
**Need:** which endpoint returns the contract; is the PDF **pre-filled with
customer data** by Perch, or do we fill it; what identifies the returned contract.

### Q12 — Contract versioning ⚠️ *explicitly deferred by Perch on the call*
Matt: *"I would like to say yes, Charlie. I feel like that's something we should
just validate with [engineering]."*

**Need:** if legal updates a contract, does the API return the new version
automatically? Is there a version identifier we should store alongside the
signature? What happens to an in-flight enrollment signed against the prior
version?

**Known mitigation:** Perch shuts a project down during contract changes and
relaunches; priority stacking rotates another project in.

### Q13 — `Submit Contracts Acceptance` payload
Named in the 403 documentation as an enrollment-token endpoint, but the schema
isn't published.

**Need:** does it want a signature image, a hash, a typed attestation, a
timestamp+IP bundle? This determines whether our existing signature-capture UI
is reusable or needs rebuilding.

**Risk:** this is the **highest-uncertainty item in the whole integration.** If
Perch requires a hosted/redirect signing flow, our Dalton-side signing UI becomes
evidence capture only.

---

## Blocking Milestone 5 (LMI and proof documents)

### Q14 — Document upload mechanics
**Need:** multipart or base64; size limits; accepted MIME types; one endpoint or
per-document-type; how a document is associated with an enrollment.

### Q15 — Complete `source_type` vocabulary
We have seen two values: `self_attestation_qualifying_income` and
`proof_doc_free_reduced_school_lunch_letter`. The call also referenced a LIHEAP
naming convention (*"send that to us as proof.lyheap"*), which doesn't obviously
match the `proof_doc_*` pattern.

**Need:** the full table, and clarification of which convention is current.

### Q16 — Who supplies self-attestation income limits?
The docs say the customer attests income *"falls within the applicable income
limit for their household size"* but not who defines the limit. Charlie raised
this on the call; unresolved.

**Need:** does Perch return thresholds by household size, or do we maintain the
NY SMI table ourselves (and own updating it annually)?

**Interim:** we have a hardcoded NY 80% SMI table from Phase 1. **Unvalidated.**

### Q17 — SharePoint resubmission specifics
**Need:** exact filename convention (the call sketched
`FirstName-LastName-AccountNumber-EID` but didn't finalize it), folder location,
confirmed scrape cadence, and how we learn a resubmission was accepted.

### Q18 — Magic-link timeline
Perch indicated early September for customer self-upload via magic link.

**Need:** confirmation, and whether it replaces or supplements SharePoint.

---

## Blocking Milestone 6 (reconciliation and reporting)

### Q19 — `GET /status`
Named in the 403 docs. **Need:** what it returns, at what granularity, and
whether it replaces or complements VIPR for status and payability.

### Q20 — VIPR correlation keys
**Need:** which identifiers appear in VIPR that we can join on. Without a shared
key, matching Dalton enrollments to VIPR payability rows is fuzzy matching on
name and account number — fragile at volume.

---

## Future enhancements

- **Webhooks.** Everything today is poll-or-portal. Webhooks for status changes,
  proof-doc rejections, and project shutdowns would remove most reconciliation lag.
- **Capacity as a live feed.** The capacity report is currently a manually
  updated sheet on a ~10-day cadence. A capacity API for planning (distinct from
  the per-enrollment check) would let us direct canvassing at real availability.
- **Project shutdown notifications.** Perch shuts projects down for contract
  updates and relies on priority stacking. A programmatic signal would let us
  warn reps before they walk into a dead territory.

---

## Nice-to-have API requests

1. **Agent/rep ID as a first-class field** (Q10). Removes the manual join.
2. **An idempotency key on `POST /enroll`** (Q8). Prevents duplicates at volume.
3. **Product ID echoed back on enrollment** (Q3). Gives us a clean VIPR join key.
4. **Structured validation errors** — field name + reason, not just a message
   string. The POD ID example Perch gave is exactly the case where a
   `{"field": "pod_id", "reason": "length", "expected": 15}` shape would let us
   put the error next to the right input instead of in a banner.
5. **A `sandbox` flag or test ZIP set** in staging, so partners can exercise
   no-capacity and rejection paths deliberately rather than hunting for a ZIP
   that happens to be full.
6. **An OpenAPI spec file** (the Swagger UI references
   `/api-docs/affiliate_partners/v1/enrollments/openapi.yaml`) — we could
   generate our client from it and stop hand-transcribing schemas from screenshots.
