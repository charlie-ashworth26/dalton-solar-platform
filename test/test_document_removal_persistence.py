"""
Document removal and replacement must PERSIST.

THE BUG THIS SUITE PINS (KNOWN_ISSUES.md #1, "Removing a previously saved bill
/ proof photo can fail to persist")

    A rep removed a saved photo, it disappeared from the screen, and the next
    reopen brought it back. Removing and then replacing made it worse: the old
    photos returned alongside the new ones - four removed-and-replaced photos
    reopening as six.

    THE ROOT CAUSE was two hydration sources in the enrollment detail payload
    disagreeing, and the second one discarding what the first got right:

        e.document_sets   the current set per category, WITH its set id.
                          hydrateDocSets() restores it correctly.
        e.documents       every document row for the enrollment, flat, and
                          WITHOUT the set id, because the detail route never
                          selected the column.

    rehydrateDocumentsFromDetail() then replaced the whole uploader state from
    the second source with `id: null`. docSetRemove() needs that set id to
    address the DELETE endpoint and returned SILENTLY without one, so the x
    spliced the entry out of the list, repainted the screen, and sent nothing
    at all. The row survived; the next reopen listed it again. New uploads with
    a null set id started a RIVAL set instead of appending, so old and new rows
    coexisted.

    Three earlier attempts missed it because each inspected the removal path
    (correct) and hydrateDocSets() (also correct). The damage happened between
    them, which is why this suite asserts on the PAYLOAD CONTRACT the browser
    depends on, not only on the endpoints.

WHAT IS COVERED
    Sections 1-4   the four required lifecycle cases, for the BILL flow
    Section 5      the same four for the LMI PROOF flow
    Section 6      the detail payload carries the set id (the root cause)
    Section 7      legacy set-less documents are removable at all
    Section 8      FK references are detached, not left to 500
    Section 9      removal is reference-only: bytes on disk are not destroyed

Run: python test/test_document_removal_persistence.py
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
    """A tiny valid JPEG, distinct per tag so uploads are distinguishable."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 20 + len(tag)), "white").save(buf, format="JPEG")
    return buf.getvalue()


def new_draft(c, h):
    r = c.post("/api/perch/drafts", headers=h, json={})
    assert r.status_code in (200, 201), r.data
    return r.get_json()["enrollment_id"]


def upload(c, h, eid, category, name, set_id=None):
    """One page, joining `set_id` when given - exactly what the browser does."""
    data = {"category": category, "files": (io.BytesIO(jpg(name.encode())), name)}
    if set_id:
        data["document_set_id"] = str(set_id)
    r = c.post(f"/api/enrollments/{eid}/document-sets", headers=h, data=data,
               content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    body = r.get_json()
    return body["document_set_id"], body["files"][0]["document_id"]


def detail(c, h, eid):
    r = c.get(f"/api/enrollments/{eid}", headers=h)
    assert r.status_code == 200, r.data
    return r.get_json()


def attached(c, h, eid, category):
    """WHAT THE REP WILL SEE on reopen, read the way the browser reads it.

    hydrateDocSets() consumes detail.document_sets, so that - not a direct
    database query - is the thing a regression has to be caught in. A query
    would have passed throughout the entire life of this bug.
    """
    sets = detail(c, h, eid).get("document_sets") or {}
    entry = sets.get(category) or {}
    return [f["original_filename"] for f in (entry.get("files") or [])]


def remove_from_set(c, h, eid, set_id, doc_id):
    return c.delete(f"/api/enrollments/{eid}/document-sets/{set_id}/files/{doc_id}",
                    headers=h)


def remove_document(c, h, eid, doc_id):
    return c.delete(f"/api/enrollments/{eid}/documents/{doc_id}", headers=h)


def main():
    init_db(reset=True)
    seed.seed()

    with app.test_client() as c:
        h = login(c)

        # ── 1 ──────────────────────────────────────────────────────────────
        section("1. UPLOAD -> SAVE -> REOPEN: the document is still there")
        eid = new_draft(c, h)
        set_id, a_id = upload(c, h, eid, "utility_bill", "A.jpg")
        _, b_id = upload(c, h, eid, "utility_bill", "B.jpg", set_id)
        check("two pages uploaded into ONE set",
              query_one("SELECT COUNT(DISTINCT document_set_id) AS n FROM documents "
                        "WHERE enrollment_id = ?", (eid,))["n"] == 1)
        check("reopen shows both pages", attached(c, h, eid, "utility_bill")
              == ["A.jpg", "B.jpg"])

        # ── 2 ──────────────────────────────────────────────────────────────
        section("2. REMOVE -> SAVE -> REOPEN: it does NOT come back")
        r = remove_from_set(c, h, eid, set_id, b_id)
        check(f"the removal is accepted ({r.status_code})", r.status_code == 200)
        check("reopen shows only the remaining page",
              attached(c, h, eid, "utility_bill") == ["A.jpg"])
        check("  ...and no row survives for the removed page",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (b_id,)) is None)

        # ── 3 ──────────────────────────────────────────────────────────────
        section("3. REPLACE: only the replacement remains")
        # The rep removes A and picks C. The browser appends to the set it is
        # holding, which is the behaviour the restored set id makes possible.
        remove_from_set(c, h, eid, set_id, a_id)
        _, c_id = upload(c, h, eid, "utility_bill", "C.jpg", set_id)
        names = attached(c, h, eid, "utility_bill")
        check(f"reopen shows ONLY the replacement ({names})", names == ["C.jpg"])
        check("  ...no duplicate or stale reference remains",
              query_one("SELECT COUNT(*) AS n FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'utility_bill'", (eid,))["n"] == 1)
        check("  ...and no rival set was created",
              query_one("SELECT COUNT(*) AS n FROM document_sets WHERE enrollment_id = ? "
                        "AND category = 'utility_bill'", (eid,))["n"] == 1)

        # ── 4 ──────────────────────────────────────────────────────────────
        section("4. NO CHANGE: an unrelated save preserves the document")
        r = c.patch(f"/api/enrollments/{eid}", headers=h,
                    json={"customer": {"first_name": "Dana", "last_name": "Reyes",
                                       "email": "dana.reyes@example.com"}})
        check(f"the unrelated PATCH succeeds ({r.status_code})",
              r.status_code in (200, 204))
        check("the document is untouched",
              attached(c, h, eid, "utility_bill") == ["C.jpg"])
        check("  ...and still the same row",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (c_id,)) is not None)

        # ── 5 ──────────────────────────────────────────────────────────────
        section("5. THE SAME FOUR CASES FOR THE LMI PROOF FLOW")
        # A separate category with its own set, its own UI and its own pointer,
        # so it gets its own coverage rather than being assumed to follow.
        leid = new_draft(c, h)
        lset, l1 = upload(c, h, leid, "lmi_document", "proof-1.jpg")
        _, l2 = upload(c, h, leid, "lmi_document", "proof-2.jpg", lset)
        check("5a. upload -> reopen: both proofs present",
              attached(c, h, leid, "lmi_document") == ["proof-1.jpg", "proof-2.jpg"])
        remove_from_set(c, h, leid, lset, l2)
        check("5b. remove -> reopen: the removed proof is gone",
              attached(c, h, leid, "lmi_document") == ["proof-1.jpg"])
        remove_from_set(c, h, leid, lset, l1)
        _, l3 = upload(c, h, leid, "lmi_document", "proof-3.jpg", lset)
        check("5c. replace -> reopen: only the replacement",
              attached(c, h, leid, "lmi_document") == ["proof-3.jpg"])
        c.patch(f"/api/enrollments/{leid}", headers=h,
                json={"customer": {"first_name": "Lee", "last_name": "Ng",
                                   "email": "lee.ng@example.com"}})
        check("5d. unrelated save -> reopen: the proof is preserved",
              attached(c, h, leid, "lmi_document") == ["proof-3.jpg"])
        check("5e. the bill category is unaffected by LMI removals",
              attached(c, h, leid, "utility_bill") == [])

        # ── 6 ──────────────────────────────────────────────────────────────
        section("6. THE PAYLOAD CARRIES THE SET ID  (the actual root cause)")
        # The browser has two sources for attached documents and the flat one
        # used to omit the set id, which is what made removal a no-op. Both
        # must now be addressable.
        d = detail(c, h, eid)
        flat = [x for x in (d.get("documents") or [])
                if x.get("doc_category") == "utility_bill"]
        check("detail.documents is present", bool(flat))
        check("  ...and every row carries document_set_id",
              all("document_set_id" in x for x in flat))
        check("  ...with the REAL set id, not null",
              all(x["document_set_id"] == set_id for x in flat))
        check("  ...and page_order, so the flat list can be ordered",
              all("page_order" in x for x in flat))
        ds = (d.get("document_sets") or {}).get("utility_bill") or {}
        check("detail.document_sets still names its set id",
              ds.get("document_set_id") == set_id)
        check("  ...and the two sources agree on what is attached",
              [f["original_filename"] for f in ds.get("files", [])]
              == [x["original_filename"] for x in flat])

        # ── 7 ──────────────────────────────────────────────────────────────
        section("7. A LEGACY SET-LESS DOCUMENT IS REMOVABLE AT ALL")
        # Uploads made through the older single-file route have
        # document_set_id NULL. No set-scoped endpoint can address those, so
        # until the per-document route existed their removal could not be
        # persisted even in principle.
        geid = new_draft(c, h)
        r = c.post(f"/api/enrollments/{geid}/documents", headers=h,
                   data={"category": "utility_bill",
                         "file": (io.BytesIO(jpg(b"legacy")), "legacy.jpg")},
                   content_type="multipart/form-data")
        check(f"the legacy upload succeeds ({r.status_code})", r.status_code == 201)
        gdoc = r.get_json()["document_id"]
        check("  ...and really has no set",
              query_one("SELECT document_set_id AS s FROM documents WHERE id = ?",
                        (gdoc,))["s"] is None)
        r = remove_document(c, h, geid, gdoc)
        check(f"the per-document removal is accepted ({r.status_code})",
              r.status_code == 200)
        check("  ...and the row is gone",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (gdoc,)) is None)

        section("7b. AND IT IS STILL SCOPED TO ONE ENROLLMENT")
        other = new_draft(c, h)
        oset, odoc = upload(c, h, other, "utility_bill", "theirs.jpg")
        r = remove_document(c, h, eid, odoc)        # wrong enrollment on purpose
        check(f"removing another enrollment's document is refused ({r.status_code})",
              r.status_code == 404)
        check("  ...and that document is untouched",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (odoc,)) is not None)
        r = remove_from_set(c, h, eid, oset, odoc)  # via the set route too
        check(f"the set-scoped route refuses it as well ({r.status_code})",
              r.status_code == 404)

        section("7c. A GENERATED DOCUMENT IS NOT REMOVABLE THROUGH THE UPLOADER")
        # Agreements, signature certificates and submission packages are
        # produced by UBS and referenced by other records.
        gen = execute(
            "INSERT INTO documents (enrollment_id, doc_category, original_filename, "
            "stored_path, mime_type, file_size) VALUES (?, 'generated_agreement', "
            "'agreement.pdf', 'uploads/x/agreement.pdf', 'application/pdf', 10)",
            (eid,)).lastrowid
        r = remove_document(c, h, eid, gen)
        check(f"it is refused ({r.status_code})", r.status_code == 400)
        check("  ...and still exists",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (gen,)) is not None)

        # ── 8 ──────────────────────────────────────────────────────────────
        section("8. FOREIGN-KEY REFERENCES ARE DETACHED, NOT LEFT TO 500")
        # Four tables reference documents(id) with no ON DELETE clause, so the
        # delete is BLOCKED by SQLite unless the reference is dealt with. A
        # legacy upload writes a validation_results row, and removing such a
        # document produced:
        #     sqlite3.IntegrityError: FOREIGN KEY constraint failed
        # surfacing to the rep as a 500 and a photo that would not go away.
        feid = new_draft(c, h)
        r = c.post(f"/api/enrollments/{feid}/documents", headers=h,
                   data={"category": "lmi_document",
                         "file": (io.BytesIO(jpg(b"proof")), "proof.jpg")},
                   content_type="multipart/form-data")
        fdoc = r.get_json()["document_id"]
        check("the legacy upload wrote a validation_results row",
              query_one("SELECT 1 AS y FROM validation_results WHERE document_id = ?",
                        (fdoc,)) is not None)
        # Rep-entered LMI work pointing at that same document.
        execute("INSERT INTO lmi_qualifications (enrollment_id, path, "
                "qualification_type, document_id, household_size) "
                "VALUES (?, 'document', 'SNAP award letter', ?, 3)", (feid, fdoc))
        r = remove_document(c, h, feid, fdoc)
        check(f"the removal succeeds instead of erroring ({r.status_code})",
              r.status_code == 200)
        check("  ...the document is gone",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (fdoc,)) is None)
        check("  ...the derived validation result went with it",
              query_one("SELECT 1 AS y FROM validation_results WHERE document_id = ?",
                        (fdoc,)) is None)
        lq = query_one("SELECT * FROM lmi_qualifications WHERE enrollment_id = ?", (feid,))
        check("  ...but the REP-ENTERED qualification survives", lq is not None)
        check("  ...detached from the removed document", lq["document_id"] is None)
        check("  ...with its own fields intact",
              lq["qualification_type"] == "SNAP award letter" and lq["household_size"] == 3)

        # ── 9 ──────────────────────────────────────────────────────────────
        section("9. REMOVAL DETACHES THE REFERENCE; IT DOES NOT DESTROY BYTES")
        # UBS has never physically deleted an uploaded document, and a rep's x
        # must not become the first thing that does. Pinned so that if physical
        # deletion is ever introduced it is a deliberate decision with this
        # test changed on purpose, not a silent new behaviour.
        peid = new_draft(c, h)
        pset, pdoc = upload(c, h, peid, "utility_bill", "keepbytes.jpg")
        path = resolve_stored_path(
            query_one("SELECT stored_path AS p FROM documents WHERE id = ?", (pdoc,))["p"])
        check("the uploaded file exists on disk", os.path.exists(path))
        remove_from_set(c, h, peid, pset, pdoc)
        check("the reference is gone",
              query_one("SELECT 1 AS y FROM documents WHERE id = ?", (pdoc,)) is None)
        check("  ...and the bytes are deliberately still there (orphan, by design)",
              os.path.exists(path))

        section("9b. THE REP-FACING VIEW NO LONGER MENTIONS IT")
        check("the removed page is not in document_sets",
              attached(c, h, peid, "utility_bill") == [])
        flat2 = [x for x in (detail(c, h, peid).get("documents") or [])
                 if x.get("doc_category") == "utility_bill"]
        check("  ...nor in the flat documents list", flat2 == [])

    print("\n" + "=" * 72)
    print("DOCUMENT REMOVAL PERSISTENCE - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
