"""
HMAC-SHA256 request authentication for Perch.

Required for (OpenAPI spec, "Authentication and Authorization"):
    POST  /token
    PATCH /refresh_token
    GET   /markets/*      (capacity, utilities, utility_account_fields,
                           counties, counties/income_limits,
                           self_attestation_options)

Enrollment-session endpoints (Check Capacity through Submit Contracts
Acceptance, plus GET /status) use X-Enrollment-Token instead and must NOT
send HMAC headers.

Signing procedure, verbatim from the spec:

    Step 1 - Serialise the request body
      * POST /token, PATCH /refresh_token: compact JSON, no extra whitespace,
        UTF-8 encoded.
      * GET /markets/* (no body): the canonical query string - key=value
        pairs joined with '&', sorted alphabetically by key, with the RAW
        (not URL-encoded) values.
        e.g. utility_name=consolidated-edison-ny&zip_code=10001
        e.g. county=Martinsville city&state=VA   <- a space stays a space
        The SIGNED string and the TRANSMITTED string therefore differ
        whenever a value needs percent-encoding. See canonical_query_string()
        (signed) vs encoded_query_string() (sent).
    Step 2 - Unix timestamp, within +/-5 minutes of server time
    Step 3 - signed_payload = timestamp + "\n" + request_body
    Step 4 - hex_signature = lowercase_hex(hmac_sha256(secret, signed_payload))
             (64 characters)
    Step 5 - Headers: X-API-Key, X-HMAC-Signature, X-HMAC-Timestamp

This module is pure computation - no I/O, no network. It is fully testable
offline, which matters because a signing mistake is invisible until staging
rejects us with a generic "Authentication failed" that does not say why.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlencode

# Spec header names, exactly as published. HTTP headers are case-insensitive
# per RFC 7230, but these are reproduced verbatim so a reviewer comparing
# against the spec sees an exact match.
HEADER_API_KEY = "X-API-Key"
HEADER_SIGNATURE = "X-HMAC-Signature"
HEADER_TIMESTAMP = "X-HMAC-Timestamp"

# Spec: "±5 min window". Ours is informational only - the server enforces it.
TIMESTAMP_TOLERANCE_SECONDS = 300


def compact_json(payload: dict) -> str:
    """Step 1 for JSON bodies: compact JSON, no extra whitespace.

    separators=(",", ":") removes the spaces json.dumps inserts by default.
    A single stray space changes the signature and produces a generic
    "Authentication failed" from Perch with no indication of the cause.

    sort_keys is deliberately NOT set: the signature is computed over the exact
    bytes we transmit, so the serialization here must be the same object that
    gets sent. Key order does not matter as long as both are identical.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _sorted_items(params: dict):
    """Shared ordering for both query-string forms, so they correspond
    key-for-key. None values are dropped, never sent as the string 'None'."""
    return sorted((str(k), str(v)) for k, v in params.items() if v is not None)


def canonical_query_string(params: dict) -> str:
    """Step 1 for GET /markets/*: the string that is SIGNED.

    key=value pairs joined with '&', sorted alphabetically by key, with RAW
    (not URL-encoded) values.

    Spec, "Generating the HMAC Signature":
        "the canonical query string - key=value pairs joined with &, sorted
         alphabetically by key, with the raw (not URL-encoded) values."
    and on GET /markets/counties/income_limits:
        "the value is signed as-is, not URL-encoded (a space stays a space)."

    This is NOT what goes on the wire - see encoded_query_string(). For the
    existing NY call (zip_code + utility slug) the two are byte-identical,
    because no value contains a character that needs encoding; they diverge for
    every multi-word county name.

    CONFIRMED AGAINST PERCH STAGING 2026-10-25, county=Martinsville city&state=VA:
        signed raw              -> HTTP 200
        signed urlencode()      -> HTTP 401
    This function previously returned urlencode(items), which is the 401 case.
    Do not "simplify" it back.
    """
    return "&".join(f"{k}={v}" for k, v in _sorted_items(params))


def encoded_query_string(params: dict) -> str:
    """The string that is TRANSMITTED, percent-encoded.

    Sorted identically to canonical_query_string so signature and wire form
    correspond key-for-key.

    quote (space -> %20), not urlencode's default quote_plus (space -> '+'):
    %20 is the form the spec's own `curl --data-urlencode` examples produce and
    the form confirmed working against staging.
    """
    return urlencode(_sorted_items(params), quote_via=quote)


def build_signed_payload(timestamp: str, request_body: str) -> str:
    """Step 3: signed_payload = timestamp + "\\n" + request_body"""
    return f"{timestamp}\n{request_body}"


def compute_signature(secret_key: str, signed_payload: str) -> str:
    """Step 4: lowercase hex HMAC-SHA256, 64 characters."""
    if not secret_key:
        raise ValueError("A Perch shared secret is required to sign requests.")
    digest = hmac.new(
        secret_key.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest.lower()


def current_timestamp() -> str:
    """Step 2: Unix timestamp as a string."""
    return str(int(time.time()))


def sign_json_request(api_key: str, secret_key: str, payload: dict, timestamp: str = None):
    """Signs a JSON-body request (POST /token, PATCH /refresh_token).

    Returns (headers, body_string). The body string MUST be sent verbatim -
    passing the dict to a client library that re-serializes it would produce
    different bytes and invalidate the signature.
    """
    ts = timestamp or current_timestamp()
    body = compact_json(payload)
    signature = compute_signature(secret_key, build_signed_payload(ts, body))
    headers = {
        "Content-Type": "application/json",
        HEADER_API_KEY: api_key,
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: ts,
    }
    return headers, body


def sign_query_request(api_key: str, secret_key: str, params: dict, timestamp: str = None):
    """Signs a query-string request (GET /markets/*).

    Returns (headers, query_string) where:
      * the signature is computed over the RAW canonical string, and
      * query_string is the PERCENT-ENCODED form, to be appended to the URL.

    The caller appends the returned string as-is and must not let a client
    library re-encode or reorder the params - re-encoding '%20' or reordering
    the keys invalidates the signature.

    Per the spec, Content-Type is NOT sent for this call (no request body).
    """
    ts = timestamp or current_timestamp()
    signed = canonical_query_string(params)
    signature = compute_signature(secret_key, build_signed_payload(ts, signed))
    headers = {
        HEADER_API_KEY: api_key,
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: ts,
    }
    return headers, encoded_query_string(params)


def verify_signature(secret_key: str, signed_payload: str, provided_signature: str) -> bool:
    """Constant-time comparison. Used by the mock client so the mock actually
    validates our signing rather than accepting anything - otherwise the mock
    would happily pass a signature that staging will reject."""
    expected = compute_signature(secret_key, signed_payload)
    return hmac.compare_digest(expected, (provided_signature or "").lower())
