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
from services.perch import capacity as cap_module

# ─────────────── next_step -> our step key ───────────────
#
# TWO SHAPES, ONE MAP (E1).
#
#   v1 sends a full URL:  https://host/affiliate_partners/v1/enrollments/enroll
#   v2 sends a bare path: /enroll
#
# Both are matched on PATH SUFFIX, so a staging host, a base-URL change or the
# version segment can all move without touching this table. The v1 suffixes
# carry `/enrollments/`, the v2 suffixes do not; listing both means one UBS
# instance can talk to either version - which it must during a migration,
# where a resumed enrollment still holds a v1 URL while new calls return v2
# paths.
#
# Longest suffix wins. Without that, `/contracts/accept` could match the
# `/contracts` entry and send an enrollment back a step; the order a dict
# literal happens to be written in must not decide that.
NEXT_STEP_PATH_MAP = {
    # ── v1: full URLs, whose path contains /enrollments/ ──
    "/enrollments/enroll": "enroll",
    "/enrollments/capacity": "service_area",
    "/enrollments/lmi/proof_docs": "proof_docs",
    "/enrollments/lmi/self_attestation": "self_attestation",
    "/enrollments/lmi/self_attestation/accept": "self_attestation_accept",
    "/enrollments/contracts": "contracts",
    "/enrollments/contracts/accept": "contracts_accept",
    "/enrollments/status": "status",
    # ── v2: bare paths, exactly as the v2 spec documents them ──
    "/enroll": "enroll",
    "/capacity": "service_area",
    "/lmi/proof_docs": "proof_docs",
    "/lmi/self_attestation": "self_attestation",
    "/lmi/self_attestation/accept": "self_attestation_accept",
    "/contracts": "contracts",
    "/contracts/accept": "contracts_accept",
    "/status": "status",
}

# Checked longest-first so a more specific path always beats a prefix of
# itself. Computed once rather than relying on dict insertion order.
_NEXT_STEP_SUFFIXES = sorted(NEXT_STEP_PATH_MAP, key=len, reverse=True)


def resolve_next_step_key(next_step):
    """Maps a Perch next_step - v1 full URL or v2 bare path - to a step key.

    Returns (step_key, recognized). recognized=False means Perch pointed us
    somewhere we have not implemented - that must be visible, not silent.

    Perch's next_step remains AUTHORITATIVE. This translates it; it never
    invents one, and an unrecognized value is reported rather than defaulted.
    """
    if not next_step:
        return None, True
    # urlparse handles both shapes: a bare path comes back unchanged, a full
    # URL loses its scheme, host and query.
    path = urlparse(str(next_step)).path or ""
    # A trailing slash is not a different step.
    path = path.rstrip("/") or path
    for suffix in _NEXT_STEP_SUFFIXES:
        if path.endswith(suffix):
            return NEXT_STEP_PATH_MAP[suffix], True
    return None, False


def resolve_next_step(result):
    """The same resolution, taken straight from a normalized Perch result.

    Routes call this instead of reaching for a key themselves: whether a
    response carried `next_step` (documented on v1, and the only spelling on
    v2) or `next_step_url` (the v1 staging alias) is the client's business,
    not theirs. Returns (step_key, recognized, raw_next_step).
    """
    result = result or {}
    raw = result.get("next_step") or result.get("next_step_url")
    key, recognized = resolve_next_step_key(raw)
    return key, recognized, raw


# ─────────────── Workflow state persistence ───────────────

def get_state(enrollment_id):
    row = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id = ?", (enrollment_id,))
    return dict(row) if row else None


def last_response_dict(state):
    """The last step response as a DICT, or {} when it is not one.

    perch_workflow_state.last_response_json holds whatever the most recent
    step returned, and from E2 that is not always a JSON object: the capacity
    step stores Perch's `project_details`, which on v2 is an ARRAY. Every
    caller then wants a field out of it, and `.get()` on a list raises
    AttributeError - which is how a v2 capacity check turned the whole
    enrollment list into a 500. The response was fine; the reader assumed a
    shape.

    So the shape check lives here rather than at each call site. A non-object
    payload is "no fields to read", which is exactly what a caller asking for
    `customer_type` or `contracts` should get from a capacity array.

    Accepts a state row/dict (what get_state returns) or None.
    """
    if not state:
        return {}
    # Works for a sqlite3.Row (IndexError on a missing column) and for a plain
    # dict (KeyError), without caring which one the caller had.
    try:
        raw = state["last_response_json"]
    except (KeyError, IndexError, TypeError):
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
                # No help line. "Used to start and securely resume this
                # enrollment" restated what the label already says and added
                # a line of grey text above every other field on the step.
                # The label and the placeholder carry it.
                "validation": {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                "message": "Enter a valid email address."},
            },
            {
                # Presentation hint only (C5): a five-digit ZIP and the
                # utility picker share one row instead of each taking the full
                # width of the card. Same family as "placeholder" and "mono" -
                # it says how the field looks, never what it means.
                "width": "half",
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
                "width": "half",
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


def _capacity_model(check):
    """The capacity model behind a check dict, whichever version produced it."""
    return adapter.capacity_model_for(check or {})


def _selected_county(enrollment):
    """The county already chosen for this enrollment, or None."""
    if not enrollment:
        return None
    try:
        value = enrollment["service_county"]
    except (KeyError, IndexError, TypeError):
        return None
    return (value or "").strip() or None


def county_field_required(check):
    """Does this capacity need the rep to say which county the customer is in?

    PUBLIC, because the ROUTES layer asks it before fetching the county list.
    services/perch/workflow.py must not import the markets layer - that
    boundary is pinned by test_market_rules_service.py and exists so the
    enrollment state machine cannot start depending on market lookups - so
    this module decides WHETHER the question applies and the routes layer,
    which may reach markets, supplies the answers.
    """
    return adapter.county_decides_options(_capacity_model(check))


COUNTY_FIELD_NAME = "county"


def _county_field(enrollment, check):
    """(field descriptor or None, blocks_continue).

    The field appears ONLY when the county decides what can be offered here.
    For every location with no county restrictions - which is every v1
    location and most v2 ones - this returns (None, False) and the
    Availability step is byte-identical to what it was.

    The descriptor leaves `options` EMPTY. They are Perch's controlled county
    list for the state, which only the routes layer may fetch; it fills them
    in through fill_county_options() below. A field that reaches the client
    still empty is rendered read-only with an explanation rather than as a
    dropdown with nothing in it.
    """
    if not county_field_required(check):
        return None, False

    selected = _selected_county(enrollment)
    return ({
        "name": COUNTY_FIELD_NAME,
        "label": "County",
        "type": "select",
        "required": True,
        "placeholder": "Select the customer's county",
        "value": selected or "",
        "options": [],
        "help": "Some solar capacity here is limited to particular counties, "
                "so the programs available depend on where the customer lives.",
    }, not selected)


def fill_county_options(descriptor, options, source=None):
    """Put the markets layer's county list into the step's County field.

    Called by the routes layer, which is the only layer allowed to reach
    markets. A no-op for a step that has no County field, so a caller never
    has to check first.

    With no options the field becomes read-only and says so: the question
    applies but cannot be answered, and a dropdown with nothing in it would
    look like a bug rather than an outage.
    """
    step = (descriptor or {}).get("step") or {}
    for field in step.get("fields") or []:
        if field.get("name") != COUNTY_FIELD_NAME:
            continue
        if options:
            field["options"] = [{"value": c, "label": c} for c in options]
            if source == "fallback_db":
                field["help"] = (field.get("help") or "") + \
                    " (Showing the local county list; Perch was unreachable.)"
        else:
            field["readonly"] = True
            field["placeholder"] = "County list unavailable"
            field["help"] = ("Some capacity here is limited to particular "
                             "counties, but the county list could not be "
                             "loaded. Re-check availability to try again.")
            if field.get("value"):
                field["options"] = [{"value": field["value"],
                                     "label": field["value"]}]
    return descriptor


def _capacity_panels(check, county=None, awaiting_county=False):
    """Renders what Perch returned for this location. Every value shown here
    comes straight from the capacity response - none of it is computed locally.

    E2: driven by the normalized entry model rather than the v1 flat fields, so
    one renderer serves both API versions.

    E2.1: the rows are the UBS PROGRAMS, not Perch's capacity types. Listing
    capacity types meant showing "Residential" and "Residential (mass market)"
    as separate rows, which is Perch's internal distinction and not a choice
    the rep has - they enrol a residential customer either way. One row per
    program, named exactly as the program cards name it.

    `county`, when given, narrows availability to what THAT customer can
    actually get: the unrestricted entries plus the ones listing their county.

    `awaiting_county` is the state in between - the county decides here and
    has not been answered. The segments still show what the LOCATION has, so
    the screen does not read as "nothing available" while the rep is being
    asked where the customer lives, but the RATES are withheld: a figure that
    exists only in two counties is not a figure to put in front of a rep
    before anyone has said which county this is.
    """
    model = _capacity_model(check)

    # One row per SELECTABLE program. Business is absent for the same reason
    # it is not selectable - UBS cannot enrol it - and listing a capacity a
    # rep cannot use as "Available" would be an invitation to a dead end.
    #
    # E4: the SAME list available_programs() offers, so a program a market may
    # not sell cannot appear here as "Available" after the cards have stopped
    # offering it - which is exactly what Virginia did until this shared the
    # one source.
    segments = []
    for customer_type in adapter.offerable_customer_types(
            getattr(model, "market_state", None)):
        if customer_type == adapter.CUSTOMER_TYPE_BUSINESS:
            continue
        rule = adapter.PROGRAM_RULES[customer_type]
        available = any(model.has_capacity(t, county=county)
                        for t in rule["capacity_types"])
        # The IRA row appears only when it is genuinely in play. On v1 it is
        # always unavailable - v1 cannot express it - and a permanent
        # "Residential LMI (IRA): Not available" row on every NY screen would
        # be noise reporting a limitation of the API version, not of the
        # location.
        if customer_type == adapter.CUSTOMER_TYPE_LMI_IRA and not available:
            continue
        segments.append((rule["label"], available))

    return [
        {
            "type": "capacity_summary",
            # NEVER the raw slug. The capacity card is the most prominent
            # place a utility name appears, and it read
            # "dominion-energy-va - ZIP 22401" for any market outside the local
            # NY table. The stored display name is used when present, and the
            # shared resolver derives a clean one otherwise - the slug itself
            # is a last resort that should now be unreachable.
            "title": f"{_utility_label(check)} - ZIP {check.get('zip_code')}",
            "segments": [{"label": lbl, "available": bool(av)} for lbl, av in segments],
            # E2: a metric per PROGRAM, from the program mapping, so the rate
            # shown beside a program is the rate that program would enroll at.
            # A program offering several rates renders them all rather than
            # collapsing to one number Perch never promised.
            "metrics": ([] if awaiting_county else [
                {"label": f"{p['label']} savings",
                 "value": _pct_list(p["savings_percents"])}
                for p in adapter.available_programs(model, county=county)
            ]),
            "notices": _capacity_notices(model, county=county),
        }
    ]


def _pct_list(values):
    """One rate, several rates, or none - as text.

    v1 always produced at most one rate, so a single value renders exactly as
    _pct() rendered it before E2 and every existing screen is unchanged.
    """
    values = list(values or [])
    if not values:
        return "-"
    if len(values) == 1:
        return _pct(values[0])
    return ", ".join(_pct(v) for v in values)


def _pct(v):
    if v is None:
        return "-"
    # Perch sends integers (10, 20). SQLite REAL round-trips them as 10.0, so
    # render whole numbers without a spurious decimal.
    return f"{int(v)}%" if float(v) == int(v) else f"{v}%"


def _capacity_notices(model_or_details, county=None):
    """Copy that matches the ACTUAL capacity response.

    An older version fired on lmi_capacity_available is False and said
    "Residential capacity may still be available" WITHOUT checking whether
    residential was available - so a screen showing Residential: Not available
    still claimed it might be. Every branch below is derived from what was
    really returned.

    E2: driven by the program mapping rather than raw flags, so it stays
    correct on both API versions and says nothing about a capacity type the rep
    cannot enroll. Accepts either a CapacityModel or a flat project_details
    dict, because older callers pass the latter.

    E2.1: `county`, when given, narrows the programs these notices describe to
    what that customer can actually get, so the copy matches the cards.
    """
    model = (model_or_details
             if isinstance(model_or_details, cap_module.CapacityModel)
             else cap_module.parse(model_or_details))

    notices = []
    programs = adapter.available_programs(model, county=county)
    offered = {p["customer_type"] for p in programs}
    residential = adapter.CUSTOMER_TYPE_RESIDENTIAL in offered
    lmi_family = [p for p in programs if adapter.is_lmi_customer_type(
        p["customer_type"])]

    # Only mention proof documents when a program that needs them is actually
    # on offer - otherwise it warns about a branch this enrollment cannot take.
    if any(p["lmi_required"] for p in lmi_family):
        notices.append({
            "tone": "warn",
            "text": "The income-qualified program requires proof of participation. "
                    "The exact documents are confirmed once the customer is enrolled.",
        })

    if residential and lmi_family:
        # Both available: the program cards carry the detail, so say nothing.
        pass
    elif residential and not lmi_family:
        notices.append({
            "tone": "info",
            "text": "Standard residential capacity is available here. The "
                    "income-qualified program is not available at this location.",
        })
    elif lmi_family and not residential:
        notices.append({
            "tone": "info",
            "text": "Only the income-qualified program is available at this location.",
        })
    else:
        notices.append({
            "tone": "warn",
            "text": "No program is available for this ZIP and utility.",
        })

    # County restrictions are REPORTED. E2.1 collects the county when it
    # decides the options (the County field above), but UBS still does not
    # SEND it to Perch until E3 - so this keeps the screen honest rather than
    # letting it imply a guarantee Perch has not given. Once a county has been
    # chosen the notice is no longer useful: the cards already reflect it.
    if model.has_county_restrictions() and not county:
        notices.append({
            "tone": "info",
            "text": "Some capacity at this location is limited to particular "
                    "counties. Final eligibility is confirmed by Perch when the "
                    "customer is enrolled.",
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

    # E2.1: the County field. A NORMAL CONDITIONAL FIELD on the step that is
    # already here - not a new screen, not a new step, and not shown at all
    # unless the answer changes what this location can offer. The renderer
    # already draws `select` fields, so this needs no new UI machinery and
    # inherits the step's existing look.
    #
    # Continue is held while the question is outstanding, for the same reason
    # the programs list is withheld: the set of valid programs is not decided
    # yet, so moving on would carry an undecided state forward.
    county_field, county_blocks = _county_field(enrollment, check)
    if county_blocks and action.get("enabled"):
        action = dict(action, enabled=False,
                      disabled_reason="Select the customer's county to see "
                                      "the available savings programs.")

    return {
        "key": "capacity_result",
        "eyebrow": "New enrollment",
        "title": "Capacity confirmed",
        # "These rates apply to this enrollment" is not TRUE while the county
        # is outstanding: a rate that exists only in two counties is not yet
        # known to apply to this customer. The subtitle says what is actually
        # the case until it is answered.
        "subtitle": ("Some capacity here depends on the customer's county. "
                     "Select it to see the rates that apply."
                     if county_blocks
                     else "These rates apply to this enrollment."),
        "fields": ([county_field] if county_field else []),
        "uploads": [],
        "panels": _capacity_panels(check,
                                   county=_selected_county(enrollment),
                                   awaiting_county=county_blocks),
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
        # D-2: what the rep needs is the utility and the ZIP, not who told us.
        # check["message"] comes from upstream, so it is NOT used as rep-facing
        # copy - it is diagnostic and is recorded with the capacity check.
        "subtitle": f"There is no open project capacity for "
                    f"{_utility_label(check)} in ZIP {check.get('zip_code')}.",
        "fields": [],
        "uploads": [],
        "panels": [{
            "type": "notice",
            "tone": "warn",
            "title": "Enrollment cannot proceed",
            "text": "This enrollment cannot be submitted until an availability "
                    "check succeeds for this utility and ZIP. Capacity changes "
                    "as projects rotate, so it is worth checking again later.",
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
def _utility_label(check):
    """The rep-facing utility name for a capacity record.

    One resolver, shared with the dashboard and the reports: the display name
    the capacity response carried, else a clean name derived from the slug,
    else the slug. A rep should never read a slug.
    """
    stored = (check or {}).get("utility_display_name")
    if stored:
        return stored
    slug = (check or {}).get("utility_slug")
    if not slug:
        return "this utility"
    try:
        from services.perch import utilities
        return utilities.display_name_for_slug(slug)
    except Exception:
        return slug


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
    # UBS-INTERNAL, never a Perch next_step.
    #
    # Set when the market publishes no `status` values - so Perch has no field
    # in which "this customer does not qualify" could be expressed - and the
    # customer answers that their household income is above the published
    # limit. Perch is never called for such an enrollment, which is precisely
    # why UBS has to hold the outcome itself.
    #
    # Only a market whose self_attestation_options returns `status: []` can
    # reach this key. New York returns ["accepted", "rejected"], so an NY
    # enrollment records its answer in that field exactly as before and can
    # never land here.
    "lmi_not_qualified":          "Does not qualify for the available program",
    # /status is in NEXT_STEP_PATH_MAP, so _next_key() can return it if Perch
    # ever points there. Labelled so it can never surface as a bare key.
    "enroll_outcome_uncertain":   "Enrollment outcome uncertain - check status",
    "status":                     "Checking status with Perch",
    "unknown_next_step":          "Needs review",
}

# Terminal: reopening these must never offer a restart or re-submit path.
TERMINAL_STEP_KEYS = {"contracts_accepted"}
# Blocked: reopened read-only, with no path onward.
#
# The first two are outcomes awaiting human reconciliation WITH Perch.
# lmi_not_qualified is different in kind: it is a settled local decision, not
# an uncertainty. It is blocked for the same practical reason - a reopened
# enrollment must offer no route to self-attestation, contracts or completion.
BLOCKED_STEP_KEYS = {"contracts_accept_uncertain", "enroll_outcome_uncertain",
                     "lmi_not_qualified"}


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


# ── PERCH COMMIT BOUNDARY ────────────────────────────────────────────────
# Steps that can ONLY be reached after POST /enroll has been accepted.
#
# Perch confirmed (2026-08) that customer information cannot be updated through
# the API after the customer/utility-account step, and that customer_type cannot
# change once an enrollment is in progress. So once the workflow has advanced
# past `enroll`, everything Perch accepted is committed and Dalton must not let
# the rep create divergence.
#
# `enroll` itself is NOT in this set: it means "ready to enroll", i.e. still
# editable. `enroll_outcome_uncertain` IS, because the enrollment MAY exist at
# Perch and the safe assumption is that it does.
POST_ENROLL_STEP_KEYS = frozenset({
    "proof_docs",
    "self_attestation",
    "self_attestation_accept",
    "contracts",
    "contracts_review",
    "contracts_accept",
    "contracts_accepted",
    "contracts_accept_uncertain",
    "status",
    "completed",
    "enroll_outcome_uncertain",
    "unknown_next_step",
})


def perch_committed(enrollment_id):
    """True once Perch has accepted this enrollment's /enroll step.

    Derived from PERSISTED workflow state, so the boundary survives a reload,
    a resume and a reopen from the dashboard - unlike the in-memory frontend
    flag it replaces.
    """
    state = get_state(enrollment_id)
    if not state:
        return False
    return (state["current_step_key"] or "") in POST_ENROLL_STEP_KEYS
