"""
NYSEG diagnostic - determines exactly WHERE the NYSEG path stops.

Distinguishes:
    A. Dalton frontend validation problem  (ruled out/in without the GUI)
    B. invalid or ineligible ZIP
    C. Perch staging has no NYSEG capacity
    D. account / POD validation failure
    E. another Perch/API issue

Runs Dalton's own validation offline FIRST, then calls Perch through the normal
client. STOPS after capacity unless capacity exists and you explicitly opt in to
validating account/POD handling.

NEVER accepts contracts. Never prints keys, tokens, or presigned URLs.

    python scripts/verify_nyseg_live.py 13210
    python scripts/verify_nyseg_live.py 13210 --validate-account 12345678901 --pod N01123456789012
"""
import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UTILITY = "nyseg"


def rule(t):
    print(f"\n{'-'*68}\n{t}\n{'-'*68}")


def main():
    p = argparse.ArgumentParser(description="NYSEG capacity / validation diagnostic.")
    p.add_argument("zip_code", help="NYSEG ZIP to test")
    p.add_argument("--validate-account", dest="account", default=None,
                   help="Optional account number to validate against Dalton's rules")
    p.add_argument("--pod", dest="pod", default=None,
                   help="Optional POD / secondary identifier to validate")
    args = p.parse_args()

    from app import app
    from services.perch import utilities as U

    # ---- Stage 1: Dalton's own rules, offline. Rules out (A) and (D) locally.
    rule("STAGE 1 - Dalton validation rules for NYSEG (offline, no API call)")
    with app.app_context():
        u = U.by_slug(UTILITY)
        if not u:
            print("  FAIL: 'nyseg' is not in the utility registry.")
            return 2
        pod_rule = U.pod_id_rule(UTILITY)
        print(f"  slug                 : {u['slug']}")
        print(f"  display name         : {u['display_name']}")
        print(f"  slug confirmed by spec: {bool(u['slug_confirmed'])}")
        print(f"  account number length : {u['account_number_length']}")
        print(f"  POD required          : {bool(u['requires_pod_id'])}")
        if pod_rule:
            print(f"  POD rule              : {pod_rule['description']}")

        if args.account is not None:
            expected = u["account_number_length"]
            ok = args.account.isdigit() and len(args.account) == expected
            print(f"\n  account {args.account!r}: "
                  f"{'VALID' if ok else f'INVALID - NYSEG requires {expected} digits, got {len(args.account)}'}")
            if not ok:
                print("  -> Cause (D): account number would be rejected. Fix before testing capacity.")
        if args.pod is not None:
            err = U.validate_pod_id(UTILITY, args.pod)
            print(f"  POD {args.pod!r}: {'VALID' if err is None else 'INVALID - ' + err}")
            if err:
                print("  -> Cause (D): POD would be rejected.")

    zip_code = args.zip_code.strip()
    if not (zip_code.isdigit() and len(zip_code) == 5):
        print(f"\n  Cause (B): {zip_code!r} is not a 5-digit ZIP. Dalton rejects it before Perch.")
        return 1

    # ---- Stage 2: live capacity. Separates (B)/(C) from (E).
    from services.perch.config import get_perch_client, get_api_mode
    mode = get_api_mode()
    rule(f"STAGE 2 - POST /capacity for NYSEG (PERCH_API_MODE={mode})")
    if mode != "live":
        print("  Not in live mode - refusing to draw conclusions about staging.")
        print("  NOTE: the mock has NO NYSEG fixture, so mock mode ALWAYS reports")
        print("  no capacity for NYSEG. That is a fixture gap, not a Dalton bug.")
        print("  Set PERCH_API_MODE=live (plus credentials) to test staging.")
        return 2

    from services.perch.errors import (
        PerchError, PerchNoCapacityError, PerchValidationError,
        PerchEnrollmentInProgressError,
    )
    client = get_perch_client()
    email = f"dalton.nyseg.{uuid.uuid4().hex[:12]}@example.com"
    print(f"  email    : {email}")
    print(f"  zip_code : {zip_code}")
    print(f"  utility  : {UTILITY}")

    try:
        tok = client.request_token(email)
    except PerchEnrollmentInProgressError as e:
        print(f"  422 for a fresh address - unexpected: {e}")
        return 1
    except PerchError as e:
        print(f"  Cause (E): token failed - {type(e).__name__}: {e}")
        return 1
    token = tok["enrollment_token"]
    print(f"  token received : yes (length {len(token)})")

    try:
        cap = client.check_capacity(token, zip_code, UTILITY)
    except PerchNoCapacityError as e:
        rule("RESULT - Cause (C): Perch staging has NO NYSEG capacity for this ZIP")
        print(f"  {e}")
        print("\n  This is a Perch-side availability fact, NOT a Dalton bug.")
        print("  Ask Perch which NYSEG ZIPs currently have open capacity in staging.")
        return 0
    except PerchValidationError as e:
        rule("RESULT - Cause (B) or (D): Perch rejected the request")
        print(f"  {e}")
        print("\n  Perch parsed the request and refused it - check the ZIP and slug.")
        return 1
    except PerchError as e:
        rule(f"RESULT - Cause (E): {type(e).__name__}")
        print(f"  {e}")
        return 1

    rule("RESULT - NYSEG CAPACITY EXISTS")
    details = cap.get("project_details") or {}
    for k in sorted(details):
        print(f"    {k} = {details[k]!r}")
    print(f"  next_step : {cap.get('next_step')}")
    print("\n  Cause (C) is ruled out. If the GUI still fails here, the problem is")
    print("  Cause (A) - Dalton frontend validation - most likely the account")
    print("  length (11 digits for NYSEG vs 10 for National Grid) or the POD ID.")
    print("\n  STOPPING after capacity, as instructed. No enrollment was created.")
    print("  Re-run with --validate-account/--pod to check those rules offline,")
    print("  or ask for explicit approval before going further.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
