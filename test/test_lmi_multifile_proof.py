"""
Multi-file LMI proof uploads must persist EVERY selected file.

THE BUG THIS SUITE PINS

    The proof-document picker allows multiple files. handleLmiUpload() listed
    all of them on screen via docSetAddFiles(), then uploaded only files[0]
    through uploadDocumentToDalton() - the single-file route. A rep who picked
    three pages of an award letter saw three rows while one file left the
    browser; the other two never existed on the server and the list showed
    them as "Uploading" indefinitely.

    The comment above that line already said "front/back or multi-page proofs
    form ONE document set". The intent was there; the implementation never
    landed. The bill flow had the identical defect and was fixed by routing
    through uploadDocumentSet(); the proof flow was simply never moved across,
    which is also why every historical proof document has document_set_id NULL.

TWO THINGS A BLIND COPY OF THE BILL FLOW WOULD HAVE BROKEN, both covered here:

    SECTION 7  LMI VALIDATION. The single-file route runs
               lmi_validation.validate_lmi_document() and writes a
               validation_results row; the document-set route never did,
               because only bills used it. Moving proofs across without it
               would have silently removed LMI validation from the enrollment
               detail payload, the developer/QA review and the submission
               package - with no failed upload anywhere to notice.

    SECTION 8  PERCH SUBMISSION. /lmi/proof_docs takes a LIST of documents,
               each with one file part and its own metadata. The route built a
               list of ONE, so storing three pages and submitting one would
               have turned an upload-truncation bug into a submission one.

Run: python test/test_lmi_multifile_proof.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from helpers import resolve_stored_path


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email="charlie@daltonsolar.com", pw="RepPass1!"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def jpg(tag=b"x"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 20 + len(tag)), "white").save(buf, format="JPEG")
    return buf.getvalue()


def new_draft(c, h):
    r = c.post("/api/perch/drafts", headers=h, json={})
    assert r.status_code in (200, 201), r.data
    return r.get_json()["enrollment_id"]


def upload_set(c, h, eid, category, names, set_id=None):
    """Upload several files AT ONCE, the way a multi-select pick arrives."""
    data = {"category": category,
            "files": [(io.BytesIO(jpg(n.encode())), n) for n in names]}
    if set_id:
        data["document_set_id"] = str(set_id)
    r = c.post(f"/api/enrollments/{eid}/document-sets", headers=h, data=data,
               content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    return r.get_json()


def attached(c, h, eid, category):
    """What the rep sees on reopen, read the way the browser reads it."""
    r = c.get(f"/api/enrollments/{eid}", headers=h)
    assert r.status_code == 200, r.data
    entry = (r.get_json().get("document_sets") or {}).get(category) or {}
    return [f["original_filename"] for f in (entry.get("files") or [])]


def main():
    init_db(reset=True)
    seed.seed()

    with app.test_client() as c:
        h = login(c)

        # ── 1 ──────────────────────────────────────────────────────────────
        section("1. ONE PROOF FILE -> SAVE -> REOPEN: one present")
        eid = new_draft(c, h)
        body = upload_set(c, h, eid, "lmi_document", ["single-proof.jpg"])
        check("the upload returns one stored file", len(body["files"]) == 1)
        check("reopen shows it",
              attached(c, h, eid, "lmi_document") == ["single-proof.jpg"])

        # ── 2 ──────────────────────────────────────────────────────────────
        section("2. THREE PROOF FILES AT ONCE: all three persist")
        meid = new_draft(c, h)
        body = upload_set(c, h, meid, "lmi_document",
                          ["award-1.jpg", "award-2.jpg", "award-3.jpg"])
        check(f"the upload returns three stored files ({len(body['files'])})",
              len(body["files"]) == 3)
        check("  ...with three distinct document ids",
              len({f["document_id"] for f in body["files"]}) == 3)
        check("  ...in the order they were picked",
              [f["page_order"] for f in body["files"]] == [0, 1, 2])
        rows = query("SELECT original_filename FROM documents WHERE enrollment_id = ? "
                     "AND doc_category = 'lmi_document' "
                     "ORDER BY COALESCE(page_order,0), id", (meid,))
        check("  ...and all three are really in the database",
              [r["original_filename"] for r in rows]
              == ["award-1.jpg", "award-2.jpg", "award-3.jpg"])
        check("  ...sharing ONE document set",
              query_one("SELECT COUNT(DISTINCT document_set_id) AS n FROM documents "
                        "WHERE enrollment_id = ? AND doc_category = 'lmi_document'",
                        (meid,))["n"] == 1)
        for r in query("SELECT stored_path FROM documents WHERE enrollment_id = ?", (meid,)):
            check(f"  ...and the bytes exist on disk ({os.path.basename(r['stored_path'])})",
                  os.path.exists(resolve_stored_path(r["stored_path"])))

        # ── 3 ──────────────────────────────────────────────────────────────
        section("3. REOPEN: all three still present")
        check("reopen lists all three, in page order",
              attached(c, h, meid, "lmi_document")
              == ["award-1.jpg", "award-2.jpg", "award-3.jpg"])

        # ── 4 ──────────────────────────────────────────────────────────────
        section("4. REMOVE THE MIDDLE FILE: only the other two remain")
        ids = {r["original_filename"]: r["id"] for r in query(
            "SELECT id, original_filename FROM documents WHERE enrollment_id = ? "
            "AND doc_category = 'lmi_document'", (meid,))}
        set_id = query_one("SELECT document_set_id AS s FROM documents WHERE id = ?",
                           (ids["award-2.jpg"],))["s"]
        r = c.delete(f"/api/enrollments/{meid}/document-sets/{set_id}"
                     f"/files/{ids['award-2.jpg']}", headers=h)
        check(f"the removal is accepted ({r.status_code})", r.status_code == 200)
        check("reopen shows the other two only",
              attached(c, h, meid, "lmi_document") == ["award-1.jpg", "award-3.jpg"])
        check("  ...and page_order was re-densed, not left with a hole",
              [x["page_order"] for x in query(
                  "SELECT page_order FROM documents WHERE document_set_id = ? "
                  "ORDER BY page_order", (set_id,))] == [0, 1])

        # ── 5 ──────────────────────────────────────────────────────────────
        section("5. ADD ANOTHER PROOF AFTERWARDS: no duplicate or stale rows")
        upload_set(c, h, meid, "lmi_document", ["award-4.jpg"], set_id=set_id)
        check("reopen shows exactly the three that should be there",
              attached(c, h, meid, "lmi_document")
              == ["award-1.jpg", "award-3.jpg", "award-4.jpg"])
        check("  ...with no rival set created",
              query_one("SELECT COUNT(*) AS n FROM document_sets WHERE enrollment_id = ? "
                        "AND category = 'lmi_document'", (meid,))["n"] == 1)
        check("  ...and no row for the removed file",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?",
                        (ids["award-2.jpg"],)) is None)

        # ── 6 ──────────────────────────────────────────────────────────────
        section("6. AN UNRELATED SAVE PRESERVES EVERY PROOF FILE")
        r = c.patch(f"/api/enrollments/{meid}", headers=h,
                    json={"customer": {"first_name": "Lee", "last_name": "Ng",
                                       "email": "lee.ng@example.com"}})
        check(f"the unrelated PATCH succeeds ({r.status_code})",
              r.status_code in (200, 204))
        check("all three proof files are still attached",
              attached(c, h, meid, "lmi_document")
              == ["award-1.jpg", "award-3.jpg", "award-4.jpg"])

        # ── 7 ──────────────────────────────────────────────────────────────
        section("7. LMI VALIDATION SURVIVES THE MOVE TO DOCUMENT SETS")
        # The single-file route always wrote a validation_results row for a
        # proof. The set route did not, because only bills used it.
        vals = query("SELECT * FROM validation_results WHERE enrollment_id = ? "
                     "AND validation_type = 'lmi_document'", (meid,))
        check(f"a set upload still produces an LMI validation ({len(vals)})",
              len(vals) >= 1)
        check("  ...one per upload, not one per file",
              len(vals) == 2)        # the 3-file pick, then the later 1-file add
        first_page = query_one(
            "SELECT id FROM documents WHERE document_set_id = ? "
            "ORDER BY COALESCE(page_order,0), id LIMIT 1", (set_id,))
        check("  ...attached to a document of that set",
              any(v["document_id"] for v in vals))
        check("  ...with a real classification, not a placeholder",
              all(v["classification"] in
                  ("likely_valid", "needs_manual_review", "likely_invalid")
                  for v in vals))
        bod = upload_set(c, h, new_draft(c, h), "lmi_document", ["p1.jpg", "p2.jpg"])
        check("  ...and the upload response carries it for the whole set",
              isinstance(bod.get("lmi_validation"), dict)
              and "classification" in bod["lmi_validation"])

        section("7b. A BILL UPLOAD IS UNAFFECTED BY THAT")
        beid = new_draft(c, h)
        bbody = upload_set(c, h, beid, "utility_bill", ["bill-1.jpg", "bill-2.jpg"])
        check("a bill set carries no LMI validation",
              bbody.get("lmi_validation") is None)
        check("  ...and writes no lmi_document validation row",
              query_one("SELECT 1 AS y FROM validation_results WHERE enrollment_id = ? "
                        "AND validation_type = 'lmi_document'", (beid,)) is None)

        # ── 8 ──────────────────────────────────────────────────────────────
        section("8. PERCH PROOF SUBMISSION RECEIVES EVERY FILE, WITH THE CATEGORY")
        # Captured at the adapter boundary, so this asserts what would really
        # be sent rather than what the route intended.
        from services.perch import adapter as perch_adapter
        import routes.perch_routes as pr
        captured = {}

        def fake_submit(enrollment_id, account_number, documents, user_id=None):
            captured["enrollment_id"] = enrollment_id
            captured["account_number"] = account_number
            captured["documents"] = documents
            return {"next_step_url": "/contracts", "response_shape": "json"}

        seid = new_draft(c, h)
        sbody = upload_set(c, h, seid, "lmi_document",
                           ["snap-front.jpg", "snap-back.jpg", "snap-letter.jpg"])
        proof_ids = [f["document_id"] for f in sbody["files"]]
        # The enrollment needs a utility account and a customer for the route.
        c.patch(f"/api/enrollments/{seid}", headers=h,
                json={"customer": {"first_name": "Ada", "last_name": "Prentice",
                                   "email": "ada.prentice@example.com"},
                      "service_address": {"street": "1 Main St", "city": "Albany",
                                          "state": "NY", "zip": "12207"},
                      "utility_account": {"account_number": "1234567890",
                                          "utility_name": "national-grid-ny"}})
        # utility_accounts hangs off the CUSTOMER, and enrollments points at it
        # by utility_account_id - which the PATCH above already wires up.
        acct_id = query_one("SELECT utility_account_id AS a FROM enrollments WHERE id = ?",
                            (seid,))["a"]
        check("the enrollment has a utility account to submit against",
              acct_id is not None)
        acct = query_one("SELECT * FROM utility_accounts WHERE id = ?", (acct_id,))
        check("  ...with an account number", bool(acct and acct["account_number"]))
        # A proof source type Perch publishes.
        ptype = query_one("SELECT source_type FROM perch_proof_doc_types "
                          "WHERE category='proof_doc' AND is_active=1 LIMIT 1")
        check("a published proof source type is available", ptype is not None)

        real = perch_adapter.submit_proof_docs
        pr_real = getattr(pr.adapter, "submit_proof_docs", None)
        perch_adapter.submit_proof_docs = fake_submit
        pr.adapter.submit_proof_docs = fake_submit
        try:
            r = c.post(f"/api/perch/enrollments/{seid}/lmi/proof_docs", headers=h,
                       json={"document_id": proof_ids[0],
                             "source_type": ptype["source_type"],
                             "name_on_document": "Ada Prentice",
                             "relationship": "self",
                             "document_type": "letter"})
            check(f"the submission route succeeds ({r.status_code})", r.status_code == 200)
        finally:
            perch_adapter.submit_proof_docs = real
            if pr_real is not None:
                pr.adapter.submit_proof_docs = pr_real

        docs = captured.get("documents") or []
        check(f"Perch is sent ALL THREE proof files ({len(docs)})", len(docs) == 3)
        sent = [os.path.basename(d["file_path"]) for d in docs]
        check("  ...the files actually uploaded, in page order",
              all("snap-front" in sent[0] and "snap-back" in sent[1]
                  and "snap-letter" in sent[2] for _ in [0]))
        check("  ...each carrying the rep's source type",
              all(d["source_type"] == ptype["source_type"] for d in docs))
        check("  ...each carrying the same name, relationship and format",
              all(d["name_on_document"] == "Ada Prentice"
                  and d["relationship"] == "self"
                  and d["document_type"] == "letter" for d in docs))
        check("  ...and every path resolves to a real file",
              all(os.path.exists(d["file_path"]) for d in docs))
        check("only lmi_document files were sent - no bill crept in",
              all(query_one("SELECT doc_category AS cat FROM documents "
                            "WHERE stored_path LIKE ?",
                            ('%' + os.path.basename(d["file_path"]),))["cat"]
                  == "lmi_document" for d in docs))
        wf = query_one("SELECT last_response_json AS j FROM perch_workflow_state "
                       "WHERE enrollment_id = ?", (seid,))
        check("  ...and the workflow records which files went",
              wf is not None and '"proof_file_count": 3' in (wf["j"] or "").replace("\n", ""))

        section("8b. THE TRANSPORT REALLY BUILDS ONE FILE PART PER DOCUMENT")
        from services.perch.client import build_proof_docs_multipart
        files, proof_doc = build_proof_docs_multipart("1234567890", docs)
        try:
            check("there is an indexed file part for each document",
                  all(f"documents[{i}][file]" in files for i in range(3)))
            check("  ...and the JSON part lists three documents",
                  len(proof_doc["documents"]) == 3)
            check("  ...correlated by index, metadata only",
                  all(set(d) == {"source_type", "name_on_document",
                                 "relationship", "document_type"}
                      for d in proof_doc["documents"]))
        finally:
            for v in files.values():
                if isinstance(v, tuple) and hasattr(v[1], "close"):
                    try:
                        v[1].close()
                    except Exception:
                        pass

        section("8c. A LEGACY SET-LESS PROOF STILL SUBMITS EXACTLY AS BEFORE")
        geid = new_draft(c, h)
        r = c.post(f"/api/enrollments/{geid}/documents", headers=h,
                   data={"category": "lmi_document",
                         "file": (io.BytesIO(jpg(b"legacy")), "legacy-proof.jpg")},
                   content_type="multipart/form-data")
        gdoc = r.get_json()["document_id"]
        check("the legacy proof has no set",
              query_one("SELECT document_set_id AS s FROM documents WHERE id = ?",
                        (gdoc,))["s"] is None)
        pages = pr._set_documents_for(geid, query_one(
            "SELECT * FROM documents WHERE id = ?", (gdoc,)), "lmi_document")
        check("  ...and expands to exactly itself - a one-document request",
              len(pages) == 1 and pages[0]["id"] == gdoc)

        section("8d. AND IT IS STILL REMOVABLE")
        r = c.delete(f"/api/enrollments/{geid}/documents/{gdoc}", headers=h)
        check(f"the per-document removal is accepted ({r.status_code})",
              r.status_code == 200)
        check("  ...and the row is gone",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (gdoc,)) is None)

        # ── 9 ──────────────────────────────────────────────────────────────
        section("9. BILL DOCUMENT-SET BEHAVIOUR IS UNCHANGED")
        check("the bill set still lists both pages in order",
              attached(c, h, beid, "utility_bill") == ["bill-1.jpg", "bill-2.jpg"])
        bset = query_one("SELECT document_set_id AS s FROM documents "
                         "WHERE enrollment_id = ? LIMIT 1", (beid,))["s"]
        upload_set(c, h, beid, "utility_bill", ["bill-3.jpg"], set_id=bset)
        check("  ...a later page appends to the same set",
              attached(c, h, beid, "utility_bill")
              == ["bill-1.jpg", "bill-2.jpg", "bill-3.jpg"])
        check("  ...with one set for the category",
              query_one("SELECT COUNT(*) AS n FROM document_sets WHERE enrollment_id = ? "
                        "AND category = 'utility_bill'", (beid,))["n"] == 1)
        bill_doc = query_one(
            "SELECT * FROM documents WHERE enrollment_id = ? "
            "AND doc_category = 'utility_bill' ORDER BY page_order LIMIT 1", (beid,))
        bill_pages = pr._bill_documents_for(beid, bill_doc)
        check(f"  ...and bill submission still expands to every page ({len(bill_pages)})",
              len(bill_pages) == 3)
        check("  ...bill and proof sets share no document",
              not (set(r["id"] for r in query(
                        "SELECT id FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'utility_bill'", (beid,)))
                   & set(r["id"] for r in query(
                        "SELECT id FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'lmi_document'", (meid,)))))

    print("\n" + "=" * 72)
    print("LMI MULTI-FILE PROOF UPLOAD - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
