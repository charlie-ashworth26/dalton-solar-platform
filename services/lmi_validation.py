"""
LMI document validation.

IMPORTANT — honesty about what this is: the spec calls this "AI-assisted
validation." What's implemented here is a rule-based classifier (keyword and
date-pattern matching against the NY accepted-document list), not a call to a
real vision/language model. It's built to the exact same contract a model-backed
version would expose (classify type, extract dates/names, return confidence +
reasons), so swapping in a real LLM/vision API later is a drop-in replacement
of validate_lmi_document()'s internals — the calling code and DB shape don't
change. This assists QA; it never sets eligibility on its own (requirement #5).
"""
import re
from datetime import datetime, timedelta

LMI_DOCUMENT_TYPES = [
    {"key": "heap", "label": "Electric bill showing HEAP/LIHEAP/EAP assistance", "dac_required": False,
     "pattern": r"\bHEAP\b|\bLIHEAP\b|Energy Assistance|\bEAP\b|Energy Affordability Credit|Billing Adjustments"},
    {"key": "snap_award", "label": "SNAP award letter", "dac_required": False,
     "pattern": r"SNAP.{0,30}(award|notice|eligib|approv)"},
    {"key": "snap_card", "label": "SNAP card", "dac_required": False, "pattern": r"\bSNAP\b"},
    {"key": "section8", "label": "Housing authority certification / Section 8", "dac_required": False,
     "pattern": r"Section\s*8|Housing Authority|Tenant Eligibility|\bHUD\b"},
    {"key": "disability", "label": "Disability benefits letter", "dac_required": False,
     "pattern": r"Disability Benefits|SSDI"},
    {"key": "ssi", "label": "SSI", "dac_required": False, "pattern": r"\bSSI\b|Supplemental Security Income"},
    {"key": "medicaid", "label": "Medicaid award letter", "dac_required": True,
     "pattern": r"Medicaid|NY State of Health|Essential Plan"},
    {"key": "lifeline", "label": "Lifeline qualification", "dac_required": True, "pattern": r"\bLifeline\b"},
    {"key": "slip", "label": "SLIP", "dac_required": True, "pattern": r"\bSLIP\b"},
]

DATE_PATTERNS = [
    r"Date Printed\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",
    r"\b([A-Z][a-z]+ \d{1,2},? \d{4})\b",
]


def _find_document_type(text):
    for doc_type in LMI_DOCUMENT_TYPES:
        if re.search(doc_type["pattern"], text, re.I):
            return doc_type
    return None


def _find_date(text):
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(m.group(1), fmt)
                except ValueError:
                    continue
    return None


def _name_on_document(text, account_holder_name):
    """Very rough presence check — looks for the account holder's last name in the doc."""
    if not account_holder_name:
        return None
    parts = account_holder_name.strip().split()
    if not parts:
        return None
    last_name = parts[-1]
    return bool(re.search(re.escape(last_name), text, re.I))


def validate_lmi_document(raw_text: str, account_holder_name: str = None) -> dict:
    text = re.sub(r"\s+", " ", raw_text or "").strip()

    doc_type = _find_document_type(text)
    doc_date = _find_date(text)
    name_match = _name_on_document(text, account_holder_name)

    reasons = []
    missing_info = []
    mismatch_warnings = []

    if doc_type:
        reasons.append(f"Matched keywords for: {doc_type['label']}")
    else:
        missing_info.append("Could not match document text to any accepted LMI document type")

    within_a_year = None
    if doc_date:
        within_a_year = (datetime.now() - doc_date) <= timedelta(days=395)
        if within_a_year:
            reasons.append(f"Document date {doc_date.date().isoformat()} is within the last 12 months")
        else:
            mismatch_warnings.append(f"Document date {doc_date.date().isoformat()} is older than 12 months")
    else:
        missing_info.append("No date found on document — cannot confirm it's within 12 months")

    if account_holder_name:
        if name_match is True:
            reasons.append("Account holder's last name appears on the document")
        elif name_match is False:
            mismatch_warnings.append("Account holder's name was not found on the document — verify manually")
    else:
        missing_info.append("No account holder name on file to cross-check against")

    if doc_type and doc_type["dac_required"]:
        missing_info.append(
            f"{doc_type['label']} additionally requires confirming the household is in a "
            f"NYSERDA Designated Disadvantaged Community — not verifiable from the document alone"
        )

    # Classification + confidence
    if doc_type and within_a_year is True and name_match is not False:
        classification = "likely_valid"
        confidence = 0.82 if name_match else 0.68
    elif doc_type and (within_a_year is False or name_match is False):
        classification = "needs_manual_review"
        confidence = 0.5
    elif doc_type:
        classification = "needs_manual_review"
        confidence = 0.55
    else:
        classification = "likely_invalid"
        confidence = 0.3

    return {
        "classification": classification,
        "confidence": round(confidence, 2),
        "matched_type": doc_type["label"] if doc_type else None,
        "matched_type_key": doc_type["key"] if doc_type else None,
        "requires_dac_check": bool(doc_type and doc_type["dac_required"]),
        "document_date": doc_date.date().isoformat() if doc_date else None,
        "reasons": reasons,
        "missing_info": missing_info,
        "mismatch_warnings": mismatch_warnings,
    }


AMI_TABLE = [
    {"size": 1, "amount": 61750}, {"size": 2, "amount": 70550}, {"size": 3, "amount": 79350},
    {"size": 4, "amount": 88150}, {"size": 5, "amount": 95250}, {"size": 6, "amount": 102300},
    {"size": 7, "amount": 109350}, {"size": 8, "amount": 116400},
]


def ami_threshold_for(household_size: int):
    row = next((r for r in AMI_TABLE if r["size"] == household_size), None)
    return row["amount"] if row else None
