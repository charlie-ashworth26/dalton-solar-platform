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

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
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
