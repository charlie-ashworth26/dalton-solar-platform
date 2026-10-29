from flask import Blueprint, request, jsonify, g

from db import query_one, execute
from auth import (
    verify_password, issue_token, require_auth,
    issue_customer_token, require_customer_auth, normalize_email,
)
from services import audit

bp = Blueprint("auth_routes", __name__, url_prefix="/api/auth")


@bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = query_one("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
    if not user or not verify_password(password, user["password_hash"]):
        audit.log("login_failed", details={"email": email}, ip_address=request.remote_addr)
        return jsonify({"error": "Invalid email or password"}), 401

    token = issue_token(user)
    audit.log("login", user_id=user["id"], ip_address=request.remote_addr)
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "role": user["role"], "full_name": user["full_name"]},
    })


@bp.route("/me", methods=["GET"])
@require_auth
def me():
    u = g.current_user
    return jsonify({"id": u["id"], "email": u["email"], "role": u["role"], "full_name": u["full_name"]})


# ─────────────── Customer agreement sign-in ───────────────

@bp.route("/customer-login", methods=["POST"])
def customer_login():
    """Authenticate a CUSTOMER against customers.password_hash.

    ROOT CAUSE THIS REPLACES: the browser previously "authenticated" customers
    entirely client-side, searching a legacy in-memory array that has been empty
    since the Perch refactor and comparing plaintext passwords. It never called
    the backend, so it could never succeed.

    The password itself was always stored correctly (hashed via
    enrollment_routes on the contact step) - only the login path was fake.
    """
    data = request.get_json(force=True, silent=True) or {}
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Enter your email and password to continue."}), 400

    # Emails are compared normalized on BOTH sides - a customer recorded as
    # "Charlie+Dalton1@Example.com" must still match "charlie+dalton1@example.com".
    customer = query_one(
        "SELECT * FROM customers WHERE lower(trim(email)) = ? ORDER BY id DESC LIMIT 1",
        (email,))

    generic = "Invalid email or password."
    if not customer or not customer["password_hash"]:
        # Same message whether the customer is unknown or has no password set,
        # so this cannot be used to enumerate customers.
        audit.log("customer_login_failed", details={"reason": "no_credential"},
                  ip_address=request.remote_addr)
        return jsonify({"error": generic}), 401

    if not verify_password(password, customer["password_hash"]):
        audit.log("customer_login_failed", user_id=None,
                  details={"reason": "bad_password", "customer_id": customer["id"]},
                  ip_address=request.remote_addr)
        return jsonify({"error": generic}), 401

    enrollment = query_one(
        "SELECT * FROM enrollments WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
        (customer["id"],))
    if not enrollment:
        return jsonify({"error": "No agreement was found for this account."}), 404

    token = issue_customer_token(customer, enrollment["id"])
    audit.log("customer_login", enrollment_id=enrollment["id"],
              details={"customer_id": customer["id"]}, ip_address=request.remote_addr)

    return jsonify({
        "token": token,
        "customer": {
            "id": customer["id"],
            "first_name": customer["first_name"],
            "last_name": customer["last_name"],
            "email": customer["email"],
        },
        "enrollment_id": enrollment["id"],
        "enrollment_code": enrollment["enrollment_code"],
    })


@bp.route("/customer-me", methods=["GET"])
@require_customer_auth
def customer_me():
    """The signed-in customer's own agreement. Scoped to their enrollment only."""
    from services.perch import workflow as perch_workflow
    enrollment_id = g.customer_enrollment_id
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return jsonify({"error": "Agreement not found"}), 404

    # Defence in depth: the token carries the enrollment id AND we re-verify the
    # enrollment still belongs to this customer.
    if enrollment["customer_id"] != g.current_customer["id"]:
        return jsonify({"error": "Forbidden"}), 403

    wf = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id = ?", (enrollment_id,))
    step_key = wf["current_step_key"] if wf else None
    project = query_one("SELECT * FROM projects WHERE id = ?", (enrollment["project_id"],)) \
        if enrollment["project_id"] else None
    address = query_one("SELECT * FROM service_addresses WHERE id = ?",
                        (enrollment["service_address_id"],)) \
        if enrollment["service_address_id"] else None
    utility = query_one("SELECT * FROM utility_accounts WHERE id = ?",
                        (enrollment["utility_account_id"],)) \
        if enrollment["utility_account_id"] else None

    return jsonify({
        "enrollment_id": enrollment["id"],
        "enrollment_code": enrollment["enrollment_code"],
        "customer": {
            "first_name": g.current_customer["first_name"],
            "last_name": g.current_customer["last_name"],
            "email": g.current_customer["email"],
        },
        # Savings comes from the SAME persisted-Perch resolver the rep view uses,
        # so a customer can never be shown a different (or invented) figure.
        "program_savings": _customer_program_savings(enrollment_id),
        # ── FIELDS THE CUSTOMER VIEW ALREADY READ BUT NEVER RECEIVED ────────
        #
        # openCustomerContracts() builds its summary from me.utility_name,
        # me.service_address and me.project_name, and renders a completed
        # customer's agreement list from
        # (me.workflow_last_response || {}).contracts. None of the four were
        # serialized, so every row the customer saw was blank and a completed
        # customer was shown an agreement card listing NO agreements. The data
        # was all persisted; it simply never left here.
        #
        # utility_name goes through the ONE shared resolver, so the customer
        # can never be shown the raw Perch slug.
        "utility_name": _customer_utility_name(enrollment, utility),
        "service_address": _format_service_address(address),
        "project_name": project["name"] if project else None,
        "workflow_step_key": step_key,
        "workflow_step_label": perch_workflow.step_label(step_key) if step_key else None,
        "workflow_is_terminal": perch_workflow.is_terminal(step_key),
        "workflow_is_blocked": perch_workflow.is_blocked(step_key),
        # WHEN UBS recorded this status - same field, same meaning and the same
        # honest label as the rep detail payload.
        "workflow_updated_at": wf["updated_at"] if wf else None,
        # CONTRACT NAMES ONLY. The rep payload returns the whole persisted
        # workflow response; a customer gets the single key their own view
        # consumes and nothing else. No document URL is persisted anywhere, so
        # none can leak through here.
        "workflow_last_response": {"contracts": _persisted_contract_names(wf)},
    })


def _format_service_address(address):
    """"street, unit, city, ST zip" from the persisted row, or None.

    The STATE comes FROM THE ROW. It is never defaulted to NY - a Virginia
    customer must not be shown a New York address.
    """
    if not address:
        return None
    a = dict(address)
    line = ", ".join([p for p in (a.get("street"), a.get("unit"), a.get("city")) if p])
    tail = " ".join([p for p in (a.get("state"), a.get("zip")) if p])
    return ", ".join([p for p in (line, tail) if p]) or None


def _customer_utility_name(enrollment, utility):
    """The published utility name for this customer's enrollment, or None.

    ONE DISPLAY-NAME SYSTEM: delegates to the same resolver the rep-facing
    payload uses. enrollments.utility_name holds the slug chosen at
    Availability and exists from the first step; utility_accounts only exists
    once the bill step has run, so it is the fallback, not the source.
    """
    from services.perch import utilities

    slug = enrollment["utility_name"] if "utility_name" in enrollment.keys() else None
    if not slug and utility:
        slug = utility["utility_name"]
    if not slug:
        return None
    try:
        return utilities.display_name_for_slug(slug)
    except Exception:
        return slug


def _persisted_contract_names(wf):
    """The contract metadata UBS already persisted, or []. Never a URL.

    Perch issues time-limited document URLs and UBS deliberately stores none,
    so this can only ever carry names - which is exactly what the read-only
    agreement list renders.
    """
    import json as _json

    if not wf or not wf["last_response_json"]:
        return []
    try:
        body = _json.loads(wf["last_response_json"]) or {}
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    out = []
    for c in (body.get("contracts") or []):
        if isinstance(c, dict):
            name = c.get("contract_name") or c.get("name")
            if name:
                out.append({"contract_name": name})
        elif c:
            out.append({"contract_name": str(c)})
    return out


@bp.route("/me/profile", methods=["PATCH"])
@require_auth
def update_own_profile():
    """Change YOUR OWN display name. Deliberately narrow.

    Exists because the seeded admin shipped with the placeholder name
    "Jordan Ellis" and there was no way to correct it without SQL. Any
    authenticated staff user may fix their own name; nobody can change anyone
    else's, and no other field is editable here.

    CANNOT change: role, email, is_active, password, or any other user - and it
    touches nothing that carries enrollment ownership or history (those
    reference users.id, never the name).
    """
    data = request.get_json(force=True, silent=True) or {}
    if "full_name" not in data:
        return jsonify({"error": "Send full_name to update your display name."}), 400

    full_name = (data.get("full_name") or "").strip()
    if not full_name:
        return jsonify({"error": "Display name cannot be blank."}), 400
    if len(full_name) > 120:
        return jsonify({"error": "Display name must be 120 characters or fewer."}), 400

    # Ignore anything else that was sent: this endpoint edits ONE field.
    execute("UPDATE users SET full_name = ?, updated_at = datetime('now') WHERE id = ?",
            (full_name, g.current_user["id"]))
    audit.log("user_profile_updated", user_id=g.current_user["id"],
              details={"field": "full_name"}, ip_address=request.remote_addr)

    row = query_one("SELECT id, email, role, full_name, is_active FROM users WHERE id = ?",
                    (g.current_user["id"],))
    return jsonify(dict(row))


def _customer_program_savings(enrollment_id):
    """Delegates to the single resolver in enrollment_routes so the rep-facing
    and customer-facing savings figures can never diverge."""
    from routes.enrollment_routes import _program_savings
    return _program_savings(enrollment_id)


# Destination per staff role. Server-authoritative: the client is TOLD where to
# go, it never asks. Unknown roles fall back to the dashboard, which every staff
# role can already reach.
STAFF_DESTINATIONS = {
    "sales_rep": "dashboard",
    "admin": "dashboard",
    "qa_reviewer": "dashboard",
    "developer": "dashboard",
}


@bp.route("/signin", methods=["POST"])
def unified_signin():
    """ONE login for staff and customers.

    Reps and customers previously had separate screens, and a customer had to
    notice a small "sign into your agreement" link to reach theirs. This
    authenticates either account type from one form.

    THIS IS A UNIFIED LOGIN, NOT UNIFIED PERMISSIONS. It issues exactly the same
    tokens the two original endpoints issue, through the same functions:
      staff    -> issue_token()          scope "staff"
      customer -> issue_customer_token() scope "customer" + enrollment_id
    require_auth still rejects customer scope and require_customer_auth still
    rejects staff scope, so no route becomes reachable that was not before.

    PRECEDENCE: staff is checked first. The same address can legitimately exist
    in both tables (a rep enrolling their own household), and a fixed order is
    the only way the outcome stays deterministic. It is documented in the
    response as `account_type` so the client never has to guess.

    ENUMERATION: every failure returns the SAME message and status, whether the
    email is unknown, belongs to a customer with no password, or belongs to a
    customer with no enrollment. The response never reveals which table an
    address lives in.
    """
    data = request.get_json(force=True, silent=True) or {}
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    GENERIC = "Invalid email or password."

    if not email or not password:
        return jsonify({"error": "Enter your email and password to continue."}), 400

    # ---- 1. staff -------------------------------------------------------
    user = query_one("SELECT * FROM users WHERE lower(trim(email)) = ? AND is_active = 1",
                     (email,))
    if user and verify_password(password, user["password_hash"]):
        token = issue_token(user)
        audit.log("login", user_id=user["id"], ip_address=request.remote_addr,
                  details={"via": "unified"})
        return jsonify({
            "account_type": "staff",
            "token": token,
            "destination": STAFF_DESTINATIONS.get(user["role"], "dashboard"),
            "user": {"id": user["id"], "email": user["email"], "role": user["role"],
                     "full_name": user["full_name"]},
        })

    # ---- 2. customer ----------------------------------------------------
    customer = query_one(
        "SELECT * FROM customers WHERE lower(trim(email)) = ? ORDER BY id DESC LIMIT 1",
        (email,))
    if customer and customer["password_hash"] and verify_password(password, customer["password_hash"]):
        enrollment = query_one(
            "SELECT * FROM enrollments WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
            (customer["id"],))
        if not enrollment:
            # Correct credentials but nothing to show. Kept GENERIC so a valid
            # password cannot confirm an account that has no enrollment.
            audit.log("customer_login_failed", details={"reason": "no_enrollment",
                                                        "customer_id": customer["id"]},
                      ip_address=request.remote_addr)
            return jsonify({"error": GENERIC}), 401
        token = issue_customer_token(customer, enrollment["id"])
        audit.log("customer_login", enrollment_id=enrollment["id"],
                  details={"customer_id": customer["id"], "via": "unified"},
                  ip_address=request.remote_addr)
        return jsonify({
            "account_type": "customer",
            "token": token,
            "destination": "customer_portal",
            "customer": {"id": customer["id"], "first_name": customer["first_name"],
                         "last_name": customer["last_name"], "email": customer["email"]},
            "enrollment_id": enrollment["id"],
            "enrollment_code": enrollment["enrollment_code"],
        })

    # ---- 3. neither -----------------------------------------------------
    audit.log("login_failed", details={"via": "unified"}, ip_address=request.remote_addr)
    return jsonify({"error": GENERIC}), 401
