"""
Program availability + explicit customer-type selection.

Guards the behaviour change: choose_customer_type() used to prefer LMI whenever
LMI capacity existed, so a rep could never enrol a Residential customer in a ZIP
that also had LMI capacity - and could silently enrol someone in the wrong
program. Availability now comes from the capacity response, and an ambiguous
result requires an explicit, validated selection.

Run: python test/test_program_selection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import adapter, workflow
from services.perch.adapter import (available_programs, resolve_customer_type,
                                    choose_customer_type,
                                    CUSTOMER_TYPE_RESIDENTIAL, CUSTOMER_TYPE_LMI,
                                    SELECTABLE_CUSTOMER_TYPES)
from services.perch.errors import PerchValidationError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Capacity shapes exactly as Perch returns them.
RES_ONLY = {"residential_capacity_available": True,
            "lmi_capacity_available": False,
            "savings_percent_for_residential_and_commercial_customers": 8,
            "savings_percent_for_lmi_customers": 0,
            "proof_documents_required": False}
LMI_ONLY = {"residential_capacity_available": False,
            "lmi_capacity_available": True,
            "savings_percent_for_residential_and_commercial_customers": 0,
            "savings_percent_for_lmi_customers": 20,
            "proof_documents_required": True}
BOTH = {"residential_capacity_available": True,
        "lmi_capacity_available": True,
        "savings_percent_for_residential_and_commercial_customers": 10,
        "savings_percent_for_lmi_customers": 20,
        "proof_documents_required": True}
NONE = {"residential_capacity_available": False, "lmi_capacity_available": False}
COMMERCIAL_ONLY = {"small_commercial_capacity_available": True}


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def rejects(details, requested):
    try:
        resolve_customer_type(details, requested=requested)
        return False
    except PerchValidationError:
        return True


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def by_type(programs, t):
    return next((p for p in programs if p["customer_type"] == t), None)


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("A. RESIDENTIAL-ONLY CAPACITY")
    progs = available_programs(RES_ONLY)
    check("exactly one program offered", len(progs) == 1)
    check("  ...and it is Residential",
          progs[0]["customer_type"] == CUSTOMER_TYPE_RESIDENTIAL)
    check("  ...LMI is NOT offered", by_type(progs, CUSTOMER_TYPE_LMI) is None)
    check("  ...savings from the residential field", progs[0]["savings_percent"] == 8.0)
    check("  ...no LMI proof required", progs[0]["lmi_required"] is False)

    t, why = resolve_customer_type(RES_ONLY)
    check("unambiguous -> auto-selected", t == CUSTOMER_TYPE_RESIDENTIAL)
    check("  ...reason states why", "only_available_option" in why)
    check("explicit Residential accepted",
          resolve_customer_type(RES_ONLY, "Residential")[0] == CUSTOMER_TYPE_RESIDENTIAL)
    check("LMI selection REJECTED", rejects(RES_ONLY, "LMI"))

    section("B. LMI-ONLY CAPACITY (existing path must be intact)")
    progs = available_programs(LMI_ONLY)
    check("exactly one program offered", len(progs) == 1)
    check("  ...and it is LMI", progs[0]["customer_type"] == CUSTOMER_TYPE_LMI)
    check("  ...Residential is NOT offered",
          by_type(progs, CUSTOMER_TYPE_RESIDENTIAL) is None)
    check("  ...savings from the LMI field", progs[0]["savings_percent"] == 20.0)
    check("  ...LMI proof required", progs[0]["lmi_required"] is True)
    check("unambiguous -> auto-selected LMI",
          resolve_customer_type(LMI_ONLY)[0] == CUSTOMER_TYPE_LMI)
    check("explicit LMI accepted",
          resolve_customer_type(LMI_ONLY, "LMI")[0] == CUSTOMER_TYPE_LMI)
    check("Residential selection REJECTED", rejects(LMI_ONLY, "Residential"))

    # An LMI program that needs no proof must not be forced to require it.
    no_proof = dict(LMI_ONLY, proof_documents_required=False)
    check("LMI without proof requirement is honoured",
          available_programs(no_proof)[0]["lmi_required"] is False)

    section("C. BOTH AVAILABLE — no silent LMI preference")
    progs = available_programs(BOTH)
    check("two programs offered", len(progs) == 2)
    res, lmi = by_type(progs, CUSTOMER_TYPE_RESIDENTIAL), by_type(progs, CUSTOMER_TYPE_LMI)
    check("Residential present", res is not None)
    check("LMI present", lmi is not None)
    check("Residential carries its OWN savings", res["savings_percent"] == 10.0)
    check("LMI carries its OWN savings", lmi["savings_percent"] == 20.0)
    check("  ...they are not conflated", res["savings_percent"] != lmi["savings_percent"])
    check("Residential needs no proof", res["lmi_required"] is False)
    check("LMI needs proof", lmi["lmi_required"] is True)

    # THE BEHAVIOUR CHANGE.
    check("ambiguous capacity does NOT auto-select", rejects(BOTH, None))
    try:
        resolve_customer_type(BOTH)
        msg = ""
    except PerchValidationError as e:
        msg = str(e)
    check("  ...the error names both options",
          "Residential" in msg and "LMI" in msg)
    check("  ...and asks for a selection", "select" in msg.lower())
    check("explicit Residential works",
          resolve_customer_type(BOTH, "Residential")[0] == CUSTOMER_TYPE_RESIDENTIAL)
    check("explicit LMI works",
          resolve_customer_type(BOTH, "LMI")[0] == CUSTOMER_TYPE_LMI)
    check("  ...reason records it was explicit",
          "explicit_selection" in resolve_customer_type(BOTH, "LMI")[1])
    check("selection is case-insensitive but canonicalised",
          resolve_customer_type(BOTH, "residential")[0] == CUSTOMER_TYPE_RESIDENTIAL
          and resolve_customer_type(BOTH, "lmi")[0] == CUSTOMER_TYPE_LMI)

    # The old wrapper must no longer silently prefer LMI.
    try:
        choose_customer_type(BOTH)
        legacy_auto = True
    except PerchValidationError:
        legacy_auto = False
    check("legacy choose_customer_type no longer prefers LMI silently", not legacy_auto)
    check("  ...but still works for unambiguous capacity",
          choose_customer_type(LMI_ONLY)[0] == CUSTOMER_TYPE_LMI
          and choose_customer_type(RES_ONLY)[0] == CUSTOMER_TYPE_RESIDENTIAL)

    section("D. UNSUPPORTED / NO CAPACITY — nothing invented")
    check("no capacity -> no programs", available_programs(NONE) == [])
    check("empty details -> no programs", available_programs({}) == [])
    check("None details -> no programs", available_programs(None) == [])
    check("no capacity is rejected", rejects(NONE, None))
    check("  ...and a request cannot conjure one", rejects(NONE, "Residential"))
    # SUPERSEDED by Perch Engineering (2026-08): "yes if you see small cs/resi =
    # resi". small_commercial_capacity_available is STANDARD RESIDENTIAL in this
    # NY funnel, so it now yields a Residential option rather than a dead end.
    # This does NOT add a Small Commercial product - see test_post_b2_followups.
    check("small-commercial capacity now maps to Residential",
          [p["customer_type"] for p in available_programs(COMMERCIAL_ONLY)] == ["Residential"])
    check("  ...and resolves without a selection prompt",
          resolve_customer_type(COMMERCIAL_ONLY)[0] == "Residential")
    check("  ...but is never offered as its own product type",
          all(p["customer_type"] in ("Residential", "LMI")
              for p in available_programs(COMMERCIAL_ONLY)))

    # Savings must never be fabricated.
    missing = {"residential_capacity_available": True}
    check("missing savings -> None, not 0 or a default",
          available_programs(missing)[0]["savings_percent"] is None)
    zero = dict(RES_ONLY, savings_percent_for_residential_and_commercial_customers=0)
    check("zero savings -> None rather than '0%'",
          available_programs(zero)[0]["savings_percent"] is None)
    junk = dict(RES_ONLY, savings_percent_for_residential_and_commercial_customers="abc")
    check("non-numeric savings -> None",
          available_programs(junk)[0]["savings_percent"] is None)

    section("E. TAMPERING — client cannot pick an unoffered program")
    for details, label in ((RES_ONLY, "residential-only"), (LMI_ONLY, "LMI-only")):
        other = "LMI" if details is RES_ONLY else "Residential"
        check(f"{label}: requesting {other} is rejected", rejects(details, other))
    for bogus in ("Business", "SmallCommercial", "admin", "", "  ", "Residential; DROP"):
        if bogus.strip():
            check(f"bogus type {bogus!r} rejected on BOTH", rejects(BOTH, bogus))
    check("blank request falls back to normal resolution, not a bypass",
          resolve_customer_type(RES_ONLY, "   ")[0] == CUSTOMER_TYPE_RESIDENTIAL)
    check("commercial cannot be selected even if Perch offers it",
          rejects(dict(BOTH, small_commercial_capacity_available=True), "SmallCommercial"))
    check("selectable set is exactly Residential + LMI",
          set(SELECTABLE_CUSTOMER_TYPES) == {"Residential", "LMI"})

    # ═══════════════════════════════════════════════════════
    section("API — GET /programs reflects the capacity response")
    eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    before = c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep).get_json()
    check("before any capacity check: no programs", before["available_programs"] == [])
    check("  ...and no selection is demanded", before["selection_required"] is False)
    check("  ...capacity_checked is False", before["capacity_checked"] is False)

    c.post(f"/api/perch/enrollments/{eid}/capacity", headers=rep,
           json={"email": "res@example.com", "zip_code": "12010",
                 "utility_name": "national-grid-ny"})
    res_body = c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep).get_json()
    check("residential-only fixture -> one program",
          len(res_body["available_programs"]) == 1)
    check("  ...Residential", res_body["available_programs"][0]["customer_type"] == "Residential")
    check("  ...no selection required", res_body["selection_required"] is False)
    check("  ...savings carried from Perch",
          res_body["available_programs"][0]["savings_percent"] == 8.0)

    eid2 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.post(f"/api/perch/enrollments/{eid2}/capacity", headers=rep,
           json={"email": "both@example.com", "zip_code": "13301",
                 "utility_name": "national-grid-ny"})
    both_body = c.get(f"/api/perch/enrollments/{eid2}/programs", headers=rep).get_json()
    check("both-available fixture -> two programs",
          len(both_body["available_programs"]) == 2)
    check("  ...selection IS required", both_body["selection_required"] is True)
    types = {p["customer_type"] for p in both_body["available_programs"]}
    check("  ...both types offered", types == {"Residential", "LMI"})
    savings = {p["customer_type"]: p["savings_percent"]
               for p in both_body["available_programs"]}
    check("  ...with distinct savings", savings["Residential"] != savings["LMI"])

    section("API — access control and side-effect freedom")
    check("unauthenticated rejected",
          c.get(f"/api/perch/enrollments/{eid}/programs").status_code == 401)
    with app.app_context():
        before_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
    for _ in range(3):
        c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep)
    with app.app_context():
        after_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
    check("reading programs makes ZERO Perch calls", after_calls == before_calls)

    # Another rep must not read this enrollment's programs.
    from auth import hash_password
    with app.app_context():
        uid = execute("INSERT INTO users (email, password_hash, role, full_name) "
                      "VALUES (?,?,?,?)",
                      ("progrep@d.com", hash_password("RepPass1!"),
                       "sales_rep", "Prog Rep")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-PR"))
    other_rep = login(c, "progrep@d.com", "RepPass1!")
    check("another rep is denied",
          c.get(f"/api/perch/enrollments/{eid}/programs",
                headers=other_rep).status_code == 403)

    section("SAVINGS FOLLOWS THE SELECTED PROGRAM")
    with app.app_context():
        execute("DELETE FROM perch_capacity_checks WHERE enrollment_id = ?", (eid2,))
        execute("""INSERT INTO perch_capacity_checks
                   (enrollment_id, zip_code, utility_slug, capacity_available,
                    residential_capacity_available, lmi_capacity_available,
                    savings_percent_res_commercial, savings_percent_lmi,
                    proof_documents_required, raw_response_json, api_mode)
                   VALUES (?,?,?,1,1,1,?,?,1,'{}','mock')""",
                (eid2, "13301", "national-grid-ny", 10, 20))
        workflow.set_state(eid2, "enroll", last_response={"customer_type": "Residential"})
    got = c.get(f"/api/enrollments/{eid2}", headers=rep).get_json()["program_savings"]
    check("Residential selected -> residential savings", got["percent"] == 10.0)
    check("  ...basis reported", got["basis"] == "residential_commercial")
    with app.app_context():
        workflow.set_state(eid2, "enroll", last_response={"customer_type": "LMI"})
    got = c.get(f"/api/enrollments/{eid2}", headers=rep).get_json()["program_savings"]
    check("LMI selected -> LMI savings", got["percent"] == 20.0)
    check("  ...basis reported", got["basis"] == "lmi")
    check("  ...no cross-fallback between programs", got["percent"] != 10.0)

    section("ENROLL VALIDATES THE SELECTION AGAINST CAPACITY")
    src = open(os.path.join(ROOT, "services", "perch", "adapter.py"), encoding="utf-8").read()
    check("create_enrollment resolves against the capacity response",
          "resolve_customer_type(details, requested=requested_type)" in src)
    check("  ...taking the request from the payload",
          'requested_type = (enrollment_payload or {}).get("customer_type")' in src)
    check("the resolved type is what gets sent to Perch",
          'payload["customer_type"] = customer_type' in src)
    check("residential reads only the residential savings field",
          "savings_percent_for_residential_and_commercial_customers" in src)
    check("LMI reads only the LMI savings field",
          "savings_percent_for_lmi_customers" in src)

    section("EXISTING BEHAVIOUR PRESERVED")
    check("acceptance timezone untouched",
          "ACCEPTANCE_TIMEZONE_NAME" in open(
              os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read())
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("one acknowledgement checkbox", js.count('id="agr-ack-check"') == 1)
    check("one Agree & finish button", js.count('id="agr-agree-btn"') == 1)
    check("inline agreement links retained", "agreementLinksHtml" in js)
    check("Phase A dashboard buckets retained", "dashBucketFor" in js)
    check("resume reconciliation retained", "canRegenerate" in js)
    check("no ZIP->utility lookup reintroduced",
          "utilities/lookup" not in js and not os.path.exists(
              os.path.join(ROOT, "services", "perch", "territories.py")))
    with app.app_context():
        migrations = query("SELECT filename FROM schema_migrations")
    # 008_program_selection added later for dual-program persistence; this
    # milestone itself still added none.
    check("this milestone added no migration of its own", len(migrations) == 8)

    print(f"\n{'='*72}\nPROGRAM SELECTION - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
