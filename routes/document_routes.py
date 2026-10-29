import os
import json
import uuid

from flask import Blueprint, request, jsonify, g, send_file

from db import query, query_one, execute, transaction
from auth import require_auth, require_role
from helpers import validate_upload, to_json, resolve_stored_path, DATA_ROOT
from services import extraction, lmi_validation, audit
from services.authz import visible_enrollment

bp = Blueprint("document_routes", __name__, url_prefix="/api/enrollments")

UPLOAD_DIR = os.path.join(DATA_ROOT, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _enrollment_utility(enrollment):
    """The enrollment's Perch utility slug, or None.

    Defensive about the row shape on purpose: this is only a HINT for field
    extraction, so a row selected without the column, or no row at all, must
    degrade to "no hint" rather than turning a successful upload into a 500.
    """
    if enrollment is None:
        return None
    try:
        return enrollment["utility_name"]
    except (KeyError, IndexError, TypeError):
        return None


@bp.route("/<int:enrollment_id>/documents", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def upload_document(enrollment_id):
    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err

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
            result = engine.extract_with_hint(
                engine.get_extractor(),
                [stored_path_abs], category=category,
                utility_hint=_enrollment_utility(enrollment))
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
    _, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
    data = request.get_json(force=True, silent=True) or {}

    # The document must belong to THIS enrollment. The UPDATE below was already
    # scoped, so another enrollment's document was never modified - but without
    # this check the route still returned 200 and audit-logged a correction that
    # never happened, which is both misleading to the caller and false in the
    # audit trail.
    doc = query_one("SELECT id FROM documents WHERE id = ? AND enrollment_id = ?",
                    (document_id, enrollment_id))
    if not doc:
        return jsonify({"error": "Document not found for this enrollment"}), 404

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

    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err

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

    # THE UTILITY THIS ENROLLMENT IS FOR, used only when the document does
    # not name its own. Read-only: nothing is written, and the upload above
    # has already completed and been recorded.
    hint = _enrollment_utility(enrollment)
    try:
        result = engine.extract_with_hint(engine.get_extractor(), paths,
                                          category=category, utility_hint=hint)
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

    # ── LMI PROOF VALIDATION, preserved when a proof arrives as a set ──────
    #
    # WHY THIS IS HERE. The older single-file route (upload_document above)
    # runs lmi_validation and writes a validation_results row for every proof
    # document; this set route never did, because until now only bills came
    # through it. Moving proof uploads onto document sets without this would
    # have silently removed LMI validation from the enrollment detail payload,
    # the developer/QA review and the submission package - a regression that
    # would not have shown up as a failed upload anywhere.
    #
    # ONE ROW FOR THE SET, not one per file. The set IS one logical proof, and
    # its classification is made from the text of every page together. Running
    # it per file is the mistake the set abstraction exists to prevent: page 2
    # of an award letter carries none of the keywords and would be judged
    # "likely_invalid" on its own.
    lmi_result = None
    if category == "lmi_document" and ordered:
        holder = query_one(
            "SELECT c.first_name, c.last_name FROM customers c "
            "JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?",
            (enrollment_id,))
        holder_name = (f"{holder['first_name']} {holder['last_name']}"
                       if holder else None)
        lmi_result = lmi_validation.validate_lmi_document(result.text or "",
                                                          holder_name)
        # Attached to the FIRST page, which is the document id every existing
        # caller already uses as the proof's identity (state.lmi.documentId,
        # lmi_qualifications.document_id, the /lmi/proof_docs request).
        execute(
            """INSERT INTO validation_results (enrollment_id, document_id, validation_type,
               classification, confidence, reasons_json, missing_info_json, mismatch_warnings_json)
               VALUES (?, ?, 'lmi_document', ?, ?, ?, ?, ?)""",
            (enrollment_id, ordered[0]["id"], lmi_result["classification"],
             lmi_result["confidence"], to_json(lmi_result["reasons"]),
             to_json(lmi_result["missing_info"]),
             to_json(lmi_result["mismatch_warnings"])))

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
        # The LMI judgement for the whole set, when this was a proof upload.
        # Absent for a bill, exactly as before.
        "lmi_validation": lmi_result,
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
    _, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
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
    # Detached inside the same transaction as the delete, for the reason given
    # above _detach_document_references(). This route has never hit the FK in
    # practice - a set upload writes no validation_results row - but it is the
    # same delete against the same constraints, and it must not be one legacy
    # upload away from the 500 the other route just produced.
    with transaction() as tx:
        _detach_document_references(tx, enrollment_id, document_id)
        tx.execute("DELETE FROM documents WHERE id = ?", (document_id,))
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


# Tables that FOREIGN KEY to documents(id). None declares ON DELETE, so SQLite
# blocks the delete (NO ACTION) and the request becomes a 500 - which is exactly
# what happened the first time a legacy single-file upload was removed:
#
#   sqlite3.IntegrityError: FOREIGN KEY constraint failed
#
# The set-scoped route never hit it only because upload_document_set() writes no
# validation_results row, while the older upload_document() does. The reference
# therefore has to be dealt with explicitly, and WHAT TO DO DIFFERS BY TABLE:
#
#   validation_results  DELETED. It is a judgement derived from that exact file
#                       at upload - classification, confidence, reasons. Once
#                       the file is detached the judgement describes nothing,
#                       and leaving it would show QA a "likely_valid utility
#                       bill" for a document that is no longer attached: the
#                       same class of stale state this whole fix is about.
#
#   lmi_qualifications  DETACHED, NOT deleted (document_id -> NULL). This row
#                       holds REP-ENTERED work - qualification type, household
#                       size, the chosen path - which is not derived from the
#                       file and must not be destroyed because a photo was
#                       swapped. The qualification survives, now pointing at no
#                       document, which is the truthful state and is what the
#                       LMI step reads to ask for the proof again.
#
# agreements.generated_document_id and submissions.package_*_document_id also
# reference documents(id), but only for UBS-GENERATED categories, which the
# category guard below refuses outright - a rep cannot reach them here.
def _detach_document_references(tx, enrollment_id, document_id):
    """Clear the FK references to this document. Scoped to the enrollment."""
    tx.execute("DELETE FROM validation_results WHERE document_id = ? AND enrollment_id = ?",
               (document_id, enrollment_id))
    tx.execute("UPDATE lmi_qualifications SET document_id = NULL "
               "WHERE document_id = ? AND enrollment_id = ?",
               (document_id, enrollment_id))


@bp.route("/<int:enrollment_id>/documents/<int:document_id>", methods=["DELETE"])
@require_auth
@require_role("sales_rep", "admin")
def remove_document(enrollment_id, document_id):
    """Detach ONE document from this enrollment, with or without a set.

    WHY THIS EXISTS. The only removal route was set-scoped
    (/document-sets/<set>/files/<doc>), so a document with document_set_id
    NULL - every single-file upload made through the older /documents route -
    had NO removal endpoint at all. The browser could not persist its removal
    even in principle, and quietly stopped trying. A rep could attach such a
    document and never detach it.

    SCOPING is by enrollment, exactly as the set-scoped route scopes by
    enrollment AND set: a document belonging to another enrollment is a 404,
    not a deletion.

    THE FILE ON DISK IS DEDUCTIBLY LEFT ALONE, matching the set-scoped route.
    UBS has never physically deleted an uploaded document - removal has always
    meant detaching the reference - and introducing unlink here would make a
    rep's click destroy bytes that QA, audit and the submission package may
    still refer to. Orphaned files are a known, separate maintenance question;
    see KNOWN_ISSUES.md.
    """
    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err

    doc = query_one("SELECT * FROM documents WHERE id = ? AND enrollment_id = ?",
                    (document_id, enrollment_id))
    if not doc:
        return jsonify({"error": "Document not found for this enrollment."}), 404

    # Only a rep-uploaded document may be detached this way. A generated
    # agreement, signature certificate or submission package is produced by
    # UBS and referenced elsewhere; deleting one through the uploader would
    # break the record it belongs to.
    if doc["doc_category"] not in ("utility_bill", "lmi_document", "other"):
        return jsonify({"error": "That document cannot be removed here."}), 400

    set_id = doc["document_set_id"] if "document_set_id" in doc.keys() else None

    # ONE all-or-nothing unit. execute() commits per call, so detaching the
    # references and removing the row separately could leave a validation
    # record deleted for a document that is still attached.
    with transaction() as tx:
        _detach_document_references(tx, enrollment_id, document_id)
        tx.execute("DELETE FROM documents WHERE id = ? AND enrollment_id = ?",
                   (document_id, enrollment_id))

    remaining = 0
    if set_id:
        # Same re-densing as the set-scoped route, so page_order stays
        # deterministic whichever route removed the page.
        rows = query(
            "SELECT id FROM documents WHERE document_set_id = ? "
            "ORDER BY page_order ASC, id ASC", (set_id,))
        for i, r in enumerate(rows):
            execute("UPDATE documents SET page_order = ? WHERE id = ?", (i, r["id"]))
        remaining = len(rows)

    audit.log("document_removed", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"document_id": document_id, "document_set_id": set_id,
                       "category": doc["doc_category"]},
              ip_address=request.remote_addr)
    return jsonify({"removed": document_id, "document_set_id": set_id,
                    "remaining": remaining})


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
    # Follows DATA_ROOT, so the containment guarantee holds whether files live
    # under the repo (local) or on the persistent disk (hosted).
    root = os.path.realpath(DATA_ROOT)
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
