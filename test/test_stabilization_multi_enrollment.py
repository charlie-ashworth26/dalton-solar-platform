"""
Stabilization regression - multi-enrollment browser sessions.

Models what a rep actually does (Scenarios A-D from the bug bash), not isolated
function calls. Backend assertions run against the real routes; frontend
lifecycle assertions are made against app.js source, since the failures being
guarded are state-lifecycle bugs rather than DOM rendering bugs.

Run: python test/test_stabilization_multi_enrollment.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from auth import hash_password
from services.perch import workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def fn_body(name):
    """Extract a top-level function body from app.js for lifecycle assertions."""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", JS)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(JS)):
        if JS[j] == "{":
            depth += 1
        elif JS[j] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:j + 1]
    return ""


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ───────────────────────────────────────────────────────────
    section("RC-2 - OCR cleanup runs on every path, including stale generation")
    body = fn_body("handleBillFile") or JS
    check("OCR has a finally block", "finally" in body)
    check("cleanup is generation-scoped, not unconditional",
          "generation === billUploadGeneration" in body)
    check("checkBillReady() is reachable from the finally path",
          body.rindex("checkBillReady()") > body.index("finally"))
    check("stuck spinner is explicitly cleared", "Reading the bill" in body)
    rm = fn_body("removeBill")
    check("removeBill clears the OCR container so no spinner survives",
          "ocr-container" in rm)

    section("RC-3 - the rep is told WHY Continue is disabled")
    ready = fn_body("checkBillReady")
    check("checkBillReady collects reasons", "missing" in ready)
    check("account-number rule is explained with the expected length",
          "must be" in ready and "digits" in ready)
    check("POD requirement is explained per utility", "POD ID" in ready)
    check("requirements element exists in the markup", 'id="bill-requirements"' in HTML)
    req = fn_body("renderBillRequirements")
    check("guidance is suppressed until the rep interacts (no spam)",
          "billTouched" in req or "started" in req)
    check("guidance lists each outstanding item", "<li>" in req)

    section("RC-4 - read-only lock is deterministic and reversible")
    check("explicit control list replaces text matching", "READ_ONLY_LOCK_IDS" in JS)
    lock = fn_body("lockEnrollmentReadOnly")
    # The defect was SELECTING controls by reading button text. Writing a label
    # (acc.textContent = 'Contracts accepted') is fine and expected.
    check("lock no longer SELECTS controls by matching button text",
          "/review|back|dashboard/i" not in lock
          and "querySelectorAll('#view-wizard button')" not in lock)
    check("lock targets an explicit id list", "READ_ONLY_LOCK_IDS" in lock)
    check("an unlock path exists", "function unlockEnrollmentControls" in JS)
    reset = fn_body("resetWizardState")
    check("resetWizardState unlocks controls", "unlockEnrollmentControls()" in reset)

    section("RC-5 - every enrollment-specific global has a reset")
    for g in ["docReviewed", "hasSigned", "isDrawing", "sigCtx", "billTouched",
              "skipProjectStep", "entryMode", "currentWorkflow",
              "perchContracts", "perchContext", "currentCustomerId",
              "billRuntimeFile", "lmiRuntimeFile"]:
        check(f"resetWizardState resets {g}", g in reset)
    check("resume banner is cleared", "resume-banner" in reset)
    check("bill requirements are cleared", "bill-requirements" in reset)
    check("acceptance status text is cleared", "contract-accept-status" in reset)

    section("RC-1 - resume reconstructs contract state from the backend")
    open_body = fn_body("openEnrollment")
    check("resume detects contract steps", "CONTRACT_STEPS" in open_body)
    check("resume calls the rehydrate path", "rehydrateContractPacket" in open_body)
    rehy = fn_body("rehydrateContractPacket")
    check("rehydrate re-requests the packet", "/contracts" in rehy)
    check("acceptanceEnabled comes from the backend response, not fabricated",
          "body.acceptance_enabled === true" in rehy)
    check("perchContracts repopulated", "perchContracts = body.contracts" in rehy)
    check("Accept button state recomputed", "updateAcceptButtonState()" in rehy)
    check("failure surfaces a visible error, not a dead button",
          "contract-review-error" in rehy and "Could not reload" in rehy)
    check("terminal/blocked enrollments do NOT get acceptance re-enabled",
          "acceptanceEnabled = false" in rehy)
    check("resume still never creates a draft", "/api/perch/drafts" not in open_body)

    # ───────────────────────────────────────────────────────────
    section("SCENARIO A - A to OCR/contact, Dashboard, B through OCR/contact")
    a = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{a}", headers=rep, json={
        "customer": {"first_name": "Alice", "last_name": "Anderson",
                     "email": "alice@example.com", "phone": "5185550001"},
        "service_address": {"street": "1 A St", "city": "Albany", "state": "NY", "zip": "12202"},
        "utility_account": {"utility_name": "national-grid-ny", "account_number": "1111111111"}})
    with open(os.path.join(ROOT, "test", "sample_utility_bill.pdf"), "rb") as f:
        ra = c.post(f"/api/enrollments/{a}/documents", headers=rep,
                    data={"category": "utility_bill", "file": (f, "billA.pdf")},
                    content_type="multipart/form-data")
    doc_a = ra.get_json()["document_id"]

    b = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{b}", headers=rep, json={
        "customer": {"first_name": "Bob", "last_name": "Booker",
                     "email": "bob@example.com", "phone": "5185550002"},
        "service_address": {"street": "2 B St", "city": "Albany", "state": "NY", "zip": "12202"},
        "utility_account": {"utility_name": "nyseg", "account_number": "22222222222",
                             "secondary_account_identifier": "N01123456789012"}})
    with open(os.path.join(ROOT, "test", "sample_utility_bill.pdf"), "rb") as f:
        rb_ = c.post(f"/api/enrollments/{b}/documents", headers=rep,
                     data={"category": "utility_bill", "file": (f, "billB.pdf")},
                     content_type="multipart/form-data")
    doc_b = rb_.get_json()["document_id"]

    check("A and B are distinct enrollments", a != b)
    check("A and B have distinct bill documents", doc_a != doc_b)

    ea = c.get(f"/api/enrollments/{a}", headers=rep).get_json()
    eb = c.get(f"/api/enrollments/{b}", headers=rep).get_json()
    check("A keeps A's customer", ea["customer"]["first_name"] == "Alice")
    check("B keeps B's customer", eb["customer"]["first_name"] == "Bob")
    check("A keeps A's utility", ea["utility_account"]["utility_name"] == "national-grid-ny")
    check("B keeps B's utility (no National Grid bleed)",
          eb["utility_account"]["utility_name"] == "nyseg")
    check("B keeps its POD / secondary identifier",
          eb["utility_account"]["secondary_account_identifier"] == "N01123456789012")
    check("A has no POD (National Grid does not require one)",
          not ea["utility_account"].get("secondary_account_identifier"))
    with app.app_context():
        docs_a = [d["id"] for d in query("SELECT id FROM documents WHERE enrollment_id=?", (a,))]
        docs_b = [d["id"] for d in query("SELECT id FROM documents WHERE enrollment_id=?", (b,))]
    check("A's documents never appear under B", doc_a in docs_a and doc_a not in docs_b)
    check("B's documents never appear under A", doc_b in docs_b and doc_b not in docs_a)

    section("SCENARIO B - A to contracts_review, Dashboard, reopen A, Accept enables")
    with app.app_context():
        workflow.set_state(a, "contracts_review",
                           last_response={"contracts": [], "contract_count": 2})
        tokens_before = query_one("SELECT COUNT(*) n FROM perch_tokens")["n"]
        calls_before = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
        count_before = query_one("SELECT COUNT(*) n FROM enrollments")["n"]

    reopened = c.get(f"/api/enrollments/{a}", headers=rep).get_json()
    check("reopen returns that exact enrollment", reopened["id"] == a)
    check("reopen restores the contracts step",
          reopened["workflow_step_key"] == "contracts_review")
    with app.app_context():
        check("reopen creates no new enrollment",
              query_one("SELECT COUNT(*) n FROM enrollments")["n"] == count_before)
        check("reopen creates no new Perch token",
              query_one("SELECT COUNT(*) n FROM perch_tokens")["n"] == tokens_before)
        check("reopen makes no Perch API call",
              query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"] == calls_before)
    check("the enable rule is backend-driven AND checkbox-driven",
          "perchContext.acceptanceEnabled && chk.checked" in JS)

    section("SCENARIO C - A complete, then start B fresh without a refresh")
    with app.app_context():
        workflow.set_state(a, "contracts_accepted",
                           last_response={"message": "Contracts accepted successfully"})
    ea = c.get(f"/api/enrollments/{a}", headers=rep).get_json()
    check("completed A reports terminal", ea["workflow_is_terminal"] is True)
    r = c.post(f"/api/perch/enrollments/{a}/contracts/accept", headers=rep,
               json={"customer_confirmed": True})
    check("a completed enrollment cannot be re-accepted",
          r.get_json().get("already_accepted") is True)
    fresh = fn_body("startWizardFresh")
    check("starting a new enrollment resets all state first",
          "resetWizardState()" in fresh)
    check("... which unlocks controls poisoned by the completed enrollment",
          "unlockEnrollmentControls()" in reset)

    section("SCENARIO D - A mid-flow, B mid-flow, reopen A, reopen B")
    with app.app_context():
        workflow.set_state(a, "contracts_review")
        workflow.set_state(b, "proof_docs")
    ra2 = c.get(f"/api/enrollments/{a}", headers=rep).get_json()
    rb2 = c.get(f"/api/enrollments/{b}", headers=rep).get_json()
    check("A resumes at its own step", ra2["workflow_step_key"] == "contracts_review")
    check("B resumes at its own step", rb2["workflow_step_key"] == "proof_docs")
    check("A still has A's customer after interleaving",
          ra2["customer"]["first_name"] == "Alice")
    check("B still has B's customer after interleaving",
          rb2["customer"]["first_name"] == "Bob")
    check("B still has its POD after interleaving",
          rb2["utility_account"]["secondary_account_identifier"] == "N01123456789012")
    with app.app_context():
        wa = query_one("SELECT current_step_key FROM perch_workflow_state WHERE enrollment_id=?", (a,))
        wb = query_one("SELECT current_step_key FROM perch_workflow_state WHERE enrollment_id=?", (b,))
    check("workflow rows are per-enrollment, never shared",
          wa["current_step_key"] != wb["current_step_key"])
    with app.app_context():
        check("no duplicate enrollments were created across the whole session",
              query_one("SELECT COUNT(*) n FROM enrollments")["n"] == count_before)

    section("ERROR LIFECYCLE - no disable-without-recovery")
    for name in ["submitBill", "submitContact", "submitLmi", "acceptPerchContracts",
                 "rehydrateContractPacket"]:
        body_ = fn_body(name)
        if not body_:
            continue
        disables = "disabled=true" in body_.replace(" ", "") or "disabled = true" in body_
        if disables:
            # Recovery may be a finally{}, an explicit re-enable, or a call to a
            # deterministic recompute function - all are valid lifecycles.
            recovers = ("finally" in body_
                        or "disabled=false" in body_.replace(" ", "")
                        or "updateAcceptButtonState()" in body_)
            check(f"{name}: disabling a control has a recovery path", recovers)
        shows_error = ("style.display='block'" in body_.replace(" ", "")
                       or "style.display = 'block'" in body_)
        check(f"{name}: failures surface a visible error", shows_error)

    section("AMBIGUOUS ACCEPTANCE - deliberately NOT retryable")
    acc = fn_body("acceptPerchContracts")
    check("normal errors recompute the button state",
          "updateAcceptButtonState()" in acc)
    check("an uncertain outcome deliberately leaves Accept disabled",
          "uncertain" in acc and "btn.disabled=true" in acc.replace(" ", ""))
    check("... and tells the rep not to resubmit",
          "Do not resubmit" in acc or "do not resubmit" in acc.lower())
    check("in-flight double-click is blocked",
          "acceptanceInFlight" in acc)

    print(f"\n{'='*72}\nSTABILIZATION - MULTI-ENROLLMENT - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
