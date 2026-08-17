"""
Enrollment authorization.

Promoted verbatim from routes/perch_routes.py::_visible(), which was the only
correct and consistently-applied ownership check in the codebase. Eleven other
enrollment-scoped routes had role gating but NO ownership check, so any
sales_rep could act on any enrollment by changing the id in the URL. Putting the
rule in one place means the next enrollment-scoped route inherits it instead of
re-deriving it.

OWNERSHIP MODEL (existing — nothing new was added)
--------------------------------------------------
    enrollments.sales_rep_id       -> sales_reps.id   assigned/owning rep
    enrollments.created_by_user_id -> users.id        immutable creator
    enrollments.updated_by_user_id -> users.id        last updater (audit)

NOTE the indirection: sales_rep_id points at sales_reps.id, NOT users.id. So a
rep is resolved as users.id -> sales_reps.user_id -> sales_reps.id. A user with
no sales_reps row therefore owns nothing, which is the safe default.

ACCESS BY ROLE (existing behaviour, preserved deliberately)
-----------------------------------------------------------
    sales_rep     only enrollments whose sales_rep_id matches their rep row
    admin         global
    qa_reviewer   global — needs it to review submissions
    developer     global — trusted internal troubleshooting role for now
                  (to be revisited during production security hardening)

Ownership is ALWAYS derived from the authenticated session. Nothing here reads
a rep or user id from the request body or query string.
"""
from flask import g, jsonify

from db import query_one

# Roles with global enrollment access. Everything not listed here is scoped to
# its own enrollments.
GLOBAL_ACCESS_ROLES = ("admin", "qa_reviewer", "developer")


def rep_for_current_user():
    """The sales_reps row for the authenticated user, or None.

    None is meaningful: an admin/QA/developer has no rep row, and a sales_rep
    without one owns nothing.
    """
    return query_one("SELECT * FROM sales_reps WHERE user_id = ?",
                     (g.current_user["id"],))


def can_access_enrollment(enrollment):
    """Does the authenticated user have access to this enrollment row?"""
    if enrollment is None:
        return False
    if g.current_user["role"] in GLOBAL_ACCESS_ROLES:
        return True
    if g.current_user["role"] != "sales_rep":
        return False          # unknown role: deny rather than assume
    rep = rep_for_current_user()
    return bool(rep) and enrollment["sales_rep_id"] == rep["id"]


def visible_enrollment(enrollment_id):
    """(enrollment, error_response). Exactly the semantics the Perch routes
    already used, so applying it elsewhere changes no existing behaviour.

        enrollment, err = visible_enrollment(enrollment_id)
        if err:
            return err

    404 when the enrollment does not exist, 403 when it exists but belongs to
    another rep. The distinction is deliberate and matches the behaviour reps
    and admins already see on GET /api/enrollments/<id>.
    """
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return None, (jsonify({"error": "Enrollment not found"}), 404)
    if not can_access_enrollment(enrollment):
        return None, (jsonify({"error": "Forbidden"}), 403)
    return enrollment, None
