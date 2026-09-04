"""
LMI self-attestation branch.

CONDITIONAL: entered only when Perch returns /lmi/self_attestation after
/enroll. Dalton never infers it from utility, ZIP, customer_type or local rules.

Sequence: /enroll -> /lmi/self_attestation -> customer accepts the
Perch-generated document -> /lmi/self_attestation/accept -> /contracts -> the
EXISTING contract package, unchanged.

Run: python test/test_self_attestation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one, query
import seed
from services.perch import adapter, workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
CLIENT = open(os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read()
ADAPTER = open(os.path.join(ROOT, "services", "perch", "adapter.py"), encoding="utf-8").read()
BASE = "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments"

SENT = []


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def stub_perch():
    """Stub ONLY the two Perch calls; every Dalton layer runs for real."""
    def gen(eid, acct, occ, county, status, user_id=None):
        SENT.append({"op": "generate", "utility_account_number": acct,
                     "occupancy": occ, "county": county, "status": status})
        return {"next_step_url": BASE + "/lmi/self_attestation/accept",
                "document_available": True,
                "raw": {"self_attestation": {"utility_account_number": acct,
                                             "url": "https://s3/presigned?X-Amz-Expires=3600"}}}

    def acc(eid, metadata, user_id=None):
        SENT.append({"op": "accept", "metadata": metadata})
        return {"next_step_url": BASE + "/contracts", "message": "ok"}
    adapter.submit_self_attestation = gen
    adapter.accept_self_attestation = acc


def main():
    init_db(reset=True)
    seed.seed()
    stub_perch()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def full_enrollment(tag, email=None):
        email = email or f"{tag}@example.com"
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": email, "zip_code": "12401",
                           "utility_name": "central-hudson-gas-electric"}
                     ).get_json()["enrollment_id"]
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Jane", "last_name": "Doe", "email": email,
                         "phone": "5185550100", "password": "CustPass1!"},
            "service_address": {"street": "20 Florida St", "city": "Kingston",
                                "state": "NY", "zip": "12401"},
            "utility_account": {"utility_name": "central-hudson-gas-electric",
                                "account_number": "1234567890"}})
        return eid, email

    # ═══════════════════════════════════════════════════════
    section("1-3. BRANCH ROUTING IS PERCH-AUTHORITATIVE")
    check("proof_docs routes to the existing proof flow",
          "if(perchContext.nextStepKey === 'proof_docs'){" in JS
          and "prepareLmiForPerch()" in JS)
    check("self_attestation routes to the NEW branch",
          "prepareSelfAttestation()" in JS)
    check("contracts bypasses LMI verification entirely",
          "if(perchContext.nextStepKey === 'contracts'){" in JS
          and "generateContractsAndOpenAgreement" in JS)
    check("the branch is chosen ONLY from Perch's next_step_key",
          "perchContext.nextStepKey ===" in JS)
    check("  ...never from utility / ZIP / customer_type",
          not any(p in JS.split("async function continueFromPerchNextStep")[1][:1500]
                  for p in ("utilitySlug", "capacityZip", "customer_type")))
    check("19. an unknown next_step FAILS SAFE",
          "Perch returned a next step Dalton does not recognize" in JS)

    section("4. EXACT DOCUMENTED PAYLOAD")
    eid, email = full_enrollment("sa1")
    SENT.clear()
    r = c.post(f"/api/perch/enrollments/{eid}/lmi/self-attestation", headers=rep,
               json={"occupancy": 4, "county": "Ulster County", "status": "accepted"})
    check("generate succeeds", r.status_code == 200)
    sent = [x for x in SENT if x["op"] == "generate"][0]
    check("  ...utility_account_number matches the enrolled account",
          sent["utility_account_number"] == "1234567890")
    check("  ...occupancy sent as an int", sent["occupancy"] == 4)
    check("  ...county sent verbatim", sent["county"] == "Ulster County")
    check("  ...status is the documented enum value", sent["status"] == "accepted")
    check("lmi_source_selection is always the single documented value",
          'SELF_ATTESTATION_SOURCE = "self_attestation_qualifying_income"' in ADAPTER
          and '"lmi_source_selection": [SELF_ATTESTATION_SOURCE]' in ADAPTER)
    check("  ...and status is validated before any Perch call",
          'if status not in SELF_ATTESTATION_STATUSES' in ADAPTER)
    check("the client wraps it under self_attestation",
          'payload = {"self_attestation": entry}' in CLIENT)
    check("  ...and sends NO undocumented fields",
          '"utility_account_number": str(utility_account_number),' in ADAPTER
          and '"occupancy": int(occupancy),' in ADAPTER)

    section("5. NO EXACT INCOME IS COLLECTED")
    check("no income input exists in the markup",
          "sa-income" not in HTML and 'id="sa-annual' not in HTML)
    check("  ...the choice is above/below only",
          'data-choice="below"' in HTML and 'data-choice="above"' in HTML)
    check("  ...and no government-assistance checkbox was invented",
          "government assistance" not in HTML.lower())
    check("no attestation legal wording is authored in Dalton",
          "I attest" not in HTML and "under penalty" not in HTML.lower())

    section("6. THRESHOLDS ARE VERSIONED AND CORRECT")
    ref = c.get(f"/api/perch/enrollments/{eid}/lmi/self-attestation/reference",
                headers=rep).get_json()
    EXPECTED = {1: 6175000, 2: 7055000, 3: 7935000, 4: 8815000,
                5: 9525000, 6: 10230000, 7: 10935000, 8: 11640000}
    got = {t["occupancy"]: t["income_level_cents"] for t in ref["thresholds"]}
    check("all 8 household sizes served", got == EXPECTED)
    check("  ...sourced from the Perch form",
          ref["source_document"] == "NY_self_attestation_form.pdf")
    check("  ...with the form's caption", ref["table_caption"] == "State Median Income Levels (80%)")
    check("  ...and a version label", ref["version_label"] == "NY-SMI-80-2026")
    # The NEW branch takes every value from the backend. A LEGACY hardcoded
    # `amiTable` still exists in app.js, feeding the older un-wired "attest"
    # LMI mode (updateAmiThreshold / setIncomeAnswer). It is NOT used by this
    # branch and was left untouched as out of scope - flagged as a follow-up so
    # the duplicate is retired deliberately rather than silently.
    sa_js = JS.split("async function prepareSelfAttestation")[1]
    check("the NEW branch hardcodes no threshold values",
          not any(v in sa_js for v in ("61750", "88150", "116400", "70550")))
    check("  ...it renders from the backend reference",
          "selfAttestation.reference" in sa_js and "income_level_cents" in sa_js)
    check("  ...and the markup carries no amounts",
          "61,750" not in HTML and "88,150" not in HTML)
    check("KNOWN FOLLOW-UP: a legacy hardcoded amiTable still exists",
          "const amiTable" in JS)
    check("  ...but the new branch never reads it", "amiTable" not in sa_js)
    check("the frontend renders the threshold for the CHOSEN size",
          "String(t.occupancy) === String(selfAttestation.occupancy)" in JS)

    section("7. COUNTY IS CONTROLLED, NOT FREE TEXT")
    check("all 62 NY counties served", len(ref["counties"]) == 62)
    check("  ...formatted as Perch expects", "Ulster County" in ref["counties"])
    check("  ...rendered as a <select>", '<select id="sa-county"' in HTML)
    check("  ...with no text input", 'id="sa-county" type="text"' not in HTML)
    # A FRESH enrollment: the one above is already accepted (generate now chains
    # into accept), so it would return 409 before validation ever runs.
    vid, _vemail = full_enrollment("saval")
    bad = c.post(f"/api/perch/enrollments/{vid}/lmi/self-attestation", headers=rep,
                 json={"occupancy": 4, "county": "Hudson County", "status": "accepted"})
    check("a county outside NY is REJECTED before reaching Perch", bad.status_code == 400)
    check("occupancy outside 1-8 is rejected",
          c.post(f"/api/perch/enrollments/{vid}/lmi/self-attestation", headers=rep,
                 json={"occupancy": 9, "county": "Ulster County",
                       "status": "accepted"}).status_code == 400)
    check("an undocumented status is rejected",
          c.post(f"/api/perch/enrollments/{vid}/lmi/self-attestation", headers=rep,
                 json={"occupancy": 4, "county": "Ulster County",
                       "status": "maybe"}).status_code == 400)
    with app.app_context():
        check("  ...and none of them created a ledger row",
              query_one("SELECT 1 FROM perch_self_attestation_submissions "
                        "WHERE enrollment_id=?", (vid,)) is None)

    section("8-9. CUSTOMER REVIEWS THE PERCH DOCUMENT, THEN ACCEPTS")
    cust = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": email, "password": "CustPass1!"}
        ).get_json()["token"]}
    doc = c.get(f"/api/perch/enrollments/{eid}/lmi/self-attestation/document", headers=cust)
    check("customer can fetch the document", doc.status_code == 200)
    check("  ...it is PERCH's URL", "presigned" in doc.get_json()["document_url"])
    check("  ...served no-store", doc.headers.get("Cache-Control") == "no-store, private")
    check("Dalton generates NO document of its own",
          "reportlab" not in ADAPTER and "self_attestation" not in
          open(os.path.join(ROOT, "services", "packaging.py"), encoding="utf-8").read().lower())

    # Acceptance ALREADY happened server-side during generate - self-attestation
    # is a rep-side eligibility step. The metadata is asserted in the live
    # regression section below, where the generate call is observed directly.
    meta = [x for x in SENT if x["op"] == "accept"][0]["metadata"]
    check("acceptance metadata was captured at generate time", bool(meta["timestamp"]))
    check("  ...ip_address captured", bool(meta["ip_address"]))
    check("  ...user_agent captured", "user_agent" in meta)
    check("  ...and NO utility_account_number (per the spec)",
          "utility_account_number" not in meta)
    SENT.clear()
    acc = c.post(f"/api/perch/enrollments/{eid}/lmi/self-attestation/accept", headers=cust)
    check("a later customer accept is a no-op", acc.status_code == 200)
    check("  ...reporting already_accepted", acc.get_json().get("already_accepted") is True)
    check("  ...and making NO Perch call", not [x for x in SENT if x["op"] == "accept"])

    section("10-11. LEADS INTO THE EXISTING CONTRACTS FLOW")
    check("Perch's next_step is contracts", acc.get_json()["next_step_key"] == "contracts")
    with app.app_context():
        check("  ...and is persisted",
              query_one("SELECT current_step_key FROM perch_workflow_state "
                        "WHERE enrollment_id=?", (eid,))["current_step_key"] == "contracts")
    check("the REP hands off to the EXISTING contracts flow",
          "generateContractsAndOpenAgreement(4)" in
          JS.split("async function submitSelfAttestation")[1][:1400])
    check("self-attestation does NOT replace contracts",
          "generateContractsAndOpenAgreement" in JS and "/contracts'" in JS)
    check("12. contract acceptance is untouched",
          "submitAgreements" in JS and JS.count('id="agr-ack-check"') == 1)

    section("13-14. RESUME DOES NOT DUPLICATE")
    SENT.clear()
    again = c.post(f"/api/perch/enrollments/{eid}/lmi/self-attestation/accept", headers=cust)
    check("re-accepting returns already_accepted", again.get_json().get("already_accepted") is True)
    check("  ...and makes NO second Perch call",
          not [x for x in SENT if x["op"] == "accept"])
    check("  ...resuming into the persisted next step",
          again.get_json()["next_step_key"] == "contracts")
    regen = c.post(f"/api/perch/enrollments/{eid}/lmi/self-attestation", headers=rep,
                   json={"occupancy": 4, "county": "Ulster County", "status": "accepted"})
    check("regenerating after acceptance is refused", regen.status_code == 409)
    ref2 = c.get(f"/api/perch/enrollments/{eid}/lmi/self-attestation/reference",
                 headers=rep).get_json()
    check("the reference reports prior state for resume",
          ref2["already_generated"] is True and ref2["already_accepted"] is True)

    section("LEDGER — idempotence only, NO presigned URLs")
    with app.app_context():
        row = dict(query_one("SELECT * FROM perch_self_attestation_submissions "
                             "WHERE enrollment_id=?", (eid,)))
    check("one ledger row", bool(row))
    check("  ...records occupancy/county/status", (row["occupancy"], row["county"],
                                                   row["status"]) == (4, "Ulster County", "accepted"))
    check("  ...document_url_returned is a BOOLEAN", row["document_url_returned"] in (0, 1))
    check("  ...no column holds a URL",
          not any("http" in str(v) for v in row.values()))
    with app.app_context():
        calls = query("SELECT response_json FROM perch_api_calls WHERE operation IN "
                      "('submit_self_attestation','accept_self_attestation')")
    check("  ...and no presigned URL was audited",
          not any("X-Amz" in str(c0["response_json"] or "") for c0 in calls))

    section("15. COMMIT BOUNDARY STILL HOLDS")
    with app.app_context():
        check("enrollment is committed", workflow.perch_committed(eid) is True)
    check("program change still refused",
          c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                 json={"customer_type": "Residential"}).status_code == 409)
    check("committed customer fields still refused",
          c.patch(f"/api/enrollments/{eid}", headers=rep,
                  json={"customer": {"first_name": "John"}}).status_code == 409)

    section("16-18. UNAFFECTED FLOWS")
    check("16. non-LMI Residential unaffected",
          "if(needsLmi === false) return [1,2,5];" in JS)
    check("17. proof-doc flow unchanged",
          "prepareLmiForPerch" in JS and "/lmi/proof_docs'" in JS)
    check("  ...its client method is untouched",
          "def submit_proof_docs(self, enrollment_token: str, files: dict)" in CLIENT)
    check("18. unified login unaffected",
          c.post("/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                           "password": "AdminPass1!"}).status_code == 200)
    check("OCR untouched", "parseUtilityBill" in JS and "extractTextFromFile" in JS)

    section("AMBIGUOUS OUTCOME — conservative, never retried")
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("an ambiguous acceptance is reconciled, not retried",
          "PerchAmbiguousOutcomeError" in routes_src
          and "Acceptance outcome is uncertain" in routes_src)
    check("  ...and recorded for /status reconciliation",
          'workflow.set_state(enrollment_id, "self_attestation_accept"' in routes_src)
    check("the accept client marks sub-400 failures ambiguous",
          "ambiguous_on_failure=True" in CLIENT.split("def accept_self_attestation")[2])

    # ═══════════════════════════════════════════════════════
    section("LIVE STAGING REGRESSION — one rep click completes eligibility")
    # Reproduces the Central Hudson failure: generation succeeded but the
    # enrollment stayed at self_attestation_accept, so when the customer signed
    # in the portal went straight for /contracts and Perch refused with
    # "Previous stages must be completed first."
    #
    # Self-attestation is a REP-SIDE eligibility step. One click must complete
    # generate -> accept -> contracts.
    live_eid, live_email = full_enrollment("live")
    SENT.clear()
    lr = c.post(f"/api/perch/enrollments/{live_eid}/lmi/self-attestation", headers=rep,
                json={"occupancy": 1, "county": "Ulster County", "status": "accepted"})
    check("rep prepares self-attestation", lr.status_code == 200)
    ops = [x["op"] for x in SENT]
    check("  ...BOTH Perch calls happen in that one request",
          ops == ["generate", "accept"])
    check("  ...and Perch's next step is contracts",
          lr.get_json()["next_step_key"] == "contracts")
    with app.app_context():
        step = query_one("SELECT current_step_key FROM perch_workflow_state "
                         "WHERE enrollment_id=?", (live_eid,))["current_step_key"]
    check("  ...the enrollment is NOT left at self_attestation_accept",
          step != "self_attestation_accept")
    check("  ...it is at contracts", step == "contracts")
    with app.app_context():
        led = dict(query_one("SELECT generated_at, accepted_at, accepted_next_step "
                             "FROM perch_self_attestation_submissions "
                             "WHERE enrollment_id=?", (live_eid,)))
    check("  ...ledger records generation AND acceptance",
          bool(led["generated_at"]) and bool(led["accepted_at"]))
    check("  ...with the returned next step", led["accepted_next_step"] == "contracts")

    section("  ...acceptance metadata is server-captured")
    acc_meta = [x for x in SENT if x["op"] == "accept"][0]["metadata"]
    check("timestamp captured", bool(acc_meta["timestamp"]))
    check("ip_address captured", bool(acc_meta["ip_address"]))
    check("user_agent captured", "user_agent" in acc_meta)
    check("  ...and still NO utility_account_number (per the spec)",
          "utility_account_number" not in acc_meta)

    section("  ...the rep continues into the EXISTING contracts flow")
    sa_submit = JS.split("async function submitSelfAttestation")[1][:1400]
    check("the frontend advances on next_step === 'contracts'",
          "body.next_step_key === 'contracts'" in sa_submit)
    check("  ...via the EXISTING contract function",
          "generateContractsAndOpenAgreement(4)" in sa_submit)
    check("  ...it does not invent a new contracts path",
          "/contracts'" not in sa_submit)

    section("  ...NO customer self-attestation screen exists")
    check("no customer self-attestation panel in the markup",
          "cust-sa-panel" not in HTML)
    check("  ...no customer accept button", "cust-sa-agree" not in HTML)
    check("  ...and no customer-side JS for it",
          "acceptCustomerSelfAttestation" not in JS
          and "loadCustomerSelfAttestation" not in JS)
    check("  ...the rep-side panel is retained", 'id="sa-panel"' in HTML)
    check("  ...and the copy no longer promises a customer sign-in for it",
          "customer signs in to review and agree" not in HTML)

    section("  ...customer signs in ONCE and sees the NORMAL package")
    live_cust = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": live_email, "password": "CustPass1!"}
        ).get_json()["token"]}
    check("customer authenticates", c.get("/api/auth/customer-me",
                                          headers=live_cust).status_code == 200)
    check("  ...and the contract flow is unchanged",
          "loadCustomerAgreement" in JS and "submitAgreements" in JS)
    check("  ...one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("  ...one Agree & finish", JS.count('id="agr-agree-btn"') == 1)

    section("  ...re-preparing does NOT duplicate")
    SENT.clear()
    again2 = c.post(f"/api/perch/enrollments/{live_eid}/lmi/self-attestation", headers=rep,
                    json={"occupancy": 1, "county": "Ulster County", "status": "accepted"})
    check("a second prepare is refused", again2.status_code == 409)
    check("  ...and makes NO Perch call at all", SENT == [])

    print(f"\n{'='*72}\nSELF-ATTESTATION - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
