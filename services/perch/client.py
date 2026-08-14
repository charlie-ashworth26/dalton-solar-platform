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
import os
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

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
    next_step = data.get("next_step") or data.get("next_step_url")
    return {
        "project_details": data.get("project_details"),
        "next_step": next_step,
        "response_shape": "documented" if "next_step" in data else (
            "staging_alias" if "next_step_url" in data else "no_next_step"),
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

    # Flat top-level fields
    for key in ("email_address", "first_name", "last_name", "phone_number",
                "customer_type", "utility_name", "zip_code",
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
    next_step = data.get("next_step") or data.get("next_step_url")
    raw_contracts = data.get("contract_urls")
    if not isinstance(raw_contracts, list):
        raw_contracts = []

    safe = []
    for item in raw_contracts:
        if not isinstance(item, dict):
            # Malformed entry - record its presence without inventing fields.
            safe.append({"contract_name": None, "expires_at": None,
                         "url_present": False, "malformed": True})
            continue
        url = item.get("url")
        safe.append({
            "contract_name": item.get("contract_name"),
            "expires_at": item.get("expires_at"),
            # Structural check only. The value never leaves `raw`.
            "url_present": bool(url) and isinstance(url, str) and url.strip() != "",
        })

    return {
        "contracts": safe,
        "contract_count": len(safe),
        "next_step": next_step,
        "response_shape": "documented" if "next_step" in data else (
            "staging_alias" if "next_step_url" in data else "no_next_step"),
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
ACCEPTANCE_CLOCK_SKEW_SECONDS = 10

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
    base = now or datetime.now()
    stamped = base - timedelta(seconds=ACCEPTANCE_CLOCK_SKEW_SECONDS)
    return stamped.strftime(ACCEPTANCE_TIMESTAMP_FORMAT)[:-2]


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
    next_step = data.get("next_step") or data.get("next_step_url")
    completed_steps = data.get("completed_steps")
    remaining_steps = data.get("remaining_steps")
    return {
        "completed_steps": completed_steps if isinstance(completed_steps, list) else [],
        "remaining_steps": remaining_steps if isinstance(remaining_steps, list) else [],
        # Only a literal True counts as complete - never coerce a truthy value.
        "completed": data.get("completed") is True,
        "next_step": next_step,
        "response_shape": "documented" if "next_step" in data else (
            "staging_alias" if "next_step_url" in data else "no_next_step"),
        "raw": data,
    }


def normalize_proof_docs_response(data: dict) -> dict:
    """Normalize proof-doc success response while preserving the raw body."""
    data = data or {}
    next_step = data.get("next_step") or data.get("next_step_url")
    return {
        "next_step": next_step,
        "response_shape": "documented" if "next_step" in data else (
            "staging_alias" if "next_step_url" in data else "no_next_step"),
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
    next_step = data.get("next_step") or data.get("next_step_url")
    return {
        "next_step": next_step,
        "response_shape": "documented" if "next_step" in data else (
            "staging_alias" if "next_step_url" in data else "no_next_step"),
        "raw": data,
    }


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
        exc.body_text = resp.text
    except Exception:
        exc.body_text = None
    try:
        exc.body_json = resp.json()
    except Exception:
        exc.body_json = None
    return exc


def _msg(resp):
    """Formats a Perch error body into a readable message.

    Perch uses TWO error envelopes, both now observed live:

      1. ErrorResponse            {"error": "...", "message": "..."}
      2. ValidationErrorsResponse {"errors": [{"field","code","message"}, ...]}

    The original implementation only handled (1), so a real /enroll 422 in
    envelope (2) rendered as the useless string "None: None". Both are handled
    now. The full response is still attached to the exception by
    attach_response_diagnostics() regardless of shape.
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
                field = item.get("field") or item.get("name") or "(no field)"
                code = item.get("code")
                message = item.get("message") or item.get("detail") or ""
                # Include the utility account number when Perch scopes an error
                # to one account, so a multi-account failure is attributable.
                acct = item.get("utility_account_number")
                seg = f"{field}"
                if code:
                    seg += f" [{code}]"
                if message:
                    seg += f": {message}"
                if acct:
                    seg += f" (utility_account_number={acct})"
                parts.append(seg)
            return "; ".join(parts)

        if body.get("error") is not None or body.get("message") is not None:
            return f"{body.get('error')}: {body.get('message')}"

    return (resp.text or "")[:200]

# Documented path suffixes, relative to the enrollment base URL.
PATH_TOKEN = "/token"
# NEW YAML: POST /token returns 201 Created (previous spec said 200).
TOKEN_CREATED_STATUS = 201
PATH_REFRESH_TOKEN = "/refresh_token"
PATH_CAPACITY = "/capacity"
PATH_ENROLL = "/enroll"          # Milestone 3
PATH_LMI_PROOF_DOCS = "/lmi/proof_docs"
PATH_CONTRACTS = "/contracts"
PATH_STATUS = "/status"          # Milestone 6
PATH_CONTRACTS_ACCEPT = "/contracts/accept"
PATH_MARKETS_CAPACITY = "/capacity"   # relative to the MARKETS base URL

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


class PerchHTTPClient(PerchClient):
    """Real Perch API client, written to the published contract.

    NOT yet exercised against Perch staging - credentials were promised but not
    yet issued at the time of writing. Paths, headers, parameter names, and
    status-code handling all come from the published docs, so the surface area
    for surprises is far smaller than the Milestone 1 version, but it remains
    unverified against a live endpoint.
    """

    mode = "live"

    def __init__(self, enrollment_base_url, markets_base_url, api_key, secret_key="", timeout=20):
        self.enrollment_base_url = (enrollment_base_url or "").rstrip("/")
        self.markets_base_url = (markets_base_url or "").rstrip("/")
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
        url = f"{self.markets_base_url}{PATH_MARKETS_CAPACITY}?{qs}"
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except Exception as e:
            raise PerchUnavailableError(f"Could not reach Perch at {url}: {e}") from e
        if resp.status_code == 503:
            raise PerchNoCapacityError(
                f"No open capacity for {utility_slug} in ZIP {zip_code}.")
        self._raise_for_hmac_status(resp, PATH_MARKETS_CAPACITY)
        return resp.json()

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
        data = resp.json()

        # Documented name first, observed staging alias second.
        token = data.get("enrollment_token") or data.get("token")
        if not token:
            raise PerchAuthError(
                "Perch token response contained neither 'enrollment_token' (documented) "
                f"nor 'token' (observed staging). Received keys: {sorted(data.keys())}")

        next_step = data.get("next_step") or data.get("next_step_url")

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
        return normalize_capacity_response(resp.json())

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
        return normalize_enroll_response(resp.json())

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
        return normalize_proof_docs_response(resp.json())

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
        return normalize_contracts_response(resp.json())

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
        return normalize_accept_response(resp.json())

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
        return normalize_status_response(resp.json())
