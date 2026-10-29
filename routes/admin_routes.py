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
            # ── THE KEY BEHIND THE LABEL (C5) ──────────────────────────────
            # `status` above is a LABEL. The Sales Report rendered it as bare
            # text while every other list in UBS shows a semantic status pill,
            # and the design system forbids choosing a colour by reading a
            # label - C1 asserts the label text is never parsed to pick one.
            # So the authoritative key this row ALREADY used to build that
            # label is serialized beside it, and the frontend reuses the same
            # resolver as every other list. Nothing is invented, nothing is
            # recomputed, and `status` is untouched, so the CSV export and
            # every existing consumer see exactly what they saw before.
            "workflow_step_key": step_key,
            "workflow_step_label": status,
            "workflow_is_terminal": (
                perch_workflow.is_terminal(step_key) if step_key else False),
            "workflow_is_blocked": (
                perch_workflow.is_blocked(step_key) if step_key else False),
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


# ═══════════════════ Sales summary (admin only) ═══════════════════
#
# A small analytics panel above the Sales Report table. READ-ONLY: it opens no
# enrollment, writes nothing, calls no partner, and adds no column - every
# field it reports is already persisted.
#
# ── WHAT COUNTS AS A COMPLETED SALE ──────────────────────────────────────
# Not a new definition. UBS already has exactly one, and three surfaces
# already read it: the customer portal (access_routes), the enrollment
# serializer, and the Sales Report row itself, which has serialized
# `workflow_is_terminal` since C5.
#
#     perch_workflow.TERMINAL_STEP_KEYS  ==  {"contracts_accepted"}
#     STEP_LABELS["contracts_accepted"]  ==  "Complete"
#
# The set is READ from the workflow module rather than written out here, so a
# terminal step added there is counted here without anyone remembering to.
# Everything short of it - a draft, an abandoned capacity check, an enrollment
# awaiting contract acceptance, an uncertain outcome, a "does not qualify" -
# is not a sale and is not counted. Discarded enrollments are excluded by the
# same `discarded_at IS NULL` rule every other listing uses.
#
# ── WHEN IT COMPLETED ────────────────────────────────────────────────────
# perch_workflow_state.updated_at. set_state() rewrites it on every step
# change and NOTHING writes it once the workflow is terminal, so for a
# completed enrollment it is the moment it reached that final step. This is
# the same column, and the same reasoning, the read-only completed view
# already relies on (enrollment_routes serializes it as workflow_updated_at).
#
# enrollments.created_at is deliberately NOT used: that is when the enrollment
# was STARTED, which for a sale completed days later lands in the wrong day,
# week and often the wrong month.

def _completed_sales_rows():
    """Every completed sale, with the facts this panel groups by.

    One row per enrollment. The JOIN to perch_workflow_state is INNER on
    purpose - an enrollment with no workflow row has not reached any Perch
    step, so it cannot be complete.
    """
    from services.perch import workflow as perch_workflow

    keys = sorted(perch_workflow.TERMINAL_STEP_KEYS)
    placeholders = ",".join("?" for _ in keys)
    # NO PROJECT NAME IS SELECTED, and that is a finding rather than an
    # omission. enrollments.project_id is written in exactly two places, both
    # from the pre-Perch flow: a developer assigning a project to a submission,
    # and the legacy enrollment PATCH. A Perch-era sale completes at
    # contracts_accepted without passing through either, so project_id is NULL
    # for them and projects.name would be blank on almost every row this panel
    # counts. Perch owns project assignment and does not report the name back.
    # Grouping is therefore by the PROGRAM, which is persisted - rather than by
    # a column that would be empty, or a name invented to fill it.
    return query(
        f"""SELECT e.id,
                   e.utility_name,
                   e.selected_customer_type,
                   w.updated_at AS completed_at
              FROM enrollments e
              JOIN perch_workflow_state w ON w.enrollment_id = e.id
             WHERE e.discarded_at IS NULL
               AND w.current_step_key IN ({placeholders})""",
        tuple(keys))


# ── WHICH PROGRAM A COMPLETED SALE USED ──────────────────────────────────
# In precedence order, and nothing is reconstructed from CURRENT capacity -
# today's availability says nothing about what a sale made in March used.
#
#   1. enrollments.selected_customer_type - the rep's explicit choice,
#      persisted by migration 008 and the value /enroll itself resolves
#      against. Authoritative whenever it is set.
#   2. The customer_type recorded on this enrollment's own successful
#      create_enrollment API call. A single-program location resolves
#      server-side and never writes (1), but what was actually sent to and
#      accepted by Perch was persisted at that moment in perch_api_calls.
#      That is a record of the enrollment, not a guess about it.
#   3. Nothing. The sale is counted under UNRECORDED_PROGRAM and says so.
#      It is never folded into Residential to make the panel look tidier.
#
# perch_workflow_state.last_response_json is NOT consulted: it holds only the
# LAST step's response, and for a completed sale that is the contract
# acceptance, which carries no customer_type. Reading it would return None for
# every completed enrollment - the same trap that once showed an LMI customer
# the residential savings rate.
UNRECORDED_PROGRAM = "Program not recorded"
UNRECORDED_UTILITY = "Utility not recorded"


def _enrolled_customer_types(enrollment_ids):
    """{enrollment_id: customer_type} from each one's successful /enroll call."""
    if not enrollment_ids:
        return {}
    import json

    placeholders = ",".join("?" for _ in enrollment_ids)
    rows = query(
        f"""SELECT enrollment_id, request_json
              FROM perch_api_calls
             WHERE operation = 'create_enrollment'
               AND status_code = 200
               AND enrollment_id IN ({placeholders})
          ORDER BY id ASC""",
        tuple(enrollment_ids))
    out = {}
    for r in rows:
        try:
            payload = json.loads(r["request_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("customer_type"):
            # Ascending id, so the LAST successful enrollment call wins - a
            # re-enrollment after a correction is the one that stands.
            out[r["enrollment_id"]] = payload["customer_type"]
    return out


# ── TIME RANGES, IN THE BUSINESS TIMEZONE ────────────────────────────────
# The server stores completion times in UTC (SQLite datetime('now')), and a
# sale closed at 8pm in New York is already "tomorrow" in UTC. Bucketing on
# the stored string would file that sale under the wrong day, and on the 31st
# under the wrong month.
#
# So the boundaries are computed as Eastern wall-clock instants and converted
# back to UTC for comparison. The zone is NOT a second opinion invented here:
# it is the one UBS already runs on, services.perch.client.acceptance_timezone()
# (America/New_York, overridable with PERCH_ACCEPTANCE_TIMEZONE), the same zone
# whose absence once made Perch reject acceptance timestamps as being in the
# future. One business timezone, one definition.
#
# Comparison is a plain string range because every stored value is UTC in the
# single format datetime('now') produces ("YYYY-MM-DD HH:MM:SS"), which sorts
# lexicographically exactly as it sorts chronologically.

#: A week starts MONDAY. Sales weeks here follow the ISO week rather than a
#: Sunday start; the panel prints the exact dates of the window it is showing,
#: so the choice is visible on screen rather than assumed.
WEEK_STARTS_MONDAY = True

SQLITE_UTC = "%Y-%m-%d %H:%M:%S"


def _business_now():
    """Now, as a wall clock in the business timezone."""
    from datetime import datetime
    from services.perch.client import acceptance_timezone

    return datetime.now(acceptance_timezone())


def _range_bounds(now_local, range_key):
    """(start_local, end_local) for a range, as business-timezone instants.

    End is the START of the next period, never "now": a half-open [start, end)
    window cannot double-count a sale at a boundary and cannot drop one whose
    recorded second is a moment ahead of the server's clock.
    """
    from datetime import timedelta

    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "today":
        return midnight, midnight + timedelta(days=1)
    if range_key == "week":
        back = midnight.weekday() if WEEK_STARTS_MONDAY else (midnight.weekday() + 1) % 7
        start = midnight - timedelta(days=back)
        return start, start + timedelta(days=7)
    # month
    start = midnight.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


def _as_stored_utc(local_dt):
    """A business-timezone instant in the exact format the DB stores."""
    from datetime import timezone

    return local_dt.astimezone(timezone.utc).strftime(SQLITE_UTC)


RANGE_LABELS = {"today": "Today", "week": "This Week", "month": "This Month"}
RANGE_KEYS = ("today", "week", "month")


def _summarize(rows, programs_by_id, start_utc, end_utc):
    """Group one range's completed sales by utility, then by program.

    Reconciliation is structural rather than asserted afterwards: each sale is
    counted once into exactly one utility bucket and once into exactly one
    program bucket inside it, so the utility totals sum to the overall total
    and each utility's programs sum to that utility.
    """
    from routes.enrollment_routes import _utility_display
    from services.perch import adapter

    by_utility = {}
    by_program = {}
    total = 0
    for r in rows:
        completed = r["completed_at"] or ""
        if not (start_utc <= completed < end_utc):
            continue
        total += 1
        utility = _utility_display(r["utility_name"]) or UNRECORDED_UTILITY
        raw_type = r["selected_customer_type"] or programs_by_id.get(r["id"])
        program = adapter.program_label(raw_type) if raw_type else UNRECORDED_PROGRAM

        u = by_utility.setdefault(utility, {"utility": utility, "count": 0,
                                            "programs": {}})
        u["count"] += 1
        u["programs"][program] = u["programs"].get(program, 0) + 1
        by_program[program] = by_program.get(program, 0) + 1

    def share(n):
        return round(n * 100.0 / total, 1) if total else 0.0

    utilities = sorted(by_utility.values(),
                       key=lambda u: (-u["count"], u["utility"]))
    for u in utilities:
        u["share"] = share(u["count"])
        u["programs"] = sorted(
            ({"program": name, "count": n, "share": share(n)}
             for name, n in u["programs"].items()),
            key=lambda p: (-p["count"], p["program"]))

    programs = sorted(
        ({"program": name, "count": n, "share": share(n)}
         for name, n in by_program.items()),
        key=lambda p: (-p["count"], p["program"]))

    return {
        "total": total,
        "utilities_active": len(utilities),
        "top_utility": utilities[0] if utilities else None,
        "top_program": programs[0] if programs else None,
        "by_utility": utilities,
        "by_program": programs,
    }


@bp.route("/sales-summary", methods=["GET"])
@require_auth
@require_role("admin")
def sales_summary():
    """Completed-sale counts for today, this week and this month.

    ALL THREE RANGES COME BACK IN ONE RESPONSE. The selector then switches
    instantly with no second request, and - more importantly - the three are
    measured against ONE clock reading, so Today can never be larger than This
    Week because the month rolled over between two round trips.
    """
    try:
        now_local = _business_now()
    except RuntimeError as e:
        # The timezone database is missing on this host. Reporting UTC day
        # boundaries instead would quietly file evening sales under tomorrow,
        # so the panel says what is wrong rather than showing wrong numbers.
        return jsonify({
            "error": "Sales summary is unavailable because the server's timezone "
                     "database is missing, so business-day boundaries cannot be "
                     "calculated. Install tzdata on the server.",
            "code": "timezone_unavailable",
            "detail": str(e),
        }), 503

    rows = _completed_sales_rows()
    programs_by_id = _enrolled_customer_types([r["id"] for r in rows])

    ranges = {}
    for key in RANGE_KEYS:
        start_local, end_local = _range_bounds(now_local, key)
        summary = _summarize(rows, programs_by_id,
                             _as_stored_utc(start_local), _as_stored_utc(end_local))
        summary["range"] = key
        summary["label"] = RANGE_LABELS[key]
        summary["window"] = {
            "timezone": str(now_local.tzinfo),
            "start_local": start_local.isoformat(),
            "end_local": end_local.isoformat(),
            "start_utc": _as_stored_utc(start_local),
            "end_utc": _as_stored_utc(end_local),
        }
        ranges[key] = summary

    return jsonify({
        "ranges": ranges,
        "order": list(RANGE_KEYS),
        "generated_at_local": now_local.isoformat(),
        "week_starts": "monday" if WEEK_STARTS_MONDAY else "sunday",
        # Named so the panel can label its own empty and fallback buckets
        # without hardcoding a second copy of these strings.
        "unrecorded_program_label": UNRECORDED_PROGRAM,
        "unrecorded_utility_label": UNRECORDED_UTILITY,
    })
