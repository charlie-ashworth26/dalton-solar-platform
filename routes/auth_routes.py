from flask import Blueprint, request, jsonify, g

from db import query_one
from auth import verify_password, issue_token, require_auth
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
