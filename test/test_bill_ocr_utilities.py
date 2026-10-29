"""
Reading a bill from NYSEG, RG&E and Dominion Energy Virginia - and proving the
upload that carries it is untouched.

WHY THIS SUITE EXISTS

    The bill reader understood exactly one layout. Its anchors were National
    Grid's own wording ("SERVICE FOR", "ACCOUNT NUMBER"), which appears on no
    other bill in this programme, so a rep enrolling a NYSEG, RG&E or Dominion
    customer uploaded the bill, watched it succeed, and then typed every field
    by hand - including the two that are easiest to get wrong and hardest to
    recover from: the POD ID and the meter number.

WHAT IS ASSERTED HERE, IN ORDER OF HOW MUCH IT MATTERS

    1. NATIONAL GRID IS UNCHANGED. The reference bill still yields the same
       fields, by the same code path, and the new per-utility layer offers
       nothing for it.
    2. NOTHING IS EVER GUESSED. A POD ID with the wrong prefix, a meter number
       of the wrong length, an account number of the wrong length, and a bill
       carrying two POD-shaped values are each left for the rep. An invoice
       number is never promoted into a POD ID.
    3. THE UPLOAD IS UNTOUCHED. Same endpoint, same set, same rows, same page
       order, same removal, same replacement, same hydration, same list of
       bills handed to the Perch boundary - asserted against an extraction
       that is forced to fail, which is the state the system was in before any
       of this existed.

ON THE VALUES IN THIS FILE

    Every name, address, account number, POD ID and meter number below is
    invented for this suite. The parser is built from LABELS and LAYOUT, so
    nothing here needs to resemble a real customer, and nothing here does.

    python test/test_bill_ocr_utilities.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-bill-ocr-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query

init_db(reset=True)

from app import app
from routes import document_routes

document_routes.UPLOAD_DIR = os.path.join(_temp.name, "uploads")
document_routes.DATA_ROOT = _temp.name
os.makedirs(document_routes.UPLOAD_DIR, exist_ok=True)
import seed
from services import bill_parsers, extraction, extraction_engine
from services.perch import adapter

JS = open(ROOT / "static" / "js" / "app.js", encoding="utf-8").read()


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


# ───────────────────────── synthetic bills ──────────────────────────────────
# Labels and layout are taken from the real templates. Every VALUE is invented.

SYNTH = {
    "name": "PAT Q SAMPLE",
    "street": "742 EXAMPLE WAY",
    "unit": "APT 9",
    "city": "ANYTOWN",
    "zip": "14850",
    "nyseg_acct": "90000000011",          # 11 digits
    "nyseg_pod": "N01000000000001",       # N01 + 12 = 15 characters
    "rge_acct": "90000000022",
    "rge_pod": "R01000000000002",
    "dom_acct10": "9000000001",
    "dom_acct12": "900000000012",
    "dom_meter": "8000000001",            # 10 digits
    "invoice": "70000000003",             # a decoy: never a POD ID
}


def avangrid_text(brand, acct, pod=None, unit=False, invoice=True, extra=""):
    """The shared NYSEG / RG&E template, flattened the way a PDF reader emits
    it - one run of text, no line structure to lean on."""
    domain = "nyseg.com" if brand == "nyseg" else "rge.com"
    title = ("New York State Electric & Gas Corporation" if brand == "nyseg"
             else "Rochester Gas and Electric Corporation")
    supply = "NYSEG Supply Service" if brand == "nyseg" else "RGE Supply Service"
    street = SYNTH["street"] + (" " + SYNTH["unit"] if unit else "")
    out = (f"{title} Visit us at {domain} "
           f"{SYNTH['name']} Account Number: {acct} "
           f"Service Address: {street}, {SYNTH['city']} NY {SYNTH['zip']} "
           f"Amount Due $123.45 ")
    if invoice:
        out += f"Invoice Number: {SYNTH['invoice']} "
    if pod:
        out += f"Electricity Rate - Residential {supply} PoD ID: {pod} "
    out += ("NYSEG and RG&E Project SHARE helps neighbours in need. " + extra)
    return out


def dominion_text(acct, meter=None, two_line_city=True):
    city_block = (f"Service Address: {SYNTH['street']} {SYNTH['city']}, VA 23220"
                  if two_line_city else
                  f"Service Address: {SYNTH['street']}, {SYNTH['city']}, VA 23220")
    return ("DOMINION ENERGY VIRGINIA Visit DominionEnergy.com "
            f"Customer Name: {SYNTH['name']} "
            f"Account Number: {acct} {city_block} "
            + (f"Electric Meter: {meter} " if meter else "")
            + "Amount Due $98.76 Account # " + acct + " ")


def text_pdf(name, body, pages=1):
    """A real, text-bearing PDF - the common case, and the one the reader
    prefers over OCR."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    import textwrap

    path = os.path.join(_temp.name, name)
    c = canvas.Canvas(path, pagesize=LETTER)
    # Wrapped on WORD boundaries: a line break through the middle of an
    # account number would be a defect in this suite, not in the parser.
    lines = textwrap.wrap(body, 95) or [""]
    per_page = (len(lines) + pages - 1) // pages
    for i in range(pages):
        text = c.beginText(40, 740)
        for line in lines[i * per_page:(i + 1) * per_page] or [" "]:
            text.textLine(line)
        c.drawText(text)
        c.showPage()
    c.save()
    return path


def scanned_pdf(name):
    """An image-only PDF: no embedded text at all, so the OCR fallback is the
    only way in."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 1600), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 80), "Account Number: " + SYNTH["dom_acct10"], fill="black")
    path = os.path.join(_temp.name, name)
    img.save(path, "PDF", resolution=150.0)
    return path


def login(c):
    r = c.post("/api/auth/login", json={"email": "charlie@daltonsolar.com",
                                        "password": "RepPass1!"})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def main():
    seed.seed()
    c = app.test_client()
    h = login(c)

    # ════════════════════════════════════════════════════════════════════════
    section("1. NATIONAL GRID IS UNCHANGED")
    # ════════════════════════════════════════════════════════════════════════
    ng_pdf = str(ROOT / "test" / "sample_utility_bill.pdf")
    ng = extraction_engine.LocalExtractor().extract([ng_pdf], category="utility_bill")
    f = ng.fields
    check("the reference bill is still read", ng.usable)
    check("  account holder", f.get("account_holder") == "JOHN J CUSTOMER")
    check("  account number", f.get("account_number") == "1234567890")
    check("  service street", f.get("service_street") == "123 MAIN ST")
    check("  city", f.get("service_city") == "ALBANY")
    check("  ZIP", f.get("service_zip") == "12207")
    check("  nothing was rejected or left ambiguous",
          not f.get("_rejected_fields") and not f.get("_ambiguous_fields"))

    raw = ng.text
    check("the new layer recognises the brand",
          bill_parsers.detect_utility(raw) == "national-grid-ny")
    check("  ...but offers no fields for it, so the old path is the only one",
          bill_parsers.parse_bill(raw)["fields"] == {})

    # ════════════════════════════════════════════════════════════════════════
    section("2. NYSEG")
    # ════════════════════════════════════════════════════════════════════════
    r = bill_parsers.parse_bill(avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                              SYNTH["nyseg_pod"]))
    check(f"the brand is identified ({r['utility']})", r["utility"] == "nyseg")
    check("  first and last name", r["fields"]["account_holder"] == SYNTH["name"])
    check("  service street", r["fields"]["service_street"] == SYNTH["street"])
    check("  city", r["fields"]["service_city"] == SYNTH["city"])
    check("  state", r["fields"]["service_state"] == "NY")
    check("  ZIP", r["fields"]["service_zip"] == SYNTH["zip"])
    check("  account number", r["fields"]["account_number"] == SYNTH["nyseg_acct"])
    check("  POD ID", r["fields"]["pod_id"] == SYNTH["nyseg_pod"])
    check("  the invoice number was NOT taken for anything",
          SYNTH["invoice"] not in r["fields"].values())

    r = bill_parsers.parse_bill(avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                              SYNTH["nyseg_pod"], unit=True))
    check("a unit is captured when the address carries one",
          r["fields"].get("service_unit") == SYNTH["unit"]
          and r["fields"]["service_street"] == SYNTH["street"])

    # ════════════════════════════════════════════════════════════════════════
    section("3. RG&E")
    # ════════════════════════════════════════════════════════════════════════
    r = bill_parsers.parse_bill(avangrid_text("rge", SYNTH["rge_acct"],
                                              SYNTH["rge_pod"]))
    check(f"the brand is identified ({r['utility']})",
          r["utility"] == "rochester-gas-electric")
    check("  account holder", r["fields"]["account_holder"] == SYNTH["name"])
    check("  service address", r["fields"]["service_street"] == SYNTH["street"]
          and r["fields"]["service_city"] == SYNTH["city"])
    check("  account number", r["fields"]["account_number"] == SYNTH["rge_acct"])
    check("  POD ID", r["fields"]["pod_id"] == SYNTH["rge_pod"])

    # The two brands share a template AND name each other in the boilerplate.
    # Picking the first brand word on the page chose wrongly; the weighting
    # that replaced it is what this pair of checks defends.
    check("a NYSEG bill is not read as RG&E",
          bill_parsers.detect_utility(
              avangrid_text("nyseg", SYNTH["nyseg_acct"], SYNTH["nyseg_pod"])) == "nyseg")
    check("an RG&E bill is not read as NYSEG",
          bill_parsers.detect_utility(
              avangrid_text("rge", SYNTH["rge_acct"], SYNTH["rge_pod"]))
          == "rochester-gas-electric")

    # ════════════════════════════════════════════════════════════════════════
    section("4. DOMINION ENERGY VIRGINIA")
    # ════════════════════════════════════════════════════════════════════════
    r = bill_parsers.parse_bill(dominion_text(SYNTH["dom_acct10"], SYNTH["dom_meter"]))
    check(f"the brand is identified ({r['utility']})",
          r["utility"] == "dominion-energy-va")
    check("  account holder", r["fields"]["account_holder"] == SYNTH["name"])
    check("  service street", r["fields"]["service_street"] == SYNTH["street"])
    check("  city", r["fields"]["service_city"] == SYNTH["city"])
    check("  state", r["fields"]["service_state"] == "VA")
    check("  a 10-digit account number is accepted",
          r["fields"]["account_number"] == SYNTH["dom_acct10"])
    check("  a 10-digit meter number is accepted",
          r["fields"]["meter_number"] == SYNTH["dom_meter"])

    r = bill_parsers.parse_bill(dominion_text(SYNTH["dom_acct12"], SYNTH["dom_meter"]))
    check("a 12-digit account number is accepted too",
          r["fields"]["account_number"] == SYNTH["dom_acct12"])

    r = bill_parsers.parse_bill(dominion_text(SYNTH["dom_acct10"], SYNTH["dom_meter"],
                                              two_line_city=False))
    check("the comma-separated address variant reads the same",
          r["fields"]["service_street"] == SYNTH["street"]
          and r["fields"]["service_city"] == SYNTH["city"])

    # ════════════════════════════════════════════════════════════════════════
    section("5. A BAD CANDIDATE IS REJECTED, NEVER TRUSTED")
    # ════════════════════════════════════════════════════════════════════════
    # Wrong prefix for the brand: the shape is right, the identifier is not.
    r = bill_parsers.parse_bill(avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                              SYNTH["rge_pod"]))
    check("a POD ID with another utility's prefix is not filled in",
          "pod_id" not in r["fields"])
    check("  ...and is reported as rejected", "pod_id" in r["rejected"])

    def not_offered(result, field):
        """Not filled in, and reported - as refused or as absent. Which of the
        two channels reported it does not matter to the rep: either way the
        field is theirs to enter and the panel says so."""
        return (field not in result["fields"]
                and field in (result["rejected"] + result["missing"]
                              + result["ambiguous"]))

    r = bill_parsers.parse_bill(avangrid_text("nyseg", "123", SYNTH["nyseg_pod"]))
    check("an account number of the wrong length is not offered",
          not_offered(r, "account_number"))

    r = bill_parsers.parse_bill(dominion_text(SYNTH["dom_acct10"], "80000001"))
    check("a meter number of the wrong length is not offered",
          not_offered(r, "meter_number"))

    r = bill_parsers.parse_bill(dominion_text("90000000011", SYNTH["dom_meter"]))
    check("an 11-digit Dominion account is not offered (10 or 12 only)",
          not_offered(r, "account_number"))

    # THE UTILITY'S RULE IS THE AUTHORITY, NOT THE PARSER'S OWN PATTERN.
    # Here the pattern matches happily and the RULE says the length is wrong -
    # which is what would happen the day a market publishes a different one.
    # The candidate must be dropped, not filled in because the regex liked it.
    r = bill_parsers.parse_bill(
        avangrid_text("nyseg", SYNTH["nyseg_acct"], SYNTH["nyseg_pod"]),
        rule_overrides={"account_lengths": (10,)})
    check("a candidate the utility's own rule refuses is rejected, not filled",
          "account_number" not in r["fields"] and "account_number" in r["rejected"])
    check("  ...and the rest of the bill is still read normally",
          r["fields"].get("pod_id") == SYNTH["nyseg_pod"])

    # ════════════════════════════════════════════════════════════════════════
    section("6. AMBIGUITY IS NOT RESOLVED BY GUESSING")
    # ════════════════════════════════════════════════════════════════════════
    two_pods = avangrid_text("nyseg", SYNTH["nyseg_acct"], SYNTH["nyseg_pod"],
                             extra="Supply Service PoD ID: N01999999999999 ")
    r = bill_parsers.parse_bill(two_pods)
    check("two POD-shaped values leave the field empty",
          "pod_id" not in r["fields"])
    check("  ...and the field is reported as ambiguous", "pod_id" in r["ambiguous"])

    r = bill_parsers.parse_bill(avangrid_text("nyseg", SYNTH["nyseg_acct"], pod=None))
    check("a bill with no POD ID yields no POD ID", "pod_id" not in r["fields"])
    check("  ...and the invoice number is not substituted for it",
          SYNTH["invoice"] not in (r["fields"].values()))
    check("  ...and it is reported missing so the rep is told",
          "pod_id" in r["missing"])

    # ════════════════════════════════════════════════════════════════════════
    section("7. A FILE THAT IS NOT A BILL IS NOT PARSED AS ONE")
    # ════════════════════════════════════════════════════════════════════════
    r = bill_parsers.parse_bill(
        "Dear Pat, thank you for your interest in Dominion Energy. "
        "Our records show 900000000012 on file. Sincerely, the team.")
    check("a letter that merely names a utility is not treated as a bill",
          r["utility"] is None and r["fields"] == {})

    # ════════════════════════════════════════════════════════════════════════
    section("8. END TO END, THROUGH THE REAL UPLOAD")
    # ════════════════════════════════════════════════════════════════════════

    def new_enrollment(tag, zip_code, utility):
        eid = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
        c.post(f"/api/perch/enrollments/{eid}/capacity", headers=h,
               json={"email": f"ocr.{tag}.{eid}@example.com", "zip_code": zip_code,
                     "utility_name": utility})
        return eid

    def upload(eid, paths, set_id=None):
        files = [(io.BytesIO(open(p, "rb").read()), os.path.basename(p)) for p in paths]
        data = {"category": "utility_bill", "files": files}
        if set_id:
            data["document_set_id"] = str(set_id)
        r = c.post(f"/api/enrollments/{eid}/document-sets", headers=h, data=data,
                   content_type="multipart/form-data")
        assert r.status_code == 200, r.data
        return r.get_json()

    eid = new_enrollment("nyseg", "13201", "nyseg")
    body = upload(eid, [text_pdf("nyseg.pdf",
                                 avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                               SYNTH["nyseg_pod"]))])
    got = body["extraction"]["fields"]
    check("the upload succeeds and carries what was read",
          body["file_count"] == 1 and got.get("account_number") == SYNTH["nyseg_acct"])
    check("  ...including the POD ID", got.get("pod_id") == SYNTH["nyseg_pod"])
    check("  ...and the rep is not told to type it all in",
          body["manual_entry_required"] is False)

    eid = new_enrollment("dom", "23220", "dominion-energy-va")
    body = upload(eid, [text_pdf("dom.pdf", dominion_text(SYNTH["dom_acct12"],
                                                          SYNTH["dom_meter"]))])
    got = body["extraction"]["fields"]
    check("a Dominion bill reads end to end",
          got.get("account_number") == SYNTH["dom_acct12"]
          and got.get("meter_number") == SYNTH["dom_meter"])

    # A page-2 field must be found: the server reads the whole set, which is
    # the one thing the in-browser reader cannot do.
    eid = new_enrollment("multi", "13201", "nyseg")
    body = upload(eid, [text_pdf("page1.pdf", "Page one of two. Amount Due $123.45 "),
                        text_pdf("page2.pdf",
                                 avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                               SYNTH["nyseg_pod"]))])
    check("a field on the second page of a set is still found",
          body["extraction"]["fields"].get("pod_id") == SYNTH["nyseg_pod"])
    check("  ...and both pages are stored, in order",
          [f["page_order"] for f in body["files"]] == [0, 1])

    # A scan has no embedded text. Whether this machine has OCR installed or
    # not, the upload must behave identically - the file is stored either way.
    eid = new_enrollment("scan", "23220", "dominion-energy-va")
    body = upload(eid, [scanned_pdf("scan.pdf")])
    check("a scanned bill still uploads and stores one page",
          body["file_count"] == 1 and len(body["files"]) == 1)
    check("  ...and the rep is told whether to enter fields by hand",
          isinstance(body["manual_entry_required"], bool))

    # ════════════════════════════════════════════════════════════════════════
    section("8b. READING A BILL WRITES NOTHING, AND CORRECTIONS SURVIVE")
    # ════════════════════════════════════════════════════════════════════════
    # Extraction is a READ. It offers values to the form; the rep decides, and
    # only a save writes. So a second upload must not quietly replace a value
    # the rep corrected after the first one.
    eid = new_enrollment("persist", "13201", "nyseg")
    bill = text_pdf("persist.pdf", avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                                 SYNTH["nyseg_pod"]))
    upload(eid, [bill])
    corrected_acct, corrected_pod = "90000000099", "N01000000000099"
    r = c.patch(f"/api/enrollments/{eid}", headers=h, json={
        "customer": {"first_name": "Robin", "last_name": "Corrected",
                     "email": f"persist.{eid}@example.com", "phone": "5185550142"},
        "service_address": {"street": "1 CORRECTED ST", "unit": "", "city": "Ithaca",
                            "state": "NY", "zip": "14850"},
        "billing_address": {"same_as_service": True},
        "utility_account": {"utility_name": "nyseg",
                            "account_number": corrected_acct,
                            "secondary_account_identifier": corrected_pod}})
    check(f"the rep's corrections save ({r.status_code})", r.status_code == 200)

    # Save & Exit, then reopen.
    detail = c.get(f"/api/enrollments/{eid}", headers=h).get_json()
    ua = detail.get("utility_account") or {}
    check("the corrected POD ID comes back on reopen",
          ua.get("secondary_account_identifier") == corrected_pod)
    check("the corrected street comes back on reopen",
          (detail.get("service_address") or {}).get("street") == "1 CORRECTED ST")
    check("  ...and the corrected name too",
          (detail.get("customer") or {}).get("last_name") == "Corrected")

    # Upload the SAME bill again - the values on it differ from the rep's.
    upload(eid, [bill])
    after = c.get(f"/api/enrollments/{eid}", headers=h).get_json()
    ua2 = after.get("utility_account") or {}
    check("re-reading the bill does not overwrite the corrected POD ID",
          ua2.get("secondary_account_identifier") == corrected_pod)
    check("  ...nor the corrected address",
          (after.get("service_address") or {}).get("street") == "1 CORRECTED ST")
    check("  ...nor the corrected name",
          (after.get("customer") or {}).get("last_name") == "Corrected")
    check("  ...and no enrollment was submitted by reading a bill",
          after.get("status") in ("Draft", "draft"))

    # ════════════════════════════════════════════════════════════════════════
    section("9. THE UPLOAD ITSELF IS UNCHANGED")
    # ════════════════════════════════════════════════════════════════════════
    # Every assertion here is run with extraction FORCED TO FAIL, which is the
    # state this system was in for these utilities before any of this work.
    # If upload behaviour depended on reading the bill, these would diverge.
    real_extract = extraction_engine.LocalExtractor.extract

    def dead_extract(self, file_paths, category="utility_bill", utility_hint=None):
        raise RuntimeError("extraction unavailable")

    def rows(eid):
        return query("SELECT id, document_set_id, page_order, original_filename FROM documents "
                     "WHERE enrollment_id = ? ORDER BY page_order, id", (eid,))

    pages = [text_pdf(f"ng_p{i}.pdf", f"Page {i}. Amount Due $1.0{i} ") for i in (1, 2, 3)]
    shapes = {}
    try:
        for mode in ("with_ocr", "without_ocr"):
            if mode == "without_ocr":
                extraction_engine.LocalExtractor.extract = dead_extract
            eid = new_enrollment(mode, "13201", "nyseg")
            body = upload(eid, pages)
            set_id = body["document_set_id"]
            after_upload = rows(eid)

            # removal of one page
            doc_id = after_upload[1]["id"]
            rm = c.delete(f"/api/enrollments/{eid}/documents/{doc_id}", headers=h)
            after_remove = rows(eid)

            # replacement: a new file into the SAME set
            body2 = upload(eid, [text_pdf(f"repl_{mode}.pdf", "Replacement page. ")],
                           set_id=set_id)
            after_replace = rows(eid)

            # Save & Exit hydration
            detail = c.get(f"/api/enrollments/{eid}", headers=h).get_json()
            docs = detail.get("documents") or []
            # SHAPE, not identity: the set id differs between two enrollments
            # by definition. What must match is how many documents reopen, in
            # what order, and that they all belong to ONE set.
            hydrated = (len(docs),
                        sorted(d.get("page_order") for d in docs),
                        len({d.get("document_set_id") for d in docs}))

            shapes[mode] = {
                "status": 200,
                "set_is_new": set_id is not None,
                "rows_after_upload": [(r["page_order"], r["original_filename"]) for r in after_upload],
                "same_set": len({r["document_set_id"] for r in after_upload}) == 1,
                "remove_status": rm.status_code,
                "rows_after_remove": len(after_remove),
                "replace_reused_set": body2["document_set_id"] == set_id,
                "rows_after_replace": len(after_replace),
                "hydrated": hydrated,
            }
    finally:
        extraction_engine.LocalExtractor.extract = real_extract

    a, b = shapes["with_ocr"], shapes["without_ocr"]
    check(f"same number of rows for the same files ({len(a['rows_after_upload'])})",
          len(a["rows_after_upload"]) == len(b["rows_after_upload"]) == 3)
    check("same page ordering",
          [p for p, _ in a["rows_after_upload"]] == [p for p, _ in b["rows_after_upload"]]
          == [0, 1, 2])
    check("one document set per upload, both ways", a["same_set"] and b["same_set"])
    check(f"same removal result ({a['remove_status']})",
          a["remove_status"] == b["remove_status"] == 200)
    check("same row count after removal",
          a["rows_after_remove"] == b["rows_after_remove"] == 2)
    check("replacement joins the existing set, both ways",
          a["replace_reused_set"] and b["replace_reused_set"])
    check("same row count after replacement",
          a["rows_after_replace"] == b["rows_after_replace"] == 3)
    check("same Save & Exit hydration", a["hydrated"] == b["hydrated"])

    # And the documents handed to the Perch boundary are the stored ones,
    # chosen by the same rule as before - extraction has no say in it.
    eid = new_enrollment("perch", "13201", "nyseg")
    body = upload(eid, pages)
    stored_paths = [r["original_filename"] for r in rows(eid)]
    captured = {}
    real_enroll = adapter.create_enrollment

    def spy(enrollment_id, payload, user_id=None):
        captured["bills"] = list(
            payload["utility_accounts"][0].get("utility_bills") or [])
        return {"customer_type": "Residential",
                "next_step_url": "https://example.invalid/contracts"}

    c.patch(f"/api/enrollments/{eid}", headers=h, json={
        "customer": {"first_name": "Pat", "last_name": "Sample",
                     "email": f"perch.boundary.{eid}@example.com", "phone": "5185550142"},
        "service_address": {"street": SYNTH["street"], "unit": "", "city": "Ithaca",
                            "state": "NY", "zip": "14850"},
        "billing_address": {"same_as_service": True},
        "utility_account": {"utility_name": "nyseg",
                            "account_number": SYNTH["nyseg_acct"],
                            "secondary_account_identifier": SYNTH["nyseg_pod"]}})
    adapter.create_enrollment = spy
    try:
        rr = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                    json={"document_id": rows(eid)[0]["id"]})
    finally:
        adapter.create_enrollment = real_enroll
    check(f"the enrollment reaches the integration boundary ({rr.status_code})",
          rr.status_code == 200)
    check(f"every stored page reaches the Perch boundary "
          f"({len(captured.get('bills') or [])} of {len(stored_paths)})",
          len(captured.get("bills") or []) == len(stored_paths))

    # ════════════════════════════════════════════════════════════════════════
    section("10. NOTHING OFF A BILL IS EVER WRITTEN TO A LOG")
    # ════════════════════════════════════════════════════════════════════════
    r = bill_parsers.parse_bill(avangrid_text("nyseg", SYNTH["nyseg_acct"],
                                              SYNTH["nyseg_pod"]))
    line = bill_parsers.diagnostic(r)
    secrets = [SYNTH["name"], SYNTH["street"], SYNTH["city"], SYNTH["nyseg_acct"],
               SYNTH["nyseg_pod"], SYNTH["zip"]]
    check(f"the diagnostic line names field types only: {line}",
          not any(s in line for s in secrets))
    check("  ...and still says which utility and what was found",
          "utility=nyseg" in line and "found=" in line)

    logged = query("SELECT details_json FROM audit_logs WHERE action = 'document_set_uploaded'")
    blob = " ".join((row["details_json"] or "") for row in logged)
    check("the upload audit record carries no value read off a bill",
          not any(s in blob for s in secrets))

    # ════════════════════════════════════════════════════════════════════════
    section("11. THE FORM STILL DECIDES WHAT IS VALID")
    # ════════════════════════════════════════════════════════════════════════
    check("a prefilled field is only ever written into an empty one",
          "if(String(el.value || '').trim()) return false;" in JS)
    check("the POD ID is checked again in the browser before it is offered",
          "!podLooksValid(currentUtilityRule(), value)" in JS)
    check("so are the account and meter numbers",
          "validateMarketAccountField(key, value)" in JS)
    check("readiness still turns on the POD ID rule, not on what was read",
          "const podOk = podLooksValid(rule, document.getElementById('c-pod').value);" in JS)
    check("the service state follows an address this pass filled, nothing else",
          "found.street && found.zip" in JS)
    # The in-browser reader can fail on a PDF the server reads perfectly. If
    # the server's reading were applied inside the try, that is exactly the
    # case where the rep would be told to type everything by hand for nothing.
    tail = JS.split("const text = await extractTextFromFile(f);")[1]
    check("the server's reading is applied even when the browser's read fails",
          tail.index("applyServerBillFields") > tail.index("} catch(err){"))
    check("  ...and a field this utility does not ask for is never filled",
          "SERVER_FIELD_WRAPPER" in JS
          and "wrap.style.display === 'none'" in JS)
    check("extraction never submits anything",
          "applyServerBillFields" in JS and "submitEnrollment()" not in
          JS.split("function applyServerBillFields")[1].split("\n}")[0])

    print("\n" + "=" * 72)
    print("BILL OCR - NYSEG / RG&E / DOMINION - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
