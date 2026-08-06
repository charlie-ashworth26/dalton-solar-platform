import os
from datetime import date

from flask import Blueprint, jsonify, g, request

from db import query_one, execute
from auth import require_auth, require_role
from helpers import mask_account_number, json_or_none
from services import documents as doc_service, status_machine, audit

bp = Blueprint("agreement_routes", __name__, url_prefix="/api/enrollments")


def _build_ctx(enrollment_id):
    e = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    customer = query_one("SELECT * FROM customers WHERE id = ?", (e["customer_id"],))
    address = query_one("SELECT * FROM service_addresses WHERE id = ?", (e["service_address_id"],))
    utility = query_one("SELECT * FROM utility_accounts WHERE id = ?", (e["utility_account_id"],))
    project = query_one("SELECT * FROM projects WHERE id = ?", (e["project_id"],))
    rep = query_one(
        "SELECT users.full_name FROM sales_reps JOIN users ON users.id = sales_reps.user_id WHERE sales_reps.id = ?",
        (e["sales_rep_id"],),
    )
    lmi = query_one("SELECT * FROM lmi_qualifications WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1", (enrollment_id,))

    addr_str = f"{address['street']}" + (f", {address['unit']}" if address['unit'] else "") + f", {address['city']}, {address['state']} {address['zip']}" if address else "—"

    return {
        "enrollment_code": e["enrollment_code"],
        "customer_name": f"{customer['first_name']} {customer['last_name']}" if customer else "—",
        "customer_email": customer["email"] if customer else "—",
        "customer_phone": customer["phone"] if customer else "—",
        "service_address": addr_str,
        "utility": utility["utility_name"] if utility else "—",
        "account_number_masked": mask_account_number(utility["account_number"]) if utility else "—",
        "project_name": project["name"] if project else "—",
        "savings_pct": project["savings_pct"] if project else 5,
        "effective_date": date.today().isoformat(),
        "rep_name": rep["full_name"] if rep else "—",
        "lmi": {
            "path": lmi["path"] if lmi else None,
            "household_size": lmi["household_size"] if lmi else None,
            "income_threshold": lmi["income_threshold"] if lmi else None,
            "attestation_response": lmi["attestation_response"] if lmi else None,
            "attestation_date": lmi["attestation_date"] if lmi else None,
            "qualification_type": lmi["qualification_type"] if lmi else None,
            "review_result": lmi["review_result"] if lmi else None,
        } if lmi else {"path": None},
    }


DOC_SPECS = [
    ("subscription_agreement", "subscription-agreement.pdf", doc_service.generate_subscription_agreement),
    ("cdg_disclosure", "cdg-disclosure.pdf", doc_service.generate_cdg_disclosure),
    ("esign_consent", "esign-consent.pdf", lambda ctx, p: doc_service.generate_consent_doc(ctx, p, "esign_consent")),
    ("credit_contact_consent", "credit-contact-consent.pdf", lambda ctx, p: doc_service.generate_consent_doc(ctx, p, "credit_contact_consent")),
    ("terms_privacy", "terms-privacy.pdf", lambda ctx, p: doc_service.generate_consent_doc(ctx, p, "terms_privacy")),
]


@bp.route("/<int:enrollment_id>/agreements/generate", methods=["POST"])
@require_auth
@require_role("sales_rep", "admin")
def generate_agreements(enrollment_id):
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return jsonify({"error": "Not found"}), 404
    project = query_one("SELECT * FROM projects WHERE id = ?", (enrollment["project_id"],))
    if not project:
        return jsonify({"error": "Enrollment has no project selected yet"}), 400

    ctx = _build_ctx(enrollment_id)
    gen_dir = os.path.join(doc_service.STORAGE_DIR, str(enrollment_id))
    os.makedirs(gen_dir, exist_ok=True)

    specs = list(DOC_SPECS)
    # Income survey is only part of the packet when the project requires LMI qualification
    if project["lmi_required"]:
        specs.insert(2, ("income_survey", "income-survey.pdf", doc_service.generate_income_survey))

    created = []
    for doc_type, filename, generator in specs:
        out_path = os.path.join(gen_dir, filename)
        generator(ctx, out_path)
        doc_cur = execute(
            """INSERT INTO documents (enrollment_id, doc_category, original_filename, stored_path, mime_type, file_size, uploaded_by_user_id)
               VALUES (?, 'generated_agreement', ?, ?, 'application/pdf', ?, ?)""",
            (enrollment_id, filename, os.path.join("storage", "generated", str(enrollment_id), filename),
             os.path.getsize(out_path), g.current_user["id"]),
        )
        execute(
            """INSERT INTO agreements (enrollment_id, document_type, template_version, effective_date, generated_at, generated_document_id, status)
               VALUES (?, ?, 'v1', ?, datetime('now'), ?, 'generated')""",
            (enrollment_id, doc_type, ctx["effective_date"], doc_cur.lastrowid),
        )
        created.append(doc_type)

    audit.log("agreements_generated", enrollment_id=enrollment_id, user_id=g.current_user["id"],
              details={"documents": created}, ip_address=request.remote_addr)

    try:
        status_machine.transition(enrollment_id, "Agreement Ready", user_id=g.current_user["id"],
                                   reason="Document packet generated", ip_address=request.remote_addr)
    except status_machine.InvalidTransition as e:
        return jsonify({"error": str(e), "documents_generated": created}), 409

    return jsonify({"documents_generated": created, "status": "Agreement Ready"}), 201
