from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute
from auth import require_auth, require_role
from helpers import mask_account_number
from services import status_machine, audit

bp = Blueprint("developer_routes", __name__, url_prefix="/api/developer")


@bp.route("/submissions", methods=["GET"])
@require_auth
@require_role("developer", "admin")
def list_submissions():
    sql = "SELECT s.*, e.enrollment_code, e.status as enrollment_status, e.project_id FROM submissions s JOIN enrollments e ON e.id = s.enrollment_id WHERE 1=1"
    params = []
    dev_status = request.args.get("developer_status")
    if dev_status:
        sql += " AND s.developer_status = ?"
        params.append(dev_status)
    sql += " ORDER BY s.submitted_at DESC"
    rows = query(sql, tuple(params))
    out = []
    for r in rows:
        customer = query_one(
            "SELECT c.first_name, c.last_name FROM customers c JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?",
            (r["enrollment_id"],),
        )
        project = query_one("SELECT name FROM projects WHERE id = ?", (r["project_id"],))
        out.append({
            "submission_id": r["id"], "enrollment_id": r["enrollment_id"], "enrollment_code": r["enrollment_code"],
            "enrollment_status": r["enrollment_status"], "developer_status": r["developer_status"],
            "customer_name": f"{customer['first_name']} {customer['last_name']}" if customer else None,
            "project_name": project["name"] if project else None, "submitted_at": r["submitted_at"],
        })
    return jsonify(out)


@bp.route("/submissions/<int:submission_id>", methods=["GET"])
@require_auth
@require_role("developer", "admin")
def submission_detail(submission_id):
    s = query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    # Reuse the full enrollment serialization for a complete record
    from routes.enrollment_routes import _serialize_enrollment
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (s["enrollment_id"],))
    payload = _serialize_enrollment(enrollment, g.current_user["role"])
    payload["submission"] = dict(s)
    payload["validation_results"] = [dict(v) for v in query("SELECT * FROM validation_results WHERE enrollment_id = ?", (s["enrollment_id"],))]
    payload["qa_reviews"] = [dict(v) for v in query("SELECT * FROM qa_reviews WHERE enrollment_id = ?", (s["enrollment_id"],))]
    payload["signatures"] = [dict(v) for v in query("SELECT * FROM signatures WHERE enrollment_id = ?", (s["enrollment_id"],))]
    audit.log("document_accessed", enrollment_id=s["enrollment_id"], user_id=g.current_user["id"],
              details={"via": "developer_submission_detail"}, ip_address=request.remote_addr)
    return jsonify(payload)


@bp.route("/submissions/<int:submission_id>/assign-project", methods=["POST"])
@require_auth
@require_role("developer", "admin")
def assign_project(submission_id):
    s = query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    project_id = data.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400

    execute("UPDATE submissions SET assigned_project_id = ? WHERE id = ?", (project_id, submission_id))
    execute("UPDATE enrollments SET project_id = ? WHERE id = ?", (project_id, s["enrollment_id"]))
    status_machine.transition(s["enrollment_id"], "Project Assigned", user_id=g.current_user["id"],
                               reason="Assigned by developer", ip_address=request.remote_addr)
    audit.log("project_assigned", enrollment_id=s["enrollment_id"], user_id=g.current_user["id"],
              details={"project_id": project_id}, ip_address=request.remote_addr)
    return jsonify({"ok": True, "status": "Project Assigned"})


@bp.route("/submissions/<int:submission_id>/activate", methods=["POST"])
@require_auth
@require_role("developer", "admin")
def activate(submission_id):
    s = query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if not s:
        return jsonify({"error": "Not found"}), 404
    status_machine.transition(s["enrollment_id"], "Active", user_id=g.current_user["id"],
                               reason="Enrollment activated", ip_address=request.remote_addr)
    audit.log("enrollment_activated", enrollment_id=s["enrollment_id"], user_id=g.current_user["id"], ip_address=request.remote_addr)
    return jsonify({"ok": True, "status": "Active"})
