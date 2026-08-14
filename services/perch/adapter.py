"""
The Perch adapter - the only Perch-aware module routes may import.

Responsibilities:
  1. Acquire a valid enrollment token (delegating expiry to token_manager).
  2. Dispatch to whichever PerchClient the environment selected.
  3. Implement the documented 403 -> refresh -> retry-once behaviour.
  4. Translate the documented 503 into a structured "no capacity" business result.
  5. Persist every request/response/error against the Dalton enrollment ID.
  6. Normalize the documented project_details shape into Dalton's schema.
  7. Record the next_step URL Perch handed back, so the workflow engine can
     resolve what comes next instead of us guessing.
"""
import json
from datetime import datetime

from db import query, query_one, execute
from services.perch.config import get_perch_client, get_api_mode
from services.perch import token_manager, utilities
from services.perch.client import (
    PATH_CAPACITY, PATH_ENROLL, PATH_LMI_PROOF_DOCS, PATH_CONTRACTS,
    build_enrollment_multipart, build_proof_docs_multipart,
    contracts_safe, redact_contract_urls,
)
from services.perch.errors import (
    PerchError, PerchValidationError, PerchTokenExpiredError, PerchNoCapacityError,
)


def _json(v):
    return json.dumps(v, default=str) if v is not None else None


def record_api_call(enrollment_id, operation, endpoint, http_method, request_json,
                    response_json, status_code, duration_ms, error_message,
                    initiated_by_user_id):
    """Writes one row to perch_api_calls. Callers redact secrets before calling;
    no Perch credential or token value is ever passed in."""
    cur = execute(
        """INSERT INTO perch_api_calls
           (enrollment_id, operation, http_method, endpoint, request_json, response_json,
            status_code, duration_ms, error_message, api_mode, initiated_by_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, operation, http_method, endpoint, _json(request_json), _json(response_json),
         status_code, duration_ms, error_message, get_api_mode(), initiated_by_user_id),
    )
    return cur.lastrowid


def set_token_email(enrollment_id, email):
    """Persists the customer email used for POST /token and PATCH /refresh_token.
    Must happen before any Perch call - the spec requires it on /token."""
    execute("UPDATE enrollments SET perch_token_email = ?, updated_at = datetime('now') WHERE id = ?",
            (email, enrollment_id))


def check_capacity(enrollment_id, zip_code, utility_input, email=None, user_id=None):
    """POST /capacity through the adapter.

    Implements the documented behaviours exactly:
      403 -> PATCH /refresh_token, retry the SAME request once, then give up.
      503 -> capacity_available: False. Not an error; the rep sees "no capacity
             in this area" and, per Perch, must not proceed to enroll.

    Always calls Perch. Stored checks are audit records, never a read cache -
    Perch enforces these rates at enroll, so a stale local copy is worthless.
    """
    zip_code = (zip_code or "").strip()
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise PerchValidationError("ZIP code must be exactly 5 digits.")

    if email:
        if "@" not in email:
            raise PerchValidationError("Enter a valid email address.")
        set_token_email(enrollment_id, email)

    utility_slug = utilities.resolve_slug(utility_input)
    if not utility_slug:
        raise PerchValidationError(
            f"'{utility_input}' is not a recognized utility. Perch matches utilities by slug; "
            "pick one from the supported list.")

    client = get_perch_client()
    token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
    payload = {"zip_code": zip_code, "utility_name": utility_slug}

    started = datetime.now()
    attempted_refresh = False
    response = None
    no_capacity = False
    error_for_log = None
    status_for_log = 200

    try:
        response = client.check_capacity(token, zip_code, utility_slug)
    except PerchTokenExpiredError:
        # Documented recovery path: refresh, then retry the original request once.
        attempted_refresh = True
        record_api_call(
            enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
            http_method="POST", request_json=payload, response_json=None, status_code=403,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message="403 enrollment_token expired - refreshing and retrying once",
            initiated_by_user_id=user_id,
        )
        token_manager.refresh_existing_token(enrollment_id, user_id=user_id)
        token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
        started = datetime.now()
        try:
            response = client.check_capacity(token, zip_code, utility_slug)
        except PerchNoCapacityError as e:
            no_capacity, status_for_log, error_for_log = True, 503, str(e)
        # A second PerchTokenExpiredError intentionally propagates - retrying
        # forever would mask a real auth problem.
    except PerchNoCapacityError as e:
        no_capacity, status_for_log, error_for_log = True, 503, str(e)
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
            http_method="POST", request_json=payload, response_json=None,
            status_code=getattr(e, "http_status", None),
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise

    duration_ms = int((datetime.now() - started).total_seconds() * 1000)
    api_call_id = record_api_call(
        enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
        http_method="POST", request_json=payload,
        response_json=(response or {}).get("raw", response),
        status_code=status_for_log, duration_ms=duration_ms, error_message=error_for_log,
        initiated_by_user_id=user_id,
    )

    details = (response or {}).get("project_details") or {}
    next_step_url = (response or {}).get("next_step")

    execute(
        """INSERT INTO perch_capacity_checks
           (enrollment_id, perch_api_call_id, zip_code, utility_slug, capacity_available,
            residential_capacity_available, small_commercial_capacity_available,
            lmi_capacity_available, proof_documents_required,
            savings_percent_res_commercial, savings_percent_lmi, next_step_url,
            raw_response_json, api_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, api_call_id, zip_code, utility_slug, 0 if no_capacity else 1,
         _b(details.get("residential_capacity_available")),
         _b(details.get("small_commercial_capacity_available")),
         _b(details.get("lmi_capacity_available")),
         _b(details.get("proof_documents_required")),
         details.get("savings_percent_for_residential_and_commercial_customers"),
         details.get("savings_percent_for_lmi_customers"),
         next_step_url,
         # Store what PERCH actually sent, not our normalized wrapper, so the
         # audit record stays a faithful copy of the wire response.
         _json((response or {}).get("raw", response) if response is not None
               else {"no_capacity": True, "detail": error_for_log}),
         get_api_mode()),
    )

    execute(
        "UPDATE enrollments SET service_zip = ?, utility_name = ?, updated_at = datetime('now') WHERE id = ?",
        (zip_code, utility_slug, enrollment_id),
    )

    return {
        "capacity_available": not no_capacity,
        "zip_code": zip_code,
        "utility_slug": utility_slug,
        "utility_display_name": (utilities.by_slug(utility_slug) or {}).get("display_name"),
        "project_details": details if not no_capacity else None,
        "next_step_url": next_step_url,
        "token_was_refreshed": attempted_refresh,
        "api_mode": get_api_mode(),
        "checked_at": datetime.now().isoformat(),
        "message": error_for_log if no_capacity else None,
    }


def _b(v):
    return None if v is None else (1 if v else 0)


def latest_capacity_check(enrollment_id):
    """Most recent stored check. AUDIT/RESUME ONLY - never a substitute for a
    fresh call when making an enrollment decision, because Perch enforces the
    live rates at enroll."""
    row = query_one(
        "SELECT * FROM perch_capacity_checks WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1",
        (enrollment_id,),
    )
    if not row:
        return None
    d = dict(row)
    return {
        "capacity_available": bool(d["capacity_available"]),
        "zip_code": d["zip_code"],
        "utility_slug": d["utility_slug"],
        "utility_display_name": (utilities.by_slug(d["utility_slug"]) or {}).get("display_name"),
        "project_details": {
            "residential_capacity_available": _ob(d["residential_capacity_available"]),
            "small_commercial_capacity_available": _ob(d["small_commercial_capacity_available"]),
            "lmi_capacity_available": _ob(d["lmi_capacity_available"]),
            "proof_documents_required": _ob(d["proof_documents_required"]),
            "savings_percent_for_residential_and_commercial_customers": d["savings_percent_res_commercial"],
            "savings_percent_for_lmi_customers": d["savings_percent_lmi"],
        } if d["capacity_available"] else None,
        "next_step_url": d["next_step_url"],
        "api_mode": d["api_mode"],
        "checked_at": d["checked_at"],
        "stale": True,
    }


def _ob(v):
    return None if v is None else bool(v)


def api_call_history(enrollment_id):
    rows = query(
        """SELECT id, operation, http_method, endpoint, status_code, duration_ms,
                  error_message, api_mode, created_at
           FROM perch_api_calls WHERE enrollment_id = ? ORDER BY id""",
        (enrollment_id,),
    )
    return [dict(r) for r in rows]


# ─────────────── Enrollment / LMI / contracts bridge ───────────────
# These methods deliberately live in the adapter instead of routes. The browser
# supplies Dalton enrollment data; this layer is responsible for translating it
# into Perch's exact multipart contract, refreshing tokens, and recording a
# URL-free / secret-free audit trail.

def choose_customer_type(details):
    """Derive Perch customer_type from the authoritative capacity response.

    Business is never silently selected because Perch requires extra business
    fields that Dalton's residential/LMI GUI does not currently collect.
    """
    details = details or {}
    if details.get("lmi_capacity_available"):
        return "LMI", "lmi_capacity_available=True"
    if details.get("residential_capacity_available"):
        return "Residential", "residential_capacity_available=True"
    if details.get("small_commercial_capacity_available"):
        raise PerchValidationError(
            "This service area currently has only small-commercial capacity. "
            "The Dalton GUI does not collect the business fields Perch requires yet.")
    raise PerchValidationError("No eligible residential or LMI capacity is available for this enrollment.")


def _rewind_files(files):
    for value in (files or {}).values():
        if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "seek"):
            try:
                value[1].seek(0)
            except Exception:
                pass


def _close_files(files):
    for value in (files or {}).values():
        if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "close"):
            try:
                value[1].close()
            except Exception:
                pass


def _call_with_refresh(enrollment_id, user_id, operation, endpoint, method,
                       request_summary, call, files=None):
    """Call one enrollment-session endpoint with the documented retry-once path."""
    client = get_perch_client()
    token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
    started = datetime.now()
    refreshed = False
    try:
        result = call(client, token)
    except PerchTokenExpiredError:
        record_api_call(
            enrollment_id=enrollment_id, operation=operation, endpoint=endpoint,
            http_method=method, request_json=request_summary, response_json=None,
            status_code=403,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message="403 enrollment_token expired - refreshing and retrying once",
            initiated_by_user_id=user_id,
        )
        refreshed = True
        token_manager.refresh_existing_token(enrollment_id, user_id=user_id)
        token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
        _rewind_files(files)
        started = datetime.now()
        result = call(client, token)
    return result, refreshed, int((datetime.now() - started).total_seconds() * 1000)


def create_enrollment(enrollment_id, enrollment_payload, user_id=None):
    """POST /enroll using already-captured Dalton data and the stored utility bill."""
    capacity = latest_capacity_check(enrollment_id)
    if not capacity or not capacity.get("capacity_available"):
        raise PerchValidationError("A successful Perch capacity check is required before enrollment.")

    details = capacity.get("project_details") or {}
    customer_type, reason = choose_customer_type(details)
    payload = dict(enrollment_payload or {})
    payload["customer_type"] = customer_type
    payload["utility_name"] = capacity["utility_slug"]
    payload["zip_code"] = capacity["zip_code"]

    stored = query_one("SELECT perch_token_email FROM enrollments WHERE id = ?", (enrollment_id,))
    token_email = (stored["perch_token_email"] if stored else None) or ""
    if not payload.get("email_address") or payload["email_address"].strip().lower() != token_email.strip().lower():
        raise PerchValidationError(
            "Customer email must match the email used to open this Perch enrollment session.")

    accounts = payload.get("utility_accounts") or []
    if len(accounts) != 1:
        raise PerchValidationError("The current Dalton GUI supports exactly one utility account per enrollment.")
    acct = accounts[0]
    utility_row = utilities.by_slug(capacity["utility_slug"]) or {}
    expected_len = utility_row.get("account_number_length")
    account_number = str(acct.get("utility_account_number") or "").strip()
    if expected_len and (not account_number.isdigit() or len(account_number) != int(expected_len)):
        raise PerchValidationError(
            f"{utility_row.get('display_name') or capacity['utility_slug']} account numbers must be {expected_len} digits.")
    pod_error = utilities.validate_pod_id(capacity["utility_slug"], acct.get("secondary_account_identifier"))
    if pod_error:
        raise PerchValidationError(pod_error)

    form, files = build_enrollment_multipart(payload)
    request_summary = {
        "customer_type": customer_type,
        "customer_type_reason": reason,
        "utility_name": capacity["utility_slug"],
        "zip_code": capacity["zip_code"],
        "utility_account_count": len(accounts),
        "utility_bill_count": len(acct.get("utility_bills") or []),
        "account_last4": account_number[-4:] if account_number else None,
    }
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "create_enrollment", PATH_ENROLL, "POST",
            request_summary,
            lambda client, token: client.create_enrollment(token, form, files),
            files=files,
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="create_enrollment", endpoint=PATH_ENROLL,
            http_method="POST", request_json=request_summary,
            response_json={"next_step": result.get("next_step"), "response_shape": result.get("response_shape")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "customer_type": customer_type,
            "next_step_url": result.get("next_step"),
            "response_shape": result.get("response_shape"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="create_enrollment", endpoint=PATH_ENROLL,
            http_method="POST", request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
    finally:
        _close_files(files)


def submit_proof_docs(enrollment_id, utility_account_number, documents, user_id=None):
    """POST /lmi/proof_docs using a stored Dalton document path and explicit metadata."""
    files, proof_doc = build_proof_docs_multipart(utility_account_number, documents)
    safe_meta = {
        "utility_account_last4": str(utility_account_number)[-4:],
        "documents": [
            {k: d.get(k) for k in ("source_type", "name_on_document", "relationship", "document_type")}
            for d in documents
        ],
    }
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "submit_proof_docs", PATH_LMI_PROOF_DOCS, "POST",
            safe_meta,
            lambda client, token: client.submit_proof_docs(token, files),
            files=files,
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_proof_docs", endpoint=PATH_LMI_PROOF_DOCS,
            http_method="POST", request_json=safe_meta,
            response_json={"next_step": result.get("next_step"), "response_shape": result.get("response_shape")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "next_step_url": result.get("next_step"),
            "response_shape": result.get("response_shape"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_proof_docs", endpoint=PATH_LMI_PROOF_DOCS,
            http_method="POST", request_json=safe_meta, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
    finally:
        _close_files(files)


def generate_contracts(enrollment_id, user_id=None):
    """POST /contracts. Returns raw URLs only to the immediate server caller.

    The API-call audit stores the URL-free normalized contract list. Nothing in
    this function persists or logs a presigned URL.
    """
    request_summary = {"body": None}
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "generate_contracts", PATH_CONTRACTS, "POST",
            request_summary,
            lambda client, token: client.generate_contracts(token),
        )
        safe = contracts_safe(result)
        record_api_call(
            enrollment_id=enrollment_id, operation="generate_contracts", endpoint=PATH_CONTRACTS,
            http_method="POST", request_json=request_summary,
            response_json={"contracts": safe, "next_step": result.get("next_step")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        result["token_was_refreshed"] = refreshed
        return result
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="generate_contracts", endpoint=PATH_CONTRACTS,
            http_method="POST", request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
