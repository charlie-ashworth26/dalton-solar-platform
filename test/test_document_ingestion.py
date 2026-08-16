"""
Document ingestion + OCR robustness.

Run: python test/test_document_ingestion.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services import extraction_engine as engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BILL_PDF = os.path.join(ROOT, "test", "sample_utility_bill.pdf")
LMI_PDF = os.path.join(ROOT, "test", "sample_lmi_doc.pdf")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def png_bytes():
    """A tiny valid PNG (1x1) so PIL can open it."""
    import base64
    return base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def jpg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="JPEG")
    return buf.getvalue()


def upload_set(c, h, eid, category, files, set_id=None):
    data = {"category": category,
            "files": [(io.BytesIO(b), n) for b, n in files]}
    if set_id:
        data["document_set_id"] = str(set_id)
    return c.post(f"/api/enrollments/{eid}/document-sets", headers=h,
                  data=data, content_type="multipart/form-data")


def force_no_ocr(on):
    if on:
        os.environ["DALTON_FORCE_OCR_UNAVAILABLE"] = "1"
    else:
        os.environ.pop("DALTON_FORCE_OCR_UNAVAILABLE", None)
    engine.ocr_available(refresh=True)


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    def new_enrollment():
        return c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]

    # ═══════════════════════════════════════════════════════
    section("CAPABILITY DETECTION — a python package is not a binary")
    force_no_ocr(False)
    real = engine.ocr_available()
    check("ocr_available() returns a boolean", isinstance(real, bool))
    st = engine.ocr_status()
    check("status reports availability", st["ocr_available"] == real)
    force_no_ocr(True)
    check("missing binary is detected", engine.ocr_available() is False)
    check("status explains the consequence", "manually" in (engine.ocr_status()["note"] or ""))
    check("engine is None when unavailable", engine.ocr_status()["engine"] is None)
    force_no_ocr(False)

    section("THE ORIGINAL 500 — missing tesseract must never 500")
    force_no_ocr(True)
    eid = new_enrollment()
    r = c.post(f"/api/enrollments/{eid}/documents", headers=rep,
               data={"category": "utility_bill", "file": (io.BytesIO(png_bytes()), "bill.png")},
               content_type="multipart/form-data")
    check("single-file upload does NOT 500", r.status_code != 500)
    check("  ...it succeeds", r.status_code in (200, 201))
    check("  ...and the document is recorded", bool(r.get_json().get("document_id")))
    with app.app_context():
        n = query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=?", (eid,))["n"]
    check("  ...the uploaded file is PRESERVED, not lost", n == 1)

    r2 = upload_set(c, rep, eid, "utility_bill", [(png_bytes(), "photo.png")])
    check("document-set upload does NOT 500", r2.status_code == 200)
    check("  ...status is ocr_unavailable, not a lie",
          r2.get_json()["extraction"]["status"] == "ocr_unavailable")
    check("  ...never claims success", r2.get_json()["extraction"]["usable"] is False)
    force_no_ocr(False)

    section("FILE TYPES")
    eid = new_enrollment()
    with open(BILL_PDF, "rb") as fh:
        pdf = fh.read()
    r = upload_set(c, rep, eid, "utility_bill", [(pdf, "bill.pdf")])
    b = r.get_json()
    check("text PDF extracts", b["extraction"]["status"] in ("success", "partial"))
    check("  ...fields were read", len(b["extraction"]["fields"]) > 0)
    check("  ...confidence is None for a text PDF (pdfplumber has none)",
          b["extraction"]["confidence"] is None)
    check("  ...no manual entry needed", b["manual_entry_required"] is False)

    for name, data in [("PNG", png_bytes()), ("JPEG", jpg_bytes())]:
        rr = upload_set(c, rep, new_enrollment(), "utility_bill", [(data, f"x.{name.lower()}")])
        check(f"{name} upload accepted without error", rr.status_code == 200)
        check(f"  ...{name} never 500s", rr.status_code != 500)

    section("MALFORMED / CORRUPT FILES")
    rr = upload_set(c, rep, new_enrollment(), "utility_bill", [(b"not-an-image-at-all", "broken.png")])
    check("corrupt image does not 500", rr.status_code == 200)
    check("  ...status is a controlled failure",
          rr.get_json()["extraction"]["status"] in ("unsupported", "unreadable", "ocr_unavailable"))
    check("  ...the file is still stored", rr.get_json()["file_count"] == 1)
    rr = upload_set(c, rep, new_enrollment(), "utility_bill", [(b"%PDF-1.4 broken", "broken.pdf")])
    check("corrupt PDF does not 500", rr.status_code == 200)
    check("  ...reported as unsupported/unreadable",
          rr.get_json()["extraction"]["status"] in ("unsupported", "unreadable"))

    section("MULTI-FILE: one logical document, many files")
    eid = new_enrollment()
    r = upload_set(c, rep, eid, "utility_bill",
                   [(png_bytes(), "page.jpg"), (png_bytes(), "page.jpg"), (png_bytes(), "page.jpg")])
    b = r.get_json()
    sid = b["document_set_id"]
    check("three files accepted as ONE set", b["file_count"] == 3)
    check("  ...deterministic page order", [f["page_order"] for f in b["files"]] == [0, 1, 2])
    check("  ...one extraction for the whole set", "status" in b["extraction"])
    with app.app_context():
        paths = [x["stored_path"] for x in
                 query("SELECT stored_path FROM documents WHERE document_set_id=?", (sid,))]
    check("duplicate filenames do NOT collide on disk", len(set(paths)) == 3)

    r = upload_set(c, rep, eid, "utility_bill", [(png_bytes(), "page4.jpg")], set_id=sid)
    check("a file can be ADDED to an existing set", r.get_json()["file_count"] == 4)
    check("  ...appended at the end", r.get_json()["files"][0]["page_order"] == 3)

    files = c.get(f"/api/enrollments/{eid}/document-sets/{sid}", headers=rep).get_json()["files"]
    rm = c.delete(f"/api/enrollments/{eid}/document-sets/{sid}/files/{files[1]['id']}", headers=rep)
    check("a single file can be REMOVED", rm.status_code == 200)
    after = c.get(f"/api/enrollments/{eid}/document-sets/{sid}", headers=rep).get_json()
    check("  ...count drops", after["file_count"] == 3)
    check("  ...page_order stays dense and ordered",
          [f["page_order"] for f in after["files"]] == [0, 1, 2])

    section("MULTI-FILE: LMI proof (front/back)")
    eid = new_enrollment()
    r = upload_set(c, rep, eid, "lmi_document",
                   [(png_bytes(), "medicaid-front.jpg"), (png_bytes(), "medicaid-back.jpg")])
    check("two LMI images form one set", r.get_json()["file_count"] == 2)
    check("  ...order preserved", [f["page_order"] for f in r.get_json()["files"]] == [0, 1])

    section("BILL FALLBACK — manual entry offered, nothing invented")
    force_no_ocr(True)
    r = upload_set(c, rep, new_enrollment(), "utility_bill", [(png_bytes(), "bill.jpg")])
    b = r.get_json()
    check("unreadable bill flags manual entry", b["manual_entry_required"] is True)
    check("  ...message tells the rep what to do",
          "manually" in (b["message"] or "").lower())
    check("  ...no fields were invented", b["extraction"]["fields"] == {})
    check("  ...no fabricated confidence", b["extraction"]["confidence"] is None)
    check("  ...upload still preserved", b["file_count"] == 1)

    section("LMI FALLBACK — never asks the rep to transcribe")
    r = upload_set(c, rep, new_enrollment(), "lmi_document", [(png_bytes(), "proof.jpg")])
    b = r.get_json()
    check("unreadable LMI does NOT demand manual entry", b["manual_entry_required"] is False)
    check("  ...message points at the program dropdown",
          "program" in (b["message"] or "").lower())
    check("  ...never asks for transcription",
          "transcribe" not in (b["message"] or "").lower()
          and "manually" not in (b["message"] or "").lower())
    check("  ...the proof is preserved", b["file_count"] == 1)
    force_no_ocr(False)

    section("EXTRACTION RESULT MODEL — honest statuses")
    S = engine.ExtractionStatus
    check("distinct statuses exist",
          len({S.SUCCESS, S.PARTIAL, S.UNREADABLE, S.OCR_UNAVAILABLE,
               S.UNSUPPORTED, S.ERROR}) == 6)
    check("needs-manual set excludes success/partial",
          S.SUCCESS not in S.NEEDS_MANUAL and S.PARTIAL not in S.NEEDS_MANUAL)
    ok = engine.ExtractionResult(S.SUCCESS, text="x", fields={"a": 1})
    check("success is usable", ok.usable is True and ok.needs_manual_entry is False)
    bad = engine.ExtractionResult(S.OCR_UNAVAILABLE)
    check("ocr_unavailable is not usable", bad.usable is False)
    check("  ...and needs manual entry", bad.needs_manual_entry is True)
    check("confidence defaults to None, never 0", bad.confidence is None)
    empty = engine.get_extractor().extract([], "utility_bill")
    check("no files -> unsupported, not a crash", empty.status == S.UNSUPPORTED)

    section("PROVIDER BOUNDARY — a future AI extractor plugs in")
    check("abstract boundary exists", hasattr(engine, "DocumentExtractor"))
    check("local implementation is behind it",
          issubclass(engine.LocalExtractor, engine.DocumentExtractor))
    check("default provider is local", engine.get_extractor().name == "local")

    class FakeVision(engine.DocumentExtractor):
        name = "fake_vision"
        def extract(self, file_paths, category="utility_bill"):
            return engine.ExtractionResult(S.SUCCESS, text="vision",
                                           fields={"account_number": "999"},
                                           confidence=0.91, provider=self.name)

    engine.register_extractor("fake_vision", FakeVision)
    os.environ["DALTON_EXTRACTOR"] = "fake_vision"
    try:
        check("a new provider is selectable", engine.get_extractor().name == "fake_vision")
        eid = new_enrollment()
        r = upload_set(c, rep, eid, "utility_bill", [(png_bytes(), "b.jpg")])
        b = r.get_json()
        check("  ...the route uses it with NO route change",
              b["extraction"]["provider"] == "fake_vision")
        check("  ...its fields flow through", b["extraction"]["fields"]["account_number"] == "999")
        check("  ...storage model unchanged", b["file_count"] == 1)
    finally:
        os.environ.pop("DALTON_EXTRACTOR", None)
    check("back to local", engine.get_extractor().name == "local")

    section("PROVIDER ERRORS NEVER REACH THE REP AS A 500")
    class ExplodingExtractor(engine.DocumentExtractor):
        name = "exploding"
        def extract(self, file_paths, category="utility_bill"):
            raise RuntimeError("provider blew up")
    engine.register_extractor("exploding", ExplodingExtractor)
    os.environ["DALTON_EXTRACTOR"] = "exploding"
    try:
        eid = new_enrollment()
        r = upload_set(c, rep, eid, "utility_bill", [(png_bytes(), "b.jpg")])
        check("a raising provider does not 500", r.status_code == 200)
        check("  ...status is error", r.get_json()["extraction"]["status"] == "error")
        check("  ...the upload is still preserved", r.get_json()["file_count"] == 1)
        check("  ...manual entry is offered", r.get_json()["manual_entry_required"] is True)
    finally:
        os.environ.pop("DALTON_EXTRACTOR", None)

    section("PREPROCESSING — the stored original is never altered")
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory(prefix="dalton_prep_") as tmp:
        orig = os.path.join(tmp, "orig.png")
        Image.new("RGB", (4000, 3000), "white").save(orig)
        before = os.path.getsize(orig)
        derived = engine.preprocess_for_ocr(orig, tmp)
        check("preprocessing returns a DIFFERENT path", derived != orig)
        check("  ...the original file is untouched", os.path.getsize(orig) == before)
        check("  ...the original still opens", Image.open(orig).size == (4000, 3000))
        with Image.open(derived) as d:
            check("  ...the derivative is downscaled",
                  max(d.size) <= engine.MAX_OCR_DIMENSION)
    bad = engine.preprocess_for_ocr("/nonexistent/x.png", "/tmp")
    check("preprocessing failure falls back to the original", bad == "/nonexistent/x.png")

    section("DATA SAFETY — no cross-enrollment leakage")
    a_id = new_enrollment()
    b_id = new_enrollment()
    ra = upload_set(c, rep, a_id, "utility_bill", [(png_bytes(), "a.jpg")])
    a_set = ra.get_json()["document_set_id"]
    check("appending A's set from B is refused",
          upload_set(c, rep, b_id, "utility_bill", [(png_bytes(), "x.jpg")],
                     set_id=a_set).status_code == 404)
    check("reading A's set as B is refused",
          c.get(f"/api/enrollments/{b_id}/document-sets/{a_set}",
                headers=rep).status_code == 404)
    a_files = c.get(f"/api/enrollments/{a_id}/document-sets/{a_set}",
                    headers=rep).get_json()["files"]
    check("deleting A's file through B is refused",
          c.delete(f"/api/enrollments/{b_id}/document-sets/{a_set}/files/{a_files[0]['id']}",
                   headers=rep).status_code == 404)
    with app.app_context():
        still = query_one("SELECT COUNT(*) n FROM documents WHERE id=?", (a_files[0]["id"],))["n"]
    check("  ...and A's file still exists", still == 1)
    with app.app_context():
        rows = query("SELECT enrollment_id, document_set_id FROM documents "
                     "WHERE document_set_id IS NOT NULL")
        sets = {r["document_set_id"]: r["enrollment_id"] for r in rows}
        consistent = all(
            all(x["enrollment_id"] == sets[sid] for x in
                query("SELECT enrollment_id FROM documents WHERE document_set_id=?", (sid,)))
            for sid in sets)
    check("every set belongs to exactly one enrollment", consistent)

    section("EXTRACTION RESULTS ATTACH ONLY TO THEIR OWN SET")
    with app.app_context():
        execute("UPDATE document_sets SET extracted_data_json = ? WHERE id = ?",
                (json.dumps({"account_number": "AAA"}), a_set))
        rb = upload_set(c, rep, b_id, "utility_bill", [(png_bytes(), "b.jpg")])
        b_set = rb.get_json()["document_set_id"]
        a_row = query_one("SELECT extracted_data_json FROM document_sets WHERE id=?", (a_set,))
        b_row = query_one("SELECT extracted_data_json FROM document_sets WHERE id=?", (b_set,))
    check("B's extraction did not overwrite A's", "AAA" in (a_row["extracted_data_json"] or ""))
    check("A's data did not leak into B", "AAA" not in (b_row["extracted_data_json"] or ""))

    section("NO NEW SHARED MUTABLE STATE")
    src = open(os.path.join(ROOT, "services", "extraction_engine.py"), encoding="utf-8").read()
    check("no 'current/latest document' globals",
          "current_document" not in src and "latest_document" not in src)
    check("extractors are constructed per call, not shared",
          "return _EXTRACTORS.get(key, LocalExtractor)()" in src)
    check("the only module-level cache is the OCR capability probe",
          src.count("global ") == 1)

    section("EXISTING FLOW UNAFFECTED")
    r = c.get("/api/enrollments/extraction-status", headers=rep)
    check("capability endpoint works", r.status_code == 200)
    check("  ...reports availability", "ocr_available" in r.get_json())
    check("unauthenticated capability check refused",
          c.get("/api/enrollments/extraction-status").status_code == 401)
    eid = new_enrollment()
    with open(BILL_PDF, "rb") as fh:
        legacy = c.post(f"/api/enrollments/{eid}/documents", headers=rep,
                        data={"category": "utility_bill", "file": (fh, "bill.pdf")},
                        content_type="multipart/form-data")
    check("the original single-file route still works", legacy.status_code in (200, 201))
    check("  ...and still returns a document_id", bool(legacy.get_json().get("document_id")))

    # ═══════════════════════════════════════════════════════
    section("INLINE VIEWING — the original file, in the browser's own viewer")
    eid = new_enrollment()
    with open(BILL_PDF, "rb") as fh:
        pdf_bytes = fh.read()
    img_bytes = png_bytes()
    r = upload_set(c, rep, eid, "utility_bill",
                   [(img_bytes, "page1.png"), (pdf_bytes, "bill.pdf")])
    ids = [f["document_id"] for f in r.get_json()["files"]]
    img_id, pdf_id = ids[0], ids[1]

    v = c.get(f"/api/enrollments/{eid}/documents/{img_id}/view", headers=rep)
    check("IMAGE opens", v.status_code == 200)
    check("  ...served inline, not as a download",
          "inline" in (v.headers.get("Content-Disposition") or ""))
    check("  ...with an image content type", v.headers.get("Content-Type") == "image/png")
    check("  ...the ORIGINAL bytes are returned", v.data == img_bytes)

    v = c.get(f"/api/enrollments/{eid}/documents/{pdf_id}/view", headers=rep)
    check("PDF opens", v.status_code == 200)
    check("  ...served inline", "inline" in (v.headers.get("Content-Disposition") or ""))
    check("  ...with a PDF content type", v.headers.get("Content-Type") == "application/pdf")
    check("  ...the ORIGINAL bytes are returned", v.data == pdf_bytes)

    check("the CORRECT document is returned, not the first in the set",
          c.get(f"/api/enrollments/{eid}/documents/{pdf_id}/view",
                headers=rep).data != img_bytes)
    check("each id maps to its own file",
          c.get(f"/api/enrollments/{eid}/documents/{img_id}/view",
                headers=rep).data == img_bytes)

    section("INLINE VIEWING — no derivative, no OCR side effects")
    before = query_one("SELECT extraction_status, extracted_data_json FROM document_sets "
                       "WHERE id = ?", (r.get_json()["document_set_id"],))
    order_before = [x["page_order"] for x in query(
        "SELECT page_order FROM documents WHERE document_set_id = ? ORDER BY page_order",
        (r.get_json()["document_set_id"],))]
    for _ in range(3):
        c.get(f"/api/enrollments/{eid}/documents/{img_id}/view", headers=rep)
    after = query_one("SELECT extraction_status, extracted_data_json FROM document_sets "
                      "WHERE id = ?", (r.get_json()["document_set_id"],))
    order_after = [x["page_order"] for x in query(
        "SELECT page_order FROM documents WHERE document_set_id = ? ORDER BY page_order",
        (r.get_json()["document_set_id"],))]
    check("viewing does not change extraction state",
          before["extraction_status"] == after["extraction_status"]
          and before["extracted_data_json"] == after["extracted_data_json"])
    check("viewing does not change page order", order_before == order_after)
    check("viewing does not add or remove files",
          query_one("SELECT COUNT(*) n FROM documents WHERE document_set_id=?",
                    (r.get_json()["document_set_id"],))["n"] == 2)

    section("INLINE VIEWING — LMI documents too")
    lmi_eid = new_enrollment()
    lr = upload_set(c, rep, lmi_eid, "lmi_document",
                    [(img_bytes, "front.png"), (img_bytes[:], "back.png")])
    lmi_ids = [f["document_id"] for f in lr.get_json()["files"]]
    lv = c.get(f"/api/enrollments/{lmi_eid}/documents/{lmi_ids[0]}/view", headers=rep)
    check("LMI document opens inline", lv.status_code == 200)
    check("  ...inline disposition", "inline" in (lv.headers.get("Content-Disposition") or ""))
    check("  ...original bytes", lv.data == img_bytes)

    section("INLINE VIEWING — access control")
    check("unauthenticated access is rejected",
          c.get(f"/api/enrollments/{eid}/documents/{img_id}/view").status_code == 401)
    other = new_enrollment()
    check("cross-enrollment access is rejected",
          c.get(f"/api/enrollments/{other}/documents/{img_id}/view",
                headers=rep).status_code == 404)
    check("a bill cannot be fetched through an LMI enrollment id",
          c.get(f"/api/enrollments/{lmi_eid}/documents/{img_id}/view",
                headers=rep).status_code == 404)
    check("a nonexistent document is 404",
          c.get(f"/api/enrollments/{eid}/documents/999999/view",
                headers=rep).status_code == 404)

    # A second rep must not reach the first rep's documents.
    with app.app_context():
        from auth import hash_password
        uid = execute("INSERT INTO users (email, password_hash, role, full_name) "
                      "VALUES (?,?,?,?)",
                      ("viewrep@daltonsolar.com", hash_password("ViewPass1!"),
                       "sales_rep", "View Rep")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-VIEW"))
    rep2 = login(c, "viewrep@daltonsolar.com", "ViewPass1!")
    check("another rep cannot view this rep's document",
          c.get(f"/api/enrollments/{eid}/documents/{img_id}/view",
                headers=rep2).status_code == 403)

    section("INLINE VIEWING — hardening")
    v = c.get(f"/api/enrollments/{eid}/documents/{img_id}/view", headers=rep)
    check("nosniff is set", v.headers.get("X-Content-Type-Options") == "nosniff")
    check("a restrictive CSP is set", "default-src 'none'" in (v.headers.get("Content-Security-Policy") or ""))
    check("responses are not cached", "no-store" in (v.headers.get("Cache-Control") or ""))
    check("only safe types render inline",
          set(__import__("routes.document_routes", fromlist=["x"])._INLINE_SAFE_TYPES)
          == {".pdf", ".jpg", ".jpeg", ".png"})

    section("INLINE VIEWING — remove stays a separate control")
    set_id = r.get_json()["document_set_id"]
    n_before = query_one("SELECT COUNT(*) n FROM documents WHERE document_set_id=?",
                         (set_id,))["n"]
    c.get(f"/api/enrollments/{eid}/documents/{img_id}/view", headers=rep)
    check("viewing never deletes",
          query_one("SELECT COUNT(*) n FROM documents WHERE document_set_id=?",
                    (set_id,))["n"] == n_before)
    rm = c.delete(f"/api/enrollments/{eid}/documents/{img_id}/view", headers=rep)
    check("the view route rejects DELETE", rm.status_code == 405)
    rm2 = c.delete(f"/api/enrollments/{eid}/document-sets/{set_id}/files/{img_id}", headers=rep)
    check("the separate remove route still works", rm2.status_code == 200)
    check("  ...and the file is gone",
          query_one("SELECT COUNT(*) n FROM documents WHERE id=?", (img_id,))["n"] == 0)
    check("  ...while the other file survives",
          query_one("SELECT COUNT(*) n FROM documents WHERE id=?", (pdf_id,))["n"] == 1)

    section("DOWNLOAD ROUTE UNCHANGED")
    d = c.get(f"/api/enrollments/{eid}/documents/{pdf_id}/download", headers=rep)
    check("download still forces an attachment",
          "attachment" in (d.headers.get("Content-Disposition") or ""))
    check("  ...and still returns the original", d.data == pdf_bytes)

    print(f"\n{'='*72}\nDOCUMENT INGESTION + OCR ROBUSTNESS - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
