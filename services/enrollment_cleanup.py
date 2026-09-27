"""Safe disposal of provisional enrollments left behind by a failed attempt.

THE PROBLEM THIS SOLVES
    POST /api/perch/enrollments/capacity creates the enrollment row BEFORE any
    call that can fail, and every failure path returned the error without
    touching that row. A duplicate-email rejection therefore left a permanent
    "(no customer info yet)" row on the dashboard.

WHY A PREDICATE MODULE
    The runtime path and the one-time historical sweep must judge a row by
    IDENTICAL rules. Keeping that judgement in one function - rather than
    inlining a WHERE clause at each call site - is what makes that true.

WHAT THIS IS NOT
    It is not "delete enrollments with no customer". The normal happy path has
    customer_id NULL between the capacity step and the customer step, so that
    rule alone would destroy live work in progress. A row is only ever
    discardable when it is provably LOCAL-ONLY: nothing was created downstream
    and Perch never issued an identifier for it.
"""

from db import query_one, execute
from services.perch import workflow as perch_workflow

# The only workflow steps that can precede /enroll. A row sitting anywhere else
# has moved on, and a row with NO workflow state at all is excluded as well -
# absence of evidence is not evidence that discarding is safe.
PRE_ENROLL_STEP_KEYS = ("service_area", "capacity_result")

# Every table that would indicate real downstream work. If an enrollment id
# appears in ANY of them, the row is kept.
DOWNSTREAM_TABLES = (
    "agreements",
    "documents",
    "signing_sessions",
    "perch_self_attestation_submissions",
    "customer_access_tokens",
    "lmi_qualifications",
    "submissions",
)


def discard_reason_blocked(enrollment_id):
    """Why this enrollment may NOT be discarded, or None when it is safe.

    Returns a short machine-ish string so the caller can audit the decision
    rather than silently doing nothing.
    """
    row = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not row:
        return "not_found"
    if row["discarded_at"]:
        return "already_discarded"

    # ── The commit boundary is the hard stop ──────────────────────────────
    # Once Perch has accepted /enroll, the enrollment exists on their side and
    # nothing here may remove it. perch_committed() is derived from PERSISTED
    # workflow state, so it survives a reload and cannot be argued away by
    # transient request state.
    if perch_workflow.perch_committed(enrollment_id):
        return "perch_committed"

    # Belt and braces: an explicit Perch identifier means Perch knows about it
    # regardless of what the workflow state says.
    if row["perch_enrollment_ref"]:
        return "has_perch_enrollment_ref"
    if row["perch_customer_ref"]:
        return "has_perch_customer_ref"

    # ── Local evidence of real work ───────────────────────────────────────
    if row["customer_id"]:
        return "has_customer"

    state = perch_workflow.get_state(enrollment_id)
    step_key = (state or {}).get("current_step_key")
    if step_key not in PRE_ENROLL_STEP_KEYS:
        # Covers a missing state row too, because None is not in the tuple.
        return f"workflow_step_{step_key or 'unknown'}"

    for table in DOWNSTREAM_TABLES:
        if query_one(f"SELECT 1 FROM {table} WHERE enrollment_id = ? LIMIT 1",
                     (enrollment_id,)):
            return f"has_{table}"

    return None


def is_discardable(enrollment_id):
    return discard_reason_blocked(enrollment_id) is None


def discard_provisional_enrollment(enrollment_id, reason):
    """Soft-discard an enrollment when - and only when - it is provably safe.

    Returns True when the row was discarded. Never raises on refusal: a failed
    cleanup must not turn the caller's real error (the duplicate email) into
    something else.
    """
    blocked = discard_reason_blocked(enrollment_id)
    if blocked:
        return False
    execute(
        "UPDATE enrollments SET discarded_at = datetime('now'), discarded_reason = ?, "
        "updated_at = datetime('now') WHERE id = ? AND discarded_at IS NULL",
        (reason, enrollment_id),
    )
    return True
