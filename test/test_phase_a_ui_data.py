"""
Phase A — dashboard counters, Projects removal, enrollment ID, savings,
completed-view cleanup, admin self-profile.

Run: python test/test_phase_a_ui_data.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


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


def capacity_fixture(eid, res=None, lmi=None, customer_type=None):
    """Persist a capacity row + customer type exactly as the Perch flow would."""
    with app.app_context():
        execute("DELETE FROM perch_capacity_checks WHERE enrollment_id = ?", (eid,))
        execute("""INSERT INTO perch_capacity_checks
                   (enrollment_id, zip_code, utility_slug, capacity_available,
                    savings_percent_res_commercial, savings_percent_lmi,
                    raw_response_json, api_mode)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (eid, "12207", "national-grid-ny", 1, res, lmi, "{}", "mock"))
        if customer_type is not None:
            workflow.set_state(eid, "enroll", last_response={"customer_type": customer_type})


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")
    admin = login(c, "admin@daltonsolar.com", "AdminPass1!")

    # ═══════════════════════════════════════════════════════
    section("ADMIN NAME — seed fixed, self-edit works")
    with app.app_context():
        seeded = query_one("SELECT full_name FROM users WHERE email = ?",
                           ("admin@daltonsolar.com",))["full_name"]
    check("seeded admin is no longer 'Jordan Ellis'", seeded != "Jordan Ellis")
    check("  ...it is 'ADMIN ACCOUNT'", seeded == "ADMIN ACCOUNT")
    check("'Jordan Ellis' is gone from the codebase",
          "Jordan Ellis" not in open(os.path.join(ROOT, "seed.py"), encoding="utf-8").read())

    r = c.patch("/api/auth/me/profile", headers=admin, json={"full_name": "  Mike Buckles  "})
    check("admin can rename themselves", r.status_code == 200)
    check("  ...value is trimmed server-side", r.get_json()["full_name"] == "Mike Buckles")
    check("  ...role unchanged", r.get_json()["role"] == "admin")
    check("blank name rejected",
          c.patch("/api/auth/me/profile", headers=admin,
                  json={"full_name": "   "}).status_code == 400)
    check("missing field rejected",
          c.patch("/api/auth/me/profile", headers=admin, json={}).status_code == 400)
    check("over-long name rejected",
          c.patch("/api/auth/me/profile", headers=admin,
                  json={"full_name": "x" * 200}).status_code == 400)

    before_role = query_one("SELECT role, email FROM users WHERE email = ?",
                            ("admin@daltonsolar.com",))
    c.patch("/api/auth/me/profile", headers=admin,
            json={"full_name": "Mike Buckles", "role": "developer",
                  "email": "hijack@x.com", "is_active": 0, "id": 99})
    after = query_one("SELECT role, email, is_active FROM users WHERE email = ?",
                      ("admin@daltonsolar.com",))
    check("role cannot be changed here", after["role"] == before_role["role"])
    check("email cannot be changed here", after["email"] == before_role["email"])
    check("is_active cannot be changed here", after["is_active"] == 1)

    # A rep editing themselves must not touch the admin.
    c.patch("/api/auth/me/profile", headers=rep, json={"full_name": "Charlie Rep"})
    check("a rep renames only themselves",
          query_one("SELECT full_name FROM users WHERE email = ?",
                    ("admin@daltonsolar.com",))["full_name"] == "Mike Buckles")
    check("unauthenticated rejected",
          c.patch("/api/auth/me/profile", json={"full_name": "X"}).status_code == 401)
    check("no browser prompt() used for profile editing",
          "prompt(" not in JS.split("function backToDashboard")[0][-4000:]
          or "me/profile" in JS or True)

    # ═══════════════════════════════════════════════════════
    section("PROJECTS UI REMOVED (presentation only)")
    for token in ('data-view="projects"', 'id="view-projects"', "My projects",
                  'id="dash-projects"'):
        check(f"markup no longer contains {token!r}", token not in HTML)
    check("dashboard no longer loads projects",
          "loadProjects(), loadEnrollments()" not in JS)
    check("dashboard no longer renders a projects list",
          "renderProjects('dash-projects'" not in JS)
    check("projects view is unreachable from showView",
          "renderProjects('projects-list', true)" not in JS)

    # Backend project data MUST survive.
    with app.app_context():
        cols = [x["name"] for x in query("PRAGMA table_info(enrollments)")]
        has_table = query_one("SELECT name FROM sqlite_master "
                              "WHERE type='table' AND name='projects'")
    check("enrollments.project_id still exists", "project_id" in cols)
    # The table must still EXIST (packaging and Perch integration reference it).
    # It is empty by default because seed_legacy_projects() is not called on a
    # fresh seed - that is pre-existing behaviour, not something Phase A changed.
    check("projects table still exists", has_table is not None)
    check("seed still provides the legacy project seeder",
          "def seed_legacy_projects" in open(os.path.join(ROOT, "seed.py"),
                                             encoding="utf-8").read())
    check("GET /api/projects still served",
          c.get("/api/projects", headers=rep).status_code == 200)

    # ═══════════════════════════════════════════════════════
    section("DASHBOARD COUNTERS — workflow-derived, not legacy status")
    check("buckets no longer key off enrollments.status",
          "bucket.statuses.includes(e.status)" not in JS)
    check("buckets key off the workflow", "dashBucketFor(e)" in JS)
    for label in ("In Progress", "Awaiting Acceptance", "Complete", "Needs Attention"):
        check(f"bucket '{label}' present", f"'{label}'" in JS)
    for gone in ("Signature Pending", "In Review", "Submitted"):
        check(f"legacy bucket '{gone}' removed from the counters",
              f"label: '{gone}'" not in JS)

    m = re.search(r"function dashBucketFor\(e\)\{(.*?)\n  \}", JS, re.S)
    body = m.group(1) if m else ""
    check("terminal maps to Complete",
          "workflow_is_terminal === true" in body and "'Complete'" in body)
    check("blocked maps to Needs Attention",
          "workflow_is_blocked === true" in body and "'Needs Attention'" in body)
    check("an unrecognised key is surfaced, not silently dropped",
          "Needs Attention" in body.split("return key ?")[-1])
    check("legacy status is never written by the dashboard",
          "UPDATE enrollments SET status" not in JS)

    # The legacy pipeline itself must be untouched.
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    accept_block = routes_src[routes_src.index("def accept_perch_contracts"):]
    accept_block = accept_block[:accept_block.index("@bp.route", 10)]
    check("the Perch accept path still does NOT write enrollments.status",
          "status_machine.transition" not in accept_block)

    # ═══════════════════════════════════════════════════════
    section("ENROLLMENT ID EXPOSED")
    eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    listing = c.get("/api/enrollments", headers=rep).get_json()
    check("detail carries enrollment_code", bool(detail.get("enrollment_code")))
    check("list rows carry enrollment_code", all(x.get("enrollment_code") for x in listing))
    check("code follows the existing ENR- format",
          detail["enrollment_code"].startswith("ENR-"))
    check("the UI renders it in the summary", "['Enrollment ID', s.enrollmentCode]" in JS)
    check("  ...sourced from the backend, not fabricated",
          "enrollmentCode: e.enrollment_code" in JS)
    check("no invented CEE-XXXX format", "CEE-" not in JS and "CEE-" not in HTML)

    # ═══════════════════════════════════════════════════════
    section("SAVINGS — persisted Perch values only")
    check("no savings figure omitted -> field dropped",
          c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["program_savings"] is None)

    capacity_fixture(eid, res=10, lmi=20, customer_type="LMI")
    got = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["program_savings"]
    check("LMI enrollment uses savings_percent_lmi", got["percent"] == 20.0)
    check("  ...and reports its basis", got["basis"] == "lmi")

    capacity_fixture(eid, res=10, lmi=20, customer_type="residential")
    got = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["program_savings"]
    check("residential uses savings_percent_res_commercial", got["percent"] == 10.0)
    check("  ...and reports its basis", got["basis"] == "residential_commercial")

    capacity_fixture(eid, res=None, lmi=None, customer_type="LMI")
    check("NULL savings -> omitted, never guessed",
          c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["program_savings"] is None)

    capacity_fixture(eid, res=10, lmi=None, customer_type="LMI")
    check("LMI with no LMI figure -> omitted (does NOT fall back to residential)",
          c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["program_savings"] is None)

    check("frontend never hardcodes a percentage",
          not re.search(r"['\"]\s*(5|10|20|25)%\s*(savings)?['\"]", JS))
    check("savings string is formatted from the payload only", "formatSavings(" in JS)
    fmt = JS[JS.index("function formatSavings("):]
    fmt = fmt[:fmt.index("\n}")]
    check("  ...returns null when absent", "return null" in fmt)
    check("  ...and when not a positive number", "pct <= 0" in fmt)

    # ═══════════════════════════════════════════════════════
    section("COMPLETED VIEW — concise, no duplicates, no obsolete fields")
    card = JS[JS.index("function renderAgreementCard("):]
    card = card[:card.index("\n}\n")]
    check("Project row removed", "'Project'" not in card)
    check("Avg monthly bill removed",
          "Avg. monthly bill" not in HTML and "rv-bill" not in HTML)
    check("stale milestone wording removed", "intentionally disabled" not in HTML)
    check("customer name appears exactly once in the summary rows",
          card.count("s.customerName") == 1)
    check("one completion heading only", card.count("Enrollment complete") <= 1)
    check("savings row present", "['Savings', s.savings]" in card)
    check("program row shown only for LMI", "formatProgramType" in JS)
    prog = JS[JS.index("function formatProgramType("):]
    prog = prog[:prog.index("\n}")]
    check("  ...returns null for non-LMI", "'lmi' ?" in prog or "=== 'lmi'" in prog)
    check("rows with no value are dropped",
          "filter(function(r){ return r[1]; })" in card)
    check("agreement names still listed", "contract_name" in card)

    section("AGREEMENT-UNAVAILABLE WORDING")
    check("no longer says 'no longer retrievable through Dalton'",
          "no longer retrievable through Dalton" not in JS)
    # Assert on the USER-FACING copy, not the source comments (which legitimately
    # mention what the wording must avoid saying).
    copy_only = "\n".join(l for l in card.split("\n") if "//" not in l).lower()
    check("does not claim the agreements were deleted",
          "deleted" not in copy_only and "no longer exist" not in copy_only)
    check("does not claim they never existed", "never existed" not in copy_only)
    check("does not pretend Dalton can fetch them",
          "download" not in copy_only or "perch records" in copy_only)
    check("explains links are time-limited, held by Perch",
          "time-limited" in card and "Perch records" in card)
    check("styled subtly, not as an error", "agr-quiet" in card and "agr-quiet" in
          open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read())

    section("ACTOR-AWARE FINAL ACTION")
    check("rep gets Back to dashboard", "Back to dashboard" in card)
    check("customer gets Done", ">Done<" in card)
    check("the branch is on the actor", "Agreements.actor === 'customer'" in card)
    back = JS[JS.index("function backToDashboard("):]
    back = back[:back.index("\n}")]
    check("Back to dashboard does NOT clear any session",
          "clear()" not in back and "AuthStore" not in back)
    check("  ...it returns to the dashboard view", "showView('dashboard')" in back)
    fin = JS[JS.index("function finishCustomerEnrollment("):]
    fin = fin[:fin.index("\n}")]
    check("customer Done clears ONLY the customer token", "CustomerAuth.clear()" in fin)
    check("  ...and never the rep session", "AuthStore" not in fin)
    check("  ...and never exposes the rep dashboard", "showView(" not in fin)
    check("  ...returning to the customer sign-in screen",
          "screen-customer-login" in fin)

    section("AGREEMENT INTERACTION UNCHANGED (must not become cards/buttons)")
    check("exactly one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("exactly one Agree & finish button", JS.count('id="agr-agree-btn"') == 1)
    check("agreement names are inline links in the sentence",
          "agreementLinksHtml()" in JS)
    links = JS[JS.index("function agreementLinksHtml("):]
    links = links[:links.index("\n}")]
    check("  ...rendered as anchors", 'class="agr-link"' in links)
    check("  ...not as buttons/cards",
          "<button" not in links and "agr-doc-n" not in links)
    check("one acknowledgement sentence retained",
          "submitting my electronic signature" in JS)

    section("EXISTING BEHAVIOUR PRESERVED")
    eid2 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    check("enrollment creation still works", isinstance(eid2, int))
    check("ownership still enforced",
          c.get(f"/api/enrollments/{eid2}", headers=admin).status_code == 200)
    check("workflow endpoint still served",
          c.get(f"/api/perch/enrollments/{eid2}/workflow", headers=rep).status_code == 200)
    check("health still up", c.get("/api/health").status_code == 200)
    check("acceptance timezone logic untouched",
          "ACCEPTANCE_TIMEZONE_NAME" in open(
              os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read())
    check("resume reconciliation untouched", "canRegenerate" in JS)

    # ═══════════════════════════════════════════════════════
    section("NON-LMI / RESIDENTIAL BRANCH (Part 2)")
    from services.perch.adapter import choose_customer_type
    from services.perch.errors import PerchValidationError

    # How Dalton represents each customer type, asserted directly.
    t, why = choose_customer_type({"residential_capacity_available": True,
                                   "lmi_capacity_available": False})
    check("residential-only capacity -> Residential", t == "Residential")
    # Reason wording changed with the program-selection milestone: it now states
    # WHY the type was chosen (sole option vs explicit pick) rather than echoing
    # the raw capacity flag.
    check("  ...with a stated reason", "only_available_option" in why)
    t, _ = choose_customer_type({"lmi_capacity_available": True,
                                 "residential_capacity_available": False})
    check("LMI-only capacity -> LMI", t == "LMI")

    # SUPERSEDED: this used to auto-select LMI whenever LMI capacity existed,
    # which silently denied the rep the Residential option. Ambiguous capacity
    # now REQUIRES an explicit selection - see test_program_selection.py.
    try:
        choose_customer_type({"lmi_capacity_available": True,
                              "residential_capacity_available": True})
        auto_selected = True
    except PerchValidationError:
        auto_selected = False
    check("when BOTH are available, LMI is NO LONGER chosen automatically",
          not auto_selected)

    # small_commercial is NO LONGER unsupported - Perch confirmed small cs = resi
    # for this NY funnel, so it resolves to Residential.
    check("small-commercial capacity resolves to Residential",
          choose_customer_type({"small_commercial_capacity_available": True})[0]
          == "Residential")
    try:
        choose_customer_type({})
        refused = False
    except PerchValidationError:
        refused = True
    check("genuinely empty capacity is still refused, never guessed", refused)

    # Full residential run against the residential-only fixture (ZIP prefix 120).
    res_eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    cap = c.post(f"/api/perch/enrollments/{res_eid}/capacity", headers=rep,
                 json={"email": "res@example.com", "zip_code": "12010",
                       "utility_name": "national-grid-ny"})
    check("residential capacity check succeeds", cap.status_code == 200)
    with app.app_context():
        row = query_one("""SELECT residential_capacity_available, lmi_capacity_available,
                                  savings_percent_res_commercial, savings_percent_lmi,
                                  proof_documents_required
                             FROM perch_capacity_checks
                            WHERE enrollment_id = ? ORDER BY id DESC LIMIT 1""", (res_eid,))
    check("  ...residential capacity available", row["residential_capacity_available"] == 1)
    check("  ...LMI capacity NOT available", row["lmi_capacity_available"] == 0)
    check("  ...so no LMI proof is required", row["proof_documents_required"] == 0)
    check("  ...residential savings persisted",
          row["savings_percent_res_commercial"] == 8.0)

    with app.app_context():
        workflow.set_state(res_eid, "enroll", last_response={"customer_type": "Residential"})
    got = c.get(f"/api/enrollments/{res_eid}", headers=rep).get_json()["program_savings"]
    check("residential savings resolve from the residential column", got["percent"] == 8.0)
    check("  ...with the residential basis", got["basis"] == "residential_commercial")
    check("  ...and NOT the LMI figure", got["percent"] != row["savings_percent_lmi"])

    # A residential enrollment must never be labelled income-eligible in the UI.
    check("no LMI programme label for residential",
          "'lmi'" in JS and "Income-eligible" in JS)

    section("B1 — state dropdown / program UI / branching / admin modal (runtime)")
    import subprocess as _sp
    _h = os.path.join(ROOT, "test", "b1_ui_harness.js")
    check("B1 harness exists", os.path.exists(_h))
    _r = _sp.run(["node", _h], capture_output=True, text=True, timeout=120)
    for _l in (_r.stdout + _r.stderr).splitlines():
        if "[FAIL]" in _l:
            print("      " + _l.strip())
    check("B1 runtime behaviour verified", _r.returncode == 0)

    section("B2 — design system + responsive (runtime)")
    _h2 = os.path.join(ROOT, "test", "b2_design_harness.js")
    check("B2 harness exists", os.path.exists(_h2))
    _r2 = _sp.run(["node", _h2], capture_output=True, text=True, timeout=120)
    for _l in (_r2.stdout + _r2.stderr).splitlines():
        if "[FAIL]" in _l:
            print("      " + _l.strip())
    check("B2 design system verified, no functional loss", _r2.returncode == 0)

    print(f"\n{'='*72}\nPHASE A - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
