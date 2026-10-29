"""
The Perch client contract — rewritten in Milestone 2 to match the published
Partner Enrollment API in NY (v1, OAS 3.0).

Every endpoint, header, parameter name, and status-code meaning below comes from
the Swagger docs, not from inference. Where something is still unknown it raises
PerchNotImplementedError rather than guessing — see PERCH_OPEN_ITEMS.md.

Base URLs (documented):
    Enrollment flow:     https://api.perchenergy.com/affiliate_partners/v1/enrollments
    Pre-enrollment flow: https://api.perchenergy.com/affiliate_partners/v1/markets

Auth (documented):
    Enrollment flow uses an enrollment_token (UUID) in the X-Enrollment-Token
    header. It expires after 1 HOUR. A 403 on an enrollment-session endpoint
    means expired/invalid - call PATCH /refresh_token, then retry the original
    request.

    Pre-enrollment (GET /markets/capacity) uses HMAC auth with NO enrollment
    token. The signing scheme is not published; get_market_capacity() therefore
    raises rather than guessing.
"""
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from services.perch import hmac_auth
from services.perch.errors import (
    PerchAuthError, PerchUnavailableError, PerchValidationError,
    PerchTokenExpiredError, PerchNoCapacityError, PerchNotImplementedError,
    PerchNotFoundError, PerchEnrollmentInProgressError, PerchAmbiguousOutcomeError,
)


# NEW YAML /token 422 examples: "Email has already been taken" and "An
# enrollment request already exists for this email. Use the /status endpoint...".
# Matched on message text because the spec gives every 422 the same
# error code ("unprocessable_entity"), so the code alone cannot distinguish them.
_IN_PROGRESS_MARKERS = ("already been taken", "already exists")


def _is_enrollment_in_progress(resp):
    try:
        return any(m in (resp.json().get("message") or "").lower()
                   for m in _IN_PROGRESS_MARKERS)
    except Exception:
        return False


# Perch's documented wording when a state-changing step is replayed against an
# enrollment it has already advanced past:
#   422 {"error": "unprocessable_entity",
#        "message": "This step has already been completed. Use the /status
#                    endpoint to check the current status of the enrollment."}
# Matched on the message text because the 422 code is shared with every
# ordinary validation failure on the same endpoint.
_STEP_COMPLETED_MARKER = "already been completed"


def is_step_already_completed_error(exc) -> bool:
    """True ONLY for the documented "this step has already been completed" 422.

    Deliberately narrow, and narrow in three independent ways: the status must
    be 422, the message must carry the documented phrase, and the error must be
    a definite validation rejection. An ordinary correctable 422 - a bad source
    type, a missing name - matches none of them and stays an ordinary error
    the rep can fix.

    PerchAmbiguousOutcomeError is NOT a PerchValidationError, so an outcome
    that may or may not have reached Perch can never be read as "already done".
    """
    from services.perch.errors import PerchValidationError
    if not isinstance(exc, PerchValidationError):
        return False
    if getattr(exc, "status_code", None) != 422:
        return False
    body = getattr(exc, "body_json", None)
    if isinstance(body, dict):
        message = str(body.get("message") or "")
        if _STEP_COMPLETED_MARKER in message.lower():
            return True
    # Fallback for a transport that gave us no parsed body: the formatted
    # message still contains Perch's text verbatim.
    return _STEP_COMPLETED_MARKER in str(exc).lower()


def is_duplicate_email_error(exc) -> bool:
    """True only for the CONFIRMED duplicate-email rejection.

    /enroll has no dedicated duplicate-email error class - Perch returns 422 for
    every validation failure and the client maps them all to
    PerchValidationError. This is the one place that decides which of those 422s
    is the duplicate-email case, reusing _IN_PROGRESS_MARKERS above so the
    wording is defined once for both /token and /enroll.

    Deliberately narrow. A marker alone is not enough: the message must also
    name the email, so an unrelated "... already exists" rejection the rep could
    fix (a duplicate utility account, say) is NOT treated as a duplicate email.

    PerchEnrollmentInProgressError needs no text match - that class IS the
    duplicate-email case by definition.
    """
    from services.perch.errors import (
        PerchEnrollmentInProgressError, PerchValidationError)
    if isinstance(exc, PerchEnrollmentInProgressError):
        return True
    if isinstance(exc, PerchValidationError):
        text = str(exc).lower()
        return "email" in text and any(m in text for m in _IN_PROGRESS_MARKERS)
    return False


def extract_next_step(data: dict):
    """THE one place UBS reads 'where do I go next' from a Perch response.

    Three spellings have to be tolerated at once, and this is the only
    function that knows that:

      * `next_step`      - documented on v1, and the ONLY spelling on v2
      * `next_step_url`  - observed on Perch staging v1 (2026-08), confirmed
                           on /token, /capacity, /enroll, /lmi/proof_docs and
                           /contracts
      * absent           - legitimately, on /contracts/accept, which is the
                           last step and documents no next_step at all

    The VALUE is returned exactly as Perch sent it: a full URL on v1, a bare
    path like `/enroll` on v2. Resolving that into a step key is
    workflow.resolve_next_step_key()'s job, not this one's - this function
    only answers which key the value was under.

    Returns (next_step, response_shape). The shape is recorded so the audit
    trail says which spelling Perch actually used on each call.
    """
    data = data or {}
    # The documented name wins when a response carries both.
    next_step = data.get("next_step") or data.get("next_step_url")
    if "next_step" in data:
        shape = "documented"
    elif "next_step_url" in data:
        shape = "staging_alias"
    else:
        shape = "no_next_step"
    return next_step, shape


def normalize_capacity_response(data: dict) -> dict:
    """Normalizes BOTH the documented and the observed staging capacity response.

    The published YAML documents:
        {"project_details": {...six fields...}, "next_step": "<url>"}

    Observed Perch staging (2026-08, verified live with zip 12202 /
    national-grid-ny) returns:
        {"project_details": {...six fields...}, "next_step_url": "<url>"}

    Same alias discrepancy already confirmed on POST /token, so this mirrors
    _parse_token_response(): accept either spelling, normalize to the documented
    name, and keep the untouched original under "raw" so the audit record stores
    exactly what Perch sent.

    `project_details` is passed through UNCHANGED - we never reshape Perch's
    business facts, only the envelope key we read the next step from.
    """
    data = data or {}
    # Documented name wins when both are present.
    next_step, _shape = extract_next_step(data)
    return {
        "project_details": data.get("project_details"),
        "next_step": next_step,
        "response_shape": _shape,
        "raw": data,
    }


def build_enrollment_multipart(enrollment: dict):
    """Builds the EXACT multipart field names POST /enroll requires.

    Returns (form_fields, file_fields) ready for requests' data=/files=.

    CRITICAL - the indexing format. An earlier spec revision used bare `[]`
    repeated keys; the current spec uses EXPLICIT NUMERIC INDICES:

        utility_accounts[0][utility_account_number]
        utility_accounts[0][service_address][city]
        utility_accounts[0][meter_numbers][0]
        utility_accounts[0][utility_bills][0]
        utility_accounts[1][utility_bills][0]

    Both the account index [n] and the inner list indices [m] are explicit.
    Getting this wrong yields a 422 that does not say why, so it is built in one
    place and unit-tested against the spec's own cURL example.

    Input shape (already validated by the caller):
        {
          "email_address", "first_name", "last_name", "phone_number",
          "customer_type", "utility_name", "zip_code",
          "billing_address": {address_1, address_2?, city, state, zip},
          "home_address": {...}?,          # Business only
          "business_name"?, "business_title"?, "business_phone"?,
          "utility_accounts": [
             {"utility_account_number", "service_address": {...},
              "secondary_account_identifier"?, "meter_numbers": [...]?,
              "utility_bills": ["/path/to/bill.pdf", ...]}
          ]
        }
    """
    form = {}
    files = {}

    # Flat top-level fields.
    #
    # E3 added two, both v2 and both OPTIONAL in the spec's sense - they are
    # sent when UBS has them and omitted otherwise, never sent blank:
    #
    #   county           "County name of the customer, from
    #                    GET /markets/counties. Matched case-insensitively. [...]
    #                    If you do not send it, only projects with no county
    #                    restriction can be used." A blank value is treated by
    #                    Perch as not sent, so omitting is the same thing and
    #                    is clearer on the wire.
    #   savings_percent  "Optional when only one rate applies, required when
    #                    more than one does." The caller decides which; this
    #                    only serializes what it was handed.
    #
    # They are harmless on v1: v1 ignores savings_percent entirely ("Ignored
    # when the project was already selected by the deprecated
    # POST /v1/enrollments/capacity") and the adapter does not supply either
    # field on a v1 enrollment - see adapter.create_enrollment.
    for key in ("email_address", "first_name", "last_name", "phone_number",
                "customer_type", "utility_name", "zip_code", "county",
                "savings_percent",
                "business_name", "business_title", "business_phone"):
        val = enrollment.get(key)
        if val not in (None, ""):
            form[key] = str(val)

    # Nested address objects: bracket notation, no index
    for addr_key in ("billing_address", "home_address"):
        addr = enrollment.get(addr_key)
        if addr:
            for part in ("address_1", "address_2", "city", "state", "zip"):
                v = addr.get(part)
                if v not in (None, ""):
                    form[f"{addr_key}[{part}]"] = str(v)

    # utility_accounts: EXPLICIT numeric indices at every level
    for n, account in enumerate(enrollment.get("utility_accounts") or []):
        base = f"utility_accounts[{n}]"

        num = account.get("utility_account_number")
        if num not in (None, ""):
            form[f"{base}[utility_account_number]"] = str(num)

        sec = account.get("secondary_account_identifier")
        if sec not in (None, ""):
            form[f"{base}[secondary_account_identifier]"] = str(sec)

        svc = account.get("service_address") or {}
        for part in ("address_1", "address_2", "city", "state", "zip"):
            v = svc.get(part)
            if v not in (None, ""):
                form[f"{base}[service_address][{part}]"] = str(v)

        for m, meter in enumerate(account.get("meter_numbers") or []):
            form[f"{base}[meter_numbers][{m}]"] = str(meter)

        # Bills are PDF ONLY per the spec. The caller supplies file paths;
        # the tuple is (filename, fileobj, content_type) for requests.
        for m, bill_path in enumerate(account.get("utility_bills") or []):
            files[f"{base}[utility_bills][{m}]"] = (
                os.path.basename(bill_path), open(bill_path, "rb"), "application/pdf")

    return form, files



def build_proof_docs_multipart(utility_account_number: str, documents: list):
    """Build POST /lmi/proof_docs multipart parts from the published contract.

    `proof_doc` is a JSON multipart part with Content-Type application/json.
    Binary files are separate indexed parts named documents[0][file], etc.,
    correlated to proof_doc.documents by array index.
    """
    if not utility_account_number:
        raise ValueError("utility_account_number is required")
    if not documents:
        raise ValueError("at least one proof document is required")

    mime_by_ext = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".heic": "image/heic", ".pdf": "application/pdf",
    }
    metadata = []
    files = {}
    opened = []
    try:
        for i, doc in enumerate(documents):
            required = ("source_type", "name_on_document", "relationship",
                        "document_type", "file_path")
            missing = [k for k in required if not doc.get(k)]
            if missing:
                raise ValueError(
                    f"proof document {i} missing required fields: {', '.join(missing)}")
            path = str(doc["file_path"])
            ext = os.path.splitext(path)[1].lower()
            content_type = mime_by_ext.get(ext)
            if not content_type:
                raise ValueError(
                    f"unsupported proof document type {ext!r}; use JPEG, PNG, HEIC, or PDF")
            fh = open(path, "rb")
            opened.append(fh)
            metadata.append({
                "source_type": str(doc["source_type"]),
                "name_on_document": str(doc["name_on_document"]),
                "relationship": str(doc["relationship"]),
                "document_type": str(doc["document_type"]),
            })
            files[f"documents[{i}][file]"] = (
                os.path.basename(path), fh, content_type)

        proof_doc = {
            "utility_account_number": str(utility_account_number),
            "documents": metadata,
        }
        files["proof_doc"] = (
            None, json.dumps(proof_doc, separators=(",", ":")), "application/json")
        return files, proof_doc
    except Exception:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass
        raise


def normalize_self_attestation_response(data: dict) -> dict:
    """Normalize POST /lmi/self_attestation.

    Perch returns:
        {"self_attestation": {"utility_account_number": ..., "url": "<presigned>"},
         "next_step": ".../lmi/self_attestation/accept"}

    Like contracts, the presigned `url` is SHORT-LIVED (1 hour). The normalized
    view therefore carries `document_available` - a boolean - and NOT the link.
    The raw payload is kept only for the request/response lifetime so the route
    can hand the URL straight to the browser; nothing persists it.
    """
    sa = (data or {}).get("self_attestation") or {}
    if isinstance(sa, str):          # defensive: accept string form
        sa = {}
    # E1: through the shared extractor, like every other normalizer. This one
    # read `next_step` alone, so it would miss the staging alias the other
    # endpoints are known to use - and on v2 it now receives a bare path.
    next_step, shape = extract_next_step(data)
    return {
        "utility_account_number": sa.get("utility_account_number"),
        "document_available": bool(sa.get("url")),
        "next_step": next_step,
        # Legacy alias kept so callers written against v1 keep working. Same
        # value, whichever spelling and whichever version Perch answered with.
        "next_step_url": next_step,
        "response_shape": shape,
        "raw": data or {},
    }


def normalize_self_attestation_accept_response(data: dict) -> dict:
    """Normalize POST /lmi/self_attestation/accept (202).

    Perch returns {"self_attestation": "<message>", "next_step": ".../contracts"}.
    """
    next_step, shape = extract_next_step(data)
    return {
        "message": (data or {}).get("self_attestation"),
        "next_step": next_step,
        "next_step_url": next_step,   # legacy alias, same value
        "response_shape": shape,
        "raw": data or {},
    }


def self_attestation_safe(normalized: dict) -> dict:
    """Redacted view safe to persist or log - never contains the presigned URL."""
    n = normalized or {}
    return {
        "utility_account_number": n.get("utility_account_number"),
        "document_available": bool(n.get("document_available")),
    }


def normalize_contracts_response(data: dict) -> dict:
    """Normalize the POST /contracts response while preserving the raw body.

    Spec (GetContractsResponse), both fields `required`:
        {"contract_urls": [{"contract_name","url","expires_at"}, ...],
         "next_step": "<url>"}

    PRESIGNED URL HANDLING - the spec is explicit:
        "Do not log URLs in application logs"
        "Do not store or cache these URLs in your own systems beyond their TTL"

    So the normalized view deliberately does NOT carry `url`. Each contract is
    exposed as {contract_name, expires_at, url_present} - everything needed for
    diagnostics, audit, and UI, with none of the secret. The presigned URLs stay
    only inside `raw`, which callers must treat as ephemeral: use it to download
    immediately, never persist it, never log it.

    contracts_safe() below is the accessor that guarantees redaction; prefer it
    over touching `raw` unless you are actually performing the download.
    """
    data = data or {}
    next_step, _shape = extract_next_step(data)
    raw_contracts = data.get("contract_urls")
    if not isinstance(raw_contracts, list):
        raw_contracts = []

    safe = []
    for position, item in enumerate(raw_contracts):
        if not isinstance(item, dict):
            # Malformed entry - record its presence without inventing fields.
            safe.append({"index": position, "contract_name": None, "expires_at": None,
                         "url_present": False, "malformed": True})
            continue
        url = item.get("url")
        safe.append({
            # AUTHORITATIVE ORDERING. Perch's contract_urls array carries no id,
            # so position IS the identity. Emitting it explicitly means the
            # frontend never has to infer an index from its own rendering order
            # or from a display name, and POST /contracts/review receives the
            # exact position Perch returned.
            "index": position,
            "contract_name": item.get("contract_name"),
            "expires_at": item.get("expires_at"),
            # Structural check only. The value never leaves `raw`.
            "url_present": bool(url) and isinstance(url, str) and url.strip() != "",
        })

    return {
        "contracts": safe,
        "contract_count": len(safe),
        "next_step": next_step,
        "response_shape": _shape,
        "contract_urls_present": "contract_urls" in data,
        "raw": data,
    }


def contracts_safe(normalized: dict) -> list:
    """Redacted contract list: never contains a presigned URL.

    Use this for logging, printing, audit records, and anything persisted.
    """
    return [dict(c) for c in (normalized or {}).get("contracts", [])]


def redact_contract_urls(data):
    """Returns a deep copy of a /contracts body with every presigned URL replaced.

    For the rare case where the raw body must be recorded (a support ticket, a
    diagnostic dump) - the shape survives, the secret does not.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    items = out.get("contract_urls")
    if isinstance(items, list):
        redacted = []
        for item in items:
            if isinstance(item, dict):
                copy = dict(item)
                if "url" in copy:
                    copy["url"] = "[REDACTED_PRESIGNED_URL]"
                redacted.append(copy)
            else:
                redacted.append(item)
        out["contract_urls"] = redacted
    return out


# ─────────────── Contract acceptance timestamp ───────────────
# Perch rejects a timestamp it considers to be in the future:
#   422 {"error":"unprocessable_entity","message":"Metadata timestamp cannot be in the future"}
# observed live on 2026-08-14 with a timestamp generated at the exact instant of
# submission (2026-08-14 15:29:47.6065). Perch parsed it and applied the
# not-in-the-future rule, so the FORMAT is accepted - only the value was wrong.
#
# Cause: ordinary clock skew between the Dalton host and Perch's server, plus
# network latency, means "now" can land marginally ahead of Perch's clock.
#
# Fix: emit a timestamp a few seconds in the past. 10s comfortably absorbs
# typical NTP skew while remaining far inside Perch's documented 24-hour
# maximum age, so the value is still an honest record of when the customer
# agreed (they clicked moments earlier, not 10 seconds in the future).
# ─────────────── Acceptance timestamp timezone ───────────────
# Perch's acceptance timestamp format carries NO timezone:
#     "YYYY-MM-DD HH:MM:SS.ffff"
# so Perch must interpret it in SOME zone. Live evidence (2026-08-17) proves it
# is EASTERN wall-clock, not UTC:
#
#     Dalton sent: 2026-08-17 04:01:22.1941   (UTC)
#     Eastern now: 2026-08-17 00:02:22        -> Perch saw ~4 hours in ITS future
#                                             -> 422 "timestamp cannot be in the future"
#
# This is why the original datetime.now() worked from a Windows box in Eastern
# and failed the moment the same code ran on Render (UTC). The value was never
# a clock-skew problem; it was a wall-clock/timezone problem.
#
# Use an EXPLICIT IANA zone, never the host's local timezone, so the value is
# identical whether Dalton runs on a laptop in New York, on Render in UTC, or
# anywhere else. ZoneInfo also handles EST/EDT automatically - a hardcoded -4
# would be wrong for roughly half the year.
ACCEPTANCE_TIMEZONE_NAME = os.environ.get(
    "PERCH_ACCEPTANCE_TIMEZONE") or "America/New_York"


def acceptance_timezone():
    """The IANA zone Perch interprets acceptance timestamps in.

    Raises a clear configuration error rather than silently falling back to UTC
    or to a hardcoded offset - either would reintroduce the exact 422 this fixes.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(ACCEPTANCE_TIMEZONE_NAME)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise RuntimeError(
            f"Timezone database unavailable for {ACCEPTANCE_TIMEZONE_NAME!r}, so the "
            f"Perch acceptance timestamp cannot be generated correctly. Install the "
            f"system tzdata (or `pip install tzdata`) on this host. Falling back to "
            f"UTC or a fixed offset would send a timestamp Perch reads as being in "
            f"the future.") from exc


# The clock-skew allowance is SEPARATE from the timezone question above and
# stays small: it absorbs NTP jitter between two servers, nothing more. 60s was
# only ever a diagnostic override while we were chasing the real cause.
DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS = 30

# Hard ceiling. A larger offset would hide a real clock problem rather than
# absorb jitter, and would make the acceptance record dishonest.
MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS = 120
MIN_ACCEPTANCE_CLOCK_SKEW_SECONDS = 0


def acceptance_clock_skew_seconds():
    """Skew allowance, overridable via PERCH_ACCEPTANCE_SKEW_SECONDS.

    Clamped to [0, 120]. An unparseable or out-of-range value falls back to the
    default rather than failing the acceptance, because a malformed env var must
    never block a customer from completing an enrollment.
    """
    raw = os.environ.get("PERCH_ACCEPTANCE_SKEW_SECONDS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS
    if value < MIN_ACCEPTANCE_CLOCK_SKEW_SECONDS:
        return MIN_ACCEPTANCE_CLOCK_SKEW_SECONDS
    if value > MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS:
        return MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS
    return value


# Kept for callers/tests that reference the historical name.
ACCEPTANCE_CLOCK_SKEW_SECONDS = DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS

# Format matches the spec's own example ("2026-07-03 15:20:36.6717"): space
# separated, no timezone, microseconds truncated to 4 digits. Confirmed
# parseable by staging - do not change without evidence.
ACCEPTANCE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def acceptance_timestamp(now=None):
    """Return the timestamp string for POST /contracts/accept.

    Single source of truth: the Dalton route and the staging verifier both call
    this, so the verifier provably exercises production behavior rather than a
    near-copy that can drift.
    """
    # EASTERN WALL-CLOCK, from an explicit IANA zone - never the host's local
    # timezone and never UTC. See ACCEPTANCE_TIMEZONE_NAME above for the live
    # evidence that Perch reads this field as Eastern.
    tz = acceptance_timezone()
    if now is None:
        aware = datetime.now(tz)
    elif now.tzinfo is not None:
        # A caller-supplied aware datetime is CONVERTED to the acceptance zone.
        aware = now.astimezone(tz)
    else:
        # A naive datetime from a caller (tests) is taken as already being
        # wall-clock in the acceptance zone.
        aware = now.replace(tzinfo=tz)
    stamped = aware - timedelta(seconds=acceptance_clock_skew_seconds())
    # tzinfo is dropped here, so strftime emits NO offset and NO "Z" - the wire
    # format is unchanged: "YYYY-MM-DD HH:MM:SS.ffff", 4-digit microseconds.
    return stamped.replace(tzinfo=None).strftime(ACCEPTANCE_TIMESTAMP_FORMAT)[:-2]


def normalize_accept_response(data: dict) -> dict:
    """Normalize the POST /contracts/accept response.

    Spec (AcceptContractsResponse): {"message": "..."} with `message` required.
    Deliberately NO next_step key - the spec states "No `next_step` is returned
    - the enrollment is complete."
    """
    data = data or {}
    return {"message": data.get("message"), "raw": data}


def normalize_status_response(data: dict) -> dict:
    """Normalize the GET /status response.

    Spec (enrollmentStatusResponse) requires completed_steps, remaining_steps,
    completed and next_step. next_step is nullable and becomes null once the
    enrollment is complete, so a missing/None value is meaningful, not an error.

    next_step/next_step_url alias tolerance matches every other endpoint - real
    staging has used the *_url spelling on /token, /capacity, /enroll,
    /lmi/proof_docs and /contracts.
    """
    data = data or {}
    next_step, _shape = extract_next_step(data)
    completed_steps = data.get("completed_steps")
    remaining_steps = data.get("remaining_steps")
    return {
        "completed_steps": completed_steps if isinstance(completed_steps, list) else [],
        "remaining_steps": remaining_steps if isinstance(remaining_steps, list) else [],
        # Only a literal True counts as complete - never coerce a truthy value.
        "completed": data.get("completed") is True,
        "next_step": next_step,
        "response_shape": _shape,
        "raw": data,
    }


def normalize_proof_docs_response(data: dict) -> dict:
    """Normalize proof-doc success response while preserving the raw body."""
    data = data or {}
    next_step, _shape = extract_next_step(data)
    return {
        "next_step": next_step,
        "response_shape": _shape,
        "raw": data,
    }

def normalize_enroll_response(data: dict) -> dict:
    """Normalizes the POST /enroll response.

    Staging has used `*_url` aliases on both /token and /capacity, so the same
    tolerance is applied here rather than assuming /enroll differs.
    `next_step` drives which endpoint comes next:
        non-LMI            -> /contracts
        LMI IRA            -> /lmi/proof_docs
        LMI self-attest    -> /lmi/self_attestation
    """
    data = data or {}
    next_step, _shape = extract_next_step(data)
    return {
        "next_step": next_step,
        "response_shape": _shape,
        "raw": data,
    }


# ─────────────── Defensive response parsing ───────────────
# Perch returned a sub-400 status with a non-JSON body during live staging use
# (2026-08). resp.json() was called unguarded on every SUCCESS path, so the
# JSONDecodeError escaped as an uncaught Flask 500 - and because it was not a
# PerchError, the adapter's `except PerchError` never fired and NOTHING was
# recorded in perch_api_calls. We had no status, no body, no request id.
#
# Every success path now goes through parse_json_response(). Parse failures
# become TYPED Perch errors so existing adapter logging catches them naturally;
# genuine programming bugs still propagate loudly rather than being mislabelled
# as API failures.

_MAX_DIAGNOSTIC_BODY = 2000

# Sentinel: distinguishes "parsed to None" from "could not parse".
_UNPARSEABLE = object()

# Anything that looks like credential or presigned-URL material is scrubbed
# before a body is ever attached to an exception, logged, or persisted.
_REDACTION_PATTERNS = [
    (re.compile(r"(X-Amz-Signature=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(X-Amz-Credential=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(X-Amz-Security-Token=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"https://[^\s\"']*amazonaws\.com[^\s\"']*", re.I), "[REDACTED_PRESIGNED_URL]"),
    (re.compile(r"(\"?enrollment_token\"?\s*[:=]\s*\"?)[A-Za-z0-9\-]{8,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"(\"?(?:api_key|secret|signature)\"?\s*[:=]\s*\"?)[^\s\"',}]+", re.I), r"\1[REDACTED]"),
]


def redact_body(text):
    """Scrub credential/presigned material from a response body before it is
    attached to an exception, logged, or persisted."""
    if not text:
        return text
    out = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _safe_body_excerpt(resp):
    try:
        return redact_body((resp.text or ""))[:_MAX_DIAGNOSTIC_BODY]
    except Exception:
        return None


def _request_id_of(resp):
    for header in ("X-Request-Id", "X-Request-ID", "x-request-id",
                   "X-Amzn-RequestId", "x-amzn-requestid", "X-Amzn-Trace-Id",
                   "Request-Id", "CF-RAY"):
        try:
            val = resp.headers.get(header)
        except Exception:
            val = None
        if val:
            return f"{header}: {val}"
    return None


def parse_json_response(resp, path, ambiguous_on_failure=False):
    """Parse a sub-400 Perch response that is expected to be JSON.

    On success: returns the decoded object.

    On an empty body, a non-JSON Content-Type, or malformed JSON, raises a TYPED
    Perch error carrying status, Content-Type, request id and a redacted body
    excerpt.

    ambiguous_on_failure=True is used ONLY for state-changing, non-idempotent
    operations (POST /enroll, POST /contracts/accept). Perch returned a success
    status, so the operation may well have been performed - the caller must NOT
    retry, and must reconcile via GET /status instead.

    Everything else raises PerchUnavailableError, which is safe to retry.
    """
    # PARSE FIRST. A readable JSON body is always accepted, regardless of what
    # other attributes the response object exposes - diagnostics are only
    # gathered on the failure path. (An earlier version inspected .text before
    # parsing, which rejected perfectly valid responses whose object did not
    # expose every requests.Response attribute.)
    try:
        parsed = resp.json()
    except Exception:
        parsed = _UNPARSEABLE
    if parsed is not _UNPARSEABLE and parsed is not None:
        return parsed

    status = getattr(resp, "status_code", None)
    try:
        content_type = resp.headers.get("Content-Type") or ""
    except Exception:
        content_type = ""
    request_id = _request_id_of(resp)
    excerpt = _safe_body_excerpt(resp)

    def _fail(reason):
        detail = (f"Perch {path} returned HTTP {status} with {reason}. "
                  f"Content-Type: {content_type or '(none)'}. "
                  f"Request id: {request_id or '(none)'}. "
                  f"Body excerpt: {excerpt!r}")
        if ambiguous_on_failure:
            exc = PerchAmbiguousOutcomeError(
                f"{detail} Perch reported success but the response could not be read, "
                f"so this operation MAY have been performed. Do not retry - "
                f"reconcile with GET /status first.")
        else:
            exc = PerchUnavailableError(
                f"{detail} This endpoint is safe to retry.")
        raise attach_response_diagnostics(exc, resp)

    # Classify WHY it could not be read, for an actionable message.
    if status == 204 or not (excerpt or "").strip():
        _fail("an empty body")
    if content_type and "json" not in content_type.lower():
        _fail(f"a non-JSON body ({content_type})")
    _fail("a body that is not valid JSON")


def attach_response_diagnostics(exc, resp):
    """Attaches the raw response to an exception for diagnostics, then returns it.

    DIAGNOSTIC ONLY - this changes no request, no control flow, and no error
    message. It exists because _msg() collapses a response into one string and
    the response object is otherwise discarded when we raise, leaving callers
    unable to see what Perch actually said.

    Known gap this exposes: _msg() assumes the standard {error, message}
    envelope, but POST /enroll returns {"errors": [{field, message}, ...]}
    (ValidationErrorsResponse). Against that shape _msg() yields the useless
    string "None: None". Left as-is deliberately - fix the message formatting
    only after the real staging body has been captured.

    Nothing sensitive is attached: only the response Perch sent us. Request
    headers (which carry our credentials) are never touched.
    """
    exc.status_code = getattr(resp, "status_code", None)
    try:
        exc.content_type = resp.headers.get("Content-Type")
    except Exception:
        exc.content_type = None

    # Correlation IDs, if the gateway or app supplies one. An allowlist rather
    # than all headers, so nothing unexpected (cookies, auth echoes) is printed.
    exc.request_id = None
    for header in ("X-Request-Id", "X-Request-ID", "x-request-id",
                   "X-Amzn-RequestId", "x-amzn-requestid", "X-Amzn-Trace-Id",
                   "Request-Id", "CF-RAY"):
        try:
            val = resp.headers.get(header)
        except Exception:
            val = None
        if val:
            exc.request_id = f"{header}: {val}"
            break

    try:
        exc.body_text = redact_body(resp.text)
    except Exception:
        exc.body_text = None
    try:
        exc.body_json = resp.json()
    except Exception:
        exc.body_json = None
    return exc


# The v2 error codes the spec defines, plus the shape they all share:
# lower_snake_case, no spaces, no punctuation. Matching the SHAPE rather than
# a fixed list means a code Perch adds later is still recognised as a code;
# matching a list alone would let a new one through into a rep's face.
_V2_ERROR_CODES = frozenset({
    "bad_request", "unauthorized", "forbidden", "not_found", "conflict",
    "unprocessable_entity", "too_many_requests", "internal_server_error",
    "service_unavailable",
})


def _looks_like_machine_code(value):
    """True when `error` is a code rather than a sentence.

    A v1 `error` holds prose - "24112 is not valid for the given utility" -
    which has spaces and usually punctuation. A v2 `error` is a token like
    `unprocessable_entity`. Anything with whitespace is prose; anything that
    is a short lower_snake_case word is a code.
    """
    if not isinstance(value, str):
        return False
    token = value.strip()
    if not token or any(ch.isspace() for ch in token):
        return False
    if token in _V2_ERROR_CODES:
        return True
    # Shape test for codes not in the published list yet.
    return (len(token) <= 48
            and token == token.lower()
            and all(ch.isalnum() or ch == "_" for ch in token)
            and "_" in token)


# What to say when a machine code arrives with NO human message beside it.
# v2 is not obliged to send `message`, and a bare {"error": "not_found"} would
# otherwise put the token itself in front of a rep.
_V2_CODE_PROSE = {
    "bad_request": "Perch could not read the request.",
    "unauthorized": "Perch did not accept the credentials for this request.",
    "forbidden": "Perch declined this request for this partner account.",
    "not_found": "Perch has no record matching this request.",
    # Deliberately "competing" and not "conflicting": the suite asserts that no
    # code token appears in its own prose, and "conflicting" contains
    # "conflict". A real word collision would blunt that assertion for every
    # other code too.
    "conflict": "Perch already has a competing record for this request.",
    "unprocessable_entity": "Perch rejected the details on this request.",
    "too_many_requests": "Perch is rate-limiting requests right now.",
    "internal_server_error": "Perch had an internal error handling this request.",
    "service_unavailable": "Perch is temporarily unavailable.",
}


def _code_prose(token):
    """A sentence for a code that came with no message.

    A code Perch adds later has no entry here, so it is softened into words
    rather than printed as an identifier: `lmi_docs_required` reads as
    "Perch rejected this request (lmi docs required)." Not elegant, but it is
    a sentence, and no rep is left staring at a JSON token. The raw code is
    still in the attached body for the audit record either way."""
    token = (token or "").strip()
    known = _V2_CODE_PROSE.get(token)
    if known:
        return known
    words = token.replace("_", " ").strip()
    return f"Perch rejected this request ({words})." if words else ""


# ─────────────── Field-level 422 errors, in plain language ───────────────
#
# E3. Perch's ValidationErrorsResponse carries, per field, a machine `code`
# and a `message` written for an integrator rather than for a rep. Both are
# a problem in front of a salesperson:
#
#   code     `capacity_unavailable`, `required`, `invalid`, `not_found` -
#            internal tokens, exactly what C3 ruled out for Perch's own
#            wording and what E1 removed from the other two envelopes.
#   message  "Contact Perch Energy and fetch capacity using the /capacity
#            endpoint" is an instruction to US, not to the rep. So is "Fetch
#            the available savings percents using the /v2/enrollments/capacity
#            endpoint". A rep cannot act on either, and reading a partner's
#            endpoint names to a customer on the phone is worse than useless.
#
# So a known (field, code) pair is rewritten into a sentence a rep can act on,
# and the raw body is still attached by attach_response_diagnostics() for the
# audit record. An UNKNOWN pair falls back to Perch's own message with the
# code dropped and any endpoint instruction trimmed - new validations keep
# working without a release, and still never print a token.
#
# Keys are (field, code). A field with no code-specific entry falls through to
# (field, None), then to the generic path.
_FIELD_ERROR_TEXT = {
    ("savings_percent", "required"):
        "Choose the savings rate for this customer before submitting.",
    ("savings_percent", "invalid"):
        "That savings rate is not available for this customer. "
        "Re-check availability and choose one of the rates shown.",
    ("county", "not_found"):
        "That county isn't recognized for this state. Please check the "
        "selected county and try again.",
    ("customer_type", "capacity_unavailable"):
        "The selected savings program no longer has capacity for this "
        "address. Re-check availability and choose from what is offered.",
    # Perch sends both of the above AND the multi-account refusal below under
    # the SAME code. See _FIELD_ERROR_BY_MESSAGE.
    ("customer_type", "invalid"):
        "The selected savings program isn't valid for this enrollment. "
        "Re-check availability and choose again.",
    ("zip_code", "not_in_service_area"):
        "This ZIP code is outside the service area for the selected utility.",
    ("utility_name", "invalid"):
        "That utility is not valid for this enrollment.",
    ("email_address", "already_exists"):
        "An enrollment already exists for this email address.",
    ("phone_number", "invalid_format"):
        "Enter a 10-digit phone number with no country code.",
}

# ONE CODE, TWO MEANINGS. Perch returns `capacity_unavailable` on
# customer_type both when the program genuinely has no capacity AND when an
# LMI enrollment carries more than one utility account - which is not a
# capacity problem at all and has a completely different fix. The code cannot
# tell them apart, so these match on a distinctive fragment of the message and
# are checked BEFORE the (field, code) table.
#
# Matching on message text is ordinarily a bad idea, and it is used here only
# because the alternative is telling a rep to re-check availability over a
# problem that has nothing to do with availability. A wording change on
# Perch's side costs the specific sentence and falls back to the generic one.
_FIELD_ERROR_BY_MESSAGE = (
    ("only one utility account",
     "The income-qualified program allows only one utility account per "
     "enrollment. Remove the extra account, or choose the standard "
     "residential program."),
)

# A sentence per CODE, for a field with no entry of its own. This is the tier
# that keeps a message like "is required for Business customer type" - which
# is Perch's wording, and is the word "required" doing double duty as both the
# code and the prose - from being echoed at a rep as a sentence fragment
# hanging off a label. `{label}` is the rep-facing field name.
_CODE_DEFAULT_TEXT = {
    "required": "{label} is required for this enrollment.",
    "invalid": "{label} is not valid for this enrollment.",
    "invalid_format": "{label} is not in the format this enrollment needs.",
    "invalid_characters": "{label} contains characters that are not allowed.",
    "not_found": "{label} was not recognised.",
    "empty": "{label} cannot be blank.",
    "duplicate": "{label} was supplied more than once.",
    "already_exists": "{label} is already in use on another enrollment.",
    "blocklisted": "{label} cannot be used for this enrollment.",
    "missing_file": "{label} is missing its file.",
    "invalid_file_type": "{label} must be a PDF.",
    "file_too_large": "{label} is too large to upload.",
    "too_many_files": "{label} has too many files attached.",
    "too_many_accounts": "{label} has more accounts than this program allows.",
    "not_in_service_area": "{label} is outside the service area.",
}

# Field names as a rep would say them. Used for the fallback path, so even an
# unmapped error reads as words rather than as a JSON key.
_FIELD_LABELS = {
    "savings_percent": "Savings rate",
    "county": "County",
    "customer_type": "Savings program",
    "utility_name": "Utility",
    "zip_code": "ZIP code",
    "email_address": "Email address",
    "phone_number": "Phone number",
    "utility_accounts": "Utility account",
    "business_name": "Business name",
    "business_title": "Business title",
    "business_phone": "Business phone",
    "home_address": "Home address",
    "billing_address": "Billing address",
}

# Perch's integrator-facing instructions, stripped from any message that
# reaches a rep. Matched case-insensitively from the first occurrence to the
# end of the sentence.
_INTEGRATOR_INSTRUCTIONS = (
    "contact perch energy",
    "fetch the available savings percents",
    "fetch capacity using",
    "using the /",
)


def _field_label(field):
    """A rep-facing name for a Perch field key."""
    key = str(field or "").strip()
    known = _FIELD_LABELS.get(key)
    if known:
        return known
    # Strip any array indexing - `utility_accounts[0][meter_numbers][1]` is
    # "Utility account" to a rep, not a path expression.
    base = key.split("[")[0]
    known = _FIELD_LABELS.get(base)
    if known:
        return known
    words = base.replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "This enrollment"


def _strip_integrator_instructions(text):
    """Remove the parts of a Perch message addressed to the integrator."""
    cleaned = str(text or "").strip()
    lowered = cleaned.lower()
    cut = len(cleaned)
    for marker in _INTEGRATOR_INSTRUCTIONS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    cleaned = cleaned[:cut].strip()
    # A message that was ONLY an instruction leaves nothing; the caller
    # substitutes a generic sentence rather than printing an empty string.
    return cleaned.rstrip(" .,;:").strip()


def _field_error_text(item):
    """One field-level 422 error, as a rep should read it.

    Never prints the machine code. Never prints Perch's endpoint names.
    """
    field = item.get("field") or item.get("name") or ""
    code = (item.get("code") or "").strip()
    raw_message = item.get("message") or item.get("detail") or ""
    acct = item.get("utility_account_number")

    lowered_message = str(raw_message).lower()
    text = next((sentence for marker, sentence in _FIELD_ERROR_BY_MESSAGE
                 if marker in lowered_message), None)

    if not text:
        text = (_FIELD_ERROR_TEXT.get((field, code))
                or _FIELD_ERROR_TEXT.get((field, None)))

    label = _field_label(field)

    if not text and code in _CODE_DEFAULT_TEXT:
        # A code UBS knows, on a field it has no specific wording for.
        text = _CODE_DEFAULT_TEXT[code].format(label=label)

    if not text:
        # Neither mapped: Perch's own wording, cleaned. "must be 10 digits"
        # becomes "Phone number: must be 10 digits." - a sentence fragment,
        # but a readable one, and it carries no token. This is the tier a
        # validation Perch adds tomorrow lands in, which is why it exists.
        cleaned = _strip_integrator_instructions(raw_message)
        if cleaned:
            text = f"{label}: {cleaned}."
        else:
            text = f"{label} was not accepted. Re-check it and try again."

    if acct:
        # Which account, when Perch scopes an error to one of several. The
        # number itself is what a rep looks up, so it stays.
        text += f" (account {acct})"
    return text


def _msg(resp):
    """Formats a Perch error body into a readable message.

    Perch uses THREE error envelopes, all now observed live:

      1. v1 ErrorResponse         {"error": "<human sentence>"}
      2. v2 ErrorResponse         {"error": "<machine_code>",
                                   "message": "<human sentence>"}
      3. ValidationErrorsResponse {"errors": [{"field","code","message"}, ...]}

    (1) and (2) are the SAME JSON shape meaning different things; see the
    comment at the error/message branch below. The original implementation
    only handled (1), so a real /enroll 422 in envelope (3) rendered as the
    useless string "None: None". All three are handled now. The full response
    is still attached to the exception by attach_response_diagnostics()
    regardless of shape, so nothing dropped from the text is lost to the audit
    record.
    """
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:200]

    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for item in errors:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                parts.append(_field_error_text(item))
            return "; ".join(parts)

        if body.get("error") is not None or body.get("message") is not None:
            # Join only the parts that are actually present.
            # This used to be an unconditional f"{error}: {message}", which
            # rendered a body carrying only `error` as
            #     "24112 is not valid for the given utility: None"
            # The trailing ": None" is this formatter printing an absent
            # field, but it reads exactly like a null value WE sent - and it
            # caused a real capacity rejection to be misread as "the utility
            # slug never reached the request".
            #
            # E1 - THE SAME ENVELOPE MEANS TWO DIFFERENT THINGS.
            #
            #   v1: {"error": "24112 is not valid for the given utility"}
            #       `error` IS the human sentence. There is no `message`.
            #   v2: {"error": "unprocessable_entity",
            #        "message": "Email is not valid"}
            #       `error` is a MACHINE CODE and `message` is the sentence.
            #
            # Joining both, as this did, produced "unprocessable_entity: Email
            # is not valid" on v2 - an internal token in front of a rep, which
            # is exactly what C3 ruled out for Perch's own wording. So when a
            # machine code sits beside a human message, the message wins and
            # the code is dropped from the text. The code is not lost: the full
            # body is still attached by attach_response_diagnostics() for the
            # audit record and the logs.
            # A bare {"error": "<code>"} with no message gets a sentence too -
            # v2 is not obliged to send `message`, and the token alone would
            # land in front of a rep unaltered.
            error_val = body.get("error")
            message_val = body.get("message")
            if _looks_like_machine_code(error_val):
                if message_val is not None:
                    return str(message_val)
                return _code_prose(error_val)
            parts = [str(body[k]) for k in ("error", "message")
                     if body.get(k) is not None]
            return ": ".join(parts)

    return (resp.text or "")[:200]

# Documented path suffixes, relative to the enrollment base URL.
PATH_TOKEN = "/token"
# NEW YAML: POST /token returns 201 Created (previous spec said 200).
TOKEN_CREATED_STATUS = 201
PATH_REFRESH_TOKEN = "/refresh_token"
PATH_CAPACITY = "/capacity"
PATH_ENROLL = "/enroll"          # Milestone 3
PATH_LMI_PROOF_DOCS = "/lmi/proof_docs"
PATH_LMI_SELF_ATTESTATION = "/lmi/self_attestation"
PATH_LMI_SELF_ATTESTATION_ACCEPT = "/lmi/self_attestation/accept"
PATH_CONTRACTS = "/contracts"
PATH_STATUS = "/status"          # Milestone 6
PATH_CONTRACTS_ACCEPT = "/contracts/accept"
PATH_MARKETS_CAPACITY = "/capacity"   # relative to the MARKETS base URL

# Market-rules path suffixes, also relative to the MARKETS base URL.
#
# Spec, "Market-Specific Rules": "Rules that differ by state or utility -
# utility slugs, account formats, proof document types, self-attestation
# options, counties and income limits - are not listed in this document. Fetch
# them from the GET /markets/* endpoints."
#
# So these are the ONLY sanctioned source for per-market values. Nothing here
# or downstream hardcodes a utility slug, an account-number pattern or a
# county name.
PATH_MARKETS_UTILITIES = "/utilities"
PATH_MARKETS_UTILITY_ACCOUNT_FIELDS = "/utility_account_fields"
PATH_MARKETS_COUNTIES = "/counties"
PATH_MARKETS_COUNTY_INCOME_LIMITS = "/counties/income_limits"
PATH_MARKETS_SELF_ATTESTATION_OPTIONS = "/self_attestation_options"

# NEW YAML: "Expires 1 hour after issuance (see `expires_at` on the token
# response)". The previous spec said 30 minutes; the engineering call predated
# the published spec. expires_at on the response is still authoritative - this
# constant is only the fallback when the field is absent.
TOKEN_TTL_SECONDS = 60 * 60

ENROLLMENT_TOKEN_HEADER = "X-Enrollment-Token"


class PerchClient(ABC):
    """Interface satisfied by both PerchMockClient and PerchHTTPClient."""

    mode = "abstract"

    @abstractmethod
    def request_token(self, email: str) -> dict:
        """POST /token - generate an enrollment token.

        Spec: HMAC-authenticated. Body is {"email": "<customer email>"}.
        Returns: {"enrollment_token", "expires_at", "next_step", "raw"}
        Raises: PerchAuthError, PerchValidationError (422), PerchUnavailableError
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_token(self, email: str) -> dict:
        """PATCH /refresh_token - new token for the most recent IN-PROGRESS
        enrollment for this email.

        Spec: HMAC-authenticated (NOT enrollment-token authenticated), body is
        {"email": ...}. Also the documented way to RESUME an interrupted flow -
        the response's next_step says where to continue.
        Returns: {"enrollment_token", "expires_at", "next_step", "raw"}
        Raises: PerchAuthError, PerchNotFoundError (404), PerchUnavailableError
        """
        raise NotImplementedError

    @abstractmethod
    def check_capacity(self, enrollment_token: str, zip_code: str, utility_slug: str) -> dict:
        """POST /capacity — authoritative capacity and savings for this session.

        Documented request body:
            {"zip_code": "10001", "utility_name": "consolidated-edison-ny"}
        Documented 200 response:
            {"project_details": {...six fields...}, "next_step": "<url>"}

        Raises:
            PerchTokenExpiredError  on 403 (caller refreshes and retries once)
            PerchNoCapacityError    on 503 (business outcome - no open capacity)
            PerchValidationError    on other 4xx
            PerchUnavailableError   on 5xx other than 503, or transport failure
        """
        raise NotImplementedError

    def create_enrollment(self, enrollment_token: str, form: dict, files: dict) -> dict:
        """POST /enroll - create the enrollment.

        `form` and `files` come from build_enrollment_multipart(), which encodes
        the exact indexed field names the spec requires.

        Returns the normalized response (see normalize_enroll_response).
        """
        raise PerchNotImplementedError("Not implemented by this client.")

    def submit_proof_docs(self, enrollment_token: str, files: dict) -> dict:
        """POST /lmi/proof_docs - submit LMI proof documents for this session."""
        raise PerchNotImplementedError("Not implemented by this client.")

    def submit_self_attestation(self, enrollment_token: str, entry: dict) -> dict:
        """POST /lmi/self_attestation - generate the official attestation document.

        Spec: JSON body {"self_attestation": {utility_account_number, occupancy,
        county, lmi_source_selection, status}}. Returns a PRESIGNED document URL
        (1 hour) plus next_step -> /lmi/self_attestation/accept.
        """
        raise PerchNotImplementedError("Not implemented by this client.")

    def accept_self_attestation(self, enrollment_token: str, metadata: dict) -> dict:
        """POST /lmi/self_attestation/accept - record the customer's acceptance.

        Spec: JSON body {"self_attestation": {timestamp, ip_address, user_agent}}.
        NO utility_account_number - the token already scopes it. Returns 202 and
        next_step -> /contracts.
        """
        raise PerchNotImplementedError("Not implemented by this client.")

    def generate_contracts(self, enrollment_token: str) -> dict:
        """POST /contracts - generate personalised contract documents.

        Spec: X-Enrollment-Token auth, NO request body. Perch generates and
        personalises the contracts internally and returns presigned S3 URLs
        valid for 1 hour.

        Returns the normalized response (see normalize_contracts_response),
        whose `contracts` list is URL-free by design.
        """
        raise PerchNotImplementedError("Not implemented by this client.")

    def accept_contracts(self, enrollment_token: str, metadata: dict) -> dict:
        """POST /contracts/accept - record acceptance of the WHOLE packet.

        `metadata` must contain exactly ip_address, timestamp, user_agent. The
        spec requires no contract list, no acknowledgment flag, no files.
        """
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_status(self, enrollment_token: str) -> dict:
        """GET /status - which steps are done and which remain. Side-effect free."""
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_market_capacity(self, zip_code: str, utility_slug: str) -> dict:
        """GET /markets/capacity - pre-enrollment targeting before a signup exists.
        HMAC-authenticated, signed over the canonical query string."""
        raise PerchNotImplementedError("Not implemented by this client.")

    # ─────────────── Market rules (HMAC, pre-enrollment, read-only) ───────────────
    # Not part of the enrollment flow: no X-Enrollment-Token, no session, no
    # side effects. Each returns Perch's parsed body verbatim - interpretation
    # belongs to the caller, not here.

    def get_market_utilities(self, state: str) -> dict:
        """GET /markets/utilities?state= -> {"state", "utilities": [slug, ...]}"""
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_market_utility_account_fields(self, utility_slug: str) -> dict:
        """GET /markets/utility_account_fields?utility= ->
        {"utility_name", "fields": [{field_type, field_name, help_text, patterns}]}"""
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_market_counties(self, state: str) -> dict:
        """GET /markets/counties?state= -> {"state", "counties": [name, ...]}"""
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_market_county_income_limits(self, state: str, county: str) -> dict:
        """GET /markets/counties/income_limits?county=&state= ->
        {"state", "county", "income_limits": [{house_occupancy, household_income}]}"""
        raise PerchNotImplementedError("Not implemented by this client.")

    def get_market_self_attestation_options(self, state: str) -> dict:
        """GET /markets/self_attestation_options?state= ->
        {"state", "source_types", "required_source_types", "single_source_type",
         "lmi_programs", "programs", "status"}"""
        raise PerchNotImplementedError("Not implemented by this client.")


class PerchHTTPClient(PerchClient):
    """Real Perch API client, written to the published contract.

    NOT yet exercised against Perch staging - credentials were promised but not
    yet issued at the time of writing. Paths, headers, parameter names, and
    status-code handling all come from the published docs, so the surface area
    for surprises is far smaller than the Milestone 1 version, but it remains
    unverified against a live endpoint.
    """

    mode = "live"

    def __init__(self, enrollment_base_url, markets_base_url, api_key, secret_key="",
                 timeout=20, markets_capacity_base_url=None, api_version="v1"):
        self.enrollment_base_url = (enrollment_base_url or "").rstrip("/")
        self.markets_base_url = (markets_base_url or "").rstrip("/")
        # Market CAPACITY moves to /v2/markets in v2 while the other market
        # lookups stay on /v1/markets, so it gets its own base. Defaults to the
        # lookup base, which is exactly the v1 arrangement - an existing caller
        # that passes neither new argument behaves as it always did.
        self.markets_capacity_base_url = (
            markets_capacity_base_url or markets_base_url or "").rstrip("/")
        # Recorded for diagnostics and for the audit trail. The client does not
        # branch on it: the base URLs already carry the version.
        self.api_version = (api_version or "v1").strip().lower()
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout = timeout

    def _requests(self):
        try:
            import requests
            return requests
        except ImportError as e:
            raise PerchUnavailableError(
                "The 'requests' library is required for live Perch calls. Install it "
                "before setting PERCH_API_MODE=live."
            ) from e

    def _session_headers(self, enrollment_token):
        """Enrollment-session endpoints: X-Enrollment-Token ONLY.

        Spec is explicit that these calls must NOT send HMAC headers -
        "no HMAC headers are needed for these calls".
        """
        return {"Content-Type": "application/json", ENROLLMENT_TOKEN_HEADER: enrollment_token}

    def _post(self, url, payload, enrollment_token=None):
        requests = self._requests()
        try:
            return requests.post(url, json=payload,
                                 headers=self._session_headers(enrollment_token),
                                 timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

    def request_token(self, email: str) -> dict:
        """POST /token - HMAC-authenticated, body {"email": ...}."""
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_TOKEN}"
        headers, body = hmac_auth.sign_json_request(self.api_key, self.secret_key, {"email": email})
        try:
            # data=body (not json=) so the EXACT bytes we signed are transmitted.
            # Re-serializing would change the signature and fail authentication.
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e
        # NEW YAML: 422 on /token now covers three distinct cases. Two of them
        # ("Email has already been taken" / "An enrollment request already
        # exists") are recoverable via PATCH /refresh_token, so they get their
        # own error type rather than being lumped in with a validation failure.
        if resp.status_code == 422 and _is_enrollment_in_progress(resp):
            raise PerchEnrollmentInProgressError(_msg(resp))
        self._raise_for_hmac_status(resp, PATH_TOKEN)
        return self._parse_token_response(resp)

    def refresh_token(self, email: str) -> dict:
        """PATCH /refresh_token - HMAC-authenticated, body {"email": ...}.

        Note this does NOT take the old enrollment token: the spec keys refresh
        on the customer email and returns a token for their most recent
        in-progress enrollment.
        """
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_REFRESH_TOKEN}"
        headers, body = hmac_auth.sign_json_request(self.api_key, self.secret_key, {"email": email})
        try:
            resp = requests.patch(url, data=body.encode("utf-8"), headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e
        if resp.status_code == 404:
            raise PerchNotFoundError(
                "Perch has no in-progress enrollment for this email address.")
        self._raise_for_hmac_status(resp, PATH_REFRESH_TOKEN)
        return self._parse_token_response(resp)

    def get_market_capacity(self, zip_code: str, utility_slug: str) -> dict:
        """GET /markets/capacity - HMAC-authenticated, signed over the canonical
        query string. Pre-enrollment targeting only; not binding at enroll."""
        requests = self._requests()
        headers, qs = hmac_auth.sign_query_request(
            self.api_key, self.secret_key,
            {"zip_code": zip_code, "utility_name": utility_slug})
        # The signed query string must be sent verbatim - letting the client
        # library re-encode params could reorder them and break the signature.
        #
        # E1: the CAPACITY base, not the lookup base. In v1 they are the same
        # string; in v2 capacity lives under /v2/markets while utilities,
        # counties and the rest stay under /v1/markets.
        url = f"{self.markets_capacity_base_url}{PATH_MARKETS_CAPACITY}?{qs}"
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e
        if resp.status_code == 503:
            raise PerchNoCapacityError(
                f"No open capacity for {utility_slug} in ZIP {zip_code}.")
        self._raise_for_hmac_status(resp, PATH_MARKETS_CAPACITY)
        return parse_json_response(resp, PATH_MARKETS_CAPACITY)

    # ─────────────── Market rules (HMAC, pre-enrollment, read-only) ───────────────

    def _get_market(self, path, params):
        """One signed GET against the MARKETS base URL.

        Shared by every market-rules endpoint so the signing, transport and
        status handling exist in exactly one place.

        Deliberately NOT used by get_market_capacity above: that method predates
        this helper, is on the NY path, and is left byte-for-byte alone. It
        picks up the raw-signing correction for free, because both go through
        hmac_auth.sign_query_request.

        404 is handled here rather than in _raise_for_hmac_status, which is
        shared with /token and /refresh_token and must not change. On the market
        endpoints 404 means "no such state, county or utility" - a validation
        problem, not an auth one - so it must not surface as PerchAuthError.
        """
        requests = self._requests()
        headers, qs = hmac_auth.sign_query_request(self.api_key, self.secret_key, params)
        # The signature covers the RAW canonical string; qs is the encoded form
        # to transmit. It must be appended verbatim - re-encoding or reordering
        # invalidates the signature.
        url = f"{self.markets_base_url}{path}?{qs}"
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e
        if resp.status_code == 404:
            raise PerchValidationError(
                f"Perch has no market data matching that request on {path}: {_msg(resp)}")
        self._raise_for_hmac_status(resp, path)
        return parse_json_response(resp, path)

    def get_market_utilities(self, state: str) -> dict:
        """GET /markets/utilities?state= - the utility slugs for a state.

        The returned slug is the ONLY sanctioned source for `utility_name`.
        Confirmed against staging 2026-10-25 that state=VA returns the Dominion
        slug. That value is deliberately not written down anywhere in this
        codebase - it is read from here at runtime, and a test asserts no
        production source contains it as a literal.
        """
        return self._get_market(PATH_MARKETS_UTILITIES, {"state": state})

    def get_market_utility_account_fields(self, utility_slug: str) -> dict:
        """GET /markets/utility_account_fields?utility= - required fields and
        the regex patterns each value must match.

        Spec: `fields` always starts with account_number, followed by
        meter_number and secondary_account_identifier only where the utility
        requires them. "A value is valid when it matches any one of the field's
        patterns. An empty patterns list means the field has no format rule."
        account_number patterns match case-insensitively, the others
        case-sensitively.
        """
        return self._get_market(PATH_MARKETS_UTILITY_ACCOUNT_FIELDS,
                                {"utility": utility_slug})

    def get_market_counties(self, state: str) -> dict:
        """GET /markets/counties?state= - county names for a state, verbatim.

        Names are returned as Perch stores them and are used as-is. Virginia's
        list mixes ordinary counties with independent cities, and the latter are
        spelled with a lowercase 'city' suffix instead of 'County'. Never
        normalize, title-case or re-suffix them: the string Perch returns is the
        string Perch validates against.
        """
        return self._get_market(PATH_MARKETS_COUNTIES, {"state": state})

    def get_market_county_income_limits(self, state: str, county: str) -> dict:
        """GET /markets/counties/income_limits?county=&state= - the household
        income limit for each household size (1-8) in a county.

        `county` sorts before `state` in the signed string, and the county value
        is signed RAW - a space stays a space. This is the call that proved the
        signing fix: raw -> 200, urlencode -> 401.
        """
        return self._get_market(PATH_MARKETS_COUNTY_INCOME_LIMITS,
                                {"state": state, "county": county})

    def get_market_self_attestation_options(self, state: str) -> dict:
        """GET /markets/self_attestation_options?state= - what to send in the
        self_attestation payload for a state.

        The `status` array is the authority on whether a `status` key is sent at
        all. Spec: "when not empty, send one of these values as status;
        otherwise do not send status."

        Confirmed against staging 2026-10-25:
            NY -> status ["accepted", "rejected"], lmi_programs "none"
            VA -> status [],                       lmi_programs "optional"

        A state not configured for self-attestation returns 422, which
        _raise_for_hmac_status surfaces as PerchValidationError.
        """
        return self._get_market(PATH_MARKETS_SELF_ATTESTATION_OPTIONS, {"state": state})

    @staticmethod
    def _raise_for_hmac_status(resp, path):
        """Documented status codes for the three HMAC endpoints.

        401 = HMAC authentication failure (bad key/signature/timestamp)
        403 = API key lacks permission (NOT token expiry - that is only for
              enrollment-session endpoints)
        422 = validation error, e.g. invalid email
        """
        if resp.status_code == 401:
            raise PerchAuthError(
                f"HMAC authentication failed on {path}: {_msg(resp)} "
                "Check the API key, the signature computation, and that the "
                "timestamp is within +/-5 minutes of Perch server time.")
        if resp.status_code == 403:
            raise PerchAuthError(
                f"Perch rejected the API key's permissions on {path}: {_msg(resp)}")
        if resp.status_code == 422:
            raise PerchValidationError(f"Perch rejected the request on {path}: {_msg(resp)}")
        if resp.status_code >= 500:
            raise PerchUnavailableError(f"Perch {path} returned {resp.status_code}.")
        if resp.status_code >= 400:
            raise PerchAuthError(f"Perch {path} failed ({resp.status_code}): {_msg(resp)}")

    @staticmethod
    def _parse_token_response(resp):
        """Normalizes BOTH the documented and the observed staging response.

        The published YAML documents, with all three marked `required`:
            {"enrollment_token": "...", "expires_at": "...", "next_step": "..."}

        Observed Perch staging (2026-08, verified from Mac and Windows, HTTP 201):
            {"token": "...", "next_step_url": "..."}
        i.e. different key names, and NO expires_at at all.

        Rather than guess which is authoritative, accept either and normalize to
        the documented names so the rest of the codebase only ever sees the YAML
        shape. `expires_at` is passed through as None when absent - this method
        never invents one. Deriving a local expiry, and recording that it was
        derived, is token_manager's responsibility.
        """
        data = parse_json_response(resp, "/token")

        # Documented name first, observed staging alias second.
        token = data.get("enrollment_token") or data.get("token")
        if not token:
            raise PerchAuthError(
                "Perch token response contained neither 'enrollment_token' (documented) "
                f"nor 'token' (observed staging). Received keys: {sorted(data.keys())}")

        next_step, _shape = extract_next_step(data)

        return {
            "enrollment_token": token,
            # May legitimately be absent on staging. Never fabricated here.
            "expires_at": data.get("expires_at"),
            "next_step": next_step,
            # Lets callers and tests see which shape actually arrived without
            # re-parsing, and keeps the discrepancy visible in the audit trail.
            "response_shape": "documented" if "enrollment_token" in data else "staging_alias",
            "raw": data,
        }

    def check_capacity(self, enrollment_token: str, zip_code: str, utility_slug: str) -> dict:
        url = f"{self.enrollment_base_url}{PATH_CAPACITY}"
        payload = {"zip_code": zip_code, "utility_name": utility_slug}
        resp = self._post(url, payload, enrollment_token=enrollment_token)

        if resp.status_code == 403:
            raise PerchTokenExpiredError(
                "Perch returned 403 - the enrollment token is expired or invalid.")
        if resp.status_code == 503:
            # Documented business outcome, not a transport failure:
            # "Returns 503 Service Unavailable when no open solar project capacity
            #  exists for the given utility and ZIP."
            raise PerchNoCapacityError(
                f"No open solar project capacity for {utility_slug} in ZIP {zip_code}.")
        if resp.status_code == 422:
            raise PerchValidationError(f"Invalid zip_code or utility_name: {_msg(resp)}")
        if resp.status_code >= 500:
            raise PerchUnavailableError(f"Perch {PATH_CAPACITY} returned {resp.status_code}.")
        if resp.status_code >= 400:
            raise PerchValidationError(f"Perch rejected the capacity request: {_msg(resp)}")
        # Staging returns next_step_url where the YAML documents next_step.
        # Normalize here, at the same layer as _parse_token_response, so nothing
        # downstream needs to know which spelling arrived.
        return normalize_capacity_response(parse_json_response(resp, PATH_CAPACITY))

    def create_enrollment(self, enrollment_token: str, form: dict, files: dict) -> dict:
        """POST /enroll - multipart, X-Enrollment-Token auth, NO HMAC headers.

        Content-Type is deliberately NOT set: requests must generate the
        multipart boundary itself. Setting it manually breaks the encoding.
        """
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_ENROLL}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token}
        try:
            resp = requests.post(url, data=form, files=files,
                                 headers=headers, timeout=max(self.timeout, 60))
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        # Error messages are unchanged; the raw response is attached so callers
        # can inspect what Perch actually returned (see attach_response_diagnostics).
        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /enroll - the enrollment token is expired or invalid."), resp)
        if resp.status_code == 413:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the upload as too large (413): {_msg(resp)}"), resp)
        if resp.status_code == 422:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the enrollment (422): {_msg(resp)}"), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_ENROLL} returned {resp.status_code}."), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the enrollment: {_msg(resp)}"), resp)
        # State-changing and non-idempotent: a sub-400 response we cannot read
        # means the enrollment MAY exist at Perch. Never retried.
        return normalize_enroll_response(
            parse_json_response(resp, PATH_ENROLL, ambiguous_on_failure=True))

    def submit_proof_docs(self, enrollment_token: str, files: dict) -> dict:
        """POST /lmi/proof_docs - JSON metadata part + indexed proof files."""
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_LMI_PROOF_DOCS}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token}
        try:
            resp = requests.post(url, files=files, headers=headers,
                                 timeout=max(self.timeout, 60))
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /lmi/proof_docs - the enrollment token is expired or invalid."), resp)
        if resp.status_code == 413:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the proof upload as too large (413): {_msg(resp)}"), resp)
        if resp.status_code == 422:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the proof documents (422): {_msg(resp)}"), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_LMI_PROOF_DOCS} returned {resp.status_code}."), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the proof documents: {_msg(resp)}"), resp)
        return normalize_proof_docs_response(
            parse_json_response(resp, PATH_LMI_PROOF_DOCS))

    def submit_self_attestation(self, enrollment_token: str, entry: dict) -> dict:
        """POST /lmi/self_attestation - JSON, exactly the documented fields."""
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_LMI_SELF_ATTESTATION}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token,
                   "Content-Type": "application/json"}
        # The wrapper key is required by SubmitSelfAttestationRequest. `entry` is
        # built by the adapter from documented fields only - nothing extra is
        # added here.
        payload = {"self_attestation": entry}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /lmi/self_attestation - the enrollment token is "
                "expired or invalid."), resp)
        if resp.status_code == 422:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the self-attestation (422): {_msg(resp)}"), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_LMI_SELF_ATTESTATION} returned {resp.status_code}."), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the self-attestation: {_msg(resp)}"), resp)
        return normalize_self_attestation_response(
            parse_json_response(resp, PATH_LMI_SELF_ATTESTATION))

    def accept_self_attestation(self, enrollment_token: str, metadata: dict) -> dict:
        """POST /lmi/self_attestation/accept - documented metadata only.

        Returns 202 (not 200) on success.
        """
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_LMI_SELF_ATTESTATION_ACCEPT}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token,
                   "Content-Type": "application/json"}
        payload = {"self_attestation": metadata}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /lmi/self_attestation/accept - the enrollment "
                "token is expired or invalid."), resp)
        if resp.status_code == 422:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the self-attestation acceptance (422): {_msg(resp)}"), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_LMI_SELF_ATTESTATION_ACCEPT} returned {resp.status_code}."), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the self-attestation acceptance: {_msg(resp)}"), resp)
        # State-changing and non-idempotent, like /enroll: an unreadable sub-400
        # means acceptance MAY have landed. Never retried blindly.
        return normalize_self_attestation_accept_response(
            parse_json_response(resp, PATH_LMI_SELF_ATTESTATION_ACCEPT,
                                ambiguous_on_failure=True))

    def generate_contracts(self, enrollment_token: str) -> dict:
        """POST /contracts - no request body, X-Enrollment-Token auth only.

        The spec's cURL sends no -d/--form payload, so nothing is transmitted
        beyond the auth header. Documented statuses are 200, 403, and 500;
        there is no documented 422 for this endpoint.
        """
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_CONTRACTS}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token}
        try:
            # No body: the spec documents none.
            resp = requests.post(url, headers=headers, timeout=max(self.timeout, 60))
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /contracts - the enrollment token is expired or invalid."), resp)
        if resp.status_code >= 500:
            # Spec: "Contract generation failed. Retry the request."
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_CONTRACTS} returned {resp.status_code} - contract generation "
                f"failed, retry the request. {_msg(resp)}"), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the contract request: {_msg(resp)}"), resp)
        return normalize_contracts_response(parse_json_response(resp, PATH_CONTRACTS))

    def accept_contracts(self, enrollment_token: str, metadata: dict) -> dict:
        """POST /contracts/accept - JSON body, X-Enrollment-Token auth, no HMAC.

        Body is exactly {"metadata": {ip_address, timestamp, user_agent}}.

        IDEMPOTENCY GUARDRAIL: Perch has not documented whether this endpoint is
        idempotent, so a transport failure, timeout, or 5xx raises
        PerchAmbiguousOutcomeError rather than a retryable error - the request
        may already have been processed. The caller must resolve the true state
        via GET /status instead of resending. A 403 still raises
        PerchTokenExpiredError because that is a DEFINITE rejection (Perch did
        not process it), so the documented refresh-and-retry-once path is safe.
        """
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_CONTRACTS_ACCEPT}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token,
                   "Content-Type": "application/json"}
        payload = {"metadata": {
            "ip_address": metadata.get("ip_address"),
            "timestamp": metadata.get("timestamp"),
            "user_agent": metadata.get("user_agent"),
        }}
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=max(self.timeout, 60))
        except Exception as e:
            raise PerchAmbiguousOutcomeError(
                f"Contract acceptance could not be confirmed - the request to {url} "
                f"failed in transport ({e}). It may or may not have been processed. "
                f"Do not resend; check GET /status.") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /contracts/accept - token expired or invalid."), resp)
        if resp.status_code == 422:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the acceptance metadata (422): {_msg(resp)}"), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchAmbiguousOutcomeError(
                f"Perch {PATH_CONTRACTS_ACCEPT} returned {resp.status_code}. Acceptance may "
                f"or may not have been recorded. Do not resend; check GET /status. "
                f"{_msg(resp)}"), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the acceptance: {_msg(resp)}"), resp)
        # Documented success is 202 Accepted (async), not 200.
        # Same hazard as /enroll: Perch may have recorded the acceptance.
        return normalize_accept_response(
            parse_json_response(resp, PATH_CONTRACTS_ACCEPT, ambiguous_on_failure=True))

    def get_status(self, enrollment_token: str) -> dict:
        """GET /status - side-effect free, safe to poll."""
        requests = self._requests()
        url = f"{self.enrollment_base_url}{PATH_STATUS}"
        headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token}
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e

        if resp.status_code == 403:
            raise attach_response_diagnostics(PerchTokenExpiredError(
                "Perch returned 403 on /status - token expired or invalid."), resp)
        if resp.status_code >= 500:
            raise attach_response_diagnostics(PerchUnavailableError(
                f"Perch {PATH_STATUS} returned {resp.status_code}. {_msg(resp)}"), resp)
        if resp.status_code >= 400:
            raise attach_response_diagnostics(PerchValidationError(
                f"Perch rejected the status request: {_msg(resp)}"), resp)
        return normalize_status_response(parse_json_response(resp, PATH_STATUS))
