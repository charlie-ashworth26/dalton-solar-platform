"""
Read-only live verification of POST /token through the NORMAL PerchClient.

Confirms the client parses the real staging response. Does NOT call /capacity,
does NOT touch the database, and does NOT print credentials.

It DOES create a real enrollment session at Perch for the email you pass, so use
a unique throwaway address each run - reusing one returns the documented 422.

    python scripts/verify_token_live.py you+run1@example.com
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    email = sys.argv[1]

    from services.perch.config import get_perch_client, get_api_mode
    mode = get_api_mode()
    print(f"PERCH_API_MODE = {mode}")
    if mode != "live":
        print("\nRefusing to run: this script is for live staging verification.")
        print("Set PERCH_API_MODE=live (plus the base URLs and credentials) first.")
        sys.exit(1)

    client = get_perch_client()
    # Presence checks only - values are never printed.
    print(f"enrollment base : {client.enrollment_base_url}")
    print(f"API key set     : {bool(client.api_key)}")
    print(f"signing key set : {bool(client.secret_key)}")
    print(f"requesting token for: {email}\n")

    from services.perch.errors import PerchEnrollmentInProgressError, PerchError
    try:
        result = client.request_token(email)
    except PerchEnrollmentInProgressError as e:
        print("422 - an enrollment already exists for this email.")
        print(f"  {e}")
        print("\nThis is the documented duplicate-email response. Use a fresh address,")
        print("or resume with PATCH /refresh_token.")
        sys.exit(0)
    except PerchError as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    tok = result["enrollment_token"]
    print("SUCCESS")
    print(f"  response shape : {result['response_shape']}")
    # Never print the token, or any part of it. Presence and length only.
    print(f"  token received : yes (length {len(tok)})")
    print(f"  next_step      : {result['next_step']}")
    print(f"  expires_at     : {result['expires_at'] or 'ABSENT - expiry will be derived locally'}")
    print(f"  raw keys       : {sorted(result['raw'].keys())}")

    if result["response_shape"] == "staging_alias":
        print("\nStaging returned the alias shape (token / next_step_url).")
        print("Normalized successfully - no code change needed.")
    if not result["expires_at"]:
        print("\nNo expires_at returned. token_manager will record expires_at_source='derived'.")


if __name__ == "__main__":
    main()
