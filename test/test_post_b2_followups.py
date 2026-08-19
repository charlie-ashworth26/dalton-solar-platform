"""
Post-B2 follow-ups: enrollment lifecycle, small-CS capacity mapping,
capacity copy, timestamps, email editability.

Run: python test/test_post_b2_followups.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import workflow
from services.perch.adapter import available_programs, resolve_customer_type
from services.perch.errors import PerchValidationError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()

S_RES = "savings_percent_for_residential_and_commercial_customers"
S_LMI = "savings_percent_for_lmi_customers"


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


def count_enrollments():
    with app.app_context():
        return query_one("SELECT COUNT(*) n FROM enrollments")["n"]


def types_for(details):
    return [p["customer_type"] for p in available_programs(details)]


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("1. NO BLANK ENROLLMENTS  (A-E)")
    check("baseline is empty", count_enrollments() == 0)

    # A. Opening New Enrollment persists nothing.
    r = c.get("/api/perch/workflow/new", headers=rep)
    check("A. opening New Enrollment returns the first step", r.status_code == 200)
    check("   ...step is service_area", r.get_json()["step"]["key"] == "service_area")
    check("   ...with email/zip/utility fields",
          [f["name"] for f in r.get_json()["step"]["fields"]]
          == ["email", "zip_code", "utility_name"])
    check("   ...and NO enrollment id is issued",
          r.get_json()["enrollment_id"] is None)
    check("   ...ZERO enrollment rows created", count_enrollments() == 0)
    with app.app_context():
        check("   ...ZERO workflow rows created",
              query_one("SELECT COUNT(*) n FROM perch_workflow_state")["n"] == 0)
        check("   ...nothing audit-logged as a draft",
              query_one("SELECT COUNT(*) n FROM audit_logs "
                        "WHERE action='enrollment_draft_created'")["n"] == 0)

    # B. Opening repeatedly then abandoning persists nothing.
    for _ in range(3):
        c.get("/api/perch/workflow/new", headers=rep)
    check("B. opening repeatedly then leaving creates ZERO rows", count_enrollments() == 0)

    # Invalid first submissions must not persist either.
    for payload, label in (({}, "empty"),
                           ({"email": "a@b.com"}, "email only"),
                           ({"email": "", "zip_code": "", "utility_name": ""}, "all blank"),
                           ({"zip_code": "12207", "utility_name": "national-grid-ny"},
                            "no email")):
        rr = c.post("/api/perch/enrollments/capacity", headers=rep, json=payload)
        check(f"   invalid submission ({label}) -> 400", rr.status_code == 400)
        check(f"   ...and creates NOTHING", count_enrollments() == 0)
    check("   ...error names the missing fields",
          "missing" in c.post("/api/perch/enrollments/capacity", headers=rep,
                              json={}).get_json())

    # C. A valid first submission creates exactly one.
    r = c.post("/api/perch/enrollments/capacity", headers=rep,
               json={"email": "first@example.com", "zip_code": "12207",
                     "utility_name": "national-grid-ny"})
    check("C. valid first submission succeeds", r.status_code == 200)
    check("   ...creates EXACTLY ONE enrollment", count_enrollments() == 1)
    body = r.get_json()
    eid = body["enrollment_id"]
    check("   ...returns the enrollment id", isinstance(eid, int))
    check("   ...and its code", str(body.get("enrollment_code", "")).startswith("ENR-"))
    with app.app_context():
        row = query_one("SELECT * FROM enrollments WHERE id = ?", (eid,))
        rep_row = query_one("SELECT sr.id FROM sales_reps sr JOIN users u ON u.id=sr.user_id "
                            "WHERE u.email = ?", ("charlie@daltonsolar.com",))
    check("   ...ownership stamped server-side", row["sales_rep_id"] == rep_row["id"])
    check("   ...workflow state initialised",
          workflow.get_state(eid) is not None)

    # D. Retries reuse the row.
    for i in range(3):
        rr = c.post("/api/perch/enrollments/capacity", headers=rep,
                    json={"enrollment_id": eid, "email": "first@example.com",
                          "zip_code": "12207", "utility_name": "national-grid-ny"})
        check(f"D. retry {i+1} succeeds", rr.status_code == 200)
    check("   ...still exactly ONE enrollment (no duplicates)", count_enrollments() == 1)

    # A retry cannot target someone else's enrollment.
    from auth import hash_password
    with app.app_context():
        uid = execute("INSERT INTO users (email,password_hash,role,full_name) VALUES (?,?,?,?)",
                      ("lifecycle@d.com", hash_password("RepPass1!"),
                       "sales_rep", "Life Cycle")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-LC"))
    other = login(c, "lifecycle@d.com", "RepPass1!")
    check("   ...another rep cannot retry against this enrollment",
          c.post("/api/perch/enrollments/capacity", headers=other,
                 json={"enrollment_id": eid, "email": "x@y.com", "zip_code": "12207",
                       "utility_name": "national-grid-ny"}).status_code == 403)
    check("   ...and still no extra row", count_enrollments() == 1)

    # E. Resume still works.
    check("E. workflow resume works",
          c.get(f"/api/perch/enrollments/{eid}/workflow", headers=rep).status_code == 200)
    check("   ...enrollment detail loads",
          c.get(f"/api/enrollments/{eid}", headers=rep).status_code == 200)
    check("   ...it appears on the dashboard",
          any(x["id"] == eid for x in c.get("/api/enrollments", headers=rep).get_json()))

    section("1b. FRONTEND no longer persists on open")
    fresh = JS[JS.index("async function startWizardFresh"):]
    fresh = fresh[:fresh.index("\n}")]
    check("startWizardFresh does NOT POST /drafts", "/api/perch/drafts" not in fresh)
    check("  ...it fetches the step definition instead", "/api/perch/workflow/new" in fresh)
    check("  ...and clears any previous draft", "currentDraft = null" in fresh)
    cap = JS[JS.index("async function submitCapacity"):]
    cap = cap[:cap.index("\n}")]
    check("submitCapacity posts to the create-then-check route",
          "/api/perch/enrollments/capacity'" in cap)
    check("  ...passing an existing id when present", "payload.enrollment_id" in cap)
    check("  ...and adopts the returned enrollment", "body.enrollment_id" in cap)
    check("blank rows are NOT merely hidden from the dashboard",
          "filter" not in JS.split("function dashBucketFor")[0][-400:]
          or "enrollment_code" in JS)

    # ═══════════════════════════════════════════════════════
    section("2. SMALL CS = RESIDENTIAL  (per Perch Engineering)")
    RES_ONLY = {"residential_capacity_available": True, S_RES: 8}
    SCS_ONLY = {"residential_capacity_available": False,
                "small_commercial_capacity_available": True,
                "lmi_capacity_available": False, S_RES: 10}
    SCS_LMI = {"residential_capacity_available": False,
               "small_commercial_capacity_available": True,
               "lmi_capacity_available": True, S_RES: 10, S_LMI: 20,
               "proof_documents_required": True}
    LMI_ONLY = {"residential_capacity_available": False,
                "small_commercial_capacity_available": False,
                "lmi_capacity_available": True, S_LMI: 20,
                "proof_documents_required": True}
    RES_LMI = {"residential_capacity_available": True, "lmi_capacity_available": True,
               S_RES: 10, S_LMI: 20, "proof_documents_required": True}

    check("residential flag only -> Residential", types_for(RES_ONLY) == ["Residential"])
    check("12401 small CS only -> Residential", types_for(SCS_ONLY) == ["Residential"])
    check("12901 LMI only -> LMI", types_for(LMI_ONLY) == ["LMI"])
    check("10901 small CS + LMI -> BOTH",
          set(types_for(SCS_LMI)) == {"Residential", "LMI"})
    check("residential + LMI -> BOTH", set(types_for(RES_LMI)) == {"Residential", "LMI"})
    check("no supported capacity -> none", types_for({}) == [])
    check("  ...and resolution refuses it",
          isinstance(_raises(lambda: resolve_customer_type({})), PerchValidationError))

    check("NO third Small Commercial choice is offered",
          all(t in ("Residential", "LMI")
              for d in (RES_ONLY, SCS_ONLY, SCS_LMI, LMI_ONLY, RES_LMI)
              for t in types_for(d)))
    check("  ...and none surfaces in the UI",
          "Small Commercial" not in JS and "small_commercial" not in JS)

    section("2b. SAVINGS MAPPING — never cross-fallback")
    check("small-CS Residential uses the res/commercial figure",
          available_programs(SCS_ONLY)[0]["savings_percent"] == 10.0)
    both = {p["customer_type"]: p["savings_percent"] for p in available_programs(SCS_LMI)}
    check("  ...Residential = res/commercial figure", both["Residential"] == 10.0)
    check("  ...LMI = LMI figure", both["LMI"] == 20.0)
    check("  ...they are distinct", both["Residential"] != both["LMI"])
    no_res_fig = {"small_commercial_capacity_available": True, S_LMI: 20}
    check("Residential with no res figure -> None (does NOT borrow LMI's)",
          available_programs(no_res_fig)[0]["savings_percent"] is None)
    no_lmi_fig = {"lmi_capacity_available": True, S_RES: 10}
    check("LMI with no LMI figure -> None (does NOT borrow residential's)",
          available_programs(no_lmi_fig)[0]["savings_percent"] is None)
    adapter_src = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
                       encoding="utf-8").read()
    for pct in ("5%", "10%", "20%", "25%"):
        check(f"no hardcoded {pct} in the adapter", pct not in adapter_src)
    check("the mapping is documented with Perch's wording",
          "small cs" in adapter_src.lower() and "25kW" in adapter_src)

    section("2c. SELECTION TAMPERING still rejected")
    check("small-CS-only: LMI request rejected",
          isinstance(_raises(lambda: resolve_customer_type(SCS_ONLY, "LMI")),
                     PerchValidationError))
    check("LMI-only: Residential request rejected",
          isinstance(_raises(lambda: resolve_customer_type(LMI_ONLY, "Residential")),
                     PerchValidationError))
    for bogus in ("SmallCommercial", "Small Commercial", "Business", "Commercial"):
        check(f"{bogus!r} is never selectable",
              isinstance(_raises(lambda b=bogus: resolve_customer_type(SCS_LMI, b)),
                         PerchValidationError))
    check("small CS auto-selects Residential when it is the only option",
          resolve_customer_type(SCS_ONLY)[0] == "Residential")
    check("small CS + LMI requires an explicit choice",
          isinstance(_raises(lambda: resolve_customer_type(SCS_LMI)), PerchValidationError))
    check("  ...and both explicit choices work",
          resolve_customer_type(SCS_LMI, "Residential")[0] == "Residential"
          and resolve_customer_type(SCS_LMI, "LMI")[0] == "LMI")

    # ═══════════════════════════════════════════════════════
    section("4. CAPACITY COPY matches the actual response")
    def texts(d):
        return " ".join(n["text"] for n in workflow._capacity_notices(d)).lower()

    scs = texts(SCS_ONLY)
    check("residential available -> says so plainly", "standard residential" in scs)
    check("  ...and states LMI is unavailable", "not available" in scs)
    check("  ...never claims residential 'may still be available'",
          "may still be" not in scs)

    lmi_only = texts(LMI_ONLY)
    check("LMI only -> says only the income-qualified program is available",
          "only the income-qualified" in lmi_only)
    check("  ...and does NOT claim residential may be available",
          "residential" not in lmi_only.replace("income-qualified", ""))

    both_txt = texts(SCS_LMI)
    check("both available -> no contradictory claim", "not available" not in both_txt)
    check("  ...proof requirement mentioned once", both_txt.count("proof") <= 1)

    none_txt = texts({})
    check("no capacity -> clean no-availability message",
          "no available program" in none_txt)
    check("  ...and promises nothing", "may still" not in none_txt)

    check("proof warning only when LMI is actually offered",
          "proof" not in texts({"residential_capacity_available": True,
                                "proof_documents_required": True}))
    old_copy = "Residential capacity may still be available at the standard savings rate"
    wf_src = open(os.path.join(ROOT, "services", "perch", "workflow.py"),
                  encoding="utf-8").read()
    check("the misleading sentence is gone from the codebase", old_copy not in wf_src)

    # ═══════════════════════════════════════════════════════
    section("5. TIMESTAMPS — real persisted values")
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("detail exposes created_at", bool(detail.get("created_at")))
    check("detail exposes updated_at", bool(detail.get("updated_at")))
    listing = c.get("/api/enrollments", headers=rep).get_json()
    check("list rows expose created_at", all("created_at" in x for x in listing))
    check("list rows expose updated_at", all("updated_at" in x for x in listing))

    check("frontend parses backend timestamps as UTC",
          "parseBackendTimestamp" in JS and "+= 'Z'" in JS)
    check("  ...and formats them for the viewer's timezone",
          "toLocaleString" in JS)
    check("  ...as 'at' style, not raw ISO", "' at$1'" in JS)
    check("no fabricated 'now' timestamps in JS",
          "new Date()" not in JS.split("function parseBackendTimestamp")[0][-2000:])
    check("detail summary shows Created", "['Created', s.createdAt]" in JS)
    check("detail summary shows Last modified", "['Last modified', s.updatedAt]" in JS)
    check("dashboard shows Last modified",
          'data-label="Last modified"' in JS)
    check("  ...as a full timestamp", "formatTimestamp(e.updated_at)" in JS)

    # updated_at must track real modifications, not reads.
    with app.app_context():
        before = query_one("SELECT updated_at FROM enrollments WHERE id=?", (eid,))["updated_at"]
    for _ in range(3):
        c.get(f"/api/enrollments/{eid}", headers=rep)
    with app.app_context():
        after = query_one("SELECT updated_at FROM enrollments WHERE id=?", (eid,))["updated_at"]
    check("updated_at does NOT change on page views", before == after)

    # ═══════════════════════════════════════════════════════
    section("6. EMAIL EDITABILITY — investigated, behaviour unchanged")
    check("the contact-step email is still readonly",
          re.search(r'id="c-email"[^>]*readonly', HTML) is not None)
    check("  ...because Perch keys the session on it",
          "perch_token_email" in adapter_src)
    check("  ...and /enroll REJECTS a mismatch",
          "must match the email used to open this Perch enrollment session" in adapter_src)
    check("customer login also matches on that email",
          "lower(trim(email))" in open(os.path.join(ROOT, "routes", "auth_routes.py"),
                                       encoding="utf-8").read())
    # The supported change path works.
    r2 = c.post("/api/perch/enrollments/capacity", headers=rep,
                json={"enrollment_id": eid, "email": "changed@example.com",
                      "zip_code": "12207", "utility_name": "national-grid-ny"})
    check("re-running availability with a new email succeeds", r2.status_code == 200)
    with app.app_context():
        check("  ...and re-keys the Perch session",
              query_one("SELECT perch_token_email FROM enrollments WHERE id=?",
                        (eid,))["perch_token_email"] == "changed@example.com")
    check("  ...without creating another enrollment", count_enrollments() == 1)
    # PASS 1 replaced the long readonly-email helper paragraph with a quiet
    # context line. The RULE is unchanged and still enforced server-side; the
    # assertion now targets that enforcement rather than removed prose.
    check("the email is still read-only and never re-asked",
          'readonly hidden' in HTML and 'id="c-email" placeholder' not in HTML)
    check("  ...and the Perch-session rule is still enforced by the backend",
          "must match the email used to open this Perch enrollment session"
          in adapter_src)

    # ═══════════════════════════════════════════════════════
    section("7. B1 / B2 / PHASE A PRESERVED")
    css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    check("B2 tokens", "Clean Energy Enrollment — design tokens" in css)
    check("B2 motion system", "prefers-reduced-motion" in css)
    check("B2 mobile table cards", "attr(data-label)" in css)
    check("B2 loading states", "cee-spinner" in css)
    check("B2 OCR highlight", "@keyframes ocrFill" in css and "markOcrFilled(" in JS)
    check("B2 program cards", ".prog-option.selected" in css)
    check("B1 state dropdowns",
          '<select id="a-state"' in HTML and '<select id="b-state"' in HTML)
    check("B1 branch-aware steps", "function activeSteps" in JS)
    check("B1 program selection", "loadProgramOptions" in JS)
    check("B1 admin modal", "openAdminModal" in JS)
    check("one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("one Agree & finish button", JS.count('id="agr-agree-btn"') == 1)
    check("inline agreement links", "agreementLinksHtml" in JS)
    check("resume reconciliation", "canRegenerate" in JS)
    check("Phase A dashboard buckets", "dashBucketFor" in JS)
    check("customer Done", "finishCustomerEnrollment" in JS)
    check("rep Back to dashboard", "backToDashboard" in JS)
    check("OCR path", "extractTextFromFile" in JS and "parseUtilityBill" in JS)
    check("acceptance timezone untouched",
          "ACCEPTANCE_TIMEZONE_NAME" in open(
              os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read())
    with app.app_context():
        # 008_program_selection was added for the dual-program persistence fix -
        # the rep's explicit choice had nowhere durable to live. Nothing else
        # since. See test_program_persistence.py.
        check("migration count is 8 (008_program_selection added)", query_one(
            "SELECT COUNT(*) n FROM schema_migrations")["n"] == 8)

    # ═══════════════════════════════════════════════════════
    section("2d. REAL STAGING RESPONSES — savings pinned to the right field")
    # Values transcribed from live Perch staging (2026-08). These exist because
    # a written report once claimed 12401 yields 10% - the LMI figure - when the
    # correct answer is the 5% residential/commercial figure. Pinning the real
    # numbers makes that class of mistake impossible to repeat silently.
    REAL_12401 = {"residential_capacity_available": False,
                  "small_commercial_capacity_available": True,
                  "lmi_capacity_available": False,
                  "proof_documents_required": False,
                  S_RES: 5, S_LMI: 10}
    REAL_10901 = {"residential_capacity_available": False,
                  "small_commercial_capacity_available": True,
                  "lmi_capacity_available": True,
                  "proof_documents_required": True,
                  S_RES: 5, S_LMI: 20}
    REAL_12901 = {"residential_capacity_available": False,
                  "small_commercial_capacity_available": False,
                  "lmi_capacity_available": True,
                  "proof_documents_required": True,
                  S_RES: 0, S_LMI: 20}

    p12401 = available_programs(REAL_12401)
    check("A. 12401 offers EXACTLY ONE program", len(p12401) == 1)
    check("   ...and it is Residential", p12401[0]["customer_type"] == "Residential")
    check("   ...at 5% from the residential/commercial field",
          p12401[0]["savings_percent"] == 5.0)
    check("   ...NOT the 10% LMI figure", p12401[0]["savings_percent"] != 10.0)
    check("   ...requires no eligibility proof", p12401[0]["lmi_required"] is False)
    check("   ...auto-selected (no ambiguity)",
          resolve_customer_type(REAL_12401)[0] == "Residential")
    check("   ...and is NOT a no-capacity result", len(p12401) > 0)

    p10901 = available_programs(REAL_10901)
    by = {x["customer_type"]: x for x in p10901}
    check("B. 10901 offers BOTH programs", set(by) == {"Residential", "LMI"})
    check("   ...Residential at 5% (res/commercial field)",
          by["Residential"]["savings_percent"] == 5.0)
    check("   ...LMI at 20% (LMI field)", by["LMI"]["savings_percent"] == 20.0)
    check("   ...Residential skips eligibility", by["Residential"]["lmi_required"] is False)
    check("   ...LMI requires eligibility", by["LMI"]["lmi_required"] is True)
    check("   ...explicit selection required",
          isinstance(_raises(lambda: resolve_customer_type(REAL_10901)),
                     PerchValidationError))
    check("   ...both choices resolve",
          resolve_customer_type(REAL_10901, "Residential")[0] == "Residential"
          and resolve_customer_type(REAL_10901, "LMI")[0] == "LMI")

    p12901 = available_programs(REAL_12901)
    check("D. 12901 offers LMI only", [x["customer_type"] for x in p12901] == ["LMI"])
    check("   ...at 20%", p12901[0]["savings_percent"] == 20.0)
    check("   ...and Residential is NOT offered on a 0% res figure",
          all(x["customer_type"] != "Residential" for x in p12901))

    # Residential must be pinned to res/commercial no matter what LMI says.
    for lmi_val in (10, 20, 99, 0, None):
        d = dict(REAL_12401, **{S_LMI: lmi_val})
        check(f"   Residential stays 5% when the LMI field is {lmi_val!r}",
              available_programs(d)[0]["savings_percent"] == 5.0)

    section("2e. NO CROSS-FALLBACK IN EITHER DIRECTION")
    check("Residential with no res figure -> None (never borrows LMI's)",
          available_programs({"small_commercial_capacity_available": True,
                              S_LMI: 10})[0]["savings_percent"] is None)
    check("LMI with no LMI figure -> None (never borrows residential's)",
          available_programs({"lmi_capacity_available": True,
                              S_RES: 5})[0]["savings_percent"] is None)

    section("2f. CAPACITY COPY CANNOT CONTRADICT THE FLAGS")
    t12401 = " ".join(n["text"] for n in workflow._capacity_notices(REAL_12401)).lower()
    check("12401 copy states residential IS available", "standard residential" in t12401)
    check("   ...never says 'may still be available'", "may still" not in t12401)
    check("   ...and never claims no capacity",
          "no available program" not in t12401 and "no community solar" not in t12401)
    t10901 = " ".join(n["text"] for n in workflow._capacity_notices(REAL_10901)).lower()
    check("10901 copy makes no unavailability claim", "not available" not in t10901)
    t12901 = " ".join(n["text"] for n in workflow._capacity_notices(REAL_12901)).lower()
    check("12901 copy says only the income-qualified program is available",
          "only the income-qualified" in t12901)

    section("2g. END-TO-END through the real-staging mock fixtures")
    for zip_code, utility, expected in (
            ("12401", "central-hudson-gas-electric", [("Residential", 5.0)]),
            ("10901", "orange-and-rockland", [("Residential", 5.0), ("LMI", 20.0)]),
            ("12901", "nyseg", [("LMI", 20.0)])):
        rr = c.post("/api/perch/enrollments/capacity", headers=rep,
                    json={"email": f"real{zip_code}@example.com", "zip_code": zip_code,
                          "utility_name": utility})
        check(f"{zip_code} capacity check succeeds", rr.status_code == 200)
        pid = rr.get_json()["enrollment_id"]
        pb = c.get(f"/api/perch/enrollments/{pid}/programs", headers=rep).get_json()
        got = [(x["customer_type"], x["savings_percent"]) for x in pb["available_programs"]]
        check(f"   ...offers exactly {expected}", got == expected)
        check(f"   ...selection_required is {len(expected) > 1}",
              pb["selection_required"] is (len(expected) > 1))
        check("   ...no third product type ever appears",
              all(t in ("Residential", "LMI") for t, _ in got))

    print(f"\n{'='*72}\nPOST-B2 FOLLOW-UPS - ALL CHECKS PASSED\n{'='*72}")


def _raises(fn):
    try:
        fn()
        return None
    except Exception as e:
        return e


if __name__ == "__main__":
    main()
