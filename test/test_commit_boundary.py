"""
Perch commit boundary.

Perch confirmed (2026-08):
  * customer information cannot be updated via the API after the
    customer/utility-account step
  * customer_type cannot change once an enrollment is in progress
  * in-progress enrollments do not expire and are resumed by regenerating the
    token
  * re-calling /contracts returns fresh links to the SAME current enrollment data

So: PRE-/enroll everything is editable; POST-/enroll Perch is authoritative and
Dalton must not permit local edits that would diverge.

Run: python test/test_commit_boundary.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed
from services.perch import workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
PROGRAM_MSG = ("This enrollment has already been submitted and the savings "
               "program can no longer be changed.")


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
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def start(zip_code, utility, tag):
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"{tag}@example.com", "zip_code": zip_code,
                            "utility_name": utility}).get_json()["enrollment_id"]

    def commit(eid, step="contracts"):
        """Advance the workflow exactly as a real /enroll success does."""
        with app.app_context():
            workflow.set_state(eid, step)

    # ═══════════════════════════════════════════════════════
    section("A. PRE-/enroll — everything editable")
    eid = start("10901", "orange-and-rockland", "pre")
    with app.app_context():
        check("not committed before /enroll", workflow.perch_committed(eid) is False)
    check("select LMI",
          c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                 json={"customer_type": "LMI"}).status_code == 200)
    check("  ...switch to Residential",
          c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                 json={"customer_type": "Residential"}).status_code == 200)
    check("  ...and back to LMI",
          c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                 json={"customer_type": "LMI"}).status_code == 200)
    check("customer details editable",
          c.patch(f"/api/enrollments/{eid}", headers=rep,
                  json={"customer": {"first_name": "Jon", "last_name": "Smith",
                                     "email": "pre@example.com"}}).status_code == 200)
    check("address editable",
          c.patch(f"/api/enrollments/{eid}", headers=rep,
                  json={"service_address": {"street": "1 Old St", "city": "Suffern",
                                            "state": "NY", "zip": "10901"}}).status_code == 200)
    check("account editable",
          c.patch(f"/api/enrollments/{eid}", headers=rep,
                  json={"utility_account": {"utility_name": "orange-and-rockland",
                                            "account_number": "1111111111"}}).status_code == 200)
    check("payload reports not committed",
          c.get(f"/api/enrollments/{eid}", headers=rep).get_json()["perch_committed"] is False)

    # ═══════════════════════════════════════════════════════
    section("B. POST-/enroll — program change REJECTED (backend)")
    commit(eid)
    with app.app_context():
        check("committed after the workflow advances", workflow.perch_committed(eid) is True)
    r = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
               json={"customer_type": "Residential"})
    check("program change refused", r.status_code == 409)
    check("  ...with the rep-facing message", r.get_json()["error"] == PROGRAM_MSG)
    check("  ...flagged as committed", r.get_json()["committed"] is True)
    check("  ...no API internals leaked to the rep",
          not any(w in r.get_json()["error"].lower()
                  for w in ("perch", "api", "endpoint", "422", "/enroll")))
    with app.app_context():
        check("  ...and the stored selection is UNCHANGED",
              query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                        (eid,))["selected_customer_type"] == "LMI")
    check("clearing the selection is refused too",
          c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                 json={"customer_type": None}).status_code == 409)

    section("B2. frontend mirrors it — but is NOT the enforcement")
    check("commit state read from the BACKEND payload",
          "e.perch_committed === true" in JS)
    check("  ...into programCommitted", "let programCommitted = false;" in JS)
    check("selectProgram refuses when committed", "if(programCommitted){" in JS)
    check("  ...with the same wording",
          "savings program can no longer be changed." in JS)
    check("cards render disabled", "programCommitted ? ' disabled aria-disabled=\"true\"' : ''" in JS)
    check("  ...and visually settled", "' committed'" in JS)
    check("a clear line explains why",
          "This enrollment has already been submitted. The savings program can no longer be changed." in JS)

    # ═══════════════════════════════════════════════════════
    section("C. COMMITTED FIELDS cannot silently change")
    with app.app_context():
        before = dict(query_one(
            "SELECT c.first_name, c.last_name, c.email, c.phone FROM customers c "
            "JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?", (eid,)))
    for payload, label in [
            ({"customer": {"first_name": "John", "last_name": "Smith",
                           "email": "pre@example.com"}}, "customer name"),
            ({"service_address": {"street": "9 New St", "city": "Suffern",
                                  "state": "NY", "zip": "10901"}}, "service address"),
            ({"utility_account": {"utility_name": "orange-and-rockland",
                                  "account_number": "9999999999"}}, "utility account"),
            ({"billing_address": {"street": "9 New St", "city": "Suffern",
                                  "state": "NY", "zip": "10901"}}, "billing address")]:
        rr = c.patch(f"/api/enrollments/{eid}", headers=rep, json=payload)
        check(f"{label} edit refused", rr.status_code == 409)
        check(f"  ...named in committed_sections",
              bool(rr.get_json().get("committed_sections")))
    with app.app_context():
        after = dict(query_one(
            "SELECT c.first_name, c.last_name, c.email, c.phone FROM customers c "
            "JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?", (eid,)))
    check("committed values are UNCHANGED", before == after)
    check("  ...and NOT wiped or hidden", after["first_name"] == "Jon")
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("  ...still readable on the detail payload", detail["customer"]["first_name"] == "Jon")

    # ═══════════════════════════════════════════════════════
    section("D. REOPENING preserves the committed customer_type")
    for i in range(3):
        d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
        check(f"reopen {i+1}: selection preserved", d["selected_customer_type"] == "LMI")
        check(f"  ...still flagged committed", d["perch_committed"] is True)
    listed = [x for x in c.get("/api/enrollments", headers=rep).get_json()
              if x["id"] == eid][0]
    check("dashboard row carries the flag", listed["perch_committed"] is True)
    check("  ...and the committed program", listed["selected_customer_type"] == "LMI")
    check("savings still follow the committed program",
          detail["program_savings"]["percent"] == 20.0)

    # ═══════════════════════════════════════════════════════
    section("E/F. RESUME, never restart or re-enroll")
    check("resume reconciles through /status first", "/perch-status'" in JS)
    check("  ...and refuses to regenerate once Perch advanced",
          "const canRegenerate = !terminal && !blocked && !statusSaysPastContracts;" in JS)
    check("/enroll is guarded by enrollmentSubmitted",
          "if(!perchContext.enrollmentSubmitted){" in JS)
    check("  ...set from the backend flag on reopen",
          "if(e.perch_committed === true) perchContext.enrollmentSubmitted = true;" in JS)
    check("proof docs are not re-posted after success",
          "if(perchContext.proofSubmitted){" in JS)
    check("no reset/cancel/abandon call was invented",
          not any(w in JS for w in ("/reset", "/cancel", "/abandon", "recreateEnrollment")))
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("  ...nor server-side",
          not any(w in routes_src for w in ("def reset_enrollment", "def cancel_enrollment")))

    # ═══════════════════════════════════════════════════════
    section("G. /contracts recall is not a correction mechanism")
    client_src = open(os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read()
    check("POST /contracts still sends NO body",
          "# No body: the spec documents none." in client_src)
    check("  ...so local edits cannot reach it",
          "def generate_contracts(self, enrollment_token: str)" in client_src)
    check("the adapter passes only the token",
          "client.generate_contracts(token)" in
          open(os.path.join(ROOT, "services", "perch", "adapter.py"), encoding="utf-8").read())

    # ═══════════════════════════════════════════════════════
    section("H. STAGING BEHAVIOUR PRESERVED")
    from routes.enrollment_routes import _program_savings
    for zip_code, utility, ctype, pct in [
            ("10901", "orange-and-rockland", "Residential", 5.0),
            ("10901", "orange-and-rockland", "LMI", 20.0),
            ("12901", "nyseg", "LMI", 20.0)]:
        e2 = start(zip_code, utility, f"h{zip_code}{ctype}")
        check(f"{zip_code} {ctype} selectable pre-enroll",
              c.post(f"/api/perch/enrollments/{e2}/program", headers=rep,
                     json={"customer_type": ctype}).status_code == 200)
        with app.app_context():
            ps = _program_savings(e2)
        check(f"  ...savings {pct}%", ps["percent"] == pct)

    section("BOUNDARY DEFINITION")
    wf_src = open(os.path.join(ROOT, "services", "perch", "workflow.py"), encoding="utf-8").read()
    check("'enroll' is NOT committed (means ready to enroll)",
          '"enroll",' not in wf_src.split("POST_ENROLL_STEP_KEYS = frozenset({")[1].split("})")[0])
    check("uncertain outcome IS treated as committed (conservative)",
          '"enroll_outcome_uncertain",' in wf_src.split("POST_ENROLL_STEP_KEYS")[1][:700])
    check("derived from PERSISTED state, not memory",
          "state = get_state(enrollment_id)" in wf_src.split("def perch_committed")[1])
    pre = start("12901", "nyseg", "boundary")
    with app.app_context():
        for step, expected in [("service_area", False), ("capacity_result", False),
                               ("enroll", False), ("proof_docs", True),
                               ("contracts", True), ("contracts_accepted", True)]:
            workflow.set_state(pre, step)
            check(f"  step '{step}' -> committed={expected}",
                  workflow.perch_committed(pre) is expected)

    section("I. UNIFIED LOGIN UNAFFECTED")
    check("staff signin still works",
          c.post("/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                           "password": "AdminPass1!"}).status_code == 200)
    check("  ...still server-routed",
          c.post("/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                           "password": "RepPass1!"}
                 ).get_json()["account_type"] == "staff")

    section("DEFERRED — NOT IMPLEMENTED")
    check("inviteCode not implemented",
          "inviteCode" not in JS and "invite_code" not in routes_src)
    check("magic links not implemented", "magic" not in JS.lower())
    check("no downstream lifecycle polling invented",
          "setInterval(" not in JS.split("function loadCustomerAgreement")[0][-3000:])

    print(f"\n{'='*72}\nCOMMIT BOUNDARY - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
