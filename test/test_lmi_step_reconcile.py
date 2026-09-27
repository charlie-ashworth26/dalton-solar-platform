"""
Proof documents reposted after Perch already advanced.

THE BUG
    /lmi/proof_docs returned Perch's documented
        422 unprocessable_entity
        "This step has already been completed. Use the /status endpoint to
         check the current status of the enrollment."
    and UBS showed that raw endpoint message to the rep. The local workflow
    stayed on proof_docs even though Perch had moved on.

THE FIX
    That ONE condition reconciles through the existing GET /status mechanism,
    adopts Perch's authoritative next step, and routes the rep there. The POST
    is never retried. Every other 422 stays an ordinary correctable failure.

    python test/test_lmi_step_reconcile.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-lmi-reconcile-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query, query_one
init_db(reset=True)

from app import app
from routes import document_routes
document_routes.UPLOAD_DIR = os.path.join(_temp.name, "uploads")
document_routes.DATA_ROOT = _temp.name
os.makedirs(document_routes.UPLOAD_DIR, exist_ok=True)
import seed
from services.perch import adapter, workflow
from services.perch.errors import (
    PerchValidationError, PerchAmbiguousOutcomeError, PerchUnavailableError)
from services.perch.client import is_step_already_completed_error

BASE = "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments"
COMPLETED_BODY = {
    "error": "unprocessable_entity",
    "message": ("This step has already been completed. Use the /status endpoint "
                "to check the current status of the enrollment."),
}


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def login(client):
    r = client.post("/api/auth/login", json={"email": "charlie@daltonsolar.com",
                                             "password": "RepPass1!"})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def upload(client, headers, eid, path, category):
    with open(path, "rb") as fh:
        r = client.post(f"/api/enrollments/{eid}/documents", headers=headers,
                        data={"category": category,
                              "file": (io.BytesIO(fh.read()), Path(path).name)},
                        content_type="multipart/form-data")
    assert r.status_code == 201, r.data
    return r.get_json()["document_id"]


def completed_error():
    e = PerchValidationError(
        "Perch rejected the proof documents (422): unprocessable_entity: "
        + COMPLETED_BODY["message"])
    e.status_code = 422
    e.body_json = COMPLETED_BODY
    return e


def ordinary_422(message):
    e = PerchValidationError(f"Perch rejected the proof documents (422): {message}")
    e.status_code = 422
    e.body_json = {"error": "unprocessable_entity", "message": message}
    return e


def main():
    seed.seed()
    seed.seed_legacy_projects()
    c = app.test_client()
    h = login(c)

    # ── An enrollment genuinely parked on proof_docs ──────────────────────
    def enrollment_at_proof_docs(email_tag):
        eid = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
        email = f"lmi.reconcile.{email_tag}.{eid}@example.com"
        r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=h,
                   json={"email": email, "zip_code": "13348",
                         "utility_name": "national-grid-ny"})
        assert r.status_code == 200, r.data
        bill = upload(c, h, eid, ROOT / "test" / "sample_utility_bill.pdf", "utility_bill")
        c.patch(f"/api/enrollments/{eid}", headers=h, json={
            "customer": {"first_name": "Dalton", "last_name": "Testcustomer",
                         "email": email, "phone": "5185550142"},
            "service_address": {"street": "123 Main St", "unit": "", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "billing_address": {"same_as_service": True, "street": "123 Main St",
                                "unit": "", "city": "Albany", "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": "national-grid-ny",
                                "account_number": "1234567890",
                                "secondary_account_identifier": None}})
        real_enroll = adapter.create_enrollment
        adapter.create_enrollment = lambda *a, **k: {
            "customer_type": "LMI", "next_step_url": f"{BASE}/lmi/proof_docs"}
        r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                   json={"document_id": bill})
        adapter.create_enrollment = real_enroll
        assert r.get_json()["next_step_key"] == "proof_docs", r.data
        proof = upload(c, h, eid, ROOT / "test" / "test_snap_proof_letter.pdf",
                       "lmi_document")
        return eid, proof

    def post_proof(eid, proof_id):
        return c.post(f"/api/perch/enrollments/{eid}/lmi/proof_docs", headers=h,
                      json={"document_id": proof_id, "source_type": "proof_doc_snap",
                            "name_on_document": "Dalton Testcustomer",
                            "relationship": "self", "document_type": "letter"})

    def step_key(eid):
        row = query_one("SELECT current_step_key FROM perch_workflow_state "
                        "WHERE enrollment_id=?", (eid,))
        return row["current_step_key"] if row else None

    real_proof, real_status = adapter.submit_proof_docs, adapter.get_status

    # ═══════════════════════════════════════════════════════
    section("5. ALREADY-COMPLETED PROOF DOCS RECONCILE INSTEAD OF REPOSTING")
    eid, proof = enrollment_at_proof_docs("completed")
    check("the enrollment starts on proof_docs", step_key(eid) == "proof_docs")

    calls = {"proof": 0, "status": 0}

    def proof_already_done(*a, **k):
        calls["proof"] += 1
        raise completed_error()

    def status_says_contracts(*a, **k):
        calls["status"] += 1
        return {"completed_steps": ["capacity", "enroll", "lmi/proof_docs"],
                "remaining_steps": ["contracts"], "completed": False,
                "next_step": f"{BASE}/contracts"}

    adapter.submit_proof_docs = proof_already_done
    adapter.get_status = status_says_contracts
    r = post_proof(eid, proof)
    adapter.submit_proof_docs, adapter.get_status = real_proof, real_status

    body = r.get_json()
    check("the request succeeds rather than surfacing the rejection",
          r.status_code == 200)
    check("  ...it is reported as reconciled", body.get("reconciled") is True)
    check("the proof POST was attempted exactly ONCE - never retried",
          calls["proof"] == 1)
    check("  ...and /status was consulted", calls["status"] == 1)
    check("Perch's authoritative next step is returned",
          body.get("next_step_key") == "contracts")
    check("  ...and PERSISTED to the workflow", step_key(eid) == "contracts")
    check("  ...marked as a recognized step",
          query_one("SELECT next_step_recognized FROM perch_workflow_state "
                    "WHERE enrollment_id=?", (eid,))["next_step_recognized"] == 1)

    section("  ...and the raw Perch message never reaches the rep")
    blob = str(body).lower()
    for token in ("already been completed", "unprocessable_entity",
                  "/status endpoint", "422"):
        check(f"response does not contain {token!r}", token.lower() not in blob)
    check("no error field at all", "error" not in body)

    section("  ...the enrollment is the SAME one throughout (7)")
    check("no second local enrollment was created",
          query_one("SELECT COUNT(*) n FROM enrollments WHERE id > ?", (eid,))["n"] == 0)
    check("  ...exactly one workflow row for it",
          query_one("SELECT COUNT(*) n FROM perch_workflow_state WHERE enrollment_id=?",
                    (eid,))["n"] == 1)
    check("  ...the proof document set is untouched",
          query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=? "
                    "AND doc_category='lmi_document'", (eid,))["n"] == 1)
    check("  ...and the utility bill is still there",
          query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=? "
                    "AND doc_category='utility_bill'", (eid,))["n"] == 1)

    section("  ...it adopts whatever step Perch names, not a guessed one")
    eid2, proof2 = enrollment_at_proof_docs("selfattest")
    adapter.submit_proof_docs = lambda *a, **k: (_ for _ in ()).throw(completed_error())
    adapter.get_status = lambda *a, **k: {
        "completed_steps": ["enroll"], "remaining_steps": ["lmi/self_attestation"],
        "completed": False, "next_step": f"{BASE}/lmi/self_attestation"}
    r2 = post_proof(eid2, proof2)
    adapter.submit_proof_docs, adapter.get_status = real_proof, real_status
    check("a self_attestation next step is adopted verbatim",
          r2.get_json().get("next_step_key") == "self_attestation")
    check("  ...and persisted", step_key(eid2) == "self_attestation")

    # ═══════════════════════════════════════════════════════
    section("6. AN ORDINARY 422 IS NOT MISTAKEN FOR 'ALREADY COMPLETED'")
    for message in ("source_type is not a supported proof source",
                    "name_on_document is required",
                    "The document could not be read"):
        eid3, proof3 = enrollment_at_proof_docs("ordinary")
        status_calls = {"n": 0}

        def counting_status(*a, **k):
            status_calls["n"] += 1
            return {"next_step": f"{BASE}/contracts"}

        adapter.submit_proof_docs = lambda *a, _m=message, **k: (
            _ for _ in ()).throw(ordinary_422(_m))
        adapter.get_status = counting_status
        r3 = post_proof(eid3, proof3)
        adapter.submit_proof_docs, adapter.get_status = real_proof, real_status

        check(f"{message[:34]!r} stays an error", r3.status_code >= 400)
        check("  ...it is NOT reconciled", r3.get_json().get("reconciled") is not True)
        check("  ...no /status call was made", status_calls["n"] == 0)
        check("  ...the rep still sees the correctable message",
              message in (r3.get_json().get("error") or ""))
        check("  ...and the enrollment is preserved on proof_docs",
              step_key(eid3) == "proof_docs")
        check("  ...with its documents intact",
              query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=?",
                        (eid3,))["n"] >= 2)

    section("  ...and the predicate itself is narrow")
    amb = PerchAmbiguousOutcomeError(COMPLETED_BODY["message"])
    amb.status_code, amb.body_json = 422, COMPLETED_BODY
    check("an AMBIGUOUS outcome with identical text is not 'already completed'",
          not is_step_already_completed_error(amb))
    wrong_status = PerchValidationError(COMPLETED_BODY["message"])
    wrong_status.status_code, wrong_status.body_json = 400, COMPLETED_BODY
    check("  ...nor the same text at a different status",
          not is_step_already_completed_error(wrong_status))
    unavailable = PerchUnavailableError("Perch returned 500.")
    unavailable.status_code, unavailable.body_json = 500, None
    check("  ...nor a 5xx", not is_step_already_completed_error(unavailable))
    check("the documented condition IS matched", is_step_already_completed_error(
        completed_error()))
    check("  ...even with no parsed body, via the message text", (
        lambda e: is_step_already_completed_error(e))(
        (lambda: (lambda x: (setattr(x, "status_code", 422),
                             setattr(x, "body_json", None), x)[-1])(
            PerchValidationError("Perch rejected the proof documents (422): "
                                 "This step has already been completed.")))()))

    # ═══════════════════════════════════════════════════════
    section("RECONCILIATION FAILURE NEVER GUESSES")
    eid4, proof4 = enrollment_at_proof_docs("statusdown")
    adapter.submit_proof_docs = lambda *a, **k: (_ for _ in ()).throw(completed_error())
    adapter.get_status = lambda *a, **k: (_ for _ in ()).throw(
        PerchUnavailableError("Perch /status returned 503."))
    r4 = post_proof(eid4, proof4)
    adapter.submit_proof_docs, adapter.get_status = real_proof, real_status

    b4 = r4.get_json()
    check("an unreachable /status does not fake success", b4.get("reconciled") is False)
    check("  ...the rep gets a controlled message",
          "could not confirm" in (b4.get("error") or "").lower())
    check("  ...with no raw endpoint detail",
          "503" not in str(b4) and "unprocessable_entity" not in str(b4))
    check("  ...the enrollment is PRESERVED on its real step",
          step_key(eid4) == "proof_docs")
    check("  ...and still exists for review",
          query_one("SELECT discarded_at FROM enrollments WHERE id=?",
                    (eid4,))["discarded_at"] is None)

    section("  ...an unrecognized next step is not adopted either")
    eid5, proof5 = enrollment_at_proof_docs("unknownstep")
    adapter.submit_proof_docs = lambda *a, **k: (_ for _ in ()).throw(completed_error())
    adapter.get_status = lambda *a, **k: {"next_step": f"{BASE}/some/future/step"}
    r5 = post_proof(eid5, proof5)
    adapter.submit_proof_docs, adapter.get_status = real_proof, real_status
    check("an unknown next step is refused rather than guessed",
          r5.get_json().get("reconciled") is False)
    check("  ...and the workflow row is left on its real step",
          step_key(eid5) == "proof_docs")

    # ═══════════════════════════════════════════════════════
    section("9. THE PUBLISHED PROOF SOURCE_TYPE TAXONOMY IS UNCHANGED")
    eid6, proof6 = enrollment_at_proof_docs("taxonomy")
    r6 = c.post(f"/api/perch/enrollments/{eid6}/lmi/proof_docs", headers=h,
                json={"document_id": proof6, "source_type": "snap_letter",
                      "name_on_document": "Dalton Testcustomer",
                      "relationship": "self", "document_type": "letter"})
    check("a legacy guessed label is still rejected", r6.status_code == 400)
    check("  ...with the taxonomy message",
          "supported by Perch" in (r6.get_json().get("error") or ""))
    check("published types still come from the DB table",
          query_one("SELECT COUNT(*) n FROM perch_proof_doc_types "
                    "WHERE category='proof_doc' AND is_active=1")["n"] > 0)
    captured = {}

    def capture_proof(enrollment_id, account_number, documents, user_id=None):
        captured["docs"] = documents
        return {"next_step_url": f"{BASE}/contracts"}

    adapter.submit_proof_docs = capture_proof
    r7 = post_proof(eid6, proof6)
    adapter.submit_proof_docs = real_proof
    check("a published source_type still submits normally", r7.status_code == 200)
    check("  ...and reaches Perch verbatim",
          captured["docs"][0]["source_type"] == "proof_doc_snap")
    check("  ...advancing to the real next step", step_key(eid6) == "contracts")

    print(f"\n{'='*72}\nLMI STEP RECONCILE - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
