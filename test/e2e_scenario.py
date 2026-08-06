"""
Full end-to-end test scenario, run against the real Flask app + real SQLite DB
using Flask's test client (no separate server process needed).

Covers exactly the flow requested:
  rep creates enrollment -> utility bill uploaded -> bill data extracted and
  reviewed -> LMI path completed -> documents generated -> customer signs ->
  enrollment enters QA -> QA approves -> packet generated -> developer
  receives it -> developer assigns a project -> final status stored.

Run from the backend/ directory: python3 test/e2e_scenario.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from db import init_db

BILL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_utility_bill.pdf")
LMI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_lmi_doc.pdf")


def step(n, title):
    print(f"\n{'='*70}\nSTEP {n}: {title}\n{'='*70}")


def check(label, condition):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}")
    if not condition:
        raise AssertionError(f"Scenario failed at: {label}")


def main():
    init_db(reset=True)
    import seed
    seed.seed()
    # Perch refactor: seed() no longer creates projects (Perch owns products now).
    # This test exercises the PRE-Perch agreement/QA/submission path, which still
    # reads the legacy projects table, so it seeds that data explicitly. Removed
    # once those routes migrate to perch_products (Milestones 3-4).
    seed.seed_legacy_projects()

    client = app.test_client()

    step(1, "Rep logs in")
    r = client.post("/api/auth/login", json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"})
    check("login succeeds (200)", r.status_code == 200)
    rep_token = r.get_json()["token"]
    rep_headers = {"Authorization": f"Bearer {rep_token}"}
    print(f"  rep token acquired, role={r.get_json()['user']['role']}")

    step(2, "Rep fetches project list, picks a project that requires LMI")
    r = client.get("/api/projects")
    projects = r.get_json()
    project = next(p for p in projects if p["lmi_required"] == 1)
    check("found an LMI-required project", project is not None)
    print(f"  using project: {project['name']} (savings {project['savings_pct']}%)")

    step(3, "Rep creates a new enrollment")
    r = client.post("/api/enrollments", json={"project_id": project["id"]}, headers=rep_headers)
    check("enrollment created (201)", r.status_code == 201)
    enrollment = r.get_json()
    enrollment_id = enrollment["id"]
    print(f"  enrollment_code = {enrollment['enrollment_code']}, status = {enrollment['status']}")

    step(4, "Rep enters customer, address, and utility account info")
    r = client.patch(f"/api/enrollments/{enrollment_id}", headers=rep_headers, json={
        "customer": {"first_name": "Jordan", "last_name": "Alvarez", "email": "jordan.alvarez@example.com",
                     "phone": "(315) 555-0199", "password": "CustomerPass1!"},
        "service_address": {"street": "123 Main St", "unit": "", "city": "Albany", "state": "NY", "zip": "12207"},
        "utility_account": {"utility_name": "National Grid", "account_number": "1234567890"},
    })
    check("enrollment updated (200)", r.status_code == 200)

    step(5, "Rep uploads the utility bill — real extraction runs")
    with open(BILL_PATH, "rb") as f:
        r = client.post(f"/api/enrollments/{enrollment_id}/documents", headers=rep_headers,
                         data={"category": "utility_bill", "file": (f, "national_grid_bill.pdf")},
                         content_type="multipart/form-data")
    check("bill uploaded and processed (201)", r.status_code == 201)
    extracted = r.get_json()["extracted"]
    print(f"  extracted fields: {json.dumps(extracted, indent=2)}")
    check("account_holder was extracted", "account_holder" in extracted)
    check("account_number was extracted", "account_number" in extracted)

    r = client.post(f"/api/enrollments/{enrollment_id}/status", headers=rep_headers,
                     json={"new_status": "Utility Bill Uploaded", "reason": "Bill file received"})
    check("status -> Utility Bill Uploaded", r.status_code == 200)
    r = client.post(f"/api/enrollments/{enrollment_id}/status", headers=rep_headers,
                     json={"new_status": "Utility Validation", "reason": "Bill extracted and reviewed"})
    check("status -> Utility Validation", r.status_code == 200)

    step(6, "LMI: customer self-attests household income (below the 80% AMI threshold)")
    r = client.post(f"/api/enrollments/{enrollment_id}/lmi", headers=rep_headers, json={
        "path": "self_attestation", "household_size": 3, "attestation_response": "below",
    })
    check("LMI qualification recorded (201)", r.status_code == 201)
    lmi = r.get_json()
    print(f"  household size 3 -> AMI threshold ${lmi['income_threshold']:,.0f}, response: {lmi['attestation_response']}")
    check("correct AMI threshold looked up", lmi["income_threshold"] == 79350)

    r = client.post(f"/api/enrollments/{enrollment_id}/status", headers=rep_headers,
                     json={"new_status": "LMI Review", "reason": "Project requires LMI qualification"})
    check("status -> LMI Review", r.status_code == 200)

    step(7, "Rep generates the document packet (dynamic — includes income survey because project requires LMI)")
    r = client.post(f"/api/enrollments/{enrollment_id}/agreements/generate", headers=rep_headers)
    check("agreements generated (201)", r.status_code == 201)
    docs_generated = r.get_json()["documents_generated"]
    print(f"  documents generated: {docs_generated}")
    check("income_survey included because project.lmi_required", "income_survey" in docs_generated)
    check("6 documents in the dynamic packet", len(docs_generated) == 6)

    step(8, "Rep creates a signing session and sends it to the customer")
    r = client.post(f"/api/enrollments/{enrollment_id}/signing-session", headers=rep_headers)
    check("signing session created (201)", r.status_code == 201)
    token = r.get_json()["token"]
    print(f"  signing session token issued, expires {r.get_json()['expires_at']}")

    step(9, "Customer opens the signing session (no login — session token)")
    r = client.get(f"/api/signing-sessions/{token}")
    check("session fetched (200)", r.status_code == 200)
    session_data = r.get_json()
    required_fields = session_data["required_fields"]
    print(f"  required fields: {[f['field_key'] for f in required_fields]}")
    check("6 required signature/initial fields", len(required_fields) == 6)

    step(10, "Customer completes every required signature/initial field")
    for f in required_fields:
        method = "drawn" if f["field_type"] == "signature" else "typed"
        payload = {"method": method, "signer_name": "Jordan Alvarez", "signer_email": "jordan.alvarez@example.com"}
        if method == "typed":
            payload["value_text"] = "JA"
        else:
            # 1x1 transparent PNG, base64-encoded — stands in for a drawn signature stroke
            payload["value_image_base64"] = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        r = client.post(f"/api/signing-sessions/{token}/fields/{f['field_key']}", json=payload)
        check(f"field '{f['field_key']}' signed", r.status_code == 201)

    step(11, "Customer submits the completed packet")
    r = client.post(f"/api/signing-sessions/{token}/complete")
    check("session completed (200)", r.status_code == 200)
    check("enrollment status -> Signed", r.get_json()["status"] == "Signed")
    print(f"  signature certificate document id: {r.get_json()['signature_certificate_document_id']}")

    step(12, "Enrollment enters internal QA")
    r = client.post(f"/api/enrollments/{enrollment_id}/status", headers=rep_headers,
                     json={"new_status": "Internal Review", "reason": "Signed, routing to QA"})
    check("status -> Internal Review", r.status_code == 200)

    r_qa_login = client.post("/api/auth/login", json={"email": "qa@daltonsolar.com", "password": "QaPass1!"})
    qa_headers = {"Authorization": f"Bearer {r_qa_login.get_json()['token']}"}

    step(13, "QA reviewer opens the queue and approves")
    r = client.get("/api/qa/queue", headers=qa_headers)
    check("enrollment appears in QA queue", any(e["id"] == enrollment_id for e in r.get_json()))
    r = client.post(f"/api/qa/enrollments/{enrollment_id}/review", headers=qa_headers,
                     json={"decision": "approved", "notes": "All fields present, signatures verified."})
    check("QA approval recorded (201)", r.status_code == 201)
    check("enrollment status -> Verified", r.get_json()["status"] == "Verified")

    step(14, "Rep/QA generates the developer submission package (merged PDF + ZIP)")
    r = client.post(f"/api/enrollments/{enrollment_id}/submit", headers=qa_headers)
    check("submission created (201)", r.status_code == 201)
    submission_id = r.get_json()["submission_id"]
    check("enrollment status -> Developer Review", r.get_json()["status"] == "Developer Review")
    print(f"  submission_id={submission_id}, pdf_doc={r.get_json()['package_pdf_document_id']}, zip_doc={r.get_json()['package_zip_document_id']}")

    r = client.get(f"/api/enrollments/{enrollment_id}/package", headers=qa_headers)
    check("package metadata retrievable", r.status_code == 200)
    pdf_dl_url = r.get_json()["download_pdf_url"]
    zip_dl_url = r.get_json()["download_zip_url"]

    r_pdf = client.get(pdf_dl_url, headers=qa_headers)
    check("merged PDF downloads (200)", r_pdf.status_code == 200 and r_pdf.data[:4] == b"%PDF")
    r_zip = client.get(zip_dl_url, headers=qa_headers)
    check("ZIP package downloads (200)", r_zip.status_code == 200 and r_zip.data[:2] == b"PK")
    print(f"  merged PDF size={len(r_pdf.data)} bytes, ZIP size={len(r_zip.data)} bytes")

    step(15, "Developer logs in and reviews the submission")
    r = client.post("/api/auth/login", json={"email": "developer@perchenergy.com", "password": "DevPass1!"})
    dev_headers = {"Authorization": f"Bearer {r.get_json()['token']}"}
    r = client.get("/api/developer/submissions", headers=dev_headers)
    check("submission visible in developer queue", any(s["submission_id"] == submission_id for s in r.get_json()))
    r = client.get(f"/api/developer/submissions/{submission_id}", headers=dev_headers)
    check("developer can open full record (200)", r.status_code == 200)
    check("developer sees signatures", len(r.get_json()["signatures"]) == 6)

    step(16, "Developer accepts the submission")
    r = client.patch(f"/api/submissions/{submission_id}/status", headers=dev_headers,
                      json={"developer_status": "accepted", "notes": "Looks complete."})
    check("developer accepted (200)", r.status_code == 200)

    step(17, "Developer assigns a project and activates the enrollment")
    r = client.post(f"/api/developer/submissions/{submission_id}/assign-project", headers=dev_headers,
                     json={"project_id": project["id"]})
    check("project assigned, status -> Project Assigned", r.status_code == 200 and r.get_json()["status"] == "Project Assigned")
    r = client.post(f"/api/developer/submissions/{submission_id}/activate", headers=dev_headers)
    check("activated, status -> Active", r.status_code == 200 and r.get_json()["status"] == "Active")

    step(18, "Final verification — full status history")
    r = client.get(f"/api/enrollments/{enrollment_id}", headers=dev_headers)
    final = r.get_json()
    check("final status is Active", final["status"] == "Active")
    print("\n  Full status history:")
    for h in final["status_history"]:
        print(f"    {h['previous_status'] or '(start)':>20} -> {h['new_status']:<20} ({h['reason'] or ''})")

    print(f"\n{'='*70}\nSCENARIO COMPLETE — enrollment {final['enrollment_code']} reached status: {final['status']}\n{'='*70}")


if __name__ == "__main__":
    main()
