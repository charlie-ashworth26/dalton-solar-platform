"""
Perch-facing routes.

Every Perch interaction the browser can trigger goes through here, and every
handler delegates to services.perch.adapter / services.perch.workflow. No Perch
credential, enrollment token, or client instance ever crosses toward the browser.

Milestone 2: the browser no longer asks for "products" - it asks for the current
WORKFLOW STEP and renders whatever descriptor comes back.
"""
from flask import Blueprint, request, jsonify, g

from db import query_one, execute
from auth import require_auth, require_role
from helpers import next_enrollment_code
from services import audit, status_machine
from services.perch import adapter, workflow, utilities
from services.perch.errors import PerchError, PerchNoCapacityError
from services.perch.token_manager import token_status

bp = Blueprint("perch_routes", __name__, url_prefix="/api/perch")


def _rep_for_current_user():
    return query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))


def _visible(enrollment_id):
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return None, (jsonify({"error": "Enrollment not found"}), 404)
    if g.current_user["role"] == "sales_rep":
        rep = _rep_for_current_user()
        if not rep or enrollment["sales_rep_id"] != rep["id"]:
            return None, (jsonify({"error": "Forbidden"}), 403)
    return enrollment, None


@bp.route("/drafts", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_draft():
    """Creates a Dalton enrollment draft and returns its immutable internal ID.

    Perch's enrollment_token is session-scoped and dies in 30 minutes, so it can
    never be the durable key. The Dalton Enrollment ID is issued first and is
    what everything else - documents, signatures, VIPR reconciliation - hangs off.
    """
    rep = _rep_for_current_user()
    code = next_enrollment_code()
    cur = execute(
        """INSERT INTO enrollments (enrollment_code, sales_rep_id, status, created_by_user_id, updated_by_user_id)
           VALUES (?, ?, 'Draft', ?, ?)""",
        (code, rep["id"] if rep else None, g.current_user["id"], g.current_user["id"]),
    )
    enrollment_id = cur.lastrowid
    workflow.set_state(enrollment_id, "service_area")
    audit.log("enrollment_draft_created", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"enrollment_code": code}, ip_address=request.remote_addr)
    return jsonify({
        "enrollment_id": enrollment_id,
        "enrollment_code": code,
        "status": "Draft",
        "rep_name": g.current_user["full_name"],
    }), 201


@bp.route("/utilities", methods=["GET"])
@require_auth
def list_utilities():
    """Supported utilities with their Perch slugs and POD ID rules.

    Sourced from the perch_utilities reference table (migration 002), which was
    seeded from Perch's published slug-mapping and secondary-identifier tables.
    """
    return jsonify({
        "utilities": [
            {
                "slug": u["slug"],
                "display_name": u["display_name"],
                "requires_pod_id": bool(u["requires_pod_id"]),
                "pod_id": utilities.pod_id_rule(u["slug"]),
                "slug_confirmed": bool(u["slug_confirmed"]),
            }
            for u in utilities.all_utilities()
        ],
        "source": "perch_published_slug_mapping",
    })


@bp.route("/enrollments/<int:enrollment_id>/workflow", methods=["GET"])
@require_auth
def get_workflow(enrollment_id):
    """The current step descriptor. This is what the frontend renderer consumes.

    The frontend does not decide what step it is on - this endpoint does, based
    on stored Perch responses and the next_step URL Perch handed back.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    descriptor = workflow.resolve(enrollment_id)
    if descriptor is None:
        return jsonify({"error": "Enrollment not found"}), 404
    return jsonify(descriptor)


@bp.route("/enrollments/<int:enrollment_id>/capacity", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def check_capacity(enrollment_id):
    """POST /capacity through the adapter, then return the NEXT workflow step.

    Returns 200 in both the capacity-available and no-capacity cases: per the
    docs, a 503 from Perch means "no open capacity for this utility and ZIP",
    which is a business outcome the rep needs to see, not a server error.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    zip_code = (data.get("zip_code") or "").strip()
    utility_name = (data.get("utility_name") or data.get("utility") or "").strip()
    email = (data.get("email") or "").strip()

    try:
        result = adapter.check_capacity(enrollment_id, zip_code, utility_name,
                                        email=email, user_id=g.current_user["id"])
    except PerchError as e:
        audit.log("perch_capacity_failed", enrollment_id=enrollment_id, user_id=g.current_user["id"],
                  details={"zip": zip_code, "utility": utility_name, "error": str(e)},
                  ip_address=request.remote_addr)
        return jsonify({"error": str(e), "perch_error": type(e).__name__}), getattr(e, "http_status", 502)

    step_key, recognized = workflow.resolve_next_step_key(result.get("next_step_url"))
    workflow.set_state(
        enrollment_id,
        "capacity_result" if result["capacity_available"] else "no_capacity",
        next_step_url=result.get("next_step_url"),
        recognized=recognized,
        last_response=result.get("project_details"),
    )

    if not recognized and result.get("next_step_url"):
        # Perch pointed us at something we have not implemented. Loud, not silent.
        audit.log("perch_unrecognized_next_step", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"next_step_url": result["next_step_url"]},
                  ip_address=request.remote_addr)

    audit.log("perch_capacity_checked", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"zip": zip_code, "utility_slug": result["utility_slug"],
                       "capacity_available": result["capacity_available"],
                       "token_was_refreshed": result["token_was_refreshed"]},
              ip_address=request.remote_addr)

    if result["capacity_available"] and enrollment["status"] == "Draft":
        try:
            status_machine.transition(enrollment_id, "Information Needed", user_id=g.current_user["id"],
                                       reason="Perch capacity confirmed", ip_address=request.remote_addr)
        except status_machine.InvalidTransition:
            pass

    return jsonify({"result": result, "workflow": workflow.resolve(enrollment_id)})


@bp.route("/enrollments/<int:enrollment_id>/capacity", methods=["GET"])
@require_auth
def last_capacity(enrollment_id):
    """Last stored check - resume/audit only, explicitly flagged stale."""
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    check = adapter.latest_capacity_check(enrollment_id)
    if not check:
        return jsonify({"error": "No capacity check has been performed for this enrollment yet."}), 404
    return jsonify(check)


@bp.route("/enrollments/<int:enrollment_id>/api-calls", methods=["GET"])
@require_auth
def api_calls(enrollment_id):
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    return jsonify({"enrollment_id": enrollment_id, "calls": adapter.api_call_history(enrollment_id)})


@bp.route("/enrollments/<int:enrollment_id>/token-status", methods=["GET"])
@require_auth
@require_role("admin")
def enrollment_token_status(enrollment_id):
    """Admin-only token diagnostics for one enrollment session.
    Returns expiry and refresh count - never the token value."""
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    return jsonify(token_status(enrollment_id))


@bp.route("/diagnostics", methods=["GET"])
@require_auth
@require_role("admin")
def diagnostics():
    """Surfaces anything we inferred rather than read from Perch, so an assumption
    cannot quietly become load-bearing."""
    from services.perch.config import get_api_mode
    return jsonify({
        "api_mode": get_api_mode(),
        "unconfirmed_utility_slugs": [
            {"slug": u["slug"], "display_name": u["display_name"]}
            for u in utilities.unconfirmed_slugs()
        ],
        "known_next_step_paths": list(workflow.NEXT_STEP_PATH_MAP.keys()),
    })
