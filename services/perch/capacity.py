"""
The internal capacity model. ONE shape, both Perch API versions.

WHY THIS MODULE EXISTS

Perch changed the shape of the capacity response, not just its contents:

    v1  project_details = {                      # ONE flat object
          "residential_capacity_available": bool,
          "small_commercial_capacity_available": bool,
          "lmi_capacity_available": bool,
          "proof_documents_required": bool,
          "savings_percent_for_residential_and_commercial_customers": num|null,
          "savings_percent_for_lmi_customers": num|null,
        }

    v2  project_details = [                      # an ARRAY of entries
          {"customer_type": "residential",
           "county_restriction": ["Aspen", "Boulder"] | null,
           "capacity_available": bool,
           "capacity_available_kw": num,
           "savings_percents": [5.0, 10.0]},
          ...one entry per customer type AND county restriction...
        ]

Three things changed at once, and each of them breaks an assumption UBS had
baked into the flat shape:

  1. ONE ENTRY PER TYPE became MANY. A type can appear several times, once per
     distinct set of county restrictions.
  2. ONE SCALAR SAVINGS RATE became AN ARRAY. A type can offer several rates,
     or none. `savings_percent_for_..._customers` has no v2 equivalent.
  3. TWO LMI TYPES. v1 had `lmi_capacity_available` plus a
     `proof_documents_required` flag; v2 has `lmi` and `lmi_ira` as separate
     capacity types, and `lmi_ira` is the one that carries the proof-document
     flow.

Rather than teach every caller both shapes, both are parsed HERE into one
entry model, and everything downstream reads only that. v1 is synthesized into
entries so there is no second code path to keep in step: the adapter, the
workflow renderer and the routes see entries whichever version answered.

WHAT THIS MODULE DOES NOT DO

It does not decide which UBS program a rep may choose - that mapping is Perch
business vocabulary translated into Dalton's, and it lives in adapter.py. This
module only reports what Perch said, in a shape that can be asked questions.

It also does not select a project. Perch selects the project at POST /enroll,
from the customer_type (and, from E3, the county and savings_percent) UBS
sends. Nothing here picks one.
"""

# ─────────────── v2 capacity types, spelled exactly as the spec spells them ───────────────
# These strings are Perch's vocabulary. They are matched against the wire and
# used as dictionary keys; they are NEVER shown to a rep. adapter.py owns the
# rep-facing labels.
RESIDENTIAL = "residential"
LMI = "lmi"
LMI_IRA = "lmi_ira"
MASS_MARKET = "mass_market"
COMMERCIAL_AND_INDUSTRIAL = "commercial_and_industrial"

CAPACITY_TYPES = (RESIDENTIAL, LMI, LMI_IRA, MASS_MARKET, COMMERCIAL_AND_INDUSTRIAL)

# Which wire shape a model was parsed from. Recorded rather than inferred,
# because "v1 said no capacity" and "this response was not capacity at all"
# must stay distinguishable in the audit record.
SHAPE_V2_ENTRIES = "v2_entries"
SHAPE_V1_FLAT = "v1_flat"
SHAPE_EMPTY = "empty"
SHAPE_UNKNOWN = "unknown"

# The v1 fields, kept here so the synthesis below is readable and so a reader
# can see exactly which v1 field became which entry.
V1_FIELD_RESIDENTIAL = "residential_capacity_available"
V1_FIELD_SMALL_COMMERCIAL = "small_commercial_capacity_available"
V1_FIELD_LMI = "lmi_capacity_available"
V1_FIELD_PROOF_DOCS = "proof_documents_required"
V1_FIELD_SAVINGS_RES_COMMERCIAL = (
    "savings_percent_for_residential_and_commercial_customers")
V1_FIELD_SAVINGS_LMI = "savings_percent_for_lmi_customers"


def _percent(value):
    """A savings rate as a float, or None.

    Zero is treated as "not published", NOT as a zero-percent offer. That is
    the pre-existing UBS rule (adapter._percent has worked this way since
    Milestone 2) and the mock relies on it: a fixture with
    savings_percent_for_lmi_customers = 0 renders as "Savings not published",
    not "0% savings". Changing that here would silently alter every v1 screen.
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _percent_list(values):
    """A savings_percents array: distinct, numeric, sorted ascending.

    Perch documents the array as already sorted ascending, but it is sorted
    again here - a response is data, not a promise, and downstream code
    compares these lists for equality.
    """
    out = []
    for v in values or []:
        num = _percent(v)
        if num is not None and num not in out:
            out.append(num)
    return sorted(out)


def _counties(value):
    """A county_restriction as a tuple of names, or None for unrestricted.

    `null` means "no county restriction" - it accepts a customer in ANY county.
    An empty array is NOT the same thing on the wire, but Perch does not
    document one, so it is treated as unrestricted with the shape recorded: a
    restriction listing no counties could otherwise exclude every customer.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    names = tuple(str(c).strip() for c in value if str(c).strip())
    return names or None


def _entry(capacity_type, county_restriction, available, kw, savings):
    return {
        "capacity_type": capacity_type,
        "county_restriction": county_restriction,
        "capacity_available": bool(available),
        "capacity_available_kw": kw,
        "savings_percents": savings,
    }


def _unavailable_entry(capacity_type):
    """A type with no capacity.

    The v2 spec: "Every customer type is always present: one without capacity
    appears once, unrestricted, with false, 0 and []." The v1 synthesis below
    fills in the same way, so a caller never has to tell "absent" from
    "present and unavailable" - on both versions every type is present.
    """
    return _entry(capacity_type, None, False, 0, [])


class CapacityModel:
    """What Perch said about capacity, in a shape that can be asked questions.

    Read-only. Built by parse(); never mutated afterwards.
    """

    def __init__(self, entries, shape, v1_proof_documents_required=None,
                 v1_residential_commercial_savings=None):
        self.entries = list(entries)
        self.shape = shape
        # v1 ONLY. v1 carried a single proof_documents_required flag alongside
        # one LMI type; v2 replaced it with the lmi_ira capacity type. Kept so
        # v1 enrollments keep behaving exactly as they did, and None on v2 so
        # no caller can mistake its absence for False.
        self.v1_proof_documents_required = v1_proof_documents_required
        # v1 ONLY, and for the same reason. v1 reports ONE savings field,
        # `savings_percent_for_residential_and_commercial_customers`, which its
        # own spec says covers residential AND commercial customers. It is not
        # the rate of a particular capacity type, so it is carried here rather
        # than attached to one entry and borrowed by another - which is what a
        # "fallback" would be. None on v2, where every type states its own
        # rates and nothing is shared.
        self.v1_residential_commercial_savings = (
            list(v1_residential_commercial_savings or [])
            if shape == SHAPE_V1_FLAT else None)
        # WHICH MARKET this capacity belongs to, as a two-letter state, or
        # None when it is not known.
        #
        # NOT PERCH DATA, and deliberately NOT serialized by as_dict(): it is
        # a UBS-side annotation set at hydration (adapter.capacity_model_for)
        # so that product rules keyed on the market - which programs may be
        # offered here - can be applied without every call site having to
        # remember to pass it. Nothing about the entries changes; this says
        # where they came from, not what they say.
        self.market_state = None

    # ─────────────── queries ───────────────

    def entries_for(self, capacity_type, county=None, include_restricted=False):
        """The AVAILABLE entries of one type that could serve a customer.

        The three cases are deliberately different, because Perch's own rules
        differ by whether a county is known:

        county given
            unrestricted entries PLUS entries whose county_restriction lists
            that county. This is the enroll-time rule: "read the entries whose
            county_restriction contains that county, together with the
            unrestricted entries."

        county None, include_restricted False
            unrestricted entries only. This is what Perch will actually accept
            when no county is sent: "Without it, only capacity with no county
            restriction is available."

        county None, include_restricted True
            every available entry of the type. This is for the AVAILABILITY
            screen, which runs before a county is known. Hiding a
            county-restricted entry there would tell a rep in a restricted
            county that nothing is available, when something may well be.
        """
        wanted = (capacity_type or "").strip().lower()
        want_county = (county or "").strip().lower() or None
        out = []
        for e in self.entries:
            if e["capacity_type"] != wanted or not e["capacity_available"]:
                continue
            counties = e["county_restriction"]
            if counties is None:
                out.append(e)
            elif want_county is not None:
                if any(c.strip().lower() == want_county for c in counties):
                    out.append(e)
            elif include_restricted:
                out.append(e)
        return out

    def has_capacity(self, capacity_type, county=None, include_restricted=True):
        """Whether a type has any capacity that could serve a customer.

        include_restricted defaults to True here, the opposite of
        entries_for(): the question "is anything available?" is asked by the
        availability screen, before a county exists.
        """
        return bool(self.entries_for(capacity_type, county=county,
                                    include_restricted=include_restricted))

    def savings_percents_for(self, capacity_type, county=None,
                             include_restricted=True):
        """Every distinct rate offered by the applicable entries of one type.

        The union across entries, not one entry's list: with a county given,
        a customer may be served by a restricted entry or an unrestricted one,
        and both rates are genuinely on offer. Returned sorted ascending and
        never collapsed to a single value - callers that want one must say so.
        """
        rates = []
        for e in self.entries_for(capacity_type, county=county,
                                  include_restricted=include_restricted):
            for r in e["savings_percents"]:
                if r not in rates:
                    rates.append(r)
        return sorted(rates)

    def kw_for(self, capacity_type, county=None, include_restricted=True):
        """Total kW across the applicable entries, or None when unknown.

        None, not 0, when no entry reports a figure: v1 never sent kW at all,
        and a synthesized 0 would read as "available, but with no capacity".
        """
        total = None
        for e in self.entries_for(capacity_type, county=county,
                                  include_restricted=include_restricted):
            kw = e["capacity_available_kw"]
            if kw is None:
                continue
            try:
                total = (total or 0) + float(kw)
            except (TypeError, ValueError):
                continue
        return total

    def counties_for(self, capacity_type):
        """Every county named by the restricted entries of one type.

        For diagnostics and for E3's county matching. Unrestricted entries
        contribute nothing - they are not restricted TO anywhere.
        """
        names = []
        for e in self.entries:
            if e["capacity_type"] != capacity_type or not e["capacity_available"]:
                continue
            for c in (e["county_restriction"] or ()):
                if c not in names:
                    names.append(c)
        return sorted(names)

    def has_county_restrictions(self):
        """Whether ANY available entry is restricted to counties.

        When this is False the county question does not arise for this
        location at all, which keeps the availability screen quiet in the
        common case.
        """
        return any(e["capacity_available"] and e["county_restriction"]
                   for e in self.entries)

    def any_capacity(self):
        return any(e["capacity_available"] for e in self.entries)

    # ─────────────── serialization ───────────────

    def as_entry_list(self):
        """The entries as plain JSON-safe dicts, county tuples as lists."""
        return [{
            "capacity_type": e["capacity_type"],
            "county_restriction": (list(e["county_restriction"])
                                   if e["county_restriction"] else None),
            "capacity_available": e["capacity_available"],
            "capacity_available_kw": e["capacity_available_kw"],
            "savings_percents": list(e["savings_percents"]),
        } for e in self.entries]

    def as_dict(self):
        return {
            "shape": self.shape,
            "entries": self.as_entry_list(),
            "v1_proof_documents_required": self.v1_proof_documents_required,
            "v1_residential_commercial_savings":
                self.v1_residential_commercial_savings,
        }


# ─────────────── parsing ───────────────

def parse(project_details):
    """Parse EITHER shape of project_details into one CapacityModel.

    A list is v2. A dict is v1. Anything else - None, a string, a number -
    yields an empty model rather than raising: a malformed capacity response
    must surface as "no capacity" on the availability screen, not as a 500 in
    the middle of a rep's enrollment.
    """
    if isinstance(project_details, list):
        return _parse_v2_entries(project_details)
    if isinstance(project_details, dict):
        return _parse_v1_flat(project_details)
    if project_details is None:
        return CapacityModel([], SHAPE_EMPTY)
    return CapacityModel([], SHAPE_UNKNOWN)


def _parse_v2_entries(items):
    """v2: the array, as sent.

    Entries of a type Perch adds later are KEPT, not dropped. An unknown type
    cannot become a program (adapter.py maps only the types it knows), so
    keeping it costs nothing and means the stored model and the audit record
    stay a faithful copy of the response.
    """
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("customer_type") or "").strip().lower()
        if not ctype:
            continue
        entries.append(_entry(
            ctype,
            _counties(item.get("county_restriction")),
            item.get("capacity_available"),
            item.get("capacity_available_kw"),
            _percent_list(item.get("savings_percents")),
        ))

    # The spec guarantees every customer type is present. Trusting that
    # guarantee would make has_capacity() depend on Perch's completeness, so
    # any missing type is filled in as explicitly unavailable.
    present = {e["capacity_type"] for e in entries}
    for ctype in CAPACITY_TYPES:
        if ctype not in present:
            entries.append(_unavailable_entry(ctype))

    return CapacityModel(entries, SHAPE_V2_ENTRIES)


def _parse_v1_flat(details):
    """v1: the flat object, synthesized into entries.

    The mapping, field by field:

        residential_capacity_available       -> residential
        small_commercial_capacity_available  -> mass_market
        lmi_capacity_available               -> lmi
        lmi_ira                              -> ALWAYS unavailable
        commercial_and_industrial            -> ALWAYS unavailable

    Two of those deserve saying out loud.

    small_commercial -> mass_market. The v2 spec names the v1 field for us:
    "small_commercial_capacity_available: Whether MASS MARKET capacity is
    available for this utility and ZIP." It was never a separate small-business
    product. The UBS rule that this capacity serves a residential enrollment is
    unchanged and lives in adapter.py, where it always has.

    lmi_ira is ALWAYS unavailable on v1. v1 cannot express IRA LMI as a
    selectable capacity type - it has one LMI flag plus a
    proof_documents_required boolean - and the spec is explicit that when the
    project was already selected by v1 capacity, "LMI_IRA is treated as LMI".
    So on v1 the rep is offered LMI and never LMI_IRA, exactly as before, and
    proof_documents_required is carried on the model instead of being turned
    into a capacity type it is not.

    No county restrictions: v1 reports none, so every synthesized entry is
    unrestricted. kW is None rather than 0 - v1 never sent a figure, and 0
    would read as "available with no capacity".
    """
    res_savings = _percent(details.get(V1_FIELD_SAVINGS_RES_COMMERCIAL))
    lmi_savings = _percent(details.get(V1_FIELD_SAVINGS_LMI))

    # v1 returns residential and commercial savings TOGETHER in one field, so
    # the same rate is correct for both synthesized entries. It is not a
    # fallback between two programs: it is one field that covers both.
    res_list = [res_savings] if res_savings is not None else []
    lmi_list = [lmi_savings] if lmi_savings is not None else []

    entries = [
        _entry(RESIDENTIAL, None, details.get(V1_FIELD_RESIDENTIAL), None,
               res_list if details.get(V1_FIELD_RESIDENTIAL) else []),
        _entry(MASS_MARKET, None, details.get(V1_FIELD_SMALL_COMMERCIAL), None,
               res_list if details.get(V1_FIELD_SMALL_COMMERCIAL) else []),
        _entry(LMI, None, details.get(V1_FIELD_LMI), None,
               lmi_list if details.get(V1_FIELD_LMI) else []),
        _unavailable_entry(LMI_IRA),
        _unavailable_entry(COMMERCIAL_AND_INDUSTRIAL),
    ]

    proof = details.get(V1_FIELD_PROOF_DOCS)
    return CapacityModel(entries, SHAPE_V1_FLAT,
                         v1_proof_documents_required=(
                             None if proof is None else bool(proof)),
                         # The one field that covers residential AND
                         # commercial on v1. Carried on the model so the
                         # program mapping can read it as what it is - a
                         # shared rate - instead of borrowing one entry's
                         # rates for another entry's program.
                         v1_residential_commercial_savings=res_list)


def parse_stored(stored):
    """Rebuild a model from what persistence stored (adapter hydration).

    Accepts the as_dict() form, a bare entry list, or None.

    THE STORED SHAPE IS NOT THE WIRE SHAPE, and that distinction is the whole
    reason this is a separate function. A wire entry names its type
    `customer_type`, because that is what Perch calls it; a stored entry names
    it `capacity_type`, because by then it has been normalized and UBS's
    `customer_type` means something different (the value POST /enroll takes -
    `Residential`, `LMI`, `LMI_IRA`, `Business`).

    An earlier version of this reused _parse_v2_entries() for both. It silently
    dropped EVERY stored entry - each one had no `customer_type` key, so each
    was skipped, and the "every type is always present" fill-in then rebuilt
    them all as unavailable. The result was a capacity check that round-tripped
    to "no capacity available anywhere", which read like a Perch answer rather
    than a parsing bug. Hence one parser per shape, and a round-trip test.
    """
    if isinstance(stored, dict) and "entries" in stored:
        model = _parse_stored_entries(stored.get("entries") or [])
        model.shape = stored.get("shape") or model.shape
        model.v1_proof_documents_required = stored.get(
            "v1_proof_documents_required")
        # Restored only for a v1-shaped model; on v2 it must stay None, or a
        # stored stray value would become a rate nobody quoted.
        model.v1_residential_commercial_savings = (
            list(stored.get("v1_residential_commercial_savings") or [])
            if model.shape == SHAPE_V1_FLAT else None)
        return model
    if isinstance(stored, list):
        return _parse_stored_entries(stored)
    return CapacityModel([], SHAPE_EMPTY)


def _parse_stored_entries(items):
    """Entries as as_entry_list() wrote them: `capacity_type`, counties as a
    list. Tolerates `customer_type` as well, so a hand-written fixture or an
    older stored row is read rather than silently discarded."""
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("capacity_type")
                    or item.get("customer_type") or "").strip().lower()
        if not ctype:
            continue
        entries.append(_entry(
            ctype,
            _counties(item.get("county_restriction")),
            item.get("capacity_available"),
            item.get("capacity_available_kw"),
            _percent_list(item.get("savings_percents")),
        ))

    present = {e["capacity_type"] for e in entries}
    for ctype in CAPACITY_TYPES:
        if ctype not in present:
            entries.append(_unavailable_entry(ctype))

    return CapacityModel(entries, SHAPE_V2_ENTRIES)
