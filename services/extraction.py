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


def parse_utility_bill(raw_text: str) -> dict:
    """Returns a dict of {field: {"value": ..., "confidence": 0-1}} plus a flat
    'fields' view for convenience. Confidence is a simple heuristic (regex hit = 0.9,
    with small penalties for ambiguity) — swap in a real model's confidence in production."""
    t = _clean(raw_text)
    result = {}

    def set_field(name, value, confidence):
        result[name] = {"value": value, "confidence": confidence}

    # Account number (e.g. "ACCOUNT NUMBER 12345-67890")
    m = re.search(r"ACCOUNT NUMBER\s*(\d{5}-\d{5}|\d{10})", t, re.I)
    if m:
        set_field("account_number", m.group(1).replace("-", ""), 0.93)

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

    return result


def amount_to_bracket(amount):
    if amount is None:
        return None
    if amount < 75:
        return "Less than $75"
    if amount < 150:
        return "$75 - $149"
    if amount < 250:
        return "$150 - $249"
    return "$250+"
