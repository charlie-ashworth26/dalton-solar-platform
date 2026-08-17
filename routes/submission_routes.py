import os

from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute
from auth import require_auth, require_role
from helpers import mask_account_number, json_or_none, resolve_stored_path
from services import status_machine, audit, documents as doc_service, packaging
from services.authz import visible_enrollment

bp = Blueprint("submission_routes", __name__)


def _gather_summary_json(enrollment_id):
    e = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    customer = query_one("SELECT * FROM customers WHERE id = ?", (e["customer_id"],))
    address = query_one("SELECT * FROM service_addresses WHERE id = ?", (e["service_address_id"],))
    utility = query_one("SELECT * FROM utility_accounts WHERE id = ?", (e["utility_account_id"],))
    project = query_one("SELECT * FROM projects WHERE id = ?", (e["project_id"],))
    lmi = query_one("SELECT * FROM lmi_qualifications WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1", (enrollment_id,))
    status_hist = query("SELECT * FROM status_history WHERE enrollment_id = ? ORDER BY id", (enrollment_id,))
    qa = query("SELECT * FROM qa_reviews WHERE enrollment_id = ? ORDER BY id", (enrollment_id,))
    sigs = query("SELECT field_key, field_type, method, signer_name, completed_at FROM signatures WHERE enrollment_id = ?", (enrollment_id,))

    return {
        "enrollment_code": e["enrollment_code"],
        "status": e["status"],
        "customer": dict(customer) if customer else None,
        "service_address": dict(address) if address else None,
        "utility_account": {**dict(utility), "account_number": mask_account_number(utility["account_number"])} if utility else None,
        "project": dict(project) if project else None,
        "lmi_qualification": dict(lmi) if lmi else None,
        "status_history": [dict(s) for s in status_hist],
        "qa_reviews": [dict(q) for q in qa],
        "signatures": [dict(s) for s in sigs],
    }


def _gather_validation_json(enrollment_id):
    rows = query("SELECT * FROM validation_results WHERE enrollment_id = ?", (enrollment_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["reasons"] = json_or_none(d.pop("reasons_json"))
        d["missing_info"] = json_or_none(d.pop("missing_info_json"))
        d["mismatch_warnings"] = json_or_none(d.pop("mismatch_warnings_json"))
        out.append(d)
    return {"validation_results": out}


@bp.route("/api/enrollments/<int:enrollment_id>/submit", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin", "qa_reviewer")
def submit_enrollment(enrollment_id):
    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
    if enrollment["status"] != "Verified":
        return jsonify({"error": f"Enrollment must be 'Verified' before submitting (currently '{enrollment['status']}')"}), 409

    gen_dir = os.path.join(doc_service.STORAGE_DIR, str(enrollment_id))
    os.makedirs(gen_dir, exist_ok=True)

    # Cover sheet
    ctx_summary = _gather_summary_json(enrollment_id)
    project = ctx_summary["project"] or {}
    cover_ctx = {
        "enrollment_code": enrollment["enrollment_code"],
        "customer_name": f"{ctx_summary['customer']['first_name']} {ctx_summary['customer']['last_name']}" if ctx_summary["customer"] else "—",
        "customer_email": ctx_summary["customer"]["email"] if ctx_summary["customer"] else "—",
        "customer_phone": ctx_summary["customer"]["phone"] if ctx_summary["customer"] else "—",
        "service_address": f"{ctx_summary['service_address']['street']}, {ctx_summary['service_address']['city']}, {ctx_summary['service_address']['state']} {ctx_summary['service_address']['zip']}" if ctx_summary["service_address"] else "—",
        "utility": ctx_summary["utility_account"]["utility_name"] if ctx_summary["utility_account"] else "—",
        "account_number_masked": ctx_summary["utility_account"]["account_number"] if ctx_summary["utility_account"] else "—",
        "project_name": project.get("name", "—"),
        "savings_pct": project.get("savings_pct", "—"),
    }
    cover_path = os.path.join(gen_dir, "enrollment-cover-sheet.pdf")
    doc_service.generate_cover_sheet(cover_ctx, cover_path)

    # Merge everything into one packet PDF
    generated_docs = query("SELECT * FROM documents WHERE enrollment_id = ? AND doc_category IN ('generated_agreement','signature_certificate')", (enrollment_id,))
    merge_paths = [cover_path] + [resolve_stored_path(d["stored_path"]) for d in generated_docs]
    packet_path = os.path.join(gen_dir, "enrollment-packet.pdf")
    doc_service.merge_pdfs(merge_paths, packet_path)

    packet_doc_cur = execute(
        """INSERT INTO documents (enrollment_id, doc_category, original_filename, stored_path, mime_type, file_size)
           VALUES (?, 'submission_package_pdf', 'enrollment-packet.pdf', ?, 'application/pdf', ?)""",
        (enrollment_id, os.path.join("storage", "generated", str(enrollment_id), "enrollment-packet.pdf"), os.path.getsize(packet_path)),
    )

    # Build the ZIP
    utility_bill = query_one("SELECT * FROM documents WHERE enrollment_id = ? AND doc_category='utility_bill' ORDER BY id DESC LIMIT 1", (enrollment_id,))
    lmi_doc = query_one("SELECT * FROM documents WHERE enrollment_id = ? AND doc_category='lmi_document' ORDER BY id DESC LIMIT 1", (enrollment_id,))
    subscription = query_one("SELECT d.* FROM documents d JOIN agreements a ON a.generated_document_id = d.id WHERE a.enrollment_id=? AND a.document_type='subscription_agreement'", (enrollment_id,))
    disclosure = query_one("SELECT d.* FROM documents d JOIN agreements a ON a.generated_document_id = d.id WHERE a.enrollment_id=? AND a.document_type='cdg_disclosure'", (enrollment_id,))
    cert = query_one("SELECT * FROM documents WHERE enrollment_id=? AND doc_category='signature_certificate' ORDER BY id DESC LIMIT 1", (enrollment_id,))

    def abspath(doc_row):
        if not doc_row:
            return None
        return resolve_stored_path(doc_row["stored_path"])

    files_by_name = {
        "enrollment-packet.pdf": packet_path,
        "utility-bill.pdf": abspath(utility_bill),
        "lmi-document.pdf": abspath(lmi_doc),
        "signed-subscription-agreement.pdf": abspath(subscription),
        "ny-cdg-disclosure.pdf": abspath(disclosure),
        "signature-certificate.pdf": abspath(cert),
    }
    zip_path = os.path.join(packaging.STORAGE_DIR, f"{enrollment['enrollment_code']}.zip")
    packaging.build_package_zip(enrollment["enrollment_code"], files_by_name, ctx_summary, _gather_validation_json(enrollment_id), zip_path)

    zip_doc_cur = execute(
        """INSERT INTO documents (enrollment_id, doc_category, original_filename, stored_path, mime_type, file_size)
           VALUES (?, 'submission_package_zip', ?, ?, 'application/zip', ?)""",
        (enrollment_id, f"{enrollment['enrollment_code']}.zip",
         os.path.join("storage", "packages", f"{enrollment['enrollment_code']}.zip"), os.path.getsize(zip_path)),
    )

    sub_cur = execute(
        """INSERT INTO submissions (enrollment_id, submitted_by_user_id, package_zip_document_id, package_pdf_document_id, developer_status)
           VALUES (?, ?, ?, ?, 'submitted')""",
        (enrollment_id, g.current_user["id"], zip_doc_cur.lastrowid, packet_doc_cur.lastrowid),
    )

    status_machine.transition(enrollment_id, "Submitted", user_id=g.current_user["id"], reason="Package generated", ip_address=request.remote_addr)
    status_machine.transition(enrollment_id, "Developer Review", user_id=g.current_user["id"], reason="Delivered to developer queue", ip_address=request.remote_addr)
    audit.log("enrollment_submitted", enrollment_id=enrollment_id, user_id=g.current_user["id"], ip_address=request.remote_addr)

    return jsonify({
        "submission_id": sub_cur.lastrowid,
        "package_pdf_document_id": packet_doc_cur.lastrowid,
        "package_zip_document_id": zip_doc_cur.lastrowid,
        "status": "Developer Review",
    }), 201


@bp.route("/api/enrollments/<int:enrollment_id>/package", methods=["GET"])
@require_auth
def get_package(enrollment_id):
    _, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
    submission = query_one("SELECT * FROM submissions WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1", (enrollment_id,))
    if not submission:
        return jsonify({"error": "No submission package has been generated for this enrollment yet"}), 404
    return jsonify({
        "submission_id": submission["id"],
        "package_pdf_document_id": submission["package_pdf_document_id"],
        "package_zip_document_id": submission["package_zip_document_id"],
        "submitted_at": submission["submitted_at"],
        "download_pdf_url": f"/api/enrollments/{enrollment_id}/documents/{submission['package_pdf_document_id']}/download",
        "download_zip_url": f"/api/enrollments/{enrollment_id}/documents/{submission['package_zip_document_id']}/download",
    })


@bp.route("/api/submissions/<int:submission_id>", methods=["GET"])
@require_auth
def get_submission(submission_id):
    s = query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(s))


@bp.route("/api/submissions/<int:submission_id>/status", methods=["GET"])
@require_auth
def get_submission_status(submission_id):
    s = query_one("SELECT developer_status FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"developer_status": s["developer_status"]})


@bp.route("/api/submissions/<int:submission_id>/status", methods=["PATCH"])
@require_auth
@require_role("developer", "admin")
def update_submission_status(submission_id):
    s = query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    new_dev_status = data.get("developer_status")
    if new_dev_status not in ("accepted", "rejected", "needs_work"):
        return jsonify({"error": "developer_status must be accepted, rejected, or needs_work"}), 400

    execute(
        """UPDATE submissions SET developer_status=?, developer_reviewer_user_id=?, developer_notes=?, decided_at=datetime('now') WHERE id=?""",
        (new_dev_status, g.current_user["id"], data.get("notes"), submission_id),
    )
    enrollment_status_map = {"accepted": "Accepted", "rejected": "Rejected", "needs_work": "Needs Work"}
    status_machine.transition(s["enrollment_id"], enrollment_status_map[new_dev_status], user_id=g.current_user["id"],
                               reason=data.get("notes"), ip_address=request.remote_addr)
    audit.log("developer_decision", enrollment_id=s["enrollment_id"], user_id=g.current_user["id"],
              details={"decision": new_dev_status}, ip_address=request.remote_addr)
    return jsonify({"ok": True, "developer_status": new_dev_status})
