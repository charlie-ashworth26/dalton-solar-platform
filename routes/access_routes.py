"""Magic-link customer access.

A secure ENTRY PATH into the existing customer-authenticated flow - not a second
auth system. Redemption ends in a call to auth.issue_customer_token(), the same
issuer normal login uses, producing an identical customer JWT. No new scope, no
new permission model, no guard changes.

SECRET HANDLING
  * token is secrets.token_urlsafe(32) - 256 bits
  * the database stores SHA-256(token) ONLY; the raw value is never persisted,
    logged or audited
  * the link carries the token in a URL FRAGMENT (/access#<token>), which
    browsers never transmit, so it cannot reach reverse-proxy or access logs
  * redemption POSTs the raw token in the JSON BODY - never a path or query
    string
  * the customer JWT is returned in a JSON body, never in a URL
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, g

from db import query_one, execute, transaction
from auth import require_auth, require_role, issue_customer_token
from services import audit
from services.authz import visible_enrollment
from services.perch import workflow as perch_workflow

bp = Blueprint("access", __name__)

TOKEN_TTL_HOURS = 24

# One generic customer-facing failure for EVERY rejection - expired, used,
# revoked, malformed, unknown, wrong customer, or completed enrollment. A
# specific message would let a caller probe which enrollments or customers
# exist.
GENERIC_FAILURE = ("This link is no longer valid. Ask your representative for a "
                   "new one, or sign in with your email and password.")


def _hash_token(raw: str) -> str:
    """SHA-256 hex of the raw token. The only form ever stored or compared."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ", "seconds")


def _public_base_url():
    """Base URL for the customer link.

    Falls back to the request host so this works in local and staging without
    configuration. No credential or deployment config is read or modified.
    """
    configured = (os.environ.get("DALTON_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    return request.host_url.rstrip("/")


@bp.route("/api/enrollments/<int:enrollment_id>/customer-link", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_customer_link(enrollment_id):
    """Rep creates a magic link for THIS enrollment's customer.

    Ownership-scoped through the same visible_enrollment() every other
    enrollment route uses, so a rep cannot mint a link for someone else's
    enrollment.
    """
    enrollment, err = visible_enrollment(enrollment_id)
    if err:
        return err

    if not enrollment["customer_id"]:
        return jsonify({"error": "Add the customer's details before creating a "
                                 "customer link."}), 400

    # A completed enrollment has nothing left for the customer to do, and a link
    # to it must never be usable. Refuse at creation as well as at redemption.
    state = perch_workflow.get_state(enrollment_id)
    step_key = (state or {}).get("current_step_key")
    if step_key and perch_workflow.is_terminal(step_key):
        return jsonify({"error": "This enrollment is already complete."}), 409

    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                  + timedelta(hours=TOKEN_TTL_HOURS)).isoformat(" ", "seconds")

    with transaction():
        # Issuing a new link REVOKES every prior active link for this
        # enrollment, so only one link is ever live at a time.
        execute(
            """UPDATE customer_access_tokens
               SET revoked_at = ?
               WHERE enrollment_id = ? AND used_at IS NULL AND revoked_at IS NULL""",
            (_now_iso(), enrollment_id),
        )
        execute(
            """INSERT INTO customer_access_tokens
               (token_hash, enrollment_id, customer_id, expires_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (token_hash, enrollment_id, enrollment["customer_id"], expires_at,
             g.current_user["id"]),
        )

    # Audit records THAT a link was issued - never the token or its hash.
    audit.log("customer_link_created", enrollment_id=enrollment_id,
              user_id=g.current_user["id"], details={"expires_at": expires_at},
              ip_address=request.remote_addr)

    # FRAGMENT, not a path segment or query parameter: browsers do not send the
    # fragment to the server, so the token cannot appear in access logs.
    url = f"{_public_base_url()}/access#{raw}"
    response = jsonify({"url": url, "expires_at": expires_at})
    response.headers["Cache-Control"] = "no-store, private"
    return response, 201


@bp.route("/api/customer-access/redeem", methods=["POST"])
def redeem_customer_link():
    """Customer redeems a magic link.

    The raw token arrives in the JSON BODY. Every failure returns the SAME
    generic message and status so this endpoint cannot be used to discover which
    enrollments or customers exist.
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("token") or ""

    def fail():
        # Deliberately identical for every cause. The raw token is never
        # included in the audit payload.
        audit.log("customer_link_redeem_failed", ip_address=request.remote_addr)
        return jsonify({"error": GENERIC_FAILURE}), 401

    if not isinstance(raw, str) or not (16 <= len(raw) <= 512):
        return fail()

    row = query_one("SELECT * FROM customer_access_tokens WHERE token_hash = ?",
                    (_hash_token(raw),))
    if not row:
        return fail()
    # Constant-time confirmation of the hash match.
    if not hmac.compare_digest(row["token_hash"], _hash_token(raw)):
        return fail()
    if row["used_at"] or row["revoked_at"]:
        return fail()
    if (row["expires_at"] or "") <= _now_iso():
        return fail()

    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?",
                           (row["enrollment_id"],))
    if not enrollment:
        return fail()
    # The token's customer must still be THE customer on that enrollment. No
    # falling back to whoever is currently attached.
    if enrollment["customer_id"] != row["customer_id"]:
        return fail()

    customer = query_one("SELECT * FROM customers WHERE id = ?", (row["customer_id"],))
    if not customer:
        return fail()

    # A completed enrollment can never be re-entered, even with a live token.
    state = perch_workflow.get_state(row["enrollment_id"])
    step_key = (state or {}).get("current_step_key")
    if step_key and perch_workflow.is_terminal(step_key):
        return fail()

    # SINGLE USE: burn it before issuing, so a replayed request finds it spent.
    execute("UPDATE customer_access_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (_now_iso(), row["id"]))

    # The EXISTING issuer - identical JWT to normal customer login.
    token = issue_customer_token(customer, row["enrollment_id"])

    audit.log("customer_link_redeemed", enrollment_id=row["enrollment_id"],
              details={"customer_id": customer["id"]}, ip_address=request.remote_addr)

    response = jsonify({
        "token": token,                       # JSON body only - never a URL
        "enrollment_id": row["enrollment_id"],
        "customer": {"id": customer["id"], "first_name": customer["first_name"],
                     "last_name": customer["last_name"], "email": customer["email"]},
    })
    response.headers["Cache-Control"] = "no-store, private"
    return response
