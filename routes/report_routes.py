from flask import Blueprint, request, jsonify, g

from db import query
from auth import require_auth, require_role

bp = Blueprint("report_routes", __name__, url_prefix="/api/reports")


@bp.route("/summary", methods=["GET"])
@require_auth
@require_role("admin", "qa_reviewer", "developer")
def summary():
    filters_sql = []
    params = []
    if request.args.get("sales_rep_id"):
        filters_sql.append("sales_rep_id = ?")
        params.append(request.args["sales_rep_id"])
    if request.args.get("project_id"):
        filters_sql.append("project_id = ?")
        params.append(request.args["project_id"])
    if request.args.get("date_from"):
        filters_sql.append("created_at >= ?")
        params.append(request.args["date_from"])
    if request.args.get("date_to"):
        filters_sql.append("created_at <= ?")
        params.append(request.args["date_to"])
    if request.args.get("lmi") == "true":
        filters_sql.append("lmi_path IS NOT NULL AND lmi_path != 'not_applicable'")
    elif request.args.get("lmi") == "false":
        filters_sql.append("(lmi_path IS NULL OR lmi_path = 'not_applicable')")

    where = (" WHERE " + " AND ".join(filters_sql)) if filters_sql else ""

    counts = {}
    for label, status_list in [
        ("enrollments_started", None),
        ("utility_bills_uploaded", ["Utility Bill Uploaded", "Utility Validation", "LMI Review", "Agreement Ready",
                                     "Signature Pending", "Signed", "Internal Review", "Needs Work", "Verified",
                                     "Submitted", "Developer Review", "Accepted", "Rejected", "Project Assigned", "Active"]),
        ("agreements_generated", ["Agreement Ready", "Signature Pending", "Signed", "Internal Review", "Needs Work",
                                   "Verified", "Submitted", "Developer Review", "Accepted", "Rejected", "Project Assigned", "Active"]),
        ("enrollments_signed", ["Signed", "Internal Review", "Needs Work", "Verified", "Submitted",
                                 "Developer Review", "Accepted", "Rejected", "Project Assigned", "Active"]),
        ("qa_approved", ["Verified", "Submitted", "Developer Review", "Accepted", "Project Assigned", "Active"]),
        ("needs_work", ["Needs Work"]),
        ("rejected", ["Rejected"]),
        ("developer_submissions", ["Submitted", "Developer Review", "Accepted", "Rejected", "Project Assigned", "Active"]),
        ("accepted", ["Accepted", "Project Assigned", "Active"]),
        ("project_assigned", ["Project Assigned", "Active"]),
        ("active", ["Active"]),
    ]:
        sub_where = list(filters_sql)
        sub_params = list(params)
        if status_list:
            placeholders = ",".join("?" for _ in status_list)
            sub_where.append(f"status IN ({placeholders})")
            sub_params.extend(status_list)
        w = (" WHERE " + " AND ".join(sub_where)) if sub_where else ""
        row = query(f"SELECT COUNT(*) as n FROM enrollments{w}", tuple(sub_params))
        counts[label] = row[0]["n"]

    lmi_row = query(f"SELECT COUNT(*) as n FROM enrollments{where}{' AND ' if where else ' WHERE '}lmi_path IS NOT NULL AND lmi_path != 'not_applicable'"
                     if not request.args.get("lmi") else f"SELECT COUNT(*) as n FROM enrollments{where}", tuple(params))
    counts["lmi_documentation_submitted"] = lmi_row[0]["n"]

    signing_links = query(f"SELECT COUNT(*) as n FROM signing_sessions ss JOIN enrollments e ON e.id = ss.enrollment_id{where}", tuple(params))
    counts["signature_links_created"] = signing_links[0]["n"]

    return jsonify(counts)
