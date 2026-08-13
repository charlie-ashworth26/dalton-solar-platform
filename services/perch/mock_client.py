"""
Mock Perch client - rewritten in Milestone 2 to emit EXACTLY the documented
schemas.

Fidelity rules this mock follows:
  * The 200 capacity body has precisely the six documented project_details
    fields and a next_step URL. No extra fields, no invented ones.
  * next_step is a URL (the documented .../enrollments/enroll), never an enum.
  * No open capacity raises PerchNoCapacityError, mirroring the documented 503.
  * An expired token raises PerchTokenExpiredError, mirroring the documented 403,
    so the adapter's refresh-and-retry path is exercised for real.
  * Tokens are UUIDs, matching the documented example format
    (550e8400-e29b-41d4-a716-446655440000).
  * The mock enforces the 1-HOUR TTL itself (NEW YAML), so token expiry is
    genuinely tested rather than simulated only by clock manipulation.
  * Re-requesting a token for an email that already has an in-progress
    enrollment raises PerchEnrollmentInProgressError, mirroring the documented
    422 - so the resume-via-refresh path is exercised for real.

Fixtures are keyed by ZIP so tests can assert exact values. The catalog is a
TEST FIXTURE, not a mirror of Perch's real projects - Perch has ~3,400 NY
projects with priority stacking and we deliberately do not model them.
"""
import uuid
from datetime import datetime, timedelta

from services.perch.client import PerchClient, TOKEN_TTL_SECONDS
from services.perch.errors import (
    PerchValidationError, PerchUnavailableError,
    PerchNoCapacityError, PerchTokenExpiredError, PerchNotFoundError,
    PerchEnrollmentInProgressError,
)

DOCUMENTED_NEXT_STEP_ENROLL = "https://api.perchenergy.com/affiliate_partners/v1/enrollments/enroll"
DOCUMENTED_NEXT_STEP_CAPACITY = "https://api.perchenergy.com/affiliate_partners/v1/enrollments/capacity"

# ZIP prefix -> the project_details Perch would return for that service area.
# Field names and value types are exactly as published.
_FIXTURES = {
    "133": {  # Upstate NY - full LMI + residential, IRA project requiring proof docs
        "utility_slug": "national-grid-ny",
        "project_details": {
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 10,
            "savings_percent_for_lmi_customers": 20,
        },
    },
    "120": {  # Capital Region - residential only, no LMI capacity left
        "utility_slug": "national-grid-ny",
        "project_details": {
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": False,
            "residential_capacity_available": True,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 8,
            "savings_percent_for_lmi_customers": 0,
        },
    },
    "100": {  # NYC - ConEd, LMI available with no proof doc (Empower Zone style)
        "utility_slug": "consolidated-edison-ny",
        "project_details": {
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 5,
            "savings_percent_for_lmi_customers": 25,
        },
    },
    "125": {  # Hudson Valley - Central Hudson, exercises the POD ID utility path
        "utility_slug": "central-hudson-gas-electric",
        "project_details": {
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 10,
            "savings_percent_for_lmi_customers": 20,
        },
    },
}

# Reserved ZIPs for exercising documented failure paths.
NO_CAPACITY_ZIP = "99999"       # -> documented 503
UPSTREAM_FAILURE_ZIP = "00000"  # -> genuine 5xx, distinct from 503


class PerchMockClient(PerchClient):
    mode = "mock"

    # Issued tokens: {token: expires_at}. Module-level so the same mock state
    # survives across the per-request client instances the factory hands out.
    _issued_tokens = {}

    # email -> most recent issued token, mirroring the spec's statement that
    # refresh returns a token for "the most recent in-progress enrollment
    # associated with the given email address".
    _tokens_by_email = {}

    def _issue(self, email, next_step=None):
        token = str(uuid.uuid4())
        expires = datetime.now() + timedelta(seconds=TOKEN_TTL_SECONDS)
        PerchMockClient._issued_tokens[token] = expires
        PerchMockClient._tokens_by_email[email] = token
        raw = {
            "enrollment_token": token,
            # Spec: ISO 8601 date-time, Z-suffixed.
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_step": next_step or DOCUMENTED_NEXT_STEP_CAPACITY,
        }
        return {"enrollment_token": token, "expires_at": raw["expires_at"],
                "next_step": raw["next_step"], "raw": raw}

    @staticmethod
    def _validate_email(email):
        if not email or "@" not in str(email):
            raise PerchValidationError("Email is invalid")

    def request_token(self, email: str) -> dict:
        """POST /token - spec requires {"email": ...}, HMAC-authenticated.

        NEW YAML: returns 201 Created. A second call for an email that already
        has an in-progress enrollment returns 422 ("An enrollment request
        already exists for this email"), NOT a second session.
        """
        self._validate_email(email)
        if email in PerchMockClient._tokens_by_email:
            raise PerchEnrollmentInProgressError(
                "An enrollment request already exists for this email. Use the "
                "/status endpoint to check the current status of the enrollment.")
        return self._issue(email)

    def refresh_token(self, email: str) -> dict:
        """PATCH /refresh_token - keyed on EMAIL, not on the old token.
        404 when no in-progress enrollment exists for that email."""
        self._validate_email(email)
        if email not in PerchMockClient._tokens_by_email:
            raise PerchNotFoundError("No enrollment request found for this email address.")
        old = PerchMockClient._tokens_by_email.get(email)
        PerchMockClient._issued_tokens.pop(old, None)
        return self._issue(email)

    def _assert_token_valid(self, token):
        expires = PerchMockClient._issued_tokens.get(token)
        if expires is None:
            raise PerchTokenExpiredError("Perch returned 403 - unknown enrollment token.")
        if expires <= datetime.now():
            raise PerchTokenExpiredError("Perch returned 403 - enrollment token expired.")

    def expire_token(self, token):
        """Test hook: force a token past its TTL without waiting an hour."""
        if token in PerchMockClient._issued_tokens:
            PerchMockClient._issued_tokens[token] = datetime.now() - timedelta(seconds=1)

    def check_capacity(self, enrollment_token: str, zip_code: str, utility_slug: str) -> dict:
        self._assert_token_valid(enrollment_token)

        zip_code = (zip_code or "").strip()
        if not zip_code.isdigit() or len(zip_code) != 5:
            raise PerchValidationError("zip_code must be a 5-digit ZIP.")
        if not utility_slug:
            raise PerchValidationError("utility_name is required (utility slug).")

        if zip_code == UPSTREAM_FAILURE_ZIP:
            raise PerchUnavailableError("Perch capacity service is unavailable (simulated 500).")

        if zip_code == NO_CAPACITY_ZIP:
            raise PerchNoCapacityError(
                f"No open solar project capacity for {utility_slug} in ZIP {zip_code}.")

        fixture = _FIXTURES.get(zip_code[:3])
        if not fixture or fixture["utility_slug"] != utility_slug:
            # Wrong utility for the ZIP, or an unmodelled ZIP: Perch has no open
            # project for that combination, which is the documented 503 case.
            raise PerchNoCapacityError(
                f"No open solar project capacity for {utility_slug} in ZIP {zip_code}.")

        # Exactly the documented response body - nothing added.
        return {
            "project_details": dict(fixture["project_details"]),
            "next_step": DOCUMENTED_NEXT_STEP_ENROLL,
        }
