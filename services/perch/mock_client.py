"""
Mock Perch client - rewritten in Milestone 2 to emit EXACTLY the documented
schemas.

Fidelity rules this mock follows:
  * The 200 capacity body has precisely the six documented project_details
    fields and a next_step URL. No extra fields, no invented ones.
  * next_step is a URL (the documented .../enrollments/enroll), never an enum.
  * No open capacity raises PerchNoCapacityError, mirroring the documented 503.
  * An expired token raises PerchTokenExpiredError, mirroring the documented 403,
    so the adapter's refresh-and-retry path is exercised for real.
  * Tokens are UUIDs, matching the documented example format
    (550e8400-e29b-41d4-a716-446655440000).
  * The mock enforces the 1-HOUR TTL itself (NEW YAML), so token expiry is
    genuinely tested rather than simulated only by clock manipulation.
  * Re-requesting a token for an email that already has an in-progress
    enrollment raises PerchEnrollmentInProgressError, mirroring the documented
    422 - so the resume-via-refresh path is exercised for real.

Fixtures are keyed by ZIP so tests can assert exact values. The catalog is a
TEST FIXTURE, not a mirror of Perch's real projects - Perch has ~3,400 NY
projects with priority stacking and we deliberately do not model them.
"""
import uuid
from datetime import datetime, timedelta

from services.perch.client import (
    PerchClient, TOKEN_TTL_SECONDS, normalize_capacity_response,
    normalize_enroll_response,
)
# The version flag, read from the environment per call. config.py imports this
# module for the factory, so this is imported from the version module directly
# rather than from config, which would be a cycle.
from services.perch.api_version import get_api_version, API_V2
from services.perch.errors import (
    PerchValidationError, PerchUnavailableError,
    PerchNoCapacityError, PerchTokenExpiredError, PerchNotFoundError,
    PerchEnrollmentInProgressError, PerchNotImplementedError,
)

DOCUMENTED_NEXT_STEP_ENROLL = "https://api.perchenergy.com/affiliate_partners/v1/enrollments/enroll"
DOCUMENTED_NEXT_STEP_CAPACITY = "https://api.perchenergy.com/affiliate_partners/v1/enrollments/capacity"

# v2 returns a BARE PATH, not a URL. Spec, on next_step: "Path of the next
# endpoint to call (`/enroll`)."
V2_NEXT_STEP_ENROLL = "/enroll"

# ZIP prefix -> the project_details Perch would return for that service area.
# Field names and value types are exactly as published.
_FIXTURES = {
    # ── Fixtures mirroring REAL Perch staging responses, so the small-CS
    # mapping is exercised against shapes we have actually observed rather
    # than invented ones. Values transcribed from live staging, 2026-08.
    "124": {  # 12401 Kingston / Central Hudson - small CS only, NO LMI.
              # Perch: "if you see small cs/resi = resi", so this is a
              # standard Residential enrollment at the res/commercial rate.
        "utility_slug": "central-hudson-gas-electric",
        "project_details": {
            "residential_capacity_available": False,
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": False,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 5,
            # Present but MUST NOT be used for Residential.
            "savings_percent_for_lmi_customers": 10,
        },
    },
    "109": {  # 10901 Suffern / Orange & Rockland - small CS + LMI, so BOTH
              # programs are offered and the rep must choose.
        "utility_slug": "orange-and-rockland",
        "project_details": {
            "residential_capacity_available": False,
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 5,
            "savings_percent_for_lmi_customers": 20,
        },
    },
    "129": {  # 12901 / NYSEG - LMI only.
        "utility_slug": "nyseg",
        "project_details": {
            "residential_capacity_available": False,
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 0,
            "savings_percent_for_lmi_customers": 20,
        },
    },
    "133": {  # Upstate NY - full LMI + residential, IRA project requiring proof docs
        "utility_slug": "national-grid-ny",
        "project_details": {
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 10,
            "savings_percent_for_lmi_customers": 20,
        },
    },
    "120": {  # Capital Region - residential only, no LMI capacity left
        "utility_slug": "national-grid-ny",
        "project_details": {
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": False,
            "residential_capacity_available": True,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 8,
            "savings_percent_for_lmi_customers": 0,
        },
    },
    "100": {  # NYC - ConEd, LMI available with no proof doc (Empower Zone style)
        "utility_slug": "consolidated-edison-ny",
        "project_details": {
            "small_commercial_capacity_available": True,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 5,
            "savings_percent_for_lmi_customers": 25,
        },
    },
    "125": {  # Hudson Valley - Central Hudson, exercises the POD ID utility path
        "utility_slug": "central-hudson-gas-electric",
        "project_details": {
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": True,
            "residential_capacity_available": True,
            "proof_documents_required": True,
            "savings_percent_for_residential_and_commercial_customers": 10,
            "savings_percent_for_lmi_customers": 20,
        },
    },
    # ── VIRGINIA / Dominion ────────────────────────────────────────────────
    # SYNTHETIC. Unlike the NY entries above, this capacity body was NOT
    # transcribed from a live response: the staging probe covered the
    # /markets/* endpoints only, never VA capacity. The savings figure is the
    # business expectation (10%), not a confirmed Perch value. It exists so the
    # VA flow can be exercised offline; the real number must come from the
    # capacity response at runtime.
    #
    # The utility slug IS confirmed (GET /markets/utilities?state=VA).
    # VA is State LMI with no non-LMI fallback, so residential capacity is
    # false and only lmi_capacity_available is set.
    "241": {  # 24112 Martinsville VA - Dominion, State LMI only.
        "utility_slug": "dominion-energy-va",
        "project_details": {
            "residential_capacity_available": False,
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": True,
            "proof_documents_required": False,
            "savings_percent_for_residential_and_commercial_customers": 0,
            "savings_percent_for_lmi_customers": 10,
        },
    },
}

# ══════════════════════ v2 CAPACITY FIXTURES (E2) ══════════════════════
#
# The v2 capacity response is an ARRAY - one entry per customer type AND county
# restriction - not the flat object above. These are written out ENTRY BY ENTRY
# rather than derived from the v1 fixtures, deliberately: a derived fixture
# would only ever express what the v1 shape can express, and the whole point of
# these is the three things it cannot (lmi_ira as a capacity type, county
# restrictions, and several savings rates for one type).
#
# The spec's rule is honoured in every fixture below: EVERY customer type is
# present, and a type without capacity appears once, unrestricted, with
# `false`, `0` and `[]`. A fixture that quietly omitted a type would let UBS
# pass tests it would fail against the real API.
#
# Selected by PERCH_API_VERSION=v2. The same ZIP prefixes as the v1 fixtures
# above resolve to the matching scenario here, so an existing test can be run
# against either version by flipping the one variable; the 3xx prefixes are
# v2-only scenarios that v1 has no way to express.


def _v2_entry(customer_type, available, kw=0, savings=(), counties=None):
    return {
        "customer_type": customer_type,
        "county_restriction": (list(counties) if counties else None),
        "capacity_available": bool(available),
        "capacity_available_kw": kw,
        "savings_percents": list(savings),
    }


def _v2_details(residential=(), lmi=(), lmi_ira=(), mass_market=(),
                commercial_and_industrial=()):
    """Assemble a project_details array, filling in every absent type.

    Each argument is a list of entries for that type. An empty list becomes the
    single unrestricted "no capacity" entry the spec guarantees.
    """
    out = []
    for ctype, entries in (("residential", residential),
                           ("lmi", lmi),
                           ("lmi_ira", lmi_ira),
                           ("mass_market", mass_market),
                           ("commercial_and_industrial",
                            commercial_and_industrial)):
        out.extend(entries if entries else [_v2_entry(ctype, False)])
    return out


_V2_FIXTURES = {
    # ── The 1xx/2xx prefixes MIRROR the v1 fixtures above, ZIP for ZIP and
    # slug for slug, so the same test can be run against either version by
    # flipping PERCH_API_VERSION. Each one expresses the SAME location in v2's
    # vocabulary - which is a translation, not a copy: v1's
    # `small_commercial_capacity_available` becomes a mass_market entry, and
    # v1's `proof_documents_required` becomes an lmi_ira entry rather than a
    # flag on the LMI one.

    # ── 100 Con Edison: residential + mass market + LMI, everything at once.
    # v1: res=T, smallcs=T, lmi=T, proof=F, rates 5 / 25.
    "100": {
        "utility_slug": "consolidated-edison-ny",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 2400, [5.0])],
            mass_market=[_v2_entry("mass_market", True, 5000, [5.0])],
            lmi=[_v2_entry("lmi", True, 1200, [25.0])]),
    },
    # ── 109 Orange & Rockland: mass market AND LMI, no residential. v1 marks
    # proof_documents_required TRUE here, so on v2 the income-qualified
    # capacity is the IRA type - which is what actually carries proof docs.
    "109": {
        "utility_slug": "orange-and-rockland",
        "project_details": _v2_details(
            mass_market=[_v2_entry("mass_market", True, 3100, [5.0])],
            lmi_ira=[_v2_entry("lmi_ira", True, 880, [20.0])]),
    },
    # ── 120 National Grid: residential ONLY. The simplest case - ONE option,
    # no choice to make, so the rep is never shown a selection prompt.
    "120": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 1500, [8.0])]),
    },
    # ── 124 Central Hudson: the REAL staging shape that made the mass-market
    # mapping matter. Mass market only, no residential, no LMI. Perch: "if you
    # see small cs/resi = resi", so this is a standard Residential enrollment
    # - and on v2 its rate must be read from the mass_market entry, because
    # the residential entry has none at all. SINGLE savings rate.
    "124": {
        "utility_slug": "central-hudson-gas-electric",
        "project_details": _v2_details(
            mass_market=[_v2_entry("mass_market", True, 4200, [5.0])]),
    },
    # ── 125 Central Hudson: residential + LMI, no mass market. v1 proof=TRUE,
    # so the income-qualified capacity is the IRA type.
    "125": {
        "utility_slug": "central-hudson-gas-electric",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 2000, [10.0])],
            lmi_ira=[_v2_entry("lmi_ira", True, 1100, [20.0])]),
    },
    # ── 129 NYSEG: income-qualified ONLY, nothing else. v1 proof=TRUE -> IRA.
    "129": {
        "utility_slug": "nyseg",
        "project_details": _v2_details(
            lmi_ira=[_v2_entry("lmi_ira", True, 1688.31, [20.0])]),
    },
    # ── 133 National Grid: residential + mass market + LMI, v1 proof=TRUE.
    # Expressed as BOTH income-qualified types available, which v1 cannot say
    # and which is the main thing a rep now has to choose between.
    "133": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 2400, [10.0])],
            mass_market=[_v2_entry("mass_market", True, 5000, [10.0])],
            lmi=[_v2_entry("lmi", True, 957.57, [20.0])],
            lmi_ira=[_v2_entry("lmi_ira", True, 1688.31, [20.0])]),
    },
    # ── 241: Virginia / Dominion. CONFIRMED - this is the REAL v2 capacity
    # response from Perch staging for Dominion / ZIP 22401, read on
    # 2026-10-28 by the E4 read-only pass. It replaces a synthetic fixture
    # that guessed income-qualified capacity ALONE.
    #
    # The difference mattered. Staging returns mass_market AVAILABLE
    # alongside lmi, and the v2 spec allows a Residential customer onto
    # mass-market capacity - so the generic mapping offered a Virginia rep a
    # non-LMI Residential program with no published rate. Virginia is state
    # LMI; that program must not exist there. The fixture now carries the
    # shape that exposed it, so the rule that withholds the program
    # (markets.income_qualified_only_markets) is tested against what Perch
    # actually sends rather than against what we assumed it sent.
    "241": {
        "utility_slug": "dominion-energy-va",
        "project_details": _v2_details(
            lmi=[_v2_entry("lmi", True, 1200, [10.0])],
            mass_market=[_v2_entry("mass_market", True, 3400, [10.0])]),
    },

    # ───────────── v2-ONLY SCENARIOS (no v1 equivalent exists) ─────────────
    #
    # Keyed on REAL NEW YORK ZIP PREFIXES (110-115), deliberately. An earlier
    # draft used 300-305, which read as "obviously synthetic" and was exactly
    # the problem: 30000 is Georgia, so services/zip_state.py resolved no
    # supported state, the county list could not be fetched for it, and a
    # fixture written to exercise county restrictions could not exercise them.
    # A fixture ZIP has to be a ZIP the rest of the stack recognises.

    # ── 110: MULTIPLE SAVINGS RATES on one type, plus a COUNTY-RESTRICTED
    # entry beside an UNRESTRICTED one. This is the spec's own worked example,
    # transcribed from the YAML (Aspen/Boulder, 3000 kW at 5%, unrestricted
    # 4000 kW at 5% and 10%), with every type present and
    # commercial_and_industrial explicitly unavailable.
    "110": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            residential=[
                _v2_entry("residential", True, 3000, [5.0],
                          counties=["Aspen", "Boulder"]),
                _v2_entry("residential", True, 4000, [5.0, 10.0]),
            ],
            lmi=[_v2_entry("lmi", True, 957.57, [10.0, 20.0])],
            lmi_ira=[_v2_entry("lmi_ira", True, 1688.31, [20.0])],
            mass_market=[_v2_entry("mass_market", True, 6729.46, [5.0, 10.0])]),
    },
    # ── 111: COUNTY-RESTRICTED ONLY, with no unrestricted entry anywhere.
    # A customer outside Albany and Rensselaer can get nothing here, which is
    # exactly why availability must not be computed from unrestricted entries
    # alone - a rep in Albany would otherwise be told there is no capacity.
    #
    # The county names are spelled as the CONTROLLED LIST spells them
    # ("Albany County", not "Albany"). Perch validates against the exact
    # string it gave us, and a fixture that quietly used a shorter form would
    # make the match look easier than it is.
    "111": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 2500, [7.5],
                                   counties=["Albany County",
                                             "Rensselaer County"])]),
    },
    # ── 112: capacity on a type UBS CANNOT ENROLL. commercial_and_industrial
    # is available and nothing else is, so there must be NO selectable program
    # - "available capacity" and "a program this rep can sell" are not the same
    # question, and this fixture is what keeps them apart.
    "112": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            commercial_and_industrial=[
                _v2_entry("commercial_and_industrial", True, 9000, [4.0])]),
    },
    # ── 113: EVERY TYPE PRESENT AND EVERY ONE UNAVAILABLE. Not a 503: Perch
    # answered 200 with a complete array saying no. UBS must read that as "no
    # capacity" without mistaking a well-formed response for a failure.
    "113": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(),
    },
    # ── 114: capacity available with NO SAVINGS RATE PUBLISHED. Available,
    # with an empty savings_percents. The program is offerable; the rate is not
    # quotable, and UBS must not invent one.
    "114": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            residential=[_v2_entry("residential", True, 1800, [])]),
    },
    # ── 115: LMI and LMI_IRA BOTH available, at different rates. The fixture
    # that proves the two are distinct rather than one type with a flag: a rep
    # choosing between them is choosing between different programs.
    "115": {
        "utility_slug": "national-grid-ny",
        "project_details": _v2_details(
            lmi=[_v2_entry("lmi", True, 957.57, [10.0, 15.0])],
            lmi_ira=[_v2_entry("lmi_ira", True, 1688.31, [20.0])]),
    },
}

# ────────────────────── Market rules (offline fixtures) ──────────────────────
# Every value marked CONFIRMED was read from Perch STAGING on 2026-10-25 by the
# read-only probe. Nothing here is invented: where the real response is larger
# than is useful offline, the fixture is a labelled SLICE of it, never a guess
# at values that were not observed.
#
# NEW YORK IS DELIBERATELY ABSENT. The mock raising "not implemented" for NY is
# exactly what makes services/perch/markets.py fall back to the ny_counties
# table, so the live market's behaviour in mock mode stays byte-identical to
# before this layer existed. Adding an NY fixture would silently repoint every
# NY test at a second source of truth.
_MARKET_UTILITIES = {
    "VA": ["dominion-energy-va"],          # CONFIRMED
}

_MARKET_ACCOUNT_FIELDS = {
    # CONFIRMED: account number accepts 10 OR 12 digits (two alternative
    # patterns, any-one-matches); meter number required at 10 digits; and NO
    # secondary_account_identifier, which the spec expresses by its absence.
    "dominion-energy-va": [
        {"field_type": "account_number", "field_name": "Account Number",
         "help_text": "10 or 12 digits", "patterns": ["^[0-9]{10}$", "^[0-9]{12}$"]},
        {"field_type": "meter_number", "field_name": "Meter Number",
         "help_text": "10 digits", "patterns": [r"^\d{10}$"]},
    ],
}

# CONFIRMED SHAPE: 133 names for VA, 38 of them independent cities spelled with
# a lowercase "city" suffix. This fixture is a REPRESENTATIVE SLICE of real
# Virginia counties and independent cities - enough to exercise casing,
# validation and the city/county distinction offline. It is not Perch's full
# list; tests that assert the 133/38 counts supply their own stub.
_MARKET_COUNTIES = {
    "VA": [
        "Accomack County", "Albemarle County", "Fairfax County",
        "Henrico County", "Pittsylvania County",
        "Alexandria city", "Martinsville city", "Richmond city",
    ],
}

# CONFIRMED: source type, single_source_type, lmi_programs, and the EMPTY
# status array. That empty array is the whole point - it is what tells UBS to
# omit the `status` key for this market.
_MARKET_SELF_ATTESTATION_OPTIONS = {
    "VA": {
        "state": "VA",
        "source_types": ["self_attestation_qualifying_income"],
        "required_source_types": ["self_attestation_qualifying_income"],
        "single_source_type": True,
        "lmi_programs": "optional",
        "programs": [
            {"value": "self_attestation_snap", "label": "SNAP"},
            {"value": "self_attestation_tanf", "label": "TANF"},
            {"value": "self_attestation_medicaid", "label": "Medicaid"},
            {"value": "self_attestation_famis", "label": "FAMIS"},
            {"value": "self_attestation_liheap", "label": "LIHEAP"},
            {"value": "self_attestation_weatherization_assistance_program",
             "label": "Weatherization Assistance Program"},
            {"value": "self_attestation_hvac",
             "label": "HVAC Health and Safety Program"},
            {"value": "self_attestation_hcvp",
             "label": "Housing Choice Voucher (HCV)"},
            {"value": "self_attestation_virginia_rent_relief_program",
             "label": "Virginia Rent Relief Program"},
        ],
        "status": [],
    },
}

# SYNTHETIC AMOUNTS. The probe confirmed VA income limits are per-county and
# returned as float dollar amounts for household sizes 1-8, but the figures for
# each county were not captured. These placeholders have an obviously-regular
# shape so no test can mistake them for published limits and nothing downstream
# can quietly depend on a number Perch never gave us.
_MARKET_INCOME_BASE = 30000.0
_MARKET_INCOME_STEP = 5000.0

# Reserved ZIPs for exercising documented failure paths.
NO_CAPACITY_ZIP = "99999"       # -> documented 503
UPSTREAM_FAILURE_ZIP = "00000"  # -> genuine 5xx, distinct from 503


# ───────────────────── POST /enroll support (E3) ─────────────────────

# UBS customer_type -> the capacity entry types Perch would draw on, straight
# from the v2 spec's own table under the enroll request's `customer_type`.
ENROLL_CUSTOMER_TYPES = {
    "Residential": ("residential", "mass_market"),
    "Business": ("mass_market", "commercial_and_industrial"),
    "LMI": ("lmi",),
    "LMI_IRA": ("lmi_ira",),
}

# Which state a utility belongs to, for the county check. Derived from the
# market fixtures rather than hardcoded twice.
_STATE_FOR_UTILITY = {"dominion-energy-va": "VA"}
for _slug in ("national-grid-ny", "consolidated-edison-ny", "nyseg",
              "orange-and-rockland", "central-hudson-gas-electric"):
    _STATE_FOR_UTILITY[_slug] = "NY"


def _enroll_422(field, code, message):
    """A PerchValidationError carrying the spec's own 422 body.

    Built the way the real client builds one, so the error travels through
    exactly the same path a live 422 would - including _msg(), which is what
    turns it into something a rep can read.
    """
    exc = PerchValidationError(f"Perch rejected the enrollment (422): {message}")
    exc.status_code = 422
    exc.body_json = {"errors": [{"field": field, "code": code,
                                 "message": message}]}
    return exc


def _applicable_entries(details, wanted_types, county):
    """The AVAILABLE entries of the wanted types that can serve this customer.

    Perch's rule, verbatim: with a county, the entries whose
    county_restriction contains it PLUS the unrestricted ones; without one,
    "only projects with no county restriction can be used".

    Handles both wire shapes so the hook works on a v1 fixture too, where
    there are no county restrictions and the flat flags are translated.
    """
    if isinstance(details, dict):
        # v1 flat: synthesize the same three facts this function needs.
        flat = []
        for ctype, flag, rate_key in (
                ("residential", "residential_capacity_available",
                 "savings_percent_for_residential_and_commercial_customers"),
                ("mass_market", "small_commercial_capacity_available",
                 "savings_percent_for_residential_and_commercial_customers"),
                ("lmi", "lmi_capacity_available",
                 "savings_percent_for_lmi_customers")):
            if details.get(flag):
                rate = details.get(rate_key)
                flat.append({"customer_type": ctype, "county_restriction": None,
                             "capacity_available": True,
                             "savings_percents": ([rate] if rate else [])})
        details = flat

    out = []
    for entry in details or []:
        if entry.get("customer_type") not in wanted_types:
            continue
        if not entry.get("capacity_available"):
            continue
        counties = entry.get("county_restriction")
        if counties is None:
            out.append(entry)
        elif county and any(str(c).strip().lower() == county.lower()
                            for c in counties):
            out.append(entry)
    return out


class PerchMockClient(PerchClient):
    mode = "mock"

    # Test hook: when True, check_capacity emits the observed staging envelope
    # (next_step_url) instead of the documented one (next_step).
    emit_staging_alias_shape = False

    # E3 test hook: when True, create_enrollment models the v2 /enroll
    # contract (customer_type, county, savings_percent) instead of raising
    # PerchNotImplementedError. Off by default - see create_enrollment.
    enforce_enroll_contract = False

    # token -> the county this mock accepted at /enroll, so the
    # self-attestation step can assert that v2 does not re-send it.
    _enrolled_counties = {}

    # Issued tokens: {token: expires_at}. Module-level so the same mock state
    # survives across the per-request client instances the factory hands out.
    _issued_tokens = {}

    # email -> most recent issued token, mirroring the spec's statement that
    # refresh returns a token for "the most recent in-progress enrollment
    # associated with the given email address".
    _tokens_by_email = {}

    def _issue(self, email, next_step=None):
        token = str(uuid.uuid4())
        expires = datetime.now() + timedelta(seconds=TOKEN_TTL_SECONDS)
        PerchMockClient._issued_tokens[token] = expires
        PerchMockClient._tokens_by_email[email] = token
        raw = {
            "enrollment_token": token,
            # Spec: ISO 8601 date-time, Z-suffixed.
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_step": next_step or DOCUMENTED_NEXT_STEP_CAPACITY,
        }
        return {"enrollment_token": token, "expires_at": raw["expires_at"],
                "next_step": raw["next_step"], "raw": raw}

    @staticmethod
    def _validate_email(email):
        if not email or "@" not in str(email):
            raise PerchValidationError("Email is invalid")

    def request_token(self, email: str) -> dict:
        """POST /token - spec requires {"email": ...}, HMAC-authenticated.

        NEW YAML: returns 201 Created. A second call for an email that already
        has an in-progress enrollment returns 422 ("An enrollment request
        already exists for this email"), NOT a second session.
        """
        self._validate_email(email)
        if email in PerchMockClient._tokens_by_email:
            raise PerchEnrollmentInProgressError(
                "An enrollment request already exists for this email. Use the "
                "/status endpoint to check the current status of the enrollment.")
        return self._issue(email)

    def refresh_token(self, email: str) -> dict:
        """PATCH /refresh_token - keyed on EMAIL, not on the old token.
        404 when no in-progress enrollment exists for that email."""
        self._validate_email(email)
        if email not in PerchMockClient._tokens_by_email:
            raise PerchNotFoundError("No enrollment request found for this email address.")
        old = PerchMockClient._tokens_by_email.get(email)
        PerchMockClient._issued_tokens.pop(old, None)
        return self._issue(email)

    def _assert_token_valid(self, token):
        expires = PerchMockClient._issued_tokens.get(token)
        if expires is None:
            raise PerchTokenExpiredError("Perch returned 403 - unknown enrollment token.")
        if expires <= datetime.now():
            raise PerchTokenExpiredError("Perch returned 403 - enrollment token expired.")

    def expire_token(self, token):
        """Test hook: force a token past its TTL without waiting an hour."""
        if token in PerchMockClient._issued_tokens:
            PerchMockClient._issued_tokens[token] = datetime.now() - timedelta(seconds=1)

    def check_capacity(self, enrollment_token: str, zip_code: str, utility_slug: str) -> dict:
        self._assert_token_valid(enrollment_token)

        zip_code = (zip_code or "").strip()
        if not zip_code.isdigit() or len(zip_code) != 5:
            raise PerchValidationError("zip_code must be a 5-digit ZIP.")
        if not utility_slug:
            raise PerchValidationError("utility_name is required (utility slug).")

        if zip_code == UPSTREAM_FAILURE_ZIP:
            raise PerchUnavailableError("Perch capacity service is unavailable (simulated 500).")

        if zip_code == NO_CAPACITY_ZIP:
            raise PerchNoCapacityError(
                f"No open solar project capacity for {utility_slug} in ZIP {zip_code}.")

        # E2: WHICH FIXTURE CATALOG, decided by the same environment variable
        # that decides which real endpoint would be called. The mock emits the
        # shape the configured version actually returns, so a test run with
        # PERCH_API_VERSION=v2 exercises the v2 parsing path end to end rather
        # than unit-testing the parser in isolation.
        #
        # Read per call, not cached at import: tests flip the variable between
        # cases, and a module-level constant would freeze whichever value
        # happened to be set when this file was first imported.
        v2 = get_api_version() == API_V2
        catalog = _V2_FIXTURES if v2 else _FIXTURES

        fixture = catalog.get(zip_code[:3])
        if not fixture or fixture["utility_slug"] != utility_slug:
            # Wrong utility for the ZIP, or an unmodelled ZIP: Perch has no open
            # project for that combination, which is the documented 503 case.
            raise PerchNoCapacityError(
                f"No open solar project capacity for {utility_slug} in ZIP {zip_code}.")

        # Exactly the documented response body - nothing added.
        #
        # v1's project_details is an object and is COPIED; v2's is an array and
        # is deep-copied, because a shallow list copy would still share the
        # entry dicts with the fixture and let one test mutate what the next
        # one reads.
        details = ([dict(entry) for entry in fixture["project_details"]] if v2
                   else dict(fixture["project_details"]))
        body = {
            "project_details": details,
            # v2 returns a BARE PATH here; v1 returns a full URL. E1's
            # normalizer resolves both, and emitting the right one per version
            # is what proves it.
            "next_step": (V2_NEXT_STEP_ENROLL if v2
                          else DOCUMENTED_NEXT_STEP_ENROLL),
        }
        # Real staging v1 returns `next_step_url` instead. Flipping this flag
        # lets tests drive the full stack through the observed staging shape
        # rather than only unit-testing the normalizer. v2 documents
        # `next_step` as the only spelling, so the alias is v1-only.
        if PerchMockClient.emit_staging_alias_shape and not v2:
            body = {"project_details": body["project_details"],
                    "next_step_url": body.pop("next_step")}
        return normalize_capacity_response(body)

    # ───────────────────────── POST /enroll (E3) ─────────────────────────

    def create_enrollment(self, enrollment_token: str, form: dict,
                          files: dict) -> dict:
        """POST /enroll, validated against the v2 contract.

        OFF BY DEFAULT, because this mock has raised PerchNotImplementedError
        for /enroll since Milestone 2 and several suites rely on that: they
        treat "not implemented" as "UBS validation passed and the call
        reached the transport", which is a legitimate way to test the
        adapter's own validation without modelling Perch's. Turning this on
        unconditionally would silently change what those suites assert.

        So it is opt-in, the same way emit_staging_alias_shape is:

            PerchMockClient.enforce_enroll_contract = True

        With it on, this models the parts of POST /v2/enrollments/enroll that
        E3 is about - customer_type, county and savings_percent - and returns
        the spec's own 422 bodies for each documented failure, so the wire
        contract can be exercised offline instead of waiting for staging.

        It deliberately models NOTHING ELSE. Address and account validation
        belong to UBS and to the market-rules layer, both already covered.
        """
        if not PerchMockClient.enforce_enroll_contract:
            raise PerchNotImplementedError("Not implemented by this client.")

        self._assert_token_valid(enrollment_token)
        form = dict(form or {})

        zip_code = (form.get("zip_code") or "").strip()
        utility = (form.get("utility_name") or "").strip()
        customer_type = (form.get("customer_type") or "").strip()
        county = (form.get("county") or "").strip() or None
        raw_rate = form.get("savings_percent")

        if customer_type not in ENROLL_CUSTOMER_TYPES:
            raise _enroll_422("customer_type", "invalid",
                              f"{customer_type} is not valid")

        details = self._capacity_details(zip_code, utility)

        # ── county ──
        # "A county that is not in the utility's state returns 422 on `county`
        # with code `not_found`." Blank is treated as not sent, which the
        # builder already guarantees by omitting it.
        #
        # CHECKED ONLY FOR A STATE THIS MOCK HAS A COUNTY LIST FOR, which
        # today means Virginia. New York is deliberately absent from
        # _MARKET_COUNTIES - that absence is what makes services/perch/
        # markets.py fall back to the ny_counties reference table, and filling
        # it in here to satisfy this check would repoint a live market's
        # controlled list at a fixture. So the unrecognized-county path is
        # exercised through VA, and NY exercises the restriction MATCHING,
        # which is the part E3 is actually about.
        state = _STATE_FOR_UTILITY.get(utility)
        known = _MARKET_COUNTIES.get(state) if state else None
        if county is not None and known:
            if not any(c.strip().lower() == county.lower() for c in known):
                raise _enroll_422(
                    "county", "not_found",
                    f"{county} is not a recognized county in {state}")

        # ── which entries apply ──
        # Perch's own rule: the entries whose county_restriction contains the
        # county, plus the unrestricted ones. WITHOUT a county, only the
        # unrestricted ones - which is the behaviour E3 exists to fix.
        wanted = ENROLL_CUSTOMER_TYPES[customer_type]
        applicable = [e for e in _applicable_entries(details, wanted, county)]
        if not applicable:
            raise _enroll_422(
                "customer_type", "capacity_unavailable",
                "Capacity is not available for this customer type, utility "
                "and zip code. Contact Perch Energy and fetch capacity using "
                "the /v2/enrollments/capacity endpoint")

        rates = sorted({float(r) for e in applicable
                        for r in (e.get("savings_percents") or [])})

        # ── savings_percent ──
        if raw_rate in (None, ""):
            if len(rates) > 1:
                raise _enroll_422(
                    "savings_percent", "required",
                    "is required when more than one savings percent is "
                    f"available ({', '.join(str(r) for r in rates)})")
        else:
            try:
                value = float(raw_rate)
            except (TypeError, ValueError):
                raise _enroll_422("savings_percent", "invalid",
                                  "must be a float")
            if value > 100:
                raise _enroll_422("savings_percent", "invalid",
                                  "must be less than or equal to 100")
            if value < 0:
                raise _enroll_422("savings_percent", "invalid",
                                  "must be greater than or equal to 0")
            if value not in rates:
                raise _enroll_422(
                    "savings_percent", "invalid",
                    "is not available for this customer type, utility and "
                    "zip code. Fetch the available savings percents using "
                    "the /v2/enrollments/capacity endpoint")

        # ── accepted ──
        # next_step follows the customer type, exactly as the real API does:
        # IRA LMI needs proof documents, plain LMI self-attests, everything
        # else goes straight to contracts.
        next_step = {"LMI_IRA": "/lmi/proof_docs",
                     "LMI": "/lmi/self_attestation"}.get(customer_type,
                                                         "/contracts")
        PerchMockClient._enrolled_counties[enrollment_token] = county
        return normalize_enroll_response({"next_step": next_step})

    def _capacity_details(self, zip_code, utility):
        """The project_details this mock would return for a ZIP and utility."""
        catalog = (_V2_FIXTURES if get_api_version() == API_V2 else _FIXTURES)
        fixture = catalog.get((zip_code or "")[:3])
        if not fixture or fixture["utility_slug"] != utility:
            raise _enroll_422(
                "customer_type", "capacity_unavailable",
                "Capacity is not available for this customer type, utility "
                "and zip code. Contact Perch Energy and fetch capacity using "
                "the /v2/enrollments/capacity endpoint")
        return fixture["project_details"]

    # ──────────────────────────── Market rules ────────────────────────────
    # HMAC pre-enrollment lookups. Offline equivalents of the five /markets/*
    # endpoints, served from the confirmed fixtures above.
    #
    # A state or utility with no fixture raises PerchNotImplementedError rather
    # than returning an empty list. services/perch/markets.py turns that into
    # MarketDataUnavailable, which means:
    #   * NY falls back to ny_counties, exactly as before this layer existed
    #   * an unmodelled market fails CLOSED instead of looking like a market
    #     with no utilities, no counties and no attestation requirements
    # Returning {} here would be the dangerous option: silently plausible.

    @staticmethod
    def _state_key(state):
        return (state or "").strip().upper()

    def get_market_utilities(self, state: str) -> dict:
        st = self._state_key(state)
        if st not in _MARKET_UTILITIES:
            raise PerchNotImplementedError(
                f"The mock has no market fixture for state {st!r}.")
        return {"state": st, "utilities": list(_MARKET_UTILITIES[st])}

    def get_market_utility_account_fields(self, utility_slug: str) -> dict:
        slug = (utility_slug or "").strip()
        if slug not in _MARKET_ACCOUNT_FIELDS:
            raise PerchNotImplementedError(
                f"The mock has no account-field fixture for utility {slug!r}.")
        return {"utility_name": slug,
                "fields": [dict(f) for f in _MARKET_ACCOUNT_FIELDS[slug]]}

    def get_market_counties(self, state: str) -> dict:
        st = self._state_key(state)
        if st not in _MARKET_COUNTIES:
            raise PerchNotImplementedError(
                f"The mock has no county fixture for state {st!r}.")
        return {"state": st, "counties": list(_MARKET_COUNTIES[st])}

    def get_market_county_income_limits(self, state: str, county: str) -> dict:
        st = self._state_key(state)
        name = (county or "").strip()
        if st not in _MARKET_COUNTIES:
            raise PerchNotImplementedError(
                f"The mock has no income-limit fixture for state {st!r}.")
        # Case-insensitive match, echoing the name AS STORED - the real endpoint
        # documents that its echo "may differ in case from the county you sent",
        # and Virginia's independent cities depend on that exact spelling.
        match = next((c for c in _MARKET_COUNTIES[st] if c.lower() == name.lower()), None)
        if match is None:
            raise PerchNotFoundError(f"county not found: {name!r}")
        return {
            "state": st,
            "county": match,
            "income_limits": [
                {"house_occupancy": n,
                 "household_income": _MARKET_INCOME_BASE + (n - 1) * _MARKET_INCOME_STEP}
                for n in range(1, 9)
            ],
        }

    def get_market_self_attestation_options(self, state: str) -> dict:
        st = self._state_key(state)
        if st not in _MARKET_SELF_ATTESTATION_OPTIONS:
            raise PerchNotImplementedError(
                f"The mock has no self-attestation fixture for state {st!r}.")
        opts = _MARKET_SELF_ATTESTATION_OPTIONS[st]
        return {**opts, "programs": [dict(p) for p in opts["programs"]],
                "status": list(opts["status"])}
