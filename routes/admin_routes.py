"""
Admin rep account management.

Before this, the ONLY way to create a rep was editing seed.py or hand-writing
SQL. This gives an admin a small, deliberately narrow surface for onboarding and
managing sales reps.

DELIBERATE LIMITS — these are security properties, not omissions:
  * role is ALWAYS 'sales_rep', hardcoded server-side. This surface can never
    create or promote an admin, qa_reviewer or developer, so compromising an
    admin session cannot mint more privileged accounts here.
  * NO delete route. sales_reps.user_id is ON DELETE CASCADE, so deleting a user
    would silently destroy their rep row and orphan enrollments.sales_rep_id.
    Deactivation is the supported path and preserves all history.
  * NO role-change route.
  * An admin cannot deactivate themselves (lock-out protection).
  * Password hashes are never returned by any route here.
"""
import re

from flask import Blueprint, request, jsonify, g, Response

from db import query, query_one, execute, transaction
from auth import require_auth, require_role, hash_password
from services import audit

bp = Blueprint("admin", __name__, url_prefix="/api/admin")

# This management surface creates exactly one role. Never taken from input.
MANAGED_ROLE = "sales_rep"

MIN_PASSWORD_LENGTH = 10
REP_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{1,31}$")


def _normalize_email(raw):
    """Trim + lowercase. users.email is UNIQUE but CASE-SENSITIVE in SQLite, so
    without this 'Rep@x.com' and 'rep@x.com' would both insert and the second
    account would be permanently unreachable (login does an exact match)."""
    return (raw or "").strip().lower()


def _validate_password(pw):
    if not pw or len(pw) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def _next_rep_code(tx=None):
    """Server-generated REP-001, REP-002, ... Never trusts frontend input.

    Derived from the highest existing REP-<n>, so it does not collide with codes
    an admin typed manually. The UNIQUE constraint on sales_reps.rep_code is the
    real guarantee; this just picks a sensible candidate.
    """
    rows = query("SELECT rep_code FROM sales_reps WHERE rep_code LIKE 'REP-%'")
    highest = 0
    for r in rows:
        m = re.fullmatch(r"REP-(\d+)", (r["rep_code"] or "").strip())
        if m:
            highest = max(highest, int(m.group(1)))
    return f"REP-{highest + 1:03d}"


def _rep_row(user_id):
    """A rep as this API exposes it. password_hash is never selected."""
    return query_one(
        """SELECT u.id AS user_id, u.email, u.full_name, u.is_active, u.role,
                  u.created_at, sr.id AS sales_rep_id, sr.rep_code, sr.phone, sr.team,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.sales_rep_id = sr.id)
                      AS enrollment_count
             FROM users u
             JOIN sales_reps sr ON sr.user_id = u.id
            WHERE u.id = ? AND u.role = ?""",
        (user_id, MANAGED_ROLE))


def _managed_rep_or_404(user_id):
    """Only sales reps are manageable here. An attempt to manage an admin,
    qa_reviewer or developer through this surface looks like a missing record."""
    rep = _rep_row(user_id)
    if not rep:
        return None, (jsonify({"error": "Sales rep not found"}), 404)
    return rep, None


# ─────────────── List ───────────────

@bp.route("/reps", methods=["GET"])
@require_auth
@require_role("admin")
def list_reps():
    rows = query(
        """SELECT u.id AS user_id, u.email, u.full_name, u.is_active,
                  u.created_at, sr.id AS sales_rep_id, sr.rep_code, sr.phone, sr.team,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.sales_rep_id = sr.id)
                      AS enrollment_count
             FROM users u
             JOIN sales_reps sr ON sr.user_id = u.id
            WHERE u.role = ?
         ORDER BY u.is_active DESC, u.full_name ASC""",
        (MANAGED_ROLE,))
    return jsonify([dict(r) for r in rows])


# ─────────────── Create ───────────────

@bp.route("/reps", methods=["POST"])
@require_auth
@require_role("admin")
def create_rep():
    """Create the users row and the sales_reps row ATOMICALLY.

    Both writes share one transaction, so a failure on either leaves neither.
    A user row with no sales_reps row would be a rep who can log in, owns
    nothing, and never appears in this list - the worst possible half-state.
    """
    data = request.get_json(force=True, silent=True) or {}

    full_name = (data.get("full_name") or "").strip()
    email = _normalize_email(data.get("email"))
    password = data.get("password") or ""
    phone = (data.get("phone") or "").strip() or None
    team = (data.get("team") or "").strip() or None
    rep_code = (data.get("rep_code") or "").strip()

    if not full_name:
        return jsonify({"error": "Full name is required."}), 400
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required."}), 400
    pw_err = _validate_password(password)
    if pw_err:
        return jsonify({"error": pw_err}), 400

    if rep_code and not REP_CODE_PATTERN.match(rep_code):
        return jsonify({"error": "Rep code may use letters, numbers, hyphens and "
                                 "underscores only (2-32 characters)."}), 400

    # Friendly pre-checks. The UNIQUE constraints below remain the real
    # guarantee against a race between two admins submitting at once.
    if query_one("SELECT id FROM users WHERE lower(trim(email)) = ?", (email,)):
        return jsonify({"error": f"A user with the email {email} already exists."}), 409
    if rep_code and query_one("SELECT id FROM sales_reps WHERE rep_code = ?", (rep_code,)):
        return jsonify({"error": f"Rep code {rep_code} is already in use."}), 409

    if not rep_code:
        rep_code = _next_rep_code()

    try:
        with transaction() as tx:
            cur = tx.execute(
                """INSERT INTO users (email, password_hash, role, full_name, is_active)
                   VALUES (?, ?, ?, ?, 1)""",
                # role is hardcoded - never read from the request body.
                (email, hash_password(password), MANAGED_ROLE, full_name))
            user_id = cur.lastrowid
            tx.execute(
                """INSERT INTO sales_reps (user_id, rep_code, phone, team)
                   VALUES (?, ?, ?, ?)""",
                (user_id, rep_code, phone, team))
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg and "email" in msg:
            return jsonify({"error": f"A user with the email {email} already exists."}), 409
        if "unique" in msg and "rep_code" in msg:
            return jsonify({"error": f"Rep code {rep_code} is already in use."}), 409
        if "unique" in msg:
            return jsonify({"error": "That rep conflicts with an existing record."}), 409
        return jsonify({"error": "Could not create the rep. No changes were saved."}), 400

    audit.log("admin_rep_created", user_id=g.current_user["id"],
              details={"new_user_id": user_id, "email": email, "rep_code": rep_code},
              ip_address=request.remote_addr)
    return jsonify(dict(_rep_row(user_id))), 201


# ─────────────── Update contact details ───────────────

@bp.route("/reps/<int:user_id>", methods=["PATCH"])
@require_auth
@require_role("admin")
def update_rep(user_id):
    """Phone, team and rep code ONLY.

    Deliberately cannot change: role, email, is_active (use the explicit
    activate/deactivate routes), or anything touching enrollment ownership.
    """
    rep, err = _managed_rep_or_404(user_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}

    updates, params = [], []
    if "phone" in data:
        updates.append("phone = ?")
        params.append((data.get("phone") or "").strip() or None)
    if "team" in data:
        updates.append("team = ?")
        params.append((data.get("team") or "").strip() or None)
    if "rep_code" in data:
        new_code = (data.get("rep_code") or "").strip()
        if not new_code:
            return jsonify({"error": "Rep code cannot be blank."}), 400
        if not REP_CODE_PATTERN.match(new_code):
            return jsonify({"error": "Rep code may use letters, numbers, hyphens and "
                                     "underscores only (2-32 characters)."}), 400
        clash = query_one("SELECT user_id FROM sales_reps WHERE rep_code = ? AND user_id != ?",
                          (new_code, user_id))
        if clash:
            return jsonify({"error": f"Rep code {new_code} is already in use."}), 409
        updates.append("rep_code = ?")
        params.append(new_code)

    if not updates:
        return jsonify({"error": "Nothing to update. Send phone, team or rep_code."}), 400

    params.append(user_id)
    try:
        execute(f"UPDATE sales_reps SET {', '.join(updates)} WHERE user_id = ?", params)
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "That rep code is already in use."}), 409
        return jsonify({"error": "Could not update the rep."}), 400

    audit.log("admin_rep_updated", user_id=g.current_user["id"],
              details={"target_user_id": user_id,
                       "fields": [u.split(" =")[0] for u in updates]},
              ip_address=request.remote_addr)
    return jsonify(dict(_rep_row(user_id)))


# ─────────────── Password reset ───────────────

@bp.route("/reps/<int:user_id>/password", methods=["POST"])
@require_auth
@require_role("admin")
def reset_rep_password(user_id):
    rep, err = _managed_rep_or_404(user_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    password = data.get("password") or ""
    pw_err = _validate_password(password)
    if pw_err:
        return jsonify({"error": pw_err}), 400

    # Same PBKDF2 helper the rest of the app uses. The hash is never returned.
    execute("UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (hash_password(password), user_id))
    audit.log("admin_rep_password_reset", user_id=g.current_user["id"],
              details={"target_user_id": user_id}, ip_address=request.remote_addr)
    return jsonify({"user_id": user_id, "password_reset": True})


# ─────────────── Activate / deactivate ───────────────

@bp.route("/reps/<int:user_id>/deactivate", methods=["POST"])
@require_auth
@require_role("admin")
def deactivate_rep(user_id):
    """Blocks login AND invalidates already-issued tokens, because require_auth
    re-checks is_active on every request. Preserves the users row, the
    sales_reps row, enrollment ownership and all history."""
    if user_id == g.current_user["id"]:
        # Not reachable today (an admin is not a managed rep) but asserted here
        # so it stays true if this surface ever widens.
        return jsonify({"error": "You cannot deactivate your own account."}), 400
    rep, err = _managed_rep_or_404(user_id)
    if err:
        return err
    execute("UPDATE users SET is_active = 0, updated_at = datetime('now') WHERE id = ?",
            (user_id,))
    audit.log("admin_rep_deactivated", user_id=g.current_user["id"],
              details={"target_user_id": user_id}, ip_address=request.remote_addr)
    return jsonify(dict(_rep_row(user_id)))


@bp.route("/reps/<int:user_id>/activate", methods=["POST"])
@require_auth
@require_role("admin")
def activate_rep(user_id):
    rep, err = _managed_rep_or_404(user_id)
    if err:
        return err
    execute("UPDATE users SET is_active = 1, updated_at = datetime('now') WHERE id = ?",
            (user_id,))
    audit.log("admin_rep_activated", user_id=g.current_user["id"],
              details={"target_user_id": user_id}, ip_address=request.remote_addr)
    return jsonify(dict(_rep_row(user_id)))


# ─────────────────────────── Sales report (admin only) ───────────────────────────
#
# Every enrollment on UBS with the rep who created it. Admin-only, and the
# authorization is on the ROUTES - hiding the nav link is convenience, not the
# boundary, exactly as the rep-management surface above.
#
# ATTRIBUTION reuses what UBS already stores:
#     enrollments.sales_rep_id -> sales_reps.id -> users.full_name
# the same chain Rep Management reads. No second attribution system, nothing
# typed by a rep, and never inferred from customer data. An enrollment whose
# rep row is missing reports "Unassigned" rather than borrowing a name.
#
# The table and the CSV are built from ONE query, so the export cannot drift
# from what is on screen and is never assembled from client-side rows.


SALES_REPORT_COLUMNS = [
    "Agent Name", "Customer Name", "Enrollment #", "Customer Email",
    "Status", "Utility", "Added", "Last Modified",
]


def _sales_report_rows():
    """Every enrollment, newest first, with its rep resolved.

    LEFT JOINs throughout: an enrollment with no customer or no rep yet is
    still a real enrollment and must appear. Discarded rows are excluded for
    the same reason every other listing excludes them - they are failed
    attempts, not sales.
    """
    from routes.enrollment_routes import _utility_display
    from services.perch import workflow as perch_workflow

    rows = query(
        """SELECT e.id,
                  e.enrollment_code,
                  e.status,
                  e.utility_name,
                  e.created_at,
                  e.updated_at,
                  u.full_name   AS agent_name,
                  c.first_name  AS customer_first,
                  c.last_name   AS customer_last,
                  c.email       AS customer_email,
                  w.current_step_key AS step_key
             FROM enrollments e
             LEFT JOIN sales_reps sr ON sr.id = e.sales_rep_id
             LEFT JOIN users u       ON u.id  = sr.user_id
             LEFT JOIN customers c   ON c.id  = e.customer_id
             LEFT JOIN perch_workflow_state w ON w.enrollment_id = e.id
            WHERE e.discarded_at IS NULL
         ORDER BY e.created_at DESC, e.id DESC""")

    out = []
    for r in rows:
        name = " ".join(p for p in (r["customer_first"], r["customer_last"]) if p).strip()
        # Same status the Customers table shows - the Perch workflow label -
        # falling back to the legacy enrollments.status when Perch has not
        # produced a step yet.
        step_key = r["step_key"]
        status = perch_workflow.step_label(step_key) if step_key else r["status"]
        out.append({
            "enrollment_id": r["id"],
            "agent_name": r["agent_name"] or "Unassigned",
            "customer_name": name,
            "enrollment_code": r["enrollment_code"],
            "customer_email": r["customer_email"],
            "status": status,
            "utility": _utility_display(r["utility_name"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


@bp.route("/sales-report", methods=["GET"])
@require_auth
@require_role("admin")
def sales_report():
    """JSON for the Sales Report table. Every enrollment, not a page of them."""
    rows = _sales_report_rows()
    return jsonify({"columns": SALES_REPORT_COLUMNS, "rows": rows, "count": len(rows)})


@bp.route("/sales-report.csv", methods=["GET"])
@require_auth
@require_role("admin")
def sales_report_csv():
    """The same rows as a CSV download, generated server-side.

    Built from _sales_report_rows() - the identical query the table uses - so
    the file contains every enrollment regardless of what the browser happened
    to have rendered.
    """
    import csv
    import io
    from datetime import date

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(SALES_REPORT_COLUMNS)
    for r in _sales_report_rows():
        writer.writerow([
            r["agent_name"], r["customer_name"], r["enrollment_code"],
            r["customer_email"], r["status"], r["utility"],
            r["created_at"], r["updated_at"],
        ])

    audit.log("admin_sales_report_exported", user_id=g.current_user["id"],
              details={"row_count": len(_sales_report_rows())},
              ip_address=request.remote_addr)

    filename = f"UBS_Sales_Report_{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
