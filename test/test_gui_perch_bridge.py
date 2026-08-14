"""Behavioral regression for the existing Dalton GUI -> Perch bridge.

Run inside the project's existing venv:
    python test/test_gui_perch_bridge.py

This never calls live Perch. It drives the real Flask routes, SQLite schema,
document storage, password hashing, capacity/session reset, and workflow state.
Only the already-live-verified outbound Perch calls after /capacity are replaced
with deterministic fakes so the GUI bridge can be tested without consuming a
staging enrollment or accepting contracts.
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

# Point the database at an isolated workspace BEFORE importing app.py, because
# app.py runs pending migrations when it creates the Flask app.
import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-gui-bridge-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
from db import init_db, query, query_one
init_db(reset=True)

from app import app
from routes import document_routes
import seed
from services.perch import adapter
from services.perch.client import normalize_contracts_response

# Keep uploaded test bytes out of the developer's real uploads/ directory.
document_routes.UPLOAD_DIR = os.path.join(_temp.name, "uploads")
os.makedirs(document_routes.UPLOAD_DIR, exist_ok=True)


def check(label, condition):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


def login(client):
    r = client.post("/api/auth/login", json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def upload(client, headers, enrollment_id, path, category):
    with open(path, "rb") as fh:
        r = client.post(
            f"/api/enrollments/{enrollment_id}/documents",
            headers=headers,
            data={"category": category, "file": (io.BytesIO(fh.read()), Path(path).name)},
            content_type="multipart/form-data",
        )
    assert r.status_code == 201, r.data
    return r.get_json()["document_id"]


def main():
    seed.seed()
    seed.seed_legacy_projects()
    client = app.test_client()
    headers = login(client)

    print("\nGUI -> PERCH BRIDGE - REAL DALTON ROUTES")
    projects = client.get("/api/projects", headers=headers).get_json()
    project = next(p for p in projects if p.get("utility") == "National Grid")
    project_draft = client.post("/api/perch/drafts", headers=headers, json={"project_id": project["id"]})
    check("project-card entry creates the same real Perch-backed Dalton draft", project_draft.status_code == 201)
    project_eid = project_draft.get_json()["enrollment_id"]
    project_wf = client.get(f"/api/perch/enrollments/{project_eid}/workflow", headers=headers).get_json()
    utility_field = next(f for f in project_wf["step"]["fields"] if f["name"] == "utility_name")
    check("project-card utility is mapped to the published Perch slug and locked",
          utility_field.get("readonly") is True and utility_field.get("value") == "national-grid-ny")

    draft_r = client.post("/api/perch/drafts", headers=headers, json={})
    assert draft_r.status_code == 201, draft_r.data
    draft = draft_r.get_json()
    eid = draft["enrollment_id"]
    first_email = f"gui.bridge.before.{eid}@example.com"
    email = f"gui.bridge.corrected.{eid}@example.com"

    # The real mock adapter/token manager covers the session + capacity boundary.
    r = client.post(
        f"/api/perch/enrollments/{eid}/capacity", headers=headers,
        json={"email": first_email, "zip_code": "13348", "utility_name": "national-grid-ny"},
    )
    check("capacity succeeds through real Dalton routes/token manager", r.status_code == 200)
    wf = r.get_json()["workflow"]
    check("capacity Continue hands control back to the existing Dalton wizard",
          wf["step"]["primary_action"] == {"label": "Continue", "operation": "advance", "enabled": True})

    before_checks = query_one("SELECT COUNT(*) AS n FROM perch_capacity_checks WHERE enrollment_id=?", (eid,))["n"]
    r = client.post(f"/api/perch/enrollments/{eid}/restart-service-area", headers=headers, json={})
    check("Change email/ZIP/utility returns to the one service-area screen",
          r.status_code == 200 and r.get_json()["workflow"]["step"]["key"] == "service_area")
    enr = query_one("SELECT perch_token_email FROM enrollments WHERE id=?", (eid,))
    active = query_one("SELECT COUNT(*) AS n FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid,))["n"]
    after_checks = query_one("SELECT COUNT(*) AS n FROM perch_capacity_checks WHERE enrollment_id=?", (eid,))["n"]
    check("email correction invalidates the old Perch token instead of reusing it",
          enr["perch_token_email"] is None and active == 0)
    check("historical capacity stays intact as an audit record", after_checks == before_checks)

    r = client.post(
        f"/api/perch/enrollments/{eid}/capacity", headers=headers,
        json={"email": email, "zip_code": "13348", "utility_name": "national-grid-ny"},
    )
    check("corrected email opens a fresh capacity session", r.status_code == 200)

    bill_id = upload(client, headers, eid, ROOT / "test" / "sample_utility_bill.pdf", "utility_bill")
    check("one exact utility-bill document is persisted",
          query_one("SELECT id FROM documents WHERE id=? AND doc_category='utility_bill'", (bill_id,)) is not None)

    patch = {
        "customer": {"first_name": "Dalton", "last_name": "Testcustomer", "email": email, "phone": None},
        "service_address": {"street": "123 Main St", "unit": "", "city": "Albany", "state": "NY", "zip": "12207"},
        "billing_address": {"same_as_service": True, "street": "123 Main St", "unit": "", "city": "Albany", "state": "NY", "zip": "12207"},
        "utility_account": {"utility_name": "national-grid-ny", "account_number": "1234567890", "secondary_account_identifier": None},
    }
    r = client.patch(f"/api/enrollments/{eid}", headers=headers, json=patch)
    check("bill-confirmed customer/address/account data persists", r.status_code == 200)

    r = client.patch(
        f"/api/enrollments/{eid}", headers=headers,
        json={"customer": {"first_name": "Dalton", "last_name": "Testcustomer", "email": email,
                           "phone": "5185550142", "password": "TestPass1!"}},
    )
    check("existing Contact/password step persists and can advance", r.status_code == 200)
    row = query_one("SELECT * FROM enrollments WHERE id=?", (eid,))
    customer = query_one("SELECT * FROM customers WHERE id=?", (row["customer_id"],))
    account = query_one("SELECT * FROM utility_accounts WHERE id=?", (row["utility_account_id"],))
    check("password is hashed rather than stored as plaintext",
          customer["password_hash"] and customer["password_hash"] != "TestPass1!")
    check("same-as-service billing address is durably copied",
          row["billing_same_as_service"] == 1 and row["billing_zip"] == "12207")
    check("National Grid account persists with no duplicate POD field required",
          account["account_number"] == "1234567890" and account["secondary_account_identifier"] is None)

    captured = {}
    original_enroll = adapter.create_enrollment
    original_proof = adapter.submit_proof_docs
    original_contracts = adapter.generate_contracts
    secret = "https://contracts.example.test/review.pdf?X-Amz-Signature=TOPSECRET"
    contract_body = {
        "contract_urls": [
            {"contract_name": "Perch Phone Number Consent", "url": secret, "expires_at": "2026-08-14T07:00:00Z"},
            {"contract_name": "ESIGN Consent Policy", "url": secret + "2", "expires_at": "2026-08-14T07:00:00Z"},
        ],
        "next_step": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/contracts/accept",
    }

    def fake_enroll(enrollment_id, payload, user_id=None):
        captured["enroll"] = payload
        return {"customer_type": "LMI", "next_step_url": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/lmi/proof_docs"}

    def fake_proof(enrollment_id, account_number, documents, user_id=None):
        captured["proof"] = {"account_number": account_number, "documents": documents}
        return {"next_step_url": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/contracts"}

    def fake_contracts(enrollment_id, user_id=None):
        return normalize_contracts_response(contract_body)

    adapter.create_enrollment = fake_enroll
    adapter.submit_proof_docs = fake_proof
    adapter.generate_contracts = fake_contracts
    try:
        r = client.post(f"/api/perch/enrollments/{eid}/enroll", headers=headers, json={"document_id": bill_id})
        body = r.get_json()
        check("Contact Continue bridge reaches /enroll", r.status_code == 200)
        check("Perch next_step routes the existing GUI to proof docs", body["next_step_key"] == "proof_docs")
        sent = captured["enroll"]
        check("/enroll reuses the single email captured before capacity", sent["email_address"] == email)
        check("/enroll reuses confirmed service address", sent["utility_accounts"][0]["service_address"]["zip"] == "12207")
        check("/enroll reuses separately persisted billing address", sent["billing_address"]["zip"] == "12207")
        check("/enroll reuses the exact stored bill instead of asking for another upload",
              len(sent["utility_accounts"][0]["utility_bills"]) == 1 and Path(sent["utility_accounts"][0]["utility_bills"][0]).exists())

        # Once /enroll advances Perch, backend identity/service-area rewinds are blocked.
        r_restart = client.post(f"/api/perch/enrollments/{eid}/restart-service-area", headers=headers, json={})
        check("service area cannot be silently rewritten after /enroll", r_restart.status_code == 409)

        proof_id = upload(client, headers, eid, ROOT / "test" / "test_snap_proof_letter.pdf", "lmi_document")
        r = client.post(
            f"/api/perch/enrollments/{eid}/lmi/proof_docs", headers=headers,
            json={"document_id": proof_id, "source_type": "proof_doc_snap",
                  "name_on_document": "Dalton Testcustomer", "relationship": "self", "document_type": "letter"},
        )
        check("existing LMI upload bridges to /lmi/proof_docs",
              r.status_code == 200 and r.get_json()["next_step_key"] == "contracts")
        check("proof metadata is attached to the exact stored proof document",
              captured["proof"]["documents"][0]["source_type"] == "proof_doc_snap" and
              captured["proof"]["documents"][0]["relationship"] == "self")

        r = client.post(f"/api/perch/enrollments/{eid}/contracts", headers=headers, json={})
        contract_json = r.get_json()
        check("Agreement bridge returns safe contract metadata", r.status_code == 200 and contract_json["contract_count"] == 2)
        check("contract packet JSON contains no Perch presigned URL",
              "TOPSECRET" not in r.get_data(as_text=True) and all("url" not in c for c in contract_json["contracts"]))
        check("contract acceptance remains disabled",
              contract_json["acceptance_enabled"] is False and contract_json["next_step_key"] == "contracts_accept")

        st = query_one("SELECT last_response_json FROM perch_workflow_state WHERE enrollment_id=?", (eid,))
        calls = query("SELECT request_json,response_json,error_message FROM perch_api_calls WHERE enrollment_id=?", (eid,))
        stored_blob = (st["last_response_json"] or "") + "\n" + "\n".join(str(dict(x)) for x in calls)
        check("presigned contract URL is absent from DB/workflow/API-call audit persistence", "TOPSECRET" not in stored_blob)

        r = client.post(f"/api/perch/enrollments/{eid}/contracts/review", headers=headers, json={"index": 0})
        review_url = r.get_json()["review_url"]
        check("Review click receives only a same-origin short-lived Dalton URL",
              r.status_code == 200 and review_url.startswith("/api/perch/contract-reviews/") and "TOPSECRET" not in review_url)
        r2 = client.get(review_url, follow_redirects=False)
        check("one-time Dalton URL redirects to the transient Perch URL only on explicit Review",
              r2.status_code == 302 and "TOPSECRET" in r2.headers.get("Location", ""))
        r3 = client.get(review_url, follow_redirects=False)
        check("contract review capability is one-time", r3.status_code == 410)

        rules = {rule.rule for rule in app.url_map.iter_rules()}
        check("there is no executable Dalton /contracts/accept route",
              not any("contracts/accept" in rule for rule in rules))
    finally:
        adapter.create_enrollment = original_enroll
        adapter.submit_proof_docs = original_proof
        adapter.generate_contracts = original_contracts

    print("\nGUI -> PERCH BRIDGE - ALL BACKEND BEHAVIOR CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close_db()
        _temp.cleanup()
