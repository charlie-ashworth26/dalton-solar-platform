"""
Controlled staging verification of POST /contracts/accept and GET /status.

Sequence:
    /token -> /capacity -> /enroll -> /lmi/proof_docs -> /contracts
    -> GET /status BEFORE acceptance   (confirms acceptance is outstanding)
    -> PAUSE
    -> POST /contracts/accept          (ONLY with the explicit safety flag)
    -> GET /status AFTER acceptance

WITHOUT the safety flag this script STOPS before acceptance. It shows what it
would send and exits 0. Nothing is submitted.

    python scripts/verify_contracts_accept_live.py                      # dry run
    python scripts/verify_contracts_accept_live.py --i-understand-this-accepts-contracts

ACCEPTANCE IS NOT REVERSIBLE. It records the customer's agreement at Perch and
Perch has not documented whether the endpoint is idempotent - so this script
never retries it.

Never printed: API key, signing key, HMAC values, enrollment token, presigned
contract URLs.
"""
import argparse
import os
import json
import socket
from datetime import datetime
import sys
import uuid
 
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
SAFETY_FLAG = "--i-understand-this-accepts-contracts"
LOCAL_IP = socket.gethostbyname(socket.gethostname())
LOCAL_USER_AGENT = "DaltonSolar-StagingVerifier/1.0 (contracts-accept)"

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
    # BUGFIX: previously positional argv[1]/argv[2], so running the script with
    # only the safety flag parsed "--i-understand-this-accepts-contracts" as the
    # ZIP code. The flag is now parsed independently of the optional overrides.
    parser = argparse.ArgumentParser(
        description="Controlled staging verification of POST /contracts/accept and GET /status.")
    parser.add_argument("zip_code", nargs="?", default=DEFAULT_ZIP,
                        help=f"Optional ZIP override (default {DEFAULT_ZIP})")
    parser.add_argument("utility", nargs="?", default=DEFAULT_UTILITY,
                        help=f"Optional utility slug override (default {DEFAULT_UTILITY})")
    parser.add_argument(SAFETY_FLAG, dest="accept_for_real", action="store_true",
                        help="Actually submit contract acceptance. NOT REVERSIBLE.")
    args = parser.parse_args()
    zip_code = args.zip_code
    utility = args.utility
 
    from services.perch.config import get_perch_client, get_api_mode
    from services.perch.client import (
        acceptance_timestamp, ACCEPTANCE_CLOCK_SKEW_SECONDS,
        build_enrollment_multipart,
        build_proof_docs_multipart,
        # Returns URL-free copies of each contract. Used instead of touching
        # normalized["raw"], so a presigned URL can never reach stdout.
        contracts_safe,
    )
    from services.perch.errors import (
        PerchAmbiguousOutcomeError,
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
 
    rule("STEP 5 RESULT - contracts generated")
    print(f"  response shape        : {contracts.get('response_shape')}")
    print(f"  number of contracts   : {contracts.get('contract_count')}")
    for i, c in enumerate(contracts_safe(contracts)):
        print(f"  [{i}] {c.get('contract_name')}  (expires {c.get('expires_at')}, "
              f"url present: {'yes' if c.get('url_present') else 'NO'})")

    # ------------------------------------------------------- 6. status BEFORE
    rule("STEP 6 - GET /status BEFORE acceptance")
    try:
        before = client.get_status(token)
    except PerchError as exc:
        rule("STATUS FAILED"); dump_error(exc); return 1
    print(f"  completed        : {before.get('completed')}")
    print(f"  completed_steps  : {before.get('completed_steps')}")
    print(f"  remaining_steps  : {before.get('remaining_steps')}")
    print(f"  next_step        : {before.get('next_step')}")

    outstanding = "submit_contracts_acceptance" in (before.get("remaining_steps") or [])
    if outstanding:
        print("\n  submit_contracts_acceptance is OUTSTANDING, as expected.")
    else:
        print("\n  WARNING: submit_contracts_acceptance is not in remaining_steps.")
        print("  Perch may already consider this enrollment accepted.")

    # ------------------------------------------------------------ 7. the gate
    rule("STEP 7 - ACCEPTANCE GATE")
    metadata_preview = {
        "ip_address": "<server-side: Flask request.remote_addr>",
        "timestamp": acceptance_timestamp(),
        "user_agent": "<server-side: incoming User-Agent, max 2048 chars>",
    }
    print("Dalton would POST /contracts/accept with exactly:")
    print(json.dumps({"metadata": metadata_preview}, indent=2))
    print("\nNo contract ids, no acceptance array, no acknowledgment flag.")
    print("customer_confirmed is a Dalton-side precondition and is NOT sent to Perch.")

    if not args.accept_for_real:
        rule("STOPPING - acceptance NOT submitted")
        print("This was a dry run. Nothing was accepted at Perch.")
        print(f"\nTo actually accept, re-run with:\n    {SAFETY_FLAG}")
        print("\nAcceptance is not reversible and is not retried on ambiguous failure.")
        return 0

    # ------------------------------------------------------------- 8. accept
    rule("STEP 8 - POST /contracts/accept  (LIVE)")
    metadata = {
        "ip_address": LOCAL_IP,
        "timestamp": acceptance_timestamp(),
        "user_agent": LOCAL_USER_AGENT[:2048],
    }
    print(f"  ip_address : {metadata['ip_address']}")
    print(f"  timestamp  : {metadata['timestamp']}")
    print(f"  user_agent : {metadata['user_agent'][:60]}... ({len(metadata['user_agent'])} chars)")
    try:
        accepted = client.accept_contracts(token, metadata)
    except PerchAmbiguousOutcomeError as exc:
        rule("AMBIGUOUS OUTCOME - DO NOT RESEND")
        print(f"{exc}")
        print("\nAcceptance may or may not have been recorded. Checking status...")
        try:
            amb = client.get_status(token)
            print(f"  completed       : {amb.get('completed')}")
            print(f"  remaining_steps : {amb.get('remaining_steps')}")
            if "submit_contracts_acceptance" not in (amb.get("remaining_steps") or []):
                print("\n  Status suggests Perch DID record the acceptance.")
            else:
                print("\n  Status suggests acceptance is still outstanding.")
        except PerchError as e2:
            print(f"  status check also failed: {e2}")
        print("\nReconcile with Perch before any further attempt.")
        return 1
    except PerchError as exc:
        rule("ACCEPTANCE REJECTED"); dump_error(exc); return 1

    print(f"\n  HTTP 202 accepted")
    print(f"  message : {accepted.get('message')}")

    # -------------------------------------------------------- 9. status AFTER
    rule("STEP 9 - GET /status AFTER acceptance")
    try:
        after = client.get_status(token)
    except PerchError as exc:
        rule("POST-ACCEPT STATUS FAILED"); dump_error(exc)
        print("\nAcceptance returned 202; only the status check failed.")
        return 1
    print(f"  completed        : {after.get('completed')}")
    print(f"  completed_steps  : {after.get('completed_steps')}")
    print(f"  remaining_steps  : {after.get('remaining_steps')}")
    print(f"  next_step        : {after.get('next_step')}")

    if after.get("completed") is True and not after.get("remaining_steps") \
            and after.get("next_step") is None:
        rule("ENROLLMENT COMPLETE AT PERCH")
    else:
        rule("ACCEPTED - PERCH PROCESSING NOT YET COMPLETE")
        print("202 means accepted for processing, not that every step finished.")
        print("The status above is authoritative; do not mark this complete locally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
