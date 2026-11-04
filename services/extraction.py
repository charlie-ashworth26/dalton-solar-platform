"""
Utility-bill text extraction and field parsing.

Two stages, same as the client-side prototype this replaces:
  1. get_text(path) — pull raw text out of a PDF (pdfplumber) or a photo (pytesseract OCR).
  2. parse_utility_bill(text) — regex/keyword extraction of the fields QA and the
     rep need to review. Tuned against the National Grid NY bill layout the team
     supplied; other utilities will need their own patterns added here.

Every extracted field carries a confidence value so the UI can show what's
solid vs. what needs a human look, per requirement #3.
"""
import re
import pdfplumber
import pytesseract
from PIL import Image


def get_text(file_path: str, mime_type: str = "") -> str:
    is_pdf = mime_type == "application/pdf" or file_path.lower().endswith(".pdf")
    if is_pdf:
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages[:6]:
                text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)
    # image — OCR
    img = Image.open(file_path)
    return pytesseract.image_to_string(img)


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    # National Grid's two-column layout interleaves the billing-period line and
    # page marker between "SERVICE FOR" and the customer's name/address when
    # extracted as a flat text stream. Strip those known artifacts before
    # field extraction rather than trying to write one regex that tolerates them.
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    t = re.sub(r"BILLING PERIOD\s+PAGE\s+\d+\s+of\s+\d+\s*", "", t)
    t = re.sub(rf"({months})\s+\d{{1,2}},?\s+\d{{4}}\s+to\s+({months})\s+\d{{1,2}},?\s+\d{{4}}\s*", "", t)
    return t


STREET_SUFFIXES = r"(?:ST|AVE|RD|DR|LN|BLVD|WAY|CT|PL|CIR|HWY|ROAD|STREET|AVENUE|DRIVE|LANE)\.?"


def _published_rules(utility_hint):
    """The utility's OWN account-number rule, from the reference table.

    bill_parsers deliberately reads no database - it is a pure text module -
    so it carries default lengths for the layouts it knows. Those defaults are
    a SECOND COPY of a rule that lives in perch_utilities, and a second copy
    is how one side comes to accept what another rejects: the day a utility's
    published format changes, the parser would go on validating against the
    old length and silently drop a perfectly good account number.

    So the authoritative lengths are passed in. Only the account rule is
    overridden here; POD and meter rules are unchanged. Returns None - meaning
    "use your defaults" - when the slug is unknown or the lookup fails, which
    keeps extraction working even with no database behind it.
    """
    if not utility_hint:
        return None
    try:
        from services.perch import utilities
        lengths = utilities.account_number_lengths(utility_hint)
    except Exception:
        return None
    return {"account_lengths": lengths} if lengths else None


def _effective_account_lengths(utility_hint, overrides):
    """The lengths the LEGACY block below must judge what it reads against.

    The published rule wins wherever there is one. Where the table is silent
    the per-utility parser's own default is the only copy of the rule -
    Dominion issues 10 OR 12 digits, which the table's single-length column
    cannot express - and the legacy fallback must never be LOOSER than the
    parser that owns that layout, or a run the utility does not issue gets
    offered by one reader and refused by the other.

    An empty tuple means no rule is published anywhere, so judge nothing and
    pass the value through whole. Never read as "reject".
    """
    published = tuple((overrides or {}).get("account_lengths") or ())
    if published or not utility_hint:
        return published
    try:
        from services import bill_parsers
        return tuple((bill_parsers.DEFAULT_RULES.get(utility_hint) or {})
                     .get("account_lengths") or ())
    except Exception:
        return ()


def parse_utility_bill(raw_text: str, utility_hint: str = None) -> dict:
    """Returns a dict of {field: {"value": ..., "confidence": 0-1}} plus a flat
    'fields' view for convenience. Confidence is a simple heuristic (regex hit = 0.9,
    with small penalties for ambiguity) — swap in a real model's confidence in production."""
    t = _clean(raw_text)
    result = {}
    # The utility's OWN lengths, from the reference table. Looked up once here
    # because BOTH readers below need them: the legacy National Grid block
    # judges what it finds, and bill_parsers is handed them as overrides.
    overrides = _published_rules(utility_hint)
    allowed_lengths = _effective_account_lengths(utility_hint, overrides)
    rejected_lengths = []

    def set_field(name, value, confidence):
        result[name] = {"value": value, "confidence": confidence}

    # ── Account number ──────────────────────────────────────────────────
    #
    # THE TRUNCATION THIS FIXES (traced 2026-11-03). The pattern was
    #
    #     ACCOUNT NUMBER\s*(\d{5}-\d{5}|\d{10})
    #
    # which asks for EXACTLY ten digits and is not anchored at the end. On an
    # eleven-digit account it matched the first ten and dropped the last one -
    # silently, and with a value that still looks like an account number. For
    # Orange & Rockland, which now accepts 10 OR 11 digits, the short result
    # also PASSES validation, so nothing downstream could catch it: the rep
    # saw a plausible number that was not theirs.
    #
    # The whole run is captured now, and the length is judged against the
    # utility's published rule rather than assumed. A run that does not match
    # any accepted length is NOT offered: a wrong account number is worse than
    # an empty box, because the box is obviously the rep's job and the wrong
    # number looks finished. Nothing here pads, trims or guesses a digit.
    m = re.search(r"ACCOUNT\s+NUMBER\s*:?\s*(\d[\d-]*\d)\b", t, re.I)
    if m:
        digits = m.group(1).replace("-", "")
        allowed = allowed_lengths
        if not allowed or len(digits) in allowed:
            set_field("account_number", digits, 0.93)
        else:
            # Read, but not the shape this utility issues. Reported so the
            # panel can say the field still needs entering.
            rejected_lengths.append("account_number")

    # Amount due
    m = re.search(r"AMOUNT DUE\D{0,25}?\$?\s*([\d,]+\.\d{2})", t, re.I)
    if m:
        set_field("amount_due", float(m.group(1).replace(",", "")), 0.9)

    # Name + service address: "SERVICE FOR NAME STREET (w/ suffix) CITY ST ZIP"
    m = re.search(
        rf"SERVICE FOR\s+([A-Z][A-Z .'\-]+?)\s+(\d+[A-Z0-9 .'\-]*?{STREET_SUFFIXES})\s+([A-Za-z .'\-]+?)\s+([A-Z]{{2}})\s+(\d{{5}})",
        t,
    )
    if m:
        set_field("account_holder", m.group(1).strip(), 0.9)
        set_field("service_street", m.group(2).strip(), 0.9)
        set_field("service_city", m.group(3).strip(), 0.9)
        set_field("service_state", m.group(4).strip(), 0.95)
        set_field("service_zip", m.group(5).strip(), 0.95)

    # Utility name (very rough — look for known brands; extend as more utilities are added)
    for utility in ["National Grid", "NYSEG", "Con Edison", "Central Hudson", "Orange and Rockland", "PSEG"]:
        if re.search(re.escape(utility), t, re.I):
            set_field("utility", utility, 0.7)
            break

    # Meter number
    m = re.search(r"METER NUMBER\s*(\w+)", t, re.I)
    if m:
        set_field("meter_number", m.group(1), 0.85)

    # Rate class, e.g. "RATE Electric SC1 Non Heat"
    m = re.search(r"RATE\s+(Electric\s+\S+(?:\s+(?:Non\s+Heat|Heat))?)", t, re.I)
    if m:
        set_field("rate_class", m.group(1).strip(), 0.75)

    # Bill date
    m = re.search(r"DATE BILL ISSUED\s*([A-Z][a-z]+ \d{1,2},? \d{4})", t)
    if m:
        set_field("bill_date", m.group(1), 0.85)

    # Billing-period total usage — the two "Actual" meter readings are followed
    # by the total, e.g. "8580 Actual 8012 Actual 568 kWh"
    m = re.search(r"Actual\s+\d+\s+Actual\s+(\d+)\s*kWh", t, re.I)
    if m:
        set_field("monthly_usage_kwh", int(m.group(1)), 0.85)

    # Historical usage table: "Mon YY kWh" repeated, e.g. "Dec 24 654 Jul 25 737"
    hist = re.findall(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s?(\d{2})\s+(\d{2,5})\b", t)
    if hist:
        result["historical_usage"] = {"value": [f"{mo} {yr}: {kwh} kWh" for mo, yr, kwh in hist[:12]], "confidence": 0.7}

    # Existing community solar credit line, if present
    m = re.search(r"(Energy Affordability Credit|Community Solar Credit|Bill Credit)\D{0,15}(-?\$?[\d,]+\.\d{2})", t, re.I)
    if m:
        set_field("existing_cs_credits", f"{m.group(1)}: {m.group(2)}", 0.6)

    # ── OTHER UTILITIES ──────────────────────────────────────────────────
    #
    # Everything above is the National Grid layout and is UNCHANGED. Its
    # anchors ("SERVICE FOR", "ACCOUNT NUMBER") appear on no other bill in
    # this programme, which is the whole reason NYSEG, RG&E and Dominion
    # extracted nothing: not a failure to read the page, but nothing looking
    # for their labels.
    #
    # bill_parsers returns {} unless it both recognises the utility AND holds
    # an extractor for it, so a National Grid bill reaches here and leaves
    # with exactly the fields it had before. Identifiers it offers have
    # already passed the utility's own validation; anything that failed is
    # absent, so a bad read can never arrive as a filled field.
    try:
        from services import bill_parsers
        parsed = bill_parsers.parse_bill(t, utility_hint=utility_hint,
                                         rule_overrides=overrides)
        for field, value in (parsed.get("fields") or {}).items():
            # Confidence is kept in the existing shape for existing readers.
            # It is NOT a measurement here any more than it is above.
            set_field(field, value, 0.9)
        if parsed.get("utility"):
            set_field("utility", parsed["utility"], 0.9)
        # Reported so a caller can tell the rep WHICH fields still need
        # typing. Field names only - never a value.
        rejected = set(parsed.get("rejected") or []) | set(rejected_lengths)
        # A field the per-utility parser FILLED is not rejected, whatever the
        # legacy block made of it: the utility's own reader is the better one.
        rejected -= set((parsed.get("fields") or {}).keys())
        if rejected:
            result["_rejected_fields"] = {"value": sorted(rejected),
                                          "confidence": 1.0}
        if parsed.get("ambiguous"):
            result["_ambiguous_fields"] = {"value": sorted(parsed["ambiguous"]),
                                           "confidence": 1.0}
    except Exception:
        # Extraction is an assist. A parser fault must never cost the rep the
        # upload, and the National Grid fields gathered above still stand.
        if rejected_lengths and "_rejected_fields" not in result:
            result["_rejected_fields"] = {"value": sorted(set(rejected_lengths)),
                                          "confidence": 1.0}

    return result


# amount_to_bracket() lived here. It was never called: bill amount is not part
# of the NY or VA workflow, no rep field collects it, the bill-validation
# missing-field list in routes/document_routes.py does not include it, and no
# Perch payload carries an amount. The amount_due extraction above is LEFT
# ALONE deliberately - it only writes into documents.extracted_data_json, a
# JSON blob that nothing requires and nothing shows a rep, so removing it would
# change stored extraction output for no benefit.
