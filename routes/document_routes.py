import os
import json
import uuid

from flask import Blueprint, request, jsonify, g, send_file

from db import query_one, execute
from auth import require_auth, require_role
from helpers import validate_upload, to_json, resolve_stored_path
from services import extraction, lmi_validation, audit

bp = Blueprint("document_routes", __name__, url_prefix="/api/enrollments")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@bp.route("/<int:enrollment_id>/documents", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def upload_document(enrollment_id):
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return jsonify({"error": "Not found"}), 404

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    category = request.form.get("category", "other")
    if category not in ("utility_bill", "lmi_document", "other"):
        return jsonify({"error": "Invalid category for upload"}), 400

    raw = file.read()
    error = validate_upload(file.filename, len(raw))
    if error:
        return jsonify({"error": error}), 400

    enrollment_dir = os.path.join(UPLOAD_DIR, str(enrollment_id))
    os.makedirs(enrollment_dir, exist_ok=True)
    stored_name = f"{category}_{uuid.uuid4().hex[:12]}_{file.filename}".replace(" ", "_")
    stored_path_abs = os.path.join(enrollment_dir, stored_name)
    with open(stored_path_abs, "wb") as f:
        f.write(raw)
    stored_path_rel = os.path.join("uploads", str(enrollment_id), stored_name)

    extracted = None
    confidence = None
    validation_row_id = None

    if category == "utility_bill":
        text = extraction.get_text(stored_path_abs, file.mimetype)
        fields = extraction.parse_utility_bill(text)
        extracted = {k: v["value"] for k, v in fields.items()}
        confidences = [v["confidence"] for v in fields.values()]
        confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0

    cur = execute(
        """INSERT INTO documents (enrollment_id, doc_category, original_filename, stored_path, mime_type,
           file_size, uploaded_by_user_id, extracted_data_json, extraction_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, category, file.filename, stored_path_rel, file.mimetype, len(raw),
         g.current_user["id"], to_json(extracted), confidence),
    )
    document_id = cur.lastrowid

    if category == "utility_bill":
        missing = [f for f in ("account_number", "account_holder", "service_street") if f not in (extracted or {})]
        execute(
            """INSERT INTO validation_results (enrollment_id, document_id, validation_type, classification, confidence, reasons_json, missing_info_json, mismatch_warnings_json)
               VALUES (?, ?, 'utility_bill', ?, ?, ?, ?, ?)""",
            (enrollment_id, document_id,
             "likely_valid" if not missing else "needs_manual_review", confidence or 0,
             to_json(["Automated extraction from uploaded bill"]), to_json(missing), to_json([])),
        )

    if category == "lmi_document":
        text = extraction.get_text(stored_path_abs, file.mimetype)
        holder = query_one(
            "SELECT c.first_name, c.last_name FROM customers c JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?",
            (enrollment_id,),
        )
        holder_name = f"{holder['first_name']} {holder['last_name']}" if holder else None
        result = lmi_validation.validate_lmi_document(text, holder_name)
        cur2 = execute(
            """INSERT INTO validation_results (enrollment_id, document_id, validation_type, classification, confidence, reasons_json, missing_info_json, mismatch_warnings_json)
               VALUES (?, ?, 'lmi_document', ?, ?, ?, ?, ?)""",
            (enrollment_id, document_id, result["classification"], result["confidence"],
             to_json(result["reasons"]), to_json(result["missing_info"]), to_json(result["mismatch_warnings"])),
        )
        validation_row_id = cur2.lastrowid
        extracted = result

    audit.log("document_uploaded", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"category": category, "filename": file.filename}, ip_address=request.remote_addr)

    return jsonify({
        "document_id": document_id,
        "category": category,
        "extracted": extracted,
        "confidence": confidence,
        "validation_result_id": validation_row_id,
    }), 201


@bp.route("/<int:enrollment_id>/documents/<int:document_id>/correct", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def correct_extraction(enrollment_id, document_id):
    """Stores rep corrections separately from the raw extraction, per requirement #3."""
    data = request.get_json(force=True, silent=True) or {}
    execute("UPDATE documents SET corrected_data_json=? WHERE id=? AND enrollment_id=?",
            (to_json(data.get("corrected_fields", {})), document_id, enrollment_id))
    audit.log("extraction_corrected", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"document_id": document_id}, ip_address=request.remote_addr)
    return jsonify({"ok": True})


@bp.route("/<int:enrollment_id>/documents/<int:document_id>/download", methods=["GET"])
@require_auth
def download_document(enrollment_id, document_id):
    doc = query_one("SELECT * FROM documents WHERE id = ? AND enrollment_id = ?", (document_id, enrollment_id))
    if not doc:
        return jsonify({"error": "Not found"}), 404
    if g.current_user["role"] == "sales_rep":
        enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
        rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
        if not rep or enrollment["sales_rep_id"] != rep["id"]:
            return jsonify({"error": "Forbidden"}), 403

    abs_path = resolve_stored_path(doc["stored_path"])
    if not os.path.exists(abs_path):
        return jsonify({"error": "File missing on disk"}), 410

    audit.log("document_accessed", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"document_id": document_id}, ip_address=request.remote_addr)
    return send_file(abs_path, as_attachment=True, download_name=doc["original_filename"] or f"document-{document_id}")
