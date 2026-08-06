"""
HMAC-SHA256 request authentication for Perch.

Required for exactly three endpoints (OpenAPI spec, "Authentication and
Authorization"):
    POST  /token
    PATCH /refresh_token
    GET   /markets/capacity

Enrollment-session endpoints (Check Capacity through Submit Contracts
Acceptance, plus GET /status) use X-Enrollment-Token instead and must NOT
send HMAC headers.

Signing procedure, verbatim from the spec:

    Step 1 - Serialise the request body
      * POST /token, PATCH /refresh_token: compact JSON, no extra whitespace,
        UTF-8 encoded.
      * GET /markets/capacity (no body): the canonical query string -
        key=value pairs joined with '&', sorted alphabetically by key.
        e.g. utility_name=consolidated-edison-ny&zip_code=10001
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
from urllib.parse import urlencode

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


def canonical_query_string(params: dict) -> str:
    """Step 1 for GET /markets/capacity: key=value pairs joined with '&',
    sorted alphabetically by key.

    The spec's example is `utility_name=consolidated-edison-ny&zip_code=10001`
    and states "this exact string is both signed and sent" - so the caller must
    append this same string to the URL rather than letting a client library
    re-encode the params in a different order.
    """
    items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
    return urlencode(items)


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
    """Signs a query-string request (GET /markets/capacity).

    Returns (headers, query_string). Per the spec, Content-Type is NOT sent for
    this call (no request body), and the returned query string must be appended
    to the URL exactly as-is.
    """
    ts = timestamp or current_timestamp()
    qs = canonical_query_string(params)
    signature = compute_signature(secret_key, build_signed_payload(ts, qs))
    headers = {
        HEADER_API_KEY: api_key,
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: ts,
    }
    return headers, qs


def verify_signature(secret_key: str, signed_payload: str, provided_signature: str) -> bool:
    """Constant-time comparison. Used by the mock client so the mock actually
    validates our signing rather than accepting anything - otherwise the mock
    would happily pass a signature that staging will reject."""
    expected = compute_signature(secret_key, signed_payload)
    return hmac.compare_digest(expected, (provided_signature or "").lower())
