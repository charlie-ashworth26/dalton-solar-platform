import json
import os
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from db import query, query_one, execute, rows_to_list
from auth import require_auth, require_role
from helpers import (mask_account_number, looks_masked, next_enrollment_code,
                     json_or_none, to_json)
from services import audit, status_machine, lmi_validation
from services.authz import visible_enrollment
from services.perch import workflow as perch_workflow, utilities
from services.perch import adapter

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

    # Perch workflow state is the accurate rep-facing progress source.
    # enrollments.status is deliberately NOT changed - QA, developer and
    # reporting routes still depend on that legacy vocabulary.
    wf = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id = ?", (d["id"],))
    step_key = wf["current_step_key"] if wf else None

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
        "billing_address": {
            "same_as_service": bool(d.get("billing_same_as_service", 1)),
            "street": d.get("billing_street"),
            "unit": d.get("billing_unit"),
            "city": d.get("billing_city"),
            "state": d.get("billing_state"),
            "zip": d.get("billing_zip"),
        },
        "utility_account": utility_dict,
        "project": dict(project) if project else None,
        "sales_rep": dict(rep) if rep else None,
        "lmi_qualification": dict(lmi) if lmi else None,
        # Rep-facing progress. next_step_url is a Perch endpoint URL, not a
        # presigned document URL, so it is safe to expose.
        # UTILITY for the dashboard column. enrollments.utility_name persists the
        # Perch SLUG chosen at Availability, so it survives refresh, Back,
        # resume, completion and reload. It was persisted all along but never
        # serialized, which is why the column rendered "—" for every row.
        # Resolved to the published display name; the slug is a readable
        # fallback, and a legacy row with no value stays None so the UI can show
        # "—" honestly rather than inventing one. Never inferred from ZIP or
        # project.
        "utility_name": _utility_display(d.get("utility_name")),
        # ── AVAILABILITY STATE, so a reopened enrollment can restore the
        #    Availability step instead of dead-ending on a later one ──────────
        #
        # ROOT CAUSE THIS FIXES. The email the rep types at Availability is
        # persisted as enrollments.perch_token_email - the Perch session
        # identity, written by services/perch/adapter.py when the token is
        # issued. customers.email is only written later, by the merged Continue
        # on the Customer & Bill step. The frontend rehydrated its Perch context
        # from customers.email alone, so an enrollment abandoned between those
        # two points came back with no email, and the guard on the later step
        # told the rep to "Re-run the availability check before continuing." on
        # a screen that has no way to do that. The value was persisted the whole
        # time; it was simply never serialized.
        "perch_token_email": d.get("perch_token_email"),
        # WHETHER a portal password exists - never the value, and never the
        # hash. The rep needs to know the customer's login is already set up so
        # the form can say so instead of presenting two empty password boxes
        # that look like unfinished work. customers.password_hash is the only
        # thing consulted; nothing derived from it leaves the server.
        "customer_has_password": _has_portal_password(d.get("customer_id")),
        # WHAT IS ACTUALLY ATTACHED. The documents and their set survive in the
        # database across leave/reopen - that was never the problem - but the
        # detail payload said nothing about them, and the browser's DocSets is
        # cleared by resetWizardState() on every open. So a rep who reopened an
        # enrollment saw an empty uploader for a bill that was still attached,
        # and the only way to make the step continue was to upload it again.
        "document_sets": _document_sets_summary(d["id"]),
        # The RAW Perch slug, alongside the display name above. utility_account
        # does not exist until the bill step has run, so the slug the rep chose
        # at Availability was otherwise unavailable on reopen - which also left
        # the Perch context's utility empty.
        "utility_slug": d.get("utility_name"),
        # The ZIP capacity was actually checked against. NOT always the service
        # address ZIP: a real staging enrollment checked 22408 at Availability
        # and recorded 22401 as the service address, so restoring the
        # Availability step from the address would have shown the wrong ZIP.
        "service_zip": d.get("service_zip"),
        # True once Perch has accepted /enroll. Persisted-state derived, so it
        # survives reload/resume/reopen - the frontend flag it replaces did not.
        "perch_committed": perch_workflow.perch_committed(row["id"]),
        "workflow_step_key": step_key,
        "workflow_step_label": perch_workflow.step_label(step_key) if step_key else None,
        "workflow_is_terminal": perch_workflow.is_terminal(step_key),
        "workflow_is_blocked": perch_workflow.is_blocked(step_key),
        # WHEN UBS RECORDED THIS STATUS. perch_workflow_state.updated_at is
        # rewritten by set_state() on every step change, and nothing writes it
        # again once the workflow is terminal - so for a completed enrollment
        # this is the moment it reached its final step.
        #
        # It is deliberately NOT called a Perch completion timestamp: Perch
        # does not give us one, and the read-only screen labels this exactly
        # what it is ("Status recorded"). Nothing is inferred from it.
        "workflow_updated_at": wf["updated_at"] if wf else None,
        "perch_next_step_url": wf["perch_next_step_url"] if wf else None,
        # URL-free workflow payload, so a completed enrollment can be rendered
        # read-only without calling Perch. Contains contract NAMES, never URLs.
        "workflow_last_response": json_or_none(wf["last_response_json"]) if wf else None,
        # Persisted Perch savings for this enrollment, or None. Never computed.
        "program_savings": _program_savings(row["id"]),
        # The rep's explicit program choice, so a resumed wizard hydrates the
        # correct branch (Residential skips eligibility) instead of falling back
        # to the full sequence.
        "selected_customer_type": d.get("selected_customer_type"),
    }



def _document_sets_summary(enrollment_id):
    """The newest document set per category, with its files in page order.

    Read-only and metadata only: ids, filenames, sizes and page order. No
    stored path and no extracted content, so this adds nothing the rep could
    not already request through the document-set endpoint.
    """
    out = {}
    for category in ("utility_bill", "lmi_document"):
        row = query_one(
            "SELECT id FROM document_sets WHERE enrollment_id = ? AND category = ? "
            "ORDER BY id DESC LIMIT 1", (enrollment_id, category))
        if not row:
            continue
        files = query(
            "SELECT id, original_filename, page_order, file_size, mime_type "
            "FROM documents WHERE document_set_id = ? "
            "ORDER BY page_order ASC, id ASC", (row["id"],))
        if not files:
            continue
        out[category] = {"document_set_id": row["id"],
                         "files": [dict(f) for f in files],
                         "file_count": len(files)}
    return out


def _has_portal_password(customer_id):
    """Whether this customer has a portal password set. Never the value.

    Asked of the database directly rather than read off the customer row: the
    row is selected with different column sets by different callers, and
    reaching for password_hash on one that did not select it raised IndexError
    inside the serializer - a 500 on an ordinary PATCH. Asking for exactly the
    one fact needed cannot depend on another query's column list.
    """
    if not customer_id:
        return False
    row = query_one(
        "SELECT 1 AS yes FROM customers "
        "WHERE id = ? AND password_hash IS NOT NULL AND password_hash != ''",
        (customer_id,))
    return bool(row)


def _utility_display(slug):
    """Rep-facing name for a persisted utility slug, or None.

    ONE DISPLAY-NAME SYSTEM. This used to look the slug up in perch_utilities
    and return the RAW SLUG for anything not in it - that table is New York's
    reference data, so every new market's utility rendered as
    "dominion-energy-va" on the dashboard, the sales report, the capacity card
    and the utility chip. display_name_for_slug() is the resolver that already
    answers for both: the curated name for a utility in the table, a derived
    clean name for one that is not.
    """
    if not slug:
        return None
    try:
        return utilities.display_name_for_slug(slug)
    except Exception:
        return slug      # readable even if the lookup itself fails


def _program_savings(enrollment_id):
    """Savings percent for THIS enrollment, from data Perch already gave us.

    Reads two already-persisted values and does no arithmetic of its own:
      perch_capacity_checks.savings_percent_lmi / _res_commercial   (Perch)
      perch_workflow_state customer_type from the /enroll response  (Perch)

    Returns None whenever either input is missing. The UI must then OMIT the
    field - a guessed or hardcoded discount on a contract summary would be a
    misrepresentation, not a cosmetic default.
    """
    row = query_one(
        """SELECT savings_percent_res_commercial, savings_percent_lmi
             FROM perch_capacity_checks
            WHERE enrollment_id = ?
         ORDER BY id DESC LIMIT 1""", (enrollment_id,))
    if not row:
        return None

    # WHICH PROGRAM. enrollments.selected_customer_type is the authoritative
    # record of the rep's choice (migration 008) and is what /enroll itself
    # resolves against.
    #
    # This previously read customer_type out of
    # perch_workflow_state.last_response_json. That column holds the LAST step
    # response, and every step after /enroll overwrites it - the capacity
    # response, for one, carries no customer_type at all. So by the time the rep
    # reached Review the lookup returned None, is_lmi went False, and an LMI
    # enrollment was shown the RESIDENTIAL percentage: 5% instead of 20%.
    row_sel = query_one("SELECT selected_customer_type FROM enrollments WHERE id = ?",
                        (enrollment_id,))
    customer_type = row_sel["selected_customer_type"] if row_sel else None

    if not customer_type:
        # Fall back to the /enroll response only when no explicit selection was
        # ever persisted (single-program locations resolve server-side).
        state = query_one("SELECT last_response_json FROM perch_workflow_state "
                          "WHERE enrollment_id = ?", (enrollment_id,))
        # Read through the shared helper, which returns {} for a payload that
        # is not a JSON object. From E2 the capacity step stores Perch's
        # `project_details`, and on v2 that is an ARRAY - `.get()` on it raised
        # AttributeError and turned this whole enrollment LIST into a 500.
        customer_type = perch_workflow.last_response_dict(state).get(
            "customer_type")

    # E2: LMI_IRA is also an income-qualified program, and `== "LMI"` is
    # FALSE for it - which would have shown an IRA enrollment the residential
    # percentage, the exact bug this function was written to fix, one customer
    # type later. Asked through the adapter's family helper so there is one
    # answer to "is this the income-qualified branch" across the codebase.
    is_lmi = adapter.is_lmi_customer_type(customer_type)
    value = row["savings_percent_lmi"] if is_lmi else row["savings_percent_res_commercial"]
    if value is None:
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    return {
        "percent": percent,
        # Which Perch figure this came from, so the UI never has to infer it.
        "basis": "lmi" if is_lmi else "residential_commercial",
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
    # Discarded rows are filtered SERVER-SIDE, so a provisional enrollment
    # abandoned by a failed attempt never reaches any dashboard or listing -
    # this is not a row hidden by the frontend.
    sql = "SELECT * FROM enrollments WHERE discarded_at IS NULL"
    params = []

    if role == "sales_rep":
        rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (g.current_user["id"],))
        sql += " AND sales_rep_id = ?"
        params.append(rep["id"] if rep else -1)

    # Privileged roles see everything by default; ?mine=true narrows to their own.
    # Sales reps are already scoped above and are unaffected by this parameter.
    if role != "sales_rep" and request.args.get("mine") == "true":
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

    # document_set_id AND page_order travel with every row.
    #
    # ROOT CAUSE THIS CLOSES. This flat list is the browser's SECOND source of
    # attached documents (the first being document_sets above), and it used to
    # omit the set id - so rehydrateDocumentsFromDetail() rebuilt the uploader
    # with `id: null` and commented that the set id "is not exposed on detail".
    # That null is what made removing a saved photo a no-op: docSetRemove()
    # needs a set id to call the DELETE endpoint and silently gave up without
    # one. The id was in the table the whole time and simply never selected.
    documents = rows_to_list(query(
        "SELECT id, doc_category, original_filename, extraction_confidence, "
        "created_at, document_set_id, page_order "
        "FROM documents WHERE enrollment_id = ? "
        "ORDER BY COALESCE(document_set_id, 0), COALESCE(page_order, 0), id",
        (enrollment_id,)))
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
    enrollment, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
    row = enrollment
    data = request.get_json(force=True, silent=True) or {}

    # COMMIT BOUNDARY. Everything below was part of the /enroll multipart, and
    # Perch confirmed it cannot be updated through the API afterwards. Accepting
    # a local edit here would silently diverge Dalton from Perch - the contracts
    # would still carry the submitted values - so the write is refused rather
    # than quietly saved.
    #
    # The values are NOT wiped or hidden; they stay readable. Only mutation is
    # blocked, and only for sections that Perch actually received.
    if perch_workflow.perch_committed(enrollment_id):
        # FIELD-LEVEL, not section-level. Only what Perch actually RECEIVED in
        # the /enroll multipart is committed:
        #     customer         email_address, first_name, last_name, phone_number
        #     service address, billing address, utility account (+ POD ID)
        # The customer PASSWORD is NOT part of that payload - it is a
        # Dalton-local credential Perch never sees - so a password-only update
        # stays allowed. Blocking it would prevent legitimate credential work on
        # a committed enrollment while protecting nothing.
        PERCH_CUSTOMER_FIELDS = {"first_name", "last_name", "email", "phone"}
        committed_sections = []
        cust = data.get("customer") or {}
        if any(k in cust for k in PERCH_CUSTOMER_FIELDS):
            committed_sections.append("customer")
        for k in ("service_address", "utility_account", "billing_address"):
            if data.get(k):
                committed_sections.append(k)
        if committed_sections:
            audit.log("enrollment_edit_rejected_committed", enrollment_id=enrollment_id,
                      user_id=g.current_user["id"],
                      details={"sections": committed_sections},
                      ip_address=request.remote_addr)
            return jsonify({
                "error": "This enrollment has already been submitted. The customer "
                         "and account details on it can no longer be changed.",
                "committed": True,
                "committed_sections": committed_sections,
            }), 409

    # Customer
    if "customer" in data:
        c = data["customer"] or {}

        # Write ONLY the identity columns the caller actually supplied.
        #
        # This previously wrote all four unconditionally with c.get(...), so a
        # password-only payload set first_name/last_name/email/phone to NULL and
        # died on "NOT NULL constraint failed: customers.first_name" - before
        # the password statement below ever ran. Absent is now distinct from
        # empty: a key that is not in the payload is left alone.
        IDENTITY_FIELDS = ("first_name", "last_name", "email", "phone")
        provided = [k for k in IDENTITY_FIELDS if k in c]

        if row["customer_id"]:
            customer_id = row["customer_id"]
            if provided:
                # Column names come from the fixed whitelist above, never from
                # caller input, so this interpolation cannot inject.
                assignments = ", ".join(f"{k}=?" for k in provided)
                execute(
                    f"UPDATE customers SET {assignments}, updated_at=datetime('now') WHERE id=?",
                    tuple(c.get(k) for k in provided) + (row["customer_id"],),
                )
        else:
            if not provided:
                # A customer cannot be created from a credential alone - there
                # is no identity to attach it to.
                return jsonify({"error": "Customer details are required before "
                                         "setting a password."}), 400

            # customers.first_name, last_name and email are all NOT NULL, so a
            # row cannot be created from a subset of them.
            #
            # WHY THIS GUARD EXISTS. The INSERT below used to hit the constraint
            # and surface as a 500 "An unexpected server error occurred." for a
            # perfectly legitimate partial payload - a name with no email, which
            # is exactly what a progress save looks like. An opaque 500 is the
            # wrong answer to "you have not given me enough yet": the caller
            # cannot tell a bug from a missing field. 400 says which.
            missing = [k for k in ("first_name", "last_name", "email")
                       if not (c.get(k) or "").strip()]
            if missing:
                return jsonify({
                    "error": "A customer needs a first name, last name and "
                             "email address before it can be saved.",
                    "missing_fields": missing,
                }), 400
            columns = ", ".join(provided)
            placeholders = ", ".join("?" for _ in provided)
            cur = execute(
                f"INSERT INTO customers ({columns}) VALUES ({placeholders})",
                tuple(c.get(k) for k in provided),
            )
            customer_id = cur.lastrowid
            execute("UPDATE enrollments SET customer_id=? WHERE id=?", (customer_id, enrollment_id))

        if c.get("password"):
            # Hashed, never stored in plaintext. This is a Dalton-local
            # credential - Perch never receives it - which is why the commit
            # boundary above deliberately allows a password-only update.
            from auth import hash_password
            execute("UPDATE customers SET password_hash=?, updated_at=datetime('now') WHERE id=?",
                    (hash_password(c["password"]), customer_id))

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

    # Billing / mailing address (Perch requires this separately from service address).
    if "billing_address" in data:
        b = data["billing_address"] or {}
        same = 1 if b.get("same_as_service", True) else 0
        if same:
            current = query_one("SELECT service_address_id FROM enrollments WHERE id=?", (enrollment_id,))
            svc = query_one("SELECT * FROM service_addresses WHERE id=?", (current["service_address_id"],)) if current and current["service_address_id"] else None
            if svc:
                execute(
                    """UPDATE enrollments SET billing_same_as_service=1, billing_street=?, billing_unit=?,
                       billing_city=?, billing_state=?, billing_zip=? WHERE id=?""",
                    (svc["street"], svc["unit"], svc["city"], svc["state"], svc["zip"], enrollment_id),
                )
        else:
            execute(
                """UPDATE enrollments SET billing_same_as_service=0, billing_street=?, billing_unit=?,
                   billing_city=?, billing_state=?, billing_zip=? WHERE id=?""",
                (b.get("street"), b.get("unit"), b.get("city"), b.get("state", "NY"), b.get("zip"), enrollment_id),
            )

    # Utility account
    if "utility_account" in data:
        u = data["utility_account"]
        current = query_one("SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
        if current["utility_account_id"]:
            # WRITE ONLY WHAT THE CALLER SUPPLIED, exactly as the customer
            # branch above does. This used to UPDATE all six columns from
            # u.get(...), so any payload that omitted one - a progress save
            # carrying only the account and meter numbers, say - silently set
            # rate_class and monthly_usage_kwh to NULL. Absent is now distinct
            # from empty here too.
            ACCOUNT_FIELDS = ("utility_name", "account_number",
                              "secondary_account_identifier", "meter_number",
                              "rate_class", "monthly_usage_kwh")
            provided_acct = [k for k in ACCOUNT_FIELDS if k in u]

            # A DISPLAY MASK IS NOT DATA.
            #
            # This payload is serialised back to a sales_rep with
            # account_number masked ("******7890"), the wizard hydrates its
            # form from it, and the next save returns the field as typed. So
            # an ordinary Save & Exit -> reopen -> Continue wrote the MASK
            # OVER THE REAL ACCOUNT NUMBER - the one value Perch matches the
            # customer on, destroyed by the most ordinary thing a rep does.
            #
            # Treated as "not supplied" rather than rejected: the rep did not
            # change the field, so the stored value is what they meant. An
            # actual edit contains no mask character and writes normally.
            # Guarded HERE, on the write, so no client can do it - the browser
            # fix is the other half, not the protection.
            masked_fields = [k for k in provided_acct if looks_masked(u.get(k))]
            if masked_fields:
                provided_acct = [k for k in provided_acct if k not in masked_fields]
                audit.log("masked_value_ignored_on_write",
                          enrollment_id=enrollment_id, user_id=g.current_user["id"],
                          details={"fields": masked_fields},
                          ip_address=request.remote_addr)

            if provided_acct:
                # Column names come from the fixed whitelist above, never from
                # caller input, so this interpolation cannot inject.
                assignments = ", ".join(f"{k}=?" for k in provided_acct)
                execute(
                    f"UPDATE utility_accounts SET {assignments}, "
                    f"updated_at=datetime('now') WHERE id=?",
                    tuple(u.get(k) for k in provided_acct)
                    + (current["utility_account_id"],),
                )
        else:
            # NO ROW YET, so there is nothing to fall back to and nothing to
            # protect: the account number IS the row. utility_accounts.
            # account_number is NOT NULL, so a payload that carries no usable
            # one has no row to create - and attempting it raised
            # IntegrityError, i.e. a 500 on an ordinary progress save. That was
            # already reachable with the key absent; a masked value is the same
            # situation wearing a value.
            new_account = u.get("account_number")
            if looks_masked(new_account):
                # A mask with no stored value behind it is not something a
                # correct client sends. Nothing is invented from it.
                audit.log("masked_value_ignored_on_write",
                          enrollment_id=enrollment_id, user_id=g.current_user["id"],
                          details={"fields": ["account_number"], "on": "create"},
                          ip_address=request.remote_addr)
                new_account = None
            if new_account:
                cur = execute(
                    """INSERT INTO utility_accounts (customer_id, service_address_id, utility_name, account_number, secondary_account_identifier, meter_number, rate_class, monthly_usage_kwh)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current["customer_id"], current["service_address_id"],
                     u.get("utility_name"), new_account,
                     u.get("secondary_account_identifier"),
                     None if looks_masked(u.get("meter_number")) else u.get("meter_number"),
                     u.get("rate_class"), u.get("monthly_usage_kwh")),
                )
                execute("UPDATE enrollments SET utility_account_id=? WHERE id=?",
                        (cur.lastrowid, enrollment_id))
            # Without one, the rest of the PATCH still saves. The utility
            # account is created on the save that carries a real number.

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
    _, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
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
    _, _authz_err = visible_enrollment(enrollment_id)
    if _authz_err:
        return _authz_err
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
