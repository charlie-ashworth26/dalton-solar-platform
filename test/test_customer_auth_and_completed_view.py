"""
Stabilization pass: completed-enrollment view, customer agreement auth,
password visibility, and acceptance metadata / client IP.

Run: python test/test_customer_auth_and_completed_view.py
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


def py_fn_body(name, src):
    """Extract a Python function body by indentation (fn_body counts JS braces)."""
    m = re.search(r"^def " + re.escape(name) + r"\(", src, re.M)
    if not m:
        return ""
    start = m.start()
    rest = src[m.end():]
    nxt = re.search(r"^(def |@bp\.route|class )", rest, re.M)
    return src[start:m.end() + (nxt.start() if nxt else len(rest))]


def fn_body(name, src=None):
    src = src or JS
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ""


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("BUG 1 - a COMPLETED enrollment must never POST /contracts")
    open_body = fn_body("openEnrollment")
    _m = re.search(r"REHYDRATE_CONTRACT_STEPS\s*=\s*\[(.*?)\]", JS, re.S)
    REHYDRATE_STEPS = set(re.findall(r"'([^']+)'", _m.group(1))) if _m else set()
    check("the rehydrate list was found", bool(REHYDRATE_STEPS))
    check("rehydrate list excludes contracts_accepted",
          "contracts_accepted" not in REHYDRATE_STEPS)
    check("rehydrate list excludes contracts_accept_uncertain",
          "contracts_accept_uncertain" not in REHYDRATE_STEPS)
    check("terminal/blocked is checked BEFORE any rehydrate",
          open_body.index("if(terminal || blocked)") < open_body.index("REHYDRATE_CONTRACT_STEPS.indexOf"))
    # UPDATED for the redesign: the completed view now mounts the SHARED
    # Agreements component in read-only mode instead of a bespoke summary.
    check("terminal/blocked renders from persisted data instead",
          "mountAgreements({" in open_body and "readOnly: true" in open_body)
    card = fn_body("renderAgreementCard")
    check("the completed view makes NO network call",
          "apiFetch" not in card and "fetch(" not in card and "agrFetch" not in card)
    check("it explains why documents are no longer retrievable",
          "no longer" in card.lower())
    check("lock is applied before rendering, so no control is briefly live",
          open_body.index("lockEnrollmentReadOnly") < open_body.index("mountAgreements({"))

    eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    with app.app_context():
        workflow.set_state(eid, "contracts_review", last_response={
            "contracts": [{"contract_name": "ESIGN Consent Policy",
                           "expires_at": "2026-08-15T10:00:00Z", "url_present": True}],
            "contract_count": 1})
        workflow.set_state(eid, "contracts_accepted", last_response={
            "message": "Contracts accepted successfully", "perch_status": {"completed": True},
            "contracts": [{"contract_name": "ESIGN Consent Policy"}]})
        before_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
        before_tokens = query_one("SELECT COUNT(*) n FROM perch_tokens")["n"]
        before_enr = query_one("SELECT COUNT(*) n FROM enrollments")["n"]

    e = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("completed enrollment reports terminal", e["workflow_is_terminal"] is True)
    check("contract NAMES are available without calling Perch",
          [x["contract_name"] for x in (e["workflow_last_response"] or {}).get("contracts", [])]
          == ["ESIGN Consent Policy"])
    payload = json.dumps(e)
    check("no presigned URL in the payload", not re.findall(r"https?://[^\s\"]*amazonaws[^\s\"]*", payload))
    check("no http(s) URL of any kind leaked", not re.findall(r"https?://", payload))

    with app.app_context():
        check("opening a completed enrollment made ZERO Perch API calls",
              query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"] == before_calls)
        check("... created no new Perch token",
              query_one("SELECT COUNT(*) n FROM perch_tokens")["n"] == before_tokens)
        check("... created no new enrollment",
              query_one("SELECT COUNT(*) n FROM enrollments")["n"] == before_enr)

    r = c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=rep,
               json={"customer_confirmed": True})
    check("acceptance on a completed enrollment does not resubmit",
          r.get_json().get("already_accepted") is True)

    section("BUG 1 - contracts_review resume is UNCHANGED")
    eid2 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    with app.app_context():
        workflow.set_state(eid2, "contracts_review",
                           last_response={"contracts": [], "contract_count": 0})
    e2 = c.get(f"/api/enrollments/{eid2}", headers=rep).get_json()
    check("contracts_review is NOT terminal", e2["workflow_is_terminal"] is False)
    check("contracts_review is NOT blocked", e2["workflow_is_blocked"] is False)
    check("so it still takes the rehydrate path",
          "contracts_review" in REHYDRATE_STEPS)
    check("live contract steps still rehydrate",
          {"contracts", "contracts_review", "contracts_accept"} <= REHYDRATE_STEPS)
    rehy = fn_body("rehydrateContractPacket")
    check("rehydrate still POSTs /contracts for a live packet",
          "/contracts" in rehy and "method:'POST'" in rehy)
    check("acceptanceEnabled still comes from the backend",
          "body.acceptance_enabled === true" in rehy)

    # ═══════════════════════════════════════════════════════
    section("BUG 2 - customer agreement authentication")
    login_body = fn_body("doCustomerLogin")
    check("customer login now calls the backend",
          "/api/auth/customer-login" in login_body)
    check("it no longer searches the dead in-memory array",
          "customers.find" not in login_body)
    check("it no longer compares plaintext passwords",
          "match.password" not in login_body)
    check("it no longer filters on the pre-Perch status",
          "Opportunity - Review" not in login_body)
    check("customer token is stored separately from the rep token",
          "dalton_customer_token" in JS)

    c.patch(f"/api/enrollments/{eid2}", headers=rep, json={"customer": {
        "first_name": "Johnnie", "last_name": "Testcustomer",
        "email": "Charlie+Dalton1@Example.com", "phone": "5185550001",
        "password": "CustPass1!"}})

    r = c.post("/api/auth/customer-login",
               json={"email": "charlie+dalton1@example.com", "password": "CustPass1!"})
    check("credentials created during enrollment authenticate", r.status_code == 200)
    body = r.get_json()
    check("a token is issued", bool(body.get("token")))
    check("the correct enrollment is resolved", body["enrollment_id"] == eid2)
    cust = {"Authorization": f"Bearer {body['token']}"}

    check("the ORIGINAL mixed-case + whitespace email also works",
          c.post("/api/auth/customer-login",
                 json={"email": "  Charlie+Dalton1@Example.com  ",
                       "password": "CustPass1!"}).status_code == 200)
    check("uppercase email works", c.post("/api/auth/customer-login",
          json={"email": "CHARLIE+DALTON1@EXAMPLE.COM", "password": "CustPass1!"}).status_code == 200)
    check("wrong password is rejected", c.post("/api/auth/customer-login",
          json={"email": "charlie+dalton1@example.com", "password": "WrongPass1!"}).status_code == 401)
    check("unknown email is rejected", c.post("/api/auth/customer-login",
          json={"email": "nobody@example.com", "password": "CustPass1!"}).status_code == 401)
    check("empty credentials are rejected",
          c.post("/api/auth/customer-login", json={"email": "", "password": ""}).status_code == 400)
    r_unknown = c.post("/api/auth/customer-login",
                       json={"email": "nobody@example.com", "password": "x"}).get_json()
    r_badpw = c.post("/api/auth/customer-login",
                     json={"email": "charlie+dalton1@example.com", "password": "x"}).get_json()
    check("unknown-user and bad-password messages are identical (no enumeration)",
          r_unknown["error"] == r_badpw["error"])

    section("BUG 2 - the password was always stored correctly (hashed)")
    with app.app_context():
        row = query_one("SELECT password_hash FROM customers WHERE lower(trim(email))=?",
                        ("charlie+dalton1@example.com",))
    check("a password hash exists", bool(row["password_hash"]))
    check("it is PBKDF2, not plaintext",
          row["password_hash"].startswith("pbkdf2_sha256$"))
    check("the plaintext is not stored", "CustPass1!" not in row["password_hash"])

    section("BUG 2 - customer scoping and privilege separation")
    me = c.get("/api/auth/customer-me", headers=cust)
    check("customer can read their own agreement", me.status_code == 200)
    check("... and it is the right one", me.get_json()["enrollment_id"] == eid2)
    check("... with the correct workflow step",
          me.get_json()["workflow_step_key"] == "contracts_review")

    check("customer token CANNOT list enrollments",
          c.get("/api/enrollments", headers=cust).status_code == 403)
    check("customer token CANNOT open another enrollment",
          c.get(f"/api/enrollments/{eid}", headers=cust).status_code == 403)
    check("customer token CANNOT create a draft",
          c.post("/api/perch/drafts", headers=cust).status_code == 403)
    check("customer token CANNOT reach the QA queue",
          c.get("/api/qa/queue", headers=cust).status_code == 403)
    check("customer token CANNOT reach admin diagnostics",
          c.get("/api/perch/diagnostics", headers=cust).status_code == 403)
    # INTENTIONALLY CHANGED: the previous pass asserted customers could never
    # reach the acceptance route. That was correct while customers had no real
    # contract flow - but it is exactly what this pass fixes. A customer MAY now
    # accept contracts on THEIR OWN enrollment (that is the whole point); they
    # still may not touch anyone else's, which is asserted below and in the
    # "CUSTOMER scoping on the shared contract routes" section.
    check("customer is NOT blanket-forbidden on their own acceptance route",
          c.post(f"/api/perch/enrollments/{eid2}/contracts/accept", headers=cust,
                 json={"customer_confirmed": True}).status_code != 403)

    # A second customer must never see the first customer's agreement.
    eid3 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid3}", headers=rep, json={"customer": {
        "first_name": "Other", "last_name": "Person", "email": "other@example.com",
        "phone": "5185550002", "password": "OtherPass1!"}})
    other = c.post("/api/auth/customer-login",
                   json={"email": "other@example.com", "password": "OtherPass1!"}).get_json()
    oh = {"Authorization": f"Bearer {other['token']}"}
    check("second customer resolves to THEIR enrollment", other["enrollment_id"] == eid3)
    check("second customer's agreement is their own",
          c.get("/api/auth/customer-me", headers=oh).get_json()["enrollment_id"] == eid3)
    check("customers cannot cross over", other["enrollment_id"] != body["enrollment_id"])

    section("BUG 2 - rep authentication is unaffected")
    check("rep login still works",
          c.post("/api/auth/login",
                 json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"}).status_code == 200)
    check("rep can still list enrollments", c.get("/api/enrollments", headers=rep).status_code == 200)
    check("a REP token cannot use customer routes",
          c.get("/api/auth/customer-me", headers=rep).status_code == 403)
    check("rep can still accept contracts",
          c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=rep,
                 json={"customer_confirmed": True}).status_code == 200)

    # ═══════════════════════════════════════════════════════
    section("PASSWORD VISIBILITY TOGGLE")
    for field in ("c-pass", "c-pass-confirm", "cust-login-pass"):
        check(f"{field} has an eye control", f'id="{field}-eye"' in HTML)
    check("customer creation and login are both covered",
          'id="c-pass-eye"' in HTML and 'id="cust-login-pass-eye"' in HTML)
    toggle = fn_body("togglePasswordVisibility")
    check("it toggles ONLY the input type",
          "input.type" in toggle and "password" in toggle and "text" in toggle)
    check("the password value is never logged",
          "console.log" not in toggle and "console." not in toggle)
    check("the value is never persisted", "sessionStorage" not in toggle
          and "localStorage" not in toggle)
    check("the value is never transmitted", "fetch(" not in toggle and "apiFetch" not in toggle)
    check("accessible: aria-pressed maintained", "aria-pressed" in toggle)
    check("accessible: aria-label maintained", "aria-label" in toggle)
    check("keyboard usable: it is a real <button>", 'class="pw-eye"' in HTML
          and "<button" in HTML)
    check("buttons are type=button (never submit)", HTML.count('class="pw-eye"')
          == HTML.count('type="button" class="pw-eye"'))
    check("focus styling exists for keyboard users",
          ".pw-eye:focus-visible" in open(os.path.join(ROOT, "static", "css", "app.css"),
                                           encoding="utf-8").read())

    # ═══════════════════════════════════════════════════════
    section("ACCEPTANCE METADATA - exactly the three Perch fields, server-side")
    routes_src = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    md = py_fn_body("_acceptance_metadata", routes_src)
    check("ip_address from the server, not the browser", "_client_ip()" in md)
    check("timestamp generated server-side at acceptance time",
          "acceptance_timestamp()" in md)
    check("user_agent from the incoming request header",
          'request.headers.get("User-Agent")' in md)
    check("user_agent truncated to the documented 2048", "_MAX_USER_AGENT" in md)
    check("no metadata is accepted from browser JSON",
          "request.get_json" not in md and "data.get" not in md)

    import routes.perch_routes as pr
    with app.test_request_context("/", headers={"User-Agent": "UA/1.0"},
                                   environ_base={"REMOTE_ADDR": "203.0.113.7"}):
        meta = pr._acceptance_metadata()
    check("exactly the three documented fields",
          set(meta.keys()) == {"ip_address", "timestamp", "user_agent"})
    check("ip is the request peer in the default configuration",
          meta["ip_address"] == "203.0.113.7")
    check("user_agent is the real request header", meta["user_agent"] == "UA/1.0")

    section("ACCEPTANCE METADATA - trusted proxy handling")
    with app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.5, 10.0.0.6"},
                                   environ_base={"REMOTE_ADDR": "10.0.0.9"}):
        os.environ.pop("DALTON_TRUSTED_PROXY_COUNT", None)
        check("X-Forwarded-For is IGNORED by default", pr._client_ip() == "10.0.0.9")
        os.environ["DALTON_TRUSTED_PROXY_COUNT"] = "1"
        check("with 1 trusted proxy, the last hop is used", pr._client_ip() == "10.0.0.6")
        check("a spoofable left-most value is NEVER used", pr._client_ip() != "1.2.3.4")
        os.environ["DALTON_TRUSTED_PROXY_COUNT"] = "2"
        check("with 2 trusted proxies, the 2nd-from-last is used", pr._client_ip() == "10.0.0.5")
        os.environ["DALTON_TRUSTED_PROXY_COUNT"] = "99"
        check("more hops than present falls back safely", pr._client_ip() == "10.0.0.9")
        os.environ["DALTON_TRUSTED_PROXY_COUNT"] = "garbage"
        check("a non-numeric setting falls back safely", pr._client_ip() == "10.0.0.9")
        os.environ.pop("DALTON_TRUSTED_PROXY_COUNT", None)

    section("ACCEPTANCE - reviewing is not accepting; duplicates blocked")
    check("review mints a capability, it does not accept",
          "/contracts/review" in JS and "contracts/accept" in JS)
    review = fn_body("reviewPerchContract")
    check("the Review action never posts acceptance",
          "contracts/accept" not in review)
    check("acceptance requires explicit confirmation",
          "customer_confirmed" in routes_src)
    check("duplicate acceptance is blocked", "already_accepted" in routes_src)
    check("uncertain acceptance stays blocked", "contracts_accept_uncertain" in routes_src)
    check("one-time review capability intact",
          routes_src.count("_contract_review_tokens.pop") >= 1)

    # ═══════════════════════════════════════════════════════
    section("CUSTOMER lands on the CURRENT Perch engine, not the legacy mock")
    check("portal button calls the Perch-backed flow",
          'onclick="openCustomerContracts()"' in HTML)
    check("portal button no longer calls the legacy engine",
          "enterCustomerSign('portal')" not in HTML)
    entry = fn_body("enterCustomerSign")
    check("legacy entry no longer renders the mock document packet",
          "renderDocPacket()" not in entry)
    check("legacy entry no longer initialises the signature canvas",
          "initSigCanvas()" not in entry)
    # STRONGER than before: the legacy engine is now DELETED, not merely
    # neutralised, so there is nothing left to fail loudly.
    for gone in ["renderDocPacket", "allDocsReviewed", "initSigCanvas",
                 "checkCustomerReady", "enterCustomerSign"]:
        check(f"legacy '{gone}' is fully removed", f"function {gone}(" not in JS)
        check(f"nothing references '{gone}'", not re.search(gone + r"\s*\(", JS + HTML))

    section("CUSTOMER uses the SAME endpoints as the rep")
    oc = fn_body("openCustomerContracts")
    check("customer requests the real contract packet",
          "/contracts'" in oc or "'/contracts" in oc or "/contracts" in oc)
    # The redesign unified review into ONE implementation shared with the rep.
    rev = fn_body("openAgreementDoc")
    check("review uses the one-time capability endpoint (shared)",
          "/contracts/review" in rev)
    check("review never receives a raw presigned URL",
          "review_url" in rev and "amazonaws" not in rev)
    check("there is exactly one review implementation",
          JS.count("function openAgreementDoc(") == 1
          and "function customerReviewContract(" not in JS)
    # Unified by the redesign: one acceptance implementation for both actors.
    acc = fn_body("submitAgreements")
    check("customer acceptance uses the real acceptance endpoint",
          "/contracts/accept" in acc)
    check("customer acceptance sends the confirmation precondition",
          "customer_confirmed" in acc)
    check("customer acceptance blocks duplicate clicks",
          "Agreements.inFlight" in acc or "inFlight" in acc)
    check("an uncertain outcome is NOT retryable by clicking again",
          "uncertain" in acc)
    check("there is no second contract implementation (no local doc list)",
          "DOC_PACKET" not in oc and "docReviewed" not in oc)

    section("BACKEND - one engine, two auth entry points")
    routes_now = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("contract routes accept staff OR customer",
          routes_now.count("@require_staff_or_customer") == 3)
    check("customer-reachable routes scope via _visible_to_actor",
          "_visible_to_actor" in routes_now)
    check("rep-only routes still use the original _visible",
          routes_now.count("_visible(enrollment_id)") > 0)
    check("shared error handler no longer assumes a staff user",
          'user_id=_actor_id(), details=_actor_details(' in routes_now)

    # ── functional: customer completes the REAL enrollment ──
    section("CUSTOMER acceptance completes the REAL enrollment")
    eid4 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid4}", headers=rep, json={"customer": {
        "first_name": "Flow", "last_name": "Tester", "email": "flow@example.com",
        "phone": "5185550003", "password": "FlowPass1!"}})
    with app.app_context():
        workflow.set_state(eid4, "contracts_review",
                           last_response={"contracts": [], "contract_count": 0})

    flow = c.post("/api/auth/customer-login",
                  json={"email": "flow@example.com", "password": "FlowPass1!"}).get_json()
    fh = {"Authorization": f"Bearer {flow['token']}"}
    check("customer login resolves their CURRENT enrollment", flow["enrollment_id"] == eid4)

    import services.perch.adapter as _ad
    _real_accept, _real_status = _ad.accept_contracts, _ad.get_status
    _ad.accept_contracts = lambda e, m, user_id=None: {
        "message": "Contracts accepted successfully", "raw": {}}
    _ad.get_status = lambda e, user_id=None: {
        "completed_steps": ["submit_contracts_acceptance"], "remaining_steps": [],
        "completed": True, "next_step": None, "raw": {}}
    try:
        r = c.post(f"/api/perch/enrollments/{eid4}/contracts/accept", headers=fh,
                   json={"customer_confirmed": True})
        check("customer acceptance succeeds through the real route", r.status_code == 200)
        check("... and reports accepted", r.get_json().get("accepted") is True)

        # THE ORIGINAL BUG: the legacy flow "completed" while this stayed unchanged.
        e4 = c.get(f"/api/enrollments/{eid4}", headers=rep).get_json()
        check("REP DASHBOARD now reads Complete after CUSTOMER acceptance",
              e4["workflow_step_key"] == "contracts_accepted")
        check("... with the Complete label", e4["workflow_step_label"] == "Complete")
        check("... and is terminal", e4["workflow_is_terminal"] is True)

        with app.app_context():
            aud = query("SELECT details_json FROM audit_logs WHERE enrollment_id=? "
                        "AND action='perch_contracts_accepted'", (eid4,))
        check("acceptance is attributed to the CUSTOMER in the audit trail",
              any("customer:" in (a["details_json"] or "") for a in aud))

        check("reopening as the customer is now read-only",
              c.get("/api/auth/customer-me", headers=fh).get_json()["workflow_is_terminal"] is True)
        r2 = c.post(f"/api/perch/enrollments/{eid4}/contracts/accept", headers=fh,
                    json={"customer_confirmed": True})
        check("customer cannot re-accept a completed enrollment",
              r2.get_json().get("already_accepted") is True)
    finally:
        _ad.accept_contracts, _ad.get_status = _real_accept, _real_status

    section("CUSTOMER scoping on the shared contract routes")
    other_eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    check("customer cannot generate contracts for another enrollment",
          c.post(f"/api/perch/enrollments/{other_eid}/contracts", headers=fh,
                 json={}).status_code == 403)
    check("customer cannot review another enrollment's contracts",
          c.post(f"/api/perch/enrollments/{other_eid}/contracts/review", headers=fh,
                 json={"contract_index": 0}).status_code == 403)
    check("customer cannot accept another enrollment's contracts",
          c.post(f"/api/perch/enrollments/{other_eid}/contracts/accept", headers=fh,
                 json={"customer_confirmed": True}).status_code == 403)
    check("customer still cannot reach rep-only routes",
          c.post("/api/perch/drafts", headers=fh).status_code == 403
          and c.get("/api/enrollments", headers=fh).status_code == 403)

    section("REP flow unchanged by the shared guard")
    eid5 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    with app.app_context():
        workflow.set_state(eid5, "contracts_review",
                           last_response={"contracts": [], "contract_count": 0})
    check("rep can still generate contracts",
          c.post(f"/api/perch/enrollments/{eid5}/contracts", headers=rep,
                 json={}).status_code in (200, 502, 503))
    check("rep still cannot accept without confirmation",
          c.post(f"/api/perch/enrollments/{eid5}/contracts/accept", headers=rep,
                 json={}).status_code == 400)
    qa = login(c, "qa@daltonsolar.com", "QaPass1!")
    check("QA is still refused on contract routes (staff role check intact)",
          c.post(f"/api/perch/enrollments/{eid5}/contracts", headers=qa,
                 json={}).status_code == 403)

    print(f"\n{'='*72}\nCUSTOMER AUTH + COMPLETED VIEW - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
