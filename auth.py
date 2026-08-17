"""
Auth: password hashing (PBKDF2-SHA256, stdlib-only — no bcrypt dependency needed),
JWT issuing/verification (PyJWT), and role-based access decorators.
"""
import hashlib
import hmac
import os
import secrets
import time
from functools import wraps

import jwt
from flask import request, jsonify, g

from db import query_one

# ─────────────── JWT signing secret ───────────────
# The development fallback below is PUBLIC — it is in the repository. Anyone who
# can read this file could forge a token for any user. That is acceptable on a
# laptop and unacceptable anywhere reachable from the internet, so a hosted
# deployment REFUSES TO BOOT rather than silently running forgeable.
#
# "Hosted" is inferred from DALTON_ENV, or from the platform variables Render
# sets automatically, so a deploy cannot accidentally look local.
DEV_JWT_SECRET = "dev-secret-change-in-production"


def _is_hosted_environment():
    env = (os.environ.get("DALTON_ENV") or "").strip().lower()
    if env in ("staging", "production", "hosted"):
        return True
    if env in ("local", "development", "dev", "test"):
        return False
    # Render sets these on every service; neither exists on a laptop.
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))


def _resolve_jwt_secret():
    secret = os.environ.get("JWT_SECRET")
    if _is_hosted_environment():
        if not secret or not secret.strip():
            raise RuntimeError(
                "JWT_SECRET is not set. A hosted Dalton deployment refuses to start "
                "without it, because the development fallback is public in the "
                "repository and every session token would be forgeable. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
        if secret.strip() == DEV_JWT_SECRET:
            raise RuntimeError(
                "JWT_SECRET is still the public development default. A hosted "
                "Dalton deployment refuses to start with it. Generate a unique "
                "value and set it in the Render dashboard.")
        if len(secret.strip()) < 32:
            raise RuntimeError(
                "JWT_SECRET is too short (minimum 32 characters) for a hosted "
                "deployment. Generate a stronger value.")
        return secret.strip()
    # Local development keeps working with no configuration at all.
    return (secret or DEV_JWT_SECRET)


JWT_SECRET = _resolve_jwt_secret()
JWT_ALG = "HS256"
JWT_EXPIRY_SECONDS = 60 * 60 * 8  # 8-hour staff session

PBKDF2_ITERATIONS = 260_000


# ─────────────────────────── Password hashing ───────────────────────────

def hash_password(plain_password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    try:
        algo, iterations, salt, hex_digest = stored_hash.split("$")
        iterations = int(iterations)
        dk = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(dk.hex(), hex_digest)
    except Exception:
        return False


# ─────────────────────────── JWT (staff auth) ───────────────────────────

def issue_token(user_row) -> str:
    payload = {
        # Explicit scope. require_auth REJECTS anything that is not "staff", so a
        # customer token can never reach a rep/admin/QA route.
        "scope": "staff",
        "sub": user_row["id"],
        "email": user_row["email"],
        "role": user_row["role"],
        "full_name": user_row["full_name"],
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str):
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])


def _extract_token():
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return None


def require_auth(fn):
    """Populates g.current_user from a valid Bearer token, or returns 401."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"error": "Missing bearer token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        # A customer token must never authenticate a staff route, even though it
        # is signed with the same key.
        if payload.get("scope") == "customer":
            return jsonify({"error": "This endpoint requires staff credentials"}), 403
        user = query_one("SELECT * FROM users WHERE id = ? AND is_active = 1", (payload["sub"],))
        if not user:
            return jsonify({"error": "User not found or inactive"}), 401
        g.current_user = dict(user)
        return fn(*args, **kwargs)
    return wrapper


def require_role(*allowed_roles):
    """Stack under @require_auth. Usage: @require_auth \n @require_role('admin','qa_reviewer')"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if g.current_user["role"] not in allowed_roles:
                return jsonify({"error": f"Role '{g.current_user['role']}' is not permitted to access this resource"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ─────────────────────────── Signing-session tokens (customer-facing) ───────────────────────────

def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


# ─────────────── Customer agreement access ───────────────
# Customers are rows in `customers`, NOT `users`. They authenticate against
# customers.password_hash (set during enrollment) and receive a token scoped to
# exactly ONE enrollment. These tokens cannot reach staff routes.

CUSTOMER_TOKEN_EXPIRY_SECONDS = 60 * 60 * 2   # shorter than a staff session


def normalize_email(value):
    """Single normalization rule for every email comparison: trim + lowercase.

    Enrollment previously stored whatever the rep typed while login lowercased
    its input, so a capitalised address could never match.
    """
    return (value or "").strip().lower()


def issue_customer_token(customer_row, enrollment_id) -> str:
    payload = {
        "scope": "customer",
        "sub": customer_row["id"],
        "customer_id": customer_row["id"],
        # Bound to ONE enrollment - the guard below refuses any other id.
        "enrollment_id": enrollment_id,
        "email": normalize_email(customer_row["email"]),
        "iat": int(time.time()),
        "exp": int(time.time()) + CUSTOMER_TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def require_customer_auth(fn):
    """Populates g.current_customer from a customer-scoped token, or 401/403.

    Sets g.customer_enrollment_id - routes MUST scope every query to it so one
    customer can never read another customer's enrollment.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"error": "Missing bearer token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Your session expired — please sign in again."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        if payload.get("scope") != "customer":
            return jsonify({"error": "This endpoint requires customer credentials"}), 403
        customer = query_one("SELECT * FROM customers WHERE id = ?", (payload.get("customer_id"),))
        if not customer:
            return jsonify({"error": "Customer not found"}), 401
        g.current_customer = dict(customer)
        g.customer_enrollment_id = payload.get("enrollment_id")
        return fn(*args, **kwargs)
    return wrapper


def require_staff_or_customer(fn):
    """Accepts EITHER a staff token or a customer token.

    Used by the Perch contract routes so the customer and the rep drive the
    SAME engine - there is deliberately no second contract implementation.

    Sets exactly one of g.current_user / g.current_customer, plus
    g.actor_user_id (None for a customer) and g.actor_description for auditing.
    Customer callers additionally get g.customer_enrollment_id; routes MUST
    check it against the requested enrollment.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"error": "Missing bearer token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Your session expired — please sign in again."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401

        g.current_user = None
        g.current_customer = None
        g.customer_enrollment_id = None

        if payload.get("scope") == "customer":
            customer = query_one("SELECT * FROM customers WHERE id = ?",
                                 (payload.get("customer_id"),))
            if not customer:
                return jsonify({"error": "Customer not found"}), 401
            g.current_customer = dict(customer)
            g.customer_enrollment_id = payload.get("enrollment_id")
            g.actor_user_id = None
            g.actor_description = f"customer:{customer['id']}"
            return fn(*args, **kwargs)

        user = query_one("SELECT * FROM users WHERE id = ? AND is_active = 1", (payload["sub"],))
        if not user:
            return jsonify({"error": "User not found or inactive"}), 401
        if user["role"] not in ("sales_rep", "admin"):
            return jsonify({"error": f"Role '{user['role']}' is not permitted here"}), 403
        g.current_user = dict(user)
        g.actor_user_id = user["id"]
        g.actor_description = f"user:{user['id']}"
        return fn(*args, **kwargs)
    return wrapper
