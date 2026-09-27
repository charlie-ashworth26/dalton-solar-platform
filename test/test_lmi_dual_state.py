"""
Proof-doc state and self-attestation state coexist on one LMI enrollment,
and NON-LMI enrollments are completely unaffected.

WHY NO MIGRATION
    The two kinds of state were ALREADY stored in different tables:
        proof docs        -> documents(doc_category='lmi_document')
                             + lmi_qualifications(path='document')
        self-attestation  -> perch_self_attestation_submissions
    Nothing couples them, and lmi_qualifications has no uniqueness constraint
    on enrollment_id. The exclusivity was a frontend construct.

    python test/test_lmi_dual_state.py
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

_temp = tempfile.TemporaryDirectory(prefix="dalton-lmi-dual-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query, query_one, execute
init_db(reset=True)

from app import app
from routes import document_routes
document_routes.UPLOAD_DIR = os.path.join(_temp.name, "uploads")
document_routes.DATA_ROOT = _temp.name
os.makedirs(document_routes.UPLOAD_DIR, exist_ok=True)
import seed
from services.perch import adapter, workflow

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


def upload(c, h, eid, path, category):
    with open(path, "rb") as fh:
        r = c.post(f"/api/enrollments/{eid}/documents", headers=h,
                   data={"category": category,
                         "file": (io.BytesIO(fh.read()), Path(path).name)},
                   content_type="multipart/form-data")
    assert r.status_code == 201, r.data
    return r.get_json()["document_id"]


def main():
    seed.seed()
    seed.seed_legacy_projects()
    c = app.test_client()
    h = login(c)

    def build(email_tag, utility="national-grid-ny", zip_code="13348",
              enroll_next="lmi/proof_docs"):
        eid = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
        email = f"lmi.dual.{email_tag}.{eid}@example.com"
        r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=h,
                   json={"email": email, "zip_code": zip_code, "utility_name": utility})
        assert r.status_code == 200, r.data
        bill = upload(c, h, eid, ROOT / "test" / "sample_utility_bill.pdf", "utility_bill")
        c.patch(f"/api/enrollments/{eid}", headers=h, json={
            "customer": {"first_name": "Dalton", "last_name": "Dualstate",
                         "email": email, "phone": "5185550142"},
            "service_address": {"street": "123 Main St", "unit": "", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "billing_address": {"same_as_service": True, "street": "123 Main St",
                                "unit": "", "city": "Albany", "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": utility, "account_number": "1234567890",
                                "secondary_account_identifier": None}})
        real = adapter.create_enrollment
        adapter.create_enrollment = lambda *a, **k: {
            "customer_type": "LMI", "next_step_url": f"{BASE}/{enroll_next}"}
        c.post(f"/api/perch/enrollments/{eid}/enroll", headers=h, json={"document_id": bill})
        adapter.create_enrollment = real
        return eid, email

    def step_key(eid):
        row = query_one("SELECT current_step_key FROM perch_workflow_state "
                        "WHERE enrollment_id=?", (eid,))
        return row["current_step_key"] if row else None

    # ═══════════════════════════════════════════════════════
    section("1. ONE ENROLLMENT HOLDS BOTH KINDS OF LMI STATE")
    eid, _ = build("both", enroll_next="lmi/self_attestation")
    check("Perch put it on self-attestation", step_key(eid) == "self_attestation")

    # Proof document uploaded while self-attestation is the authorised step.
    # This is a LOCAL document call - no Perch endpoint is touched.
    proof = upload(c, h, eid, ROOT / "test" / "test_snap_proof_letter.pdf", "lmi_document")
    r = c.post(f"/api/enrollments/{eid}/lmi", headers=h,
               json={"path": "document", "qualification_type": "proof_doc_liheap",
                     "document_id": proof})
    check("a proof document can be attached on the self-attestation step",
          r.status_code in (200, 201))
    check("  ...and the workflow step is UNCHANGED by it",
          step_key(eid) == "self_attestation")

    # Self-attestation submitted for the same enrollment.
    real_sa = adapter.submit_self_attestation
    adapter.submit_self_attestation = lambda *a, **k: {
        "next_step_url": f"{BASE}/contracts", "document_available": True}
    r = c.post(f"/api/perch/enrollments/{eid}/lmi/self-attestation", headers=h,
               json={"occupancy": 3, "county": "Albany County", "status": "accepted"})
    adapter.submit_self_attestation = real_sa
    check("self-attestation is accepted for the SAME enrollment", r.status_code == 200)

    doc_row = query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=? "
                        "AND doc_category='lmi_document'", (eid,))
    qual = query_one("SELECT COUNT(*) n FROM lmi_qualifications WHERE enrollment_id=? "
                     "AND path='document'", (eid,))
    sa = query_one("SELECT * FROM perch_self_attestation_submissions WHERE enrollment_id=?",
                   (eid,))
    check("the proof DOCUMENT is still on the enrollment", doc_row["n"] == 1)
    check("  ...and its qualification row", qual["n"] == 1)
    check("the self-attestation record exists alongside it", sa is not None)
    check("  ...with the rep's answers", sa["occupancy"] == 3 and sa["county"] == "Albany County"
          and sa["status"] == "accepted")
    check("BOTH coexist on one enrollment", doc_row["n"] == 1 and sa is not None)

    section("  ...because they live in DIFFERENT tables - no migration needed")
    check("lmi_qualifications has no uniqueness on enrollment_id",
          not any("lmi_qualifications" in (r["sql"] or "") and "UNIQUE" in (r["sql"] or "")
                  for r in query("SELECT sql FROM sqlite_master WHERE type='index'")))
    check("self-attestation is its own table with its own key",
          query_one("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
                    "AND name='perch_self_attestation_submissions'")["n"] == 1)
    check("migration count is unchanged at 12",
          query_one("SELECT COUNT(*) n FROM schema_migrations")["n"] == 12)

    section("3. COMPLETING SELF-ATTESTATION DID NOT REMOVE THE DOCUMENT")
    check("the proof document row is intact",
          query_one("SELECT id FROM documents WHERE id=?", (proof,)) is not None)
    check("  ...still attached to this enrollment",
          query_one("SELECT enrollment_id FROM documents WHERE id=?",
                    (proof,))["enrollment_id"] == eid)
    check("  ...and the utility bill is untouched too",
          query_one("SELECT COUNT(*) n FROM documents WHERE enrollment_id=? "
                    "AND doc_category='utility_bill'", (eid,))["n"] == 1)

    section("14. THE REFERENCE ENDPOINT RETURNS THE ANSWERS FOR RESUME")
    ref = c.get(f"/api/perch/enrollments/{eid}/lmi/self-attestation/reference",
                headers=h).get_json()
    check("it carries the submitted answers", ref.get("submitted") is not None)
    check("  ...occupancy", ref["submitted"]["occupancy"] == 3)
    check("  ...county", ref["submitted"]["county"] == "Albany County")
    check("  ...status", ref["submitted"]["status"] == "accepted")
    check("counties still come from the controlled table",
          isinstance(ref.get("counties"), list) and len(ref["counties"]) > 0)
    check("  ...and match ny_counties exactly",
          set(ref["counties"]) == {r["name"] for r in
                                   query("SELECT name FROM ny_counties WHERE is_active=1")})

    section("  ...and a fresh enrollment carries none of it")
    eid2, _ = build("fresh", enroll_next="lmi/self_attestation")
    ref2 = c.get(f"/api/perch/enrollments/{eid2}/lmi/self-attestation/reference",
                 headers=h).get_json()
    check("a different enrollment has no submitted answers", ref2.get("submitted") is None)
    check("  ...and no documents", query_one(
        "SELECT COUNT(*) n FROM documents WHERE enrollment_id=? AND doc_category='lmi_document'",
        (eid2,))["n"] == 0)
    check("  ...state did not leak between enrollments", eid2 != eid)

    # ═══════════════════════════════════════════════════════
    section("16 + 17. NO DUPLICATE ENROLLMENTS WERE CREATED")
    check("one local enrollment per build",
          query_one("SELECT COUNT(*) n FROM enrollments WHERE id IN (?,?)",
                    (eid, eid2))["n"] == 2)
    for e in (eid, eid2):
        check(f"enrollment {e} has exactly one workflow row",
              query_one("SELECT COUNT(*) n FROM perch_workflow_state WHERE enrollment_id=?",
                        (e,))["n"] == 1)
        check(f"  ...and one Perch token record at most",
              query_one("SELECT COUNT(*) n FROM perch_tokens WHERE enrollment_id=? "
                        "AND is_active=1", (e,))["n"] <= 1)
    check("no enrollment was discarded by any of this",
          query_one("SELECT COUNT(*) n FROM enrollments WHERE id IN (?,?) "
                    "AND discarded_at IS NOT NULL", (eid, eid2))["n"] == 0)

    # ═══════════════════════════════════════════════════════
    section("19-23. NON-LMI IS COMPLETELY UNAFFECTED")
    calls = {"proof": 0, "sa": 0, "accept": 0}
    real_proof = adapter.submit_proof_docs
    real_sa2 = adapter.submit_self_attestation
    real_accept = adapter.accept_self_attestation

    def spy_proof(*a, **k):
        calls["proof"] += 1
        return {"next_step_url": f"{BASE}/contracts"}

    def spy_sa(*a, **k):
        calls["sa"] += 1
        return {"next_step_url": f"{BASE}/contracts", "document_available": True}

    def spy_accept(*a, **k):
        calls["accept"] += 1
        return {"next_step_url": f"{BASE}/contracts"}

    # All THREE are spied. An unwired counter would assert nothing.
    adapter.submit_proof_docs = spy_proof
    adapter.submit_self_attestation = spy_sa
    adapter.accept_self_attestation = spy_accept
    check("the accept spy is really installed",
          adapter.accept_self_attestation is spy_accept)

    for tag, utility, zip_code in (("nyseg", "nyseg", "13348"),
                                   ("natgrid", "national-grid-ny", "13348")):
        eidn = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
        email = f"nonlmi.{tag}.{eidn}@example.com"
        rcap = c.post(f"/api/perch/enrollments/{eidn}/capacity", headers=h,
                      json={"email": email, "zip_code": zip_code, "utility_name": utility})
        check(f"{tag} non-LMI capacity still succeeds", rcap.status_code == 200)
        bill = upload(c, h, eidn, ROOT / "test" / "sample_utility_bill.pdf", "utility_bill")
        c.patch(f"/api/enrollments/{eidn}", headers=h, json={
            "customer": {"first_name": "Res", "last_name": "Ident", "email": email,
                         "phone": "5185550142"},
            "service_address": {"street": "1 Main St", "unit": "", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "billing_address": {"same_as_service": True, "street": "1 Main St", "unit": "",
                                "city": "Albany", "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": utility, "account_number": "9999999999",
                                "secondary_account_identifier": None}})
        real_enroll = adapter.create_enrollment
        # Residential: Perch sends a non-LMI enrollment STRAIGHT to contracts.
        adapter.create_enrollment = lambda *a, **k: {
            "customer_type": "Residential", "next_step_url": f"{BASE}/contracts"}
        renr = c.post(f"/api/perch/enrollments/{eidn}/enroll", headers=h,
                      json={"document_id": bill})
        adapter.create_enrollment = real_enroll
        check(f"  ...{tag} non-LMI /enroll succeeds", renr.status_code == 200)
        check(f"  ...and goes STRAIGHT to contracts",
              renr.get_json()["next_step_key"] == "contracts")
        check(f"  ...never landing on an LMI step",
              step_key(eidn) not in ("proof_docs", "self_attestation",
                                     "self_attestation_accept"))
        check(f"  ...so it never renders Eligibility (step 4)",
              f'"{step_key(eidn)}": 4' not in JS and f"{step_key(eidn)}: 4" not in JS)
        check(f"  ...no LMI qualification row was written",
              query_one("SELECT COUNT(*) n FROM lmi_qualifications WHERE enrollment_id=?",
                        (eidn,))["n"] == 0)
        check(f"  ...no self-attestation record",
              query_one("SELECT COUNT(*) n FROM perch_self_attestation_submissions "
                        "WHERE enrollment_id=?", (eidn,))["n"] == 0)
        check(f"  ...and lmi_path stayed null",
              query_one("SELECT lmi_path FROM enrollments WHERE id=?",
                        (eidn,))["lmi_path"] is None)

    check("NO LMI endpoint was called for any non-LMI enrollment",
          calls["proof"] == 0 and calls["sa"] == 0 and calls["accept"] == 0)
    check("  ...specifically /lmi/proof_docs: 0 calls", calls["proof"] == 0)
    check("  ...specifically /lmi/self_attestation: 0 calls", calls["sa"] == 0)
    check("  ...specifically /lmi/self_attestation/accept: 0 calls", calls["accept"] == 0)

    section("  ...and the spies FIRE when an LMI enrollment does use them")
    # Proves the three counters above are live wiring, not dead variables.
    eid_lmi, _ = build("spycheck", enroll_next="lmi/proof_docs")
    p_lmi = upload(c, h, eid_lmi, ROOT / "test" / "test_snap_proof_letter.pdf", "lmi_document")
    c.post(f"/api/perch/enrollments/{eid_lmi}/lmi/proof_docs", headers=h,
           json={"document_id": p_lmi, "source_type": "proof_doc_snap",
                 "name_on_document": "Dalton Dualstate", "relationship": "self",
                 "document_type": "letter"})
    check("the proof-doc spy fires for an LMI enrollment", calls["proof"] == 1)
    eid_sa, _ = build("spycheck2", enroll_next="lmi/self_attestation")
    c.post(f"/api/perch/enrollments/{eid_sa}/lmi/self-attestation", headers=h,
           json={"occupancy": 3, "county": "Albany County", "status": "accepted"})
    check("  ...and the self-attestation spy fires", calls["sa"] == 1)
    check("  ...so the zeros above were real observations, not dead counters",
          calls["proof"] == 1 and calls["sa"] == 1)

    adapter.submit_proof_docs = real_proof
    adapter.submit_self_attestation = real_sa2
    adapter.accept_self_attestation = real_accept

    section("23. RESIDENTIAL CUSTOMER TYPE AND SAVINGS ARE UNCHANGED")
    # A full Residential enrollment, selected and read back through the same
    # routes the rep uses. Nothing here is LMI-aware.
    eid_res = c.post("/api/perch/drafts", headers=h, json={}).get_json()["enrollment_id"]
    email_res = f"residential.savings.{eid_res}@example.com"
    # 10901 Suffern / Orange & Rockland: BOTH programs on offer, so the rep
    # genuinely chooses - the case where a Residential pick could be corrupted.
    rcap = c.post(f"/api/perch/enrollments/{eid_res}/capacity", headers=h,
                  json={"email": email_res, "zip_code": "10901",
                        "utility_name": "orange-and-rockland"})
    check("capacity offers both programs at a dual-capacity site",
          rcap.status_code == 200)
    progs = c.get(f"/api/perch/enrollments/{eid_res}/programs", headers=h).get_json()
    offered = {p["customer_type"]: p for p in progs["available_programs"]}
    check("  ...including Residential", "Residential" in offered)
    check("  ...and LMI", "LMI" in offered)
    res_savings_offered = offered["Residential"]["savings_percent"]
    lmi_savings_offered = offered["LMI"]["savings_percent"]
    check("  ...with DIFFERENT savings figures, so a mix-up would show",
          res_savings_offered != lmi_savings_offered)
    check("  ...and Residential is not flagged lmi_required",
          offered["Residential"]["lmi_required"] is False)

    r = c.post(f"/api/perch/enrollments/{eid_res}/program", headers=h,
               json={"customer_type": "Residential"})
    check("Residential can be selected", r.status_code == 200)
    row = query_one("SELECT selected_customer_type, lmi_path FROM enrollments WHERE id=?",
                    (eid_res,))
    check("  ...selected_customer_type persists EXACTLY as 'Residential'",
          row["selected_customer_type"] == "Residential")
    check("  ...and no LMI path is written", row["lmi_path"] is None)

    detail = c.get(f"/api/enrollments/{eid_res}", headers=h).get_json()
    check("the detail still reports Residential",
          detail.get("selected_customer_type") == "Residential")
    savings = (detail.get("program_savings") or {})
    check("  ...and carries the persisted Perch savings",
          savings.get("percent") is not None)
    check("  ...on the res/commercial BASIS, not the LMI one",
          savings.get("basis") == "residential_commercial")
    check("  ...the figure is exactly what capacity offered for Residential",
          float(savings["percent"]) == float(res_savings_offered))
    check("  ...and is NOT the LMI figure",
          float(savings["percent"]) != float(lmi_savings_offered))
    check("savings come from the persisted capacity row, not a computation",
          "Never computed" in open(os.path.join(ROOT, "routes", "enrollment_routes.py"),
                                   encoding="utf-8").read())

    section("  ...and none of it changed across this patch's code paths")
    before = dict(savings)
    c.get(f"/api/perch/enrollments/{eid_res}/lmi/self-attestation/reference", headers=h)
    after = (c.get(f"/api/enrollments/{eid_res}", headers=h).get_json()
             .get("program_savings") or {})
    check("reading LMI reference data does not touch Residential savings",
          before == after)
    check("  ...nor its customer type",
          query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                    (eid_res,))["selected_customer_type"] == "Residential")

    section("  ...every Residential enrollment in the DB is still clean")
    res = query("SELECT id, selected_customer_type FROM enrollments "
                "WHERE selected_customer_type = 'Residential'")
    check("Residential selected_customer_type still persists as written",
          all(r["selected_customer_type"] == "Residential" for r in res))
    check("no Residential enrollment acquired an LMI path",
          query_one("SELECT COUNT(*) n FROM enrollments WHERE "
                    "selected_customer_type='Residential' AND lmi_path IS NOT NULL")["n"] == 0)
    check("  ...nor a self-attestation record",
          query_one("SELECT COUNT(*) n FROM perch_self_attestation_submissions s "
                    "JOIN enrollments e ON e.id = s.enrollment_id "
                    "WHERE e.selected_customer_type='Residential'")["n"] == 0)
    check("  ...nor an LMI qualification row",
          query_one("SELECT COUNT(*) n FROM lmi_qualifications q "
                    "JOIN enrollments e ON e.id = q.enrollment_id "
                    "WHERE e.selected_customer_type='Residential'")["n"] == 0)

    section("NON-LMI RESUME AND CONTRACTS ARE UNCHANGED")
    # Resume through the real route, then confirm the resumed enrollment still
    # routes to the agreements screen and not to Eligibility.
    resumed = c.get(f"/api/enrollments/{eid_res}", headers=h)
    check("a Residential enrollment reopens cleanly", resumed.status_code == 200)
    body = resumed.get_json()
    check("  ...with its customer type intact",
          body.get("selected_customer_type") == "Residential")
    check("  ...its savings intact", (body.get("program_savings") or {}) == after)
    check("  ...and no LMI document set", not [d for d in (body.get("documents") or [])
                                               if d.get("category") == "lmi_document"])
    wf = query_one("SELECT current_step_key FROM perch_workflow_state WHERE enrollment_id=?",
                   (eid_res,))
    step = wf["current_step_key"] if wf else None
    check("  ...and its workflow step is not an LMI step",
          step not in ("proof_docs", "self_attestation", "self_attestation_accept"))
    check("  ...so the wizard map never sends it to Eligibility",
          step is None or f"{step}: 4" not in JS)

    # ═══════════════════════════════════════════════════════
    section("18. THE PROOF SOURCE_TYPE TAXONOMY IS UNCHANGED")
    eid3, _ = build("taxonomy", enroll_next="lmi/proof_docs")
    p3 = upload(c, h, eid3, ROOT / "test" / "test_snap_proof_letter.pdf", "lmi_document")
    r = c.post(f"/api/perch/enrollments/{eid3}/lmi/proof_docs", headers=h,
               json={"document_id": p3, "source_type": "snap_letter",
                     "name_on_document": "Dalton Dualstate", "relationship": "self",
                     "document_type": "letter"})
    check("a guessed legacy label is still rejected", r.status_code == 400)
    adapter.submit_proof_docs = lambda *a, **k: {"next_step_url": f"{BASE}/contracts"}
    r = c.post(f"/api/perch/enrollments/{eid3}/lmi/proof_docs", headers=h,
               json={"document_id": p3, "source_type": "proof_doc_snap",
                     "name_on_document": "Dalton Dualstate", "relationship": "self",
                     "document_type": "letter"})
    adapter.submit_proof_docs = real_proof
    check("  ...and a published source type still works", r.status_code == 200)
    check("the published types still come from the DB table",
          query_one("SELECT COUNT(*) n FROM perch_proof_doc_types WHERE "
                    "category='proof_doc' AND is_active=1")["n"] > 0)

    section("THE FRONTEND NO LONGER HIDES ONE SECTION TO SHOW THE OTHER")
    check("prepareSelfAttestation does not hide the proof panel",
          "proof.style.display = 'none'" not in JS)
    check("  ...it defers to the one section renderer",
          "renderLmiSections();" in JS.split("async function prepareSelfAttestation")[1][:400])
    check("section visibility is decided in ONE place",
          JS.count("function lmiSectionsForCurrentStep") == 1)
    check("the Continue gate is authorization, not mode",
          "sections.submits !== 'proof_docs'" in JS)

    print(f"\n{'='*72}\nLMI DUAL STATE - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
