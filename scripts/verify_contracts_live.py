"""
Live verification of POST /contracts through the normal PerchClient.
 
Runs the already-live-verified flow and adds one step:
 
    /token -> /capacity -> /enroll -> /lmi/proof_docs -> /contracts
 
and then STOPS. It does NOT call /contracts/accept, does not simulate customer
acceptance, and does not complete the enrollment.
 
PRESIGNED URL SAFETY
--------------------
Perch returns presigned S3 URLs valid for 1 hour. The spec says:
    "Do not log URLs in application logs"
    "Do not store or cache these URLs in your own systems beyond their TTL"
This script therefore prints contract NAMES, EXPIRY timestamps, and a
url present: yes/no structural check - never a URL. Nothing is persisted.
 
Also never printed: API key, signing key, enrollment token.
 
    python scripts/verify_contracts_live.py
    python scripts/verify_contracts_live.py 12202 national-grid-ny
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
    from services.perch.client import (
        build_enrollment_multipart,
        build_proof_docs_multipart,
        # Returns URL-free copies of each contract. Used instead of touching
        # normalized["raw"], so a presigned URL can never reach stdout.
        contracts_safe,
    )
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
 
    rule("STEP 4 RESULT - proof documents accepted")
    print(f"  response shape : {result.get('response_shape')}")
    print(f"  next_step      : {result.get('next_step')}")
 
    next_step = (result.get("next_step") or "").rstrip("/")
    if not next_step.endswith("/contracts"):
        rule("STOPPING - Perch did not direct this enrollment to /contracts")
        print(f"next_step was: {result.get('next_step')!r}")
        print("Only the endpoint Perch actually returns is called. No guessing.")
        return 1
 
    # ------------------------------------------------------------- 5. contracts
    rule("STEP 5 - POST /contracts")
    print("request: X-Enrollment-Token only, NO request body (per spec)")
    try:
        contracts = client.generate_contracts(token)
    except (PerchValidationError, PerchTokenExpiredError, PerchError) as exc:
        rule("CONTRACT GENERATION REJECTED")
        dump_error(exc)
        return 1
 
    rule("SUCCESS - contracts generated")
    print(f"  response shape        : {contracts.get('response_shape')}")
    print(f"  contract_urls present : {contracts.get('contract_urls_present')}")
    print(f"  number of contracts   : {contracts.get('contract_count')}")
    print()
 
    safe = contracts_safe(contracts)
    if not safe:
        print("  (no contracts returned - report this, the spec marks contract_urls required)")
    for i, c in enumerate(safe):
        print(f"  [{i}] contract_name : {c.get('contract_name')}")
        print(f"      expires_at    : {c.get('expires_at')}")
        # Structural check only - the URL value is never printed.
        print(f"      url present   : {'yes' if c.get('url_present') else 'NO'}")
        if c.get("malformed"):
            print("      ** malformed entry - not an object **")
        print()
 
    print(f"  normalized next step  : {contracts.get('next_step')}")
 
    missing = [c.get("contract_name") for c in safe if not c.get("url_present")]
    if missing:
        rule("WARNING - some contracts returned without a usable URL")
        print(f"  {missing}")
        return 1
 
    rule("STOPPING HERE - as instructed")
    print("/contracts/accept was NOT called. No acceptance metadata was sent.")
    print("The enrollment is intentionally left mid-flow at Perch staging so the")
    print("real /contracts response can be inspected before acceptance is built.")
    print()
    print("Presigned URLs were received but deliberately not printed, logged, or")
    print("stored. They expire 1 hour after generation; call /contracts again for")
    print("fresh ones rather than caching.")
    return 0
 
 
 
if __name__ == "__main__":
    raise SystemExit(main())