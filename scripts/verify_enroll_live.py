"""
Live verification of POST /enroll through the normal PerchClient.

Sequence: /token -> /capacity -> /enroll, then STOPS. It does not call
/contracts, /lmi/*, or anything else - it prints the returned next_step so you
can decide what to verify next.

SAFETY
------
* Creates a REAL enrollment at Perch staging with TEST/EXAMPLE data only.
  Never run with real customer information.
* A fresh UUID email each run, so the documented duplicate-email 422 cannot hit.
* Calls the CLIENT directly, not the adapter: nothing is written to the Dalton
  database.
* Never prints the API key, signing key, or enrollment token.

    python scripts/verify_enroll_live.py
    python scripts/verify_enroll_live.py 12202 national-grid-ny
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ZIP = "12202"                      # verified live: capacity available
DEFAULT_UTILITY_SLUG = "national-grid-ny"  # verified live
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_BILL = os.path.join(REPO_ROOT, "test", "sample_utility_bill.pdf")

# Test/example customer.
#
# ADDRESS SOURCE: the address fields below are taken from the sample utility
# bill we upload (test/sample_utility_bill.pdf -> "123 MAIN ST, ALBANY NY
# 12207"), so the enrollment and the supporting document agree. If Perch
# cross-checks the bill against the submitted service address, this removes
# that failure mode.
#
# WHY zip_code (12202) DIFFERS FROM THE ADDRESS ZIP (12207): the spec states
# explicitly that zip_code is
#     "used for an initial solar project capacity check; independent of
#      billing_address[zip] and home_address[zip]"
# so they are allowed to differ. We keep 12202 because it is our verified
# capacity pair, and use the bill's real 12207 for the address fields. Both are
# Albany / National Grid territory.
#
# The account number is 10 digits, National Grid's published format. National
# Grid requires NO secondary_account_identifier (POD ID) and no meter numbers -
# those apply only to NYSEG, Central Hudson, and Rochester G&E.
TEST_CUSTOMER = {
    "first_name": "Dalton",
    "last_name": "Testcustomer",
    "phone_number": "5185550142",   # 10 digits, no country code, per the spec
    # customer_type is NOT hardcoded - it is derived from the live capacity
    # response by choose_customer_type(). Hardcoding "Residential" against an
    # LMI-only project is exactly what produced the 422 capacity_unavailable.
    "address_1": "123 Main St",     # from the sample bill
    "city": "Albany",               # from the sample bill
    "state": "NY",
    "address_zip": "12207",         # from the sample bill
    "utility_account_number": "1234567890",  # 10 digits (National Grid format)
}


def rule(t):
    print(f"\n{'-'*68}\n{t}\n{'-'*68}")


def choose_customer_type(details):
    """Selects customer_type from the LIVE capacity response.

    SPEC BASIS - there is NO separate field that selects the LMI path.
    `customer_type` alone does it. From the spec:

        customer_type:
          enum: [Residential, Business, LMI]
          description: >
            Customer type. `Residential`, `Business`, or `LMI`. `LMI` requires
            LMI capacity to be available for the project and is limited to a
            single utility account per enrollment.

    Perch validates the chosen type against the capacity it returned:
      * LMI when lmi_capacity_available is false        -> 422 capacity_unavailable
      * Residential when residential capacity is false  -> 422 capacity_unavailable
        ("Residential or Small CS capacity is not available") - the exact error
        we hit by hardcoding Residential against an LMI-only project.

    So the type must be derived from capacity, not assumed. Business is never
    auto-selected: it additionally requires business_name/title/phone and a
    home_address, which is a deliberate rep decision rather than a fallback.
    """
    if details.get("lmi_capacity_available"):
        return "LMI", "lmi_capacity_available=True"
    if details.get("residential_capacity_available"):
        return "Residential", "residential_capacity_available=True"
    if details.get("small_commercial_capacity_available"):
        # Small commercial is served by customer_type Business, which needs the
        # business_* fields and home_address - not something to guess at here.
        return None, ("only small_commercial_capacity_available=True; that path needs "
                      "customer_type=Business plus business_name/business_title/"
                      "business_phone and home_address, which this verifier does not send")
    return None, "no capacity of any type is available for this ZIP/utility"


def _dump_error_diagnostics(exc):
    """Prints everything Perch returned on an error, so the real cause is visible.

    Only the RESPONSE is printed. Request headers - which carry the API key,
    signing key, and enrollment token - are never touched.
    """
    import json as _json

    print(f"exception     : {type(exc).__name__}")
    print(f"message       : {exc}")
    print(f"HTTP status   : {getattr(exc, 'status_code', '(not captured)')}")
    print(f"Content-Type  : {getattr(exc, 'content_type', '(not captured)')}")
    print(f"request id    : {getattr(exc, 'request_id', None) or '(none returned)'}")

    body_json = getattr(exc, "body_json", None)
    body_text = getattr(exc, "body_text", None)

    if body_json is not None:
        print(f"\nbody is JSON  : type={type(body_json).__name__}")
        if isinstance(body_json, dict):
            print(f"top-level keys: {sorted(body_json.keys())}")
        elif isinstance(body_json, list):
            print(f"list length   : {len(body_json)}")

        print("\n--- COMPLETE JSON BODY ---")
        try:
            print(_json.dumps(body_json, indent=2, ensure_ascii=False)[:6000])
        except Exception:
            print(repr(body_json)[:6000])

        # The spec documents ValidationErrorsResponse as {"errors":[{field,message}]}.
        # Surface it explicitly - this is the actionable part.
        errors = None
        if isinstance(body_json, dict):
            for key in ("errors", "validation_errors", "error_details", "details"):
                if isinstance(body_json.get(key), list):
                    errors = body_json[key]
                    print(f"\n--- FIELD ERRORS (from '{key}') ---")
                    break
        elif isinstance(body_json, list):
            errors = body_json
            print("\n--- FIELD ERRORS (top-level list) ---")

        if errors:
            for item in errors:
                if isinstance(item, dict):
                    field = item.get("field") or item.get("name") or "(no field)"
                    msg = item.get("message") or item.get("error") or item.get("detail") or ""
                    print(f"  {field}: {msg}")
                    extra = {k: v for k, v in item.items()
                             if k not in ("field", "name", "message", "error", "detail")}
                    if extra:
                        print(f"      (other keys: {extra})")
                else:
                    print(f"  {item!r}")
        else:
            print("\n(no recognizable field-error list - see the complete JSON body above)")
    elif body_text:
        print(f"\nbody is NOT JSON (length {len(body_text)})")
        print("--- RAW BODY ---")
        print(body_text[:6000])
    else:
        print("\n(empty response body)")


def main():
    zip_code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP
    utility_slug = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_UTILITY_SLUG

    from services.perch.config import get_perch_client, get_api_mode
    if get_api_mode() != "live":
        print("PERCH_API_MODE is not 'live'. Refusing to run.")
        sys.exit(2)

    if not os.path.exists(SAMPLE_BILL):
        print(f"Sample bill not found: {SAMPLE_BILL}")
        sys.exit(2)

    from services.perch.client import build_enrollment_multipart
    from services.perch.errors import (
        PerchError, PerchEnrollmentInProgressError, PerchNoCapacityError,
        PerchValidationError, PerchTokenExpiredError,
    )

    client = get_perch_client()
    print(f"PERCH_API_MODE  = live")
    print(f"enrollment base : {client.enrollment_base_url}")
    print(f"API key set     : {bool(client.api_key)}")
    print(f"signing key set : {bool(client.secret_key)}")

    email = f"dalton.enroll.{uuid.uuid4().hex[:12]}@example.com"

    # ------------------------------------------------------------- 1. token
    rule("STEP 1 - POST /token")
    print(f"email : {email}")
    try:
        tok = client.request_token(email)
    except PerchEnrollmentInProgressError as e:
        print(f"UNEXPECTED 422 for a fresh address: {e}")
        sys.exit(1)
    except PerchError as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    token = tok["enrollment_token"]
    print(f"  token received : yes (length {len(token)})")
    print(f"  response shape : {tok['response_shape']}")

    # ---------------------------------------------------------- 2. capacity
    rule("STEP 2 - POST /capacity")
    print(f"zip_code={zip_code}  utility_name={utility_slug}")
    try:
        cap = client.check_capacity(token, zip_code, utility_slug)
    except PerchNoCapacityError as e:
        print(f"503 NO CAPACITY: {e}")
        print("Cannot enroll without capacity. Ask Perch for a ZIP/utility that has it.")
        sys.exit(1)
    except PerchError as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
    details = cap.get("project_details") or {}
    print(f"  response shape : {cap['response_shape']}")
    for k in sorted(details):
        print(f"    {k} = {details[k]!r}")
    print(f"  next_step      : {cap['next_step']}")

    customer_type, why = choose_customer_type(details)
    if customer_type is None:
        rule("CANNOT ENROLL - no suitable customer_type for the returned capacity")
        print(why)
        print("\nNot sending /enroll: it would fail 422 capacity_unavailable.")
        sys.exit(1)
    print(f"\n  customer_type  : {customer_type}   (chosen because {why})")
    if customer_type == "LMI":
        print("  NOTE: the spec limits LMI enrollments to ONE utility account.")

    # ------------------------------------------------------------ 3. enroll
    rule("STEP 3 - POST /enroll  (multipart, indexed [n] fields)")
    enrollment = {
        "email_address": email,
        "first_name": TEST_CUSTOMER["first_name"],
        "last_name": TEST_CUSTOMER["last_name"],
        "phone_number": TEST_CUSTOMER["phone_number"],
        "customer_type": customer_type,   # derived from live capacity
        "utility_name": utility_slug,
        # Capacity-check ZIP. Spec: independent of the address ZIPs below.
        "zip_code": zip_code,
        "billing_address": {
            "address_1": TEST_CUSTOMER["address_1"],
            "city": TEST_CUSTOMER["city"],
            "state": TEST_CUSTOMER["state"],
            "zip": TEST_CUSTOMER["address_zip"],
        },
        "utility_accounts": [{
            "utility_account_number": TEST_CUSTOMER["utility_account_number"],
            "service_address": {
                "address_1": TEST_CUSTOMER["address_1"],
                "city": TEST_CUSTOMER["city"],
                "state": TEST_CUSTOMER["state"],
                "zip": TEST_CUSTOMER["address_zip"],
            },
            "utility_bills": [SAMPLE_BILL],
        }],
    }

    if zip_code != TEST_CUSTOMER["address_zip"]:
        print(f"NOTE: capacity zip_code={zip_code} differs from the address ZIP "
              f"{TEST_CUSTOMER['address_zip']} (taken from the sample bill).")
        print("      The spec states zip_code is independent of billing_address[zip].")
        print("      If staging 422s on this, that independence does not hold in practice.")

    # Spec: LMI "is limited to a single utility account per enrollment"
    # (422 too_many_utility_accounts otherwise). Guard before spending a call.
    if customer_type == "LMI" and len(enrollment["utility_accounts"]) != 1:
        print(f"LMI requires exactly one utility account; built "
              f"{len(enrollment['utility_accounts'])}. Aborting before the call.")
        sys.exit(1)

    form, files = build_enrollment_multipart(enrollment)
    print("multipart form fields:")
    for k in sorted(form):
        print(f"    {k} = {form[k]!r}")
    print("multipart file fields:")
    for k, v in files.items():
        print(f"    {k} = {v[0]} ({v[2]})")

    try:
        result = client.create_enrollment(token, form, files)
    except PerchValidationError as e:
        rule("422 VALIDATION - staging rejected the enrollment")
        _dump_error_diagnostics(e)
        print("\nNothing was changed in application code. Send this whole block back")
        print("so the payload can be corrected against the real reason.")
        sys.exit(1)
    except PerchTokenExpiredError as e:
        rule("403 - token rejected on /enroll")
        _dump_error_diagnostics(e)
        sys.exit(1)
    except PerchError as e:
        rule(f"FAILED - {type(e).__name__}")
        _dump_error_diagnostics(e)
        sys.exit(1)
    finally:
        for v in files.values():
            try:
                v[1].close()
            except Exception:
                pass

    # ----------------------------------------------------------- 4. report
    rule("RESULT - enrollment created")
    raw = result.get("raw", {})
    print(f"raw response keys : {sorted(raw.keys())}")
    print(f"response shape    : {result['response_shape']}")
    print(f"next_step         : {result['next_step']}")

    nxt = (result["next_step"] or "").rstrip("/")
    if nxt.endswith("/contracts"):
        verdict = "NON-LMI project -> next endpoint is POST /contracts"
    elif nxt.endswith("/lmi/proof_docs"):
        verdict = "LMI IRA project -> next endpoint is POST /lmi/proof_docs"
    elif nxt.endswith("/lmi/self_attestation"):
        verdict = "LMI self-attestation -> next endpoint is POST /lmi/self_attestation"
    else:
        verdict = f"UNRECOGNIZED next_step: {result['next_step']!r} - report this"
    print(f"\n=> {verdict}")

    print("\nSTOPPING HERE as instructed. No further endpoint was called.")
    print("This enrollment now exists at Perch staging and is mid-flow.")
    sys.exit(0)


if __name__ == "__main__":
    main()
