"""
Perch enrollment-token lifecycle. Entirely server-side.

Documented model (Milestone 2 rewrite):
  * POST /token issues an enrollment_token (UUID).
  * It is sent as the X-Enrollment-Token header.
  * It expires after 30 MINUTES.
  * PATCH /refresh_token issues a new one.
  * Any enrollment-token endpoint returns 403 when it has expired; Perch's
    guidance is to refresh and retry the original request.

Tokens are scoped per enrollment session. A rep working three enrollments has
three tokens; a rep who comes back an hour later to upload a proof doc has a
dead one and gets a fresh token transparently. That scenario was called out
explicitly on the engineering call.

The token value never appears in an API response or an audit log - there are
tests asserting both.
"""
import threading
from datetime import datetime, timedelta

from db import query_one, execute
from services.perch.config import get_perch_client, get_api_mode, TOKEN_REFRESH_SKEW_SECONDS
from services.perch.client import TOKEN_TTL_SECONDS
from services.perch.errors import PerchAuthError

# Serializes refresh within a process. A multi-worker deployment needs a DB
# advisory lock or Redis lease instead - noted for the AWS milestone.
_refresh_lock = threading.Lock()


def _now():
    return datetime.now()


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def active_token_row(enrollment_id):
    return query_one(
        """SELECT * FROM perch_tokens
           WHERE is_active = 1 AND api_mode = ? AND enrollment_id = ?
           ORDER BY id DESC LIMIT 1""",
        (get_api_mode(), enrollment_id),
    )


def _is_usable(row):
    if not row:
        return False
    expires = _parse(row["expires_at"])
    if not expires:
        return False
    return expires > (_now() + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS))


def _server_expiry(expires_at_iso):
    """Prefer Perch's own expires_at over our local clock. Falls back to
    now+TTL only if the field is absent, which the spec says it never is."""
    if expires_at_iso:
        try:
            return datetime.fromisoformat(str(expires_at_iso).replace("Z", "+00:00")) \
                .replace(tzinfo=None)
        except ValueError:
            pass
    return _now() + timedelta(seconds=TOKEN_TTL_SECONDS)


def _store(enrollment_id, token, refresh_count=0, expires_at_iso=None):
    execute(
        "UPDATE perch_tokens SET is_active = 0 WHERE api_mode = ? AND enrollment_id = ?",
        (get_api_mode(), enrollment_id),
    )
    expires_at = _server_expiry(expires_at_iso).isoformat()
    cur = execute(
        """INSERT INTO perch_tokens
           (access_token, token_type, scope, expires_at, is_active, api_mode, enrollment_id, refresh_count)
           VALUES (?, 'enrollment_token', NULL, ?, 1, ?, ?, ?)""",
        (token, expires_at, get_api_mode(), enrollment_id, refresh_count),
    )
    return query_one("SELECT * FROM perch_tokens WHERE id = ?", (cur.lastrowid,))


def _log(enrollment_id, operation, endpoint, method, started, error=None, user_id=None):
    from services.perch.adapter import record_api_call
    record_api_call(
        enrollment_id=enrollment_id, operation=operation, endpoint=endpoint,
        http_method=method, request_json={}, 
        response_json=None if error else {"enrollment_token": "[REDACTED]"},
        status_code=None if error else 200,
        duration_ms=int((_now() - started).total_seconds() * 1000),
        error_message=error, initiated_by_user_id=user_id,
    )


def _email_for(enrollment_id):
    """POST /token requires the customer's email, so it must exist before any
    Perch call. Stored on the enrollment by the workflow's first step."""
    row = query_one("SELECT perch_token_email FROM enrollments WHERE id = ?", (enrollment_id,))
    email = row["perch_token_email"] if row else None
    if not email:
        raise PerchAuthError(
            "This enrollment has no customer email yet. Perch requires an email on "
            "POST /token, so it must be collected before any Perch call.")
    return email


def issue_new_token(enrollment_id, user_id=None):
    """POST /token - brand new enrollment token.

    NEW YAML: if Perch already has an in-progress enrollment for this email it
    returns 422 rather than a second session. The documented recovery is to
    resume via PATCH /refresh_token, which is what we do here - otherwise a rep
    revisiting an abandoned customer would be permanently stuck.
    """
    from services.perch.client import PATH_TOKEN
    from services.perch.errors import PerchEnrollmentInProgressError
    client = get_perch_client()
    started = _now()
    try:
        data = client.request_token(_email_for(enrollment_id))
    except PerchEnrollmentInProgressError:
        _log(enrollment_id, "request_token", PATH_TOKEN, "POST", started,
             error="422 enrollment already in progress - resuming via refresh_token",
             user_id=user_id)
        return _resume_existing_enrollment(enrollment_id, user_id=user_id)
    except Exception as e:
        _log(enrollment_id, "request_token", PATH_TOKEN, "POST", started, error=str(e), user_id=user_id)
        raise
    _log(enrollment_id, "request_token", PATH_TOKEN, "POST", started, user_id=user_id)
    return _store(enrollment_id, data["enrollment_token"], refresh_count=0,
                  expires_at_iso=data.get("expires_at"))


def _resume_existing_enrollment(enrollment_id, user_id=None):
    """Recovery for the documented 422: Perch already has an in-progress
    enrollment for this email, so obtain a token for it via PATCH /refresh_token
    instead of trying to create a second one."""
    from services.perch.client import PATH_REFRESH_TOKEN
    client = get_perch_client()
    started = _now()
    try:
        data = client.refresh_token(_email_for(enrollment_id))
    except Exception as e:
        _log(enrollment_id, "refresh_token", PATH_REFRESH_TOKEN, "PATCH", started,
             error=str(e), user_id=user_id)
        raise
    _log(enrollment_id, "refresh_token", PATH_REFRESH_TOKEN, "PATCH", started, user_id=user_id)
    return _store(enrollment_id, data["enrollment_token"], refresh_count=0,
                  expires_at_iso=data.get("expires_at"))


def refresh_existing_token(enrollment_id, user_id=None):
    """PATCH /refresh_token - new token from the current (possibly expired) one.

    Falls back to a brand-new token if refresh fails, because Perch's guidance
    ("if the problem persists, contact Perch Energy") implies refresh is not
    always recoverable, and a rep should not be blocked by it.
    """
    from services.perch.client import PATH_REFRESH_TOKEN
    row = active_token_row(enrollment_id)
    if not row:
        return issue_new_token(enrollment_id, user_id=user_id)

    client = get_perch_client()
    started = _now()
    try:
        data = client.refresh_token(_email_for(enrollment_id))
    except Exception as e:
        _log(enrollment_id, "refresh_token", PATH_REFRESH_TOKEN, "PATCH", started,
             error=str(e), user_id=user_id)
        return issue_new_token(enrollment_id, user_id=user_id)
    _log(enrollment_id, "refresh_token", PATH_REFRESH_TOKEN, "PATCH", started, user_id=user_id)
    return _store(enrollment_id, data["enrollment_token"],
                  refresh_count=(row["refresh_count"] or 0) + 1,
                  expires_at_iso=data.get("expires_at"))


def get_valid_token(enrollment_id, user_id=None) -> str:
    """Returns a usable enrollment token for this enrollment, obtaining or
    refreshing one as needed. Callers never handle expiry."""
    row = active_token_row(enrollment_id)
    if _is_usable(row):
        return row["access_token"]

    with _refresh_lock:
        row = active_token_row(enrollment_id)
        if _is_usable(row):
            return row["access_token"]
        if row:
            row = refresh_existing_token(enrollment_id, user_id=user_id)
        else:
            row = issue_new_token(enrollment_id, user_id=user_id)

    if not row:
        raise PerchAuthError("Could not obtain a Perch enrollment token.")
    return row["access_token"]


def token_status(enrollment_id):
    """Non-sensitive token state for diagnostics. Excludes the token value."""
    row = active_token_row(enrollment_id)
    if not row:
        return {"has_token": False, "api_mode": get_api_mode(), "ttl_seconds": TOKEN_TTL_SECONDS}
    expires = _parse(row["expires_at"])
    return {
        "has_token": True,
        "api_mode": row["api_mode"],
        "expires_at": row["expires_at"],
        "seconds_remaining": int((expires - _now()).total_seconds()) if expires else None,
        "is_usable": _is_usable(row),
        "refresh_count": row["refresh_count"],
        "ttl_seconds": TOKEN_TTL_SECONDS,
    }
