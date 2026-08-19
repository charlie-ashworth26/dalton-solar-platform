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

from db import query_one, execute
from auth import require_auth, require_role, require_staff_or_customer
from helpers import next_enrollment_code, resolve_stored_path
from services import audit, status_machine
from services.perch import adapter, workflow, utilities
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
    descriptor = workflow.resolve(enrollment_id)
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
    return jsonify({"workflow": workflow.resolve(enrollment_id)})


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
    audit.log(
        f"perch_{operation}_failed", enrollment_id=enrollment_id,
        user_id=_actor_id(), details=_actor_details({"error": str(exc)}),
        ip_address=request.remote_addr,
    )
    return jsonify({"error": str(exc), "perch_error": type(exc).__name__}), getattr(exc, "http_status", 502)


def _next_key(url):
    key, recognized = workflow.resolve_next_step_key(url)
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
        return jsonify({"error": "A complete billing/mailing address is required by Perch."}), 400

    digits = ''.join(ch for ch in (customer["phone"] or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return jsonify({"error": "Phone number must contain exactly 10 US digits."}), 400

    pdf_path = cleanup_path = None
    try:
        pdf_path, cleanup_path = _bill_pdf_path(doc)
        payload = {
            "email_address": customer["email"],
            "first_name": customer["first_name"],
            "last_name": customer["last_name"],
            "phone_number": digits,
            "billing_address": billing,
            "utility_accounts": [{
                "utility_account_number": acct["account_number"],
                "secondary_account_identifier": acct["secondary_account_identifier"],
                "service_address": {
                    "address_1": svc["street"], "address_2": svc["unit"],
                    "city": svc["city"], "state": svc["state"], "zip": svc["zip"],
                },
                "utility_bills": [pdf_path],
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
        return _perch_error_response(enrollment_id, "enroll", e)
    finally:
        if cleanup_path:
            import os
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

    step_key, recognized = _next_key(result.get("next_step_url"))
    workflow.set_state(
        enrollment_id, step_key or "unknown_next_step",
        next_step_url=result.get("next_step_url"), recognized=recognized,
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
        return jsonify({"error": "Select a proof-document type supported by Perch."}), 400
    if not name_on_document:
        return jsonify({"error": "Name on document is required."}), 400
    if relationship not in valid_relationships:
        return jsonify({"error": "Relationship must be self, other, or unknown."}), 400
    if document_type not in valid_doc_types:
        return jsonify({"error": "Select the physical document format."}), 400

    documents = [{
        "source_type": source_type,
        "name_on_document": name_on_document,
        "relationship": relationship,
        "document_type": document_type,
        "file_path": resolve_stored_path(doc["stored_path"]),
    }]
    try:
        result = adapter.submit_proof_docs(
            enrollment_id, acct["account_number"], documents,
            user_id=g.current_user["id"],
        )
    except (PerchError, ValueError) as e:
        if isinstance(e, PerchError):
            return _perch_error_response(enrollment_id, "proof_docs", e)
        return jsonify({"error": str(e)}), 400

    step_key, recognized = _next_key(result.get("next_step_url"))
    workflow.set_state(
        enrollment_id, step_key or "unknown_next_step",
        next_step_url=result.get("next_step_url"), recognized=recognized,
        last_response={"proof_document_id": doc["id"], "source_type": source_type},
    )
    return jsonify({"result": result, "next_step_key": step_key, "next_step_recognized": recognized})


@bp.route("/enrollments/<int:enrollment_id>/contracts", methods=["POST"])
@require_staff_or_customer
def generate_perch_contracts(enrollment_id):
    enrollment, err = _visible_to_actor(enrollment_id)
    if err:
        return err
    try:
        result = adapter.generate_contracts(enrollment_id, user_id=_actor_id())
    except PerchError as e:
        return _perch_error_response(enrollment_id, "contracts", e)

    safe = adapter.contracts_safe(result) if hasattr(adapter, "contracts_safe") else result.get("contracts", [])
    step_key, recognized = _next_key(result.get("next_step"))
    # Persist only the URL-free contract metadata. Presigned URLs remain in the
    # local `result` object for this request and die with it.
    workflow.set_state(
        enrollment_id, "contracts_review",
        next_step_url=result.get("next_step"), recognized=recognized,
        last_response={"contracts": safe, "contract_count": len(safe)},
    )
    return jsonify({
        "contracts": safe,
        "contract_count": len(safe),
        "next_step_url": result.get("next_step"),
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
    try:
        saved = json.loads(state.get("last_response_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    safe_contracts = saved.get("contracts") or []
    if index < 0 or index >= len(safe_contracts):
        return jsonify({"error": "That contract is not in the current Perch review packet."}), 404

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
        return jsonify({"error": "Perch did not return that contract in the refreshed packet."}), 502
    url = item.get("url") if isinstance(item, dict) else None
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return jsonify({"error": "Perch did not return a review URL for that contract."}), 502
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
    try:
        saved = json.loads(state.get("last_response_json") or "{}")
    except (TypeError, ValueError):
        return []
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
        try:
            saved = json.loads(state.get("last_response_json") or "{}")
        except (TypeError, ValueError):
            saved = {}
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

    details = capacity.get("project_details") or {}
    programs = adapter.available_programs(details)
    return jsonify({
        "enrollment_id": enrollment_id,
        "capacity_checked": True,
        "capacity_available": bool(capacity.get("capacity_available")),
        "utility_slug": capacity.get("utility_slug"),
        "zip_code": capacity.get("zip_code"),
        "available_programs": programs,
        # True only when the rep genuinely has a choice to make. One option is
        # unambiguous and is selected automatically at enroll time.
        "selection_required": len(programs) > 1,
        # The persisted choice, so the wizard hydrates from the backend instead
        # of relying on transient JS state. Only returned when it is still one
        # of the programs currently on offer.
        "selected_customer_type": (
            enrollment["selected_customer_type"]
            if enrollment["selected_customer_type"] in [p["customer_type"] for p in programs]
            else None),
    })


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

    # Delegate to the existing capacity handler so there is ONE capacity
    # implementation, not a near-copy that can drift. The enrollment identity is
    # merged into the successful response because the browser does not know it
    # yet on a first submission.
    resp = check_capacity(enrollment_id)
    body, status = (resp if isinstance(resp, tuple) else (resp, 200))
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

    details = capacity.get("project_details") or {}
    try:
        # Reuses the SAME validator the enroll path uses, so the two can never
        # disagree about what is selectable.
        canonical, _reason = adapter.resolve_customer_type(details, requested=requested)
    except PerchValidationError as e:
        return jsonify({"error": str(e), "perch_error": "PerchValidationError"}), 400

    execute("UPDATE enrollments SET selected_customer_type = ?, "
            "updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
            (canonical, g.current_user["id"], enrollment_id))
    audit.log("enrollment_program_selected", enrollment_id=enrollment_id,
              user_id=g.current_user["id"], details={"customer_type": canonical},
              ip_address=request.remote_addr)
    return jsonify({"enrollment_id": enrollment_id, "selected_customer_type": canonical})
