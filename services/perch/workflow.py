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
    # enrollment is None when the wizard is opened but nothing has been entered
    # yet. No enrollment row exists at that point ON PURPOSE - opening the
    # screen must not persist anything - so the step renders with empty
    # prefills. Every use below is a .get() lookup, so an empty dict is a
    # complete substitute.
    enrollment = enrollment or {}
    return {
        "key": "service_area",
        "eyebrow": "New enrollment",
        "title": "Check availability",
        "subtitle": "Enter the customer's email, ZIP code and utility to see available savings programs.",
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
                "help": "Used to start and securely resume this enrollment.",
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
    """Copy that matches the ACTUAL capacity response.

    The previous version fired on lmi_capacity_available is False and said
    "Residential capacity may still be available" WITHOUT checking whether
    residential was available - so a screen showing Residential: Not available
    still claimed it might be. Every branch below is now derived from the flags
    that were really returned.

    Standard residential is available when EITHER residential_capacity_available
    or small_commercial_capacity_available is set: Perch confirmed that small
    CS = resi in this NY funnel.
    """
    d = d or {}
    notices = []

    residential = bool(d.get("residential_capacity_available")
                       or d.get("small_commercial_capacity_available"))
    lmi = bool(d.get("lmi_capacity_available"))

    # Only mention proof documents when an LMI program is actually on offer -
    # otherwise it warns about a branch this enrollment cannot take.
    if lmi and d.get("proof_documents_required"):
        notices.append({
            "tone": "warn",
            "text": "The income-qualified program requires proof of participation. "
                    "The exact documents are confirmed once the customer is enrolled.",
        })

    if residential and lmi:
        # Both available: the program cards carry the detail, so say nothing.
        pass
    elif residential and not lmi:
        notices.append({
            "tone": "info",
            "text": "Standard residential capacity is available here. The "
                    "income-qualified program is not available at this location.",
        })
    elif lmi and not residential:
        notices.append({
            "tone": "info",
            "text": "Only the income-qualified program is available at this location.",
        })
    else:
        notices.append({
            "tone": "warn",
            "text": "Perch returned no available program for this ZIP and utility.",
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
        "subtitle": "These rates apply to this enrollment.",
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

# ─────────────── Rep-facing progress labels ───────────────
# Every key that can be persisted by a route, mapped to what a rep should see.
# Derived from Perch's own next_step vocabulary plus the keys the contract
# routes set explicitly - NO new state vocabulary is invented here, and
# enrollments.status is deliberately left alone for QA/reporting.
STEP_LABELS = {
    "service_area":               "Service area",
    "capacity_result":            "Capacity confirmed",
    "no_capacity":                "No capacity",
    "enroll":                     "Ready to enroll",
    "proof_docs":                 "Proof documents needed",
    "self_attestation":           "Self-attestation needed",
    "self_attestation_accept":    "Self-attestation acceptance needed",
    "contracts":                  "Ready to generate contracts",
    "contracts_review":           "Contracts awaiting acceptance",
    "contracts_accept":           "Contracts awaiting acceptance",
    "contracts_accepted":         "Complete",
    "contracts_accept_uncertain": "Needs review",
    # /status is in NEXT_STEP_PATH_MAP, so _next_key() can return it if Perch
    # ever points there. Labelled so it can never surface as a bare key.
    "enroll_outcome_uncertain":   "Enrollment outcome uncertain - check status",
    "status":                     "Checking status with Perch",
    "unknown_next_step":          "Needs review",
}

# Terminal: reopening these must never offer a restart or re-submit path.
TERMINAL_STEP_KEYS = {"contracts_accepted"}
# Blocked: reopened read-only pending human reconciliation with Perch.
BLOCKED_STEP_KEYS = {"contracts_accept_uncertain", "enroll_outcome_uncertain"}


def step_label(step_key):
    return STEP_LABELS.get(step_key, "In progress")


def is_terminal(step_key):
    return step_key in TERMINAL_STEP_KEYS


def is_blocked(step_key):
    return step_key in BLOCKED_STEP_KEYS


def _step_mid_flow(enrollment, step_key, state):
    """Descriptor for a persisted step the GUI drives through dedicated routes
    (enroll / proof docs / contracts) rather than the descriptor engine.

    Without this, resolve() fell through to the capacity branch and silently
    showed the wrong screen when an enrollment was reopened mid-flow.
    """
    return {
        "key": step_key,
        "eyebrow": "Enrollment in progress",
        "title": step_label(step_key),
        "subtitle": "This enrollment is already underway. Continue from the step below.",
        "fields": [],
        "uploads": [],
        "panels": [],
        "terminal": is_terminal(step_key),
        "blocked": is_blocked(step_key),
        "perch_next_step": {
            "url": (state or {}).get("perch_next_step_url"),
            "recognized": bool((state or {}).get("next_step_recognized", 1)),
            "resolved_step": step_key,
        },
        "primary_action": None,
        "secondary_action": {"label": "Back to dashboard", "operation": "exit"},
    }


def resolve(enrollment_id):
    """Returns the full workflow descriptor for an enrollment: the current step,
    plus enough context for the renderer to draw it."""
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        return None
    enrollment = dict(enrollment)
    check = adapter.latest_capacity_check(enrollment_id)
    state = get_state(enrollment_id)

    persisted_key = (state or {}).get("current_step_key")
    # Any key past capacity is driven by dedicated routes; return a descriptor
    # for it so a reopened enrollment never lands on the wrong screen.
    if persisted_key and persisted_key not in (
            "service_area", "capacity_result", "no_capacity"):
        return {
            "enrollment_id": enrollment_id,
            "enrollment_code": enrollment["enrollment_code"],
            "status": enrollment["status"],
            "step": _step_mid_flow(enrollment, persisted_key, state),
            "workflow_state": {
                "current_step_key": persisted_key,
                "perch_next_step_url": (state or {}).get("perch_next_step_url"),
                "next_step_recognized": bool((state or {}).get("next_step_recognized", 1)),
            },
            "progress": _progress_hint(persisted_key),
        }

    if state and persisted_key == "service_area":
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
