"""
Per-utility bill field extraction.

WHAT THIS IS, AND WHAT IT IS NOT

    It is a READ-ONLY parser over text that has ALREADY been pulled out of a
    bill the rep has ALREADY uploaded. It does no I/O, opens no file, stores
    nothing and knows nothing about document sets. The upload path, the
    document-set semantics, page ordering and the Perch submission are
    untouched by anything in this module - extraction reads the stored bill
    and offers values; it never participates in storing one.

WHY IT EXISTS

    parse_utility_bill() was tuned against one layout: National Grid NY, whose
    bill anchors the customer block on "SERVICE FOR" and the account on
    "ACCOUNT NUMBER". Those two anchors appear on no other utility's bill in
    this programme, so NYSEG, RG&E and Dominion extracted NOTHING - not
    because OCR failed, but because nothing in the parser was looking for
    their labels. There is also no POD ID extraction at all, though NYSEG and
    RG&E both REQUIRE one to enrol.

TEXT SHAPE: ONE FLAT LINE

    Every rule here is written against text with all whitespace collapsed to
    single spaces, because that is what both readers produce:

        server  pdfplumber page text, joined, then _clean()
        browser pdf.js content.items.map(str).join(' ')

    pdf.js does not emit line breaks at all, so a rule that depends on a
    newline would work in tests and fail in the browser. Anything that reads
    across what LOOKS like two lines on paper - Dominion's street and its
    city/state/zip - is matched as one run of text on purpose.

NEVER GUESS

    Every identifier is validated with the SAME rule manual entry uses before
    it is offered (see validation.py callers and perch_utilities). A candidate
    that fails is reported as NOT FOUND rather than filled in: a wrong account
    number enrols the wrong customer, and a rep who sees a filled field will
    reasonably believe it was read correctly. Where two plausible candidates
    for one identifier appear, that is also NOT FOUND - an ambiguous POD ID is
    exactly the case where a confident-looking wrong answer does most harm.

PRIVACY

    Nothing here logs, prints or stores a value. Callers receive the fields and
    decide what to do; the diagnostics this module produces name FIELD TYPES
    ("account_number found", "pod_id missing"), never contents.
"""
import re

# ── Utility identification ──────────────────────────────────────────────
#
# Anchored on the brand as it is PRINTED, not on the UBS slug. A bill is
# identified by what it says it is; the enrollment's chosen utility is only a
# fallback, because the rep can pick the wrong one and the document is the
# evidence.
#: Each brand scored, not matched first-wins. WHY: a NYSEG bill carries one
#: line of shared-charity boilerplate naming RG&E, and first-match treated that
#: single mention as decisive - so a NYSEG bill was parsed with RG&E's rules
#: and its POD ID was correctly rejected for having the wrong prefix. A bill
#: names its own utility many times; another utility appears once, in passing.
#:
#: The self-referential markers - the account website and the supply-service
#: line - are weighted because boilerplate does not contain them.
_BRAND_SIGNALS = {
    "rochester-gas-electric": ((r"\brge\.com\b", 5), (r"\bRGE Supply Service\b", 5),
                               (r"\bRochester Gas\b", 3), (r"\bRG&E\b", 1)),
    "nyseg":                  ((r"\bnyseg\.com\b", 5), (r"\bNYSEG Supply Service\b", 5),
                               (r"New York State Electric", 3), (r"\bNYSEG\b", 1)),
    "dominion-energy-va":     ((r"DominionEnergy\.com", 5), (r"DOMINION ENERGY VIRGINIA", 5),
                               (r"\bDominion Energy\b", 2)),
    "national-grid-ny":       ((r"nationalgridus\.com", 5), (r"\bNational Grid\b", 1)),
}

#: A document is only treated as a bill when it carries at least one of these.
#: Without it, any PDF mentioning a utility - a programme flyer, an eligibility
#: sheet - would be handed to a bill parser.
_BILL_ANCHORS = (r"Account Number", r"Account\s*#", r"Service Address",
                 r"SERVICE FOR", r"Amount Due")


def looks_like_a_bill(text):
    """Whether this document is shaped like a utility bill at all."""
    return any(re.search(p, text or "", re.I) for p in _BILL_ANCHORS)


def detect_utility(text):
    """The utility this bill is FROM, or None when it does not say clearly.

    Scored across the whole document: the utility whose own name and website
    run through the page wins over one named once in a paragraph of small
    print. A tie, or no signal at all, returns None so the caller falls back
    to the utility the enrollment is for rather than picking one.
    """
    t = text or ""
    if not looks_like_a_bill(t):
        return None
    scores = {}
    for slug, signals in _BRAND_SIGNALS.items():
        total = 0
        for pattern, weight in signals:
            total += weight * len(re.findall(pattern, t, re.I))
        if total:
            scores[slug] = total
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None                      # genuinely ambiguous: do not guess
    return ranked[0][0]


# ── Shared shapes ───────────────────────────────────────────────────────

_STREET_SUFFIX = (r"(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|BLVD|"
                  r"BOULEVARD|CT|COURT|PL|PLACE|CIR|CIRCLE|WAY|TER|TERRACE|"
                  r"PKWY|PARKWAY|HWY|HIGHWAY|TRL|TRAIL)")

#: Secondary-address markers, as USPS publication 28 names them. Captured
#: separately so the unit does not end up glued to the street.
_UNIT_RE = re.compile(
    r"\b(APT|APARTMENT|UNIT|STE|SUITE|BLDG|BUILDING|FL|FLOOR|RM|ROOM|#)\s*"
    r"([A-Z0-9][A-Z0-9-]{0,7})\b", re.I)


def split_unit(street):
    """('123 MAIN ST APT 4B') -> ('123 MAIN ST', 'APT 4B').

    Returns (street, unit_or_empty). Only splits on an explicit marker: a bare
    trailing token is left on the street, because "123 MAIN ST B" is at least
    as likely to be part of the street as a unit, and inventing a unit changes
    the service address.
    """
    s = (street or "").strip(" ,")
    m = _UNIT_RE.search(s)
    if not m:
        return s, ""
    unit = f"{m.group(1).upper()} {m.group(2).upper()}".replace("# ", "#")
    cleaned = (s[:m.start()] + " " + s[m.end():]).strip(" ,")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned, unit


def split_name(full):
    """('JANE Q PUBLIC') -> ('JANE Q', 'PUBLIC'). Last token is the surname."""
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _digits(value):
    return re.sub(r"\D", "", value or "")


# ── Validation: the SAME rules manual entry uses ────────────────────────

def validate_account(value, rules=None):
    """True when this account number would be accepted if typed by hand.

    `rules` is the utility's row from perch_utilities (NY) or the market's
    published field rules (VA), passed in by the caller so this module reads
    no database of its own. Every validator here takes (value, rules) so the
    dispatch table below cannot pair one with the wrong argument order.
    """
    d = _digits(value)
    if not d:
        return False
    allowed = (rules or {}).get("account_lengths")
    if allowed:
        return len(d) in set(allowed)
    return False


def validate_pod(value, rules=None):
    """True when this POD ID would be accepted if typed by hand.

    Mirrors podLooksValid() in the browser: exact length, then prefix.
    """
    v = (value or "").strip().upper()
    if not v:
        return False
    rules = rules or {}
    length = rules.get("pod_length")
    prefix = rules.get("pod_prefix")
    if length and len(v) != int(length):
        return False
    if prefix and not v.startswith(str(prefix).upper()):
        return False
    return bool(length or prefix)


def validate_meter(value, rules=None):
    """True when this meter number would be accepted if typed by hand."""
    d = _digits(value)
    lengths = (rules or {}).get("meter_lengths")
    if not lengths:
        return False
    return len(d) in set(lengths)


#: The validation each utility applies to what is read off its bill. These are
#: the SAME numbers the manual-entry path uses - perch_utilities for the New
#: York utilities, and the account/meter rules Perch publishes for Dominion -
#: restated here only so the parser can be exercised without a database.
#: A caller holding the live rules should pass them in.
DEFAULT_RULES = {
    "national-grid-ny":       {"account_lengths": (10,)},
    "nyseg":                  {"account_lengths": (11,), "pod_length": 15,
                               "pod_prefix": "N01"},
    "rochester-gas-electric": {"account_lengths": (11,), "pod_length": 15,
                               "pod_prefix": "R01"},
    "dominion-energy-va":     {"account_lengths": (10, 12), "meter_lengths": (10,)},
}


def rules_for(slug, overrides=None):
    base = dict(DEFAULT_RULES.get(slug) or {})
    if overrides:
        base.update({k: v for k, v in overrides.items() if v})
    return base


# ── Per-utility extraction ──────────────────────────────────────────────
#
# Each returns a plain {field: value} of CANDIDATES. Validation happens once,
# centrally, in parse_bill() - so no parser can accidentally skip it.

def _parse_avangrid(t):
    """NYSEG and RG&E share one bill template (both are Avangrid).

    Anchors, all of which are printed labels rather than positions:

        "<NAME> Account Number: ####-####-###"
        "Service Address: <street>, <CITY> <ST> <ZIP>"
        "... Supply Service PoD ID: <letter + 14 digits>"

    The name is taken from the run of capitals IMMEDIATELY before the account
    label, which is where this template puts it. "Invoice Number:" is a
    deliberate non-target: it is an 11-digit number on the same page and would
    validate as an account number if it were ever allowed to reach that field.
    """
    out = {}
    m = re.search(r"Account Number:?\s*(\d{4}-\d{4}-\d{3}|\d{11})", t, re.I)
    if m:
        out["account_number"] = _digits(m.group(1))

    # The customer name sits directly before the account label on this layout.
    m = re.search(r"\b([A-Z][A-Z]+(?:\s+[A-Z][A-Z.'-]*){0,3})\s+Account Number:", t)
    if m:
        out["account_holder"] = m.group(1).strip()

    # Street, then city/state/zip after a comma.
    m = re.search(
        r"Service Address:?\s*(.+?),\s*([A-Za-z][A-Za-z .'-]*?)\s+([A-Z]{2})\s+(\d{5})",
        t, re.I)
    if m:
        street, unit = split_unit(m.group(1))
        out["service_street"] = street
        if unit:
            out["service_unit"] = unit
        out["service_city"] = m.group(2).strip()
        out["service_state"] = m.group(3).strip().upper()
        out["service_zip"] = m.group(4)

    # POD ID. The label is printed "PoD ID" on this template; matched
    # case-insensitively and allowing the spacing to vary.
    pods = {p.upper() for p in re.findall(r"Po\s*D\s*ID:?\s*([A-Z]\d{14})", t, re.I)}
    if len(pods) == 1:
        out["pod_id"] = pods.pop()
    elif len(pods) > 1:
        # TWO different POD-shaped values on one bill. Guessing between them
        # is the one thing this must never do.
        out["_ambiguous"] = out.get("_ambiguous", []) + ["pod_id"]
    return out


def _parse_dominion(t):
    """Dominion Energy Virginia.

    Anchors:
        "Account Number: ############"   (10 or 12 digits)
        "Customer Name: <NAME>"
        "Service Address: <street> <CITY>, <ST> <ZIP>"
        "Electric Meter: ##########"

    The street and the city/state/zip are printed on two lines; in the flat
    text stream they run together, which is why one expression spans both.
    The coupon at the foot repeats the account as "Account # ############" -
    the same value, so it is harmless, but the first match is used.
    """
    out = {}
    m = re.search(r"Account\s*(?:Number|#)\s*:?\s*(\d{12}|\d{10})\b", t, re.I)
    if m:
        out["account_number"] = _digits(m.group(1))

    m = re.search(r"Customer Name:?\s*([A-Z][A-Z]+(?:\s+[A-Z][A-Z.'-]*){0,3})\b", t)
    if m:
        out["account_holder"] = m.group(1).strip()

    m = re.search(
        # The comma after the street appears on some printings and not on
        # others; the street is terminated by its own suffix either way.
        rf"Service Address:?\s*(\d+[A-Z0-9 .'#-]*?{_STREET_SUFFIX}),?\s+"
        r"([A-Za-z][A-Za-z .'-]*?),\s*([A-Z]{2})\s+(\d{5})",
        t, re.I)
    if m:
        street, unit = split_unit(m.group(1))
        out["service_street"] = street
        if unit:
            out["service_unit"] = unit
        out["service_city"] = m.group(2).strip()
        out["service_state"] = m.group(3).strip().upper()
        out["service_zip"] = m.group(4)

    meters = {_digits(x) for x in re.findall(r"Electric Meter:?\s*(\d{10})\b", t, re.I)}
    if len(meters) == 1:
        out["meter_number"] = meters.pop()
    elif len(meters) > 1:
        out["_ambiguous"] = out.get("_ambiguous", []) + ["meter_number"]
    return out


#: Which extractor serves which utility. National Grid is deliberately absent:
#: its fields keep coming from the original parse_utility_bill() rules, which
#: are working and are not touched by this module.
_EXTRACTORS = {
    "nyseg": _parse_avangrid,
    "rochester-gas-electric": _parse_avangrid,
    "dominion-energy-va": _parse_dominion,
}

#: Every field this module can offer. Order is the order a rep reads the form.
FIELDS = ("account_holder", "service_street", "service_unit", "service_city",
          "service_state", "service_zip", "account_number", "pod_id",
          "meter_number")

#: Identifiers that must pass the utility's own rule before being offered.
_VALIDATED = {"account_number": validate_account,
              "pod_id": validate_pod,
              "meter_number": validate_meter}


def parse_bill(text, utility_hint=None, rule_overrides=None):
    """Read a bill. Returns a result dict; raises nothing.

        {"utility":   slug or None,
         "fields":    {field: value}      - validated, safe to offer
         "found":     [field, ...]
         "missing":   [field, ...]        - expected here but not read
         "rejected":  [field, ...]        - read, but failed the utility's rule
         "ambiguous": [field, ...]}       - more than one candidate; not guessed

    `utility_hint` is the slug the enrollment is for. It is used ONLY when the
    document does not identify itself, never to override what the bill says.
    """
    t = re.sub(r"\s+", " ", text or "").strip()
    slug = detect_utility(t) or (utility_hint or None)
    result = {"utility": slug, "fields": {}, "found": [], "missing": [],
              "rejected": [], "ambiguous": []}
    if not t or not slug:
        return result

    extractor = _EXTRACTORS.get(slug)
    if not extractor:
        # Not a layout this module handles (National Grid takes its own path).
        return result

    raw = extractor(t)
    result["ambiguous"] = list(raw.pop("_ambiguous", []))
    rules = rules_for(slug, rule_overrides)

    for field, value in raw.items():
        if field not in FIELDS:
            continue
        validator = _VALIDATED.get(field)
        if validator and not validator(value, rules):
            # READ, BUT REFUSED. Reported so the rep can be told the field
            # needs entering, never filled in on the strength of a bad read.
            result["rejected"].append(field)
            continue
        result["fields"][field] = value

    result["found"] = sorted(result["fields"])
    expected = set(expected_fields(slug))
    result["missing"] = sorted(expected - set(result["fields"]))
    return result


def expected_fields(slug):
    """What a complete read of THIS utility's bill would contain.

    A unit is never expected - most addresses do not have one, and treating a
    missing unit as a failed read would make every house look like a bad scan.
    """
    base = ["account_holder", "service_street", "service_city", "service_state",
            "service_zip", "account_number"]
    rules = DEFAULT_RULES.get(slug) or {}
    if rules.get("pod_length") or rules.get("pod_prefix"):
        base.append("pod_id")
    if rules.get("meter_lengths"):
        base.append("meter_number")
    return base


def diagnostic(result):
    """A log line that names FIELD TYPES and never a value."""
    return ("bill parse: utility=%s found=%s missing=%s rejected=%s ambiguous=%s"
            % (result.get("utility"), len(result.get("found") or []),
               ",".join(result.get("missing") or []) or "-",
               ",".join(result.get("rejected") or []) or "-",
               ",".join(result.get("ambiguous") or []) or "-"))
