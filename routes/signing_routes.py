import os
import base64
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute
from auth import require_auth, require_role, generate_session_token
from helpers import json_or_none, resolve_stored_path
from services import status_machine, audit, documents as doc_service
from services.authz import visible_enrollment

bp = Blueprint("signing_routes", __name__)

SESSION_LIFETIME_HOURS = 72

FIELD_SPEC = {
    "subscription_agreement": ("subscription_signature", "signature"),
    "cdg_disclosure": ("disclosure_signature", "signature"),
    "income_survey": ("income_survey_initial", "initial"),
    "esign_consent": ("esign_initial", "initial"),
    "credit_contact_consent": ("credit_contact_initial", "initial"),
    "terms_privacy": ("terms_privacy_initial", "initial"),
}

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")


def _required_fields(enrollment_id):
    agreements = query("SELECT * FROM agreements WHERE enrollment_id = ?", (enrollment_id,))
    fields = []
    for a in agreements:
        spec = FIELD_SPEC.get(a["document_type"])
        if spec:
            fields.append({"agreement_id": a["id"], "document_type": a["document_type"],
                            "field_key": spec[0], "field_type": spec[1]})
    return fields


# ─────────────────────────── Staff: create a session ───────────────────────────

@bp.route("/api/enrollments/<int:enrollment_id>/signing-session", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_signing_session(enrollment_id):
    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
    customer = query_one("SELECT * FROM customers WHERE id = ?", (enrollment["customer_id"],))
    if not query("SELECT id FROM agreements WHERE enrollment_id = ?", (enrollment_id,)):
        return jsonify({"error": "No agreements generated yet — call /agreements/generate first"}), 400

    token = generate_session_token()
    expires_at = (datetime.now() + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
    execute(
        "INSERT INTO signing_sessions (enrollment_id, token, signer_name, signer_email, expires_at) VALUES (?, ?, ?, ?, ?)",
        (enrollment_id, token, f"{customer['first_name']} {customer['last_name']}" if customer else None,
         customer["email"] if customer else None, expires_at),
    )
    try:
        status_machine.transition(enrollment_id, "Signature Pending", user_id=g.current_user["id"],
                                   reason="Signing session created", ip_address=request.remote_addr)
    except status_machine.InvalidTransition:
        pass  # already past this point (e.g. resending a link) — non-fatal
    audit.log("signing_session_created", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              ip_address=request.remote_addr)
    return jsonify({"token": token, "expires_at": expires_at}), 201


# ─────────────────────────── Customer: session-token auth ───────────────────────────

def _session_or_error(token):
    session = query_one("SELECT * FROM signing_sessions WHERE token = ?", (token,))
    if not session:
        return None, (jsonify({"error": "Signing link not found"}), 404)
    if session["status"] == "expired" or datetime.fromisoformat(session["expires_at"]) < datetime.now():
        if session["status"] != "expired":
            execute("UPDATE signing_sessions SET status='expired' WHERE id=?", (session["id"],))
            execute("INSERT INTO signature_events (enrollment_id, signing_session_id, event_type) VALUES (?, ?, 'session_expired')",
                    (session["enrollment_id"], session["id"]))
        return None, (jsonify({"error": "This signing link has expired. Ask your rep to send a new one."}), 410)
    return session, None


@bp.route("/api/signing-sessions/<token>", methods=["GET"])
def get_signing_session(token):
    session, err = _session_or_error(token)
    if err:
        return err
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (session["enrollment_id"],))
    customer = query_one("SELECT * FROM customers WHERE id = ?", (enrollment["customer_id"],))
    address = query_one("SELECT * FROM service_addresses WHERE id = ?", (enrollment["service_address_id"],))
    utility = query_one("SELECT * FROM utility_accounts WHERE id = ?", (enrollment["utility_account_id"],))
    project = query_one("SELECT * FROM projects WHERE id = ?", (enrollment["project_id"],))
    agreements = query("SELECT * FROM agreements WHERE enrollment_id = ?", (session["enrollment_id"],))
    completed = query("SELECT field_key FROM signatures WHERE signing_session_id = ?", (session["id"],))
    completed_keys = {r["field_key"] for r in completed}

    required = _required_fields(session["enrollment_id"])
    for f in required:
        f["completed"] = f["field_key"] in completed_keys

    execute("INSERT INTO signature_events (enrollment_id, signing_session_id, event_type, ip_address) VALUES (?, ?, 'session_opened', ?)",
            (session["enrollment_id"], session["id"], request.remote_addr))

    return jsonify({
        "enrollment_code": enrollment["enrollment_code"],
        "status": enrollment["status"],
        "customer_name": f"{customer['first_name']} {customer['last_name']}" if customer else None,
        "service_address": f"{address['street']}, {address['city']}, {address['state']} {address['zip']}" if address else None,
        "utility": utility["utility_name"] if utility else None,
        "project_name": project["name"] if project else None,
        "savings_pct": project["savings_pct"] if project else None,
        "agreements": [dict(a) for a in agreements],
        "required_fields": required,
        "expires_at": session["expires_at"],
        "session_status": session["status"],
    })


@bp.route("/api/signing-sessions/<token>/documents/<int:agreement_id>", methods=["GET"])
def download_session_document(token, agreement_id):
    session, err = _session_or_error(token)
    if err:
        return err
    agreement = query_one("SELECT * FROM agreements WHERE id = ? AND enrollment_id = ?", (agreement_id, session["enrollment_id"]))
    if not agreement:
        return jsonify({"error": "Not found"}), 404
    document = query_one("SELECT * FROM documents WHERE id = ?", (agreement["generated_document_id"],))
    from flask import send_file
    abs_path = resolve_stored_path(document["stored_path"])
    execute("INSERT INTO signature_events (enrollment_id, signing_session_id, event_type, field_key, ip_address) VALUES (?, ?, 'document_viewed', ?, ?)",
            (session["enrollment_id"], session["id"], agreement["document_type"], request.remote_addr))
    return send_file(abs_path, mimetype="application/pdf")


@bp.route("/api/signing-sessions/<token>/fields/<field_key>", methods=["POST"])
def submit_field(token, field_key):
    session, err = _session_or_error(token)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    method = data.get("method")
    if method not in ("typed", "drawn", "adopted"):
        return jsonify({"error": "method must be typed, drawn, or adopted"}), 400

    required = _required_fields(session["enrollment_id"])
    match = next((f for f in required if f["field_key"] == field_key), None)
    if not match:
        return jsonify({"error": f"'{field_key}' is not a required field on this packet"}), 400

    value_image_path = None
    value_text = None
    if method == "drawn":
        image_b64 = data.get("value_image_base64", "")
        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]
        sig_dir = os.path.join(UPLOAD_DIR, str(session["enrollment_id"]), "signatures")
        os.makedirs(sig_dir, exist_ok=True)
        fname = f"{field_key}.png"
        abs_path = os.path.join(sig_dir, fname)
        with open(abs_path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        value_image_path = os.path.join("uploads", str(session["enrollment_id"]), "signatures", fname)
    else:
        value_text = data.get("value_text", "")
        if not value_text:
            return jsonify({"error": "value_text is required for typed/adopted signatures"}), 400

    execute(
        """INSERT INTO signatures (enrollment_id, agreement_id, signing_session_id, field_key, field_type, method,
           value_text, value_image_path, signer_name, signer_email, page_label)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session["enrollment_id"], match["agreement_id"], session["id"], field_key, match["field_type"], method,
         value_text, value_image_path, data.get("signer_name") or session["signer_name"],
         data.get("signer_email") or session["signer_email"], match["document_type"]),
    )
    execute(
        "INSERT INTO signature_events (enrollment_id, signing_session_id, event_type, field_key, ip_address) VALUES (?, ?, 'field_completed', ?, ?)",
        (session["enrollment_id"], session["id"], field_key, request.remote_addr),
    )
    return jsonify({"ok": True, "field_key": field_key}), 201


@bp.route("/api/signing-sessions/<token>/complete", methods=["POST"])
def complete_session(token):
    session, err = _session_or_error(token)
    if err:
        return err
    enrollment_id = session["enrollment_id"]
    required = _required_fields(enrollment_id)
    completed = {r["field_key"] for r in query("SELECT field_key FROM signatures WHERE signing_session_id = ?", (session["id"],))}
    missing = [f["field_key"] for f in required if f["field_key"] not in completed]
    if missing:
        return jsonify({"error": "Not all required fields are signed", "missing_fields": missing}), 400

    now_iso = datetime.now().isoformat()
    execute("UPDATE signing_sessions SET status='completed', completed_at=? WHERE id=?", (now_iso, session["id"]))
    execute("UPDATE agreements SET status='signed' WHERE enrollment_id=?", (enrollment_id,))
    execute(
        "INSERT INTO signature_events (enrollment_id, signing_session_id, event_type, ip_address) VALUES (?, ?, 'session_completed', ?)",
        (enrollment_id, session["id"], request.remote_addr),
    )

    # Signature certificate
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    customer = query_one("SELECT * FROM customers WHERE id = ?", (enrollment["customer_id"],))
    sigs = query("SELECT * FROM signatures WHERE enrollment_id = ? ORDER BY id", (enrollment_id,))
    gen_dir = os.path.join(doc_service.STORAGE_DIR, str(enrollment_id))
    os.makedirs(gen_dir, exist_ok=True)
    cert_path = os.path.join(gen_dir, "signature-certificate.pdf")
    doc_service.generate_signature_certificate(
        {"enrollment_code": enrollment["enrollment_code"], "customer_name": f"{customer['first_name']} {customer['last_name']}",
         "customer_email": customer["email"], "session_completed_at": now_iso, "ip_address": request.remote_addr},
        [dict(s) for s in sigs], cert_path,
    )
    doc_cur = execute(
        """INSERT INTO documents (enrollment_id, doc_category, original_filename, stored_path, mime_type, file_size)
           VALUES (?, 'signature_certificate', 'signature-certificate.pdf', ?, 'application/pdf', ?)""",
        (enrollment_id, os.path.join("storage", "generated", str(enrollment_id), "signature-certificate.pdf"),
         os.path.getsize(cert_path)),
    )

    status_machine.transition(enrollment_id, "Signed", reason="Customer completed signing session", ip_address=request.remote_addr)
    audit.log("signing_completed", enrollment_id=enrollment_id, ip_address=request.remote_addr)

    return jsonify({"ok": True, "status": "Signed", "signature_certificate_document_id": doc_cur.lastrowid})
