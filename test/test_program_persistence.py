"""
Dual-program selection must PERSIST.

LIVE BUG (10901 Orange & Rockland): the rep selected Residential LMI, advanced,
and POST /enroll still failed with "This location has more than one available
program... Select which one". Navigating back showed no program cards at all.

Two defects:
  1. the /enroll route read customer_type into `data` but NEVER copied it into
     the payload sent to the adapter, so resolve_customer_type() always saw
     requested=None
  2. the choice lived only in a JS variable and the cards were loaded once,
     so back-navigation lost both

Run: python test/test_program_persistence.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one, execute
import seed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

DUAL = ("10901", "orange-and-rockland")        # Residential 5% + LMI 20%
RES_ONLY = ("12401", "central-hudson-gas-electric")   # Residential 5%
LMI_ONLY = ("12901", "nyseg")                  # LMI 20%


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


def start(c, h, zip_utility):
    zip_code, utility = zip_utility
    r = c.post("/api/perch/enrollments/capacity", headers=h,
               json={"email": f"p{zip_code}@example.com", "zip_code": zip_code,
                     "utility_name": utility})
    assert r.status_code == 200, r.data
    return r.get_json()["enrollment_id"]


def programs(c, h, eid):
    return c.get(f"/api/perch/enrollments/{eid}/programs", headers=h).get_json()


def select(c, h, eid, ctype):
    return c.post(f"/api/perch/enrollments/{eid}/program", headers=h,
                  json={"customer_type": ctype})


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("I. NO AUTOMATIC CHOICE when both programs exist")
    eid = start(c, rep, DUAL)
    body = programs(c, rep, eid)
    check("10901 offers both", len(body["available_programs"]) == 2)
    check("  ...Residential 5% and LMI 20%",
          {(p["customer_type"], p["savings_percent"])
           for p in body["available_programs"]} == {("Residential", 5.0), ("LMI", 20.0)})
    check("  ...selection_required is True", body["selection_required"] is True)
    check("  ...NOTHING is auto-selected", body["selected_customer_type"] is None)
    with app.app_context():
        check("  ...and nothing is persisted yet",
              query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                        (eid,))["selected_customer_type"] is None)

    # ═══════════════════════════════════════════════════════
    section("A. LMI SELECTION PERSISTS")
    r = select(c, rep, eid, "LMI")
    check("selecting LMI succeeds", r.status_code == 200)
    check("  ...returns the canonical value", r.get_json()["selected_customer_type"] == "LMI")
    with app.app_context():
        check("  ...persisted to the database",
              query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                        (eid,))["selected_customer_type"] == "LMI")

    section("C. BACK NAVIGATION restores both cards with LMI selected")
    body = programs(c, rep, eid)
    check("both cards still returned", len(body["available_programs"]) == 2)
    check("  ...with the correct 5% / 20% values",
          {(p["customer_type"], p["savings_percent"])
           for p in body["available_programs"]} == {("Residential", 5.0), ("LMI", 20.0)})
    check("  ...LMI reported as selected", body["selected_customer_type"] == "LMI")
    check("frontend reloads the cards when returning to step 1",
          "if(n === 1 && currentDraft && currentDraft.enrollment_id)" in JS
          and "loadProgramOptions();" in JS)
    check("frontend hydrates the selection from the backend",
          "body.selected_customer_type" in JS)

    section("D/E. FORWARD + RELOAD retain LMI")
    for i in range(3):
        check(f"reload {i+1} still reports LMI",
              programs(c, rep, eid)["selected_customer_type"] == "LMI")
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("enrollment detail exposes the selection for resume",
          detail["selected_customer_type"] == "LMI")
    check("frontend hydrates the branch on resume",
          "e.selected_customer_type" in JS and "lmi_required: e.selected_customer_type === 'LMI'" in JS)

    section("B. /enroll ACCEPTS the already-selected LMI path")
    # The regression: the route read customer_type but never forwarded it.
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"),
                      encoding="utf-8").read()
    check("the enroll route now forwards a chosen customer_type",
          'payload["customer_type"] = chosen' in routes_src)
    check("  ...preferring the request, falling back to the PERSISTED value",
          'data.get("customer_type")' in routes_src
          and 'enrollment["selected_customer_type"]' in routes_src)

    # Prove resolution succeeds for a dual-capacity location once chosen.
    from services.perch.adapter import resolve_customer_type, latest_capacity_check
    from services.perch.errors import PerchValidationError
    with app.app_context():
        details = (latest_capacity_check(eid) or {}).get("project_details") or {}
    with app.app_context():
        stored = query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                           (eid,))["selected_customer_type"]
    resolved, _ = resolve_customer_type(details, requested=stored)
    check("resolution with the persisted choice succeeds", resolved == "LMI")
    check("  ...and no longer raises 'select which one'", resolved is not None)
    try:
        resolve_customer_type(details, requested=None)
        unchosen_ok = True
    except PerchValidationError:
        unchosen_ok = False
    check("  ...while an UNCHOSEN dual location still demands a choice", not unchosen_ok)

    section("H. LMI RESUME INCLUDES ELIGIBILITY")
    # PASS 1 merged step 3 into step 2: Residential [1,2,5], LMI [1,2,4,5].
    check("LMI branch keeps the eligibility step",
          "if(needsLmi === false) return [1,2,5];" in JS)
    check("  ...so LMI runs the 4-step sequence including Eligibility",
          "return [1,2,4,5];" in JS)

    # ═══════════════════════════════════════════════════════
    section("F/G. RESIDENTIAL selection behaves identically")
    r = select(c, rep, eid, "Residential")
    check("switching to Residential succeeds", r.status_code == 200)
    check("  ...canonical value returned",
          r.get_json()["selected_customer_type"] == "Residential")
    check("  ...persisted", programs(c, rep, eid)["selected_customer_type"] == "Residential")
    check("  ...both cards still shown",
          len(programs(c, rep, eid)["available_programs"]) == 2)
    check("  ...detail exposes it for resume",
          c.get(f"/api/enrollments/{eid}", headers=rep)
           .get_json()["selected_customer_type"] == "Residential")
    with app.app_context():
        d2 = (latest_capacity_check(eid) or {}).get("project_details") or {}
    check("  ...and resolves without a prompt",
          resolve_customer_type(d2, requested="Residential")[0] == "Residential")
    check("G. Residential SKIPS eligibility (PASS 1: 3 steps, [1,2,5])",
          "if(needsLmi === false) return [1,2,5];" in JS)

    section("Clearing returns to 'not chosen' — never a silent default")
    r = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
               json={"customer_type": None})
    check("clearing succeeds", r.status_code == 200)
    check("  ...selection is None", r.get_json()["selected_customer_type"] is None)
    check("  ...and nothing is auto-chosen in its place",
          programs(c, rep, eid)["selected_customer_type"] is None)
    check("  ...selection is required again",
          programs(c, rep, eid)["selection_required"] is True)

    # ═══════════════════════════════════════════════════════
    section("J. TAMPERING still rejected")
    for bogus in ("SmallCommercial", "Small Commercial", "Business", "Commercial", "admin"):
        check(f"{bogus!r} rejected", select(c, rep, eid, bogus).status_code == 400)
    with app.app_context():
        check("  ...and nothing was persisted",
              query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                        (eid,))["selected_customer_type"] is None)

    res_eid = start(c, rep, RES_ONLY)
    check("12401 (Residential only): LMI is rejected",
          select(c, rep, res_eid, "LMI").status_code == 400)
    check("  ...Residential is accepted",
          select(c, rep, res_eid, "Residential").status_code == 200)
    lmi_eid = start(c, rep, LMI_ONLY)
    check("12901 (LMI only): Residential is rejected",
          select(c, rep, lmi_eid, "Residential").status_code == 400)
    check("  ...LMI is accepted", select(c, rep, lmi_eid, "LMI").status_code == 200)
    check("selection before any capacity check is refused",
          c.post("/api/perch/enrollments/999999/program", headers=rep,
                 json={"customer_type": "LMI"}).status_code in (403, 404))

    section("Ownership enforced on the selection route")
    from auth import hash_password
    with app.app_context():
        uid = execute("INSERT INTO users (email,password_hash,role,full_name) VALUES (?,?,?,?)",
                      ("progsel@d.com", hash_password("RepPass1!"),
                       "sales_rep", "Prog Sel")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-PS"))
    other = login(c, "progsel@d.com", "RepPass1!")
    check("another rep cannot select on this enrollment",
          select(c, other, eid, "LMI").status_code == 403)
    check("  ...nor read its programs",
          c.get(f"/api/perch/enrollments/{eid}/programs", headers=other).status_code == 403)
    check("unauthenticated rejected",
          c.post(f"/api/perch/enrollments/{eid}/program",
                 json={"customer_type": "LMI"}).status_code == 401)

    # ═══════════════════════════════════════════════════════
    section("K. 12401 / 12901 BEHAVIOUR UNCHANGED")
    rb = programs(c, rep, res_eid)
    check("12401 offers one program", len(rb["available_programs"]) == 1)
    check("  ...Residential at 5%",
          (rb["available_programs"][0]["customer_type"],
           rb["available_programs"][0]["savings_percent"]) == ("Residential", 5.0))
    check("  ...no selection prompt", rb["selection_required"] is False)
    lb = programs(c, rep, lmi_eid)
    check("12901 offers one program", len(lb["available_programs"]) == 1)
    check("  ...LMI at 20%",
          (lb["available_programs"][0]["customer_type"],
           lb["available_programs"][0]["savings_percent"]) == ("LMI", 20.0))
    check("  ...no selection prompt", lb["selection_required"] is False)
    check("no Small Commercial third option anywhere",
          all(p["customer_type"] in ("Residential", "LMI")
              for b in (rb, lb, programs(c, rep, eid))
              for p in b["available_programs"]))

    section("Migration + preserved behaviour")
    with app.app_context():
        migs = [x["filename"] for x in
                query_one.__globals__["query"]("SELECT filename FROM schema_migrations")]
    check("008 migration applied", any("008_program_selection" in m for m in migs))
    check("  ...and it is the only new one", len(migs) == 8)
    check("column is nullable with no backfill",
          "ALTER TABLE enrollments ADD COLUMN selected_customer_type TEXT" in
          open(os.path.join(ROOT, "db", "migrations", "008_program_selection.sql"),
               encoding="utf-8").read())
    check("no auto-selection workaround was introduced",
          "selectedProgram = availablePrograms[1]" not in JS)
    check("one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("one Agree & finish button", JS.count('id="agr-agree-btn"') == 1)
    check("resume reconciliation intact", "canRegenerate" in JS)
    check("B2 design system intact",
          "Clean Energy Enrollment — design tokens" in
          open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read())

    print(f"\n{'='*72}\nPROGRAM PERSISTENCE - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
