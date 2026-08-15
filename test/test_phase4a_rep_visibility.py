"""
Phase 4A - rep enrollment visibility and resume.

Proves that:
  * a rep sees only their own enrollments
  * a rep cannot open another rep's enrollment
  * privileged roles see everything, and can narrow with ?mine=true
  * opening an enrollment operates on the EXISTING enrollment_id and creates
    no new Dalton enrollment, no Perch session, no token, and no Perch API call
  * the correct workflow step is restored
  * completed enrollments reopen read-only with no restart path
  * uncertain acceptance reopens blocked

Run: python test/test_phase4a_rep_visibility.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from auth import hash_password
from services.perch import workflow


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


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()

    # A second sales rep, so cross-rep isolation is testable.
    with app.app_context():
        uid = execute(
            "INSERT INTO users (email, password_hash, role, full_name) VALUES (?,?,?,?)",
            ("repb@daltonsolar.com", hash_password("RepBPass1!"), "sales_rep", "Rep Bravo"),
        ).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-002"))

    rep_a = login(c, "charlie@daltonsolar.com", "RepPass1!")
    rep_b = login(c, "repb@daltonsolar.com", "RepBPass1!")
    admin = login(c, "admin@daltonsolar.com", "AdminPass1!")
    qa = login(c, "qa@daltonsolar.com", "QaPass1!")

    a1 = c.post("/api/perch/drafts", headers=rep_a).get_json()["enrollment_id"]
    a2 = c.post("/api/perch/drafts", headers=rep_a).get_json()["enrollment_id"]
    b1 = c.post("/api/perch/drafts", headers=rep_b).get_json()["enrollment_id"]

    section("VISIBILITY - reps see only their own enrollments")
    ids_a = {e["id"] for e in c.get("/api/enrollments", headers=rep_a).get_json()}
    ids_b = {e["id"] for e in c.get("/api/enrollments", headers=rep_b).get_json()}
    check("Rep A sees both of Rep A's enrollments", {a1, a2} <= ids_a)
    check("Rep A does NOT see Rep B's enrollment", b1 not in ids_a)
    check("Rep B sees Rep B's enrollment", b1 in ids_b)
    check("Rep B does NOT see Rep A's enrollments", not ({a1, a2} & ids_b))

    check("Rep A cannot OPEN Rep B's enrollment (403)",
          c.get(f"/api/enrollments/{b1}", headers=rep_a).status_code == 403)
    check("Rep B cannot OPEN Rep A's enrollment (403)",
          c.get(f"/api/enrollments/{a1}", headers=rep_b).status_code == 403)
    check("Rep A CAN open their own enrollment",
          c.get(f"/api/enrollments/{a1}", headers=rep_a).status_code == 200)
    check("unauthenticated listing is rejected",
          c.get("/api/enrollments").status_code == 401)

    section("VISIBILITY - privileged roles")
    ids_admin = {e["id"] for e in c.get("/api/enrollments", headers=admin).get_json()}
    ids_qa = {e["id"] for e in c.get("/api/enrollments", headers=qa).get_json()}
    check("admin sees every rep's enrollments", {a1, a2, b1} <= ids_admin)
    check("QA sees every rep's enrollments", {a1, a2, b1} <= ids_qa)
    check("admin can open any enrollment",
          c.get(f"/api/enrollments/{b1}", headers=admin).status_code == 200)
    # ?mine=true narrows privileged roles; it must not widen a rep's scope.
    mine_admin = {e["id"] for e in c.get("/api/enrollments?mine=true", headers=admin).get_json()}
    check("admin ?mine=true returns only their own (none here)", not ({a1, a2, b1} & mine_admin))
    mine_a = {e["id"] for e in c.get("/api/enrollments?mine=true", headers=rep_a).get_json()}
    check("?mine=true cannot widen a rep's visibility", b1 not in mine_a and {a1, a2} <= mine_a)

    section("STATUS - workflow state is the rep-facing progress")
    e = c.get(f"/api/enrollments/{a1}", headers=rep_a).get_json()
    check("workflow_step_key exposed", e["workflow_step_key"] == "service_area")
    check("human label exposed", e["workflow_step_label"] == "Service area")
    check("terminal flag exposed", e["workflow_is_terminal"] is False)
    check("blocked flag exposed", e["workflow_is_blocked"] is False)
    # enrollments.status must be left alone for QA/reporting compatibility.
    check("legacy enrollments.status still present and unchanged",
          e["status"] in ("Draft", "Information Needed"))

    for key, label, term, blocked in [
        ("capacity_result", "Capacity confirmed", False, False),
        ("proof_docs", "Proof documents needed", False, False),
        ("contracts", "Ready to generate contracts", False, False),
        ("contracts_review", "Contracts awaiting acceptance", False, False),
        ("contracts_accepted", "Complete", True, False),
        ("contracts_accept_uncertain", "Needs review", False, True),
    ]:
        with app.app_context():
            workflow.set_state(a1, key)
        row = c.get(f"/api/enrollments/{a1}", headers=rep_a).get_json()
        check(f"'{key}' -> '{label}' (terminal={term}, blocked={blocked})",
              row["workflow_step_label"] == label
              and row["workflow_is_terminal"] is term
              and row["workflow_is_blocked"] is blocked)

    section("RESUME - opening creates nothing new")
    with app.app_context():
        workflow.set_state(a1, "contracts_review")
        before_enrollments = query_one("SELECT COUNT(*) n FROM enrollments")["n"]
        before_tokens = query_one("SELECT COUNT(*) n FROM perch_tokens")["n"]
        before_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]

    opened = c.get(f"/api/enrollments/{a1}", headers=rep_a)
    check("open returns 200", opened.status_code == 200)
    check("open returns THAT exact enrollment", opened.get_json()["id"] == a1)

    with app.app_context():
        after_enrollments = query_one("SELECT COUNT(*) n FROM enrollments")["n"]
        after_tokens = query_one("SELECT COUNT(*) n FROM perch_tokens")["n"]
        after_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
    check("no duplicate Dalton enrollment created", after_enrollments == before_enrollments)
    check("no new Perch token/session created", after_tokens == before_tokens)
    check("no Perch API call made by opening", after_calls == before_calls)

    section("RESUME - correct step restored, resolver never mis-routes")
    for key in ("proof_docs", "contracts", "contracts_review",
                "contracts_accepted", "contracts_accept_uncertain"):
        with app.app_context():
            workflow.set_state(a1, key)
        wf = c.get(f"/api/perch/enrollments/{a1}/workflow", headers=rep_a).get_json()
        check(f"resolver returns '{key}' rather than falling back to capacity",
              wf["step"]["key"] == key and wf["workflow_state"]["current_step_key"] == key)

    with app.app_context():
        workflow.set_state(a1, "contracts_accepted")
    wf = c.get(f"/api/perch/enrollments/{a1}/workflow", headers=rep_a).get_json()
    check("completed enrollment resolves as terminal", wf["step"]["terminal"] is True)
    check("terminal step exposes NO primary action (no restart path)",
          wf["step"].get("primary_action") is None)

    with app.app_context():
        workflow.set_state(a1, "contracts_accept_uncertain")
    wf = c.get(f"/api/perch/enrollments/{a1}/workflow", headers=rep_a).get_json()
    check("uncertain enrollment resolves as blocked", wf["step"]["blocked"] is True)
    check("blocked step exposes no primary action", wf["step"].get("primary_action") is None)

    section("RESUME - acceptance guardrails still hold on a reopened enrollment")
    r = c.post(f"/api/perch/enrollments/{a1}/contracts/accept", headers=rep_a,
               json={"customer_confirmed": True})
    check("re-accepting an uncertain enrollment is blocked (409)", r.status_code == 409)
    with app.app_context():
        workflow.set_state(a1, "contracts_accepted",
                           last_response={"message": "Contracts accepted successfully"})
    r = c.post(f"/api/perch/enrollments/{a1}/contracts/accept", headers=rep_a,
               json={"customer_confirmed": True})
    check("re-accepting a completed enrollment does not resubmit",
          r.get_json().get("already_accepted") is True)

    section("FRONTEND - Open control replaces the dead legacy path")
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "static", "js", "app.js"), encoding="utf-8").read()
    check("openEnrollment exists", "function openEnrollment(" in js)
    check("rows expose an Open action", "openActionHtml" in js and "openEnrollment(" in js)
    check("rows display the workflow label, not the legacy status",
          "workflowPillHtml" in js)
    check("resume NEVER calls create_draft",
          "openEnrollment" in js and "/api/perch/drafts" not in
          js[js.index("async function openEnrollment("):js.index("function renderResumeBanner(")])
    check("dead resumeCustomer() removed", "function resumeCustomer(" not in js)
    check("dead resumeStepFor() removed", "function resumeStepFor(" not in js)
    check("no remaining reference to resumeCustomer", "resumeCustomer(" not in js)
    check("no remaining reference to resumeStepFor", "resumeStepFor(" not in js)
    check("fake signing magic link is gone from the resume path",
          "sign.daltonsolar.com" not in js)
    check("terminal/blocked enrollments are locked read-only",
          "lockEnrollmentReadOnly" in js)
    check("every persisted workflow key maps to a wizard step",
          all(k in js for k in ("service_area", "proof_docs", "contracts_review",
                                 "contracts_accepted", "contracts_accept_uncertain")))

    section("LABELS - every persisted key is first-class, not a partial string")
    from services.perch.workflow import STEP_LABELS, NEXT_STEP_PATH_MAP
    persisted = set(NEXT_STEP_PATH_MAP.values()) | {
        "service_area", "capacity_result", "no_capacity", "contracts_review",
        "contracts_accepted", "contracts_accept_uncertain", "unknown_next_step"}
    missing = persisted - set(STEP_LABELS)
    check(f"all {len(persisted)} persistable keys have a rep-facing label", not missing)
    for k in sorted(persisted):
        check(f"  '{k}' maps to a wizard step in the frontend", f"{k}:" in js or f"'{k}'" in js)

    print(f"\n{'='*72}\nPHASE 4A - REP VISIBILITY & RESUME - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
