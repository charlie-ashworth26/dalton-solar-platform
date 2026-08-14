"""Live staging verifier: /token -> /capacity -> /enroll -> /lmi/proof_docs.

Uses only fictional test data and stops immediately after proof-doc submission.
Never prints API keys, signing keys, or enrollment-token values.

Run from the repo root:
    python scripts/verify_lmi_proof_docs_live.py 12202 national-grid-ny
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ZIP = "12202"
DEFAULT_UTILITY = "national-grid-ny"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_BILL = os.path.join(REPO_ROOT, "test", "sample_utility_bill.pdf")
SAMPLE_PROOF = os.path.join(REPO_ROOT, "test", "test_snap_proof_letter.pdf")
ACCOUNT = "1234567890"


def rule(text):
    print(f"\n{'-'*70}\n{text}\n{'-'*70}")


def dump_error(exc):
    import json
    print(f"exception     : {type(exc).__name__}")
    print(f"message       : {exc}")
    print(f"HTTP status   : {getattr(exc, 'status_code', '(not captured)')}")
    print(f"Content-Type  : {getattr(exc, 'content_type', '(not captured)')}")
    print(f"request id    : {getattr(exc, 'request_id', None) or '(none returned)'}")
    body = getattr(exc, "body_json", None)
    text = getattr(exc, "body_text", None)
    if body is not None:
        print("\n--- COMPLETE JSON BODY ---")
        print(json.dumps(body, indent=2, ensure_ascii=False)[:6000])
    elif text:
        print("\n--- RAW BODY ---")
        print(text[:6000])
    else:
        print("\n(empty response body)")


def main():
    zip_code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP
    utility = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_UTILITY

    from services.perch.config import get_perch_client, get_api_mode
    from services.perch.client import build_enrollment_multipart, build_proof_docs_multipart
    from services.perch.errors import (
        PerchError, PerchNoCapacityError, PerchValidationError,
        PerchTokenExpiredError,
    )

    if get_api_mode() != "live":
        print("PERCH_API_MODE is not 'live'. Refusing to run.")
        return 2
    for path in (SAMPLE_BILL, SAMPLE_PROOF):
        if not os.path.exists(path):
            print(f"Required test file not found: {path}")
            return 2

    client = get_perch_client()
    print("PERCH_API_MODE  = live")
    print(f"enrollment base : {client.enrollment_base_url}")
    print(f"API key set     : {bool(client.api_key)}")
    print(f"signing key set : {bool(client.secret_key)}")

    email = f"dalton.proof.{uuid.uuid4().hex[:12]}@example.com"

    rule("STEP 1 - POST /token")
    tok = client.request_token(email)
    token = tok["enrollment_token"]
    print(f"email           : {email}")
    print(f"token received  : yes (length {len(token)})")
    print(f"response shape  : {tok.get('response_shape')}")

    rule("STEP 2 - POST /capacity")
    try:
        cap = client.check_capacity(token, zip_code, utility)
    except PerchNoCapacityError as exc:
        print(f"NO CAPACITY: {exc}")
        return 1
    details = cap.get("project_details") or {}
    for key in sorted(details):
        print(f"  {key} = {details[key]!r}")
    if not details.get("lmi_capacity_available"):
        print("LMI capacity is not available; refusing to build an LMI enrollment.")
        return 1
    print(f"next_step       : {cap.get('next_step')}")

    rule("STEP 3 - POST /enroll (LMI test customer)")
    enrollment = {
        "email_address": email,
        "first_name": "Dalton",
        "last_name": "Testcustomer",
        "phone_number": "5185550142",
        "customer_type": "LMI",
        "utility_name": utility,
        "zip_code": zip_code,
        "billing_address": {
            "address_1": "123 Main St", "city": "Albany", "state": "NY", "zip": "12207"
        },
        "utility_accounts": [{
            "utility_account_number": ACCOUNT,
            "service_address": {
                "address_1": "123 Main St", "city": "Albany", "state": "NY", "zip": "12207"
            },
            "utility_bills": [SAMPLE_BILL],
        }],
    }
    form, enroll_files = build_enrollment_multipart(enrollment)
    try:
        enrolled = client.create_enrollment(token, form, enroll_files)
    except PerchError as exc:
        rule("FAILED ON /enroll")
        dump_error(exc)
        return 1
    finally:
        for value in enroll_files.values():
            try:
                value[1].close()
            except Exception:
                pass
    print(f"response shape  : {enrolled.get('response_shape')}")
    print(f"next_step       : {enrolled.get('next_step')}")
    if not (enrolled.get("next_step") or "").rstrip("/").endswith("/lmi/proof_docs"):
        print("Perch did not direct this enrollment to /lmi/proof_docs. STOPPING.")
        return 1

    rule("STEP 4 - POST /lmi/proof_docs")
    print("proof metadata:")
    print(f"  utility_account_number : {ACCOUNT}")
    print("  source_type            : proof_doc_snap")
    print("  name_on_document       : Dalton Testcustomer")
    print("  relationship           : self")
    print("  document_type          : letter")
    print(f"  file                    : {os.path.basename(SAMPLE_PROOF)} (application/pdf)")
    print("NOTE: the PDF is clearly marked fictional/test-only and is for staging verification only.")

    proof_files, proof_doc = build_proof_docs_multipart(ACCOUNT, [{
        "source_type": "proof_doc_snap",
        "name_on_document": "Dalton Testcustomer",
        "relationship": "self",
        "document_type": "letter",
        "file_path": SAMPLE_PROOF,
    }])
    try:
        result = client.submit_proof_docs(token, proof_files)
    except (PerchValidationError, PerchTokenExpiredError, PerchError) as exc:
        rule("PROOF-DOC REQUEST REJECTED")
        dump_error(exc)
        return 1
    finally:
        for key, value in proof_files.items():
            if key == "proof_doc":
                continue
            try:
                value[1].close()
            except Exception:
                pass

    rule("SUCCESS - proof documents accepted")
    print(f"raw response keys : {sorted((result.get('raw') or {}).keys())}")
    print(f"response shape    : {result.get('response_shape')}")
    print(f"next_step         : {result.get('next_step')}")
    print("\nSTOPPING HERE. No contracts or later endpoint was called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
