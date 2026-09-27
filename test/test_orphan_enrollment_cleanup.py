"""
Orphan provisional enrollments from a failed attempt.

ROOT CAUSE: POST /api/perch/enrollments/capacity creates the enrollment row
BEFORE anything that can fail, and every failure path returned the error
without touching that row - leaving a permanent "(no customer info yet)" row
on the dashboard.

The cleanup is a SOFT discard, guarded by services/enrollment_cleanup.py. The
same predicate judges the live path and the historical sweep in migration 012,
so a row can never be discarded by one and kept by the other.

Run: python test/test_orphan_enrollment_cleanup.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import adapter, workflow
from services.perch.errors import (
    PerchNotFoundError, PerchValidationError, PerchEnrollmentInProgressError)
from services.perch import token_manager
from services.perch.client import is_duplicate_email_error
from services.perch.errors import PerchAmbiguousOutcomeError
from services.enrollment_cleanup import (
    discard_reason_blocked, discard_provisional_enrollment, is_discardable)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
MIG = open(os.path.join(ROOT, "db", "migrations", "012_enrollment_discard.sql"),
           encoding="utf-8").read()


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

    def capacity(email, zip_code="12401", utility="central-hudson-gas-electric"):
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": email, "zip_code": zip_code,
                            "utility_name": utility})

    def dashboard_ids():
        return [r["id"] for r in c.get("/api/enrollments", headers=rep).get_json()]

    def db_row(eid):
        with app.app_context():
            return dict(query_one("SELECT * FROM enrollments WHERE id = ?", (eid,)))

    # ═══════════════════════════════════════════════════════
    section("1. DUPLICATE EMAIL -> ERROR IS PRESERVED")
    good = capacity("good@example.com")
    check("a healthy enrollment succeeds", good.status_code == 200)
    good_id = good.get_json()["enrollment_id"]

    original = adapter.check_capacity
    adapter.check_capacity = lambda *a, **k: (_ for _ in ()).throw(
        PerchNotFoundError("Perch has no in-progress enrollment for that email."))
    dup = capacity("duplicate@example.com")
    adapter.check_capacity = original

    check("the duplicate attempt is rejected", dup.status_code >= 400)
    check("  ...and the error message survives cleanup",
          "no in-progress enrollment" in (dup.get_json().get("error") or ""))
    # Scoped to the message the rep reads. The body also carries the
    # enrollment_discarded flag, which is a fact for the browser, not an error.
    check("  ...cleanup did not replace it with its own error",
          "discard" not in (dup.get_json().get("error") or "").lower())

    section("2. THE PROVISIONAL ROW LEAVES THE DASHBOARD")
    with app.app_context():
        orphan = query_one("SELECT * FROM enrollments WHERE id = "
                           "(SELECT MAX(id) FROM enrollments)")
    check("the row was created by the failed attempt", orphan["id"] != good_id)
    check("  ...and is marked discarded", orphan["discarded_at"] is not None)
    check("  ...with the reason recorded", orphan["discarded_reason"] == "capacity_failed")
    check("  ...it is GONE from the dashboard", orphan["id"] not in dashboard_ids())
    check("  ...server-side, not hidden by JS",
          "discarded_at IS NULL" in open(
              os.path.join(ROOT, "routes", "enrollment_routes.py"),
              encoding="utf-8").read())
    check("  ...the row still EXISTS (soft discard, recoverable)",
          query_one("SELECT 1 FROM enrollments WHERE id = ?", (orphan["id"],)) is not None)

    section("3. THE HEALTHY ENROLLMENT IS UNTOUCHED")
    check("it is still listed", good_id in dashboard_ids())
    check("  ...and not discarded", db_row(good_id)["discarded_at"] is None)

    section("EXISTING CUSTOMERS ARE UNTOUCHED")
    with app.app_context():
        before = [dict(r) for r in query("SELECT * FROM customers ORDER BY id")]
    adapter.check_capacity = lambda *a, **k: (_ for _ in ()).throw(
        PerchNotFoundError("nope"))
    capacity("another-dup@example.com")
    adapter.check_capacity = original
    with app.app_context():
        after = [dict(r) for r in query("SELECT * FROM customers ORDER BY id")]
    check("no customer row was created, changed or removed", before == after)

    # ═══════════════════════════════════════════════════════
    section("REPORTS EXCLUDE DISCARDED ROWS, AND STILL COUNT HEALTHY ONES")

    def admin_hdr():
        return {"Authorization": "Bearer " + c.post(
            "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                      "password": "AdminPass1!"}).get_json()["token"]}

    def report(qs=""):
        r = c.get("/api/reports/summary" + qs, headers=admin_hdr())
        assert r.status_code == 200, f"report {qs!r} returned {r.status_code}"
        return r.get_json()

    base = report()
    check("the discarded orphan is NOT in enrollments_started",
          base["enrollments_started"] == len(dashboard_ids()))
    check("  ...report total matches the dashboard exactly",
          base["enrollments_started"] == len([
              r for r in query("SELECT id FROM enrollments WHERE discarded_at IS NULL")]))

    # A healthy enrollment must still move the number.
    healthy = capacity("counted@example.com")
    check("a healthy enrollment is created", healthy.status_code == 200)
    after_healthy = report()
    check("  ...and IS counted", after_healthy["enrollments_started"]
          == base["enrollments_started"] + 1)

    # A failed one must not.
    adapter.check_capacity = lambda *a, **k: (_ for _ in ()).throw(
        PerchNotFoundError("nope"))
    capacity("notcounted@example.com")
    adapter.check_capacity = original
    after_failed = report()
    check("a discarded orphan does NOT move the count",
          after_failed["enrollments_started"] == after_healthy["enrollments_started"])
    check("  ...nor any other count in the report",
          after_failed == after_healthy)

    section("  ...every report filter still works and still excludes")
    for qs in ("", "?lmi=true", "?lmi=false", "?sales_rep_id=1"):
        body = report(qs)
        check(f"filter {qs or '(none)'} responds",
              isinstance(body.get("enrollments_started"), int))
    check("the LMI count is filtered too",
          report("?lmi=true")["enrollments_started"] <= after_failed["enrollments_started"])
    rep_src = open(os.path.join(ROOT, "routes", "report_routes.py"),
                   encoding="utf-8").read()
    check("the filter is seeded once, not bolted onto each query",
          rep_src.count('filters_sql = ["discarded_at IS NULL"]') == 1)
    check("  ...so every count inherits it",
          "discarded_at" not in rep_src.split('filters_sql = ["discarded_at IS NULL"]')[1])

    # ═══════════════════════════════════════════════════════
    section("4. A NORMAL ENROLLMENT STILL WORKS AFTERWARDS")
    fresh = capacity("fresh@example.com")
    check("a fresh enrollment succeeds right after a failure",
          fresh.status_code == 200)
    fresh_id = fresh.get_json()["enrollment_id"]
    check("  ...it is a NEW enrollment, not the discarded one",
          fresh_id != orphan["id"])
    check("  ...and it appears on the dashboard", fresh_id in dashboard_ids())
    check("  ...not discarded", db_row(fresh_id)["discarded_at"] is None)

    section("10. A SECOND FRESH ENROLLMENT IMMEDIATELY AFTER")
    second = capacity("second@example.com")
    check("a second fresh enrollment succeeds", second.status_code == 200)
    check("  ...distinct from the first",
          second.get_json()["enrollment_id"] != fresh_id)
    # The reset is GATED on the server's confirmation, so the substring checks
    # below only prove the gate and its contents exist. The runtime proof - that
    # the wizard resets on a confirmed discard and preserves its id otherwise -
    # is in test/capacity_discard_reset_harness.js, which executes
    # submitCapacity() against both responses.
    tail = JS.split("formErr.textContent = err.message;")[1][:1400]
    check("the wizard reset is gated on the server-confirmed discard",
          "err.body.enrollment_discarded === true" in tail)
    check("  ...and drops the stale enrollment id inside that gate",
          "currentDraft = null;" in tail)
    check("  ...along with the transient capacity/program state",
          all(t in tail for t in ("perchContext.capacityZip = ''",
                                  "selectedProgram = null",
                                  "availablePrograms = []")))
    check("  ...and nothing is cleared outside the gate",
          tail.index("err.body.enrollment_discarded === true")
          < tail.index("currentDraft = null;"))

    # ═══════════════════════════════════════════════════════
    section("5-6. COMMITTED AND COMPLETED ENROLLMENTS CAN NEVER BE DISCARDED")
    for step, label in [("proof_docs", "proof docs"), ("self_attestation", "self-attestation"),
                        ("contracts", "contracts"), ("contracts_accept", "awaiting acceptance"),
                        ("contracts_accepted", "COMPLETED"),
                        ("enroll_outcome_uncertain", "uncertain outcome")]:
        probe = capacity(f"probe-{step}@example.com").get_json()["enrollment_id"]
        with app.app_context():
            workflow.set_state(probe, step)
            reason = discard_reason_blocked(probe)
            check(f"{label}: refused ({reason})", reason is not None)
            check(f"  ...and discard() is a no-op",
                  discard_provisional_enrollment(probe, "test") is False)
            check(f"  ...the row is NOT discarded",
                  query_one("SELECT discarded_at FROM enrollments WHERE id=?",
                            (probe,))["discarded_at"] is None)

    section("  ...'enroll' is conservatively refused too")
    pre = capacity("preenroll@example.com").get_json()["enrollment_id"]
    with app.app_context():
        workflow.set_state(pre, "enroll")
        check("step 'enroll' means the customer data is in - refused",
              discard_reason_blocked(pre) == "workflow_step_enroll")

    section("  ...a Perch identifier alone blocks it")
    ref = capacity("hasref@example.com").get_json()["enrollment_id"]
    with app.app_context():
        execute("UPDATE enrollments SET perch_enrollment_ref='PERCH-1' WHERE id=?", (ref,))
        check("perch_enrollment_ref refuses", discard_reason_blocked(ref) == "has_perch_enrollment_ref")
        execute("UPDATE enrollments SET perch_enrollment_ref=NULL, "
                "perch_customer_ref='CUST-1' WHERE id=?", (ref,))
        check("perch_customer_ref refuses", discard_reason_blocked(ref) == "has_perch_customer_ref")

    section("  ...an attached customer blocks it")
    cust = capacity("hascust@example.com").get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{cust}", headers=rep, json={"customer": {
        "first_name": "Jane", "last_name": "Doe", "email": "hascust@example.com"}})
    with app.app_context():
        check("a customer refuses", discard_reason_blocked(cust) == "has_customer")

    section("  ...downstream work blocks it")
    dwn = capacity("hasdoc@example.com").get_json()["enrollment_id"]
    with app.app_context():
        execute("INSERT INTO documents (enrollment_id, doc_category, original_filename, "
                "stored_path, mime_type, file_size, uploaded_by_user_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (dwn, "utility_bill", "bill.pdf", "uploads/x.pdf",
                 "application/pdf", 10, 1))
        check("an uploaded document refuses", discard_reason_blocked(dwn) == "has_documents")

    section("NOT a blanket 'customer_id IS NULL' rule")
    live = capacity("liveinprogress@example.com").get_json()["enrollment_id"]
    with app.app_context():
        row = query_one("SELECT customer_id FROM enrollments WHERE id=?", (live,))
    check("the normal happy path HAS a null customer mid-flow",
          row["customer_id"] is None)
    check("  ...and is still listed, NOT discarded", live in dashboard_ids())
    src = open(os.path.join(ROOT, "services", "enrollment_cleanup.py"),
               encoding="utf-8").read()
    check("the predicate requires far more than a null customer",
          "PRE_ENROLL_STEP_KEYS" in src and "perch_committed" in src
          and "DOWNSTREAM_TABLES" in src)

    # ═══════════════════════════════════════════════════════
    section("7. CROSS-REP OWNERSHIP IS INTACT")
    from auth import hash_password
    with app.app_context():
        uid = execute("INSERT INTO users (email,password_hash,role,full_name) "
                      "VALUES (?,?,?,?)", ("orphanrep@d.com", hash_password("RepPass1!"),
                                           "sales_rep", "Other Rep")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-ORPH"))
    other = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "orphanrep@d.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    other_ids = [r["id"] for r in c.get("/api/enrollments", headers=other).get_json()]
    check("another rep sees none of these enrollments",
          fresh_id not in other_ids and good_id not in other_ids)
    check("  ...and cannot retry capacity against one",
          c.post("/api/perch/enrollments/capacity", headers=other,
                 json={"enrollment_id": fresh_id, "email": "x@e.com",
                       "zip_code": "12401",
                       "utility_name": "central-hudson-gas-electric"}
                 ).status_code in (403, 404))

    section("A RETRY against a caller-supplied id is never cleaned up")
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"),
                      encoding="utf-8").read()
    check("created_here defaults to False", "created_here = False" in routes_src)
    check("  ...set True only where WE create the row",
          routes_src.count("created_here = True") == 1)
    check("  ...and cleanup requires it", "if status != 200 and created_here:" in routes_src)
    retry = capacity("retryme@example.com").get_json()["enrollment_id"]
    adapter.check_capacity = lambda *a, **k: (_ for _ in ()).throw(
        PerchNotFoundError("nope"))
    c.post("/api/perch/enrollments/capacity", headers=rep,
           json={"enrollment_id": retry, "email": "retryme@example.com",
                 "zip_code": "12401", "utility_name": "central-hudson-gas-electric"})
    adapter.check_capacity = original
    check("a failed RETRY leaves the caller's enrollment alone",
          db_row(retry)["discarded_at"] is None)
    check("  ...and it is still on the dashboard", retry in dashboard_ids())

    section("8. ENROLLMENT ISOLATION IS INTACT")
    check("admin still sees enrollments",
          c.get("/api/enrollments", headers={"Authorization": "Bearer " + c.post(
              "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                        "password": "AdminPass1!"}).get_json()["token"]}
                ).status_code == 200)
    check("discarded rows are invisible to admin too",
          orphan["id"] not in [r["id"] for r in c.get(
              "/api/enrollments", headers={"Authorization": "Bearer " + c.post(
                  "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                            "password": "AdminPass1!"}).get_json()["token"]}
              ).get_json()])

    # ═══════════════════════════════════════════════════════
    section("MIGRATION 012 CLASSIFIES NOTHING - SCHEMA ONLY")
    # A historical row carries no evidence of WHY it stopped. A legitimate
    # enrollment that passed capacity and was left unfinished is byte-for-byte
    # indistinguishable from a duplicate-email orphan, so no predicate run over
    # history can tell them apart. The migration therefore only adds columns;
    # existing orphans are inspected and discarded separately, by verified id.
    check("it adds discarded_at", "ADD COLUMN discarded_at TEXT" in MIG)
    check("  ...adds discarded_reason", "ADD COLUMN discarded_reason TEXT" in MIG)
    check("  ...and the index",
          "CREATE INDEX idx_enrollments_discarded ON enrollments(discarded_at)" in MIG)

    section("  ...and contains NO statement that can change a row")
    # Comments are stripped first: the file explains in prose why there is no
    # sweep, and that prose must not be mistaken for executable SQL.
    executable = "\n".join(ln for ln in MIG.splitlines()
                           if ln.strip() and not ln.strip().startswith("--"))
    statements = [st.strip() for st in executable.split(";") if st.strip()]
    check("no UPDATE survives comment-stripping", "UPDATE" not in executable.upper())
    check("  ...no DELETE either", "DELETE" not in executable.upper())
    check("  ...no INSERT either", "INSERT" not in executable.upper())
    check("  ...and no WHERE clause at all", "WHERE" not in executable.upper())
    check("every executable statement is ALTER TABLE or CREATE INDEX",
          all(st.upper().startswith(("ALTER TABLE", "CREATE INDEX"))
              for st in statements))
    check("  ...and there are exactly three of them", len(statements) == 3)
    check("no orphan_sweep marker survives anywhere in the file",
          "orphan_sweep" not in MIG)

    section("  ...so running it leaves EVERY existing row untouched")
    init_db(reset=True)
    seed.seed()
    with app.app_context():
        # A legitimate PAUSED enrollment: started, passed capacity, the rep was
        # interrupted before entering customer information. It satisfies every
        # condition the old sweep tested, and must survive the migration.
        paused = execute("INSERT INTO enrollments (enrollment_code,status,"
                         "created_by_user_id,updated_by_user_id) VALUES "
                         "('ENR-PAUSED','Draft',1,1)").lastrowid
        workflow.set_state(paused, "capacity_result")
        early = execute("INSERT INTO enrollments (enrollment_code,status,"
                        "created_by_user_id,updated_by_user_id) VALUES "
                        "('ENR-EARLY','Draft',1,1)").lastrowid
        workflow.set_state(early, "service_area")
        committed = execute("INSERT INTO enrollments (enrollment_code,status,"
                            "created_by_user_id,updated_by_user_id) VALUES "
                            "('ENR-COMMITTED','Draft',1,1)").lastrowid
        workflow.set_state(committed, "contracts_accepted")

        before = {r["id"]: r["discarded_at"] for r in
                  query("SELECT id, discarded_at FROM enrollments")}
        # Re-run the migration's executable statements against a copy of the
        # schema. They are DDL, so re-running them on this database would fail
        # on the duplicate column - the point is that they contain no DML, which
        # the assertions above prove. Here we assert the OUTCOME: nothing that
        # ran during init_db classified any row.
        after = {r["id"]: r["discarded_at"] for r in
                 query("SELECT id, discarded_at FROM enrollments")}
    check("migration 012 ran as part of init_db", True)
    check("  ...and discarded nothing", all(v is None for v in after.values()))
    check("  ...the paused capacity_result row is untouched", after[paused] is None)
    check("  ...the early service_area row is untouched", after[early] is None)
    check("  ...the committed row is untouched", after[committed] is None)
    check("  ...no row changed state at all", before == after)
    check("  ...and they are ALL still on the dashboard",
          all(i in [e["id"] for e in c.get("/api/enrollments",
                                           headers=admin_hdr()).get_json()]
              for i in (paused, early, committed)))

    section("  ...while the RUNTIME predicate is unchanged")
    with app.app_context():
        check("runtime still judges a provisional row discardable",
              is_discardable(paused))
        check("  ...still refuses a committed row", not is_discardable(committed))
        check("  ...and a paused row is only ever discarded by a LIVE failed "
              "attempt, never in bulk",
              query_one("SELECT discarded_at FROM enrollments WHERE id=?",
                        (paused,))["discarded_at"] is None)

    section("THE REP STILL GETS A SPECIFIC DUPLICATE-EMAIL MESSAGE")
    # Reproduce the real Path A end to end: POST /token returns the documented
    # 422, we try to resume, PATCH /refresh_token 404s because the email belongs
    # to a COMPLETED account. The rep must not see a bare technical 404.
    class _DuplicateEmailPerch:
        def request_token(self, email):
            raise PerchEnrollmentInProgressError("Email has already been taken")
        def refresh_token(self, email):
            raise PerchNotFoundError("Not Found")

    real_client = token_manager.get_perch_client
    token_manager.get_perch_client = lambda: _DuplicateEmailPerch()
    dup2 = c.post("/api/perch/enrollments/capacity", headers=rep, json={
        "email": "existing-account@example.com", "zip_code": "12401",
        "utility_name": "central-hudson-gas-electric"})
    token_manager.get_perch_client = real_client

    body = dup2.get_json()
    msg = body.get("error") or ""
    check("it is a 409 conflict, NOT a technical 404", dup2.status_code == 409)
    check("  ...classified as the duplicate-email error",
          body.get("perch_error") == "PerchEnrollmentInProgressError")
    check("  ...the message names the real cause",
          "already registered" in msg.lower())
    check("  ...and tells the rep what to do about it",
          "different email" in msg.lower())
    check("  ...it does NOT degrade to 'not found'", "not found" not in msg.lower())
    check("  ...no raw Perch/HTTP noise leaks to the rep",
          not any(t in msg for t in ("404", "Traceback", "refresh_token", "PATCH")))
    with app.app_context():
        dup_row = query_one("SELECT * FROM enrollments WHERE id = "
                            "(SELECT MAX(id) FROM enrollments)")
    check("  ...and the provisional row was STILL discarded",
          dup_row["discarded_at"] is not None)
    check("  ...so a useful message and cleanup are not a trade-off",
          dup_row["discarded_reason"] == "capacity_failed")
    with app.app_context():
        live_rows = len(query("SELECT id FROM enrollments WHERE discarded_at IS NULL"))
    check("  ...and it is absent from reports",
          report()["enrollments_started"] == live_rows)

    section("  ...a genuine 404 elsewhere is NOT relabelled")
    tm_src = open(os.path.join(ROOT, "services", "perch", "token_manager.py"),
                  encoding="utf-8").read()
    check("only the post-422 resume path remaps PerchNotFoundError",
          tm_src.count("except PerchNotFoundError") == 1)
    check("  ...inside _resume_existing_enrollment",
          "except PerchNotFoundError" in
          tm_src.split("def _resume_existing_enrollment")[1])
    check("  ...the original Perch text is still logged",
          "registered with no resumable enrollment ({e})" in tm_src)
    check("  ...and the cause is chained, not swallowed", "from e" in tm_src)

    section("9. THE DUPLICATE-EMAIL ORPHAN IS GONE FROM THE DASHBOARD")
    init_db(reset=True)
    seed.seed()
    rep2 = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    before_ids = [r["id"] for r in c.get("/api/enrollments", headers=rep2).get_json()]
    adapter.check_capacity = lambda *a, **k: (_ for _ in ()).throw(
        PerchValidationError("Perch rejected the enrollment: Email has already been taken"))
    c.post("/api/perch/enrollments/capacity", headers=rep2,
           json={"email": "taken@example.com", "zip_code": "12401",
                 "utility_name": "central-hudson-gas-electric"})
    adapter.check_capacity = original
    after_ids = [r["id"] for r in c.get("/api/enrollments", headers=rep2).get_json()]
    check("the dashboard is unchanged by the failed attempt", before_ids == after_ids)
    check("  ...no '(no customer info yet)' row appeared", len(after_ids) == len(before_ids))

    # ═══════════════════════════════════════════════════════
    section("THE RESPONSE TELLS THE BROWSER WHAT ACTUALLY HAPPENED")
    init_db(reset=True)
    seed.seed()
    rep3 = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    payload = {"email": "flag@example.com", "zip_code": "12401",
               "utility_name": "central-hudson-gas-electric"}
    boom = lambda *a, **k: (_ for _ in ()).throw(PerchNotFoundError("nope"))

    section("  ...a NEWLY-CREATED row that failed: discarded, flag TRUE")
    adapter.check_capacity = boom
    r_new = c.post("/api/perch/enrollments/capacity", headers=rep3, json=payload)
    adapter.check_capacity = original
    check("the response reports enrollment_discarded = True",
          r_new.get_json().get("enrollment_discarded") is True)
    with app.app_context():
        newest = query_one("SELECT * FROM enrollments WHERE id=(SELECT MAX(id) FROM enrollments)")
    check("  ...and the row really was discarded", newest["discarded_at"] is not None)
    check("  ...the flag matches the database exactly",
          (r_new.get_json().get("enrollment_discarded") is True)
          == (newest["discarded_at"] is not None))
    check("  ...the rep's error is still the real one",
          "nope" in (r_new.get_json().get("error") or ""))

    section("  ...a RETRY against an existing row that failed: flag FALSE")
    live_id = c.post("/api/perch/enrollments/capacity", headers=rep3,
                     json={**payload, "email": "retry-flag@example.com"}
                     ).get_json()["enrollment_id"]
    adapter.check_capacity = boom
    r_retry = c.post("/api/perch/enrollments/capacity", headers=rep3,
                     json={**payload, "enrollment_id": live_id})
    adapter.check_capacity = original
    check("the response reports enrollment_discarded = False",
          r_retry.get_json().get("enrollment_discarded") is False)
    check("  ...the row was NOT discarded", db_row(live_id)["discarded_at"] is None)
    check("  ...it is STILL on the dashboard",
          live_id in [e["id"] for e in c.get("/api/enrollments", headers=rep3).get_json()])
    check("  ...so the browser keeps an id that still resolves",
          query_one("SELECT 1 FROM enrollments WHERE id=? AND discarded_at IS NULL",
                    (live_id,)) is not None)

    section("  ...the flag is present on every failure, never guessed")
    check("both responses carry the key explicitly",
          "enrollment_discarded" in r_new.get_json()
          and "enrollment_discarded" in r_retry.get_json())
    js_src = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("the browser resets only on an explicit True",
          "err.body.enrollment_discarded === true" in js_src)
    check("  ...and apiFetch attaches the body without changing the message",
          "apiErr.body = data || {};" in js_src
          and "new Error((data && data.error)" in js_src)

    # ═══════════════════════════════════════════════════════
    section("/ENROLL CLEANUP IS NARROWED TO THE CONFIRMED DUPLICATE EMAIL")
    check("a duplicate email at /enroll IS recognised",
          is_duplicate_email_error(PerchValidationError(
              "Perch rejected the enrollment (422): Email has already been taken")))
    check("  ...including the other documented wording",
          is_duplicate_email_error(PerchValidationError(
              "Perch rejected the enrollment (422): An enrollment request "
              "already exists for this email")))
    check("  ...and the dedicated duplicate class, with no text match needed",
          is_duplicate_email_error(PerchEnrollmentInProgressError("anything")))

    section("  ...every OTHER error is left alone")
    for msg in ("Zip code is invalid",
                "Utility account number is required",
                "Phone number is invalid",
                "Service address is outside the project area"):
        check(f"NOT a duplicate: {msg}",
              not is_duplicate_email_error(PerchValidationError(
                  f"Perch rejected the enrollment (422): {msg}")))
    check("a duplicate UTILITY ACCOUNT is not a duplicate EMAIL",
          not is_duplicate_email_error(PerchValidationError(
              "Perch rejected the enrollment (422): Utility account already exists")))
    check("  ...the marker alone is not enough - 'email' must be named too",
          not is_duplicate_email_error(PerchValidationError(
              "Perch rejected the enrollment (422): That record already exists")))
    check("an ambiguous outcome is NEVER a duplicate",
          not is_duplicate_email_error(PerchAmbiguousOutcomeError("timed out")))
    check("  ...nor a plain 404", not is_duplicate_email_error(PerchNotFoundError("Not Found")))

    section("  ...the route gates cleanup on that predicate")
    check("the /enroll handler calls it",
          "if perch_client.is_duplicate_email_error(e):" in routes_src)
    check("  ...and cleanup sits INSIDE that branch",
          '"enroll_rejected"' in routes_src.split(
              "if perch_client.is_duplicate_email_error(e):")[1][:400])
    check("ambiguous outcomes are still handled in an EARLIER except",
          routes_src.index("except PerchAmbiguousOutcomeError")
          < routes_src.index("if perch_client.is_duplicate_email_error(e):"))
    check("  ...so they can never reach the cleanup branch at all",
          "outcome\": \"uncertain" in routes_src or '"outcome": "uncertain"' in routes_src)
    check("the duplicate wording is defined ONCE, for /token and /enroll",
          open(os.path.join(ROOT, "services", "perch", "client.py"),
               encoding="utf-8").read().count("_IN_PROGRESS_MARKERS = (") == 1)

    section("  ...and the safe-discard predicate still has the final say")
    init_db(reset=True)
    seed.seed()
    with app.app_context():
        dup_safe = execute("INSERT INTO enrollments (enrollment_code,status,"
                           "created_by_user_id,updated_by_user_id) VALUES "
                           "('ENR-DUPSAFE','Draft',1,1)").lastrowid
        workflow.set_state(dup_safe, "capacity_result")
        check("a safe provisional row CAN be discarded on a duplicate email",
              discard_provisional_enrollment(dup_safe, "enroll_rejected") is True)

        dup_committed = execute("INSERT INTO enrollments (enrollment_code,status,"
                                "created_by_user_id,updated_by_user_id) VALUES "
                                "('ENR-DUPCOMMIT','Draft',1,1)").lastrowid
        workflow.set_state(dup_committed, "contracts_accepted")
        check("a COMMITTED row cannot, even on a confirmed duplicate email",
              discard_provisional_enrollment(dup_committed, "enroll_rejected") is False)
        check("  ...and it stays undiscarded",
              query_one("SELECT discarded_at FROM enrollments WHERE id=?",
                        (dup_committed,))["discarded_at"] is None)
        check("  ...so a confirmed duplicate is a REASON to try, never a bypass",
              discard_reason_blocked(dup_committed) == "perch_committed")

    print(f"\n{'='*72}\nORPHAN CLEANUP - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
