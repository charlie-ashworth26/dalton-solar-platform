"""
UBS 2.0 Phase E2: v2 capacity parsing, customer types, and program selection.

WHAT E2 CHANGED, AND WHAT EACH PART HERE PROVES

The v2 capacity response is an ARRAY of entries, not a flat object, and three
assumptions UBS had baked into the flat shape stopped holding at once:

  1. ONE ENTRY PER TYPE became MANY - one per distinct set of county
     restrictions.
  2. ONE SCALAR SAVINGS RATE became AN ARRAY - several rates, or none.
  3. TWO LMI TYPES - `lmi` and `lmi_ira` - replaced one LMI flag plus a
     proof_documents_required boolean.

Both shapes are now parsed into one entry model (services/perch/capacity.py)
and mapped to UBS programs in one place (adapter.PROGRAM_RULES). This suite is
organised around the things that could silently go wrong in that translation:

  A. v1 produces EXACTLY the choices it produced before E2. Every v1 fixture
     has a hand-written expectation here, so a regression is a failed
     assertion rather than a judgement call.
  B. v2 produces the right choices, including the real NY case where the
     savings rate has to come from the mass_market entry.
  C. An unavailable entry is never selectable, however it is phrased.
  D. County-restricted and unrestricted entries are both preserved, and
     availability is not computed from unrestricted entries alone.
  E. Several savings rates survive; nothing is collapsed to one number.
  F. `lmi` and `lmi_ira` stay distinct - a request for one is not satisfied
     by the other.
  G. No raw Perch customer-type string reaches anything a rep can read.
  H. A committed selection cannot switch, silently or otherwise.
  I. The stored shape round-trips, and the migration is additive.

MOSTLY OFFLINE. Sections A-G and I run against the model and the mock with no
HTTP and no app context. Section H drives the real Flask app, because "the UI
and the backend cannot disagree" is a claim about routes, not about functions.

Run: python test/test_e2_capacity_model.py
"""
import json
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one, execute
import seed
from services.perch import adapter, workflow
from services.perch import capacity as cap
from services.perch import client as perch_client
from services.perch import config as perch_config
from services.perch import mock_client
from services.perch import market_products
from routes import perch_routes
from services.perch.client import build_enrollment_multipart
from services.perch.mock_client import PerchMockClient, _FIXTURES, _V2_FIXTURES
from services.perch.errors import (
    PerchValidationError, PerchNotImplementedError)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

RES = adapter.CUSTOMER_TYPE_RESIDENTIAL
LMI = adapter.CUSTOMER_TYPE_LMI
LMI_IRA = adapter.CUSTOMER_TYPE_LMI_IRA
BUSINESS = adapter.CUSTOMER_TYPE_BUSINESS

# Every string Perch uses for a capacity type or a customer type on the wire.
# None of these may appear in anything a rep reads.
RAW_PERCH_TOKENS = ("mass_market", "commercial_and_industrial", "lmi_ira",
                    "small_commercial_capacity_available",
                    "residential_capacity_available", "lmi_capacity_available",
                    "proof_documents_required", "savings_percents",
                    "county_restriction", "capacity_available_kw")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


@contextmanager
def api_version(value):
    """Run a block with PERCH_API_VERSION set, then restore it."""
    saved = os.environ.get("PERCH_API_VERSION")
    try:
        if value is None:
            os.environ.pop("PERCH_API_VERSION", None)
        else:
            os.environ["PERCH_API_VERSION"] = value
        yield
    finally:
        if saved is None:
            os.environ.pop("PERCH_API_VERSION", None)
        else:
            os.environ["PERCH_API_VERSION"] = saved


def programs_of(details, county=None):
    """(customer_type, savings_percents) for each offered program."""
    return [(p["customer_type"], tuple(p["savings_percents"]))
            for p in adapter.available_programs(details, county=county)]


def types_of(details, county=None):
    return [t for t, _rates in programs_of(details, county=county)]


def entry(ctype, available=True, kw=0, savings=(), counties=None):
    """A v2 wire entry, as Perch sends it."""
    return {"customer_type": ctype,
            "county_restriction": (list(counties) if counties else None),
            "capacity_available": available,
            "capacity_available_kw": kw,
            "savings_percents": list(savings)}


# ═══════════════ A. v1 IS UNCHANGED ═══════════════

# Hand-written expectations for every v1 mock fixture, derived from the flat
# fields and the pre-E2 rules - NOT from the new code. That is the whole point:
# if the new model disagrees with these, the model changed v1 behaviour.
#
#   Residential is offered when residential OR small_commercial is set, at the
#     res/commercial rate (one field covers both).
#   LMI is offered when lmi is set, at the LMI rate, with lmi_required from
#     proof_documents_required.
#   A rate of 0 means "not published" and yields no rate at all.
#   LMI_IRA is NEVER offered on v1 - v1 cannot express it.
V1_EXPECTED = {
    "100": [(RES, (5.0,), False), (LMI, (25.0,), False)],
    "109": [(RES, (5.0,), False), (LMI, (20.0,), True)],
    "120": [(RES, (8.0,), False)],
    "124": [(RES, (5.0,), False)],
    "125": [(RES, (10.0,), False), (LMI, (20.0,), True)],
    "129": [(LMI, (20.0,), True)],
    "133": [(RES, (10.0,), False), (LMI, (20.0,), True)],
    "241": [(LMI, (10.0,), False)],
}


def test_v1_unchanged():
    section("A. v1 flat capacity produces EXACTLY the pre-E2 choices")

    for prefix in sorted(V1_EXPECTED):
        details = _FIXTURES[prefix]["project_details"]
        got = [(p["customer_type"], tuple(p["savings_percents"]),
                p["lmi_required"])
               for p in adapter.available_programs(details)]
        check(f"{prefix} ({_FIXTURES[prefix]['utility_slug']}) -> {got}",
              got == V1_EXPECTED[prefix])

    print("\n-- A2. the scalar savings field still works for pre-E2 callers --")
    for prefix, expected in (("124", 5.0), ("129", None), ("241", None)):
        progs = adapter.available_programs(_FIXTURES[prefix]["project_details"])
        scalar = progs[0]["savings_percent"] if progs else None
        if prefix == "129" or prefix == "241":
            # Single-rate LMI: the scalar IS still populated. Only a program
            # with SEVERAL rates leaves it None, and v1 never has several.
            check(f"{prefix}: single-rate program keeps a scalar",
                  isinstance(scalar, float))
        else:
            check(f"{prefix}: scalar savings is {expected}", scalar == expected)
    check("no v1 fixture produces a program with several rates "
          "(so no v1 scalar is ever None)",
          all(len(p["savings_percents"]) <= 1
              for f in _FIXTURES.values()
              for p in adapter.available_programs(f["project_details"])))

    print("\n-- A3. v1 never offers LMI_IRA --")
    for prefix, f in _FIXTURES.items():
        check(f"{prefix}: LMI_IRA absent",
              LMI_IRA not in types_of(f["project_details"]))
        model = cap.parse(f["project_details"])
        check(f"  ...and lmi_ira has no capacity in the model",
              model.has_capacity(cap.LMI_IRA) is False)

    print("\n-- A4. the v1 proof-docs flag is still what decides LMI proof --")
    base = {"residential_capacity_available": False,
            "small_commercial_capacity_available": False,
            "lmi_capacity_available": True,
            "savings_percent_for_lmi_customers": 20}
    for flag, expected in ((True, True), (False, False)):
        progs = adapter.available_programs(
            dict(base, proof_documents_required=flag))
        check(f"proof_documents_required={flag} -> lmi_required={expected}",
              progs[0]["lmi_required"] is expected)
    # Absent has always meant True in UBS - warn, and let Perch correct it.
    check("an ABSENT flag still defaults to requiring proof",
          adapter.available_programs(base)[0]["lmi_required"] is True)


# ═══════════════ B. v2 PRODUCES THE RIGHT CHOICES ═══════════════

def test_v2_choices():
    section("B. v2 entry-shaped capacity produces the right choices")

    print("\n-- B1. the spec's own worked example --")
    spec_example = [
        entry("residential", True, 3000, [5.0], ["Aspen", "Boulder"]),
        entry("residential", True, 4000, [5.0, 10.0]),
        entry("lmi", True, 957.57, [10.0, 20.0]),
        entry("lmi_ira", True, 1688.31, [20.0]),
        entry("mass_market", True, 6729.46, [5.0, 10.0]),
        entry("commercial_and_industrial", False, 0, []),
    ]
    model = cap.parse(spec_example)
    check("parsed as the v2 entry shape", model.shape == cap.SHAPE_V2_ENTRIES)
    check("three programs offered, in a stable order",
          types_of(spec_example) == [RES, LMI, LMI_IRA])
    check("  ...Residential rates come from the residential entries",
          dict(programs_of(spec_example))[RES] == (5.0, 10.0))
    check("  ...LMI rates from the lmi entries",
          dict(programs_of(spec_example))[LMI] == (10.0, 20.0))
    check("  ...LMI_IRA rates from the lmi_ira entries",
          dict(programs_of(spec_example))[LMI_IRA] == (20.0,))
    check("commercial_and_industrial is NOT offered - UBS cannot enroll it",
          BUSINESS not in types_of(spec_example))

    print("\n-- B2. the real NY case: mass market with NO residential entry --")
    # residential has no capacity and therefore no rates, but mass_market
    # does. The spec's table says Residential's rates come from the
    # `residential` entries.
    #
    # E2.1 REMOVED THE SUBSTITUTION THAT USED TO FILL THIS GAP. Quoting the
    # mass-market rate for a Residential enrollment is a guess dressed as a
    # quote - Perch has not said that is the price - and savings_percent is a
    # figure a rep reads out to a customer. The program is still OFFERED,
    # because the capacity is genuinely there; the rate stays empty and the
    # ambiguity is stated rather than resolved.
    ny = [entry("residential", False, 0, []),
          entry("mass_market", True, 4200, [5.0])]
    progs = adapter.available_programs(ny)
    check("Residential IS still offered on mass-market capacity",
          [p["customer_type"] for p in progs] == [RES])
    check("  ...but NO rate is substituted for it",
          progs[0]["savings_percents"] == [] and progs[0]["savings_percent"] is None)
    check("  ...the authoritative source is still named",
          progs[0]["savings_source"] == cap.RESIDENTIAL)
    check("  ...the gap is flagged, not hidden",
          progs[0]["savings_unresolved"] is True)
    check("  ...and the raw array of the backing type is preserved verbatim",
          progs[0]["savings_candidates"]
          == [{"capacity_type": cap.MASS_MARKET, "savings_percents": [5.0]}])
    check("  ...with the backing capacity type named",
          [t["capacity_type"] for t in progs[0]["capacity_types"]]
          == [cap.MASS_MARKET])
    # When residential DOES have capacity, its own rates are used and nothing
    # is flagged - there is no ambiguity to report.
    both = [entry("residential", True, 2000, [7.0]),
            entry("mass_market", True, 4200, [5.0])]
    p0 = adapter.available_programs(both)[0]
    check("when residential HAS capacity its own rate is used",
          p0["savings_percents"] == [7.0]
          and p0["savings_source"] == cap.RESIDENTIAL)
    check("  ...and nothing is flagged as unresolved",
          p0["savings_unresolved"] is False and p0["savings_candidates"] == [])
    # A program with no rate ANYWHERE is not ambiguous - there is nothing to
    # have chosen differently, so it is not flagged either.
    nothing = [entry("residential", True, 100, [])]
    check("no rate anywhere is not an 'unresolved' ambiguity",
          adapter.available_programs(nothing)[0]["savings_unresolved"] is False)

    print("\n-- B2b. v1's shared field is NOT a substitution --")
    # v1 reports ONE savings field that its own spec says covers residential
    # AND commercial customers. Reading it for the Residential program is
    # reading the field Perch sent, not borrowing another type's rate - so v1
    # keeps quoting it, and keeps quoting it unflagged.
    v1_ny = _FIXTURES["109"]["project_details"]
    v1p = adapter.available_programs(v1_ny)[0]
    check("v1 Residential still quotes the res/commercial field",
          v1p["savings_percents"] == [5.0] and v1p["savings_percent"] == 5.0)
    check("  ...sourced from the FIELD, not from a capacity type",
          v1p["savings_source"] == "v1_residential_and_commercial_field")
    check("  ...and not flagged as unresolved",
          v1p["savings_unresolved"] is False)

    print("\n-- B3. every v2 mock fixture resolves to sane programs --")
    for prefix in sorted(_V2_FIXTURES):
        details = _V2_FIXTURES[prefix]["project_details"]
        got = programs_of(details)
        check(f"{prefix}: {got}", all(t in (RES, LMI, LMI_IRA)
                                      for t, _r in got))

    print("\n-- B4. every customer type is present in the model, always --")
    for prefix, f in _V2_FIXTURES.items():
        model = cap.parse(f["project_details"])
        present = {e["capacity_type"] for e in model.entries}
        check(f"{prefix}: all five types present",
              set(cap.CAPACITY_TYPES).issubset(present))
    # Even when Perch omits one, which the spec says it will not do.
    sparse = cap.parse([entry("residential", True, 100, [5.0])])
    check("a type Perch omitted is filled in as explicitly unavailable",
          sparse.has_capacity(cap.LMI) is False
          and len([e for e in sparse.entries
                   if e["capacity_type"] == cap.LMI]) == 1)

    print("\n-- B5. a malformed body is 'no capacity', not a crash --")
    for bad in (None, "nonsense", 42, [], {}, [{"no_type": 1}], [None, 3]):
        model = cap.parse(bad)
        check(f"{bad!r} -> no programs, no exception",
              adapter.available_programs(model) == [])


# ═══════════════ C. UNAVAILABLE IS NEVER SELECTABLE ═══════════════

def test_unavailable_never_selectable():
    section("C. Unavailable entries are never selectable")

    print("\n-- C1. available:false is not capacity --")
    none_available = [entry("residential", False, 0, []),
                      entry("lmi", False, 0, []),
                      entry("lmi_ira", False, 0, []),
                      entry("mass_market", False, 0, []),
                      entry("commercial_and_industrial", False, 0, [])]
    check("no programs at all", adapter.available_programs(none_available) == [])
    for requested in (RES, LMI, LMI_IRA):
        try:
            adapter.resolve_customer_type(none_available, requested=requested)
            check(f"{requested} rejected", False)
        except PerchValidationError as e:
            check(f"{requested} rejected ({str(e)[:44]!r})", True)

    print("\n-- C2. available:false WITH a savings rate is still unavailable --")
    # A rate on an unavailable entry is not an offer. Reading savings before
    # availability would turn one into a selectable program.
    contradictory = [entry("residential", False, 0, [5.0, 10.0])]
    check("still no program", adapter.available_programs(contradictory) == [])
    check("  ...and no rates are published for it",
          cap.parse(contradictory).savings_percents_for(cap.RESIDENTIAL) == [])

    print("\n-- C3. capacity ONLY on a type UBS cannot enroll --")
    ci_only = [entry("commercial_and_industrial", True, 9000, [4.0])]
    check("commercial-and-industrial capacity offers NO program",
          adapter.available_programs(ci_only) == [])
    check("  ...the model still records that it IS available",
          cap.parse(ci_only).has_capacity(cap.COMMERCIAL_AND_INDUSTRIAL) is True)
    try:
        adapter.resolve_customer_type(ci_only)
        check("resolving raises rather than inventing a program", False)
    except PerchValidationError as e:
        check(f"resolving raises ({str(e)[:50]!r})", True)

    print("\n-- C4. Business is modeled but not selectable --")
    mass_and_ci = [entry("mass_market", True, 5000, [5.0]),
                   entry("commercial_and_industrial", True, 9000, [4.0])]
    check("Business never appears as a program",
          BUSINESS not in types_of(mass_and_ci))
    check("  ...even though both its capacity types are available",
          adapter.PROGRAM_RULES[BUSINESS]["capacity_types"]
          == (cap.MASS_MARKET, cap.COMMERCIAL_AND_INDUSTRIAL))
    check("  ...because the rule says unselectable",
          adapter.PROGRAM_RULES[BUSINESS]["selectable"] is False)
    try:
        adapter.resolve_customer_type(mass_and_ci, requested="Business")
        check("requesting Business is refused", False)
    except PerchValidationError:
        check("requesting Business is refused", True)
    # Mass market alone still means a Residential enrollment, as it always has.
    check("  ...but mass market alone is still a Residential program",
          types_of(mass_and_ci) == [RES])

    print("\n-- C5. a tampered client cannot select an unoffered program --")
    lmi_only = [entry("lmi", True, 900, [20.0])]
    for bogus in ("Residential", "LMI_IRA", "Business", "SmallCommercial",
                  "admin", "LMI; DROP TABLE enrollments"):
        try:
            adapter.resolve_customer_type(lmi_only, requested=bogus)
            check(f"{bogus!r} refused", False)
        except PerchValidationError:
            check(f"{bogus!r} refused", True)
    check("  ...while the genuinely offered type is accepted",
          adapter.resolve_customer_type(lmi_only, requested="LMI")[0] == LMI)
    check("  ...case-insensitively, returning the CANONICAL value",
          adapter.resolve_customer_type(lmi_only, requested="lmi")[0] == "LMI")


# ═══════════════ D. COUNTY RESTRICTIONS ═══════════════

def test_county_restrictions():
    section("D. County-restricted and unrestricted entries are preserved")

    details = [
        entry("residential", True, 3000, [5.0], ["Aspen", "Boulder"]),
        entry("residential", True, 4000, [10.0]),
        entry("lmi", True, 900, [20.0], ["Albany"]),
    ]
    model = cap.parse(details)

    print("\n-- D1. both kinds of entry survive the parse --")
    res_entries = [e for e in model.entries
                   if e["capacity_type"] == cap.RESIDENTIAL
                   and e["capacity_available"]]
    check("two residential entries, not merged into one",
          len(res_entries) == 2)
    check("  ...one restricted to Aspen and Boulder",
          any(e["county_restriction"] == ("Aspen", "Boulder")
              for e in res_entries))
    check("  ...and one unrestricted",
          any(e["county_restriction"] is None for e in res_entries))
    check("a multi-county restriction is NOT split across its counties",
          all(len(e["county_restriction"]) == 2 for e in res_entries
              if e["county_restriction"]))

    print("\n-- D2. the three county questions are answered differently --")
    check("no county, unrestricted only -> 1 entry",
          len(model.entries_for(cap.RESIDENTIAL)) == 1)
    check("  ...which is the unrestricted one",
          model.entries_for(cap.RESIDENTIAL)[0]["county_restriction"] is None)
    check("county given -> unrestricted PLUS the matching restricted entry",
          len(model.entries_for(cap.RESIDENTIAL, county="Aspen")) == 2)
    check("  ...a non-matching county gets the unrestricted entry only",
          len(model.entries_for(cap.RESIDENTIAL, county="Queens")) == 1)
    check("  ...county matching is case-insensitive",
          len(model.entries_for(cap.RESIDENTIAL, county="aspen")) == 2
          and len(model.entries_for(cap.RESIDENTIAL, county="  BOULDER ")) == 2)
    check("include_restricted -> every available entry",
          len(model.entries_for(cap.RESIDENTIAL, include_restricted=True)) == 2)

    print("\n-- D3. availability does NOT ignore restricted entries --")
    # The rep has no county at availability time. Computing availability from
    # unrestricted entries alone would tell a rep in Albany that nothing is
    # available when the Albany-restricted LMI project is right there.
    restricted_only = [entry("residential", True, 2500, [7.5],
                             ["Albany", "Rensselaer"])]
    check("a county-RESTRICTED-only location still offers a program",
          types_of(restricted_only) == [RES])
    check("  ...and the program is flagged as county-restricted",
          adapter.available_programs(restricted_only)[0]["county_restricted"]
          is True)
    check("  ...while Perch's strict no-county rule is still representable",
          cap.parse(restricted_only).entries_for(cap.RESIDENTIAL) == [])
    check("an unrestricted-only location is NOT flagged",
          adapter.available_programs(
              [entry("residential", True, 100, [5.0])])[0]["county_restricted"]
          is False)

    print("\n-- D4. the counties themselves are kept for E3 --")
    check("residential counties recorded",
          model.counties_for(cap.RESIDENTIAL) == ["Aspen", "Boulder"])
    check("lmi counties recorded", model.counties_for(cap.LMI) == ["Albany"])
    check("the location reports that restrictions exist",
          model.has_county_restrictions() is True)
    check("  ...and a location with none reports that too",
          cap.parse([entry("residential", True, 100, [5.0])]
                    ).has_county_restrictions() is False)
    check("counties reach the program entry for E3 to use",
          sorted(adapter.available_programs(details)[0]["capacity_types"][0]
                 ["counties"]) == ["Aspen", "Boulder"])

    print("\n-- D5. v1 has no county restrictions, and says so --")
    v1model = cap.parse(_FIXTURES["109"]["project_details"])
    check("every v1 entry is unrestricted",
          all(e["county_restriction"] is None for e in v1model.entries))
    check("  ...and the location reports no restrictions",
          v1model.has_county_restrictions() is False)

    print("\n-- D6. E2 does NOT send county to Perch --")
    enroll_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                      encoding="utf-8").read()
    multipart_src = open(os.path.join(ROOT, "services", "perch", "client.py"),
                         encoding="utf-8").read()
    builder = multipart_src.split("def build_enrollment_multipart")[1][:4000]
    # E3 SENDS IT. Until E3 this asserted the opposite - the county was
    # collected and deliberately not transmitted - which was correct for E2.1
    # and is exactly what E3 was for. The county is now a flat multipart
    # field, and the detailed coverage lives in section K below.
    check("the enroll multipart builder sends the county field",
          '"county",' in builder)
    check("  ...and create_enrollment validates against the STORED county",
          "SELECT service_county, selected_savings_percent FROM enrollments"
          in enroll_src and "county=county" in enroll_src)


# ═══════════════ E. SAVINGS RATES ═══════════════

def test_savings_rates():
    section("E. Savings rates are preserved, never collapsed")

    print("\n-- E1. one rate, several rates, no rate --")
    one = [entry("residential", True, 100, [5.0])]
    several = [entry("residential", True, 100, [5.0, 10.0, 15.0])]
    none_ = [entry("residential", True, 100, [])]

    p_one = adapter.available_programs(one)[0]
    check("one rate -> the array holds it", p_one["savings_percents"] == [5.0])
    check("  ...and the scalar is populated for pre-E2 callers",
          p_one["savings_percent"] == 5.0)

    p_several = adapter.available_programs(several)[0]
    check("several rates -> ALL of them survive",
          p_several["savings_percents"] == [5.0, 10.0, 15.0])
    check("  ...and the scalar is None rather than a guess",
          p_several["savings_percent"] is None)

    p_none = adapter.available_programs(none_)[0]
    check("no rate -> an empty array, and the program is still offered",
          p_none["savings_percents"] == [] and p_none["customer_type"] == RES)
    check("  ...with a null scalar", p_none["savings_percent"] is None)

    print("\n-- E2. rates are sorted, de-duplicated and numeric --")
    messy = [entry("residential", True, 100, [10.0, 5.0, 10.0, "7.5", None, 0])]
    rates = adapter.available_programs(messy)[0]["savings_percents"]
    check(f"sorted ascending, no duplicates, numbers only ({rates})",
          rates == [5.0, 7.5, 10.0])
    check("  ...and zero is 'not published', not a zero-percent offer",
          0 not in rates and 0.0 not in rates)

    print("\n-- E3. rates are unioned across the applicable entries --")
    # A customer in Aspen may be served by the restricted entry OR the
    # unrestricted one, so both rates are genuinely on offer to them.
    split = [entry("residential", True, 3000, [5.0], ["Aspen"]),
             entry("residential", True, 4000, [10.0])]
    model = cap.parse(split)
    check("no county -> the unrestricted rate only",
          model.savings_percents_for(cap.RESIDENTIAL,
                                     include_restricted=False) == [10.0])
    check("in Aspen -> both rates",
          model.savings_percents_for(cap.RESIDENTIAL, county="Aspen")
          == [5.0, 10.0])
    check("elsewhere -> the unrestricted rate only",
          model.savings_percents_for(cap.RESIDENTIAL, county="Queens")
          == [10.0])

    print("\n-- E4. rates are NOT shared between programs --")
    distinct = [entry("residential", True, 100, [5.0]),
                entry("lmi", True, 100, [20.0]),
                entry("lmi_ira", True, 100, [25.0])]
    by_type = dict(programs_of(distinct))
    check("Residential gets only its own rate", by_type[RES] == (5.0,))
    check("LMI gets only its own rate", by_type[LMI] == (20.0,))
    check("LMI_IRA gets only its own rate", by_type[LMI_IRA] == (25.0,))

    print("\n-- E5. E3 sends savings_percent; the RULES are what matter --")
    builder = open(os.path.join(ROOT, "services", "perch", "client.py"),
                   encoding="utf-8").read()
    builder = builder.split("def build_enrollment_multipart")[1][:4000]
    check("the enroll multipart builder sends savings_percent",
          '"savings_percent",' in builder)
    # The builder only serializes. WHICH value it gets - or whether it gets
    # one at all - is resolve_savings_percent()'s decision, covered in K.

    print("\n-- E6. the UI renders a range rather than one number --")
    check("programSavingsHtml exists and reads the ARRAY",
          "function programSavingsHtml" in JS
          and "p.savings_percents" in JS)
    check("  ...renders a range for several rates",
          "Math.min.apply" in JS and "Math.max.apply" in JS)
    check("  ...and never averages them",
          "reduce(" not in JS.split("function programSavingsHtml")[1][:900])


# ═══════════════ F. lmi vs lmi_ira ═══════════════

def test_lmi_vs_lmi_ira():
    section("F. lmi and lmi_ira stay distinct")

    print("\n-- F1. distinct capacity types in the model --")
    both = [entry("lmi", True, 957.57, [10.0, 15.0]),
            entry("lmi_ira", True, 1688.31, [20.0])]
    model = cap.parse(both)
    check("both report capacity independently",
          model.has_capacity(cap.LMI) and model.has_capacity(cap.LMI_IRA))
    check("  ...with their own rates",
          model.savings_percents_for(cap.LMI) == [10.0, 15.0]
          and model.savings_percents_for(cap.LMI_IRA) == [20.0])
    check("  ...and their own kW",
          model.kw_for(cap.LMI) == 957.57
          and model.kw_for(cap.LMI_IRA) == 1688.31)

    print("\n-- F2. two separate programs, two separate labels --")
    check("both offered", types_of(both) == [LMI, LMI_IRA])
    labels = {p["customer_type"]: p["label"]
              for p in adapter.available_programs(both)}
    check(f"distinct labels ({labels})", labels[LMI] != labels[LMI_IRA])

    print("\n-- F3. one does NOT satisfy a request for the other --")
    lmi_only = [entry("lmi", True, 900, [20.0])]
    ira_only = [entry("lmi_ira", True, 900, [20.0])]
    check("LMI_IRA requested on lmi-only capacity is refused",
          _rejects(lmi_only, "LMI_IRA"))
    check("LMI requested on lmi_ira-only capacity is refused",
          _rejects(ira_only, "LMI"))
    check("  ...each is accepted on its own capacity",
          adapter.resolve_customer_type(lmi_only, requested="LMI")[0] == LMI
          and adapter.resolve_customer_type(
              ira_only, requested="LMI_IRA")[0] == LMI_IRA)
    check("a single-option location auto-resolves to the RIGHT one",
          adapter.resolve_customer_type(ira_only)[0] == LMI_IRA)

    print("\n-- F4. proof docs come from the TYPE on v2, not the old flag --")
    ira = adapter.available_programs(ira_only)[0]
    plain = adapter.available_programs(lmi_only)[0]
    check("lmi_ira requires proof documents", ira["lmi_required"] is True)
    check("plain lmi does not", plain["lmi_required"] is False)
    check("the v1 flag is not consulted on a v2 response",
          cap.parse(ira_only).v1_proof_documents_required is None)
    # And the rule is written as the capacity type, not as a flag lookup.
    adapter_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                       encoding="utf-8").read()
    check("  ...and the v1 flag is only read for a v1-shaped response",
          "if model.shape == cap_module.SHAPE_V1_FLAT:" in adapter_src)

    print("\n-- F5. BOTH are income-qualified everywhere it matters --")
    check("the family helper covers both",
          adapter.is_lmi_customer_type("LMI")
          and adapter.is_lmi_customer_type("LMI_IRA")
          and adapter.is_lmi_customer_type("lmi_ira"))
    check("  ...and not Residential or Business",
          not adapter.is_lmi_customer_type("Residential")
          and not adapter.is_lmi_customer_type("Business")
          and not adapter.is_lmi_customer_type(None))
    check("LMI_CUSTOMER_TYPES is exactly the two of them",
          set(adapter.LMI_CUSTOMER_TYPES) == {"LMI", "LMI_IRA"})

    print("\n-- F6. no `== 'LMI'` branch survives in production code --")
    # This is the bug class: `=== 'LMI'` is FALSE for LMI_IRA, so every such
    # test would route an IRA enrollment down the residential branch.
    for rel in (("routes", "enrollment_routes.py"),):
        src = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
        code = "\n".join(l for l in src.split("\n")
                         if not l.strip().startswith("#"))
        check(f"{rel[-1]}: no bare == \"LMI\" comparison",
              '== "LMI"' not in code and "== 'LMI'" not in code)
    js_code = "\n".join(l for l in JS.split("\n")
                        if not l.strip().startswith("//"))
    # The one remaining literal is the LMI_IRA label branch in
    # formatProgramType, which is a LABEL choice and not a family test.
    bare = [l for l in js_code.split("\n")
            if "=== 'LMI'" in l and "LMI_IRA" not in l]
    check(f"app.js: no bare === 'LMI' family test ({bare})", not bare)
    check("  ...the family test is used instead",
          "isLmiCustomerType(e.selected_customer_type)" in JS
          and "const isLmi = isLmiCustomerType(type)" in JS)

    print("\n-- F7. the proof-doc flow itself is UNCHANGED (E3 territory) --")
    # E2 preserves the selected type for E3; it does not rewire the downstream
    # proof-doc or self-attestation steps.
    wf_src = open(os.path.join(ROOT, "services", "perch", "workflow.py"),
                  encoding="utf-8").read()
    check("the step map still routes proof docs by Perch's next_step",
          '"/lmi/proof_docs": "proof_docs"' in wf_src)
    check("  ...and next_step is still the authority",
          "Perch's next_step remains AUTHORITATIVE" in wf_src
          or "next_step remains AUTHORITATIVE" in wf_src)


def _rejects(details, requested):
    try:
        adapter.resolve_customer_type(details, requested=requested)
        return False
    except PerchValidationError:
        return True


# ═══════════════ G. NO RAW PERCH VOCABULARY IN THE UI ═══════════════

def test_no_raw_vocabulary():
    section("G. No raw Perch customer-type string reaches a rep")

    print("\n-- G1. every program label is English --")
    everything = [entry("residential", True, 100, [5.0]),
                  entry("lmi", True, 100, [10.0]),
                  entry("lmi_ira", True, 100, [20.0]),
                  entry("mass_market", True, 100, [5.0]),
                  entry("commercial_and_industrial", True, 100, [4.0])]
    for p in adapter.available_programs(everything):
        for field in ("label", "description"):
            text = p[field]
            bad = [tok for tok in RAW_PERCH_TOKENS if tok in text]
            check(f"{p['customer_type']} {field}: {text!r}", not bad)
        # The capacity-type labels shown beside a program are English too.
        for ct in p["capacity_types"]:
            bad = [tok for tok in RAW_PERCH_TOKENS if tok in ct["label"]]
            check(f"  capacity label {ct['label']!r}", not bad)

    print("\n-- G2. a type Perch adds LATER is softened, not printed raw --")
    check("an unknown capacity type reads as words",
          adapter.capacity_type_label("agrivoltaic_shared")
          == "Agrivoltaic shared")
    check("  ...and an unknown customer type does too",
          "_" not in adapter.program_label("some_new_type"))

    print("\n-- G3. rep-facing ERRORS name programs by label --")
    lmi_only = [entry("lmi", True, 900, [20.0])]
    messages = []
    for requested in ("LMI_IRA", "Business", "Residential", "nonsense_type"):
        try:
            adapter.resolve_customer_type(lmi_only, requested=requested)
        except PerchValidationError as e:
            messages.append(str(e))
    # The multi-program and no-capacity messages too.
    for details in ([entry("residential", True, 1, [5.0]),
                     entry("lmi", True, 1, [9.0])],
                    [entry("commercial_and_industrial", True, 1, [4.0])]):
        try:
            adapter.resolve_customer_type(details)
        except PerchValidationError as e:
            messages.append(str(e))
    check(f"{len(messages)} rep-facing messages collected", len(messages) >= 5)
    for msg in messages:
        bad = [tok for tok in RAW_PERCH_TOKENS if tok in msg]
        check(f"no raw token in {msg[:52]!r}", not bad)
    check("  ...and LMI_IRA itself never appears verbatim",
          not any("LMI_IRA" in m for m in messages))

    print("\n-- G4. the capacity panel is English --")
    check_dict = {"zip_code": "12345", "utility_slug": "national-grid-ny",
                  "utility_display_name": "National Grid",
                  "capacity_available": True,
                  "capacity_entries": cap.parse(everything).as_dict()}
    panels = workflow._capacity_panels(check_dict)
    blob = json.dumps(panels)
    for tok in RAW_PERCH_TOKENS:
        check(f"panel JSON has no {tok!r}", tok not in blob)
    check("  ...and no raw utility slug either",
          "national-grid-ny" not in blob)
    labels = [s["label"] for s in panels[0]["segments"]]
    check(f"segments are labelled for people ({labels})",
          "Residential" in labels and any("LMI" in l for l in labels))
    check("commercial capacity is not listed as a rep option",
          not any("Commercial" == l for l in labels))

    print("\n-- G5. the UI takes labels from the SERVER --")
    check("programLabel prefers the server label",
          "if(p && p.label) return p.label;" in JS)
    check("programBlurb prefers the server description",
          "if(p && p.description) return p.description;" in JS)
    check("  ...so a raw type cannot be printed by a forgotten case",
          "p.customer_type === 'LMI' ? 'Residential LMI'" not in JS)
    check("the selection copy counts programs instead of saying 'Two'",
          "'Two programs are available" not in JS
          and "availablePrograms.length + ' programs are available" in JS)


# ═══════════════ H. THE SELECTION LOCKS ═══════════════

def test_selection_locks():
    section("H. A committed selection cannot silently switch")

    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def start(zip_code, utility, tag):
        r = c.post("/api/perch/enrollments/capacity", headers=rep,
                   json={"email": f"{tag}@example.com", "zip_code": zip_code,
                         "utility_name": utility})
        assert r.status_code in (200, 201), r.data
        return r.get_json()["enrollment_id"]

    def programs(eid):
        return c.get(f"/api/perch/enrollments/{eid}/programs",
                     headers=rep).get_json()

    def select(eid, ctype):
        return c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                      json={"customer_type": ctype})

    def stored(eid):
        with app.app_context():
            return query_one("SELECT selected_customer_type FROM enrollments "
                             "WHERE id=?", (eid,))["selected_customer_type"]

    # 133 under v2 offers Residential, LMI and LMI_IRA - three options, which
    # is the case that did not exist before E2.
    with api_version("v2"):
        eid = start("13350", "national-grid-ny", "e2lock")
        body = programs(eid)
        offered = [p["customer_type"] for p in body["available_programs"]]
        check(f"v2 offers three programs ({offered})",
              offered == [RES, LMI, LMI_IRA])
        check("  ...and says a selection is required",
              body["selection_required"] is True)
        check("  ...with nothing auto-selected",
              body["selected_customer_type"] is None)
        check("  ...reporting the v2 capacity shape",
              body["capacity_shape"] == cap.SHAPE_V2_ENTRIES)

        print("\n-- H1. before Continue, the rep may change their mind --")
        for ctype in (LMI, RES, LMI_IRA):
            check(f"select {ctype}", select(eid, ctype).status_code == 200)
            check(f"  ...persisted as {ctype}", stored(eid) == ctype)
        check("the programs endpoint reports the live selection",
              programs(eid)["selected_customer_type"] == LMI_IRA)

        print("\n-- H2. after Continue, it is LOCKED --")
        with app.app_context():
            workflow.set_state(eid, "contracts")
            check("the enrollment is committed",
                  workflow.perch_committed(eid) is True)
        for attempt in (RES, LMI, None):
            r = select(eid, attempt)
            check(f"changing to {attempt!r} is refused with 409",
                  r.status_code == 409)
            check("  ...and flagged as committed",
                  r.get_json().get("committed") is True)
        check("the stored selection is UNCHANGED", stored(eid) == LMI_IRA)
        check("  ...and the programs endpoint still reports it",
              programs(eid)["selected_customer_type"] == LMI_IRA)

        print("\n-- H3. the refusal says nothing about API internals --")
        msg = select(eid, RES).get_json()["error"]
        check(f"rep-facing message ({msg[:46]!r})",
              not any(w in msg.lower() for w in
                      ("perch", "api", "endpoint", "422", "409", "/enroll",
                       "lmi_ira", "customer_type")))

        print("\n-- H4. the enroll path re-validates the SAME way --")
        # The selection the UI shows and the type the backend would send are
        # resolved by one validator against one stored model, so they cannot
        # drift apart.
        with app.app_context():
            cap_row = adapter.latest_capacity_check(eid)
            model = adapter.capacity_model_for(cap_row)
            resolved, reason = adapter.resolve_customer_type(
                model, requested=stored(eid))
        check(f"enroll resolves the stored selection unchanged ({reason})",
              resolved == LMI_IRA)
        check("  ...and the stored model is the v2 one",
              model.shape == cap.SHAPE_V2_ENTRIES)

        print("\n-- H5. a single-option location needs no selection --")
        eid2 = start("12050", "national-grid-ny", "e2single")
        body2 = programs(eid2)
        check("one program offered",
              [p["customer_type"] for p in body2["available_programs"]] == [RES])
        check("  ...no selection prompt", body2["selection_required"] is False)
        with app.app_context():
            check("  ...and enroll resolves it automatically",
                  adapter.resolve_customer_type(
                      adapter.capacity_model_for(
                          adapter.latest_capacity_check(eid2)))[0] == RES)

    print("\n-- H5b. the v2 capacity array does not break the ENROLLMENT LIST --")
    # FOUND IN A BROWSER, NOT BY A UNIT TEST. perch_workflow_state
    # .last_response_json holds whatever the last step returned, and the
    # capacity step stores Perch's `project_details`. On v1 that is an object;
    # on v2 it is an ARRAY. _program_savings() fell back to that column when no
    # program was persisted and called .get() on it, so a single v2 capacity
    # check returned 500 for GET /api/enrollments - the whole dashboard, for
    # every enrollment, not just that one.
    #
    # Every reader of the column now goes through workflow.last_response_dict(),
    # which returns {} for a payload that is not an object.
    with api_version("v2"):
        eid_list = start("13350", "national-grid-ny", "e2listcrash")
        with app.app_context():
            raw = query_one("SELECT last_response_json FROM perch_workflow_state "
                            "WHERE enrollment_id=?", (eid_list,))
            check("the capacity step really did store a JSON ARRAY",
                  isinstance(json.loads(raw["last_response_json"]), list))
            check("  ...and the shared reader returns {} for it, not a crash",
                  workflow.last_response_dict(raw) == {})
        r = c.get("/api/enrollments", headers=rep)
        check(f"GET /api/enrollments is 200, not 500 ({r.status_code})",
              r.status_code == 200)
        listed = [e for e in r.get_json() if e["id"] == eid_list]
        check("  ...and the v2 enrollment is in the list", len(listed) == 1)
        check("  ...with no savings figure invented for it",
              listed[0]["program_savings"] is None
              or isinstance(listed[0]["program_savings"], dict))
        check("the enrollment DETAIL endpoint is 200 too",
              c.get(f"/api/enrollments/{eid_list}",
                    headers=rep).status_code == 200)

    print("\n-- H5c. the helper is robust to every stored shape --")
    for payload, expected in ((None, {}), ({}, {}),
                              ({"last_response_json": None}, {}),
                              ({"last_response_json": ""}, {}),
                              ({"last_response_json": "[]"}, {}),
                              ({"last_response_json": "[1,2]"}, {}),
                              ({"last_response_json": "not json"}, {}),
                              ({"last_response_json": "null"}, {}),
                              ({"last_response_json": '"text"'}, {}),
                              ({"last_response_json": '{"customer_type":"LMI"}'},
                               {"customer_type": "LMI"})):
        check(f"{str(payload)[:40]!r} -> {expected}",
              workflow.last_response_dict(payload) == expected)

    print("\n-- H6. a v1 enrollment behaves exactly as before --")
    with api_version(None):
        eid3 = start("10901", "orange-and-rockland", "e2v1")
        body3 = programs(eid3)
        offered3 = [p["customer_type"] for p in body3["available_programs"]]
        check(f"v1 offers Residential + LMI only ({offered3})",
              offered3 == [RES, LMI])
        check("  ...never LMI_IRA", LMI_IRA not in offered3)
        check("  ...and reports the v1 shape",
              body3["capacity_shape"] == cap.SHAPE_V1_FLAT)
        check("selecting LMI works", select(eid3, LMI).status_code == 200)
        check("  ...selecting LMI_IRA is refused",
              select(eid3, LMI_IRA).status_code == 400)
        check("  ...and the stored selection is untouched by the refusal",
              stored(eid3) == LMI)


# ═══════════════ I. PERSISTENCE ═══════════════

def test_persistence():
    section("I. Persistence is additive and round-trips")

    print("\n-- I1. the stored shape round-trips through the model --")
    # The stored entry key is `capacity_type`; the WIRE key is `customer_type`.
    # Parsing the stored form with the wire parser silently dropped every
    # entry and rebuilt them all as unavailable - a capacity check that
    # round-tripped to "nothing available anywhere".
    for details in ([entry("residential", True, 3000, [5.0], ["Aspen"]),
                     entry("residential", True, 4000, [5.0, 10.0]),
                     entry("lmi_ira", True, 1688.31, [20.0])],
                    _FIXTURES["109"]["project_details"],
                    _FIXTURES["241"]["project_details"]):
        before = cap.parse(details)
        after = cap.parse_stored(before.as_dict())
        check(f"shape survives ({before.shape})", after.shape == before.shape)
        check("  ...every entry survives",
              after.as_entry_list() == before.as_entry_list())
        check("  ...and so do the programs",
              programs_of(after) == programs_of(before))
    v1 = cap.parse(_FIXTURES["109"]["project_details"])
    check("the v1 proof-docs flag survives the round trip",
          cap.parse_stored(v1.as_dict()).v1_proof_documents_required
          == v1.v1_proof_documents_required)
    check("a bare entry LIST is accepted too",
          cap.parse_stored(v1.as_entry_list()).has_capacity(cap.LMI) is True)
    check("  ...and None is an empty model, not a crash",
          cap.parse_stored(None).entries == [])

    print("\n-- I2. the migration is additive --")
    sql = open(os.path.join(ROOT, "db", "migrations",
                            "013_v2_capacity_entries.sql"),
               encoding="utf-8").read()
    statements = [l.strip() for l in sql.split("\n")
                  if l.strip() and not l.strip().startswith("--")]
    check(f"{len(statements)} statements, both ALTER ... ADD COLUMN",
          statements and all(s.upper().startswith("ALTER TABLE")
                             and "ADD COLUMN" in s.upper()
                             for s in statements))
    check("  ...no UPDATE, DELETE, DROP or INSERT anywhere",
          not any(kw in " ".join(statements).upper()
                  for kw in ("UPDATE", "DELETE", "DROP", "INSERT")))
    check("  ...and the legacy flat columns are NOT touched",
          "residential_capacity_available" not in " ".join(statements))

    print("\n-- I3. a v2 check is stored and hydrated as entries --")
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    with api_version("v2"):
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": "e2store@example.com",
                           "zip_code": "13350",
                           "utility_name": "national-grid-ny"}
                     ).get_json()["enrollment_id"]
        with app.app_context():
            row = query_one("SELECT capacity_entries_json, capacity_shape, "
                            "raw_response_json, residential_capacity_available, "
                            "small_commercial_capacity_available, "
                            "lmi_capacity_available, proof_documents_required, "
                            "savings_percent_res_commercial, savings_percent_lmi "
                            "FROM perch_capacity_checks WHERE enrollment_id=? "
                            "ORDER BY id DESC LIMIT 1", (eid,))
            stored_model = json.loads(row["capacity_entries_json"])
            check("the shape is recorded as v2",
                  row["capacity_shape"] == cap.SHAPE_V2_ENTRIES)
            check("  ...and every entry is stored",
                  len(stored_model["entries"]) >= 5)
            check("raw_response_json still holds what PERCH sent",
                  isinstance(json.loads(row["raw_response_json"])
                             .get("project_details"), list))
            print("\n-- I4. the legacy flat columns stay populated on v2 --")
            check("residential flag set", row["residential_capacity_available"] == 1)
            check("mass market -> small_commercial flag set",
                  row["small_commercial_capacity_available"] == 1)
            check("lmi flag set", row["lmi_capacity_available"] == 1)
            check("proof_documents_required reflects lmi_ira capacity",
                  row["proof_documents_required"] == 1)
            check("  ...and the single-rate columns are populated",
                  row["savings_percent_res_commercial"] == 10.0
                  and row["savings_percent_lmi"] == 20.0)
            hydrated = adapter.capacity_model_for(
                adapter.latest_capacity_check(eid))
            check("hydration prefers the stored ENTRIES",
                  hydrated.shape == cap.SHAPE_V2_ENTRIES)
            check("  ...and yields the same programs as the live response",
                  [p["customer_type"] for p in
                   adapter.available_programs(hydrated)]
                  == [RES, LMI, LMI_IRA])

    print("\n-- I5. a v1 check is stored exactly as it was before E2 --")
    with api_version(None):
        eid2 = c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": "e2v1store@example.com",
                            "zip_code": "10901",
                            "utility_name": "orange-and-rockland"}
                      ).get_json()["enrollment_id"]
        with app.app_context():
            row2 = query_one("SELECT * FROM perch_capacity_checks "
                             "WHERE enrollment_id=? ORDER BY id DESC LIMIT 1",
                             (eid2,))
        v1f = _FIXTURES["109"]["project_details"]
        check("residential flag matches the v1 field",
              row2["residential_capacity_available"]
              == (1 if v1f["residential_capacity_available"] else 0))
        check("small_commercial flag matches the v1 field",
              row2["small_commercial_capacity_available"]
              == (1 if v1f["small_commercial_capacity_available"] else 0))
        check("lmi flag matches the v1 field",
              row2["lmi_capacity_available"]
              == (1 if v1f["lmi_capacity_available"] else 0))
        check("proof flag is the v1 value VERBATIM",
              row2["proof_documents_required"]
              == (1 if v1f["proof_documents_required"] else 0))
        check("res/commercial savings is the v1 field VERBATIM",
              row2["savings_percent_res_commercial"]
              == v1f["savings_percent_for_residential_and_commercial_customers"])
        check("lmi savings is the v1 field VERBATIM",
              row2["savings_percent_lmi"]
              == v1f["savings_percent_for_lmi_customers"])
        check("  ...and the shape is recorded as v1",
              row2["capacity_shape"] == cap.SHAPE_V1_FLAT)

    print("\n-- I6. a PRE-MIGRATION row still works --")
    # Every historical row has capacity_entries_json NULL. Those enrollments
    # must keep resolving, from the flat columns, or a rep resuming a draft
    # started last week loses their program list.
    with app.app_context():
        from db import execute
        execute("UPDATE perch_capacity_checks SET capacity_entries_json = NULL, "
                "capacity_shape = NULL WHERE enrollment_id = ?", (eid2,))
        legacy = adapter.latest_capacity_check(eid2)
        check("hydration falls back to the flat columns",
              legacy["capacity_entries"] is None)
        model = adapter.capacity_model_for(legacy)
        check("  ...producing a v1-shaped model",
              model.shape == cap.SHAPE_V1_FLAT)
        check("  ...with the SAME programs as before the row was blanked",
              [p["customer_type"] for p in adapter.available_programs(model)]
              == [RES, LMI])


# ═══════════════ J. E2.1 - THE CONDITIONAL COUNTY FIELD ═══════════════

def test_county_field():
    section("J. E2.1 - the County field appears only when it decides")

    print("\n-- J1. county_decides_options: relevance, not mere presence --")
    # Restrictions can EXIST without MATTERING. The question is whether the
    # restricted entries add a program or a rate the unrestricted ones do not.
    irrelevant = [entry("residential", True, 3000, [5.0], ["Aspen"]),
                  entry("residential", True, 4000, [5.0])]
    check("a restricted entry offering nothing extra does NOT require a county",
          adapter.county_decides_options(irrelevant) is False)
    check("  ...even though restrictions are present",
          cap.parse(irrelevant).has_county_restrictions() is True)

    adds_program = [entry("lmi", True, 900, [20.0], ["Albany"])]
    check("a restricted entry that is the ONLY capacity requires a county",
          adapter.county_decides_options(adds_program) is True)

    adds_rate = [entry("residential", True, 3000, [7.0], ["Albany"]),
                 entry("residential", True, 4000, [5.0])]
    check("a restricted entry adding a RATE requires a county",
          adapter.county_decides_options(adds_rate) is True)

    check("no restrictions at all never requires a county",
          adapter.county_decides_options(
              [entry("residential", True, 100, [5.0])]) is False)
    print("   every v1 fixture:")
    for prefix, f in sorted(_FIXTURES.items()):
        check(f"   {prefix} never requires a county (v1 has no restrictions)",
              adapter.county_decides_options(f["project_details"]) is False)

    print("\n-- J2. filtering by the selected county --")
    mixed = [entry("residential", True, 4000, [5.0]),
             entry("lmi", True, 900, [20.0], ["Albany", "Rensselaer"])]
    check("in Albany: unrestricted AND the matching restricted entry",
          types_of(mixed, county="Albany") == [RES, LMI])
    check("  ...case-insensitively",
          types_of(mixed, county="albany") == [RES, LMI]
          and types_of(mixed, county="  RENSSELAER ") == [RES, LMI])
    check("in the WRONG county: the restricted capacity is NOT shown",
          types_of(mixed, county="Queens") == [RES])
    check("  ...and its rate is not shown either",
          dict(programs_of(mixed, county="Queens")).get(LMI) is None)
    check("with no county at all: everything the LOCATION can offer",
          types_of(mixed) == [RES, LMI])
    check("  ...and unrestricted-only when asked strictly",
          [p["customer_type"] for p in adapter.available_programs(
              mixed, include_restricted=False)] == [RES])

    print("\n-- J3. a wrong county can leave NOTHING available --")
    restricted_only = [entry("residential", True, 2500, [7.5], ["Albany"])]
    check("in Albany the program is offered",
          types_of(restricted_only, county="Albany") == [RES])
    check("in Queens nothing is", types_of(restricted_only, county="Queens") == [])
    check("  ...and resolving refuses rather than offering it anyway",
          _rejects_county(restricted_only, "Residential", "Queens"))

    print("\n-- J4. unavailable entries stay unselectable, county or not --")
    unavailable_restricted = [entry("residential", False, 0, [5.0], ["Albany"]),
                              entry("lmi", False, 0, [20.0])]
    for county in (None, "Albany", "Queens"):
        check(f"county={county!r}: no programs",
              adapter.available_programs(unavailable_restricted,
                                         county=county) == [])

    print("\n-- J5. workflow: the field is declared, never fetched --")
    wf_src = open(os.path.join(ROOT, "services", "perch", "workflow.py"),
                  encoding="utf-8").read()
    check("workflow.py still does NOT import the markets layer",
          not any(("markets" in l and "perch" in l)
                  for l in wf_src.splitlines()
                  if l.strip().startswith(("import ", "from "))))
    check("  ...so it declares the field and the routes layer fills it",
          "def fill_county_options" in wf_src
          and "def county_field_required" in wf_src)

    def step_for(details, selected=None):
        chk = {"zip_code": "12345", "utility_slug": "national-grid-ny",
               "utility_display_name": "National Grid",
               "capacity_available": True,
               "next_step_url": "/enroll",
               "capacity_entries": cap.parse(details).as_dict()}
        enr = {"service_county": selected}
        return workflow._step_capacity_result(enr, chk)

    unrestricted_step = step_for([entry("residential", True, 100, [5.0])])
    check("an unrestricted location gets NO County field",
          unrestricted_step["fields"] == [])
    check("  ...and Continue is enabled, exactly as before",
          unrestricted_step["primary_action"]["enabled"] is True)

    needs = step_for(restricted_only)
    check("a county-dependent location gets ONE field",
          [f["name"] for f in needs["fields"]] == ["county"])
    field = needs["fields"][0]
    check("  ...a select, required, with a placeholder - a normal field",
          field["type"] == "select" and field["required"] is True
          and field["placeholder"])
    check("  ...whose options the workflow layer leaves empty for routes",
          field["options"] == [])
    check("  ...and Continue is HELD until it is answered",
          needs["primary_action"]["enabled"] is False
          and "county" in needs["primary_action"]["disabled_reason"].lower())
    # A rate that exists only in two counties must not be quoted before
    # anyone has said which county this customer is in.
    check("  ...no savings figure is quoted while the county is outstanding",
          needs["panels"][0]["metrics"] == [])
    check("  ...and the subtitle does not claim the rates apply",
          "apply to this enrollment" not in needs["subtitle"])
    check("  ...while the segments still show the location HAS capacity",
          any(sg["available"] for sg in needs["panels"][0]["segments"]))
    answered = step_for(restricted_only, selected="Albany")
    check("once answered the field keeps the value",
          answered["fields"][0]["value"] == "Albany")
    check("  ...and Continue is released",
          answered["primary_action"]["enabled"] is True)
    check("  ...and the rate for THAT county is quoted",
          [m["value"] for m in answered["panels"][0]["metrics"]] == ["7.5%"])
    wrong = step_for(restricted_only, selected="Queens")
    check("in the wrong county no rate is quoted at all",
          wrong["panels"][0]["metrics"] == [])
    check("  ...and nothing shows as available",
          not any(sg["available"] for sg in wrong["panels"][0]["segments"]))

    print("\n-- J6. fill_county_options is the routes-side seam --")
    descriptor = {"step": needs}
    workflow.fill_county_options(descriptor, ["Albany", "Queens"], "perch")
    check("options are filled from the list routes supplied",
          [o["value"] for o in needs["fields"][0]["options"]]
          == ["Albany", "Queens"])
    empty_step = step_for(restricted_only)
    workflow.fill_county_options({"step": empty_step}, [], None)
    check("an unavailable list makes the field read-only, not an empty menu",
          empty_step["fields"][0]["readonly"] is True
          and "could not be loaded" in empty_step["fields"][0]["help"])
    check("filling a step with no County field is a no-op",
          workflow.fill_county_options({"step": unrestricted_step}, ["X"], None)
          is not None and unrestricted_step["fields"] == [])

    print("\n-- J7. end to end through the app --")
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def start(zip_code, utility, tag):
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"{tag}@example.com", "zip_code": zip_code,
                            "utility_name": utility}).get_json()["enrollment_id"]

    def programs(eid):
        return c.get(f"/api/perch/enrollments/{eid}/programs",
                     headers=rep).get_json()

    def set_county(eid, name):
        return c.post(f"/api/perch/enrollments/{eid}/county", headers=rep,
                      json={"county": name})

    with api_version("v2"):
        # 30100 - county-restricted capacity ONLY, so the county decides.
        eid = start("11100", "national-grid-ny", "e21county")
        body = programs(eid)
        check("the endpoint says a county is required",
              body["county_required"] is True)
        check("  ...and is awaiting it", body["awaiting_county"] is True)
        check("  ...so NO programs are presented yet",
              body["available_programs"] == [])
        check("  ...with no selection prompt either",
              body["selection_required"] is False)
        check("  ...and the county list comes from the markets layer",
              isinstance(body["county_options"], list)
              and len(body["county_options"]) > 0)
        check("  ...which is NY's, for a NY ZIP",
              "Albany County" in body["county_options"])

        print("\n   ...a program cannot be chosen before the county")
        r = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                   json={"customer_type": RES})
        check("choosing a program first is refused", r.status_code == 400)
        check("  ...saying why", r.get_json().get("county_required") is True)

        print("\n   ...the county cannot be free-typed")
        # "Albany" without the suffix is refused too: the controlled list
        # spells it "Albany County" and Perch validates the exact string.
        for bogus in ("Notacounty", "Albany",
                      "albany'; DROP TABLE enrollments"):
            check(f"{bogus!r} refused", set_county(eid, bogus).status_code == 400)
        with app.app_context():
            check("  ...and nothing was stored",
                  query_one("SELECT service_county FROM enrollments WHERE id=?",
                            (eid,))["service_county"] is None)
        # Blank CLEARS the answer, the same way a blank program selection does.
        # That is not an invalid value; it is the rep un-answering.
        blank = set_county(eid, "   ")
        check("a blank value clears the county rather than erroring",
              blank.status_code == 200
              and blank.get_json()["selected_county"] is None)

        print("\n   ...the right county reveals the program")
        ok = set_county(eid, "albany county")   # lower case on purpose
        check("a valid county is accepted case-insensitively",
              ok.status_code == 200)
        check("  ...and stored in PERCH'S spelling",
              ok.get_json()["selected_county"] == "Albany County")
        body2 = programs(eid)
        check("the program now appears",
              [p["customer_type"] for p in body2["available_programs"]] == [RES])
        check("  ...and nothing is awaited", body2["awaiting_county"] is False)
        check("  ...with the county reported back",
              body2["selected_county"] == "Albany County")
        check("selecting the program now works",
              c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                     json={"customer_type": RES}).status_code == 200)

        print("\n   ...a wrong county hides it again, and clears the selection")
        moved = set_county(eid, "Queens County")
        check("the county can be corrected", moved.status_code == 200)
        check("  ...and the now-invalid program selection is CLEARED, not kept",
              moved.get_json()["cleared_program"] == RES)
        body3 = programs(eid)
        check("  ...no programs in the wrong county",
              body3["available_programs"] == [])
        check("  ...and no stale selection reported",
              body3["selected_customer_type"] is None)
        with app.app_context():
            check("  ...nor stored",
                  query_one("SELECT selected_customer_type FROM enrollments "
                            "WHERE id=?", (eid,))["selected_customer_type"] is None)

        print("\n   ...the workflow step carries the field, populated")
        wf = c.get(f"/api/perch/enrollments/{eid}/workflow",
                   headers=rep).get_json()
        fields = wf["step"]["fields"]
        check("the Availability step has the County field",
              [f["name"] for f in fields] == ["county"])
        check("  ...populated by the routes layer",
              len(fields[0]["options"]) > 0)
        check("  ...holding the stored value",
              fields[0]["value"] == "Queens County")
        check("  ...and it is still the SAME step, not a new one",
              wf["step"]["key"] == "capacity_result")

        print("\n   ...an unrestricted location is untouched")
        eid2 = start("13350", "national-grid-ny", "e21plain")
        body4 = programs(eid2)
        check("no county required", body4["county_required"] is False)
        check("  ...no options offered", body4["county_options"] == [])
        check("  ...nothing awaited", body4["awaiting_county"] is False)
        check("  ...and the programs are presented immediately",
              [p["customer_type"] for p in body4["available_programs"]]
              == [RES, LMI, LMI_IRA])
        wf2 = c.get(f"/api/perch/enrollments/{eid2}/workflow",
                    headers=rep).get_json()
        check("  ...with NO County field on the step",
              wf2["step"]["fields"] == [])

        print("\n   ...the county locks with the program")
        with app.app_context():
            workflow.set_state(eid, "contracts")
        r = set_county(eid, "Albany County")
        check("changing the county after commit is refused",
              r.status_code == 409)
        check("  ...flagged as committed",
              r.get_json().get("committed") is True)
        with app.app_context():
            check("  ...and the stored county is unchanged",
                  query_one("SELECT service_county FROM enrollments WHERE id=?",
                            (eid,))["service_county"] == "Queens County")

    print("\n-- J8. v1 is completely untouched by all of this --")
    with api_version(None):
        eid3 = start("10901", "orange-and-rockland", "e21v1")
        b = programs(eid3)
        check("no county required on v1", b["county_required"] is False)
        check("  ...nothing awaited", b["awaiting_county"] is False)
        check("  ...programs presented straight away",
              [p["customer_type"] for p in b["available_programs"]] == [RES, LMI])
        check("  ...and the program can be chosen with no county",
              c.post(f"/api/perch/enrollments/{eid3}/program", headers=rep,
                     json={"customer_type": LMI}).status_code == 200)
        wf3 = c.get(f"/api/perch/enrollments/{eid3}/workflow",
                    headers=rep).get_json()
        check("  ...and the Availability step has no County field",
              wf3["step"]["fields"] == [])

    print("\n-- J9. E3 carries the county through to Perch --")
    client_src = open(os.path.join(ROOT, "services", "perch", "client.py"),
                      encoding="utf-8").read()
    builder = client_src.split("def build_enrollment_multipart")[1][:4000]
    check("the enroll multipart builder carries county",
          '"county",' in builder)
    check("  ...and savings_percent", '"savings_percent",' in builder)

    print("\n-- J10. no raw Perch vocabulary in any of the new copy --")
    blob = json.dumps([step_for(restricted_only),
                       step_for(restricted_only, selected="Albany")])
    for tok in RAW_PERCH_TOKENS:
        check(f"county step JSON has no {tok!r}", tok not in blob)
    check("  ...and no 'mass market' wording anywhere in the UI labels",
          "mass market" not in blob.lower())


# ═══════════════ K. E3 - THE ENROLL PAYLOAD ═══════════════

def test_enroll_payload():
    section("K. E3 - customer_type, county and savings_percent at /enroll")

    print("\n-- K1. the multipart builder carries both new fields --")
    form, _files = build_enrollment_multipart({
        "email_address": "a@b.com", "first_name": "A", "last_name": "B",
        "phone_number": "5185550100", "customer_type": "Residential",
        "utility_name": "national-grid-ny", "zip_code": "12345",
        "county": "Albany County", "savings_percent": 7.5,
        "billing_address": {"address_1": "1 St", "city": "Albany",
                            "state": "NY", "zip": "12345"},
        "utility_accounts": [{"utility_account_number": "1234567890",
                              "service_address": {"address_1": "1 St",
                                                  "city": "Albany",
                                                  "state": "NY",
                                                  "zip": "12345"}}],
    })
    check("county is a FLAT field, spelled exactly as given",
          form.get("county") == "Albany County")
    check("savings_percent is a FLAT field", form.get("savings_percent") == "7.5")
    check("  ...and neither is nested or indexed",
          not any(k.startswith(("county[", "savings_percent[")) for k in form))

    print("\n-- K2. omitted, never sent blank --")
    bare, _f = build_enrollment_multipart({
        "email_address": "a@b.com", "customer_type": "LMI",
        "county": "", "savings_percent": None,
        "utility_accounts": []})
    check("a blank county is omitted entirely", "county" not in bare)
    check("a null savings_percent is omitted entirely",
          "savings_percent" not in bare)

    print("\n-- K3. the savings rules, exactly as the spec states them --")
    one = [entry("residential", True, 100, [5.0])]
    many = [entry("residential", True, 100, [5.0, 10.0])]
    none_ = [entry("residential", True, 100, [])]

    value, why = adapter.resolve_savings_percent(one, RES)
    check(f"ONE rate -> sent automatically ({value}, {why})", value == 5.0)
    value, why = adapter.resolve_savings_percent(none_, RES)
    check(f"NO rate -> omitted, never invented ({value}, {why})", value is None)
    try:
        adapter.resolve_savings_percent(many, RES)
        check("SEVERAL rates with no choice -> stops", False)
    except PerchValidationError as e:
        check(f"SEVERAL rates with no choice -> stops ({str(e)[:46]!r})", True)
        check("  ...with a rep-facing message, not a Perch one",
              "choose" in str(e).lower() and "perch" not in str(e).lower())
    check("SEVERAL rates WITH a choice -> that choice is sent",
          adapter.resolve_savings_percent(many, RES, selected=10.0)[0] == 10.0)
    check("a chosen rate that is no longer offered is REFUSED, not swapped",
          _raises(lambda: adapter.resolve_savings_percent(many, RES,
                                                          selected=99.0)))
    check("  ...and so is a stale choice on a now-single-rate program",
          _raises(lambda: adapter.resolve_savings_percent(one, RES,
                                                          selected=99.0)))
    check("a non-numeric choice is refused rather than coerced",
          _raises(lambda: adapter.resolve_savings_percent(many, RES,
                                                          selected="ten")))

    print("\n-- K4. the rate follows the COUNTY --")
    split = [entry("residential", True, 100, [5.0]),
             entry("residential", True, 100, [5.0, 9.0], ["Albany County"])]
    check("in Albany both rates apply, so a choice is required",
          _raises(lambda: adapter.resolve_savings_percent(
              split, RES, county="Albany County")))
    check("  ...and the county-only rate IS selectable there",
          adapter.resolve_savings_percent(split, RES, county="Albany County",
                                          selected=9.0)[0] == 9.0)
    check("elsewhere only the unrestricted rate applies, sent automatically",
          adapter.resolve_savings_percent(split, RES,
                                          county="Queens County")[0] == 5.0)
    check("  ...and the county-only rate is REFUSED there",
          _raises(lambda: adapter.resolve_savings_percent(
              split, RES, county="Queens County", selected=9.0)))

    print("\n-- K5. end to end: what actually reaches Perch --")
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    sent = {}

    def capture(enrollment_token, form, files):
        sent.clear()
        sent.update(form)
        raise PerchNotImplementedError("captured")

    def enroll(eid, email, customer_type=None):
        """Run create_enrollment far enough to capture the multipart form.

        customer_type is passed the way the enroll ROUTE passes it: from the
        enrollment's stored selection, never from the client.
        """
        if customer_type is None:
            with app.app_context():
                customer_type = query_one(
                    "SELECT selected_customer_type FROM enrollments WHERE id=?",
                    (eid,))["selected_customer_type"]
        payload = {
            "email_address": email, "first_name": "Jane", "last_name": "Doe",
            "phone_number": "5185550100", "customer_type": customer_type,
            "billing_address": {"address_1": "1 Main St", "city": "Albany",
                                "state": "NY", "zip": "12205"},
            "utility_accounts": [{
                "utility_account_number": "1234567890",
                "service_address": {"address_1": "1 Main St",
                                    "city": "Albany", "state": "NY",
                                    "zip": "12205"},
                "utility_bills": []}],
        }
        with app.app_context():
            try:
                adapter.create_enrollment(eid, payload)
            except PerchNotImplementedError:
                return None
        return None

    real_client = perch_config.get_perch_client

    def patched():
        client = real_client()
        client.create_enrollment = capture
        return client

    def start(zip_code, utility, tag):
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"{tag}@example.com", "zip_code": zip_code,
                            "utility_name": utility}).get_json()["enrollment_id"]

    adapter.get_perch_client = patched
    try:
        with api_version("v2"):
            # (a) county-restricted Residential - THE E3 FIX
            eid = start("11100", "national-grid-ny", "e3county")
            c.post(f"/api/perch/enrollments/{eid}/county", headers=rep,
                   json={"county": "Albany County"})
            c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                   json={"customer_type": RES})
            enroll(eid, "e3county@example.com")
            check(f"county-restricted: county IS sent ({sent.get('county')!r})",
                  sent.get("county") == "Albany County")
            check("  ...with the program", sent.get("customer_type") == RES)
            check("  ...and the single applicable rate",
                  sent.get("savings_percent") == "7.5")

            # (b) unrestricted Residential, one rate
            eid2 = start("12050", "national-grid-ny", "e3one")
            c.post(f"/api/perch/enrollments/{eid2}/program", headers=rep,
                   json={"customer_type": RES})
            enroll(eid2, "e3one@example.com")
            check("unrestricted, one rate: no county is sent",
                  "county" not in sent)
            check("  ...and the one rate is sent automatically",
                  sent.get("savings_percent") == "8.0")

            # (c) unrestricted Residential, MULTIPLE rates
            eid3 = start("11000", "national-grid-ny", "e3many")
            c.post(f"/api/perch/enrollments/{eid3}/program", headers=rep,
                   json={"customer_type": RES})
            sent.clear()
            with app.app_context():
                # customer_type comes from the stored selection, exactly as
                # the enroll ROUTE supplies it.
                stopped = _raises_message(
                    lambda: adapter.create_enrollment(eid3, {
                        "email_address": "e3many@example.com",
                        "customer_type": RES,
                        "utility_accounts": [{}]}))
            check(f"multiple rates, none chosen: UBS STOPS ({stopped[:44]!r})",
                  "savings rate" in (stopped or "").lower())
            check("  ...before any call to Perch", sent == {})
            r = c.post(f"/api/perch/enrollments/{eid3}/savings-rate",
                       headers=rep, json={"savings_percent": 10.0})
            check("  ...the rep chooses", r.status_code == 200)
            enroll(eid3, "e3many@example.com")
            check("  ...and THAT rate is what is sent",
                  sent.get("savings_percent") == "10.0")

            # (d) LMI and LMI_IRA
            eid4 = start("11500", "national-grid-ny", "e3lmi")
            c.post(f"/api/perch/enrollments/{eid4}/program", headers=rep,
                   json={"customer_type": LMI_IRA})
            enroll(eid4, "e3lmi@example.com")
            check("LMI_IRA is sent as LMI_IRA, not LMI",
                  sent.get("customer_type") == "LMI_IRA")
            check("  ...with its own rate", sent.get("savings_percent") == "20.0")

        # (e) v1 sends NEITHER new field
        with api_version(None):
            eid5 = start("10901", "orange-and-rockland", "e3v1")
            c.post(f"/api/perch/enrollments/{eid5}/program", headers=rep,
                   json={"customer_type": LMI})
            enroll(eid5, "e3v1@example.com")
            check("v1: no county on the wire", "county" not in sent)
            check("v1: no savings_percent on the wire",
                  "savings_percent" not in sent)
            check("  ...while customer_type is unchanged",
                  sent.get("customer_type") == "LMI")
            check("  ...and so is everything else that was always sent",
                  sent.get("utility_name") == "orange-and-rockland"
                  and sent.get("zip_code") == "10901")
    finally:
        adapter.get_perch_client = real_client

    print("\n-- K6. the mock's /enroll contract (opt-in) --")
    mock_client.PerchMockClient.enforce_enroll_contract = True
    try:
        with api_version("v2"):
            mc = mock_client.PerchMockClient()
            tok = mc.request_token("e3mock@example.com")["enrollment_token"]

            def call(**over):
                form = {"zip_code": "11100",
                        "utility_name": "national-grid-ny",
                        "customer_type": RES}
                form.update(over)
                try:
                    return "ok", mc.create_enrollment(tok, form, {})
                except PerchValidationError as e:
                    item = e.body_json["errors"][0]
                    return "422", (item["field"], item["code"])

            # THE KEY E3 PROOF: the same request, with and without a county.
            check("county-restricted WITHOUT a county -> Perch refuses",
                  call() == ("422", ("customer_type", "capacity_unavailable")))
            kind, _ = call(county="Albany County")
            check("  ...WITH the county -> Perch accepts", kind == "ok")
            check("a wrong county -> refused again",
                  call(county="Queens County")[1]
                  == ("customer_type", "capacity_unavailable"))
            check("an unknown county -> 422 on county (VA has the list)",
                  mock_client.PerchMockClient().create_enrollment
                  and _mock_422(mc, tok, {"zip_code": "24112",
                                          "utility_name": "dominion-energy-va",
                                          "customer_type": LMI,
                                          "county": "Atlantis"})
                  == ("county", "not_found"))
            check("several rates and none sent -> 422 savings_percent/required",
                  _mock_422(mc, tok, {"zip_code": "11000",
                                      "utility_name": "national-grid-ny",
                                      "customer_type": RES})
                  == ("savings_percent", "required"))
            check("a rate that is not offered -> 422 savings_percent/invalid",
                  _mock_422(mc, tok, {"zip_code": "11000",
                                      "utility_name": "national-grid-ny",
                                      "customer_type": RES,
                                      "savings_percent": "99"})
                  == ("savings_percent", "invalid"))
            for bad in ("101", "-1", "abc"):
                check(f"  ...and {bad!r} too",
                      _mock_422(mc, tok, {"zip_code": "11000",
                                          "utility_name": "national-grid-ny",
                                          "customer_type": RES,
                                          "savings_percent": bad})
                      == ("savings_percent", "invalid"))
            check("a customer type with no capacity -> 422 customer_type",
                  _mock_422(mc, tok, {"zip_code": "11200",
                                      "utility_name": "national-grid-ny",
                                      "customer_type": RES})
                  == ("customer_type", "capacity_unavailable"))
            check("next_step follows the program: LMI_IRA -> proof docs",
                  mc.create_enrollment(tok, {"zip_code": "11500",
                                             "utility_name": "national-grid-ny",
                                             "customer_type": LMI_IRA}, {}
                                       )["next_step"] == "/lmi/proof_docs")
            check("  ...LMI -> self-attestation",
                  mc.create_enrollment(tok, {"zip_code": "11500",
                                             "utility_name": "national-grid-ny",
                                             "customer_type": LMI,
                                             "savings_percent": "10.0"}, {}
                                       )["next_step"] == "/lmi/self_attestation")
            check("  ...Residential -> contracts",
                  mc.create_enrollment(tok, {"zip_code": "12050",
                                             "utility_name": "national-grid-ny",
                                             "customer_type": RES}, {}
                                       )["next_step"] == "/contracts")
    finally:
        mock_client.PerchMockClient.enforce_enroll_contract = False
    check("the mock hook is OFF again",
          mock_client.PerchMockClient.enforce_enroll_contract is False)

    print("\n-- K7. every v2 422 reaches the rep in plain language --")
    documented = [
        ({"field": "savings_percent", "code": "required",
          "message": "is required when more than one savings percent is "
                     "available (5.0, 10.0)"}, "choose the savings rate"),
        ({"field": "savings_percent", "code": "invalid",
          "message": "is not available for this customer type, utility and "
                     "zip code. Fetch the available savings percents using "
                     "the /v2/enrollments/capacity endpoint"},
         "not available for this customer"),
        # E3.1: vendor-neutral. A rep reading this to a customer should not
        # be naming our capacity partner.
        ({"field": "county", "code": "not_found",
          "message": "Atlantis County is not a recognized county in NY"},
         "isn't recognized for this state"),
        ({"field": "customer_type", "code": "capacity_unavailable",
          "message": "Capacity is not available for this customer type, "
                     "utility and zip code. Contact Perch Energy and fetch "
                     "capacity using the /v2/enrollments/capacity endpoint"},
         "no longer has capacity"),
        ({"field": "customer_type", "code": "capacity_unavailable",
          "message": "Only one utility account can be processed when LMI "
                     "capacity is available"}, "only one utility account"),
        ({"field": "customer_type", "code": "invalid",
          "message": "Corporate is not valid"},
         "isn't valid for this enrollment"),
        ({"field": "phone_number", "code": "invalid_format",
          "message": "must be 10 digits"}, "10-digit phone number"),
        ({"field": "zip_code", "code": "not_in_service_area",
          "message": "is not in the service area"}, "outside the service area"),
        ({"field": "business_name", "code": "required",
          "message": "is required for Business customer type"},
         "business name is required"),
    ]
    # Code-shaped tokens and raw field keys. The single English words
    # "required" and "invalid" are deliberately NOT here: the code-default
    # tier writes sentences like "Business name is required for this
    # enrollment", where "required" is the English word doing its ordinary
    # job, not a token printed at a rep. Everything with an underscore is a
    # token by construction and must never appear.
    MACHINE = ("unprocessable_entity", "capacity_unavailable",
               "invalid_format", "not_found", "not_in_service_area",
               "savings_percent", "customer_type", "zip_code",
               "phone_number", "business_name", "utility_name",
               "email_address", "[required]", "[invalid]")
    LEAKED_INSTRUCTIONS = ("Contact Perch Energy", "fetch capacity using",
                           "Fetch the available", "/v2/enrollments",
                           "/capacity endpoint")
    for item, expected in documented:
        text = perch_client._msg(_Err({"errors": [item]}))
        check(f"{item['field']}/{item['code']} -> {text[:50]!r}",
              expected in text.lower())
        leaks = [t for t in MACHINE if t in text]
        check("  ...no machine code or field key in the text", not leaks)
        instr = [t for t in LEAKED_INSTRUCTIONS if t in text]
        check("  ...and none of Perch's endpoint instructions", not instr)

    check("an UNDOCUMENTED field/code still reads as words",
          perch_client._msg(_Err({"errors": [
              {"field": "some_new_field", "code": "brand_new_code",
               "message": "is wrong somehow"}]}))
          == "Some new field: is wrong somehow.")
    check("  ...and a message that was ONLY an instruction still says something",
          "not accepted" in perch_client._msg(_Err({"errors": [
              {"field": "whatever", "code": "x",
               "message": "Contact Perch Energy about this"}]})).lower())

    print("\n-- K8. locking: program, county AND rate --")
    with api_version("v2"):
        eid = start("11000", "national-grid-ny", "e3lock")
        c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
               json={"customer_type": RES})
        c.post(f"/api/perch/enrollments/{eid}/savings-rate", headers=rep,
               json={"savings_percent": 10.0})
        with app.app_context():
            workflow.set_state(eid, "contracts")
        for path, payload in (("program", {"customer_type": LMI}),
                              ("county", {"county": "Albany County"}),
                              ("savings-rate", {"savings_percent": 5.0})):
            r = c.post(f"/api/perch/enrollments/{eid}/{path}", headers=rep,
                       json=payload)
            check(f"{path} cannot change after commit ({r.status_code})",
                  r.status_code == 409)
            check("  ...flagged as committed",
                  r.get_json().get("committed") is True)
        with app.app_context():
            row = query_one("SELECT selected_customer_type, service_county, "
                            "selected_savings_percent FROM enrollments "
                            "WHERE id=?", (eid,))
        check("the committed program is unchanged",
              row["selected_customer_type"] == RES)
        check("the committed rate is unchanged",
              row["selected_savings_percent"] == 10.0)

    print("\n-- K9. a rate cannot outlive the choice it belonged to --")
    with api_version("v2"):
        eid = start("11500", "national-grid-ny", "e3switch")
        c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
               json={"customer_type": LMI})
        c.post(f"/api/perch/enrollments/{eid}/savings-rate", headers=rep,
               json={"savings_percent": 15.0})
        with app.app_context():
            check("the LMI rate is stored",
                  query_one("SELECT selected_savings_percent FROM enrollments "
                            "WHERE id=?", (eid,))["selected_savings_percent"]
                  == 15.0)
        switched = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                          json={"customer_type": LMI_IRA})
        check("switching program reports the rate it cleared",
              switched.get_json().get("cleared_savings_percent") == 15.0)
        with app.app_context():
            check("  ...and the rate is gone, not carried across",
                  query_one("SELECT selected_savings_percent FROM enrollments "
                            "WHERE id=?", (eid,))["selected_savings_percent"]
                  is None)
        body = c.get(f"/api/perch/enrollments/{eid}/programs",
                     headers=rep).get_json()
        check("  ...and nothing stale is reported to the UI",
              body["selected_savings_percent"] is None)

    print("\n-- K10. resume / Save & Exit keeps all three --")
    with api_version("v2"):
        eid = start("11100", "national-grid-ny", "e3resume")
        c.post(f"/api/perch/enrollments/{eid}/county", headers=rep,
               json={"county": "Albany County"})
        c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
               json={"customer_type": RES})
        for i in range(3):
            body = c.get(f"/api/perch/enrollments/{eid}/programs",
                         headers=rep).get_json()
            check(f"reload {i+1}: program preserved",
                  body["selected_customer_type"] == RES)
            check(f"reload {i+1}: county preserved",
                  body["selected_county"] == "Albany County")
        detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
        check("the enrollment detail still opens cleanly",
              detail.get("id") == eid)

    print("\n-- K11. Business stays out --")
    check("Business is not selectable",
          "Business" not in adapter.SELECTABLE_CUSTOMER_TYPES)
    check("  ...and the wizard still collects no business fields",
          all(f not in open(os.path.join(ROOT, "templates", "index.html"),
                            encoding="utf-8").read()
              for f in ('id="business-name"', 'id="business-title"')))
    check("  ...so no capacity shape can make it appear",
          BUSINESS not in types_of([
              entry("mass_market", True, 100, [5.0]),
              entry("commercial_and_industrial", True, 100, [4.0])]))

    print("\n-- K12. self-attestation county: sent only when Perch has none --")
    sa_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                  encoding="utf-8").read()
    check("the entry omits county when one was stored at enroll",
          "if send_county:" in sa_src and 'entry["county"] = county' in sa_src)
    check("  ...decided by the stored capacity SHAPE, not the env flag",
          "shape == cap_module.SHAPE_V2_ENTRIES" in sa_src)
    check("  ...and v1 always sends it, since its schema requires it",
          "_enrolled_county" in sa_src)
    # E3.1: a divergence is now a HARD STOP, not a log line. Covered in L.
    check("a divergence RAISES rather than being logged and carried on",
          "raise PerchValidationError(" in sa_src.split(
              "elif stored_county and county")[1][:1200])


# ═══════════════ L. E3.1 - ONE AUTHORITATIVE COUNTY ═══════════════

def test_authoritative_county():
    section("L. E3.1 - the enrolled county is the only county")

    print("\n-- L1. vendor-neutral wording --")
    county_text = perch_client._msg(_Err({"errors": [
        {"field": "county", "code": "not_found",
         "message": "Atlantis County is not a recognized county in NY"}]}))
    check(f"the county error names no vendor ({county_text!r})",
          "Perch" not in county_text and "perch" not in county_text)
    check("  ...and reads as the approved wording",
          county_text == "That county isn't recognized for this state. "
                         "Please check the selected county and try again.")
    # Nothing a rep can read should name the capacity partner.
    table = perch_client._FIELD_ERROR_TEXT
    offenders = [(k, v) for k, v in table.items() if "Perch" in v]
    check(f"no rep-facing 422 sentence names the vendor ({offenders})",
          not offenders)
    defaults = [(k, v) for k, v in perch_client._CODE_DEFAULT_TEXT.items()
                if "Perch" in v]
    check(f"nor any code-default sentence ({defaults})", not defaults)

    print("\n-- L2. _authoritative_county: v2 with a county, and nothing else --")
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def start(zip_code, utility, tag):
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"{tag}@example.com", "zip_code": zip_code,
                            "utility_name": utility}).get_json()["enrollment_id"]

    def bound(eid):
        with app.app_context():
            row = query_one("SELECT * FROM enrollments WHERE id = ?", (eid,))
            return perch_routes._authoritative_county(row)

    with api_version("v2"):
        eid_locked = start("11100", "national-grid-ny", "e31locked")
        check("before a county is chosen, nothing is bound",
              bound(eid_locked) == (None, False))
        c.post(f"/api/perch/enrollments/{eid_locked}/county", headers=rep,
               json={"county": "Albany County"})
        check("once chosen on a v2 enrollment, it is BOUND",
              bound(eid_locked) == ("Albany County", True))

        eid_free = start("13350", "national-grid-ny", "e31free")
        check("a v2 enrollment with no county binds nothing",
              bound(eid_free) == (None, False))

    with api_version(None):
        eid_v1 = start("10901", "orange-and-rockland", "e31v1")
        # Force a county onto a v1 enrollment the way stale state might.
        with app.app_context():
            execute("UPDATE enrollments SET service_county = ? WHERE id = ?",
                    ("Albany County", eid_v1))
        county, locked = bound(eid_v1)
        check(f"a county on a V1 enrollment is NOT bound ({county!r})",
              county == "Albany County" and locked is False)
        check("  ...because v1 /enroll has no county field, so Perch has none",
              True)

    print("\n-- L3. the reference endpoint locks the county --")
    def reference(eid):
        return c.get(
            f"/api/perch/enrollments/{eid}/lmi/self-attestation/reference",
            headers=rep).get_json()

    with api_version("v2"):
        ref = reference(eid_locked)
        check("the bound county is reported", ref.get("county") == "Albany County")
        check("  ...and flagged as locked", ref.get("county_locked") is True)
        check("  ...with the list collapsed to it alone",
              ref.get("counties") == ["Albany County"])

        ref_free = reference(eid_free)
        check("an unbound v2 enrollment is NOT locked",
              ref_free.get("county_locked") is False)
        check("  ...and still gets the full controlled list",
              len(ref_free.get("counties") or []) > 1)

    with api_version(None):
        ref_v1 = reference(eid_v1)
        check("v1 is never locked", ref_v1.get("county_locked") is False)
        check("  ...and keeps the full list, exactly as before",
              len(ref_v1.get("counties") or []) > 1)

    print("\n-- L4. the submit path refuses a conflicting county --")
    def attest(eid, county, occupancy=2, status="accepted"):
        return c.post(
            f"/api/perch/enrollments/{eid}/lmi/self-attestation", headers=rep,
            json={"occupancy": occupancy, "county": county, "status": status})

    with api_version("v2"):
        r = attest(eid_locked, "Queens County")
        check(f"a DIFFERENT county is refused ({r.status_code})",
              r.status_code == 409)
        body = r.get_json()
        check("  ...flagged as a county conflict",
              body.get("county_conflict") is True)
        check("  ...naming the county the enrollment is bound to",
              body.get("enrolled_county") == "Albany County")
        check("  ...with a rep-facing message, no vendor name",
              "Albany County" in body["error"]
              and "Perch" not in body["error"])
        check("  ...and nothing about two counties being merged",
              "Queens" not in body["error"])
        with app.app_context():
            check("  ...and NO attestation row was written",
                  query_one("SELECT COUNT(*) n FROM "
                            "perch_self_attestation_submissions "
                            "WHERE enrollment_id = ?", (eid_locked,))["n"] == 0)
            check("  ...and the conflict is audited",
                  query_one("SELECT COUNT(*) n FROM audit_logs WHERE "
                            "action = 'self_attestation_county_conflict' AND "
                            "enrollment_id = ?", (eid_locked,))["n"] == 1)

    print("\n-- L5. the bound county is what gets used --")
    with api_version("v2"):
        # The matching county passes the county gate, and so does sending NONE
        # at all - which is exactly what the locked control produces, since a
        # disabled <select> submits nothing. Both then stop at the next
        # validation (this fixture enrollment has no utility account yet),
        # which is what proves the county stage was cleared rather than that
        # the whole request succeeded.
        for sent in ("Albany County", "albany county", ""):
            r = attest(eid_locked, sent)
            body = r.get_json() or {}
            check(f"county={sent!r} clears the county gate ({r.status_code})",
                  r.status_code != 409
                  and body.get("county_conflict") is not True)
            check("  ...stopping later, on something that is not the county",
                  "county" not in (body.get("error") or "").lower())
        # And the gate is specific: only a DIFFERENT county is refused.
        check("a different county is still the only thing refused",
              attest(eid_locked, "Queens County").status_code == 409)

    print("\n-- L6. the adapter refuses to send two counties --")
    # Unreachable through the route now; this is the backstop for a caller
    # that bypasses it - legacy state, a half-migrated record, a future path.
    with api_version("v2"):
        with app.app_context():
            account = query_one(
                "SELECT ua.account_number FROM utility_accounts ua JOIN "
                "enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
                (eid_locked,))
            raised = _raises_message(
                lambda: adapter.submit_self_attestation(
                    eid_locked,
                    (account or {}).get("account_number") or "1234567890",
                    2, "Queens County", "accepted"))
        check(f"a conflicting county RAISES ({(raised or '')[:48]!r})",
              raised is not None and "Albany County" in raised)
        check("  ...rather than logging and carrying on",
              "has to use that county" in (raised or ""))

    print("\n-- L7. the county survives Save & Exit / resume --")
    with api_version("v2"):
        for i in range(3):
            check(f"reload {i+1}: the programs endpoint still reports it",
                  c.get(f"/api/perch/enrollments/{eid_locked}/programs",
                        headers=rep).get_json()["selected_county"]
                  == "Albany County")
            check(f"reload {i+1}: the attestation reference still locks it",
                  reference(eid_locked)["county"] == "Albany County")
            check(f"reload {i+1}: _authoritative_county agrees",
                  bound(eid_locked) == ("Albany County", True))
        with app.app_context():
            check("  ...and it is the same value on the enrollment row",
                  query_one("SELECT service_county FROM enrollments WHERE id=?",
                            (eid_locked,))["service_county"] == "Albany County")

    print("\n-- L8. no second chance to change it after commit --")
    with api_version("v2"):
        with app.app_context():
            workflow.set_state(eid_locked, "contracts")
        r = c.post(f"/api/perch/enrollments/{eid_locked}/county", headers=rep,
                   json={"county": "Queens County"})
        check(f"the county endpoint refuses after commit ({r.status_code})",
              r.status_code == 409)
        check("  ...and it is still bound to the original",
              bound(eid_locked) == ("Albany County", True))

    print("\n-- L9. v1 self-attestation is untouched --")
    with api_version(None):
        ref_v1 = reference(eid_v1)
        check("v1 still offers the full county list",
              len(ref_v1["counties"]) > 1)
        check("  ...and reports no lock", ref_v1["county_locked"] is False)
        sa_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                      encoding="utf-8").read()
        check("v1 still SENDS the county, as its schema requires",
              "if send_county:" in sa_src
              and 'entry["county"] = county' in sa_src)
        check("  ...and the lock is decided by the v2 capacity shape alone",
              "shape == cap_module.SHAPE_V2_ENTRIES" in sa_src)

    print("\n-- L10. the UI cannot offer a second county either --")
    check("the locked county renders as the only option",
          "ref.county_locked && ref.county" in JS
          and "ctySel.disabled = true" in JS)
    check("  ...and the income limits are fetched for THAT county",
          "ref0.county_locked && ref0.county" in JS)
    check("  ...with the reason shown to the rep",
          "sa-county-help" in JS
          and "so the income limit is checked for that county" in JS)
    check("  ...and the helper element exists, hidden by default",
          'id="sa-county-help"' in open(
              os.path.join(ROOT, "templates", "index.html"),
              encoding="utf-8").read())


# ═══════════ M. E4 - THE REAL VIRGINIA CAPACITY RESPONSE ═══════════

# The EXACT v2 capacity Perch staging returned for Dominion / ZIP 22401 on
# 2026-10-28 (E4 read-only pass). Not a guess: this shape is why the fix
# below exists, so it is pinned here verbatim.
REAL_VA_STAGING = [
    {"customer_type": "residential", "county_restriction": None,
     "capacity_available": False, "capacity_available_kw": 0,
     "savings_percents": []},
    {"customer_type": "lmi", "county_restriction": None,
     "capacity_available": True, "capacity_available_kw": 1200,
     "savings_percents": [10.0]},
    {"customer_type": "lmi_ira", "county_restriction": None,
     "capacity_available": False, "capacity_available_kw": 0,
     "savings_percents": []},
    {"customer_type": "mass_market", "county_restriction": None,
     "capacity_available": True, "capacity_available_kw": 3400,
     "savings_percents": [10.0]},
    {"customer_type": "commercial_and_industrial", "county_restriction": None,
     "capacity_available": False, "capacity_available_kw": 0,
     "savings_percents": []},
]


def test_virginia_is_income_qualified_only():
    section("M. E4 - Virginia offers income-qualified programs only")

    print("\n-- M1. the real staging response, mapped for a VA rep --")
    progs = adapter.available_programs(REAL_VA_STAGING, market_state="VA")
    shown = [(p["customer_type"], tuple(p["savings_percents"])) for p in progs]
    check(f"a VA rep is offered exactly one program ({shown})",
          shown == [(LMI, (10.0,))])
    check("  ...which is the income-qualified one, at the published 10%",
          progs[0]["customer_type"] == LMI
          and progs[0]["savings_percents"] == [10.0])
    check("  ...labelled for a rep", progs[0]["label"] == "Residential LMI")
    check("  ...and NOT flagged unresolved - 10% is its own published rate",
          progs[0]["savings_unresolved"] is False
          and progs[0]["savings_source"] == cap.LMI)

    print("\n-- M2. the non-LMI Residential program is GONE --")
    check("Residential is not offered in VA",
          RES not in [p["customer_type"] for p in progs])
    # The server-side refusal, through the path production actually uses: a
    # model annotated with its market, which is what capacity_model_for()
    # hands every caller.
    va_model = adapter.capacity_model_for(
        {"zip_code": "22401", "capacity_available": True,
         "capacity_entries": cap.parse(REAL_VA_STAGING).as_dict()})
    check("  ...and cannot be selected by a tampered client",
          _raises(lambda: adapter.resolve_customer_type(va_model,
                                                        requested=RES)))
    check("  ...while LMI resolves normally",
          adapter.resolve_customer_type(va_model, requested=LMI)[0] == LMI)
    check("  ...and with nothing requested, LMI is the only option",
          adapter.resolve_customer_type(va_model)[0] == LMI)
    # Residential was previously offered with NO rate, which is the shape that
    # made the bug visible: a program a rep could pick and never quote.
    unrestricted = adapter.available_programs(REAL_VA_STAGING)
    check("  ...whereas with no market known it WAS offered, rateless "
          f"({[(p['customer_type'], p['savings_percents']) for p in unrestricted]})",
          RES in [p["customer_type"] for p in unrestricted]
          and unrestricted[0]["savings_unresolved"] is True)

    print("\n-- M3. Perch's capacity model is untouched --")
    model = cap.parse(REAL_VA_STAGING)
    check("mass_market is still recorded as AVAILABLE",
          model.has_capacity(cap.MASS_MARKET) is True)
    check("  ...with its real rate preserved",
          model.savings_percents_for(cap.MASS_MARKET) == [10.0])
    check("  ...and its real kW", model.kw_for(cap.MASS_MARKET) == 3400)
    check("residential is still recorded as unavailable",
          model.has_capacity(cap.RESIDENTIAL) is False)
    check("all five entries survive",
          len(model.as_dict()["entries"]) == 5)
    check("  ...and the stored shape carries no market annotation",
          "market_state" not in model.as_dict())

    print("\n-- M4. mass market still means Residential everywhere else --")
    # The exact NY shape this rule must not disturb: Central Hudson and
    # Orange & Rockland return residential=false with mass_market=true and
    # are standard residential enrollments.
    ny_mass_market = [entry("residential", False, 0, []),
                      entry("mass_market", True, 4200, [5.0])]
    for market in ("NY", None, "MA"):
        got = [p["customer_type"] for p in
               adapter.available_programs(ny_mass_market, market_state=market)]
        check(f"market={market!r}: mass market still offers Residential ({got})",
              got == [RES])

    print("\n-- M5. the rule is configuration, and names its markets --")
    check("VA is restricted by default",
          market_products.is_income_qualified_only("VA") is True)
    check("  ...case-insensitively",
          market_products.is_income_qualified_only("va") is True
          and market_products.is_income_qualified_only(" Va ") is True)
    for other in ("NY", "ny", "MA", "", None, "XX", "V", "VAA"):
        check(f"{other!r} is NOT restricted",
              market_products.is_income_qualified_only(other) is False)
    saved = os.environ.get("UBS_LMI_ONLY_MARKETS")
    try:
        os.environ["UBS_LMI_ONLY_MARKETS"] = "MD, va"
        check("the set is configurable",
              market_products.income_qualified_only_markets()
              == frozenset({"MD", "VA"}))
        os.environ["UBS_LMI_ONLY_MARKETS"] = ""
        check("  ...and an explicitly empty value removes every restriction",
              market_products.income_qualified_only_markets() == frozenset())
        check("  ...which lets VA offer Residential again, by configuration",
              RES in [p["customer_type"] for p in adapter.available_programs(
                  REAL_VA_STAGING, market_state="VA")])
    finally:
        if saved is None:
            os.environ.pop("UBS_LMI_ONLY_MARKETS", None)
        else:
            os.environ["UBS_LMI_ONLY_MARKETS"] = saved
    check("the default is restored after the override",
          market_products.is_income_qualified_only("VA") is True)

    print("\n-- M6. no state-name branch entered the request path --")
    import ast as _ast
    for rel in (("services", "perch", "adapter.py"),
                ("routes", "perch_routes.py"),
                ("services", "perch", "markets.py")):
        code = _ast.unparse(_ast.parse(
            open(os.path.join(ROOT, *rel), encoding="utf-8").read()))
        check(f"{rel[-1]} still compares no state name",
              '"VA"' not in code and "'VA'" not in code)
    adapter_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                       encoding="utf-8").read()
    check("the rule is ASKED, not inlined",
          "market_products.is_income_qualified_only(" in adapter_src)
    check("  ...and applied in ONE place both consumers share",
          adapter_src.count("market_products.is_income_qualified_only(") == 1
          and "def offerable_customer_types(" in adapter_src)
    wf_src = open(os.path.join(ROOT, "services", "perch", "workflow.py"),
                  encoding="utf-8").read()
    check("  ...which the availability panel uses too",
          "adapter.offerable_customer_types(" in wf_src)

    print("\n-- M7. the market reaches the model through hydration --")
    model = adapter.capacity_model_for(
        {"zip_code": "22401", "capacity_available": True,
         "capacity_entries": cap.parse(REAL_VA_STAGING).as_dict()})
    check("a VA ZIP annotates the model", model.market_state == "VA")
    check("  ...so every caller inherits the rule with no argument",
          [p["customer_type"] for p in adapter.available_programs(model)]
          == [LMI])
    ny_model = adapter.capacity_model_for(
        {"zip_code": "12202", "capacity_available": True,
         "capacity_entries": cap.parse(ny_mass_market).as_dict()})
    check("an NY ZIP annotates NY", ny_model.market_state == "NY")
    check("  ...and still offers Residential",
          [p["customer_type"] for p in adapter.available_programs(ny_model)]
          == [RES])
    unknown = adapter.capacity_model_for(
        {"zip_code": "00000", "capacity_available": True,
         "capacity_entries": cap.parse(REAL_VA_STAGING).as_dict()})
    check("an unresolvable ZIP annotates nothing",
          unknown.market_state is None)
    check("  ...and carries NO restriction - an unknown market is not guessed at",
          RES in [p["customer_type"] for p in
                  adapter.available_programs(unknown)])

    print("\n-- M8. end to end: a VA rep sees one program --")
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    with api_version("v2"):
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": "e4va@example.com", "zip_code": "24112",
                           "utility_name": "dominion-energy-va"}
                     ).get_json()["enrollment_id"]
        body = c.get(f"/api/perch/enrollments/{eid}/programs",
                     headers=rep).get_json()
        offered = [p["customer_type"] for p in body["available_programs"]]
        check(f"the programs endpoint offers LMI only ({offered})",
              offered == [LMI])
        check("  ...with no selection prompt - there is one option",
              body["selection_required"] is False)
        check("selecting LMI works",
              c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                     json={"customer_type": LMI}).status_code == 200)
        check("  ...and selecting Residential is REFUSED",
              c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                     json={"customer_type": RES}).status_code == 400)
        # The availability panel must agree with the cards.
        wf = c.get(f"/api/perch/enrollments/{eid}/workflow",
                   headers=rep).get_json()
        labels = [s["label"] for s in wf["step"]["panels"][0]["segments"]]
        check(f"the availability panel shows no Residential row ({labels})",
              "Residential" not in labels)
        metrics = [m["label"] for m in wf["step"]["panels"][0]["metrics"]]
        check(f"  ...and quotes only the LMI rate ({metrics})",
              metrics == ["Residential LMI savings"])

    print("\n-- M9. New York is completely unaffected --")
    with api_version("v2"):
        eid_ny = c.post("/api/perch/enrollments/capacity", headers=rep,
                        json={"email": "e4ny@example.com", "zip_code": "13350",
                              "utility_name": "national-grid-ny"}
                        ).get_json()["enrollment_id"]
        offered_ny = [p["customer_type"] for p in
                      c.get(f"/api/perch/enrollments/{eid_ny}/programs",
                            headers=rep).get_json()["available_programs"]]
        check(f"NY still offers all three ({offered_ny})",
              offered_ny == [RES, LMI, LMI_IRA])
        eid_ch = c.post("/api/perch/enrollments/capacity", headers=rep,
                        json={"email": "e4ch@example.com", "zip_code": "12401",
                              "utility_name": "central-hudson-gas-electric"}
                        ).get_json()["enrollment_id"]
        offered_ch = [p["customer_type"] for p in
                      c.get(f"/api/perch/enrollments/{eid_ch}/programs",
                            headers=rep).get_json()["available_programs"]]
        check(f"NY mass-market-only still offers Residential ({offered_ch})",
              offered_ch == [RES])

    print("\n-- M10. v1 is unchanged --")
    with api_version(None):
        for prefix, expected in (("241", [LMI]), ("109", [RES, LMI])):
            got = [p["customer_type"] for p in adapter.available_programs(
                _FIXTURES[prefix]["project_details"])]
            check(f"v1 fixture {prefix} -> {got}", got == expected)
        eid_v1 = c.post("/api/perch/enrollments/capacity", headers=rep,
                        json={"email": "e4v1@example.com", "zip_code": "24112",
                              "utility_name": "dominion-energy-va"}
                        ).get_json()["enrollment_id"]
        offered_v1 = [p["customer_type"] for p in
                      c.get(f"/api/perch/enrollments/{eid_v1}/programs",
                            headers=rep).get_json()["available_programs"]]
        check(f"v1 VA still resolves as it always did ({offered_v1})",
              offered_v1 == [LMI])


class _Err:
    status_code = 422
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        self._b = body
        self.text = json.dumps(body)

    def json(self):
        return self._b


def _raises(fn):
    try:
        fn()
        return False
    except PerchValidationError:
        return True


def _raises_message(fn):
    try:
        fn()
        return None
    except PerchValidationError as e:
        return str(e)


def _mock_422(mc, tok, form):
    try:
        mc.create_enrollment(tok, form, {})
        return None
    except PerchValidationError as e:
        item = e.body_json["errors"][0]
        return (item["field"], item["code"])


def _rejects_county(details, requested, county):
    try:
        adapter.resolve_customer_type(details, requested=requested,
                                      county=county)
        return False
    except PerchValidationError:
        return True


def main():
    test_v1_unchanged()
    test_v2_choices()
    test_unavailable_never_selectable()
    test_county_restrictions()
    test_savings_rates()
    test_lmi_vs_lmi_ira()
    test_no_raw_vocabulary()
    test_selection_locks()
    test_persistence()
    test_county_field()
    test_enroll_payload()
    test_authoritative_county()
    test_virginia_is_income_qualified_only()
    print("\nAll E2 / E2.1 / E3 / E3.1 / E4 checks passed.")


if __name__ == "__main__":
    main()
