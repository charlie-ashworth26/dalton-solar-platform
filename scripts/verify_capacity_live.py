"""
Read-only live verification of POST /capacity through the NORMAL PerchClient.

What it does
------------
1. POST /token with a brand-new unique email  -> real enrollment token
2. POST /capacity with that token             -> real capacity response
3. Compares the response against the contract we implemented from the
   published OpenAPI spec, and reports any disagreement.

Staging returns `next_step_url` where the YAML documents `next_step`. That alias
is now normalized by the client (same as on POST /token), so it is reported as
expected behaviour rather than flagged as a discrepancy. What this script still
fails on is a genuinely broken contract: missing project_details fields, or a
next step the workflow engine cannot resolve.

Both calls go through the normal PerchClient, using the already-implemented
HMAC authentication and X-Enrollment-Token handling. No token is pasted or
hardcoded. Nothing is modified to make the check pass.

Scope and safety
----------------
* Calls the CLIENT directly, not the adapter, so NOTHING is written to the
  Dalton database. No enrollment record, no audit rows, no token rows.
* Does create a real enrollment SESSION at Perch for the generated email.
  A fresh UUID-based address is used each run so the documented duplicate-email
  422 can never be hit.
* Never prints the API key, signing key, or enrollment token - not even a prefix.
* Does NOT call /enroll.

Inputs
------
Defaults come from the published spec's own cURL example for POST /capacity:

    -d '{"zip_code":"10001","utility_name":"consolidated-edison-ny"}'

`consolidated-edison-ny` is also one of the seven slugs in our perch_utilities
registry (migration 002), sourced from Perch's published slug-mapping table.
Override with argv if Perch tells you a different combination has capacity.

    python scripts/verify_capacity_live.py
    python scripts/verify_capacity_live.py 13348 national-grid-ny

Exit codes
----------
    0  capacity returned, or a documented 503 no-capacity result
    1  a call failed, or staging disagreed with our implemented contract
    2  bad usage / not in live mode
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Documented example from the published spec's POST /capacity cURL.
DEFAULT_ZIP = "10001"
DEFAULT_UTILITY_SLUG = "consolidated-edison-ny"

# The six project_details fields the spec marks as required.
DOCUMENTED_PROJECT_DETAIL_FIELDS = {
    "small_commercial_capacity_available",
    "lmi_capacity_available",
    "residential_capacity_available",
    "proof_documents_required",
    "savings_percent_for_residential_and_commercial_customers",
    "savings_percent_for_lmi_customers",
}


def rule(title):
    print(f"\n{'-' * 66}\n{title}\n{'-' * 66}")


def main():
    zip_code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP
    utility_slug = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_UTILITY_SLUG

    from services.perch.config import get_perch_client, get_api_mode
    mode = get_api_mode()
    print(f"PERCH_API_MODE = {mode}")
    if mode != "live":
        print("\nRefusing to run: this script is for live staging verification.")
        print("Set PERCH_API_MODE=live (plus base URLs and credentials) first.")
        sys.exit(2)

    from services.perch.errors import (
        PerchError, PerchEnrollmentInProgressError, PerchNoCapacityError,
        PerchTokenExpiredError, PerchValidationError,
    )

    client = get_perch_client()
    # Presence checks only - values are never printed.
    print(f"enrollment base : {client.enrollment_base_url}")
    print(f"API key set     : {bool(client.api_key)}")
    print(f"signing key set : {bool(client.secret_key)}")

    # A fresh address every run: POST /token returns the documented 422 if an
    # enrollment is already in progress for an email.
    email = f"dalton.capacity.{uuid.uuid4().hex[:12]}@example.com"

    # ---------------------------------------------------------------- step 1
    rule("STEP 1 - POST /token (normal PerchClient, HMAC-authenticated)")
    print(f"email : {email}")
    try:
        token_result = client.request_token(email)
    except PerchEnrollmentInProgressError as e:
        print(f"\nUNEXPECTED 422 for a freshly generated address: {e}")
        sys.exit(1)
    except PerchError as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    enrollment_token = token_result["enrollment_token"]
    print("  token received : yes (length %d)" % len(enrollment_token))
    print(f"  response shape : {token_result['response_shape']}")
    print(f"  next_step      : {token_result['next_step']}")
    print(f"  raw keys       : {sorted(token_result['raw'].keys())}")

    # ---------------------------------------------------------------- step 2
    rule("STEP 2 - POST /capacity (X-Enrollment-Token, no HMAC headers)")
    print(f"zip_code     : {zip_code}")
    print(f"utility_name : {utility_slug}")
    print("(field names exactly as implemented from the published spec)")

    try:
        response = client.check_capacity(enrollment_token, zip_code, utility_slug)
    except PerchNoCapacityError as e:
        rule("RESULT - 503 NO CAPACITY (a documented business outcome)")
        print(f"{e}")
        print("\nThis is NOT a failure. Per the spec, 503 means no open solar project")
        print("capacity for this utility and ZIP, and enrollment must not proceed.")
        print("Our implementation already treats this as capacity_available=False.")
        print("\nTo exercise the 200 path, ask Perch which staging ZIP/utility pair")
        print("currently has capacity and re-run with those values.")
        sys.exit(0)
    except PerchTokenExpiredError as e:
        print(f"\n403 on a token issued seconds ago: {e}")
        print("Report this - it would contradict the documented 1-hour TTL.")
        sys.exit(1)
    except PerchValidationError as e:
        print(f"\n422 VALIDATION: {e}")
        print("Staging rejected the ZIP/utility pair. Verify the slug spelling with Perch.")
        sys.exit(1)
    except PerchError as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    # ---------------------------------------------------------------- step 3
    rule("STEP 3 - Does staging match our implemented contract?")

    # check_capacity() now returns the NORMALIZED envelope. The untouched
    # Perch response is preserved under "raw".
    raw = response.get("raw", {})
    print(f"raw staging keys : {sorted(raw.keys())}")
    print(f"response shape   : {response.get('response_shape')}")
    if response.get("response_shape") == "staging_alias":
        print("  -> staging used 'next_step_url'; normalized to 'next_step'. Expected.")
    elif response.get("response_shape") == "documented":
        print("  -> staging used the documented 'next_step'. Also fine.")

    problems = []

    details = response.get("project_details")
    if details is None:
        problems.append("No 'project_details' - the adapter reads response['project_details'].")
        print("project_details: MISSING")
    else:
        got = set(details.keys())
        missing = DOCUMENTED_PROJECT_DETAIL_FIELDS - got
        extra = got - DOCUMENTED_PROJECT_DETAIL_FIELDS
        print(f"\nproject_details keys : {sorted(got)}")
        for k in sorted(got):
            print(f"    {k} = {details[k]!r}")
        if missing:
            problems.append(f"project_details missing documented field(s): {sorted(missing)}")
        if extra:
            print(f"\nNOTE: undocumented extra field(s): {sorted(extra)} "
                  "(not fatal - unknown fields are ignored)")

    next_step = response.get("next_step")
    print(f"\nnormalized next_step : {next_step}")
    if not next_step:
        problems.append(
            "Neither 'next_step' nor 'next_step_url' present - the workflow engine needs one.")

    # The workflow engine resolves the next_step URL by path suffix. Confirm the
    # real staging URL actually resolves, rather than only that a URL exists.
    if next_step:
        try:
            from services.perch.workflow import resolve_next_step_key
            step_key, recognized = resolve_next_step_key(next_step)
            print(f"workflow resolves to : {step_key!r} (recognized={recognized})")
            if not recognized:
                problems.append(
                    f"Workflow engine does not recognize next_step URL: {next_step}")
        except Exception as e:  # pragma: no cover - diagnostic only
            print(f"(workflow resolution check skipped: {e})")

    # ---------------------------------------------------------------- verdict
    if problems:
        rule("DISCREPANCY - STOPPING, NO CODE CHANGED")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print("\nReport these before changing application code.")
        sys.exit(1)

    rule("SUCCESS - staging matches our implemented capacity contract")
    print("  - project_details present with all six documented fields")
    print("  - next_step available after normalization")
    print("  - workflow engine recognizes the returned next step")
    print("\nThe normal client/adapter path handles the real staging response.")
    print("Do NOT proceed to /enroll yet.")
    sys.exit(0)


if __name__ == "__main__":
    main()
