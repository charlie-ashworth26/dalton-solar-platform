from flask import Blueprint, request, jsonify, g

from db import query
from auth import require_auth, require_role

bp = Blueprint("report_routes", __name__, url_prefix="/api/reports")


@bp.route("/summary", methods=["GET"])
@require_auth
@require_role("admin", "qa_reviewer", "developer")
def summary():
    # Soft-discarded enrollments are failed attempts that were never real work
    # (see services/enrollment_cleanup.py). They are excluded from every count
    # here for the same reason they are excluded from the dashboard listing.
    # Seeded as the first filter so it applies to the totals, the per-status
    # counts, the LMI count and the signing-link join alike.
    #
    # EVERY FILTER COLUMN IS TABLE-QUALIFIED, and that is load-bearing rather
    # than tidiness. This one `where` string is reused by four queries, and the
    # last of them joins signing_sessions to enrollments. `created_at` exists on
    # BOTH tables, so an unqualified `created_at >= ?` made that join ambiguous
    # and SQLite aborted the request - a 500 on any date filter, while every
    # other filter worked. (`status` is on both tables too; it only ever reaches
    # the single-table counts below, but it is qualified here for the same
    # reason - the next person to reuse this clause should not have to know
    # which columns happen to be safe.)
    #
    # `enrollments.` is used rather than an alias so the one string is valid in
    # all four queries: three select FROM enrollments directly, and the join
    # below names the table rather than aliasing it.
    filters_sql = ["enrollments.discarded_at IS NULL"]
    params = []
    if request.args.get("sales_rep_id"):
        filters_sql.append("enrollments.sales_rep_id = ?")
        params.append(request.args["sales_rep_id"])
    if request.args.get("project_id"):
        filters_sql.append("enrollments.project_id = ?")
        params.append(request.args["project_id"])
    if request.args.get("date_from"):
        filters_sql.append("enrollments.created_at >= ?")
        params.append(request.args["date_from"])
    if request.args.get("date_to"):
        filters_sql.append("enrollments.created_at <= ?")
        params.append(request.args["date_to"])
    if request.args.get("lmi") == "true":
        filters_sql.append("enrollments.lmi_path IS NOT NULL "
                           "AND enrollments.lmi_path != 'not_applicable'")
    elif request.args.get("lmi") == "false":
        filters_sql.append("(enrollments.lmi_path IS NULL "
                           "OR enrollments.lmi_path = 'not_applicable')")

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
            sub_where.append(f"enrollments.status IN ({placeholders})")
            sub_params.extend(status_list)
        w = (" WHERE " + " AND ".join(sub_where)) if sub_where else ""
        row = query(f"SELECT COUNT(*) as n FROM enrollments{w}", tuple(sub_params))
        counts[label] = row[0]["n"]

    lmi_row = query(
        f"SELECT COUNT(*) as n FROM enrollments{where}"
        f"{' AND ' if where else ' WHERE '}enrollments.lmi_path IS NOT NULL "
        f"AND enrollments.lmi_path != 'not_applicable'"
        if not request.args.get("lmi")
        else f"SELECT COUNT(*) as n FROM enrollments{where}", tuple(params))
    counts["lmi_documentation_submitted"] = lmi_row[0]["n"]

    # enrollments is NOT aliased here: the shared `where` above qualifies its
    # columns with the table name, so the join must expose that name.
    signing_links = query(
        f"SELECT COUNT(*) as n FROM signing_sessions "
        f"JOIN enrollments ON enrollments.id = signing_sessions.enrollment_id"
        f"{where}", tuple(params))
    counts["signature_links_created"] = signing_links[0]["n"]

    return jsonify(counts)
