"""
Review & Finish must resolve program + savings identically for a LIVE, RESUMED
and COMPLETED enrollment.

Three defects this covers:
  (a) currentEnrollmentDetail was set only by openEnrollment(), so during a live
      enrollment detail.program_savings was undefined and the percentage vanished
  (b) a single-program location auto-selected CLIENT-SIDE only, leaving
      selected_customer_type NULL; the server then fell back to workflow state,
      decided Residential, and read the wrong field - 0% for LMI-only 12901
  (c) formatProgramType() inferred the program from program_savings.basis, so an
      enrollment with no figure read as Residential

Run: python test/test_review_consistency.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed
from routes.enrollment_routes import _program_savings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

FIXTURES = {
    "12401": ("central-hudson-gas-electric", {"Residential": 5.0}),
    "10901": ("orange-and-rockland", {"Residential": 5.0, "LMI": 20.0}),
    "12901": ("nyseg", {"LMI": 20.0}),
}


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    r = c.post("/api/auth/login",
               json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"})
    rep = {"Authorization": "Bearer " + r.get_json()["token"]}

    def start(zip_code):
        utility = FIXTURES[zip_code][0]
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"rv{zip_code}@example.com", "zip_code": zip_code,
                            "utility_name": utility}).get_json()["enrollment_id"]

    section("SAVINGS FOLLOWS THE SELECTED PROGRAM (staging fixtures)")
    for zip_code, (_utility, expected) in FIXTURES.items():
        for ctype, pct in expected.items():
            eid = start(zip_code)
            sel = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                         json={"customer_type": ctype})
            check(f"{zip_code} accepts {ctype}", sel.status_code == 200)
            with app.app_context():
                ps = _program_savings(eid)
            check(f"  ...{zip_code} {ctype} -> {pct}% (got {ps and ps['percent']})",
                  ps and ps["percent"] == pct)
            check("  ...with the matching basis",
                  ps["basis"] == ("lmi" if ctype == "LMI" else "residential_commercial"))

    section("NO CROSS-FALLBACK")
    eid = start("12901")                       # LMI only, residential figure is 0
    c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
           json={"customer_type": "LMI"})
    with app.app_context():
        ps = _program_savings(eid)
    check("12901 LMI reads 20%, NOT the 0% residential figure", ps["percent"] == 20.0)
    check("  ...basis is lmi", ps["basis"] == "lmi")

    dual = start("10901")
    c.post(f"/api/perch/enrollments/{dual}/program", headers=rep,
           json={"customer_type": "LMI"})
    with app.app_context():
        lmi_val = _program_savings(dual)["percent"]
    c.post(f"/api/perch/enrollments/{dual}/program", headers=rep,
           json={"customer_type": "Residential"})
    with app.app_context():
        res_val = _program_savings(dual)["percent"]
    check("switching LMI -> Residential moves 20% -> 5%",
          (lmi_val, res_val) == (20.0, 5.0))
    check("  ...and the two are distinct", lmi_val != res_val)

    section("THE THREE STATES AGREE")
    live = start("10901")
    c.post(f"/api/perch/enrollments/{live}/program", headers=rep,
           json={"customer_type": "LMI"})
    detail_live = c.get(f"/api/enrollments/{live}", headers=rep).get_json()
    check("LIVE: detail exposes the selected type",
          detail_live["selected_customer_type"] == "LMI")
    check("  ...and its savings", detail_live["program_savings"]["percent"] == 20.0)

    detail_resumed = c.get(f"/api/enrollments/{live}", headers=rep).get_json()
    check("RESUMED: identical selected type",
          detail_resumed["selected_customer_type"] == detail_live["selected_customer_type"])
    check("  ...identical savings",
          detail_resumed["program_savings"] == detail_live["program_savings"])

    listed = [x for x in c.get("/api/enrollments", headers=rep).get_json()
              if x["id"] == live][0]
    check("DASHBOARD row carries the selected type too",
          listed.get("selected_customer_type") == "LMI")
    check("  ...and the same savings", listed["program_savings"]["percent"] == 20.0)

    section("SERVER SIDE — selection is authoritative, basis is not")
    src = open(os.path.join(ROOT, "routes", "enrollment_routes.py"), encoding="utf-8").read()
    check("_program_savings reads enrollments.selected_customer_type",
          "SELECT selected_customer_type FROM enrollments WHERE id = ?" in src)
    check("  ...before any workflow-state fallback",
          src.index("selected_customer_type FROM enrollments")
          < src.index("last_response_json FROM perch_workflow_state"))
    check("no hardcoded percentages in the resolver",
          not any(f"= {n}" in src.split("def _program_savings")[1][:1500]
                  for n in ("5.0", "20.0", "25.0")))

    section("CLIENT SIDE — one resolver for all three states")
    check("resolveProgramView() exists", "function resolveProgramView(" in JS)
    check("  ...prefers the PERSISTED selection",
          "detail.selected_customer_type" in JS.split("function resolveProgramView")[1][:600])
    check("  ...falls back to the live selection only after that",
          JS.split("function resolveProgramView")[1].index("detail.selected_customer_type")
          < JS.split("function resolveProgramView")[1].index("selectedProgram && selectedProgram.customer_type"))
    check("  ...rejects a savings value whose basis contradicts the type",
          "(ps.basis === 'lmi') === isLmi" in JS)
    check("Review uses it", "const view = resolveProgramView();" in JS)
    check("  ...and so does the completion summary",
          JS.count("resolveProgramView()") >= 2)
    check("the old detail-only read is gone from Review",
          "const prog = detail.program_savings || null;" not in JS)

    check("single-program selections are PERSISTED, not just local",
          "persistSelectedProgram(selectedProgram.customer_type)" in JS)
    check("  ...via the existing program endpoint",
          "'/program'" in JS and "function persistSelectedProgram(" in JS)

    check("formatProgramType takes the authoritative type",
          "function formatProgramType(programSavings, selectedType)" in JS)
    check("  ...and no longer infers LMI purely from basis",
          "if(t === 'LMI') return 'Income-eligible (LMI)';" in JS)

    check("live enrollments refresh the persisted detail before Review",
          "function refreshEnrollmentDetail(" in JS
          and "refreshEnrollmentDetail().then(function(){ fillReview(); });" in JS)

    section("AVAILABILITY: button + validation + rehydration")
    check("the primary button restores on EVERY exit path",
          "const restoreBtn = () =>" in JS and JS.count("restoreBtn()") >= 3)
    check("an empty required field reports MISSING, not malformed",
          "(f.label || 'This field') + ' is required.'" in JS)
    check("  ...the pattern message is reserved for a non-empty bad value",
          "msg = f.validation.message || ('That ' + f.label" in JS)
    check("Back to Availability rehydrates the saved values",
          "function rehydrateAvailability(" in JS and "rehydrateAvailability();" in JS)

    section("RESUME: the persisted bill is represented")
    check("documents are rehydrated from the server record",
          "function rehydrateDocumentsFromDetail(" in JS)
    check("  ...invoked on resume", "rehydrateDocumentsFromDetail(e);" in JS)
    check("  ...restoring the document id, not a fake File",
          "persisted: true" in JS and "size: null" in JS)
    check("  ...for the utility bill", "state.bill.documentId = first.id;" in JS)
    check("no plaintext password is ever persisted or restored",
          "state.customer.password = detail" not in JS
          and "password" not in JS.split("function rehydrateDocumentsFromDetail")[1][:900])

    print(f"\n{'='*72}\nREVIEW CONSISTENCY - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
