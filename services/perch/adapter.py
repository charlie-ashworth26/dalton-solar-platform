"""
The Perch adapter - the only Perch-aware module routes may import.

Responsibilities:
  1. Acquire a valid enrollment token (delegating expiry to token_manager).
  2. Dispatch to whichever PerchClient the environment selected.
  3. Implement the documented 403 -> refresh -> retry-once behaviour.
  4. Translate the documented 503 into a structured "no capacity" business result.
  5. Persist every request/response/error against the Dalton enrollment ID.
  6. Normalize the documented project_details shape into Dalton's schema.
  7. Record the next_step URL Perch handed back, so the workflow engine can
     resolve what comes next instead of us guessing.
"""
import json
import logging
from datetime import datetime

from db import query, query_one, execute
from services.perch.config import get_perch_client, get_api_mode
from services.perch import token_manager, utilities, markets
# Imported under a distinct name on purpose: several functions here already use
# `capacity` as a local variable for a stored capacity-check row, and a module
# called `capacity` would be shadowed inside exactly the functions that need it.
from services.perch import capacity as cap_module
from services.perch.markets import MarketDataUnavailable
from services import zip_state
# WHICH PRODUCTS a market may offer. Asked, never compared -
# see services/perch/market_products.py.
from services.perch import market_products
from services.perch.client import (
    PATH_CONTRACTS_ACCEPT, PATH_STATUS,
    PATH_CAPACITY, PATH_ENROLL, PATH_LMI_PROOF_DOCS, PATH_CONTRACTS,
    PATH_LMI_SELF_ATTESTATION, PATH_LMI_SELF_ATTESTATION_ACCEPT,
    self_attestation_safe,
    build_enrollment_multipart, build_proof_docs_multipart,
    contracts_safe, redact_contract_urls,
)
from services.perch.errors import (
    PerchError, PerchValidationError, PerchTokenExpiredError, PerchNoCapacityError,
    PerchAmbiguousOutcomeError,
)


_log = logging.getLogger(__name__)


def _json(v):
    return json.dumps(v, default=str) if v is not None else None


def record_api_call(enrollment_id, operation, endpoint, http_method, request_json,
                    response_json, status_code, duration_ms, error_message,
                    initiated_by_user_id):
    """Writes one row to perch_api_calls. Callers redact secrets before calling;
    no Perch credential or token value is ever passed in."""
    cur = execute(
        """INSERT INTO perch_api_calls
           (enrollment_id, operation, http_method, endpoint, request_json, response_json,
            status_code, duration_ms, error_message, api_mode, initiated_by_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, operation, http_method, endpoint, _json(request_json), _json(response_json),
         status_code, duration_ms, error_message, get_api_mode(), initiated_by_user_id),
    )
    return cur.lastrowid


def set_token_email(enrollment_id, email):
    """Persists the customer email used for POST /token and PATCH /refresh_token.
    Must happen before any Perch call - the spec requires it on /token."""
    execute("UPDATE enrollments SET perch_token_email = ?, updated_at = datetime('now') WHERE id = ?",
            (email, enrollment_id))


def check_capacity(enrollment_id, zip_code, utility_input, email=None, user_id=None):
    """POST /capacity through the adapter.

    Implements the documented behaviours exactly:
      403 -> PATCH /refresh_token, retry the SAME request once, then give up.
      503 -> capacity_available: False. Not an error; the rep sees "no capacity
             in this area" and, per Perch, must not proceed to enroll.

    Always calls Perch. Stored checks are audit records, never a read cache -
    Perch enforces these rates at enroll, so a stale local copy is worthless.
    """
    zip_code = (zip_code or "").strip()
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise PerchValidationError("ZIP code must be exactly 5 digits.")

    if email:
        if "@" not in email:
            raise PerchValidationError("Enter a valid email address.")
        set_token_email(enrollment_id, email)

    utility_slug = utilities.resolve_slug(utility_input)
    if not utility_slug:
        # NOT IN THE LOCAL TABLE. Before rejecting, ask the market-rules layer,
        # which is the spec's sanctioned source for utility slugs:
        #
        #   "Rules that differ by state or utility - utility slugs, account
        #    formats ... are not listed in this document. Fetch them from the
        #    GET /markets/* endpoints."
        #
        # GET /markets/utility_account_fields is keyed by slug alone, so it
        # confirms a slug without needing to know its state. A slug Perch
        # publishes fields for is a real slug.
        #
        # This is how a NEW MARKET's utility becomes usable with no seed row and
        # no migration - nothing about Dominion is written down in UBS. The
        # local table remains the fast path, so every NY lookup resolves exactly
        # as before and never reaches this branch.
        candidate = (utility_input or "").strip()
        confirmed = False
        if candidate:
            try:
                confirmed = bool(markets.utility_account_fields(candidate)["fields"])
            except (MarketDataUnavailable, PerchValidationError):
                confirmed = False
        if not confirmed:
            raise PerchValidationError(
                f"'{utility_input}' is not a recognized utility. Perch matches utilities by slug; "
                "pick one from the supported list.")
        utility_slug = candidate

    client = get_perch_client()
    token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
    payload = {"zip_code": zip_code, "utility_name": utility_slug}

    started = datetime.now()
    attempted_refresh = False
    response = None
    no_capacity = False
    error_for_log = None
    status_for_log = 200

    try:
        response = client.check_capacity(token, zip_code, utility_slug)
    except PerchTokenExpiredError:
        # Documented recovery path: refresh, then retry the original request once.
        attempted_refresh = True
        record_api_call(
            enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
            http_method="POST", request_json=payload, response_json=None, status_code=403,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message="403 enrollment_token expired - refreshing and retrying once",
            initiated_by_user_id=user_id,
        )
        token_manager.refresh_existing_token(enrollment_id, user_id=user_id)
        token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
        started = datetime.now()
        try:
            response = client.check_capacity(token, zip_code, utility_slug)
        except PerchNoCapacityError as e:
            no_capacity, status_for_log, error_for_log = True, 503, str(e)
        # A second PerchTokenExpiredError intentionally propagates - retrying
        # forever would mask a real auth problem.
    except PerchNoCapacityError as e:
        no_capacity, status_for_log, error_for_log = True, 503, str(e)
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
            http_method="POST", request_json=payload, response_json=None,
            status_code=getattr(e, "http_status", None),
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise

    duration_ms = int((datetime.now() - started).total_seconds() * 1000)
    api_call_id = record_api_call(
        enrollment_id=enrollment_id, operation="check_capacity", endpoint=PATH_CAPACITY,
        http_method="POST", request_json=payload,
        response_json=(response or {}).get("raw", response),
        status_code=status_for_log, duration_ms=duration_ms, error_message=error_for_log,
        initiated_by_user_id=user_id,
    )

    # project_details is EITHER wire shape here - the v1 flat object or the v2
    # entry array - and is passed through by the client untouched. `or {}` was
    # safe while it was always a dict; an empty LIST is also falsy, so the
    # default is chosen to match what arrived rather than forced to a dict.
    raw_details = (response or {}).get("project_details")
    details = raw_details if raw_details is not None else {}
    next_step_url = (response or {}).get("next_step")

    # One parse, here, for both the flat columns and the entries column, so the
    # two can never disagree about the same response.
    model = cap_module.parse(raw_details)

    execute(
        """INSERT INTO perch_capacity_checks
           (enrollment_id, perch_api_call_id, zip_code, utility_slug, capacity_available,
            residential_capacity_available, small_commercial_capacity_available,
            lmi_capacity_available, proof_documents_required,
            savings_percent_res_commercial, savings_percent_lmi, next_step_url,
            raw_response_json, api_mode, capacity_entries_json, capacity_shape)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (enrollment_id, api_call_id, zip_code, utility_slug, 0 if no_capacity else 1,
         # THE LEGACY FLAT COLUMNS ARE STILL WRITTEN, from the model rather
         # than from the raw body. On a v1 response the model is a faithful
         # synthesis of exactly these fields, so the values are identical to
         # what this statement stored before E2. On a v2 response they are the
         # closest honest equivalent, which keeps every pre-E2 reader of this
         # table working instead of finding NULLs:
         #   small_commercial <- mass_market, the field's own documented meaning
         #   proof_documents_required <- whether lmi_ira capacity exists, which
         #     is precisely what the v1 flag meant ("whether IRA LMI proof
         #     documents will be required later in the enrollment flow")
         # The entries column below is the authoritative record; these are a
         # compatibility surface.
         _b(model.has_capacity(cap_module.RESIDENTIAL)),
         _b(model.has_capacity(cap_module.MASS_MARKET)),
         _b(model.has_capacity(cap_module.LMI)),
         _b(_legacy_proof_docs_flag(model, details)),
         # One number or NULL. A v2 type offering several rates has no single
         # scalar, and picking one (lowest? highest?) would misquote Perch, so
         # it stores NULL and the entries column carries all of them.
         _legacy_savings(model, details, CUSTOMER_TYPE_RESIDENTIAL,
                         cap_module.V1_FIELD_SAVINGS_RES_COMMERCIAL),
         _legacy_savings(model, details, CUSTOMER_TYPE_LMI,
                         cap_module.V1_FIELD_SAVINGS_LMI),
         next_step_url,
         # Store what PERCH actually sent, not our normalized wrapper, so the
         # audit record stays a faithful copy of the wire response.
         _json((response or {}).get("raw", response) if response is not None
               else {"no_capacity": True, "detail": error_for_log}),
         get_api_mode(),
         # The authoritative record from E2 on: every entry, every county
         # restriction, every savings rate.
         _json(model.as_dict()),
         model.shape),
    )

    execute(
        "UPDATE enrollments SET service_zip = ?, utility_name = ?, updated_at = datetime('now') WHERE id = ?",
        (zip_code, utility_slug, enrollment_id),
    )

    return {
        "capacity_available": not no_capacity,
        "zip_code": zip_code,
        "utility_slug": utility_slug,
        # One display-name system: the curated name when the utility is in
        # the local table, a derived clean name when it is not. by_slug()
        # alone returned None for every new market, and the caller then
        # showed the raw slug.
        "utility_display_name": utilities.display_name_for_slug(utility_slug),
        # Perch's own shape, whichever version answered, passed through
        # untouched - this is still the faithful copy.
        "project_details": details if not no_capacity else None,
        # E2: the same facts in UBS's normalized entry model, so a caller does
        # not have to know which version it just talked to.
        "capacity_entries": model.as_dict() if not no_capacity else None,
        "capacity_shape": model.shape,
        "next_step_url": next_step_url,
        "token_was_refreshed": attempted_refresh,
        "api_mode": get_api_mode(),
        "checked_at": datetime.now().isoformat(),
        "message": error_for_log if no_capacity else None,
    }


def _b(v):
    return None if v is None else (1 if v else 0)


# ─────────────── The legacy flat columns ───────────────
# These two helpers exist so a v1 response is persisted BYTE-IDENTICALLY to the
# way it was before E2. The rule for both is the same: on a v1 response, store
# the v1 field verbatim - no derivation, no model, no cleverness - and only on
# a v2 response fall back to the closest honest equivalent.
#
# Deriving these from the model in both cases would have been tidier and was
# the first attempt. It also silently changed the stored value in the ordinary
# NY case (residential=false with mass_market=true), where the v1 savings field
# carries a real rate that a model keyed on the `residential` capacity type
# alone does not see. "Tidier" is not worth a changed number in a table the
# review screen reads.

def _legacy_savings(model, details, customer_type, v1_field):
    """A scalar savings rate for a legacy flat column, or None."""
    if model.shape == cap_module.SHAPE_V1_FLAT:
        return (details or {}).get(v1_field)
    for program in available_programs(model):
        if program["customer_type"] == customer_type:
            # None when the program offers several rates; see the INSERT.
            return program["savings_percent"]
    return None


def _legacy_proof_docs_flag(model, details):
    """A value for the legacy proof_documents_required column.

    v1: the flag Perch sent, verbatim.
    v2: whether lmi_ira capacity exists, which is what the v1 flag meant.
    """
    if model.shape == cap_module.SHAPE_V1_FLAT:
        return (details or {}).get(cap_module.V1_FIELD_PROOF_DOCS)
    return model.has_capacity(cap_module.LMI_IRA)


def latest_capacity_check(enrollment_id):
    """Most recent stored check. AUDIT/RESUME ONLY - never a substitute for a
    fresh call when making an enrollment decision, because Perch enforces the
    live rates at enroll."""
    row = query_one(
        "SELECT * FROM perch_capacity_checks WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1",
        (enrollment_id,),
    )
    if not row:
        return None
    d = dict(row)
    return {
        "capacity_available": bool(d["capacity_available"]),
        "zip_code": d["zip_code"],
        "utility_slug": d["utility_slug"],
        "utility_display_name": utilities.display_name_for_slug(d["utility_slug"]),
        # The legacy flat shape, rebuilt exactly as it was before E2. Kept
        # because pre-E2 readers still exist and because a row written before
        # this migration has nothing else: capacity_entries_json is NULL for
        # every historical row, and capacity_model_for() below falls back to
        # this when it is.
        "project_details": {
            "residential_capacity_available": _ob(d["residential_capacity_available"]),
            "small_commercial_capacity_available": _ob(d["small_commercial_capacity_available"]),
            "lmi_capacity_available": _ob(d["lmi_capacity_available"]),
            "proof_documents_required": _ob(d["proof_documents_required"]),
            "savings_percent_for_residential_and_commercial_customers": d["savings_percent_res_commercial"],
            "savings_percent_for_lmi_customers": d["savings_percent_lmi"],
        } if d["capacity_available"] else None,
        # E2: the authoritative stored capacity, with every entry, county
        # restriction and savings rate. None on a row written before the
        # migration, which is what capacity_model_for() keys off.
        "capacity_entries": (_json_loads(d.get("capacity_entries_json"))
                             if d["capacity_available"] else None),
        "capacity_shape": d.get("capacity_shape"),
        "next_step_url": d["next_step_url"],
        "api_mode": d["api_mode"],
        "checked_at": d["checked_at"],
        "stale": True,
    }


def _json_loads(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def capacity_model_for(check):
    """The CapacityModel for a stored capacity check.

    THE one place the rest of the application turns a stored row back into a
    capacity model, so the fallback below is written once rather than guessed
    at each call site.

    Prefers the stored entries. Falls back to the legacy flat columns when
    there are none - which is the case for every row written before migration
    013, and those rows must keep working: a rep resuming a draft started last
    week has a v1 row and still needs a program list.
    """
    check = check or {}
    stored = check.get("capacity_entries")
    if stored:
        model = cap_module.parse_stored(stored)
    else:
        model = cap_module.parse(check.get("project_details"))
    # E4: record WHICH MARKET this capacity is for, so market-level product
    # rules (which programs may be offered here) apply everywhere the model
    # is used without each of a dozen call sites having to pass it - and so
    # the availability screen, the programs endpoint and the enroll validator
    # cannot end up disagreeing because one of them forgot.
    #
    # Resolved from the ZIP, which is the only market identifier available at
    # availability time; this is the same resolution the Availability step
    # already uses to populate its utility list. An unresolvable ZIP leaves it
    # None, which means "no market-specific restriction" - see
    # markets.income_qualified_only_markets().
    model.market_state = zip_state.state_for_zip(check.get("zip_code"))
    return model


def _ob(v):
    return None if v is None else bool(v)


def api_call_history(enrollment_id):
    rows = query(
        """SELECT id, operation, http_method, endpoint, status_code, duration_ms,
                  error_message, api_mode, created_at
           FROM perch_api_calls WHERE enrollment_id = ? ORDER BY id""",
        (enrollment_id,),
    )
    return [dict(r) for r in rows]


# ─────────────── Enrollment / LMI / contracts bridge ───────────────
# These methods deliberately live in the adapter instead of routes. The browser
# supplies Dalton enrollment data; this layer is responsible for translating it
# into Perch's exact multipart contract, refreshing tokens, and recording a
# URL-free / secret-free audit trail.

# ─────────────── Perch customer_type values ───────────────
# These are the values POST /enroll accepts. They are Perch's vocabulary, used
# on the wire and stored in enrollments.selected_customer_type. They are NEVER
# rendered: PROGRAM_RULES below carries the rep-facing label for each.
CUSTOMER_TYPE_RESIDENTIAL = "Residential"
CUSTOMER_TYPE_BUSINESS = "Business"
CUSTOMER_TYPE_LMI = "LMI"
CUSTOMER_TYPE_LMI_IRA = "LMI_IRA"

# What a rep may choose. Business is modeled below but deliberately NOT
# selectable - see PROGRAM_RULES.
SELECTABLE_CUSTOMER_TYPES = (CUSTOMER_TYPE_RESIDENTIAL, CUSTOMER_TYPE_LMI,
                             CUSTOMER_TYPE_LMI_IRA)

# The income-qualified family. Asked as a question rather than string-compared
# at each site, because `== "LMI"` was true for the only LMI type there used to
# be and is quietly FALSE for LMI_IRA - which would route an IRA enrollment
# down the standard-residential branch.
LMI_CUSTOMER_TYPES = (CUSTOMER_TYPE_LMI, CUSTOMER_TYPE_LMI_IRA)


def is_lmi_customer_type(value):
    """Whether a customer_type is in the income-qualified family."""
    return str(value or "").strip().upper() in {t.upper()
                                                for t in LMI_CUSTOMER_TYPES}


# ─────────────── Capacity type -> UBS program ───────────────
# Straight from the v2 spec's own table, under POST /enroll's customer_type:
#
#   | customer_type | Capacity                          | customer_type of the capacity entries |
#   | Residential   | residential or mass market        | residential |
#   | Business      | mass market or commercial and ind | mass_market |
#   | LMI           | LMI, non-IRA                      | lmi         |
#   | LMI_IRA       | LMI, IRA                          | lmi_ira     |
#
# NOTE THE ASYMMETRY, which is the spec's and not ours: a program may be
# AVAILABLE on either of two capacity types, while its SAVINGS RATES are read
# from one named entry type. Business is the clearest case - it can be enrolled
# into commercial_and_industrial capacity, but its rates come from mass_market.
# Both columns are modeled separately rather than collapsed into one list.
PROGRAM_RULES = {
    CUSTOMER_TYPE_RESIDENTIAL: {
        # Available on residential OR mass market.
        #
        # Perch Engineering, 2026-08: "For the most part our Resi capacity and
        # our small cs capacity sign the same forms and are both accepted
        # within the funnel. For our resi in NY, that's the same as small cs
        # <25kW that is non-demand." and "yes if you see small cs/resi = resi".
        # The v2 spec says the same thing in its own words - the v1 field
        # `small_commercial_capacity_available` is documented as "whether MASS
        # MARKET capacity is available" - so the UBS rule did not change, only
        # the name it is written in.
        #
        # Real staging examples: 12401/Central Hudson and 10901/Orange &
        # Rockland both return residential=false with small_commercial=true,
        # and both are residential enrollments.
        "capacity_types": (cap_module.RESIDENTIAL, cap_module.MASS_MARKET),
        "savings_type": cap_module.RESIDENTIAL,
        "label": "Residential",
        "description": "Standard residential program. "
                       "No eligibility documentation needed.",
        "lmi_required": False,
        "selectable": True,
    },
    CUSTOMER_TYPE_LMI: {
        "capacity_types": (cap_module.LMI,),
        "savings_type": cap_module.LMI,
        "label": "Residential LMI",
        "description": "Income-eligible program.",
        # Decided per response, not here: on v1 by proof_documents_required, on
        # v2 by the capacity type itself. See _lmi_required_for().
        "lmi_required": None,
        "selectable": True,
    },
    CUSTOMER_TYPE_LMI_IRA: {
        "capacity_types": (cap_module.LMI_IRA,),
        "savings_type": cap_module.LMI_IRA,
        "label": "Residential LMI (IRA)",
        "description": "Income-eligible IRA program. "
                       "Requires proof of participation in a qualifying program.",
        # The v2 spec, on the lmi_ira capacity type: "Enrollments into it
        # require proof documents later in the enrollment flow."
        "lmi_required": True,
        "selectable": True,
    },
    CUSTOMER_TYPE_BUSINESS: {
        "capacity_types": (cap_module.MASS_MARKET,
                           cap_module.COMMERCIAL_AND_INDUSTRIAL),
        "savings_type": cap_module.MASS_MARKET,
        "label": "Business",
        "description": "Commercial enrollment.",
        "lmi_required": False,
        # MODELED BUT NOT OFFERED, and this is a deliberate limit rather than
        # an oversight. POST /enroll requires business_name, business_title,
        # business_phone and a separate home_address when customer_type is
        # Business; the UBS wizard collects none of them, so offering the
        # option would produce a guaranteed 422 at enroll time with a rep
        # already three screens in. It is in this table so the capacity model
        # is complete and so the commercial channel is a configuration change
        # later rather than a rewrite.
        "selectable": False,
    },
}

def offerable_customer_types(market_state=None):
    """The programs this market may offer, in the order a rep sees them.

    THE one place the market product rule is applied. available_programs()
    iterates this, and so does the availability panel - which is the point:
    when the panel built its own list from PROGRAM_RULES it kept showing a
    Residential row for Virginia after the cards had correctly stopped
    offering it, which is precisely the UI-and-backend-disagree failure the
    selection rules exist to prevent.

    `market_state` is a two-letter state, or None for "market not known",
    which carries no restriction. See services/perch/market_products.py.
    """
    income_qualified_only = market_products.is_income_qualified_only(market_state)
    return tuple(
        customer_type
        for customer_type in (CUSTOMER_TYPE_RESIDENTIAL, CUSTOMER_TYPE_LMI,
                              CUSTOMER_TYPE_LMI_IRA, CUSTOMER_TYPE_BUSINESS)
        if PROGRAM_RULES[customer_type]["selectable"]
        and not (income_qualified_only and not is_lmi_customer_type(customer_type))
    )


# Rep-facing label per capacity type. Raw Perch vocabulary never reaches a
# screen, and neither does Perch's internal distinction between the two
# capacity types that are both plainly "Residential" to a rep.
#
# E2.1: mass_market used to read "Residential (mass market)". That is the API's
# distinction, not the rep's - Perch Engineering's own rule is "if you see
# small cs/resi = resi", and a rep enrolling on mass-market capacity is filling
# in a residential enrollment either way. Showing them two kinds of Residential
# invites the question "which one do I pick?" when there is nothing to pick.
# These labels now match the UBS PROGRAM names exactly.
CAPACITY_TYPE_LABELS = {
    cap_module.RESIDENTIAL: "Residential",
    cap_module.MASS_MARKET: "Residential",
    cap_module.LMI: "Residential LMI",
    cap_module.LMI_IRA: "Residential LMI (IRA)",
    cap_module.COMMERCIAL_AND_INDUSTRIAL: "Commercial",
}


def capacity_type_label(capacity_type):
    """A rep-facing label for a Perch capacity type.

    An unknown type - one Perch adds after this was written - is softened into
    words rather than printed as a slug, the same rule the error formatter
    follows. `foo_bar` reads as "Foo bar", never as `foo_bar`.
    """
    known = CAPACITY_TYPE_LABELS.get(capacity_type)
    if known:
        return known
    words = str(capacity_type or "").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Other"


def _lmi_required_for(customer_type, model):
    """Whether this program will need proof documents.

    A UI HINT ONLY. Perch's next_step remains the authority on whether the
    proof-doc step is actually requested; this exists so the program card can
    say so before the rep commits, and nothing routes on it.

      v2: the capacity type decides. `lmi_ira` requires proof documents;
          plain `lmi` is the non-IRA type and does not.
      v1: there is no lmi_ira, so the old proof_documents_required flag is
          still the only signal and is still used - absent means True, which
          is what UBS has always done (the safer default: warn, then let Perch
          correct it).
    """
    rule = PROGRAM_RULES.get(customer_type) or {}
    if rule.get("lmi_required") is not None:
        return bool(rule["lmi_required"])
    # Only CUSTOMER_TYPE_LMI reaches here.
    if model.shape == cap_module.SHAPE_V1_FLAT:
        flag = model.v1_proof_documents_required
        return True if flag is None else bool(flag)
    return False


def _program_rates(model, rule, county, include_restricted):
    """The savings rates for one program, and where they came from.

    Returns (rates, source, unresolved, candidates).

    THE RULE IS: READ THE NAMED TYPE, AND ONLY THE NAMED TYPE.

    The v2 spec names exactly one capacity entry type per program as the
    source of its `savings_percent` at enroll. A program may be AVAILABLE on a
    second type while that named type reports no rates - the ordinary NY case
    is residential=false with mass_market=true - and it is tempting to fill the
    gap from the type that does have rates.

    E2 did exactly that. E2.1 removes it, because the substituted number is a
    guess dressed as a quote: Perch has not said that a Residential enrollment
    on mass-market capacity will be priced at the mass-market rate, and
    `savings_percent` is a figure a rep reads out to a customer. When the
    named source is empty the rates stay EMPTY, `savings_unresolved` says so,
    and `savings_candidates` carries the raw arrays of the backing types
    untouched, so the ambiguity is visible to a human reading the record
    instead of resolved by this function on their behalf.

    THE ONE CASE THAT IS NOT A SUBSTITUTION is v1. v1 reports a single field,
    `savings_percent_for_residential_and_commercial_customers`, which its own
    spec says covers residential AND commercial customers. That is one rate
    that genuinely applies to both, carried on the model rather than attached
    to an entry, so reading it for the Residential program is reading the field
    Perch sent - not borrowing another type's.
    """
    savings_type = rule["savings_type"]

    if (model.shape == cap_module.SHAPE_V1_FLAT
            and savings_type in (cap_module.RESIDENTIAL,
                                 cap_module.MASS_MARKET)):
        return (list(model.v1_residential_commercial_savings or []),
                "v1_residential_and_commercial_field", False, [])

    rates = model.savings_percents_for(savings_type, county=county,
                                       include_restricted=include_restricted)
    if rates:
        return rates, savings_type, False, []

    # Empty. Record what the backing types DO offer, without adopting any of
    # it, so the gap can be investigated rather than guessed at.
    candidates = []
    for t in rule["capacity_types"]:
        if t == savings_type:
            continue
        other = model.savings_percents_for(t, county=county,
                                           include_restricted=include_restricted)
        if other and model.has_capacity(t, county=county,
                                        include_restricted=include_restricted):
            candidates.append({"capacity_type": t, "savings_percents": other})
    return [], savings_type, bool(candidates), candidates


def available_programs(details, county=None, include_restricted=True,
                       market_state=None):
    """Programs Perch ACTUALLY returned for this location, in a stable shape.

    The capacity response is the single source of truth. Nothing is inferred:
    a program whose capacity types all came back unavailable never appears
    here, and a savings rate Perch did not send stays ABSENT rather than being
    substituted from somewhere else - see _program_rates().

    `details` may be EITHER wire shape - the v1 flat object or the v2 entry
    array - or an already-parsed CapacityModel. capacity.parse() reconciles
    them, so there is one code path from here down.

    Each program carries:
        customer_type      the value POST /enroll takes
        label              what the rep reads; never Perch vocabulary
        description        one line for the program card
        savings_percents   EVERY rate on offer, sorted ascending, never
                           collapsed, and never borrowed from another type
        savings_percent    the single rate when there is exactly one, else None
                           (kept so pre-E2 callers keep working unchanged)
        savings_source     which capacity type (or v1 field) the rates came from
        savings_unresolved True when the named source published no rate while a
                           backing type did - an ambiguity, stated not resolved
        savings_candidates the raw arrays of those backing types, untouched
        capacity_types     the capacity types backing this program, with kW
        lmi_required       UI hint; Perch's next_step still decides
        county_restricted  whether any backing entry is county-restricted

    COUNTY (E2.1). The two arguments answer two different questions:

      county="Albany"            what THIS customer can be offered: the
                                 unrestricted entries plus the Albany ones.
      include_restricted=True    what this LOCATION can offer ANYONE, before a
                                 county is known. The availability screen asks
                                 this, because hiding restricted capacity would
                                 tell a rep in a restricted county that nothing
                                 is available when something is.
      include_restricted=False   what Perch will accept with NO county sent:
                                 unrestricted entries only.

    county_decides_options() compares the last two to decide whether the rep
    needs to be asked at all.

    MARKET (E4). `market_state` is the two-letter state this capacity belongs
    to. It is normally taken from the model, which capacity_model_for()
    annotates at hydration, so callers do not pass it; the argument exists so
    the rule can be exercised directly and so a caller holding a raw
    project_details dict can still state the market. A market UBS cannot
    identify carries no restriction.
    """
    model = (details if isinstance(details, cap_module.CapacityModel)
             else cap_module.parse(details))
    programs = []

    # E4 - MARKET PRODUCT RULE, proven necessary by real staging.
    #
    # Dominion / ZIP 22401 returns mass_market AVAILABLE and residential
    # unavailable. The v2 spec allows a Residential customer onto mass-market
    # capacity, so the generic mapping offered a Residential program with no
    # rate of its own - correct about Perch, wrong about our product: Virginia
    # is state LMI and a rep must not be offered a non-LMI enrollment there.
    #
    # This restricts WHICH PROGRAMS ARE OFFERED. It does not touch the
    # capacity model, invent a savings rate, or change what mass-market
    # capacity means anywhere else - New York's Central Hudson and Orange &
    # Rockland still map mass market to a standard residential enrollment.
    market = market_state or getattr(model, "market_state", None)

    for customer_type in offerable_customer_types(market):
        rule = PROGRAM_RULES[customer_type]

        backing = [t for t in rule["capacity_types"]
                   if model.has_capacity(t, county=county,
                                         include_restricted=include_restricted)]
        if not backing:
            continue

        rates, savings_source, unresolved, candidates = _program_rates(
            model, rule, county, include_restricted)

        if unresolved:
            # Logged, not swallowed. This is the signal that Perch's capacity
            # for this ZIP/utility does not price a program UBS is about to
            # offer, which is worth a human look rather than a silent blank.
            _log.info(
                "perch capacity: no savings rate published for program %s "
                "(source=%s, backing=%s, candidates=%s) - rates left empty "
                "rather than substituted",
                customer_type, savings_source, backing, candidates)

        programs.append({
            "customer_type": customer_type,
            "label": rule["label"],
            "description": rule["description"],
            "savings_percents": rates,
            # Exactly one rate collapses to the old scalar, so every pre-E2
            # caller and screen behaves as it did. Several rates leave it None
            # on purpose: there is no single number to show, and inventing one
            # (the lowest, the highest, an average) would misquote Perch.
            "savings_percent": rates[0] if len(rates) == 1 else None,
            "savings_source": savings_source,
            "savings_unresolved": unresolved,
            "savings_candidates": candidates,
            "capacity_types": [{
                "capacity_type": t,
                "label": capacity_type_label(t),
                "capacity_available_kw": model.kw_for(
                    t, county=county, include_restricted=include_restricted),
                "counties": model.counties_for(t),
            } for t in backing],
            "lmi_required": _lmi_required_for(customer_type, model),
            "county_restricted": any(
                e["county_restriction"]
                for t in backing
                for e in model.entries_for(t, county=county,
                                           include_restricted=True)),
        })

    return programs


def program_for(details, customer_type, county=None):
    """The one offered program matching a customer_type, or None."""
    for p in available_programs(details, county=county):
        if p["customer_type"] == customer_type:
            return p
    return None


# Raised when /enroll cannot be called because the rep still has a decision to
# make. Deliberately NOT a PerchError: nothing has gone wrong with Perch, and
# nothing has been sent. It is a PerchValidationError so every existing
# handler already turns it into a 400 with a rep-facing message.
SAVINGS_SELECTION_REQUIRED = (
    "This program offers more than one savings rate. Choose the rate for "
    "this customer before submitting the enrollment.")


def resolve_savings_percent(details, customer_type, county=None,
                            selected=None):
    """The savings_percent to send to POST /enroll, or None to omit it.

    Returns (value_or_None, reason). Raises PerchValidationError when the rep
    still has to choose.

    THE SPEC'S THREE CASES, AND NOTHING ELSE:

      one rate applies   -> `savings_percent` is OPTIONAL and Perch will use
                            that rate anyway. UBS sends it explicitly, because
                            sending the number it showed the rep is how the
                            screen and the enrollment are kept honest - and
                            because the spec says that if you do send it, it
                            must match, which it does by construction.
      several apply      -> REQUIRED. "A missing value returns 422 on
                            savings_percent with code `required`." UBS stops
                            BEFORE calling Perch rather than spending a round
                            trip to be told what it already knows.
      none published     -> omitted. There is no authoritative rate, so there
                            is nothing to send and nothing to invent. E2.1
                            already refuses to substitute one.

    A rate the rep chose earlier is re-validated against the rates available
    NOW, for THIS county. Capacity moves, and a stored rate that Perch no
    longer offers must not be sent just because it is on the record.
    """
    program = program_for(details, customer_type, county=county)
    if program is None:
        # The caller validates the customer_type separately and will have
        # raised already; omitting is the safe answer either way.
        return None, "no_matching_program"

    rates = program["savings_percents"]

    if not rates:
        return None, "no_rate_published"

    if selected is not None:
        try:
            chosen = float(selected)
        except (TypeError, ValueError):
            raise PerchValidationError(SAVINGS_SELECTION_REQUIRED)
        if chosen in rates:
            return chosen, f"selected={chosen}"
        # NOT SUBSTITUTED, however few rates remain. An earlier draft replaced
        # a stale choice with the only rate still on offer, on the grounds
        # that one rate is unambiguous. It is not UBS's to replace: the rep
        # chose a number, and quietly enrolling the customer at a different
        # one is precisely the silent switch the selection rules forbid - and
        # it is a number the rep may already have said out loud.
        #
        # In practice this is rare: changing the program or the county CLEARS
        # a rate that no longer applies (routes._clear_rate_if_unavailable),
        # so reaching here means the capacity itself moved underneath a
        # standing choice. Stopping is the right answer to that.
        raise PerchValidationError(
            "That savings rate is no longer available for this customer. "
            "Choose one of the rates shown before submitting.")

    if len(rates) == 1:
        return rates[0], f"only_rate={rates[0]}"

    raise PerchValidationError(SAVINGS_SELECTION_REQUIRED)


def _program_signature(programs):
    """What a rep would actually see, reduced to something comparable."""
    return tuple(sorted((p["customer_type"], tuple(p["savings_percents"]))
                        for p in programs))


def county_decides_options(details):
    """Does the customer's county change what this location can offer?

    True when the county-restricted entries add something the unrestricted
    ones do not - a program, or a savings rate on a program. That is exactly
    when the rep has to be asked, and asking only then is what keeps the
    Availability screen unchanged for every location where county is
    irrelevant, which is most of them.

    Compared on what a rep would SEE (programs and their rates), not on
    whether restrictions exist at all: a location whose restricted entries
    offer nothing beyond the unrestricted ones is a location where the answer
    cannot matter, and asking there would be a question with no consequence.
    """
    model = (details if isinstance(details, cap_module.CapacityModel)
             else cap_module.parse(details))
    if not model.has_county_restrictions():
        return False
    unrestricted = _program_signature(
        available_programs(model, include_restricted=False))
    everything = _program_signature(
        available_programs(model, include_restricted=True))
    return unrestricted != everything


def _percent(value):
    """Numeric savings percent, or None. Never a default."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def program_label(customer_type):
    """The rep-facing label for a Perch customer_type.

    Every message a rep can read goes through here. `LMI_IRA` is a wire value,
    not English, and must never appear on a screen or in an error.
    """
    rule = PROGRAM_RULES.get(customer_type)
    if rule:
        return rule["label"]
    for key, r in PROGRAM_RULES.items():
        if key.lower() == str(customer_type or "").strip().lower():
            return r["label"]
    words = str(customer_type or "").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "that program"


def _label_list(customer_types):
    return ", ".join(program_label(t) for t in customer_types)


def resolve_customer_type(details, requested=None, county=None):
    """Decide the customer_type for POST /enroll.

    (customer_type, reason) or raises PerchValidationError.

    ONE available program  -> selected automatically; there is no ambiguity.
    SEVERAL available      -> an explicit, VALID selection is required. Dalton
                              used to prefer LMI here, which silently denied the
                              rep the Residential option and could enroll a
                              customer in the wrong program.
    Anything requested     -> validated against the capacity response, so a
                              tampered client cannot select a program Perch did
                              not offer for this location.

    E2: there may now be up to three programs rather than two, and LMI and
    LMI_IRA are distinct - a request for one is NOT satisfied by the other.
    Rep-facing messages name programs by LABEL; the canonical wire value is
    what gets returned and sent.
    """
    programs = available_programs(details, county=county)
    offered = [p["customer_type"] for p in programs]
    requested = (requested or "").strip() or None

    if requested:
        # Case-insensitive match, but the CANONICAL value is what gets sent.
        match = next((t for t in offered if t.lower() == requested.lower()), None)
        if match:
            return match, f"explicit_selection={match}"
        if requested.lower() not in (t.lower() for t in SELECTABLE_CUSTOMER_TYPES):
            raise PerchValidationError(
                f"{program_label(requested)} is not a program this enrollment "
                f"can use. Choose one of: "
                f"{_label_list(SELECTABLE_CUSTOMER_TYPES)}.")
        raise PerchValidationError(
            f"{program_label(requested)} is not available for this location. "
            + (f"Available here: {_label_list(offered)}." if offered
               else "No eligible capacity was returned for this location."))

    if len(programs) == 1:
        only = programs[0]["customer_type"]
        return only, f"only_available_option={only}"

    if len(programs) > 1:
        raise PerchValidationError(
            "This location has more than one available program "
            f"({_label_list(offered)}). Select which one this customer is "
            "enrolling in before continuing.")

    # Mass-market capacity is handled ABOVE as standard residential, so
    # reaching here means Perch returned no usable capacity at all for this
    # location - or only capacity on a type UBS does not offer, such as
    # commercial and industrial.
    raise PerchValidationError(
        "No eligible residential or income-qualified capacity is available "
        "for this enrollment.")


def choose_customer_type(details):
    """Backwards-compatible wrapper. Callers that pass no explicit selection get
    the same automatic behaviour for unambiguous capacity, and now a clear error
    instead of a silent LMI preference when both are available."""
    return resolve_customer_type(details, requested=None)


def _rewind_files(files):
    for value in (files or {}).values():
        if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "seek"):
            try:
                value[1].seek(0)
            except Exception:
                pass


def _close_files(files):
    for value in (files or {}).values():
        if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "close"):
            try:
                value[1].close()
            except Exception:
                pass


def _call_with_refresh(enrollment_id, user_id, operation, endpoint, method,
                       request_summary, call, files=None):
    """Call one enrollment-session endpoint with the documented retry-once path."""
    client = get_perch_client()
    token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
    started = datetime.now()
    refreshed = False
    try:
        result = call(client, token)
    except PerchTokenExpiredError:
        record_api_call(
            enrollment_id=enrollment_id, operation=operation, endpoint=endpoint,
            http_method=method, request_json=request_summary, response_json=None,
            status_code=403,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            error_message="403 enrollment_token expired - refreshing and retrying once",
            initiated_by_user_id=user_id,
        )
        refreshed = True
        token_manager.refresh_existing_token(enrollment_id, user_id=user_id)
        token = token_manager.get_valid_token(enrollment_id, user_id=user_id)
        _rewind_files(files)
        started = datetime.now()
        result = call(client, token)
    return result, refreshed, int((datetime.now() - started).total_seconds() * 1000)


def create_enrollment(enrollment_id, enrollment_payload, user_id=None):
    """POST /enroll using already-captured Dalton data and the stored utility bill."""
    capacity = latest_capacity_check(enrollment_id)
    if not capacity or not capacity.get("capacity_available"):
        raise PerchValidationError("A successful Perch capacity check is required before enrollment.")

    # E2: validated against the stored ENTRY MODEL rather than the flat
    # columns, so a v2 capacity check is checked against what v2 actually
    # returned. capacity_model_for() falls back to the flat columns for a row
    # written before migration 013, so a draft started before this release
    # still enrolls.
    model = capacity_model_for(capacity)
    # The selection is validated against THIS enrollment's capacity response, so
    # a client cannot submit a program Perch did not offer here.
    #
    # E2.1: validated against the SAME county the rep was shown the options
    # under, read from the enrollment. Validating without it would refuse a
    # program the rep legitimately selected on county-restricted capacity -
    # UBS rejecting its own screen.
    #
    # E3: and that county is now SENT, which is what makes a county-restricted
    # enrollment actually work. Perch's rule: "If a project is restricted to
    # this county, Perch enrolls the customer into it. [...] If you do not
    # send it, only projects with no county restriction can be used." Until
    # E3 a rep could select a county-restricted program and have Perch refuse
    # it; now the same county decides both sides.
    requested_type = (enrollment_payload or {}).get("customer_type")
    decided = query_one(
        "SELECT service_county, selected_savings_percent FROM enrollments "
        "WHERE id = ?", (enrollment_id,))
    county = ((decided["service_county"] if decided else "") or "").strip() or None
    selected_rate = decided["selected_savings_percent"] if decided else None

    customer_type, reason = resolve_customer_type(
        model, requested=requested_type, county=county)

    # E3: the savings rate, re-validated against what is offered NOW for this
    # customer_type and county. Raises when the rep still has to choose, which
    # stops UBS BEFORE the Perch call rather than spending a round trip to be
    # told something it already knew.
    savings_percent, savings_reason = resolve_savings_percent(
        model, customer_type, county=county, selected=selected_rate)

    payload = dict(enrollment_payload or {})
    payload["customer_type"] = customer_type
    payload["utility_name"] = capacity["utility_slug"]
    payload["zip_code"] = capacity["zip_code"]

    # VERSION-GATED. v1 has no `county` field on /enroll and ignores
    # `savings_percent` outright ("Ignored when the project was already
    # selected by the deprecated POST /v1/enrollments/capacity"), so a v1
    # enrollment's payload stays byte-identical to what it was before E3.
    # The gate is the capacity SHAPE, not the environment flag: what matters
    # is which API produced the capacity this enrollment is being validated
    # against, and a flag flipped mid-enrollment must not change the payload
    # for a session that was opened on v1.
    if model.shape == cap_module.SHAPE_V2_ENTRIES:
        if county:
            payload["county"] = county
        if savings_percent is not None:
            payload["savings_percent"] = savings_percent
    else:
        savings_reason = "v1_ignores_savings_percent"

    stored = query_one("SELECT perch_token_email FROM enrollments WHERE id = ?", (enrollment_id,))
    token_email = (stored["perch_token_email"] if stored else None) or ""
    if not payload.get("email_address") or payload["email_address"].strip().lower() != token_email.strip().lower():
        raise PerchValidationError(
            "Customer email must match the email used to open this Perch enrollment session.")

    accounts = payload.get("utility_accounts") or []
    if len(accounts) != 1:
        raise PerchValidationError("The current Dalton GUI supports exactly one utility account per enrollment.")
    acct = accounts[0]
    utility_row = utilities.by_slug(capacity["utility_slug"]) or {}
    expected_len = utility_row.get("account_number_length")
    account_number = str(acct.get("utility_account_number") or "").strip()
    if expected_len and (not account_number.isdigit() or len(account_number) != int(expected_len)):
        raise PerchValidationError(
            f"{utility_row.get('display_name') or capacity['utility_slug']} account numbers must be {expected_len} digits.")
    pod_error = utilities.validate_pod_id(capacity["utility_slug"], acct.get("secondary_account_identifier"))
    if pod_error:
        raise PerchValidationError(pod_error)

    # MARKET-PUBLISHED FIELD RULES, layered on top of the checks above.
    #
    # The two checks above are the original NY rules: an exact account-number
    # length from perch_utilities, and the POD ID rule. They run first and are
    # unchanged, so every NY enrollment is validated exactly as before.
    #
    # This block adds the regex patterns Perch publishes per utility, which can
    # express things the local model cannot - Dominion accepts EITHER 10 or 12
    # digits, and requires a meter number. Nothing is hardcoded per utility:
    # the rules are read for whichever slug this enrollment uses.
    #
    # Deliberately ADVISORY-IF-UNAVAILABLE. If market rules cannot be fetched,
    # validation falls back to the checks above and Perch remains the authority
    # - it validates these fields itself and returns a field-level 422. A
    # market-data outage must not block an enrollment that would otherwise be
    # accepted.
    try:
        for field_type, value in (
            ("account_number", account_number),
            ("secondary_account_identifier", acct.get("secondary_account_identifier")),
            ("meter_number", (acct.get("meter_numbers") or [None])[0]),
        ):
            message = markets.validate_account_value(
                capacity["utility_slug"], field_type, value)
            if message:
                raise PerchValidationError(message)
    except MarketDataUnavailable:
        pass

    form, files = build_enrollment_multipart(payload)
    request_summary = {
        "customer_type": customer_type,
        "customer_type_reason": reason,
        "utility_name": capacity["utility_slug"],
        "zip_code": capacity["zip_code"],
        # E3: the two new decisions, and WHY each landed where it did, so the
        # audit record explains an enrollment rather than just recording it.
        # `county` is a place name and `savings_percent` a published rate -
        # neither is a secret, and both are exactly what a later question
        # about "why was this customer put on that project" needs.
        "county": payload.get("county"),
        "savings_percent": payload.get("savings_percent"),
        "savings_percent_reason": savings_reason,
        "capacity_shape": model.shape,
        "utility_account_count": len(accounts),
        "utility_bill_count": len(acct.get("utility_bills") or []),
        "account_last4": account_number[-4:] if account_number else None,
    }
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "create_enrollment", PATH_ENROLL, "POST",
            request_summary,
            lambda client, token: client.create_enrollment(token, form, files),
            files=files,
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="create_enrollment", endpoint=PATH_ENROLL,
            http_method="POST", request_json=request_summary,
            response_json={"next_step": result.get("next_step"), "response_shape": result.get("response_shape")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "customer_type": customer_type,
            "next_step_url": result.get("next_step"),
            "response_shape": result.get("response_shape"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="create_enrollment", endpoint=PATH_ENROLL,
            http_method="POST", request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
    finally:
        _close_files(files)


def submit_proof_docs(enrollment_id, utility_account_number, documents, user_id=None):
    """POST /lmi/proof_docs using a stored Dalton document path and explicit metadata."""
    files, proof_doc = build_proof_docs_multipart(utility_account_number, documents)
    safe_meta = {
        "utility_account_last4": str(utility_account_number)[-4:],
        "documents": [
            {k: d.get(k) for k in ("source_type", "name_on_document", "relationship", "document_type")}
            for d in documents
        ],
    }
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "submit_proof_docs", PATH_LMI_PROOF_DOCS, "POST",
            safe_meta,
            lambda client, token: client.submit_proof_docs(token, files),
            files=files,
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_proof_docs", endpoint=PATH_LMI_PROOF_DOCS,
            http_method="POST", request_json=safe_meta,
            response_json={"next_step": result.get("next_step"), "response_shape": result.get("response_shape")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "next_step_url": result.get("next_step"),
            "response_shape": result.get("response_shape"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_proof_docs", endpoint=PATH_LMI_PROOF_DOCS,
            http_method="POST", request_json=safe_meta, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
    finally:
        _close_files(files)


# The only value either market accepts in lmi_source_selection. Confirmed for
# NY and VA alike, and `single_source_type` is true in both, so it is sent as a
# one-element list.
SELF_ATTESTATION_SOURCE = "self_attestation_qualifying_income"
# The enum of values Perch defines for `status` WHEN a market uses the field at
# all. This is not the decision of whether to send it - see below.
SELF_ATTESTATION_STATUSES = ("accepted", "rejected")


def _enrolled_county(enrollment_id):
    """(stored_county, was_sent_at_enroll) for this enrollment.

    `was_sent_at_enroll` is true only when BOTH hold: a county is stored, and
    the enrollment was validated against a v2 capacity response - because v1's
    /enroll has no county field, so a county stored on a v1 enrollment never
    reached Perch and the self-attestation must still carry it.

    Read from the stored capacity SHAPE rather than the current environment
    flag, for the same reason create_enrollment does: a flag flipped between
    the enroll call and this one must not change what UBS believes it sent.
    """
    row = query_one("SELECT service_county FROM enrollments WHERE id = ?",
                    (enrollment_id,))
    stored = ((row["service_county"] if row else "") or "").strip() or None
    if not stored:
        return None, False
    check = latest_capacity_check(enrollment_id)
    shape = capacity_model_for(check).shape if check else None
    return stored, shape == cap_module.SHAPE_V2_ENTRIES


def submit_self_attestation(enrollment_id, utility_account_number, occupancy,
                            county, status=None, user_id=None):
    """POST /lmi/self_attestation.

    Sends only the documented fields. The returned presigned URL is handed back
    to the immediate caller only - the audit record stores the URL-free view.

    `status` IS MARKET-DEPENDENT and is omitted entirely when None.

        NY  - self_attestation_options returns status ["accepted", "rejected"],
              so the caller passes one and it is sent, exactly as before.
        VA  - status is [], and the spec says: "Do not send `status`: it is
              ignored, and cannot be used to skip this step or signal that the
              customer does not qualify." So the caller passes None and the key
              is absent from the payload - not null, not empty string, ABSENT.

    The caller decides, from the market's own options response, whether a status
    applies. This function does not compare state names; it only enforces that a
    status, IF supplied, is one Perch defines.
    """
    # E3 - THE COUNTY IS SENT ONLY WHEN PERCH HAS NONE.
    #
    # v2 spec, on the self-attestation entry's county: "The county stored for
    # the customer at POST /v2/enrollments/enroll is used, and TAKES
    # PRECEDENCE over a `county` sent here. Send `county` only when none was
    # stored, which is the case when `county` was not sent at enroll."
    #
    # So once E3 has sent a county at enroll, repeating it here is at best
    # ignored and at worst misleading - the record would show UBS asserting a
    # county that had no effect. It is omitted instead.
    #
    # v1 is UNCHANGED: its entry schema marks county REQUIRED, so it is always
    # sent, and the NY and VA flows behave exactly as they did.
    stored_county, enrolled_with_county = _enrolled_county(enrollment_id)
    send_county = not enrolled_with_county

    entry = {
        "utility_account_number": str(utility_account_number),
        "occupancy": int(occupancy),
        "lmi_source_selection": [SELF_ATTESTATION_SOURCE],
    }
    if send_county:
        entry["county"] = county
    elif stored_county and county and \
            stored_county.strip().lower() != str(county).strip().lower():
        # E3.1 - A HARD STOP, NOT A WARNING.
        #
        # Reaching here means UBS is about to attest against one county while
        # Perch holds another for the same enrollment: the income limits were
        # read for the attested county, and Perch will apply the stored one.
        # Both halves look correct on their own, which is exactly what makes
        # the mismatch dangerous - it is an eligibility decision made against
        # the wrong table, and nothing downstream would notice.
        #
        # An earlier draft logged this and carried on. A log is a record of
        # the mismatch, not a defence against it. The route already binds the
        # attestation to the enrolled county (routes/perch_routes.py,
        # _authoritative_county), so this is unreachable through the UI and
        # exists to catch a caller that bypasses it - legacy state, a
        # half-migrated record, a future code path. Nothing is sent.
        raise PerchValidationError(
            f"This enrollment was submitted for {stored_county}, so the "
            "eligibility check has to use that county. Reload this page to "
            "continue.")

    if status is not None:
        if status not in SELF_ATTESTATION_STATUSES:
            raise PerchValidationError(
                "Self-attestation status must be 'accepted' or 'rejected'.")
        entry["status"] = status
    safe_meta = {
        "utility_account_last4": str(utility_account_number)[-4:],
        "occupancy": entry["occupancy"],
        "county": county,
        # Which county Perch will actually use, and whether UBS sent one.
        "county_sent": send_county,
        "county_stored_at_enroll": stored_county,
        # Mirrors the payload: when no status was sent, the ledger records that
        # rather than implying one was.
        "status": entry.get("status"),
        "status_sent": "status" in entry,
        "lmi_source_selection": entry["lmi_source_selection"],
    }
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "submit_self_attestation",
            PATH_LMI_SELF_ATTESTATION, "POST", safe_meta,
            lambda client, token: client.submit_self_attestation(token, entry),
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_self_attestation",
            endpoint=PATH_LMI_SELF_ATTESTATION, http_method="POST",
            request_json=safe_meta,
            # URL-free by construction.
            response_json={"next_step": result.get("next_step_url"),
                           **self_attestation_safe(result)},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "next_step_url": result.get("next_step_url"),
            "document_available": result.get("document_available"),
            # Raw is for the immediate response only; callers must not persist it.
            "raw": result.get("raw"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="submit_self_attestation",
            endpoint=PATH_LMI_SELF_ATTESTATION, http_method="POST",
            request_json=safe_meta, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise


def accept_self_attestation(enrollment_id, metadata, user_id=None):
    """POST /lmi/self_attestation/accept with the documented metadata only."""
    entry = {
        "timestamp": metadata.get("timestamp"),
        "ip_address": metadata.get("ip_address"),
        "user_agent": metadata.get("user_agent"),
    }
    safe_meta = {"timestamp": entry["timestamp"], "ip_address": entry["ip_address"],
                 "user_agent": (entry["user_agent"] or "")[:120]}
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "accept_self_attestation",
            PATH_LMI_SELF_ATTESTATION_ACCEPT, "POST", safe_meta,
            lambda client, token: client.accept_self_attestation(token, entry),
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="accept_self_attestation",
            endpoint=PATH_LMI_SELF_ATTESTATION_ACCEPT, http_method="POST",
            request_json=safe_meta,
            response_json={"next_step": result.get("next_step_url"),
                           "message": result.get("message")},
            status_code=202, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        return {
            "next_step_url": result.get("next_step_url"),
            "message": result.get("message"),
            "token_was_refreshed": refreshed,
        }
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="accept_self_attestation",
            endpoint=PATH_LMI_SELF_ATTESTATION_ACCEPT, http_method="POST",
            request_json=safe_meta, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise


def generate_contracts(enrollment_id, user_id=None):
    """POST /contracts. Returns raw URLs only to the immediate server caller.

    The API-call audit stores the URL-free normalized contract list. Nothing in
    this function persists or logs a presigned URL.
    """
    request_summary = {"body": None}
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "generate_contracts", PATH_CONTRACTS, "POST",
            request_summary,
            lambda client, token: client.generate_contracts(token),
        )
        safe = contracts_safe(result)
        record_api_call(
            enrollment_id=enrollment_id, operation="generate_contracts", endpoint=PATH_CONTRACTS,
            http_method="POST", request_json=request_summary,
            response_json={"contracts": safe, "next_step": result.get("next_step")},
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        result["token_was_refreshed"] = refreshed
        return result
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="generate_contracts", endpoint=PATH_CONTRACTS,
            http_method="POST", request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise


def accept_contracts(enrollment_id, metadata, user_id=None):
    """POST /contracts/accept.

    NOT routed through _call_with_refresh's generic path for ambiguous failures.
    A 403 is a definite rejection, so refresh-and-retry-once is safe and is
    applied. Anything ambiguous (transport failure, timeout, 5xx) propagates as
    PerchAmbiguousOutcomeError and is NEVER retried, because Perch has not
    documented whether this endpoint is idempotent.

    The metadata is audit data (IP / user-agent / timestamp), not a secret, so
    it is recorded. No presigned URL or token is touched by this function.
    """
    request_summary = {"metadata": metadata}
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "accept_contracts", PATH_CONTRACTS_ACCEPT, "POST",
            request_summary,
            lambda client, token: client.accept_contracts(token, metadata),
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="accept_contracts",
            endpoint=PATH_CONTRACTS_ACCEPT, http_method="POST",
            request_json=request_summary,
            response_json={"message": result.get("message")},
            status_code=202, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        result["token_was_refreshed"] = refreshed
        return result
    except PerchAmbiguousOutcomeError as e:
        # Record loudly: this is the state a human must reconcile.
        record_api_call(
            enrollment_id=enrollment_id, operation="accept_contracts",
            endpoint=PATH_CONTRACTS_ACCEPT, http_method="POST",
            request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=f"AMBIGUOUS - acceptance may or may not have been recorded: {e}",
            initiated_by_user_id=user_id,
        )
        raise
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="accept_contracts",
            endpoint=PATH_CONTRACTS_ACCEPT, http_method="POST",
            request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise


def get_status(enrollment_id, user_id=None):
    """GET /status. Side-effect free per the spec, so the standard
    refresh-and-retry-once path is safe here."""
    request_summary = {"body": None}
    try:
        result, refreshed, duration_ms = _call_with_refresh(
            enrollment_id, user_id, "get_status", PATH_STATUS, "GET",
            request_summary,
            lambda client, token: client.get_status(token),
        )
        record_api_call(
            enrollment_id=enrollment_id, operation="get_status", endpoint=PATH_STATUS,
            http_method="GET", request_json=request_summary,
            response_json={
                "completed_steps": result.get("completed_steps"),
                "remaining_steps": result.get("remaining_steps"),
                "completed": result.get("completed"),
                "next_step": result.get("next_step"),
            },
            status_code=200, duration_ms=duration_ms, error_message=None,
            initiated_by_user_id=user_id,
        )
        result["token_was_refreshed"] = refreshed
        return result
    except PerchError as e:
        record_api_call(
            enrollment_id=enrollment_id, operation="get_status", endpoint=PATH_STATUS,
            http_method="GET", request_json=request_summary, response_json=None,
            status_code=getattr(e, "http_status", None), duration_ms=0,
            error_message=str(e), initiated_by_user_id=user_id,
        )
        raise
