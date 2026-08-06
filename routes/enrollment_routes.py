import os
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute, rows_to_list
from auth import require_auth, require_role
from helpers import mask_account_number, next_enrollment_code, json_or_none, to_json
from services import audit, status_machine, lmi_validation

bp = Blueprint("enrollment_routes", __name__, url_prefix="/api/enrollments")


def _serialize_enrollment(row, requester_role):
    d = dict(row)
    customer = query_one(
        "SELECT id, first_name, last_name, email, phone, created_at, updated_at FROM customers WHERE id = ?",
        (d["customer_id"],),
    ) if d["customer_id"] else None
    address = query_one("SELECT * FROM service_addresses WHERE id = ?", (d["service_address_id"],)) if d["service_address_id"] else None
    utility = query_one("SELECT * FROM utility_accounts WHERE id = ?", (d["utility_account_id"],)) if d["utility_account_id"] else None
    project = query_one("SELECT * FROM projects WHERE id = ?", (d["project_id"],)) if d["project_id"] else None
    rep = query_one(
        "SELECT sales_reps.*, users.full_name, users.email FROM sales_reps JOIN users ON users.id = sales_reps.user_id WHERE sales_reps.id = ?",
        (d["sales_rep_id"],),
    ) if d["sales_rep_id"] else None
    lmi = query_one("SELECT * FROM lmi_qualifications WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1", (d["id"],))

    utility_dict = dict(utility) if utility else None
    if utility_dict and requester_role not in ("admin", "qa_reviewer"):
        utility_dict["account_number"] = mask_account_number(utility_dict["account_number"])

    return {
        "id": d["id"],
        "enrollment_code": d["enrollment_code"],
        "status": d["status"],
        "lmi_path": d["lmi_path"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "customer": dict(customer) if customer else None,
        "service_address": dict(address) if address else None,
        "utility_account": utility_dict,
        "project": dict(project) if project else None,
        "sales_rep": dict(rep) if rep else None,
        "lmi_qualification": dict(lmi) if lmi else None,
    }


@bp.route("", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_enrollment():
    data = request.get_json(force=True, silent=True) or {}
    rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
    sales_rep_id = rep["id"] if rep else data.get("sales_rep_id")

    project_id = data.get("project_id")
    code = next_enrollment_code()
    cur = execute(
        """INSERT INTO enrollments (enrollment_code, project_id, sales_rep_id, status, created_by_user_id, updated_by_user_id)
           VALUES (?, ?, ?, 'Draft', ?, ?)""",
        (code, project_id, sales_rep_id, g.current_user["id"], g.current_user["id"]),
    )
    enrollment_id = cur.lastrowid
    status_machine.transition(enrollment_id, "Information Needed", user_id=g.current_user["id"],
                               reason="Enrollment created", force=True, ip_address=request.remote_addr)
    audit.log("enrollment_created", enrollment_id=enrollment_id, user_id=g.current_user["id"], ip_address=request.remote_addr)

    row = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    return jsonify(_serialize_enrollment(row, g.current_user["role"])), 201


@bp.route("", methods=["GET"])
@require_auth
def list_enrollments():
    role = g.current_user["role"]
    sql = "SELECT * FROM enrollments WHERE 1=1"
    params = []

    if role == "sales_rep":
        rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
        sql += " AND sales_rep_id = ?"
        params.append(rep["id"] if rep else -1)

    status_filter = request.args.get("status")
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)

    project_filter = request.args.get("project_id")
    if project_filter:
        sql += " AND project_id = ?"
        params.append(project_filter)

    date_from = request.args.get("date_from")
    if date_from:
        sql += " AND created_at >= ?"
        params.append(date_from)
    date_to = request.args.get("date_to")
    if date_to:
        sql += " AND created_at <= ?"
        params.append(date_to)

    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = query(sql, tuple(params))
    return jsonify([_serialize_enrollment(r, role) for r in rows])


@bp.route("/<int:enrollment_id>", methods=["GET"])
@require_auth
def get_enrollment(enrollment_id):
    row = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    if g.current_user["role"] == "sales_rep":
        rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
        if not rep or row["sales_rep_id"] != rep["id"]:
            return jsonify({"error": "Forbidden"}), 403

    documents = rows_to_list(query("SELECT id, doc_category, original_filename, extraction_confidence, created_at FROM documents WHERE enrollment_id = ?", (enrollment_id,)))
    agreements = rows_to_list(query("SELECT * FROM agreements WHERE enrollment_id = ?", (enrollment_id,)))
    signatures = rows_to_list(query("SELECT * FROM signatures WHERE enrollment_id = ?", (enrollment_id,)))
    status_hist = rows_to_list(query("SELECT * FROM status_history WHERE enrollment_id = ? ORDER BY id", (enrollment_id,)))
    qa = rows_to_list(query("SELECT * FROM qa_reviews WHERE enrollment_id = ? ORDER BY id DESC", (enrollment_id,)))
    validations = rows_to_list(query("SELECT * FROM validation_results WHERE enrollment_id = ? ORDER BY id DESC", (enrollment_id,)))
    submission = query_one("SELECT * FROM submissions WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1", (enrollment_id,))

    audit.log("document_accessed", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"via": "get_enrollment"}, ip_address=request.remote_addr)

    payload = _serialize_enrollment(row, g.current_user["role"])
    payload["documents"] = documents
    payload["agreements"] = agreements
    payload["signatures"] = signatures
    payload["status_history"] = status_hist
    payload["qa_reviews"] = qa
    payload["validation_results"] = [
        {**v, "reasons_json": json_or_none(v["reasons_json"]), "missing_info_json": json_or_none(v["missing_info_json"]),
         "mismatch_warnings_json": json_or_none(v["mismatch_warnings_json"])}
        for v in validations
    ]
    payload["submission"] = dict(submission) if submission else None
    return jsonify(payload)


@bp.route("/<int:enrollment_id>", methods=["PATCH"])
@require_auth
@require_role("sales_rep", "admin")
def update_enrollment(enrollment_id):
    row = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True, silent=True) or {}

    # Customer
    if "customer" in data:
        c = data["customer"]
        if row["customer_id"]:
            execute(
                "UPDATE customers SET first_name=?, last_name=?, email=?, phone=?, updated_at=datetime('now') WHERE id=?",
                (c.get("first_name"), c.get("last_name"), c.get("email"), c.get("phone"), row["customer_id"]),
            )
            customer_id = row["customer_id"]
        else:
            cur = execute(
                "INSERT INTO customers (first_name, last_name, email, phone) VALUES (?, ?, ?, ?)",
                (c.get("first_name"), c.get("last_name"), c.get("email"), c.get("phone")),
            )
            customer_id = cur.lastrowid
            execute("UPDATE enrollments SET customer_id=? WHERE id=?", (customer_id, enrollment_id))
        if c.get("password"):
            from auth import hash_password
            execute("UPDATE customers SET password_hash=? WHERE id=?", (hash_password(c["password"]), customer_id))

    # Address
    if "service_address" in data:
        a = data["service_address"]
        current = query_one("SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
        cust_id = current["customer_id"]
        if current["service_address_id"]:
            execute(
                "UPDATE service_addresses SET street=?, unit=?, city=?, state=?, zip=? WHERE id=?",
                (a.get("street"), a.get("unit"), a.get("city"), a.get("state", "NY"), a.get("zip"), current["service_address_id"]),
            )
        else:
            cur = execute(
                "INSERT INTO service_addresses (customer_id, street, unit, city, state, zip) VALUES (?, ?, ?, ?, ?, ?)",
                (cust_id, a.get("street"), a.get("unit"), a.get("city"), a.get("state", "NY"), a.get("zip")),
            )
            execute("UPDATE enrollments SET service_address_id=? WHERE id=?", (cur.lastrowid, enrollment_id))

    # Utility account
    if "utility_account" in data:
        u = data["utility_account"]
        current = query_one("SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
        if current["utility_account_id"]:
            execute(
                """UPDATE utility_accounts SET utility_name=?, account_number=?, meter_number=?, rate_class=?,
                   monthly_usage_kwh=?, updated_at=datetime('now') WHERE id=?""",
                (u.get("utility_name"), u.get("account_number"), u.get("meter_number"), u.get("rate_class"),
                 u.get("monthly_usage_kwh"), current["utility_account_id"]),
            )
        else:
            cur = execute(
                """INSERT INTO utility_accounts (customer_id, service_address_id, utility_name, account_number, meter_number, rate_class, monthly_usage_kwh)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (current["customer_id"], current["service_address_id"], u.get("utility_name"), u.get("account_number"),
                 u.get("meter_number"), u.get("rate_class"), u.get("monthly_usage_kwh")),
            )
            execute("UPDATE enrollments SET utility_account_id=? WHERE id=?", (cur.lastrowid, enrollment_id))

    if "project_id" in data:
        execute("UPDATE enrollments SET project_id=? WHERE id=?", (data["project_id"], enrollment_id))

    execute("UPDATE enrollments SET updated_at=datetime('now'), updated_by_user_id=? WHERE id=?", (g.current_user["id"], enrollment_id))
    audit.log("enrollment_edited", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"fields": list(data.keys())}, ip_address=request.remote_addr)

    updated = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    return jsonify(_serialize_enrollment(updated, g.current_user["role"]))


@bp.route("/<int:enrollment_id>/status", methods=["POST"])
@require_auth
def change_status(enrollment_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        new_status = status_machine.transition(
            enrollment_id, data.get("new_status"), user_id=g.current_user["id"],
            reason=data.get("reason"), notes=data.get("notes"),
            force=(g.current_user["role"] == "admin" and data.get("force", False)),
            ip_address=request.remote_addr,
        )
    except status_machine.InvalidTransition as e:
        return jsonify({"error": str(e)}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": new_status})


@bp.route("/<int:enrollment_id>/lmi", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def set_lmi(enrollment_id):
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path")
    if path not in ("document", "self_attestation", "not_applicable"):
        return jsonify({"error": "path must be document, self_attestation, or not_applicable"}), 400

    household_size = data.get("household_size")
    income_threshold = lmi_validation.ami_threshold_for(int(household_size)) if household_size else None

    cur = execute(
        """INSERT INTO lmi_qualifications
           (enrollment_id, path, qualification_type, document_id, household_size, income_threshold,
            attestation_response, attestation_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, path, data.get("qualification_type"), data.get("document_id"),
         household_size, income_threshold, data.get("attestation_response"),
         datetime.now().isoformat() if path == "self_attestation" else None),
    )
    execute("UPDATE enrollments SET lmi_path=? WHERE id=?", (path, enrollment_id))
    audit.log("lmi_recorded", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"path": path}, ip_address=request.remote_addr)

    row = query_one("SELECT * FROM lmi_qualifications WHERE id = ?", (cur.lastrowid,))
    return jsonify(dict(row)), 201
