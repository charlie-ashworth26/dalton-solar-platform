"""
Perch-facing routes.

Every Perch interaction the browser can trigger goes through here, and every
handler delegates to services.perch.adapter / services.perch.workflow. No Perch
credential, enrollment token, or client instance ever crosses toward the browser.

Milestone 2: the browser no longer asks for "products" - it asks for the current
WORKFLOW STEP and renders whatever descriptor comes back.
"""
import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify, g, redirect

from db import query_one, execute
from auth import require_auth, require_role
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
    rep = _rep_for_current_user()
    data = request.get_json(force=True, silent=True) or {}
    project_id = data.get("project_id")
    utility_slug = None
    if project_id:
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            return jsonify({"error": "Project not found"}), 404
        utility_slug = utilities.resolve_slug(project["utility"])
        if not utility_slug:
            return jsonify({"error": "This Dalton project does not map to a published Perch utility slug."}), 400
    code = next_enrollment_code()
    cur = execute(
        """INSERT INTO enrollments (enrollment_code, project_id, sales_rep_id, status, utility_name, created_by_user_id, updated_by_user_id)
           VALUES (?, ?, ?, 'Draft', ?, ?, ?)""",
        (code, project_id, rep["id"] if rep else None, utility_slug, g.current_user["id"], g.current_user["id"]),
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
        user_id=g.current_user["id"], details={"error": str(exc)},
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
@require_auth
@require_role("sales_rep", "admin")
def generate_perch_contracts(enrollment_id):
    enrollment, err = _visible(enrollment_id)
    if err:
        return err
    try:
        result = adapter.generate_contracts(enrollment_id, user_id=g.current_user["id"])
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
@require_auth
@require_role("sales_rep", "admin")
def review_perch_contract(enrollment_id):
    enrollment, err = _visible(enrollment_id)
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
        "user_id": g.current_user["id"],
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
        "ip_address": request.remote_addr,
        "timestamp": acceptance_timestamp(),
        "user_agent": user_agent,
    }


@bp.route("/enrollments/<int:enrollment_id>/contracts/accept", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def accept_perch_contracts(enrollment_id):
    """Record the customer's acceptance of the whole Perch contract packet.

    Preconditions checked BEFORE any Perch call, so a bad request never reaches
    Perch: authenticated, authorized, enrollment visible, workflow in the
    contract-review state, and explicit customer confirmation present.
    """
    enrollment, err = _visible(enrollment_id)
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
        result = adapter.accept_contracts(enrollment_id, metadata, user_id=g.current_user["id"])
    except PerchAmbiguousOutcomeError as e:
        # Do NOT mark accepted, do NOT mark failed, do NOT retry.
        workflow.set_state(
            enrollment_id, "contracts_accept_uncertain",
            last_response={"uncertain": True, "detail": str(e),
                           "attempted_at": metadata["timestamp"]},
        )
        audit.log("perch_contracts_accept_uncertain", enrollment_id=enrollment_id,
                  user_id=g.current_user["id"], details={"detail": str(e)},
                  ip_address=request.remote_addr)
        status_payload = None
        try:
            # GET /status is side-effect free, so it is safe even here - and it
            # is the only way to discover what actually happened.
            status_payload = adapter.get_status(enrollment_id, user_id=g.current_user["id"])
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
        status_payload = adapter.get_status(enrollment_id, user_id=g.current_user["id"])
    except PerchError:
        status_payload = None

    safe_status = _safe_status(status_payload)
    workflow.set_state(
        enrollment_id, "contracts_accepted",
        next_step_url=None, recognized=True,
        last_response={"message": result.get("message"), "perch_status": safe_status},
    )
    audit.log("perch_contracts_accepted", enrollment_id=enrollment_id,
              user_id=g.current_user["id"],
              details={"message": result.get("message"),
                       "perch_completed": (safe_status or {}).get("completed")},
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
