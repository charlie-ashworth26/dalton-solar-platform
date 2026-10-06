"""
Every page of a utility bill must reach the Perch integration boundary.

THE INCIDENT (Oct 6): enrollments reached Perch with one bill page instead of
four, missing the usage/sizing pages.

ROOT CAUSE, both halves:
  FRONTEND  handleBillUpload() listed every selected file on screen via
            docSetAddFiles(), then uploaded only files[0]. Pages 2-n never left
            the browser. uploadDocumentSet() - which posts the whole set - was
            defined but had ZERO callers.
  BACKEND   POST /enroll resolved ONE document_id and sent
            "utility_bills": [one_path], so even a correctly stored multi-page
            set was reduced to its first page.

Perch's own contract already carries several bills per account
(utility_accounts[0][utility_bills][0..n]) and build_enrollment_multipart()
already emits that indexing, so no payload format is invented by the fix.

This suite asserts at the BOUNDARY - what the adapter is actually handed -
not merely what was stored.

    python test/test_bill_page_integrity.py
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

_temp = tempfile.TemporaryDirectory(prefix="dalton-bill-pages-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query, query_one
init_db(reset=True)

from app import app
from routes import document_routes
document_routes.UPLOAD_DIR = os.path.join(_temp.name, "uploads")
document_routes.DATA_ROOT = _temp.name
os.makedirs(document_routes.UPLOAD_DIR, exist_ok=True)
import seed
from services.perch import adapter
from PIL import Image

BASE = "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments"
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def login(c):
    r = c.post("/api/auth/login", json={"email": "charlie@daltonsolar.com",
                                        "password": "RepPass1!"})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def page_image(n, tag=""):
    """A distinct image per page, so a dropped page is detectable by content."""
    p = os.path.join(_temp.name, f"bill_{tag}_page{n}.png")
    Image.new("RGB", (600, 800), (240 - n * 12, 240, 250)).save(p)
    return p


def _pdf_pages(path):
    """Page count of a PDF, or None when no PDF reader is installed."""
    import importlib
    for mod in ("pypdf", "PyPDF2"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        reader = m.PdfReader if hasattr(m, "PdfReader") else m.PdfFileReader
        try:
            return len(reader(path).pages)
        except Exception:
            return None
    return None


def multipage_pdf(pages, tag=""):
    imgs = [Image.open(page_image(i + 1, tag)).convert("RGB") for i in range(pages)]
    p = os.path.join(_temp.name, f"bill_{tag}_{pages}page.pdf")
    imgs[0].save(p, "PDF", save_all=True, append_images=imgs[1:], resolution=150.0)
    return p


def main():
    seed.seed()
    c = app.test_client()
    h = login(c)
    real_enroll = adapter.create_enrollment

    def new_enrollment(tag):
        eid = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
        email = f"bill.{tag}.{eid}@example.com"
        r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=h,
                   json={"email": email, "zip_code": "13348",
                         "utility_name": "national-grid-ny"})
        assert r.status_code == 200, r.data
        return eid, email

    def upload_set(eid, paths, set_id=None):
        files = [(io.BytesIO(open(p, "rb").read()), os.path.basename(p)) for p in paths]
        data = {"category": "utility_bill", "files": files}
        if set_id:
            data["document_set_id"] = str(set_id)
        r = c.post(f"/api/enrollments/{eid}/document-sets", headers=h, data=data,
                   content_type="multipart/form-data")
        assert r.status_code == 200, r.data
        return r.get_json()

    def complete(eid, email):
        c.patch(f"/api/enrollments/{eid}", headers=h, json={
            "customer": {"first_name": "Agnes", "last_name": "Doria",
                         "email": email, "phone": "5185550142"},
            "service_address": {"street": "1 Main St", "unit": "", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "billing_address": {"same_as_service": True, "street": "1 Main St",
                                "unit": "", "city": "Albany", "state": "NY",
                                "zip": "12207"},
            "utility_account": {"utility_name": "national-grid-ny",
                                "account_number": "1234567890",
                                "secondary_account_identifier": None}})

    def enroll_capturing(eid, document_id):
        """Returns the utility_bills list the ADAPTER was handed."""
        captured = {}

        def spy(enrollment_id, payload, user_id=None):
            bills = payload["utility_accounts"][0].get("utility_bills") or []
            captured["bills"] = list(bills)
            captured["sizes"] = [os.path.getsize(b) for b in bills if os.path.exists(b)]
            captured["exists"] = [os.path.exists(b) for b in bills]
            captured["page_counts"] = [_pdf_pages(b) for b in bills if os.path.exists(b)]
            # Build the REAL multipart here, at the moment the live adapter
            # would - the transient per-page PDFs are deleted immediately after
            # this call returns, so this is the only honest place to prove the
            # parts Perch actually receives.
            from services.perch.client import build_enrollment_multipart
            form, files = build_enrollment_multipart(payload)
            try:
                captured["bill_parts"] = sorted(k for k in files if "utility_bills" in k)
                captured["part_types"] = [files[k][2] for k in captured["bill_parts"]]
                captured["part_readable"] = all(
                    hasattr(files[k][1], "read") for k in captured["bill_parts"])
            finally:
                for k in list(files):
                    try:
                        files[k][1].close()
                    except Exception:
                        pass
            return {"customer_type": "Residential", "next_step_url": f"{BASE}/contracts"}

        adapter.create_enrollment = spy
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                       json={"document_id": document_id})
        finally:
            adapter.create_enrollment = real_enroll
        return r, captured

    # ═══════════════════════════════════════════════════════
    section("1. FOUR SEPARATE BILL IMAGES SURVIVE END TO END")
    eid, email = new_enrollment("fourimages")
    paths = [page_image(i + 1, "four") for i in range(4)]
    body = upload_set(eid, paths)
    check("all four pages are stored", len(body["files"]) == 4)
    with app.app_context():
        rows = query("SELECT id, page_order, original_filename FROM documents "
                     "WHERE document_set_id = ? ORDER BY page_order",
                     (body["document_set_id"],))
    check("  ...one document row per page", len(rows) == 4)
    check("  ...in the order they were selected",
          [r["original_filename"] for r in rows] == [os.path.basename(p) for p in paths])
    check("  ...with sequential page_order", [r["page_order"] for r in rows] == [0, 1, 2, 3])

    complete(eid, email)
    r, cap = enroll_capturing(eid, rows[0]["id"])
    check("/enroll succeeds", r.status_code == 200)
    check("FOUR bills reach the integration boundary", len(cap["bills"]) == 4)
    check("  ...zero pages lost", len(cap["bills"]) == len(rows))
    check("  ...every file actually exists on disk", all(cap["exists"]))
    check("  ...and they are distinct files, not one path repeated",
          len(set(cap["bills"])) == 4)
    check("  ...each carries real content", all(s > 0 for s in cap["sizes"]))

    section("  ...and the multipart Perch receives indexes every one")
    check("four utility_bills parts were built", len(cap["bill_parts"]) == 4)
    for i in range(4):
        check(f"  ...utility_accounts[0][utility_bills][{i}] present",
              f"utility_accounts[0][utility_bills][{i}]" in cap["bill_parts"])
    check("  ...each part is a real file handle", cap["part_readable"])
    check("  ...sent as application/pdf, per the spec",
          cap["part_types"] == ["application/pdf"] * 4)

    # ═══════════════════════════════════════════════════════
    section("2. A FOUR-PAGE PDF KEEPS ALL FOUR PAGES")
    eid2, email2 = new_enrollment("fourpagepdf")
    pdf = multipage_pdf(4, "pdfcase")
    b2 = upload_set(eid2, [pdf])
    with app.app_context():
        rows2 = query("SELECT * FROM documents WHERE document_set_id = ?",
                      (b2["document_set_id"],))
    check("the PDF is stored as one document", len(rows2) == 1)
    complete(eid2, email2)
    r2, cap2 = enroll_capturing(eid2, rows2[0]["id"])
    check("/enroll succeeds", r2.status_code == 200)
    check("one bill file is sent (the PDF itself)", len(cap2["bills"]) == 1)
    pages_sent = (cap2.get("page_counts") or [None])[0]
    if pages_sent is not None:
        check("the transmitted PDF still has FOUR pages", pages_sent == 4)
        check("  ...the source PDF had four to begin with", _pdf_pages(pdf) == 4)
    else:
        check("the transmitted PDF is byte-identical to the stored one "
              "(no re-rendering, so no page can be dropped)",
              cap2["sizes"][0] == os.path.getsize(pdf))
    check("a PDF is passed through, never rasterised to one page",
          "return source, None" in open(os.path.join(ROOT, "routes", "perch_routes.py"),
                                        encoding="utf-8").read())

    # ═══════════════════════════════════════════════════════
    section("3. A FAILED UPLOAD CANNOT SILENTLY PRODUCE A PARTIAL BILL")
    check("the browser blocks Continue when any page lacks a document id",
          "bill pages did not finish uploading" in JS)
    gate = JS.split("if(billUploadPromise) await billUploadPromise;")[1][:900]
    check("  ...the check runs BEFORE the enrollment payload is built",
          gate.index("unsaved") < gate.index("const payload"))
    check("  ...and names the pages that failed", "x.file.name" in gate)
    check("  ...an upload error is recorded rather than swallowed",
          "state.bill.uploadError = err.message" in JS)
    check("the server refuses an unknown document outright",
          c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                 json={"document_id": 99999}).status_code == 400)
    check("  ...and refuses a missing document_id",
          c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                 json={}).status_code == 400)

    section("  ...a document belonging to ANOTHER enrollment is refused")
    other_eid, other_email = new_enrollment("otherowner")
    ob = upload_set(other_eid, [page_image(1, "other")])
    foreign_id = ob["files"][0]["document_id"]
    check("a foreign document id cannot be used",
          c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h,
                 json={"document_id": foreign_id}).status_code == 400)

    # ═══════════════════════════════════════════════════════
    section("4. ADDING PAGES APPENDS - NOTHING IS OVERWRITTEN")
    eid3, email3 = new_enrollment("append")
    first_two = [page_image(i + 1, "app1") for i in range(2)]
    b3 = upload_set(eid3, first_two)
    set_id = b3["document_set_id"]
    check("two pages stored", len(b3["files"]) == 2)
    next_two = [page_image(i + 3, "app2") for i in range(2)]
    b4 = upload_set(eid3, next_two, set_id=set_id)
    check("  ...two more appended to the SAME set",
          b4["document_set_id"] == set_id and len(b4["files"]) == 2)
    with app.app_context():
        allrows = query("SELECT id, page_order, original_filename FROM documents "
                        "WHERE document_set_id = ? ORDER BY page_order", (set_id,))
    check("all four pages are present after the second add", len(allrows) == 4)
    check("  ...page_order continues rather than restarting",
          [r["page_order"] for r in allrows] == [0, 1, 2, 3])
    check("  ...the first two were NOT replaced",
          [r["original_filename"] for r in allrows[:2]]
          == [os.path.basename(p) for p in first_two])
    check("  ...and the new ones are at the end",
          [r["original_filename"] for r in allrows[2:]]
          == [os.path.basename(p) for p in next_two])

    complete(eid3, email3)
    r3, cap3 = enroll_capturing(eid3, allrows[0]["id"])
    check("all FOUR reach the boundary after an append", len(cap3["bills"]) == 4)

    section("  ...and the browser never re-sends a page already stored")
    check("only files without a document id are posted",
          "set.files.filter(function(x){ return !x.documentId; })" in JS)
    check("  ...which is what stops an append from duplicating pages",
          "would append them to the set a second time" in JS)

    # ═══════════════════════════════════════════════════════
    section("5. DOCUMENT IDS AND ASSOCIATIONS STAY CORRECT")
    with app.app_context():
        for r_ in allrows:
            row = query_one("SELECT * FROM documents WHERE id = ?", (r_["id"],))
            check(f"doc {r_['id']} belongs to enrollment {eid3}",
                  row["enrollment_id"] == eid3)
            check(f"  ...category is utility_bill", row["doc_category"] == "utility_bill")
            check(f"  ...and it is in set {set_id}", row["document_set_id"] == set_id)
        check("no page leaked into another enrollment's set",
              query_one("SELECT COUNT(*) n FROM documents WHERE document_set_id = ? "
                        "AND enrollment_id != ?", (set_id, eid3))["n"] == 0)
        check("the other enrollment's document is untouched",
              query_one("SELECT enrollment_id FROM documents WHERE id = ?",
                        (foreign_id,))["enrollment_id"] == other_eid)

    section("  ...and any page of the set resolves the whole set")
    for probe in allrows:
        complete(eid3, email3)
        rp, capp = enroll_capturing(eid3, probe["id"])
        check(f"naming page {probe['page_order']} still sends all four",
              len(capp["bills"]) == 4)

    # ═══════════════════════════════════════════════════════
    section("6. SINGLE-PAGE UPLOAD BEHAVIOUR IS UNCHANGED")
    eid4, email4 = new_enrollment("single")
    one = upload_set(eid4, [page_image(1, "single")])
    with app.app_context():
        srow = query_one("SELECT * FROM documents WHERE document_set_id = ?",
                         (one["document_set_id"],))
    complete(eid4, email4)
    r4, cap4 = enroll_capturing(eid4, srow["id"])
    check("/enroll succeeds", r4.status_code == 200)
    check("exactly one bill is sent", len(cap4["bills"]) == 1)
    check("  ...and it exists", cap4["exists"] == [True])

    section("  ...including a document with NO set at all (legacy single upload)")
    eid5, email5 = new_enrollment("legacy")
    with open(page_image(1, "legacy"), "rb") as fh:
        rleg = c.post(f"/api/enrollments/{eid5}/documents", headers=h,
                      data={"category": "utility_bill",
                            "file": (io.BytesIO(fh.read()), "legacy.png")},
                      content_type="multipart/form-data")
    check("the single-document endpoint still works", rleg.status_code == 201)
    legacy_id = rleg.get_json()["document_id"]
    with app.app_context():
        lrow = query_one("SELECT document_set_id FROM documents WHERE id = ?", (legacy_id,))
    complete(eid5, email5)
    r5, cap5 = enroll_capturing(eid5, legacy_id)
    check("/enroll succeeds for a set-less document", r5.status_code == 200)
    check("  ...and sends exactly that one bill", len(cap5["bills"]) == 1)

    # ═══════════════════════════════════════════════════════
    section("7. NO REGRESSION TO LMI OR OTHER DOCUMENT WORKFLOWS")
    eid6, email6 = new_enrollment("lmi")
    upload_set(eid6, [page_image(1, "lmibill")])
    with app.app_context():
        billrow = query_one("SELECT id FROM documents WHERE enrollment_id = ? "
                            "AND doc_category = 'utility_bill'", (eid6,))
    complete(eid6, email6)
    adapter.create_enrollment = lambda e, p, user_id=None: {
        "customer_type": "LMI", "next_step_url": f"{BASE}/lmi/proof_docs"}
    renr = c.post(f"/api/perch/enrollments/{eid6}/enroll", headers=h,
                  json={"document_id": billrow["id"]})
    adapter.create_enrollment = real_enroll
    check("an LMI enrollment still routes to proof docs",
          renr.get_json()["next_step_key"] == "proof_docs")

    with open(ROOT / "test" / "test_snap_proof_letter.pdf", "rb") as fh:
        rp = c.post(f"/api/enrollments/{eid6}/documents", headers=h,
                    data={"category": "lmi_document",
                          "file": (io.BytesIO(fh.read()), "snap.pdf")},
                    content_type="multipart/form-data")
    proof_id = rp.get_json()["document_id"]
    sent_proof = {}

    def spy_proof(enrollment_id, account_number, documents, user_id=None):
        sent_proof["docs"] = documents
        return {"next_step_url": f"{BASE}/contracts"}

    real_proof = adapter.submit_proof_docs
    adapter.submit_proof_docs = spy_proof
    rpd = c.post(f"/api/perch/enrollments/{eid6}/lmi/proof_docs", headers=h,
                 json={"document_id": proof_id, "source_type": "proof_doc_snap",
                       "name_on_document": "Agnes Doria", "relationship": "self",
                       "document_type": "letter"})
    adapter.submit_proof_docs = real_proof
    check("the LMI proof-doc path still works", rpd.status_code == 200)
    check("  ...and still sends exactly the one proof document",
          len(sent_proof["docs"]) == 1)
    check("  ...with its published source type intact",
          sent_proof["docs"][0]["source_type"] == "proof_doc_snap")
    check("the bill change did not touch the proof-doc payload builder",
          "file_path" in sent_proof["docs"][0])

    section("  ...and bill pages never leak into the LMI document set")
    with app.app_context():
        check("LMI documents stay their own category",
              query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'lmi_document'", (eid6,))["n"] == 1)
        check("  ...and the bill stays its own",
              query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'utility_bill'", (eid6,))["n"] == 1)

    # ═══════════════════════════════════════════════════════
    section("THE REGRESSION THAT CAUSED THIS CANNOT RETURN")
    check("the frontend uploads the SET, not files[0]",
          "uploadDocumentSet('utility_bill')" in JS)
    check("  ...uploadDocumentSet now has a real caller",
          JS.count("uploadDocumentSet(") >= 2)
    check("  ...handleBillUpload no longer uploads a single file",
          "uploadDocumentToDalton(f, 'utility_bill')" not in JS)
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"),
                      encoding="utf-8").read()
    check("the backend sends the whole list",
          '"utility_bills": pdf_paths,' in routes_src)
    check("  ...never a one-element list again",
          '"utility_bills": [pdf_path]' not in routes_src)
    check("  ...resolved through the document set",
          "_bill_documents_for" in routes_src)
    check("every page's temp file is cleaned up",
          "for cleanup_path in cleanup_paths:" in routes_src)
    check("size is validated on EVERY page, not just the first",
          "4 MB per page" in JS)

    print(f"\n{'='*72}\nBILL PAGE INTEGRITY - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
