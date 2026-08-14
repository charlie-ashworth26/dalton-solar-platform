"""
Perch workflow state and the service-area/capacity descriptor.

Perch is hypermedia-driven: each response supplies a next_step URL, so Dalton
stores and resolves that URL rather than inventing its own API sequence.

The descriptor renderer deliberately owns only the service-area/capacity entry
point. Once capacity succeeds, the browser returns to Dalton's existing Bill,
Contact, LMI, and Agreement screens. Those screens reuse already-collected data
while this module continues to map Perch next_step URLs into safe branch keys.
Unknown next steps are surfaced instead of guessed.
"""
import json
from urllib.parse import urlparse

from db import query_one, execute
from services.perch import adapter, utilities

# ─────────────── next_step URL -> our step key ───────────────
# Matched on PATH SUFFIX so a staging host or base-URL change doesn't break it.
NEXT_STEP_PATH_MAP = {
    "/enrollments/enroll": "enroll",
    "/enrollments/capacity": "service_area",
    "/enrollments/lmi/proof_docs": "proof_docs",
    "/enrollments/lmi/self_attestation": "self_attestation",
    "/enrollments/lmi/self_attestation/accept": "self_attestation_accept",
    "/enrollments/contracts": "contracts",
    "/enrollments/contracts/accept": "contracts_accept",
    "/enrollments/status": "status",
}


def resolve_next_step_key(next_step_url):
    """Maps a Perch next_step URL to a step key.
    Returns (step_key, recognized). recognized=False means Perch pointed us
    somewhere we have not implemented - that must be visible, not silent."""
    if not next_step_url:
        return None, True
    path = urlparse(next_step_url).path or ""
    for suffix, key in NEXT_STEP_PATH_MAP.items():
        if path.endswith(suffix):
            return key, True
    return None, False


# ─────────────── Workflow state persistence ───────────────

def get_state(enrollment_id):
    row = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id = ?", (enrollment_id,))
    return dict(row) if row else None


def set_state(enrollment_id, step_key, next_step_url=None, recognized=True, last_response=None):
    existing = get_state(enrollment_id)
    payload = json.dumps(last_response, default=str) if last_response is not None else None
    if existing:
        execute(
            """UPDATE perch_workflow_state
               SET current_step_key = ?, perch_next_step_url = ?, next_step_recognized = ?,
                   last_response_json = COALESCE(?, last_response_json), updated_at = datetime('now')
               WHERE enrollment_id = ?""",
            (step_key, next_step_url, 1 if recognized else 0, payload, enrollment_id),
        )
    else:
        execute(
            """INSERT INTO perch_workflow_state
               (enrollment_id, current_step_key, perch_next_step_url, next_step_recognized, last_response_json)
               VALUES (?, ?, ?, ?, ?)""",
            (enrollment_id, step_key, next_step_url, 1 if recognized else 0, payload),
        )
    return get_state(enrollment_id)


# ─────────────── Step descriptor builders ───────────────
# Each returns the contract the frontend renderer consumes.

def _step_service_area(enrollment, last_check):
    return {
        "key": "service_area",
        "eyebrow": "New enrollment",
        "title": "Service area",
        "subtitle": "Enter the customer's email, ZIP code, and utility. Perch returns the "
                    "capacity, savings, and document requirements that apply.",
        "fields": [
            {
                # POST /token requires the customer's email (OpenAPI spec), so it
                # must be collected BEFORE any Perch call - not at a later
                # "customer details" step. It is also the key PATCH /refresh_token
                # uses to resume an interrupted enrollment.
                "name": "email",
                "label": "Customer email",
                "type": "text",
                "required": True,
                "placeholder": "customer@example.com",
                "value": enrollment.get("perch_token_email") or "",
                "help": "Perch requires this to open an enrollment session, and uses it to resume one.",
                "validation": {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                "message": "Enter a valid email address."},
            },
            {
                "name": "zip_code",
                "label": "ZIP code",
                "type": "text",
                "required": True,
                "input_mode": "numeric",
                "max_length": 5,
                "mono": True,
                "placeholder": "13348",
                "value": enrollment.get("service_zip") or "",
                "validation": {"pattern": r"^\d{5}$", "message": "ZIP code must be exactly 5 digits."},
            },
            {
                "name": "utility_name",
                "label": "Utility",
                "type": "select",
                "required": True,
                "placeholder": "Select a utility",
                "value": enrollment.get("utility_name") or "",
                # A project-card enrollment already chose its utility. Keep that
                # choice visible but locked so the rep cannot accidentally pair
                # a Dalton project with a different Perch utility.
                "readonly": bool(enrollment.get("project_id") and enrollment.get("utility_name")),
                "help": "Utility is fixed by the selected Dalton project." if enrollment.get("project_id") and enrollment.get("utility_name") else None,
                # Values are Perch SLUGS. The renderer never sees display names
                # as values, so a display-name/slug mismatch is impossible.
                "options": ([o for o in utilities.select_options() if o["value"] == enrollment.get("utility_name")]
                            if enrollment.get("project_id") and enrollment.get("utility_name")
                            else utilities.select_options()),
                "validation": {"message": "Select the customer's utility."},
            },
        ],
        "uploads": [],
        "panels": [],
        "primary_action": {"label": "Check availability", "operation": "check_capacity"},
        "secondary_action": {"label": "Back to dashboard", "operation": "exit"},
    }


def _capacity_panels(check):
    """Renders the documented project_details. Every value shown here comes
    straight from Perch - none of it is computed locally."""
    d = check.get("project_details") or {}
    segments = [
        ("Residential", d.get("residential_capacity_available")),
        ("Small commercial", d.get("small_commercial_capacity_available")),
        ("Income qualified (LMI)", d.get("lmi_capacity_available")),
    ]
    return [
        {
            "type": "capacity_summary",
            "title": f"{check.get('utility_display_name') or check.get('utility_slug')} - ZIP {check.get('zip_code')}",
            "segments": [{"label": lbl, "available": bool(av)} for lbl, av in segments],
            "metrics": [
                {"label": "Residential / commercial savings",
                 "value": _pct(d.get("savings_percent_for_residential_and_commercial_customers"))},
                {"label": "Income-qualified savings",
                 "value": _pct(d.get("savings_percent_for_lmi_customers"))},
            ],
            "notices": _capacity_notices(d),
        }
    ]


def _pct(v):
    if v is None:
        return "-"
    # Perch sends integers (10, 20). SQLite REAL round-trips them as 10.0, so
    # render whole numbers without a spurious decimal.
    return f"{int(v)}%" if float(v) == int(v) else f"{v}%"


def _capacity_notices(d):
    notices = []
    if d.get("proof_documents_required"):
        notices.append({
            "tone": "warn",
            "text": "This project may require proof documents. Perch confirms the required branch after customer enrollment.",
        })
    if d.get("lmi_capacity_available") is False:
        notices.append({
            "tone": "info",
            "text": "No income-qualified capacity here. Residential capacity may still be "
                    "available at the standard savings rate.",
        })
    return notices


def _step_capacity_result(enrollment, check):
    recognized_key, recognized = resolve_next_step_key(check.get("next_step_url"))
    if recognized and recognized_key == "enroll":
        action = {
            "label": "Continue",
            "operation": "advance",
            "enabled": True,
        }
    elif not recognized:
        action = {
            "label": "Continue",
            "operation": "advance",
            "enabled": False,
            "disabled_reason": "Perch returned a next_step URL we do not recognize. "
                               "This has been logged for engineering review.",
        }
    else:
        action = {"label": "Continue", "operation": "advance", "enabled": False,
                  "disabled_reason": "No further step is implemented yet."}

    return {
        "key": "capacity_result",
        "eyebrow": "New enrollment",
        "title": "Capacity confirmed",
        "subtitle": "These rates are what Perch will enforce at enrollment. They are re-checked "
                    "before submission rather than cached.",
        "fields": [],
        "uploads": [],
        "panels": _capacity_panels(check),
        "perch_next_step": {
            "url": check.get("next_step_url"),
            "recognized": recognized,
            "resolved_step": recognized_key,
        },
        "primary_action": action,
        "secondary_action": {"label": "Change email, ZIP or utility", "operation": "restart_service_area"},
    }


def _step_no_capacity(enrollment, check):
    return {
        "key": "no_capacity",
        "eyebrow": "New enrollment",
        "title": "No capacity available",
        "subtitle": check.get("message")
                    or f"Perch has no open project capacity for {check.get('utility_display_name')} "
                       f"in ZIP {check.get('zip_code')}.",
        "fields": [],
        "uploads": [],
        "panels": [{
            "type": "notice",
            "tone": "warn",
            "title": "Enrollment cannot proceed",
            "text": "Perch returned 503 for this utility and ZIP, which means no open solar "
                    "project capacity. Per Perch's guidance, enrollment must not be submitted "
                    "until a capacity check succeeds. Capacity changes as projects rotate - "
                    "it is worth re-checking later.",
        }],
        "primary_action": {"label": "Try a different ZIP or utility", "operation": "restart_service_area"},
        "secondary_action": {"label": "Back to dashboard", "operation": "exit"},
    }


# ─────────────── Resolution ───────────────

def resolve(enrollment_id):
    """Returns the full workflow descriptor for an enrollment: the current step,
    plus enough context for the renderer to draw it."""
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return None
    enrollment = dict(enrollment)
    check = adapter.latest_capacity_check(enrollment_id)
    state = get_state(enrollment_id)

    if state and state.get("current_step_key") == "service_area":
        step = _step_service_area(enrollment, check)
    elif check is None:
        step = _step_service_area(enrollment, None)
    elif not check["capacity_available"]:
        step = _step_no_capacity(enrollment, check)
    else:
        step = _step_capacity_result(enrollment, check)

    return {
        "enrollment_id": enrollment_id,
        "enrollment_code": enrollment["enrollment_code"],
        "status": enrollment["status"],
        "step": step,
        "workflow_state": {
            "current_step_key": step["key"],
            "perch_next_step_url": (state or {}).get("perch_next_step_url"),
            "next_step_recognized": bool((state or {}).get("next_step_recognized", 1)),
        },
        # Progress is presentational only. The authoritative sequence is whatever
        # Perch's next_step URLs say; this is a hint for the rep, not a state machine.
        "progress": _progress_hint(step["key"]),
    }


PROGRESS_STEPS = [
    {"key": "service_area", "label": "Service area"},
    {"key": "capacity_result", "label": "Capacity"},
    {"key": "enroll", "label": "Customer", "milestone": 3},
    {"key": "contract", "label": "Contract", "milestone": 4},
    {"key": "documents", "label": "Documents", "milestone": 5},
]


def _progress_hint(current_key):
    reached = False
    out = []
    for s in PROGRESS_STEPS:
        item = dict(s)
        if s["key"] == current_key:
            item["state"] = "current"
            reached = True
        elif not reached:
            item["state"] = "done"
        else:
            item["state"] = "upcoming"
        out.append(item)
    # no_capacity is a terminal branch off service_area, not its own column
    if current_key == "no_capacity":
        for item in out:
            item["state"] = "current" if item["key"] == "service_area" else "upcoming"
    return out
