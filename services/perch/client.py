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
from abc import ABC, abstractmethod

from services.perch import hmac_auth
from services.perch.errors import (
    PerchAuthError, PerchUnavailableError, PerchValidationError,
    PerchTokenExpiredError, PerchNoCapacityError, PerchNotImplementedError,
    PerchNotFoundError, PerchEnrollmentInProgressError,
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


def _msg(resp):
    """Perch returns a standard {error, message} envelope on every non-2xx."""
    try:
        body = resp.json()
        return f"{body.get('error')}: {body.get('message')}"
    except Exception:
        return (resp.text or "")[:200]

# Documented path suffixes, relative to the enrollment base URL.
PATH_TOKEN = "/token"
# NEW YAML: POST /token returns 201 Created (previous spec said 200).
TOKEN_CREATED_STATUS = 201
PATH_REFRESH_TOKEN = "/refresh_token"
PATH_CAPACITY = "/capacity"
PATH_ENROLL = "/enroll"          # Milestone 3
PATH_STATUS = "/status"          # Milestone 6
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
        data = resp.json()
        token = data.get("enrollment_token")
        if not token:
            raise PerchAuthError(
                "Perch token response did not contain an enrollment_token. "
                f"Received keys: {sorted(data.keys())}")
        # NEW YAML: enrollment_token, expires_at AND next_step are all `required`
        # on the token response. expires_at is authoritative - prefer Perch's
        # clock over ours. next_step is how an interrupted flow resumes.
        return {
            "enrollment_token": token,
            "expires_at": data.get("expires_at"),
            "next_step": data.get("next_step"),
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
        return resp.json()
