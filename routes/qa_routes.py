from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute
from auth import require_auth, require_role
from services import status_machine, audit

bp = Blueprint("qa_routes", __name__, url_prefix="/api/qa")

CORRECTION_REASONS = [
    "address_mismatch", "account_number_unclear", "lmi_doc_expired", "lmi_doc_wrong_type",
    "name_mismatch", "signature_incomplete", "bill_unreadable", "other",
]


@bp.route("/correction-reasons", methods=["GET"])
@require_auth
def correction_reasons():
    return jsonify(CORRECTION_REASONS)


@bp.route("/queue", methods=["GET"])
@require_auth
@require_role("qa_reviewer", "admin")
def qa_queue():
    rows = query("SELECT * FROM enrollments WHERE status = 'Internal Review' ORDER BY updated_at ASC")
    out = []
    for r in rows:
        customer = query_one("SELECT first_name, last_name FROM customers WHERE id = ?", (r["customer_id"],))
        project = query_one("SELECT name FROM projects WHERE id = ?", (r["project_id"],))
        out.append({
            "id": r["id"], "enrollment_code": r["enrollment_code"], "status": r["status"],
            "customer_name": f"{customer['first_name']} {customer['last_name']}" if customer else None,
            "project_name": project["name"] if project else None,
            "updated_at": r["updated_at"],
        })
    return jsonify(out)


@bp.route("/enrollments/<int:enrollment_id>/review", methods=["POST"])
@require_auth
@require_role("qa_reviewer", "admin")
def submit_review(enrollment_id):
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    decision = data.get("decision")
    if decision not in ("approved", "rejected", "needs_work"):
        return jsonify({"error": "decision must be approved, rejected, or needs_work"}), 400

    execute(
        "INSERT INTO qa_reviews (enrollment_id, reviewer_user_id, decision, correction_reason, notes) VALUES (?, ?, ?, ?, ?)",
        (enrollment_id, g.current_user["id"], decision, data.get("correction_reason"), data.get("notes")),
    )

    status_map = {"approved": "Verified", "rejected": "Rejected", "needs_work": "Needs Work"}
    status_machine.transition(enrollment_id, status_map[decision], user_id=g.current_user["id"],
                               reason=data.get("correction_reason"), notes=data.get("notes"), ip_address=request.remote_addr)

    if data.get("reassign_to_rep_id") and decision == "needs_work":
        execute("UPDATE enrollments SET sales_rep_id = ? WHERE id = ?", (data["reassign_to_rep_id"], enrollment_id))
        audit.log("enrollment_reassigned", enrollment_id=enrollment_id, user_id=g.current_user["id"],
                  details={"new_rep_id": data["reassign_to_rep_id"]}, ip_address=request.remote_addr)

    audit.log("qa_review_submitted", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"decision": decision}, ip_address=request.remote_addr)
    return jsonify({"decision": decision, "status": status_map[decision]}), 201
