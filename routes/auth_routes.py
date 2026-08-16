from flask import Blueprint, request, jsonify, g

from db import query_one
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

    return jsonify({
        "enrollment_id": enrollment["id"],
        "enrollment_code": enrollment["enrollment_code"],
        "customer": {
            "first_name": g.current_customer["first_name"],
            "last_name": g.current_customer["last_name"],
            "email": g.current_customer["email"],
        },
        "project_name": project["name"] if project else None,
        "workflow_step_key": step_key,
        "workflow_step_label": perch_workflow.step_label(step_key) if step_key else None,
        "workflow_is_terminal": perch_workflow.is_terminal(step_key),
        "workflow_is_blocked": perch_workflow.is_blocked(step_key),
    })
