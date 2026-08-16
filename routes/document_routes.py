import os
import json
import uuid

from flask import Blueprint, request, jsonify, g, send_file

from db import query, query_one, execute
from auth import require_auth, require_role
from helpers import validate_upload, to_json, resolve_stored_path, BACKEND_ROOT
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

    extraction_status = None
    extraction_issues = []

    if category == "utility_bill":
        # HARDENED: extraction can no longer 500 or lose the upload. The file is
        # already written; the documents row is inserted below regardless.
        from services import extraction_engine as engine
        try:
            result = engine.get_extractor().extract([stored_path_abs], category=category)
        except Exception as e:
            result = engine.ExtractionResult(
                engine.ExtractionStatus.ERROR, provider="local",
                issues=[f"Extraction failed unexpectedly: {e}"])
        extracted = result.fields or {}
        confidence = result.confidence   # None when not defensibly measurable
        extraction_status = result.status
        extraction_issues = result.issues

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
        # HARDENED the same way. An unreadable LMI proof must never block the
        # rep - the document is kept and they pick the program from the dropdown.
        from services import extraction_engine as engine
        try:
            lmi_result = engine.get_extractor().extract([stored_path_abs], category=category)
            text = lmi_result.text
            extraction_status = lmi_result.status
            extraction_issues = lmi_result.issues
        except Exception as e:
            text = ""
            extraction_status = engine.ExtractionStatus.ERROR
            extraction_issues = [f"Extraction failed unexpectedly: {e}"]
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


# ═══════════ Multi-file document sets + hardened extraction ═══════════
#
# ROOT CAUSE THIS REPLACES: the single-file route called extraction.get_text()
# with no guard, and the documents INSERT happened AFTER extraction. On a
# machine with no native tesseract, TesseractNotFoundError became a Flask 500
# AND the file was written to disk with no database row - the rep lost the
# upload entirely.
#
# Here the files are stored and recorded FIRST, then extraction runs inside a
# guard. Extraction can never lose an upload and can never produce a 500.

@bp.route("/<int:enrollment_id>/document-sets", methods=["POST"])
@require_auth
def upload_document_set(enrollment_id):
    """Upload one or more files as ONE logical document.

    multipart field `files` may repeat. Order of receipt is preserved as
    page_order, so a 3-page bill stays in page order.
    """
    from services import extraction_engine as engine

    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return jsonify({"error": "Enrollment not found"}), 404

    category = (request.form.get("category") or "").strip()
    if category not in ("utility_bill", "lmi_document", "other"):
        return jsonify({"error": "Invalid category for upload"}), 400

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"error": "Select at least one file to upload."}), 400

    set_id = request.form.get("document_set_id")
    if set_id:
        # Adding more files to an existing set. Scoped to THIS enrollment so a
        # set id from another enrollment cannot be appended to.
        existing = query_one(
            "SELECT * FROM document_sets WHERE id = ? AND enrollment_id = ? AND category = ?",
            (set_id, enrollment_id, category))
        if not existing:
            return jsonify({"error": "Document set not found for this enrollment."}), 404
        set_id = existing["id"]
        start_order = (query_one(
            "SELECT COALESCE(MAX(page_order), -1) AS m FROM documents WHERE document_set_id = ?",
            (set_id,)) or {"m": -1})["m"] + 1
    else:
        set_id = execute(
            """INSERT INTO document_sets (enrollment_id, category, created_by_user_id)
               VALUES (?, ?, ?)""",
            (enrollment_id, category, g.current_user["id"])).lastrowid
        start_order = 0

    enrollment_dir = os.path.join(UPLOAD_DIR, str(enrollment_id))
    os.makedirs(enrollment_dir, exist_ok=True)

    stored = []
    for offset, file in enumerate(files):
        raw = file.read()
        err = validate_upload(file.filename, len(raw))
        if err:
            return jsonify({"error": f"{file.filename}: {err}"}), 400
        # uuid keeps duplicate filenames from colliding; the enrollment-scoped
        # directory keeps sets from different enrollments apart on disk.
        safe = f"{category}_{uuid.uuid4().hex[:12]}_{file.filename}".replace(" ", "_")
        abs_path = os.path.join(enrollment_dir, safe)
        with open(abs_path, "wb") as fh:
            fh.write(raw)
        rel_path = os.path.join("uploads", str(enrollment_id), safe)
        doc_id = execute(
            """INSERT INTO documents
               (enrollment_id, doc_category, original_filename, stored_path, mime_type,
                file_size, uploaded_by_user_id, document_set_id, page_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (enrollment_id, category, file.filename, rel_path, file.mimetype,
             len(raw), g.current_user["id"], set_id, start_order + offset)).lastrowid
        stored.append({"document_id": doc_id, "filename": file.filename,
                       "page_order": start_order + offset, "abs_path": abs_path})

    # ── Files are safely persisted. Extraction can now fail harmlessly. ──
    ordered = query(
        "SELECT * FROM documents WHERE document_set_id = ? ORDER BY page_order ASC, id ASC",
        (set_id,))
    paths = [resolve_stored_path(r["stored_path"]) for r in ordered]

    try:
        result = engine.get_extractor().extract(paths, category=category)
    except Exception as e:
        # Defence in depth: extract() is contracted not to raise, but a bug in a
        # provider must still never cost the rep their upload.
        result = engine.ExtractionResult(
            engine.ExtractionStatus.ERROR, provider="local",
            issues=[f"Extraction failed unexpectedly: {e}"])

    execute(
        """UPDATE document_sets
              SET extraction_status = ?, extraction_provider = ?, extracted_data_json = ?,
                  extraction_confidence = ?, extraction_issues_json = ?, updated_at = datetime('now')
            WHERE id = ?""",
        (result.status, result.provider, to_json(result.fields),
         result.confidence, to_json(result.issues), set_id))

    # Mirror onto the documents rows so existing readers keep working.
    for r in ordered:
        execute("UPDATE documents SET extracted_data_json = ?, extraction_confidence = ? WHERE id = ?",
                (to_json(result.fields), result.confidence, r["id"]))

    audit.log("document_set_uploaded", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"category": category, "document_set_id": set_id,
                       "file_count": len(stored), "extraction_status": result.status},
              ip_address=request.remote_addr)

    return jsonify({
        "document_set_id": set_id,
        "category": category,
        "files": [{"document_id": d["document_id"], "filename": d["filename"],
                   "page_order": d["page_order"]} for d in stored],
        "file_count": len(ordered),
        "extraction": result.to_dict(),
        # The rep-facing consequence, decided server-side.
        "manual_entry_required": bool(result.needs_manual_entry and category == "utility_bill"),
        "message": _extraction_message(result, category),
    })


def _extraction_message(result, category):
    """Plain-language outcome. Never claims success we did not have."""
    from services import extraction_engine as engine
    S = engine.ExtractionStatus
    if result.status == S.SUCCESS:
        return None
    if category == "utility_bill":
        if result.status == S.PARTIAL:
            return ("We read part of this bill. Please check the fields below and "
                    "fill in anything that is blank.")
        return ("We couldn't reliably read this bill. Please enter the customer "
                "information manually or upload another file.")
    # LMI: the rep must never be asked to transcribe a proof document.
    if result.status in S.NEEDS_MANUAL:
        return ("We couldn't read this document automatically. It has been saved — "
                "just choose the qualifying program below to continue.")
    return None


@bp.route("/<int:enrollment_id>/document-sets/<int:set_id>", methods=["GET"])
@require_auth
def get_document_set(enrollment_id, set_id):
    """A set and its files, in deterministic page order."""
    row = query_one("SELECT * FROM document_sets WHERE id = ? AND enrollment_id = ?",
                    (set_id, enrollment_id))
    if not row:
        return jsonify({"error": "Document set not found for this enrollment."}), 404
    files = query(
        "SELECT id, original_filename, page_order, file_size, mime_type "
        "FROM documents WHERE document_set_id = ? ORDER BY page_order ASC, id ASC",
        (set_id,))
    return jsonify({
        "document_set_id": row["id"],
        "category": row["category"],
        "extraction_status": row["extraction_status"],
        "extraction_confidence": row["extraction_confidence"],
        "files": [dict(f) for f in files],
        "file_count": len(files),
    })


@bp.route("/<int:enrollment_id>/document-sets/<int:set_id>/files/<int:document_id>",
          methods=["DELETE"])
@require_auth
def remove_document_from_set(enrollment_id, set_id, document_id):
    """Remove ONE file from a set. Scoped by enrollment AND set, so a document
    from another enrollment can never be deleted through this route."""
    doc = query_one(
        "SELECT * FROM documents WHERE id = ? AND document_set_id = ? AND enrollment_id = ?",
        (document_id, set_id, enrollment_id))
    if not doc:
        return jsonify({"error": "File not found in this document set."}), 404
    execute("DELETE FROM documents WHERE id = ?", (document_id,))
    # Re-number so page_order stays dense and deterministic.
    remaining = query(
        "SELECT id FROM documents WHERE document_set_id = ? ORDER BY page_order ASC, id ASC",
        (set_id,))
    for i, r in enumerate(remaining):
        execute("UPDATE documents SET page_order = ? WHERE id = ?", (i, r["id"]))
    audit.log("document_removed_from_set", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"document_set_id": set_id, "document_id": document_id},
              ip_address=request.remote_addr)
    return jsonify({"removed": document_id, "remaining": len(remaining)})


@bp.route("/extraction-status", methods=["GET"])
@require_auth
def extraction_capability():
    """Is OCR actually available on this server? Surfaces the missing-binary
    condition explicitly instead of it appearing as a failed upload."""
    from services import extraction_engine as engine
    return jsonify(engine.ocr_status())


# ─────────────── Inline document viewing ───────────────

# Only these are ever served inline. The upload validator already restricts to
# PDF/JPG/PNG, but this is an independent gate: if the allowed set is ever
# widened to something scriptable (SVG, HTML), those must NOT render in-origin.
_INLINE_SAFE_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _document_visible_to_user(enrollment_id, document_id):
    """(doc, error_response). Scoping matches the existing download route so
    viewing can never reach a document the rep could not already download."""
    doc = query_one("SELECT * FROM documents WHERE id = ? AND enrollment_id = ?",
                    (document_id, enrollment_id))
    if not doc:
        # Same 404 whether the document does not exist or belongs to another
        # enrollment - no probing for valid document ids.
        return None, (jsonify({"error": "Not found"}), 404)
    if g.current_user["role"] == "sales_rep":
        enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
        rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
        if not rep or not enrollment or enrollment["sales_rep_id"] != rep["id"]:
            return None, (jsonify({"error": "Forbidden"}), 403)
    return doc, None


@bp.route("/<int:enrollment_id>/documents/<int:document_id>/view", methods=["GET"])
@require_auth
def view_document(enrollment_id, document_id):
    """Serve the ORIGINAL uploaded file INLINE so the browser renders it natively.

    Read-only: no OCR, no preprocessing, no document-set or page-order change.
    Returns exactly the bytes that were uploaded - never a derivative.
    """
    doc, err = _document_visible_to_user(enrollment_id, document_id)
    if err:
        return err

    abs_path = resolve_stored_path(doc["stored_path"])

    # Containment check: the resolved file must live under the backend root.
    # stored_path is written by our own upload code, but this makes traversal
    # structurally impossible rather than merely unlikely.
    root = os.path.realpath(BACKEND_ROOT)
    real = os.path.realpath(abs_path)
    if not (real == root or real.startswith(root + os.sep)):
        return jsonify({"error": "Not found"}), 404

    if not os.path.exists(real):
        return jsonify({"error": "File missing on disk"}), 410

    ext = os.path.splitext(doc["original_filename"] or doc["stored_path"] or "")[1].lower()
    mimetype = _INLINE_SAFE_TYPES.get(ext)
    if not mimetype:
        # Not a type we are willing to render in-origin - hand it over as a
        # download instead of guessing.
        return send_file(real, as_attachment=True,
                         download_name=doc["original_filename"] or f"document-{document_id}")

    audit.log("document_viewed", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"document_id": document_id}, ip_address=request.remote_addr)

    resp = send_file(real, mimetype=mimetype, as_attachment=False,
                     download_name=doc["original_filename"] or f"document-{document_id}")
    # Defence in depth for user-supplied bytes served from our own origin.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; object-src 'self'"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp
