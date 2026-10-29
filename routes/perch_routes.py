"""
Perch-facing routes.

Every Perch interaction the browser can trigger goes through here, and every
handler delegates to services.perch.adapter / services.perch.workflow. No Perch
credential, enrollment token, or client instance ever crosses toward the browser.

Milestone 2: the browser no longer asks for "products" - it asks for the current
WORKFLOW STEP and renders whatever descriptor comes back.
"""
import json
import os
import secrets
import time
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify, g, redirect, make_response
from markupsafe import escape

from db import query_one, query, execute
from auth import (require_auth, require_role, require_staff_or_customer,
                  require_customer_auth)
from helpers import next_enrollment_code, resolve_stored_path
from services import audit, status_machine, enrollment_cleanup, zip_state
from services import rep_errors
from services.perch import adapter, workflow, utilities, markets
from services.perch import capacity
from services.perch import client as perch_client
from services.perch.errors import (
    PerchError, PerchNoCapacityError, PerchValidationError, PerchAmbiguousOutcomeError,
)
from services.perch.token_manager import token_status
from services.perch.client import acceptance_timestamp

bp = Blueprint("perch_routes", __name__, url_prefix="/api/perch")

# A contract Review click originates in authenticated JavaScript, but the new
# browser tab cannot carry Dalton's Authorization header. Issue a short-lived,
# one-time same-origin capability instead of ever returning Perch's presigned
# S3 URL in JSON. This is process-local for the current prototype; moving it to
# Redis is the natural multi-worker production upgrade.
_contract_review_tokens = {}
_CONTRACT_REVIEW_TTL_SECONDS = 5 * 60


def _rep_for_current_user():
    return query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))


def _actor_id():
    """The acting user id for audit/adapter calls. None for a customer."""
    return getattr(g, "actor_user_id", None) if hasattr(g, "actor_user_id") else g.current_user["id"]


def _actor_details(extra=None):
    actor = getattr(g, "actor_description", None)
    if not actor:
        user = getattr(g, "current_user", None)
        actor = f"user:{user['id']}" if user else "unknown"
    d = {"actor": actor}
    if extra:
        d.update(extra)
    return d


def _visible_to_actor(enrollment_id):
    """Visibility for a route reachable by BOTH a rep and a customer.

    A customer may only ever touch the single enrollment bound to their token.
    A rep falls through to the existing _visible() rules.
    """
    customer = getattr(g, "current_customer", None)
    if customer:
        enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
        if not enrollment:
            return None, (jsonify({"error": "Enrollment not found"}), 404)
        # Two independent checks: the token's enrollment id AND ownership.
        if str(getattr(g, "customer_enrollment_id", None)) != str(enrollment_id):
            return None, (jsonify({"error": "Forbidden"}), 403)
        if enrollment["customer_id"] != customer["id"]:
            return None, (jsonify({"error": "Forbidden"}), 403)
        return enrollment, None
    return _visible(enrollment_id)


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

    Perch's enrollment_token is session-scoped and dies in 1 hour, so it can
    never be the durable key. The Dalton Enrollment ID is issued first and is
    what everything else - documents, signatures, VIPR reconciliation - hangs off.
    """
    data = request.get_json(force=True, silent=True) or {}
    created, err = _create_enrollment_row(data.get("project_id"))
    if err:
        return err
    return jsonify(created), 201


def _create_enrollment_row(project_id=None):
    """Insert one enrollment and its initial workflow state.

    (payload, None) or (None, error_response). Shared by POST /drafts and by the
    deferred creation path, so there is exactly ONE place an enrollment row is
    born and ownership stamping cannot drift between them.
    """
    rep = _rep_for_current_user()
    utility_slug = None
    if project_id:
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            return None, (jsonify({"error": "Project not found"}), 404)
        utility_slug = utilities.resolve_slug(project["utility"])
        if not utility_slug:
            return None, (jsonify({"error": "This Dalton project does not map to a "
                                            "published Perch utility slug."}), 400)
    code = next_enrollment_code()
    cur = execute(
        """INSERT INTO enrollments (enrollment_code, project_id, sales_rep_id, status,
                                    utility_name, created_by_user_id, updated_by_user_id)
           VALUES (?, ?, ?, 'Draft', ?, ?, ?)""",
        (code, project_id, rep["id"] if rep else None, utility_slug,
         g.current_user["id"], g.current_user["id"]),
    )
    enrollment_id = cur.lastrowid
    workflow.set_state(enrollment_id, "service_area")
    audit.log("enrollment_draft_created", enrollment_id=enrollment_id,
              user_id=g.current_user["id"], details={"enrollment_code": code},
              ip_address=request.remote_addr)
    return {
        "enrollment_id": enrollment_id,
        "enrollment_code": code,
        "status": "Draft",
        "rep_name": g.current_user["full_name"],
    }, None


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
                "account_number_length": u.get("account_number_length"),
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
    descriptor = _workflow_descriptor(enrollment_id)
    if descriptor is None:
        return jsonify({"error": "Enrollment not found"}), 404
    return jsonify(descriptor)


@bp.route("/enrollments/<int:enrollment_id>/restart-service-area", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def restart_service_area(enrollment_id):
    """Return to the one service-area screen before /enroll has started.

    Perch enrollment tokens are tied to the email used to open the session. If
    the rep corrects that email, merely changing Dalton's local email while
    reusing the old token can associate the next call with the wrong Perch
    session. Therefore a service-area restart deliberately deactivates the
    current local token and clears the token email. Historical capacity rows
    remain untouched as audit records.

    Once /enroll has advanced the Perch workflow, service-area identity can no
    longer be rewritten safely; the route fails instead of inventing a rewind.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    state = workflow.get_state(enrollment_id) or {}
    current = state.get("current_step_key") or "service_area"
    if current not in {"service_area", "capacity_result", "no_capacity"}:
        return jsonify({
            "error": "This Perch enrollment has already advanced past capacity and cannot be restarted from service area."
        }), 409

    execute("UPDATE perch_tokens SET is_active=0 WHERE enrollment_id=?", (enrollment_id,))
    if enrollment["project_id"]:
        # The Dalton project fixes the utility. Keep that mapping while clearing
        # the customer/session-specific values.
        execute(
            "UPDATE enrollments SET perch_token_email=NULL, service_zip=NULL, updated_at=datetime('now') WHERE id=?",
            (enrollment_id,),
        )
    else:
        execute(
            """UPDATE enrollments
               SET perch_token_email=NULL, service_zip=NULL, utility_name=NULL, updated_at=datetime('now')
               WHERE id=?""",
            (enrollment_id,),
        )
    workflow.set_state(enrollment_id, "service_area", next_step_url=None, recognized=True)
    audit.log(
        "perch_service_area_restarted", enrollment_id=enrollment_id,
        user_id=g.current_user["id"], details={"prior_step": current},
        ip_address=request.remote_addr,
    )
    return jsonify({"workflow": _workflow_descriptor(enrollment_id)})


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

    step_key, recognized = _next_key_of(result)
    workflow.set_state(
        enrollment_id,
        "capacity_result" if result["capacity_available"] else "no_capacity",
        next_step_url=_next_step_raw(result),
        recognized=recognized,
        last_response=result.get("project_details"),
    )

    # E1: asks for the value, not for a version's name for it. An
    # unrecognized v2 path has to be as loud as an unrecognized v1 URL.
    _unrecognized = _next_step_raw(result)
    if not recognized and _unrecognized:
        # Perch pointed us at something we have not implemented. Loud, not silent.
        audit.log("perch_unrecognized_next_step", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"next_step_url": _unrecognized},
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

    return jsonify({"result": result,
                    "workflow": _workflow_descriptor(enrollment_id)})


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
    from config_bootstrap import perch_config_report
    cfg = perch_config_report()   # presence flags only - never secret values
    return jsonify({
        "api_mode": get_api_mode(),
        "configuration": cfg,
        "unconfirmed_utility_slugs": [
            {"slug": u["slug"], "display_name": u["display_name"]}
            for u in utilities.unconfirmed_slugs()
        ],
        "known_next_step_paths": list(workflow.NEXT_STEP_PATH_MAP.keys()),
    })



def _perch_error_response(enrollment_id, operation, exc):
    """The rep-facing half of a failed Perch call.

    D-2. This used to return str(exc) verbatim, so a rep could be shown
    "Perch returned 503 ...", an endpoint path or an exception class name. The
    full text is still recorded in the audit entry below, and in the
    perch_api_calls record the adapter writes, so nothing is hidden from an
    admin, a developer or a log. Only the sentence on the rep's screen changes.

    A message already written FOR a rep - the E3 422 field translations, which
    name the exact field to correct - passes through untouched: rep_message()
    replaces only what carries internals.
    """
    audit.log(
        f"perch_{operation}_failed", enrollment_id=enrollment_id,
        user_id=_actor_id(),
        # The diagnostic detail, kept whole.
        details=_actor_details({"error": str(exc), "error_class": type(exc).__name__}),
        ip_address=request.remote_addr,
    )
    return jsonify({
        "error": rep_errors.for_operation(operation, exc),
        # Kept because the browser routes one documented condition on it
        # (PerchEnrollmentInProgressError). It is a class name, not prose, and
        # is never rendered to the rep.
        "perch_error": type(exc).__name__,
    }), getattr(exc, "http_status", 502)


def _next_key(value):
    """Step key for a next_step that may be a v1 URL or a v2 path.

    Kept for callers that already hold the raw value; prefer
    _next_key_of(result), which does not make the caller choose a
    spelling."""
    key, recognized = workflow.resolve_next_step_key(value)
    return key, recognized


def _next_step_raw(result):
    """The next_step VALUE from a normalized result, whichever key held
    it. Stored as-is: a v1 full URL or a v2 bare path, exactly as Perch
    sent it, so the audit record is what came back and not our reading
    of it. The column is still named *_url for now - renaming it is
    database work, not transport work."""
    return (result or {}).get("next_step") or (result or {}).get("next_step_url")


def _next_key_of(result):
    """Step key straight from a normalized Perch result (E1).

    Routes used to read `result["next_step_url"]` - a v1 name for a
    value that is a bare path on v2 - which made every call site care
    which version answered. They ask for the step instead, and the
    client/workflow layer decides which key carried it."""
    key, recognized, _raw = workflow.resolve_next_step(result)
    return key, recognized


def _stored_document(enrollment_id, category, document_id=None):
    if document_id:
        return query_one(
            "SELECT * FROM documents WHERE id=? AND enrollment_id=? AND doc_category=?",
            (document_id, enrollment_id, category),
        )
    return query_one(
        "SELECT * FROM documents WHERE enrollment_id=? AND doc_category=? ORDER BY id DESC LIMIT 1",
        (enrollment_id, category),
    )


def _set_documents_for(enrollment_id, doc, category):
    """Every document of the SET that `doc` belongs to, in page order.

    A multi-file upload is stored as a document SET: one row per file, sharing
    a document_set_id and ordered by page_order. Sending only the single
    document_id the caller named dropped every file after the first.

    Falls back to the one document when it has no set - a single-file upload is
    complete on its own, and that path is unchanged, which is what keeps every
    historical set-less document working exactly as before.

    Scoped to THIS enrollment AND this category, so a set id can never pull in
    another enrollment's documents or another category's.
    """
    set_id = doc["document_set_id"] if "document_set_id" in doc.keys() else None
    if not set_id:
        return [doc]
    pages = query(
        """SELECT * FROM documents
            WHERE document_set_id = ? AND enrollment_id = ? AND doc_category = ?
         ORDER BY COALESCE(page_order, 0), id""",
        (set_id, enrollment_id, category))
    return list(pages) if pages else [doc]


def _bill_documents_for(enrollment_id, doc):
    """Every page of the bill `doc` belongs to. Behaviour unchanged."""
    return _set_documents_for(enrollment_id, doc, "utility_bill")


def _bill_pdf_path(doc):
    """Return (path, cleanup_path). Perch accepts bills as PDF only.

    Existing Dalton OCR still accepts JPG/PNG. For those files we create a
    transient PDF representation solely for the Perch request and delete it
    immediately afterwards; the original Dalton document remains unchanged.
    """
    import os
    import tempfile
    source = resolve_stored_path(doc["stored_path"])
    if not os.path.exists(source):
        raise PerchValidationError("The saved utility bill file is missing. Please upload it again.")
    if (doc["mime_type"] == "application/pdf") or source.lower().endswith(".pdf"):
        if os.path.getsize(source) > 4 * 1024 * 1024:
            raise PerchValidationError("The saved utility bill exceeds Perch's 4 MB limit.")
        return source, None
    from PIL import Image
    img = Image.open(source)
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, "white")
        alpha = img.getchannel("A") if "A" in img.getbands() else None
        bg.paste(img.convert("RGBA"), mask=alpha)
        img = bg
    else:
        img = img.convert("RGB")
    fd, path = tempfile.mkstemp(prefix="dalton-perch-bill-", suffix=".pdf")
    os.close(fd)
    img.save(path, "PDF", resolution=150.0)
    if os.path.getsize(path) > 4 * 1024 * 1024:
        os.remove(path)
        raise PerchValidationError("The PDF representation of this bill exceeds Perch's 4 MB limit.")
    return path, path


@bp.route("/enrollments/<int:enrollment_id>/enroll", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_perch_enrollment(enrollment_id):
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    customer = query_one("SELECT * FROM customers WHERE id=?", (enrollment["customer_id"],)) if enrollment["customer_id"] else None
    svc = query_one("SELECT * FROM service_addresses WHERE id=?", (enrollment["service_address_id"],)) if enrollment["service_address_id"] else None
    acct = query_one("SELECT * FROM utility_accounts WHERE id=?", (enrollment["utility_account_id"],)) if enrollment["utility_account_id"] else None
    if not customer or not svc or not acct:
        return jsonify({"error": "Customer, service address, and utility account must be saved before enrollment."}), 400

    data = request.get_json(force=True, silent=True) or {}
    document_id = data.get("document_id")
    if not document_id:
        return jsonify({"error": "The exact saved utility-bill document is required before continuing."}), 400
    doc = _stored_document(enrollment_id, "utility_bill", document_id=document_id)
    if not doc:
        return jsonify({"error": "That saved utility bill does not belong to this enrollment. Please choose the bill again."}), 400

    billing = {
        "address_1": enrollment["billing_street"] or (svc["street"] if enrollment["billing_same_as_service"] else None),
        "address_2": enrollment["billing_unit"] if enrollment["billing_street"] else (svc["unit"] if enrollment["billing_same_as_service"] else None),
        "city": enrollment["billing_city"] or (svc["city"] if enrollment["billing_same_as_service"] else None),
        "state": enrollment["billing_state"] or (svc["state"] if enrollment["billing_same_as_service"] else None),
        "zip": enrollment["billing_zip"] or (svc["zip"] if enrollment["billing_same_as_service"] else None),
    }
    if any(not billing[k] for k in ("address_1", "city", "state", "zip")):
        return jsonify({"error": "A complete billing/mailing address is required."}), 400

    digits = ''.join(ch for ch in (customer["phone"] or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return jsonify({"error": "Phone number must contain exactly 10 US digits."}), 400

    # EVERY PAGE OF THE BILL, NOT JUST THE ONE NAMED.
    # A bill photographed page by page is stored as a document SET
    # (documents.document_set_id, ordered by page_order). Sending only the
    # document_id the browser happened to name transmitted page 1 and silently
    # dropped the rest, which is how enrollments reached Perch missing their
    # usage/sizing pages.
    #
    # Perch's multipart contract already carries several bills per account -
    # utility_accounts[0][utility_bills][0..n] - and build_enrollment_multipart()
    # already emits that indexing. No payload format is invented here.
    bill_docs = _bill_documents_for(enrollment_id, doc)

    pdf_paths = []
    cleanup_paths = []
    try:
        for bill_doc in bill_docs:
            path, cleanup = _bill_pdf_path(bill_doc)
            pdf_paths.append(path)
            if cleanup:
                cleanup_paths.append(cleanup)
        payload = {
            "email_address": customer["email"],
            "first_name": customer["first_name"],
            "last_name": customer["last_name"],
            "phone_number": digits,
            "billing_address": billing,
            "utility_accounts": [{
                "utility_account_number": acct["account_number"],
                "secondary_account_identifier": acct["secondary_account_identifier"],
                # Required by some utilities (Dominion needs a 10-digit meter
                # number), not required by others. The multipart builder has
                # always emitted utility_accounts[n][meter_numbers][m]; nothing
                # was populating it because no NY utility in this funnel asks
                # for one. Omitted entirely when absent, so no NY request body
                # changes: a None here produces no multipart part at all.
                "meter_numbers": ([acct["meter_number"]]
                                  if acct["meter_number"] else []),
                "service_address": {
                    "address_1": svc["street"], "address_2": svc["unit"],
                    "city": svc["city"], "state": svc["state"], "zip": svc["zip"],
                },
                "utility_bills": pdf_paths,
            }],
        }
        # The program choice is authoritative from the DATABASE. It used to be
        # read only from the request body - and the body value was never copied
        # into `payload` - so resolve_customer_type() always saw None and every
        # dual-program location (e.g. 10901) failed with "select which one".
        # A body value is still accepted for a first-time selection, but the
        # persisted value wins on retries/resume, and the adapter re-validates
        # whatever arrives against this enrollment's capacity response.
        chosen = (data.get("customer_type")
                  or enrollment["selected_customer_type"] or None)
        if chosen:
            payload["customer_type"] = chosen
        result = adapter.create_enrollment(enrollment_id, payload, user_id=g.current_user["id"])
    except PerchAmbiguousOutcomeError as e:
        # Perch returned success but the response could not be read, so the
        # enrollment MAY exist there. Do not retry; reconcile via GET /status.
        workflow.set_state(
            enrollment_id, "enroll_outcome_uncertain",
            last_response={"uncertain": True, "detail": str(e)},
        )
        audit.log("perch_enroll_uncertain", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"detail": str(e)[:500]},
                  ip_address=request.remote_addr)
        status_payload = None
        try:
            status_payload = adapter.get_status(enrollment_id, user_id=g.current_user["id"])
        except PerchError:
            pass
        return jsonify({
            "error": "Enrollment outcome is uncertain. Check status before retrying.",
            "detail": str(e),
            "perch_error": type(e).__name__,
            "outcome": "uncertain",
            "retry_safe": False,
            "perch_status": _safe_status(status_payload),
        }), 502
    except PerchError as e:
        # NARROW: cleanup is attempted ONLY for the confirmed duplicate-email
        # rejection - the failure this change set exists to fix, and the one the
        # rep cannot correct by editing the form.
        #
        # Every other definite error (a bad ZIP, a rejected utility account, any
        # other 422) leaves the enrollment ALONE, because the rep may well fix
        # the input and resubmit against the same row. Discarding those would
        # destroy recoverable work.
        #
        # PerchAmbiguousOutcomeError is handled ABOVE and never reaches here, so
        # an enrollment that MAY exist at Perch can never be discarded. When the
        # duplicate IS confirmed, enrollment_cleanup still has the final say and
        # refuses anything past the commit boundary or with real local work.
        if perch_client.is_duplicate_email_error(e):
            try:
                enrollment_cleanup.discard_provisional_enrollment(
                    enrollment_id, "enroll_rejected")
            except Exception:
                pass
        return _perch_error_response(enrollment_id, "enroll", e)
    finally:
        import os
        for cleanup_path in cleanup_paths:
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    step_key, recognized = _next_key_of(result)
    workflow.set_state(
        enrollment_id, step_key or "unknown_next_step",
        next_step_url=_next_step_raw(result), recognized=recognized,
        last_response={"customer_type": result.get("customer_type")},
    )
    return jsonify({"result": result, "next_step_key": step_key, "next_step_recognized": recognized})


@bp.route("/enrollments/<int:enrollment_id>/lmi/proof_docs", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def submit_perch_proof_docs(enrollment_id):
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    document_id = data.get("document_id")
    doc = _stored_document(enrollment_id, "lmi_document", document_id=document_id)
    acct = query_one("SELECT * FROM utility_accounts WHERE id=?", (enrollment["utility_account_id"],)) if enrollment["utility_account_id"] else None
    if not doc or not acct:
        return jsonify({"error": "A saved LMI proof document and utility account are required."}), 400

    source_type = (data.get("source_type") or "").strip()
    name_on_document = (data.get("name_on_document") or "").strip()
    relationship = (data.get("relationship") or "").strip()
    document_type = (data.get("document_type") or "").strip()
    valid_relationships = {"self", "other", "unknown"}
    valid_doc_types = {"card", "letter", "account_statement", "utility_bill", "other", "unknown"}
    proof_type = query_one("SELECT * FROM perch_proof_doc_types WHERE source_type=? AND category='proof_doc' AND is_active=1", (source_type,))
    if not proof_type:
        return jsonify({"error": "Select a supported proof-document type."}), 400
    if not name_on_document:
        return jsonify({"error": "Name on document is required."}), 400
    if relationship not in valid_relationships:
        return jsonify({"error": "Relationship must be self, other, or unknown."}), 400
    if document_type not in valid_doc_types:
        return jsonify({"error": "Select the physical document format."}), 400

    # EVERY STORED PROOF FILE IS SENT, not just the one the caller named.
    #
    # Perch's /lmi/proof_docs contract takes a LIST of documents, each with
    # exactly one file part (documents[i][file]) and its own metadata, and
    # build_proof_docs_multipart() has always looped over that list. This route
    # built a list of ONE, so a rep who attached a two-page award letter had the
    # second page stored by UBS and never seen by Perch. The bill path already
    # expands its set for exactly this reason.
    #
    # The METADATA IS THE REP'S SINGLE ANSWER, repeated per file: source_type,
    # name on document, relationship and physical format describe the proof the
    # rep is attesting to, and the files are the pages of it. Nothing is
    # invented per page - there is no second answer to copy from.
    #
    # A set-less legacy proof returns a one-element list here, which is byte
    # for byte the request this route sent before.
    proof_files = _set_documents_for(enrollment_id, doc, "lmi_document")
    documents = [{
        "source_type": source_type,
        "name_on_document": name_on_document,
        "relationship": relationship,
        "document_type": document_type,
        "file_path": resolve_stored_path(p["stored_path"]),
    } for p in proof_files]
    try:
        result = adapter.submit_proof_docs(
            enrollment_id, acct["account_number"], documents,
            user_id=g.current_user["id"],
        )
    except (PerchError, ValueError) as e:
        # ALREADY COMPLETED: Perch has advanced this enrollment past
        # /lmi/proof_docs, so the documents are on file and the local workflow
        # is simply stale. Perch's own instruction is to ask /status - so we
        # reconcile instead of reposting. The POST is NOT retried.
        #
        # Strictly limited to that one documented condition: every other 422
        # stays an ordinary correctable validation failure, and
        # PerchAmbiguousOutcomeError cannot reach this branch as a validation
        # error at all.
        if isinstance(e, PerchError) and perch_client.is_step_already_completed_error(e):
            return _reconcile_completed_step(enrollment_id, "proof_docs", e)
        if isinstance(e, PerchError):
            return _perch_error_response(enrollment_id, "proof_docs", e)
        return jsonify({"error": str(e)}), 400

    step_key, recognized = _next_key_of(result)
    workflow.set_state(
        enrollment_id, step_key or "unknown_next_step",
        next_step_url=_next_step_raw(result), recognized=recognized,
        last_response={"proof_document_id": doc["id"], "source_type": source_type,
                       # WHICH files went, so a later reader is not left
                       # inferring it from the one id the caller happened to
                       # name.
                       "proof_document_ids": [p["id"] for p in proof_files],
                       "proof_file_count": len(proof_files)},
    )
    return jsonify({"result": result, "next_step_key": step_key, "next_step_recognized": recognized})


@bp.route("/enrollments/<int:enrollment_id>/contracts", methods=["POST"])
@require_staff_or_customer
def generate_perch_contracts(enrollment_id):
    enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    # Server-side half of the Virginia hard stop. A not-qualified enrollment
    # must never reach contract generation, however the request arrives.
    blocked = _blocked_guard(enrollment_id)
    if blocked:
        return blocked
    try:
        result = adapter.generate_contracts(enrollment_id, user_id=_actor_id())
    except PerchError as e:
        return _perch_error_response(enrollment_id, "contracts", e)

    safe = adapter.contracts_safe(result) if hasattr(adapter, "contracts_safe") else result.get("contracts", [])
    step_key, recognized = _next_key_of(result)
    # Persist only the URL-free contract metadata. Presigned URLs remain in the
    # local `result` object for this request and die with it.
    workflow.set_state(
        enrollment_id, "contracts_review",
        next_step_url=_next_step_raw(result), recognized=recognized,
        last_response={"contracts": safe, "contract_count": len(safe)},
    )
    return jsonify({
        "contracts": safe,
        "contract_count": len(safe),
        "next_step_url": _next_step_raw(result),
        "next_step_key": step_key,
        "acceptance_enabled": True,
    })


@bp.route("/enrollments/<int:enrollment_id>/contracts/review", methods=["POST"])
@require_staff_or_customer
def review_perch_contract(enrollment_id):
    enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "A contract index is required."}), 400

    state = workflow.get_state(enrollment_id) or {}
    # Via the shared helper: a last_response payload that is not a JSON object
    # (the v2 capacity array, for one) yields {} rather than raising.
    saved = workflow.last_response_dict(state)
    safe_contracts = saved.get("contracts") or []
    if index < 0 or index >= len(safe_contracts):
        return jsonify({"error": "That agreement is not in the current review packet."}), 404

    now = time.time()
    for expired_token, ctx in list(_contract_review_tokens.items()):
        if ctx.get("expires_at", 0) < now:
            _contract_review_tokens.pop(expired_token, None)
    token = secrets.token_urlsafe(24)
    _contract_review_tokens[token] = {
        "enrollment_id": enrollment_id,
        "contract_index": index,
        "contract_name": safe_contracts[index].get("contract_name"),
        "user_id": _actor_id(),
        "expires_at": now + _CONTRACT_REVIEW_TTL_SECONDS,
    }
    response = jsonify({"review_url": f"/api/perch/contract-reviews/{token}"})
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


@bp.route("/contract-reviews/<token>", methods=["GET"])
def open_contract_review(token):
    """Consume a one-time Dalton review link and redirect to Perch.

    The presigned URL exists only inside this request stack and the outgoing
    Location header. It never enters Dalton JSON responses, workflow state,
    audit details, local/session storage, or the database.
    """
    ctx = _contract_review_tokens.get(token)
    if not ctx or ctx["expires_at"] < time.time():
        _contract_review_tokens.pop(token, None)
        return jsonify({"error": "This contract review link expired. Click Review again."}), 410
    try:
        result = adapter.generate_contracts(ctx["enrollment_id"], user_id=ctx["user_id"])
    except PerchError as e:
        # No g.current_user exists on the capability URL, so handle the Perch
        # failure locally rather than using the authenticated-route helper.
        return jsonify({"error": str(e), "perch_error": type(e).__name__}), getattr(e, "http_status", 502)
    raw_items = (result.get("raw") or {}).get("contract_urls")
    if not isinstance(raw_items, list):
        return jsonify({"error": "That contract is no longer available. Refresh the contract packet."}), 404
    index = ctx["contract_index"]
    requested_name = ctx.get("contract_name")
    item = None
    if requested_name:
        item = next((r for r in raw_items if isinstance(r, dict) and r.get("contract_name") == requested_name), None)
    if item is None and 0 <= index < len(raw_items) and isinstance(raw_items[index], dict):
        item = raw_items[index]
    if item is None:
        return jsonify({"error": "That agreement could not be opened right now. "
                                 "Please try again."}), 502
    url = item.get("url") if isinstance(item, dict) else None
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return jsonify({"error": "That agreement could not be opened right now. "
                                 "Please try again."}), 502
    # PRE-FLIGHT: confirm the object actually exists before handing the browser
    # to S3.
    #
    # A National Grid enrollment produced a valid presigned URL for
    # .../perch-credit-check-consent-1-0.pdf whose OBJECT was missing, so the rep
    # was redirected into raw S3 XML ("NoSuchKey. The specified key does not
    # exist."). The URL is Perch's, unmodified - the object is simply absent on
    # their side - but a raw XML error page is a poor way to learn that.
    #
    # A ranged GET of one byte is used rather than HEAD: presigned signatures
    # cover the HTTP method, so HEAD against a GET-signed URL fails signature
    # validation and would produce a false negative. Nothing is downloaded,
    # stored or cached - this only asks whether the key resolves.
    #
    # The check NEVER blocks on its own failure: any error reaching S3 falls
    # through to the redirect, so Dalton cannot make a working link unusable.
    missing_detail = None
    try:
        import requests as _rq
        probe = _rq.get(url, headers={"Range": "bytes=0-0"}, timeout=6, stream=True)
        try:
            if probe.status_code in (403, 404):
                body = (probe.text or "")[:400]
                if "NoSuchKey" in body or probe.status_code == 404:
                    missing_detail = "NoSuchKey"
        finally:
            probe.close()
    except Exception:
        missing_detail = None      # unreachable probe -> proceed as before

    if missing_detail:
        audit.log(
            "perch_contract_document_missing", enrollment_id=ctx["enrollment_id"],
            user_id=ctx["user_id"],
            details={"contract_index": index, "contract_name": item.get("contract_name"),
                     "reason": missing_detail},
            ip_address=request.remote_addr,
        )
        _contract_review_tokens.pop(token, None)
        # This endpoint is loaded INSIDE the review iframe, so the reply must be
        # readable as a page. Returning JSON here would be nearly as opaque to
        # the rep as the raw S3 XML this replaces.
        name = escape(item.get("contract_name") or "This document")
        page = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Document unavailable</title>"
            "<style>body{margin:0;padding:28px;font:15px/1.5 Inter,system-ui,sans-serif;"
            "color:#14223a;background:#fff}h1{font-size:17px;margin:0 0 10px}"
            "p{margin:0 0 10px;color:#5f6f85}code{font:12.5px ui-monospace,monospace;"
            "background:#f1f4f8;padding:2px 5px;border-radius:4px}</style>"
            f"<h1>{name} isn't available right now</h1>"
            "<p>Perch issued a valid link, but the document is missing on their "
            "storage. This is on Perch's side &mdash; nothing is wrong with this "
            "enrollment.</p>"
            "<p>The other agreements open normally and the enrollment can still be "
            "completed. Please report this document name to Perch:</p>"
            f"<p><code>{name}</code></p>"
        )
        resp = make_response(page, 502)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store, private"
        return resp

    audit.log(
        "perch_contract_review_opened", enrollment_id=ctx["enrollment_id"],
        user_id=ctx["user_id"], details={"contract_index": index, "contract_name": item.get("contract_name")},
        ip_address=request.remote_addr,
    )
    _contract_review_tokens.pop(token, None)
    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


# ─────────────── Contract acceptance (POST /contracts/accept) ───────────────

_MAX_USER_AGENT = 2048


def _prior_contract_names(enrollment_id):
    """URL-free contract names previously stored at the contracts_review step."""
    state = workflow.get_state(enrollment_id) or {}
    saved = workflow.last_response_dict(state)
    items = saved.get("contracts")
    if not isinstance(items, list):
        return []
    return [{"contract_name": (c or {}).get("contract_name")} for c in items
            if isinstance(c, dict)]


def _client_ip():
    """The accepting client's IP.

    request.remote_addr is the TCP peer. Behind a reverse proxy or load
    balancer that is the PROXY's address, so every customer would appear to
    share one IP in Perch's acceptance record.

    X-Forwarded-For is trusted ONLY when DALTON_TRUSTED_PROXY_COUNT is set to a
    positive integer, and then only the Nth-from-last entry - the hop actually
    written by our own proxy. Entries further left are client-supplied and
    forgeable, so they are never used. Unset (the default, and our current local
    setup) means the header is ignored entirely.
    """
    try:
        hops = int(os.environ.get("DALTON_TRUSTED_PROXY_COUNT", "0"))
    except (TypeError, ValueError):
        hops = 0
    if hops > 0:
        xff = request.headers.get("X-Forwarded-For") or ""
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.remote_addr


def _acceptance_metadata():
    """Build Perch's required metadata SERVER-SIDE.

    Never taken from browser JSON: a client-supplied IP or timestamp would be
    unverifiable, and Perch requires the values captured at the moment the
    customer agreed. Uses the app's existing request/IP behavior - no new
    X-Forwarded-For trust is introduced here.

    timestamp: delegated to client.acceptance_timestamp(), which applies a small
    clock-skew allowance. Perch rejected a timestamp generated at the exact
    instant of submission with 422 "Metadata timestamp cannot be in the future"
    (observed live 2026-08-14). Sharing that helper with the staging verifier
    keeps the two provably identical.
    OPEN QUESTION for Perch: is timezone-aware ISO-8601 accepted/preferred?
    """
    user_agent = (request.headers.get("User-Agent") or "")[:_MAX_USER_AGENT]
    return {
        "ip_address": _client_ip(),
        "timestamp": acceptance_timestamp(),
        "user_agent": user_agent,
    }


@bp.route("/enrollments/<int:enrollment_id>/contracts/accept", methods=["POST"])
@require_staff_or_customer
def accept_perch_contracts(enrollment_id):
    """Record the customer's acceptance of the whole Perch contract packet.

    Preconditions checked BEFORE any Perch call, so a bad request never reaches
    Perch: authenticated, authorized, enrollment visible, workflow in the
    contract-review state, and explicit customer confirmation present.
    """
    enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    # No agreement completion for a stopped enrollment. The pre-existing
    # blocked states were already refused further down via step_key; this makes
    # the refusal uniform and covers lmi_not_qualified.
    blocked = _blocked_guard(enrollment_id)
    if blocked:
        return blocked

    data = request.get_json(force=True, silent=True) or {}

    # Dalton-side safety precondition only. NEVER forwarded to Perch - the spec
    # requires no acknowledgment field; calling the endpoint IS the acceptance.
    if data.get("customer_confirmed") is not True:
        return jsonify({
            "error": "The customer must explicitly confirm they reviewed and agree to the "
                     "contracts before acceptance can be submitted.",
            "code": "customer_confirmation_required",
        }), 400

    state = workflow.get_state(enrollment_id) or {}
    step_key = state.get("current_step_key")

    # Double-submission protection: if Dalton already definitively recorded a
    # successful acceptance, do not call Perch again. Perch has not documented
    # whether /contracts/accept is idempotent.
    if step_key == "contracts_accepted":
        saved = workflow.last_response_dict(state)
        return jsonify({
            "already_accepted": True,
            "message": saved.get("message"),
            "perch_status": saved.get("perch_status"),
            "note": "Contracts were already accepted for this enrollment. Perch was not called again.",
        })

    # An ambiguous prior attempt must NOT auto-retry. A human decides.
    if step_key == "contracts_accept_uncertain":
        return jsonify({
            "error": "A previous acceptance attempt for this enrollment could not be confirmed. "
                     "Perch may or may not have recorded it. Check enrollment status with Perch "
                     "before submitting again.",
            "code": "acceptance_outcome_uncertain",
        }), 409

    if step_key != "contracts_review":
        return jsonify({
            "error": "Contracts must be generated and under review before they can be accepted.",
            "code": "wrong_workflow_state",
            "current_step_key": step_key,
        }), 409

    metadata = _acceptance_metadata()

    try:
        result = adapter.accept_contracts(enrollment_id, metadata, user_id=_actor_id())
    except PerchAmbiguousOutcomeError as e:
        # Do NOT mark accepted, do NOT mark failed, do NOT retry.
        workflow.set_state(
            enrollment_id, "contracts_accept_uncertain",
            last_response={"uncertain": True, "detail": str(e),
                           "attempted_at": metadata["timestamp"]},
        )
        audit.log("perch_contracts_accept_uncertain", enrollment_id=enrollment_id,
                  user_id=_actor_id(), details={"detail": str(e)},
                  ip_address=request.remote_addr)
        status_payload = None
        try:
            # GET /status is side-effect free, so it is safe even here - and it
            # is the only way to discover what actually happened.
            status_payload = adapter.get_status(enrollment_id, user_id=_actor_id())
        except PerchError:
            pass
        return jsonify({
            "error": str(e),
            "perch_error": type(e).__name__,
            "outcome": "uncertain",
            "retry_safe": False,
            "perch_status": _safe_status(status_payload),
        }), 502
    except PerchError as e:
        return _perch_error_response(enrollment_id, "contracts_accept", e)

    # 202 accepted. Perch processes asynchronously, so 202 alone does not mean
    # every downstream step finished - confirm via GET /status.
    status_payload = None
    try:
        status_payload = adapter.get_status(enrollment_id, user_id=_actor_id())
    except PerchError:
        status_payload = None

    safe_status = _safe_status(status_payload)
    workflow.set_state(
        enrollment_id, "contracts_accepted",
        next_step_url=None, recognized=True,
        # Carry the URL-free contract names forward so the completed read-only
        # view can list them WITHOUT calling Perch. Names only - never URLs.
        last_response={"message": result.get("message"), "perch_status": safe_status,
                       "contracts": _prior_contract_names(enrollment_id)},
    )
    audit.log("perch_contracts_accepted", enrollment_id=enrollment_id,
              user_id=_actor_id(),
              details=_actor_details({"message": result.get("message"),
                       "perch_completed": (safe_status or {}).get("completed")}),
              ip_address=request.remote_addr)

    return jsonify({
        "accepted": True,
        "message": result.get("message"),
        "perch_status": safe_status,
        "token_was_refreshed": result.get("token_was_refreshed", False),
    })


def _reconcile_completed_step(enrollment_id, operation, exc):
    """Perch says this step is already done: adopt its authoritative next step.

    GET /status is side-effect free, so it is safe on a failure path. Nothing
    is retried and nothing is guessed: if /status cannot tell us where the
    enrollment actually is, the rep gets a controlled message and the
    enrollment is left exactly as it was for review.
    """
    audit.log(
        f"perch_{operation}_already_completed", enrollment_id=enrollment_id,
        user_id=_actor_id(), details=_actor_details({"detail": str(exc)[:500]}),
        ip_address=request.remote_addr,
    )
    try:
        status_payload = adapter.get_status(enrollment_id, user_id=_actor_id())
    except PerchError:
        status_payload = None

    safe_status = _safe_status(status_payload)
    step_key = (safe_status or {}).get("next_step_key")
    if not step_key:
        # Reconciliation failed, or Perch returned a step we do not recognise.
        # Never guess - the enrollment is preserved and the workflow row is
        # left untouched so the current state is still auditable.
        return jsonify({
            "error": "This step was already completed at Perch, and we could not "
                     "confirm the current status. The enrollment is unchanged - "
                     "reopen it in a moment to continue.",
            "reconciled": False,
        }), 502

    workflow.set_state(
        enrollment_id, step_key,
        next_step_url=(status_payload or {}).get("next_step"), recognized=True,
        last_response={"reconciled_from": operation, "perch_status": safe_status},
    )
    audit.log(
        f"perch_{operation}_reconciled", enrollment_id=enrollment_id,
        user_id=_actor_id(), details=_actor_details({"next_step_key": step_key}),
        ip_address=request.remote_addr,
    )
    # Success shape. The rep is routed to the real step and never sees Perch's
    # raw endpoint message, which reconciliation has just made irrelevant.
    return jsonify({
        "reconciled": True,
        "next_step_key": step_key,
        "next_step_recognized": True,
        "perch_status": safe_status,
    })


def _safe_status(status_payload):
    """URL-free projection of GET /status for JSON, workflow and audit."""
    if not status_payload:
        return None
    return {
        "completed_steps": status_payload.get("completed_steps"),
        "remaining_steps": status_payload.get("remaining_steps"),
        "completed": status_payload.get("completed"),
        "next_step_key": _next_key(status_payload.get("next_step"))[0],
    }


@bp.route("/enrollments/<int:enrollment_id>/perch-status", methods=["GET"])
@require_auth
def perch_enrollment_status(enrollment_id):
    """Read-through to Perch GET /status. Side-effect free and safe to poll."""
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    try:
        status_payload = adapter.get_status(enrollment_id, user_id=g.current_user["id"])
    except PerchError as e:
        return _perch_error_response(enrollment_id, "status", e)
    return jsonify(_safe_status(status_payload) or {})


@bp.route("/enrollments/<int:enrollment_id>/programs", methods=["GET"])
@require_auth
def enrollment_available_programs(enrollment_id):
    """Program choices Perch returned for THIS enrollment's capacity result.

    Read-only and side-effect free - it reads the persisted capacity row and
    makes no Perch call. This is the endpoint Phase B's selection UI will use:
    it must only ever offer programs Perch actually returned.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    capacity = adapter.latest_capacity_check(enrollment_id)
    if not capacity:
        return jsonify({
            "enrollment_id": enrollment_id,
            "capacity_checked": False,
            "available_programs": [],
            "selection_required": False,
            "message": "Run a capacity check before choosing a program.",
        })

    # E2: built from the stored ENTRY MODEL, so a v2 capacity response drives
    # the program list. Falls back to the flat columns for a capacity row
    # written before migration 013.
    model = adapter.capacity_model_for(capacity)
    county_required, county_options, county_source = _county_requirement(
        model, capacity)
    selected_county = _stored_county(enrollment)

    # E2.1: WHEN COUNTY DECIDES, NOTHING IS OFFERED UNTIL IT IS ANSWERED.
    #
    # Returning the unrestricted subset first and letting it GROW once a county
    # arrives would show a rep one list and then a different one - the "UI
    # shows one option while the backend carries another" failure, just spread
    # over two seconds. So while the answer is outstanding the list is empty
    # and `awaiting_county` says why.
    awaiting_county = bool(county_required and not selected_county)
    if awaiting_county:
        programs = []
    else:
        programs = adapter.available_programs(model, county=selected_county)

    # The stored program, kept only while it is still on offer - a selection
    # the capacity no longer supports is not a selection.
    selected_type = (
        enrollment["selected_customer_type"]
        if enrollment["selected_customer_type"] in [p["customer_type"] for p in programs]
        else None)
    selected_program = next((p for p in programs
                             if p["customer_type"] == selected_type), None)
    selected_rate = _stored_savings_rate(enrollment)
    # Same rule for the rate: reported only while the selected program still
    # offers it, so the UI can never show a rate the backend would refuse.
    if selected_rate is not None and selected_program is not None:
        if selected_rate not in selected_program["savings_percents"]:
            selected_rate = None
    elif selected_program is None:
        selected_rate = None

    return jsonify({
        "enrollment_id": enrollment_id,
        "capacity_checked": True,
        "capacity_available": bool(capacity.get("capacity_available")),
        "utility_slug": capacity.get("utility_slug"),
        "zip_code": capacity.get("zip_code"),
        "available_programs": programs,
        # Which wire shape this capacity came from. Diagnostic only - no
        # client branches on it - but it is the one thing that tells a reader
        # of a bug report whether they are looking at v1 or v2 behaviour.
        "capacity_shape": model.shape,
        # Whether ANY available entry is restricted to counties at this
        # location. Informational: restrictions can exist without MATTERING.
        "county_restrictions_present": model.has_county_restrictions(),
        # E2.1 - whether the county actually CHANGES what can be offered here.
        # This, not the line above, is what reveals the field.
        "county_required": county_required,
        "county_options": county_options,
        "county_source": county_source,
        "selected_county": selected_county,
        "awaiting_county": awaiting_county,
        # True only when the rep genuinely has a choice to make. One option is
        # unambiguous and is selected automatically at enroll time.
        "selection_required": len(programs) > 1,
        # The persisted choice, so the wizard hydrates from the backend instead
        # of relying on transient JS state. Only returned when it is still one
        # of the programs currently on offer.
        "selected_customer_type": selected_type,
        # E3 - THE SAVINGS RATE.
        #
        # Perch v2 can offer several rates for one program and then REQUIRES
        # one at /enroll. These three fields are what the program area needs
        # to render that choice, and nothing more: which rate is stored, and
        # whether one is still outstanding.
        #
        # savings_selection_required is about the SELECTED program only. A
        # location where some other program offers several rates does not owe
        # the rep a decision they are not making.
        "selected_savings_percent": selected_rate,
        "savings_selection_required": bool(
            selected_program and len(selected_program["savings_percents"]) > 1
            and selected_rate is None),
        "savings_options": ((selected_program or {}).get("savings_percents")
                            if selected_program else []),
    })


def _workflow_descriptor(enrollment_id):
    """workflow.resolve(), with the County field's options filled in.

    THE ONE PLACE the two layers meet. services/perch/workflow.py decides
    WHETHER the Availability step needs a County field - it owns the step
    descriptors - but it must not import the markets layer, so it cannot fetch
    the county list itself. This supplies it.

    For every location with no county-dependent capacity this is
    workflow.resolve() and nothing else: the step has no County field, the
    fill is a no-op, and not a single market lookup happens.
    """
    descriptor = workflow.resolve(enrollment_id)
    step = (descriptor or {}).get("step") or {}
    if not any(f.get("name") == workflow.COUNTY_FIELD_NAME
               for f in (step.get("fields") or [])):
        return descriptor

    capacity = adapter.latest_capacity_check(enrollment_id)
    counties, source = _state_counties(capacity)
    return workflow.fill_county_options(descriptor, counties, source)


def _stored_savings_rate(enrollment):
    """The savings rate already chosen for this enrollment, or None."""
    try:
        value = enrollment["selected_savings_percent"]
    except (KeyError, IndexError, TypeError):
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clear_rate_if_unavailable(enrollment_id, model, customer_type, county):
    """Drop a stored savings rate the capacity no longer offers. Returns it.

    Called whenever the PROGRAM or the COUNTY changes, because both decide
    which rates apply. Leaving a stale rate behind would not be harmless: it
    is re-validated at enroll, so the rep would be stopped at the end of the
    wizard over a choice they made and then invalidated three screens ago.

    Clearing rather than re-pointing is deliberate - the same rule the program
    selection follows. UBS does not pick a replacement rate on the rep's
    behalf.
    """
    row = query_one("SELECT selected_savings_percent FROM enrollments "
                    "WHERE id = ?", (enrollment_id,))
    current = row["selected_savings_percent"] if row else None
    if current is None:
        return None
    offered = []
    if customer_type:
        program = adapter.program_for(model, customer_type, county=county)
        offered = (program or {}).get("savings_percents") or []
    if current in offered:
        return None
    execute("UPDATE enrollments SET selected_savings_percent = NULL, "
            "updated_at = datetime('now') WHERE id = ?", (enrollment_id,))
    return current


def _stored_county(enrollment):
    """The county the rep already chose for this enrollment, or None."""
    try:
        value = enrollment["service_county"]
    except (KeyError, IndexError, TypeError):
        return None
    return (value or "").strip() or None


def _capacity_state(capacity):
    """The two-letter state a capacity check belongs to, or None.

    Resolved from the ZIP, because the Availability step runs BEFORE any
    service address exists - there is nothing else to ask. This is the same
    resolution the step already uses to populate its utility dropdown, so a
    ZIP the utility list could be built from is a ZIP the county list can be
    built from too.
    """
    return zip_state.state_for_zip((capacity or {}).get("zip_code"))


def _state_counties(capacity):
    """(counties, source) for the state a capacity check belongs to.

    THE ONE E2.1 ENTRY POINT to the county list: this feature reaches the
    shared non-NY lookup here and nowhere else, so it adds one call site
    rather than three. A failure yields an
    empty list rather than an exception: every caller here has something
    honest to do with "the question applies but there are no options", and
    none of them should turn a market outage into a 500 in the middle of an
    enrollment.
    """
    state = _capacity_state(capacity)
    if not state:
        return [], None
    counties, source, err = _market_counties(state)
    if err:
        return [], None
    return list(counties or []), source


def _county_requirement(model, capacity):
    """(county_required, county_options, county_source) for this capacity.

    county_required is True only when the county genuinely CHANGES what can be
    offered - see adapter.county_decides_options(). A location whose
    restricted entries add nothing beyond the unrestricted ones never shows
    the field, which is what keeps the Availability screen exactly as it was
    for every market that has no county restrictions at all.

    The options are Perch's OWN controlled list for the state
    (GET /markets/counties, via the markets service, which falls back to the
    local NY reference table when Perch is unreachable). The full state list,
    not only the restricted counties: a customer in an unrestricted county
    still has to be able to say where they are, and would otherwise be unable
    to pick anything.

    A lookup failure returns required=True with NO options rather than
    pretending the question does not apply. Offering programs whose
    availability depends on an answer that cannot be collected would be the
    guess this whole mechanism exists to avoid.
    """
    if not adapter.county_decides_options(model):
        return False, [], None

    counties, source = _state_counties(capacity)
    return True, counties, source


@bp.route("/enrollments/<int:enrollment_id>/savings-rate", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def select_enrollment_savings_rate(enrollment_id):
    """Persist the savings rate the rep chose (E3).

    Perch v2 can offer SEVERAL rates for one customer type, and POST /enroll
    then requires one: "Optional when only one rate applies, required when
    more than one does." This is where that choice is recorded.

    VALIDATED against the rates Perch published for THIS enrollment's
    customer_type and county, so a tampered client cannot store a rate that
    was never offered - and the same validation runs again at enroll time,
    because capacity moves.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    # Same commit boundary as the program and the county. After /enroll the
    # rate is part of what Perch accepted, and the record must keep explaining
    # the enrollment that actually happened.
    if workflow.perch_committed(enrollment_id):
        audit.log("savings_rate_change_rejected_committed",
                  enrollment_id=enrollment_id, user_id=g.current_user["id"],
                  ip_address=request.remote_addr)
        return jsonify({
            "error": "This enrollment has already been submitted and the "
                     "savings rate can no longer be changed.",
            "committed": True,
        }), 409

    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("savings_percent")

    if raw in (None, "", "null"):
        execute("UPDATE enrollments SET selected_savings_percent = NULL, "
                "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
                (g.current_user["id"], enrollment_id))
        return jsonify({"enrollment_id": enrollment_id,
                        "selected_savings_percent": None})

    capacity = adapter.latest_capacity_check(enrollment_id)
    if not capacity:
        return jsonify({"error": "Run a capacity check before choosing a "
                                 "savings rate."}), 400

    selected_type = enrollment["selected_customer_type"]
    if not selected_type:
        return jsonify({"error": "Choose a savings program before choosing a "
                                 "rate."}), 400

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Choose one of the savings rates shown."}), 400

    model = adapter.capacity_model_for(capacity)
    program = adapter.program_for(model, selected_type,
                                  county=_stored_county(enrollment))
    offered = (program or {}).get("savings_percents") or []
    if value not in offered:
        return jsonify({"error": "That savings rate is not available for this "
                                 "customer. Choose one of the rates "
                                 "shown."}), 400

    execute("UPDATE enrollments SET selected_savings_percent = ?, "
            "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
            (value, g.current_user["id"], enrollment_id))
    audit.log("enrollment_savings_rate_selected", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"savings_percent": value,
                       "customer_type": selected_type},
              ip_address=request.remote_addr)
    return jsonify({"enrollment_id": enrollment_id,
                    "selected_savings_percent": value})


@bp.route("/enrollments/<int:enrollment_id>/county", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def select_enrollment_county(enrollment_id):
    """Persist the customer's county for capacity filtering (E2.1).

    The value is VALIDATED against Perch's controlled list for the state and
    stored in Perch's spelling - a rep cannot free-type a county, and a
    tampered client cannot store one Perch has never heard of. Matching is
    case-insensitive; the stored value is not.

    NOT SENT ANYWHERE YET. This filters capacity inside UBS. POST /enroll
    carries no county until E3.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    # Same commit boundary as the program choice, for the same reason: after
    # /enroll the customer_type is fixed, and the county is what decided which
    # ones were selectable. Letting it move afterwards would leave the stored
    # program unexplainable by the stored county.
    if workflow.perch_committed(enrollment_id):
        audit.log("county_change_rejected_committed", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], ip_address=request.remote_addr)
        return jsonify({
            "error": "This enrollment has already been submitted and the "
                     "county can no longer be changed.",
            "committed": True,
        }), 409

    data = request.get_json(force=True, silent=True) or {}
    requested = (data.get("county") or "").strip()

    capacity = adapter.latest_capacity_check(enrollment_id)
    if not capacity:
        return jsonify({"error": "Run a capacity check before choosing a county."}), 400

    if not requested:
        execute("UPDATE enrollments SET service_county = NULL, "
                "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
                (g.current_user["id"], enrollment_id))
        return jsonify({"enrollment_id": enrollment_id, "selected_county": None})

    counties, _source = _state_counties(capacity)
    if not counties:
        return jsonify({"error": "The county list for this market is "
                                 "unavailable right now. Re-check "
                                 "availability and try again.",
                        "market_data_unavailable": True}), 503

    # Case-insensitive match, PERCH'S SPELLING stored. Virginia's independent
    # cities carry a lowercase "city" suffix that Perch validates exactly.
    canonical = next((c for c in counties
                      if c.strip().lower() == requested.lower()), None)
    if not canonical:
        return jsonify({"error": "Select the customer's county from the list."}), 400

    execute("UPDATE enrollments SET service_county = ?, "
            "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
            (canonical, g.current_user["id"], enrollment_id))

    # CHANGING THE COUNTY CAN INVALIDATE THE PROGRAM. A rep who picked a
    # program under one county and then corrected the county must not be left
    # holding a selection that county cannot support - that is precisely the
    # stale state the selection rules forbid. The stored choice is cleared
    # rather than silently re-pointed, so the rep chooses again from what is
    # actually available.
    cleared = None
    previous = enrollment["selected_customer_type"]
    model = adapter.capacity_model_for(capacity)
    if previous:
        still_offered = [p["customer_type"] for p in
                         adapter.available_programs(model, county=canonical)]
        if previous not in still_offered:
            execute("UPDATE enrollments SET selected_customer_type = NULL, "
                    "selected_savings_percent = NULL, "
                    "updated_at = datetime('now') WHERE id = ?", (enrollment_id,))
            cleared = previous

    # E3: THE RATE DEPENDS ON THE COUNTY TOO. A county-restricted entry can
    # publish rates the unrestricted one does not, so a rate chosen under one
    # county may simply not exist under another. Cleared for the same reason
    # the program is - a stored value the capacity no longer offers would be
    # re-validated and refused at enroll, after the rep had moved on.
    cleared_rate = _clear_rate_if_unavailable(enrollment_id, model,
                                              (previous if not cleared else None),
                                              canonical)

    audit.log("enrollment_county_selected", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"county": canonical, "cleared_program": cleared,
                       "cleared_savings_percent": cleared_rate},
              ip_address=request.remote_addr)
    return jsonify({"enrollment_id": enrollment_id,
                    "selected_county": canonical,
                    "cleared_program": cleared,
                    "cleared_savings_percent": cleared_rate})


@bp.route("/workflow/new", methods=["GET"])
@require_auth
@require_role("sales_rep", "admin")
def new_enrollment_workflow():
    """The first wizard step, rendered WITHOUT creating anything.

    Opening "New enrollment" used to POST /drafts immediately, so a rep who
    opened the screen and clicked Back left a blank enrollment in the database
    and on every dashboard. Nothing is persisted here - no enrollment, no
    workflow row, no audit entry. The row is created by
    POST /enrollments/capacity below, once real data has been submitted.
    """
    step = workflow._step_service_area(None, None)
    return jsonify({"enrollment_id": None, "step": step})


@bp.route("/enrollments/capacity", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def create_enrollment_and_check_capacity():
    """FIRST real action: create the enrollment, then run the capacity check.

    This is the earliest point at which the rep has supplied data worth keeping
    (email + ZIP + utility), so it is the safest place to persist. Creating the
    row here rather than at screen-open is what stops blank drafts.

    IDEMPOTENCE: if the caller already has an enrollment_id (a retry after a
    failed capacity call), it is REUSED rather than creating a second row.
    Ownership is enforced on that path exactly as everywhere else.
    """
    data = request.get_json(force=True, silent=True) or {}
    existing_id = data.get("enrollment_id")
    # Default False: a RETRY against a caller-supplied enrollment_id must never
    # be eligible for cleanup, even when the retry fails.
    created_here = False

    if existing_id:
        # Retry against an enrollment the caller already created.
        enrollment, err = _visible(existing_id)
        if err:
            return err
        enrollment_id = enrollment["id"]
    else:
        # Validate BEFORE persisting, so a malformed submission still creates
        # nothing.
        email = (data.get("email") or "").strip()
        zip_code = (data.get("zip_code") or "").strip()
        utility_name = (data.get("utility_name") or "").strip()
        missing = [n for n, v in (("email", email), ("zip_code", zip_code),
                                  ("utility_name", utility_name)) if not v]
        if missing:
            return jsonify({
                "error": "Enter the customer's email, ZIP code and utility before "
                         "checking availability.",
                "missing": missing,
            }), 400
        created, cerr = _create_enrollment_row(data.get("project_id"))
        if cerr:
            return cerr
        enrollment_id = created["enrollment_id"]
        # Remember that WE created it. Only a row created by this very request
        # may be discarded if the work below fails; a caller-supplied
        # enrollment_id is never touched.
        created_here = True

    # Delegate to the existing capacity handler so there is ONE capacity
    # implementation, not a near-copy that can drift. The enrollment identity is
    # merged into the successful response because the browser does not know it
    # yet on a first submission.
    resp = check_capacity(enrollment_id)
    body, status = (resp if isinstance(resp, tuple) else (resp, 200))

    # FAILED ATTEMPT: discard the provisional row we just created, so a
    # duplicate-email rejection (Perch 422 -> refresh_token 404 when the email
    # already has a completed account) does not leave a permanent
    # "(no customer info yet)" row on the dashboard.
    #
    # Guarded by enrollment_cleanup, which refuses anything past the commit
    # boundary. A cleanup failure never masks the real error: the original
    # response is returned either way.
    enrollment_discarded = False
    if status != 200 and created_here:
        try:
            enrollment_discarded = bool(
                enrollment_cleanup.discard_provisional_enrollment(
                    enrollment_id, "capacity_failed"))
        except Exception:
            enrollment_discarded = False

    if status != 200:
        # Tell the browser whether the provisional row ACTUALLY went away, so it
        # resets its wizard identity only when that is true. It is false on a
        # retry against a caller-supplied enrollment_id, and false when cleanup
        # refused - in both cases the caller's enrollment is still live and the
        # browser must keep holding it.
        try:
            payload = body.get_json() or {}
            payload["enrollment_discarded"] = enrollment_discarded
            return jsonify(payload), status
        except Exception:
            return resp

    if status == 200:
        try:
            payload = body.get_json() or {}
            row = query_one("SELECT enrollment_code FROM enrollments WHERE id = ?",
                            (enrollment_id,))
            payload["enrollment_id"] = enrollment_id
            payload["enrollment_code"] = row["enrollment_code"] if row else None
            return jsonify(payload), 200
        except Exception:
            return resp
    return resp


@bp.route("/enrollments/<int:enrollment_id>/program", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def select_enrollment_program(enrollment_id):
    """Persist the rep's explicit program choice.

    The selection must survive leaving the screen, navigating back, and
    reloading, so it lives in the database rather than a JS variable.

    The requested type is VALIDATED against this enrollment's own capacity
    response, so a tampered client cannot persist a program Perch did not offer
    here. Clearing (customer_type: null) is allowed and returns to "not chosen".
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    # COMMIT BOUNDARY. Perch confirmed customer_type cannot change once an
    # enrollment is in progress, so this is refused server-side - frontend
    # locking alone would not be enough. The message is rep-facing and says
    # nothing about API internals.
    if workflow.perch_committed(enrollment_id):
        audit.log("program_change_rejected_committed", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], ip_address=request.remote_addr)
        return jsonify({
            "error": "This enrollment has already been submitted and the savings "
                     "program can no longer be changed.",
            "committed": True,
        }), 409

    data = request.get_json(force=True, silent=True) or {}
    requested = data.get("customer_type")

    if requested in (None, "", "null"):
        execute("UPDATE enrollments SET selected_customer_type = NULL, "
                "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
                (g.current_user["id"], enrollment_id))
        return jsonify({"enrollment_id": enrollment_id, "selected_customer_type": None})

    capacity = adapter.latest_capacity_check(enrollment_id)
    if not capacity:
        return jsonify({"error": "Run a capacity check before choosing a program."}), 400

    # E2.1: a county-dependent location cannot have a program chosen until the
    # county is known - the set of valid programs is not yet decided, so there
    # is nothing to validate against.
    model = adapter.capacity_model_for(capacity)
    selected_county = _stored_county(enrollment)
    county_required, _options, _source = _county_requirement(model, capacity)
    if county_required and not selected_county:
        return jsonify({
            "error": "Select the customer's county before choosing a savings "
                     "program.",
            "county_required": True,
        }), 400

    try:
        # Reuses the SAME validator the enroll path uses, on the SAME stored
        # entry model and the SAME county, so the two can never disagree about
        # what is selectable. A selection that passes here is therefore a
        # selection that will still pass at enroll - which is what stops the UI
        # and the backend from carrying different programs for one enrollment.
        canonical, _reason = adapter.resolve_customer_type(
            model, requested=requested, county=selected_county)
    except PerchValidationError as e:
        return jsonify({"error": str(e), "perch_error": "PerchValidationError"}), 400

    execute("UPDATE enrollments SET selected_customer_type = ?, "
            "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
            (canonical, g.current_user["id"], enrollment_id))

    # E3: a rate belongs to a PROGRAM. Switching programs makes the stored
    # rate meaningless - 20% was the LMI rate, not the Residential one - so it
    # is cleared unless the new program happens to offer the same number.
    cleared_rate = _clear_rate_if_unavailable(enrollment_id, model, canonical,
                                              selected_county)

    audit.log("enrollment_program_selected", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"customer_type": canonical,
                       "cleared_savings_percent": cleared_rate},
              ip_address=request.remote_addr)
    return jsonify({"enrollment_id": enrollment_id,
                    "selected_customer_type": canonical,
                    "cleared_savings_percent": cleared_rate})


# ══════════════════════════════════════════════════════════════════════════
# LMI SELF-ATTESTATION
#
# Conditional branch. Dalton NEVER decides it applies - it is entered only when
# Perch returns /lmi/self_attestation as next_step after /enroll. Perch performs
# the geo/project eligibility determination on their side.
#
# Two Perch calls, in order:
#   POST /lmi/self_attestation         -> official document (presigned, 1h)
#   POST /lmi/self_attestation/accept  -> 202, next_step -> /contracts
#
# perch_self_attestation_submissions is the idempotence ledger and exists for
# nothing else. Presigned URLs are never written to it.
# ══════════════════════════════════════════════════════════════════════════

def _self_attestation_row(enrollment_id):
    return query_one("SELECT * FROM perch_self_attestation_submissions "
                     "WHERE enrollment_id = ?", (enrollment_id,))


def _blocked_guard(enrollment_id):
    """Refuse to advance an enrollment whose workflow state is blocked.

    Returns a (response, code) tuple to return, or None to continue.

    Blocked states are settled outcomes with no path onward: the two
    reconciliation states that already existed, plus lmi_not_qualified. For
    lmi_not_qualified specifically this is the server-side half of the hard
    stop - a client that skipped the UI message still cannot reach contracts.

    No NY enrollment can be in lmi_not_qualified (NY publishes status values,
    so it records its answer at Perch instead), and the two pre-existing
    blocked states are unchanged, so this guard is a no-op for every
    enrollment that reaches contracts today.
    """
    state = workflow.get_state(enrollment_id) or {}
    key = state.get("current_step_key")
    if not key or not workflow.is_blocked(key):
        return None
    if key == "lmi_not_qualified":
        return jsonify({
            "error": ("This customer does not qualify for the community solar "
                      "program currently available in their area. The enrollment "
                      "cannot continue."),
            "not_qualified": True,
            "next_step_key": key,
        }), 409
    return jsonify({
        "error": f"This enrollment needs review before it can continue "
                 f"({workflow.step_label(key)}).",
        "next_step_key": key,
    }), 409


def _enrollment_state(enrollment):
    """Two-letter state for this enrollment, from its service address.

    Returns None when no service address has been captured yet. Callers treat
    None the way they always have: as the existing single-market behaviour.
    """
    if not enrollment or not enrollment["service_address_id"]:
        return None
    row = query_one("SELECT state FROM service_addresses WHERE id = ?",
                    (enrollment["service_address_id"],))
    st = ((row["state"] if row else "") or "").strip().upper()
    return st or None


def _authoritative_county(enrollment):
    """(county, locked) - the county this enrollment is BOUND to, if any.

    E3.1. From the moment a v2 enrollment is submitted with a `county`, that
    county is the only one this enrollment has. Perch stores it and reuses it
    for the self-attestation, so every county-dependent decision UBS makes
    afterwards - which income limits apply, above all - has to be made against
    the SAME county, or UBS and Perch end up disagreeing about who is
    eligible.

    The failure this prevents is specific and quiet: enrolled under county A,
    income limits read against county B, both sides internally consistent and
    nobody notified. An earlier draft logged a warning and carried on, which
    is not a safeguard - it is a record of the mismatch after it happened.

    locked is True only when BOTH hold:
      * a county is stored on the enrollment, and
      * the enrollment was validated against a V2 capacity response.

    v1 is excluded deliberately. v1's /enroll has no county field, so a county
    stored on a v1 enrollment never reached Perch, Perch holds none, and the
    self-attestation must still collect and send one. Nothing about the v1
    path changes.

    Read from the stored capacity SHAPE rather than the environment flag, for
    the same reason every other E2/E3 decision is: a flag flipped after the
    enrollment was submitted must not retroactively change what UBS believes
    it sent.
    """
    if not enrollment:
        return None, False
    county = _stored_county(enrollment)
    if not county:
        return None, False
    try:
        enrollment_id = enrollment["id"]
    except (KeyError, IndexError, TypeError):
        return county, False
    check = adapter.latest_capacity_check(enrollment_id)
    if not check:
        return county, False
    shape = adapter.capacity_model_for(check).shape
    return county, shape == capacity.SHAPE_V2_ENTRIES


def _market_counties(state, with_state_in_error=False):
    """Perch's controlled county list for a NON-NY market.

    Returns (counties, source, error). `error` is a ready (response, status)
    pair when the lookup failed and None otherwise, so each caller returns it
    unchanged and the two endpoints keep byte-identical error payloads - the
    reference endpoint includes `state`, the submit path does not, which is
    what `with_state_in_error` selects.

    ONLY THE MARKET LOOKUP IS SHARED. Each caller keeps its own NY branch on
    purpose: the reference endpoint lists every active ny_counties row, while
    the submit path does an exact-match existence check against that table.
    Folding those together would quietly make the live NY path
    case-insensitive, which is a change to a production market and not part of
    adding Virginia.
    """
    try:
        found = markets.counties(state)
    except markets.MarketDataUnavailable as e:
        payload = {"error": str(e), "market_data_unavailable": True}
        if with_state_in_error:
            payload["state"] = state
        return None, None, (jsonify(payload), 503)
    except PerchValidationError as e:
        payload = {"error": str(e)}
        if with_state_in_error:
            payload["state"] = state
        return None, None, (jsonify(payload), 400)
    return found["counties"], found["source"], None


def _sends_status(state):
    """Does this market's self-attestation payload carry a `status` key?

    Read from the market's own self_attestation_options - never from a state
    comparison. NY publishes ["accepted", "rejected"] so it keeps sending
    status; VA publishes [] so the key is omitted.

    FAILS SAFE TOWARD TODAY'S BEHAVIOUR. If the market layer cannot answer
    (outage, or mock mode which has no NY market data), this returns True, which
    is exactly what this route has always required. So an NY enrollment is never
    altered by a market-data problem, and a market that genuinely has no status
    field stops rather than proceeding on a guess - its county list and income
    limits would be unavailable in that situation anyway.
    """
    if not state:
        return True
    try:
        return bool(markets.self_attestation_options(state)["status"])
    except (markets.MarketDataUnavailable, PerchValidationError):
        return True


@bp.route("/enrollments/<int:enrollment_id>/lmi/self-attestation/reference", methods=["GET"])
@require_auth
@require_role("sales_rep", "admin")
def self_attestation_reference(enrollment_id):
    """Threshold table + controlled county list for the attestation form.

    Served from the DB so the values are versioned and updatable by migration -
    never hardcoded in the frontend. Side-effect free.
    """
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    state = _enrollment_state(enrollment)
    sends_status = _sends_status(state)
    existing = _self_attestation_row(enrollment_id)

    # ── COUNTY LIST ───────────────────────────────────────────────────────
    # NY keeps reading ny_counties. That table has backed the live NY dropdown
    # since migration 009 and is already reconciled with Perch in production;
    # repointing a live market's controlled list at a different source is a
    # change of its own, not something to fold into adding Virginia. Every
    # other market reads Perch, which is the only place its counties exist.
    #
    # The state comparison is deliberate conservatism rather than a derived
    # rule. It appears at TWO call sites in this file - here and in the
    # self-attestation submit path below - and both route the non-NY lookup
    # through _market_counties() so the Perch call and its status mapping exist
    # once. The NY branches stay separate by design; see that helper. Moving NY
    # onto markets.counties() - which already falls back to this very table -
    # is the obvious follow-up now that VA HAS been exercised end to end: Perch
    # updated staging, the VA state LMI self-attestation flow was tested
    # successfully, and Perch confirmed Virginia can move toward production
    # (2026-10-27). That consolidation is a change of its own and is not folded
    # in here.
    if state and state != "NY":
        counties, counties_source, err = _market_counties(
            state, with_state_in_error=True)
        if err:
            return err
    else:
        counties = [r["name"] for r in
                    query("SELECT name FROM ny_counties WHERE is_active = 1 ORDER BY name")]
        counties_source = "ny_counties"

    # ── INCOME LIMITS ─────────────────────────────────────────────────────
    # NY publishes ONE statewide 80%-SMI table, versioned locally, so the whole
    # table ships with this response as it always has.
    #
    # Virginia's limits are PER COUNTY, so there is no table to ship: the
    # frontend reads them from GET /api/markets/county-income-limits once a
    # county is chosen. `income_basis` tells it which shape to expect, so the
    # frontend never has to know a state name either.
    version = query_one("SELECT * FROM perch_lmi_threshold_versions "
                        "WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    statewide = version is not None and counties_source == "ny_counties"
    if statewide:
        rows = query("SELECT occupancy, income_level_cents FROM perch_lmi_income_thresholds "
                     "WHERE version_label = ? AND is_active = 1 ORDER BY occupancy",
                     (version["version_label"],))
    else:
        rows = []
    if counties_source == "ny_counties" and not version:
        # Unchanged behaviour for the live market: without the versioned table
        # the NY form cannot be rendered honestly.
        return jsonify({"error": "No active income-threshold table is configured."}), 500

    # E3.1 - THE ENROLLED COUNTY WINS.
    #
    # A v2 enrollment that was submitted with a county is BOUND to it: Perch
    # stored it and will reuse it for this attestation, so offering the rep a
    # list here would be offering them a way to make UBS's income test
    # disagree with the enrollment Perch actually created. The list collapses
    # to that one county and the frontend renders it as a fixed value.
    #
    # v1 and any enrollment with no stored county are untouched: `locked` is
    # False, the full controlled list is returned, and the form behaves
    # exactly as it did.
    bound_county, county_locked = _authoritative_county(enrollment)
    if county_locked:
        counties = [bound_county]

    return jsonify({
        "state": state,
        # Whether the payload carries `status`, derived from the market's own
        # options. The frontend asks the above/below question when true and the
        # qualification question when false.
        "sends_status": sends_status,
        "income_basis": "statewide_table" if statewide else "per_county",
        "counties_source": counties_source,
        # E3.1: the county this enrollment is bound to, and whether it may be
        # changed here. When locked, the income limits MUST be read against
        # this county - it is the one Perch enrolled the customer under.
        "county": bound_county,
        "county_locked": county_locked,
        "version_label": version["version_label"] if statewide else None,
        "source_document": version["source_document"] if statewide else None,
        "table_caption": version["table_caption"] if statewide else None,
        "thresholds": [{"occupancy": r["occupancy"],
                        "income_level_cents": r["income_level_cents"]} for r in rows],
        "counties": counties,
        "already_generated": bool(existing and existing["generated_at"]),
        "already_accepted": bool(existing and existing["accepted_at"]),
        # The rep's own previous answers, so a RESUMED enrollment restores the
        # self-attestation section instead of presenting it blank. Read-only,
        # from columns that already exist on perch_self_attestation_submissions;
        # no schema change. Null when nothing has been submitted yet.
        "submitted": {
            "occupancy": existing["occupancy"] if existing else None,
            "county": existing["county"] if existing else None,
            "status": existing["status"] if existing else None,
        } if existing else None,
    })


@bp.route("/enrollments/<int:enrollment_id>/lmi/self-attestation", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def generate_self_attestation(enrollment_id):
    """Rep submits household size, county and the above/below selection."""
    enrollment, err = _visible(enrollment_id)
    if err:
        return err

    existing = _self_attestation_row(enrollment_id)
    if existing and existing["accepted_at"]:
        return jsonify({"error": "This self-attestation has already been accepted.",
                        "already_accepted": True}), 409

    data = request.get_json(force=True, silent=True) or {}
    occupancy = data.get("occupancy")
    county = (data.get("county") or "").strip()

    state = _enrollment_state(enrollment)
    sends_status = _sends_status(state)

    # ---- validate against the CONTROLLED sources, before touching Perch ----
    try:
        occupancy = int(occupancy)
    except (TypeError, ValueError):
        return jsonify({"error": "Select the number of people living in the home."}), 400
    # 1-8 is the documented range for every market, so this bound is checked
    # against the local table for NY and against the range itself elsewhere -
    # the VA limits live per-county at Perch, not in this table.
    if sends_status or (state or "NY") == "NY":
        if not query_one("SELECT 1 FROM perch_lmi_income_thresholds "
                         "WHERE occupancy = ? AND is_active = 1", (occupancy,)):
            # The published table covers 1-8, matching the documented range.
            return jsonify({"error": "Household size must be between 1 and 8."}), 400
    elif not 1 <= occupancy <= 8:
        return jsonify({"error": "Household size must be between 1 and 8."}), 400

    # ---- county: THE ENROLLED ONE, when there is one (E3.1) ---------------
    #
    # A v2 enrollment submitted with a county is bound to it. Perch stored it
    # and will use it for this attestation whatever is sent here, so UBS must
    # make its own eligibility decision against the SAME county - otherwise
    # the income limits are read for one county while the customer is enrolled
    # under another, and both halves look correct in isolation.
    #
    # A submitted county that DISAGREES is refused rather than reconciled.
    # Silently preferring one would be choosing which county the customer
    # lives in, which is not UBS's to choose, and silently preferring the
    # other would re-create the mismatch. The realistic cause is a stale form
    # left open across a county change, so the message says to reload.
    bound_county, county_locked = _authoritative_county(enrollment)
    if county_locked:
        if county and county.strip().lower() != bound_county.strip().lower():
            audit.log("self_attestation_county_conflict",
                      enrollment_id=enrollment_id, user_id=g.current_user["id"],
                      details={"submitted": county, "enrolled": bound_county},
                      ip_address=request.remote_addr)
            return jsonify({
                "error": "This enrollment was submitted for "
                         f"{bound_county}, so the eligibility check has to "
                         "use that county. Reload this page to continue.",
                "county_conflict": True,
                "enrolled_county": bound_county,
            }), 409
        county = bound_county

    # ---- county: a controlled list, from whichever source owns this market ---
    # Perch 422s on free text, so the value must come from a list we fetched.
    # The NY branch is the original query, unchanged. The bound county above
    # is validated by exactly the same rules - it came from this list when the
    # rep chose it on Availability, and is re-checked here rather than trusted
    # because the controlled list is what Perch validates against.
    if state and state != "NY":
        allowed, _source, err = _market_counties(state)
        if err:
            return err
        # Matched case-insensitively, then SENT AS PERCH SPELLS IT. Virginia's
        # independent cities ("Alexandria city") must not be title-cased or
        # re-suffixed on the way through.
        match = next((c for c in allowed if c.lower() == county.lower()), None)
        if not match:
            return jsonify({"error": f"Select a {state} county from the list."}), 400
        county = match
    elif not county or not query_one(
            "SELECT 1 FROM ny_counties WHERE name = ? AND is_active = 1", (county,)):
        # Controlled list only - Perch validates county and would 422 on free text.
        return jsonify({"error": "Select a New York county from the list."}), 400

    # ---- the income answer ------------------------------------------------
    # Two shapes, chosen by whether the market HAS a status field:
    #
    #   status field exists (NY)  -> `status`, "accepted" or "rejected", sent on
    #                                to Perch exactly as before.
    #   no status field (VA)      -> `qualifies`, true or false, which Perch
    #                                never sees. Perch has no way to express
    #                                "does not qualify", so a false answer must
    #                                stop the enrollment HERE.
    if sends_status:
        status = (data.get("status") or "").strip()
        if status not in ("accepted", "rejected"):
            return jsonify({"error": "Select whether the household income is at or below, "
                                     "or above, the level shown."}), 400
    else:
        status = None
        qualifies = data.get("qualifies")
        if not isinstance(qualifies, bool):
            return jsonify({"error": "Confirm whether the household meets the income "
                                     "requirement shown."}), 400
        if not qualifies:
            # HARD STOP. No Perch call is made - not /lmi/self_attestation, not
            # /accept, not /contracts. Nothing is written to
            # perch_self_attestation_submissions either, because no attestation
            # was submitted; the decision lives in the workflow state, which is
            # what the contracts route checks.
            workflow.set_state(enrollment_id, "lmi_not_qualified", recognized=False,
                               last_response={"not_qualified": True,
                                              "occupancy": occupancy,
                                              "county": county})
            audit.log("perch_self_attestation_not_qualified",
                      enrollment_id=enrollment_id, user_id=g.current_user["id"],
                      details={"occupancy": occupancy, "county": county,
                               "state": state},
                      ip_address=request.remote_addr)
            return jsonify({
                "not_qualified": True,
                "next_step_key": "lmi_not_qualified",
                "message": ("This customer does not qualify for the community solar "
                            "program currently available in their area, which requires "
                            "household income at or below the published limit. The "
                            "enrollment cannot continue."),
            })

    account = query_one("SELECT ua.account_number FROM utility_accounts ua "
                        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
                        (enrollment_id,))
    if not account or not account["account_number"]:
        return jsonify({"error": "The utility account number is missing for this "
                                 "enrollment."}), 400

    version = query_one("SELECT version_label FROM perch_lmi_threshold_versions "
                        "WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    try:
        result = adapter.submit_self_attestation(
            enrollment_id, account["account_number"], occupancy, county, status,
            user_id=g.current_user["id"])
    except PerchError as e:
        audit.log("perch_self_attestation_failed", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"error": str(e)[:400]},
                  ip_address=request.remote_addr)
        return jsonify({"error": str(e), "perch_error": type(e).__name__}), getattr(
            e, "http_status", 502)

    step_key, recognized = _next_key_of(result)
    workflow.set_state(enrollment_id, step_key or "unknown_next_step",
                       next_step_url=_next_step_raw(result), recognized=recognized)

    # LEDGER. document_url_returned is a BOOLEAN - the presigned URL is not stored.
    if existing:
        execute("""UPDATE perch_self_attestation_submissions
                   SET occupancy=?, county=?, status=?, version_label=?,
                       document_url_returned=?, generated_at=datetime('now'),
                       generated_by_user_id=?, updated_at=datetime('now')
                   WHERE enrollment_id=?""",
                (occupancy, county, status, version["version_label"] if version else None,
                 1 if result.get("document_available") else 0,
                 g.current_user["id"], enrollment_id))
    else:
        execute("""INSERT INTO perch_self_attestation_submissions
                   (enrollment_id, occupancy, county, status, version_label,
                    document_url_returned, generated_at, generated_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
                (enrollment_id, occupancy, county, status,
                 version["version_label"] if version else None,
                 1 if result.get("document_available") else 0, g.current_user["id"]))

    audit.log("perch_self_attestation_generated", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"occupancy": occupancy, "county": county, "status": status},
              ip_address=request.remote_addr)

    # ── CHAIN STRAIGHT INTO ACCEPTANCE ────────────────────────────────────
    # Self-attestation is a REP-SIDE ELIGIBILITY step, not a customer-facing
    # acceptance. Perch confirmed the rep may attest on the customer's behalf.
    #
    # Leaving the enrollment parked at self_attestation_accept is what broke
    # live staging: the customer signed in, the portal went straight for
    # /contracts, and Perch correctly refused with "Previous stages must be
    # completed first."
    #
    # Accepting here means one rep action completes generate -> accept ->
    # contracts, and the customer only ever sees the NORMAL agreement package.
    # The customer-facing contract acceptance is unchanged and still theirs.
    if step_key == "self_attestation_accept" and not (existing and existing["accepted_at"]):
        metadata = {
            # Same clock the contract-acceptance path uses.
            "timestamp": acceptance_timestamp(),
            "ip_address": _client_ip(),
            "user_agent": (request.headers.get("User-Agent") or "")[:2048],
        }
        try:
            accept_result = adapter.accept_self_attestation(
                enrollment_id, metadata, user_id=g.current_user["id"])
        except PerchAmbiguousOutcomeError as e:
            # MAY have landed. Never retried - reconcile via /status, the same
            # conservative pattern /enroll and /contracts/accept use.
            workflow.set_state(enrollment_id, "self_attestation_accept",
                               last_response={"uncertain": True, "detail": str(e)})
            audit.log("perch_self_attestation_accept_uncertain",
                      enrollment_id=enrollment_id, user_id=g.current_user["id"],
                      details={"detail": str(e)[:400]}, ip_address=request.remote_addr)
            return jsonify({
                "error": "The self-attestation was prepared, but its acceptance "
                         "outcome is uncertain. Check the enrollment status before "
                         "retrying.",
                "uncertain": True,
            }), 502
        except PerchError as e:
            # The document EXISTS at Perch - the ledger records that. Acceptance
            # can be retried from the reference/resume path without regenerating.
            audit.log("perch_self_attestation_accept_failed", enrollment_id=enrollment_id,
                      user_id=g.current_user["id"], details={"error": str(e)[:400]},
                      ip_address=request.remote_addr)
            return jsonify({"error": str(e), "perch_error": type(e).__name__,
                            "document_prepared": True}), getattr(e, "http_status", 502)

        step_key, recognized = _next_key_of(accept_result)
        workflow.set_state(enrollment_id, step_key or "unknown_next_step",
                           next_step_url=_next_step_raw(accept_result),
                           recognized=recognized)
        execute("""UPDATE perch_self_attestation_submissions
                   SET accepted_at=datetime('now'), accepted_next_step=?,
                       updated_at=datetime('now')
                   WHERE enrollment_id=?""", (step_key, enrollment_id))
        audit.log("perch_self_attestation_accepted", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"next_step": step_key},
                  ip_address=request.remote_addr)

    return jsonify({
        "next_step_key": step_key,
        "next_step_recognized": recognized,
        "document_available": bool(result.get("document_available")),
    })


@bp.route("/enrollments/<int:enrollment_id>/lmi/self-attestation/document", methods=["GET"])
@require_customer_auth
def customer_self_attestation_document(enrollment_id):
    """Re-fetch the official attestation document for the customer to review.

    The presigned URL is never stored, so it is obtained fresh from Perch on each
    view - the same treatment contracts already receive. Re-calling generate with
    the SAME persisted answers is safe and returns fresh links to the same
    document; it does not create a second attestation.
    """
    # Use the SAME actor-aware helper every other dual-actor route uses: it
    # checks the token's enrollment id AND ownership, rather than one ad-hoc
    # comparison.
    _enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    row = _self_attestation_row(enrollment_id)
    if not row or not row["generated_at"]:
        return jsonify({"error": "No self-attestation has been prepared for this "
                                 "enrollment."}), 404

    account = query_one("SELECT ua.account_number FROM utility_accounts ua "
                        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
                        (enrollment_id,))
    try:
        result = adapter.submit_self_attestation(
            enrollment_id, account["account_number"], row["occupancy"],
            row["county"], row["status"], user_id=None)
    except PerchError as e:
        return jsonify({"error": str(e), "perch_error": type(e).__name__}), getattr(
            e, "http_status", 502)

    url = ((result.get("raw") or {}).get("self_attestation") or {}).get("url")
    if not url:
        return jsonify({"error": "The self-attestation document could not be opened "
                                 "right now. Please try again."}), 502
    response = jsonify({
        "document_url": url,          # handed to the browser, never persisted
        "already_accepted": bool(row["accepted_at"]),
    })
    response.headers["Cache-Control"] = "no-store, private"
    return response


@bp.route("/enrollments/<int:enrollment_id>/lmi/self-attestation/accept", methods=["POST"])
@require_customer_auth
def accept_self_attestation(enrollment_id):
    """Customer accepts the official self-attestation document.

    Fired at the MOMENT of agreement so the metadata Perch requires (timestamp,
    ip_address, user_agent) describes that moment. Customer-scoped: this is the
    customer's act, not the rep's.
    """
    _enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    # A not-qualified enrollment has no attestation row at all, so the next
    # check would already stop it. This one makes the reason explicit rather
    # than reporting "not prepared yet" for something that will never be.
    blocked = _blocked_guard(enrollment_id)
    if blocked:
        return blocked

    row = _self_attestation_row(enrollment_id)
    if not row or not row["generated_at"]:
        return jsonify({"error": "The self-attestation document has not been prepared "
                                 "yet."}), 409
    if row["accepted_at"]:
        # IDEMPOTENCE: never accept twice. Resume into the persisted next step.
        return jsonify({
            "already_accepted": True,
            "next_step_key": row["accepted_next_step"] or "contracts",
        })

    metadata = {
        # Same clock the contract acceptance path uses, so both are consistent.
        "timestamp": acceptance_timestamp(),
        "ip_address": _client_ip(),
        "user_agent": (request.headers.get("User-Agent") or "")[:2048],
    }
    try:
        result = adapter.accept_self_attestation(enrollment_id, metadata, user_id=None)
    except PerchAmbiguousOutcomeError as e:
        # Acceptance MAY have landed. Do not retry - reconcile via /status, the
        # same conservative pattern /enroll and /contracts/accept already use.
        workflow.set_state(enrollment_id, "self_attestation_accept",
                           last_response={"uncertain": True, "detail": str(e)})
        audit.log("perch_self_attestation_accept_uncertain", enrollment_id=enrollment_id,
                  details={"detail": str(e)[:400]}, ip_address=request.remote_addr)
        return jsonify({"error": "Acceptance outcome is uncertain. Check status before "
                                 "retrying.", "uncertain": True}), 502
    except PerchError as e:
        return jsonify({"error": str(e), "perch_error": type(e).__name__}), getattr(
            e, "http_status", 502)

    step_key, recognized = _next_key_of(result)
    workflow.set_state(enrollment_id, step_key or "unknown_next_step",
                       next_step_url=_next_step_raw(result), recognized=recognized)
    execute("""UPDATE perch_self_attestation_submissions
               SET accepted_at=datetime('now'), accepted_next_step=?,
                   updated_at=datetime('now')
               WHERE enrollment_id=?""", (step_key, enrollment_id))
    audit.log("perch_self_attestation_accepted", enrollment_id=enrollment_id,
              details={"next_step": step_key}, ip_address=request.remote_addr)

    return jsonify({"next_step_key": step_key, "next_step_recognized": recognized})
