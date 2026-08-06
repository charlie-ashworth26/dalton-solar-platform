"""
Milestone 2 test suite - the documented Perch contract.

Every assertion here traces to something PUBLISHED (Swagger screenshots) or
stated on the engineering call, not to an assumption. Where a value is our
inference, the test says so.

Run: python3 test/test_perch_milestone2.py
"""
import sys, os, json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PERCH_API_MODE"] = "mock"

from app import app
from db import init_db, query, query_one, execute
import seed


# POST /token requires the customer's email (OpenAPI spec), so every capacity
# call now carries one. Distinct per-suite so mock refresh-by-email is isolated.
TEST_EMAIL = "suite.customer@example.com"


def section(t):
    print(f"\n{'='*74}\n{t}\n{'='*74}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def new_draft(c, h):
    return c.post("/api/perch/drafts", headers=h).get_json()["enrollment_id"]


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")
    admin = login(c, "admin@daltonsolar.com", "AdminPass1!")

    # ───────────────────────────────────────────────────────────
    section("MIGRATION 002 - documented shape replaces the guessed one")
    with app.app_context():
        tables = {r["name"] for r in query("SELECT name FROM sqlite_master WHERE type='table'")}
    check("perch_capacity_checks created", "perch_capacity_checks" in tables)
    check("perch_workflow_state created", "perch_workflow_state" in tables)
    check("perch_utilities created", "perch_utilities" in tables)
    check("perch_products DROPPED (modeled a shape the API does not return)", "perch_products" not in tables)
    with app.app_context():
        cols = {r["name"] for r in query("PRAGMA table_info(enrollments)")}
    check("orphaned selected_perch_product_id column removed", "selected_perch_product_id" not in cols)
    check("enrollments data columns preserved", {"enrollment_code", "service_zip", "utility_name"} <= cols)
    with app.app_context():
        violations = query("PRAGMA foreign_key_check")
    check("no foreign-key violations after the table rebuild", len(violations) == 0)

    from db.migrate import run_migrations
    from db import DB_PATH
    check("migrations are idempotent", run_migrations(DB_PATH, verbose=False) == [])

    # ───────────────────────────────────────────────────────────
    section("UTILITY SLUGS - published slug mapping is reference data")
    r = c.get("/api/perch/utilities", headers=rep)
    check("utilities endpoint 200", r.status_code == 200)
    utils = r.get_json()["utilities"]
    slugs = {u["slug"] for u in utils}
    for s in ("national-grid-ny", "consolidated-edison-ny", "orange-and-rockland",
              "central-hudson-gas-electric", "rochester-gas-electric"):
        check(f"published slug '{s}' present", s in slugs)

    section("POD ID rules - published secondary-identifier table")
    from services.perch import utilities as U
    with app.app_context():
        check("NYSEG requires POD ID, 15 digits, prefix N01",
              U.pod_id_rule("nyseg") == {"required": True, "length": 15, "prefix": "N01",
                                          "description": "15 digits, starting with N01"})
        check("Central Hudson requires 10 digits, no prefix",
              U.pod_id_rule("central-hudson-gas-electric")["length"] == 10)
        check("Rochester G&E requires 15 digits, prefix R01",
              U.pod_id_rule("rochester-gas-electric")["prefix"] == "R01")
        check("National Grid requires no POD ID", U.pod_id_rule("national-grid-ny") is None)
        # The exact scenario Perch walked through on the call.
        err = U.validate_pod_id("nyseg", "N0112345678901")
        check("14-char NYSEG POD ID is rejected before reaching Perch", err is not None)
        check("rejection message is specific about length", "15" in err)
        check("valid NYSEG POD ID passes", U.validate_pod_id("nyseg", "N01123456789012") is None)
        check("wrong prefix rejected", "N01" in (U.validate_pod_id("nyseg", "R01123456789012") or ""))
        # Migration 003: the official spec published 'nyseg', confirming the slug
        # we had inferred in Milestone 2. It is no longer flagged as unconfirmed.
        check("NYSEG slug now CONFIRMED by the official spec",
              not any(u["slug"] == "nyseg" for u in U.unconfirmed_slugs()))
        check("no inferred slugs remain at all", U.unconfirmed_slugs() == [])

    # ───────────────────────────────────────────────────────────
    section("AUTH - X-Enrollment-Token, 30 minutes, PATCH /refresh_token")
    from services.perch.client import TOKEN_TTL_SECONDS, ENROLLMENT_TOKEN_HEADER
    check("TTL is 30 minutes as stated on the call", TOKEN_TTL_SECONDS == 1800)
    check("auth header is X-Enrollment-Token", ENROLLMENT_TOKEN_HEADER == "X-Enrollment-Token")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "perch", "client.py"), encoding="utf-8").read()
    check("no OAuth2 assumptions remain", "/oauth/token" not in src)
    check("no Bearer assumption remains", 'Bearer {' not in src and '"Bearer "' not in src)
    check("documented token path used", 'PATH_TOKEN = "/token"' in src)
    check("documented refresh path used", 'PATH_REFRESH_TOKEN = "/refresh_token"' in src)

    eid = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("capacity call succeeds", r.status_code == 200)
    with app.app_context():
        tok = query_one("SELECT * FROM perch_tokens WHERE enrollment_id = ? AND is_active = 1", (eid,))
    check("an enrollment token was obtained and stored", tok is not None)
    check("token stored as type 'enrollment_token'", tok["token_type"] == "enrollment_token")
    check("token is scoped to the enrollment", tok["enrollment_id"] == eid)
    import uuid as _uuid
    try:
        _uuid.UUID(tok["access_token"]); is_uuid = True
    except ValueError:
        is_uuid = False
    check("token is a UUID, matching the documented example format", is_uuid)

    section("SECURITY - the token never leaves the backend")
    token_value = tok["access_token"]
    check("capacity response does not contain the token", token_value not in r.data.decode())
    r_ts = c.get(f"/api/perch/enrollments/{eid}/token-status", headers=admin)
    check("admin token-status works", r_ts.status_code == 200)
    check("token-status does NOT leak the token value", token_value not in r_ts.data.decode())
    check("token-status reports expiry instead", "expires_at" in r_ts.get_json())
    check("token-status reports the 30-min TTL", r_ts.get_json()["ttl_seconds"] == 1800)
    check("token-status is admin-only",
          c.get(f"/api/perch/enrollments/{eid}/token-status", headers=rep).status_code == 403)
    with app.app_context():
        calls = query("SELECT * FROM perch_api_calls WHERE enrollment_id = ? AND operation='request_token'", (eid,))
    check("token request audit-logged", len(calls) >= 1)
    check("audit log redacts the token", token_value not in (calls[0]["response_json"] or ""))
    check("redaction marker present", "[REDACTED]" in (calls[0]["response_json"] or ""))
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "static", "js", "app.js"), encoding="utf-8").read()
    check("no Perch token handling in frontend JS",
          "enrollment_token" not in js and "X-Enrollment-Token" not in js)

    # ───────────────────────────────────────────────────────────
    section("403 -> PATCH /refresh_token -> retry once (documented recovery)")
    with app.app_context():
        before = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid,))
    # Expire the token AT PERCH ONLY, leaving our local record looking valid.
    # This is the real-world case the documented 403 recovery exists for: clock
    # skew, or Perch invalidating a session early. If we also expired our own
    # record, proactive refresh would catch it first and the reactive 403 path
    # would never actually run - which is what an earlier version of this test
    # accidentally asserted.
    from services.perch.mock_client import PerchMockClient
    PerchMockClient().expire_token(before["access_token"])

    r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("request still succeeds after a 403 from Perch", r.status_code == 200)
    body = r.get_json()
    check("capacity data returned despite the expired token", body["result"]["capacity_available"] is True)
    check("the response reports that a refresh happened", body["result"]["token_was_refreshed"] is True)
    with app.app_context():
        after = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid,))
        old = query_one("SELECT * FROM perch_tokens WHERE id = ?", (before["id"],))
        refresh_calls = query(
            "SELECT * FROM perch_api_calls WHERE enrollment_id=? AND operation='refresh_token'", (eid,))
        logged_403 = query(
            "SELECT * FROM perch_api_calls WHERE enrollment_id=? AND status_code=403", (eid,))
    check("a NEW token is now active", after["access_token"] != before["access_token"])
    check("the expired token was deactivated", old["is_active"] == 0)
    check("PATCH /refresh_token was actually called", len(refresh_calls) >= 1)
    check("refresh endpoint recorded as PATCH", refresh_calls[0]["http_method"] == "PATCH")
    check("the 403 itself was audit-logged", len(logged_403) >= 1)
    check("the 403 log explains the recovery",
          "refreshing and retrying" in (logged_403[0]["error_message"] or ""))
    check("refresh_count incremented on the new token", after["refresh_count"] >= 1)

    section("Proactive refresh means 403s are rare in the first place")
    eid_p = new_draft(c, rep)
    c.post(f"/api/perch/enrollments/{eid_p}/capacity", headers=rep,
           json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"})
    with app.app_context():
        t = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_p,))
        # Age our own record past the skew window; the token is still live at Perch.
        execute("UPDATE perch_tokens SET expires_at=? WHERE id=?",
                ((datetime.now() + timedelta(seconds=30)).isoformat(), t["id"]))
    r = c.post(f"/api/perch/enrollments/{eid_p}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("a near-expiry token is refreshed before the call, not after a 403",
          r.status_code == 200 and r.get_json()["result"]["token_was_refreshed"] is False)
    with app.app_context():
        t2 = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_p,))
    check("and the token was rotated proactively", t2["access_token"] != t["access_token"])

    # ───────────────────────────────────────────────────────────
    section("POST /capacity - documented request and response contract")
    eid2 = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid2}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "10001", "utility_name": "consolidated-edison-ny"})
    res = r.get_json()["result"]
    with app.app_context():
        call = query_one("""SELECT * FROM perch_api_calls WHERE enrollment_id=?
                            AND operation='check_capacity' ORDER BY id DESC LIMIT 1""", (eid2,))
    req = json.loads(call["request_json"])
    check("request uses documented key 'zip_code'", req["zip_code"] == "10001")
    check("request uses documented key 'utility_name' (NOT 'utility')", "utility_name" in req)
    check("utility_name is sent as a SLUG", req["utility_name"] == "consolidated-edison-ny")
    check("endpoint recorded as /capacity", call["endpoint"] == "/capacity")

    pd = res["project_details"]
    documented_fields = {
        "small_commercial_capacity_available", "lmi_capacity_available",
        "residential_capacity_available", "proof_documents_required",
        "savings_percent_for_residential_and_commercial_customers",
        "savings_percent_for_lmi_customers",
    }
    check("project_details has EXACTLY the six documented fields", set(pd.keys()) == documented_fields)
    check("no invented product array", "products" not in res)
    check("no invented available_capacity_kw", "available_capacity_kw" not in json.dumps(res))
    check("no invented product_id", "product_id" not in json.dumps(res))
    check("savings values are the two documented rates",
          pd["savings_percent_for_residential_and_commercial_customers"] == 5
          and pd["savings_percent_for_lmi_customers"] == 25)

    section("next_step is a URL, not an enum")
    check("next_step_url is an absolute URL", res["next_step_url"].startswith("https://"))
    check("next_step_url is the documented /enroll endpoint",
          res["next_step_url"].endswith("/affiliate_partners/v1/enrollments/enroll"))
    from services.perch import workflow as W
    key, recognized = W.resolve_next_step_key(res["next_step_url"])
    check("URL resolves to the 'enroll' step", key == "enroll" and recognized)
    check("resolution is path-suffix based, so a staging host still resolves",
          W.resolve_next_step_key("https://staging.internal/affiliate_partners/v1/enrollments/enroll")[0] == "enroll")
    unknown_key, unknown_ok = W.resolve_next_step_key("https://api.perchenergy.com/v1/something/new")
    check("an UNRECOGNIZED next_step is flagged, not silently ignored",
          unknown_key is None and unknown_ok is False)

    # ───────────────────────────────────────────────────────────
    section("503 - no capacity is a BUSINESS OUTCOME, not an error")
    eid3 = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "99999", "utility_name": "national-grid-ny"})
    check("HTTP 200 to our frontend (not a 5xx bubbled up)", r.status_code == 200)
    b = r.get_json()
    check("capacity_available is False", b["result"]["capacity_available"] is False)
    check("no project_details when there is no capacity", b["result"]["project_details"] is None)
    check("workflow moves to the no_capacity step", b["workflow"]["step"]["key"] == "no_capacity")
    check("rep is told not to proceed",
          "must not be submitted" in json.dumps(b["workflow"]["step"]["panels"]))
    with app.app_context():
        chk = query_one("SELECT * FROM perch_capacity_checks WHERE enrollment_id=?", (eid3,))
        e3 = query_one("SELECT status FROM enrollments WHERE id=?", (eid3,))
    check("the no-capacity result is still persisted for audit", chk is not None)
    check("persisted with capacity_available = 0", chk["capacity_available"] == 0)
    check("enrollment stays in Draft (must not proceed to enroll)", e3["status"] == "Draft")

    section("Genuine upstream failure is still an error")
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "00000", "utility_name": "national-grid-ny"})
    check("real 5xx surfaces as 503 to the client", r.status_code == 503)
    check("distinguished from no-capacity by error type",
          r.get_json()["perch_error"] == "PerchUnavailableError")

    section("Validation happens before we call Perch")
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "123", "utility_name": "national-grid-ny"})
    check("short ZIP rejected with 400", r.status_code == 400)
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "Some Made Up Utility"})
    check("unknown utility rejected with 400", r.status_code == 400)
    check("error explains slugs", "slug" in r.get_json()["error"].lower())
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "National Grid NY"})
    check("a DISPLAY NAME is translated to its slug rather than rejected",
          r.get_json()["result"]["utility_slug"] == "national-grid-ny")

    # ───────────────────────────────────────────────────────────
    section("WORKFLOW ENGINE - the backend decides the step, not the frontend")
    eid4 = new_draft(c, rep)
    wf = c.get(f"/api/perch/enrollments/{eid4}/workflow", headers=rep).get_json()
    check("a fresh draft starts at service_area", wf["step"]["key"] == "service_area")
    field_names = [f["name"] for f in wf["step"]["fields"]]
    # email comes first because POST /token requires it before any other Perch call
    check("descriptor declares the fields to render",
          field_names == ["email", "zip_code", "utility_name"])
    zipf = wf["step"]["fields"][1]
    check("descriptor carries the validation rule", zipf["validation"]["pattern"] == r"^\d{5}$")
    check("descriptor carries the validation message", "5 digits" in zipf["validation"]["message"])
    utilf = wf["step"]["fields"][2]
    check("select options carry SLUGS as values, so no translation is possible client-side",
          all(o["value"] == o["value"].lower().replace(" ", "-") for o in utilf["options"]))
    check("descriptor declares the primary action", wf["step"]["primary_action"]["operation"] == "check_capacity")

    c.post(f"/api/perch/enrollments/{eid4}/capacity", headers=rep,
           json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"})
    wf2 = c.get(f"/api/perch/enrollments/{eid4}/workflow", headers=rep).get_json()
    check("workflow advanced to capacity_result", wf2["step"]["key"] == "capacity_result")
    check("step renders Perch's data as panels", wf2["step"]["panels"][0]["type"] == "capacity_summary")
    check("three capacity segments rendered", len(wf2["step"]["panels"][0]["segments"]) == 3)
    check("savings shown without spurious decimals",
          wf2["step"]["panels"][0]["metrics"][0]["value"] == "10%")
    check("proof-document requirement surfaced as a notice",
          any("proof documents" in n["text"] for n in wf2["step"]["panels"][0]["notices"]))
    check("Perch's next_step exposed to the UI", wf2["step"]["perch_next_step"]["resolved_step"] == "enroll")
    check("continue is disabled because /enroll is Milestone 3",
          wf2["step"]["primary_action"]["enabled"] is False)
    check("and it says why", "Milestone 3" in wf2["step"]["primary_action"]["disabled_reason"])
    with app.app_context():
        st = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id=?", (eid4,))
    check("workflow state persisted", st["current_step_key"] == "capacity_result")
    check("Perch's next_step URL persisted", st["perch_next_step_url"].endswith("/enroll"))
    check("recognized flag persisted", st["next_step_recognized"] == 1)

    section("FRONTEND IS A RENDERER, not a page sequence")
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    check("workflow mount point present", 'id="workflow-root"' in html)
    check("no hardcoded ZIP input in markup", 'id="perch-zip"' not in html)
    check("no hardcoded utility select in markup", 'id="perch-utility"' not in html)
    check("no hardcoded product container in markup", 'perch-products-wrap' not in html)
    check("renderer builds fields from the descriptor", "function renderField" in js)
    check("renderer builds panels from the descriptor", "function renderPanel" in js)
    check("validation is descriptor-driven", "f.validation.pattern" in js)
    check("no product-selection logic remains", "selectPerchProduct" not in js)
    check("no hardcoded savings/capacity strings in JS",
          "savings_percent_for_lmi_customers" not in js and "available_capacity_kw" not in js)

    section("ADAPTER BOUNDARY still holds")
    routes_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "routes", "perch_routes.py"), encoding="utf-8").read()
    check("routes never import a concrete client", "PerchHTTPClient" not in routes_src
          and "PerchMockClient" not in routes_src)
    check("routes never read credentials", "PERCH_API_KEY" not in routes_src)
    check("routes go through the adapter", "from services.perch import adapter" in routes_src)

    section("MODE SWITCH is config-only")
    from services.perch.config import get_perch_client
    os.environ["PERCH_API_MODE"] = "live"
    live = get_perch_client()
    check("live mode selects the HTTP client", type(live).__name__ == "PerchHTTPClient")
    check("live client defaults to the documented enrollment base URL",
          live.enrollment_base_url.endswith("/affiliate_partners/v1/enrollments"))
    check("live client knows the markets base URL",
          live.markets_base_url.endswith("/affiliate_partners/v1/markets"))
    os.environ["PERCH_API_MODE"] = "mock"
    check("mock mode restores the mock client", type(get_perch_client()).__name__ == "PerchMockClient")

    section("GET /markets/capacity - HMAC now implemented on the live client")
    from services.perch.errors import PerchNotImplementedError
    # The published spec supplied the HMAC scheme, so the LIVE client now
    # implements this. The mock does not: pre-enrollment targeting is not part
    # of the enrollment flow and is out of Milestone 2 scope.
    _client_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "services", "perch", "client.py"), encoding="utf-8").read()
    check("live client implements HMAC-signed market capacity",
          "def get_market_capacity" in _client_src and "sign_query_request" in _client_src)
    try:
        get_perch_client().get_market_capacity("13348", "national-grid-ny")
        raised = False
    except PerchNotImplementedError:
        raised = True
    check("mock still declines it (out of Milestone 2 scope)", raised)

    section("DIAGNOSTICS surface what we inferred")
    r = c.get("/api/perch/diagnostics", headers=admin)
    check("diagnostics is admin-only", c.get("/api/perch/diagnostics", headers=rep).status_code == 403)
    diag = r.get_json()
    check("no inferred slugs remain now that the spec is published",
          diag["unconfirmed_utility_slugs"] == [])
    check("known next_step paths are listed", "/enrollments/enroll" in diag["known_next_step_paths"])

    section("DRAFT LIFECYCLE + ACCESS CONTROL (ported from the Milestone 1 suite)")
    d = c.post("/api/perch/drafts", headers=rep)
    check("draft created (201)", d.status_code == 201)
    draft = d.get_json()
    check("immutable Dalton enrollment code issued", draft["enrollment_code"].startswith("ENR-"))
    check("status is Draft", draft["status"] == "Draft")
    check("rep name returned", draft["rep_name"] == "Charlie Mren")
    with app.app_context():
        row = query_one("SELECT * FROM enrollments WHERE id = ?", (draft["enrollment_id"],))
        rrep = query_one("SELECT * FROM sales_reps WHERE id = ?", (row["sales_rep_id"],))
        ruser = query_one("SELECT * FROM users WHERE id = ?", (rrep["user_id"],))
        wstate = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id = ?",
                           (draft["enrollment_id"],))
    check("draft persisted", row is not None)
    check("draft associated with the authenticated rep", ruser["email"] == "charlie@daltonsolar.com")
    check("workflow state initialized at service_area", wstate["current_step_key"] == "service_area")

    qa = login(c, "qa@daltonsolar.com", "QaPass1!")
    check("QA reviewer cannot create a draft (403)",
          c.post("/api/perch/drafts", headers=qa).status_code == 403)
    check("QA reviewer cannot run capacity (403)",
          c.post(f"/api/perch/enrollments/{eid4}/capacity", headers=qa,
                 json={"email": TEST_EMAIL, "zip_code": "13348", "utility_name": "national-grid-ny"}).status_code == 403)
    check("QA reviewer CAN read the Perch audit trail (200)",
          c.get(f"/api/perch/enrollments/{eid4}/api-calls", headers=qa).status_code == 200)
    check("unauthenticated requests are rejected",
          c.get(f"/api/perch/enrollments/{eid4}/workflow").status_code == 401)

    section("RESUME - stored checks are audit records, explicitly flagged stale")
    r = c.get(f"/api/perch/enrollments/{eid4}/capacity", headers=rep)
    check("stored check retrievable", r.status_code == 200)
    stored = r.get_json()
    check("flagged stale so it is never mistaken for live capacity", stored["stale"] is True)
    check("stored check carries the documented project_details shape",
          set(stored["project_details"].keys()) == documented_fields)
    check("a fresh draft has no stored check yet",
          c.get(f"/api/perch/enrollments/{new_draft(c, rep)}/capacity", headers=rep).status_code == 404)

    section("AUDIT SPINE - every Perch call is recorded against the Dalton enrollment ID")
    hist = c.get(f"/api/perch/enrollments/{eid4}/api-calls", headers=rep).get_json()["calls"]
    check("api-call history returned", len(hist) >= 2)
    ops = {h["operation"] for h in hist}
    check("token acquisition recorded", "request_token" in ops)
    check("capacity call recorded", "check_capacity" in ops)
    check("calls carry timing", all(h["duration_ms"] is not None for h in hist if not h["error_message"]))
    check("calls carry api_mode so mocked data is distinguishable in audit",
          all(h["api_mode"] == "mock" for h in hist))

    # ───────────────────────────────────────────────────────────
    section("OPENAPI SPEC ALIGNMENT - HMAC-SHA256 signing")
    from services.perch import hmac_auth as H
    # Reproduces the spec's own worked example: compact JSON, no whitespace.
    check("compact JSON serialization matches the spec sample body",
          H.compact_json({"email": "john.doe@example.com"}) == '{"email":"john.doe@example.com"}')
    # Spec: "key=value pairs joined with &, sorted alphabetically by key"
    check("canonical query string matches the spec example verbatim",
          H.canonical_query_string({"zip_code": "10001", "utility_name": "consolidated-edison-ny"})
          == "utility_name=consolidated-edison-ny&zip_code=10001")
    check("signed payload is timestamp + newline + body",
          H.build_signed_payload("1617187200", "BODY") == "1617187200\nBODY")
    sig = H.compute_signature("YOUR_SECRET_KEY", H.build_signed_payload("1617187200",
                              H.compact_json({"email": "john.doe@example.com"})))
    # Cross-verified against `openssl dgst -sha256 -hmac` using the spec's procedure.
    check("signature is 64-char lowercase hex", len(sig) == 64 and sig == sig.lower())
    check("signature matches the openssl-verified expected value",
          sig == "78c4cc8f5c02760d16b9911ab8ed5db3076096273f5bdcca14e4ec10772db5b5")
    check("signature verification is symmetric",
          H.verify_signature("YOUR_SECRET_KEY",
                             H.build_signed_payload("1617187200",
                                 H.compact_json({"email": "john.doe@example.com"})), sig))
    check("a tampered payload fails verification",
          not H.verify_signature("YOUR_SECRET_KEY",
                                 H.build_signed_payload("1617187200", '{"email":"other@x.com"}'), sig))
    hdrs, body = H.sign_json_request("KEY", "SECRET", {"email": "a@b.com"})
    check("header names match the spec exactly (X-API-Key)", "X-API-Key" in hdrs)
    check("X-HMAC-Signature header present", "X-HMAC-Signature" in hdrs)
    check("X-HMAC-Timestamp header present", "X-HMAC-Timestamp" in hdrs)
    check("Content-Type sent for JSON bodies", hdrs["Content-Type"] == "application/json")
    qhdrs, qs = H.sign_query_request("KEY", "SECRET", {"zip_code": "10001"})
    check("Content-Type NOT sent for GET /markets/capacity (no body)", "Content-Type" not in qhdrs)

    section("OPENAPI SPEC ALIGNMENT - token endpoints require an email")
    client_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "services", "perch", "client.py"), encoding="utf-8").read()
    check("request_token takes an email", "def request_token(self, email: str)" in client_src)
    check("refresh_token takes an email (keyed on email, not the old token)",
          "def refresh_token(self, email: str)" in client_src)
    check("token calls are HMAC-signed", "hmac_auth.sign_json_request" in client_src)
    check("signed bytes are transmitted verbatim (data=, not json=)",
          "data=body.encode" in client_src)
    check("enrollment-session calls send NO HMAC headers", "_session_headers" in client_src)
    check("markets capacity signs the canonical query string",
          "hmac_auth.sign_query_request" in client_src)

    eid_e = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid_e}/capacity", headers=rep,
               json={"utility_name": "national-grid-ny", "zip_code": "13348"})  # deliberately no email
    check("capacity without an email is rejected (POST /token needs it)", r.status_code >= 400)
    r = c.post(f"/api/perch/enrollments/{eid_e}/capacity", headers=rep,
               json={"email": "notanemail", "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("an invalid email is rejected before calling Perch", r.status_code == 400)
    r = c.post(f"/api/perch/enrollments/{eid_e}/capacity", headers=rep,
               json={"email": "john.doe@example.com", "zip_code": "13348",
                     "utility_name": "national-grid-ny"})
    check("capacity succeeds once an email is supplied", r.status_code == 200)
    with app.app_context():
        erow = query_one("SELECT perch_token_email FROM enrollments WHERE id = ?", (eid_e,))
        trow = query_one("SELECT expires_at FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_e,))
    check("email persisted for later refresh", erow["perch_token_email"] == "john.doe@example.com")
    check("token expiry taken from Perch's expires_at, not our clock", trow["expires_at"] is not None)

    section("OPENAPI SPEC ALIGNMENT - published reference data")
    r = c.get("/api/perch/utilities", headers=rep)
    slugs2 = {u["slug"] for u in r.get_json()["utilities"]}
    for s_ in ("national-grid-ny", "consolidated-edison-ny", "orange-and-rockland",
               "central-hudson-gas-electric", "rochester-gas-electric", "pse-g-ny", "nyseg"):
        check(f"spec utility '{s_}' present", s_ in slugs2)
    check("all SEVEN published utilities present", len(slugs2) == 7)
    with app.app_context():
        acct = {u["slug"]: u["account_number_length"] for u in query("SELECT * FROM perch_utilities")}
    check("ConEd account number is 11 digits", acct["consolidated-edison-ny"] == 11)
    check("National Grid account number is 10 digits", acct["national-grid-ny"] == 10)
    check("Rochester G&E account number is 11 digits", acct["rochester-gas-electric"] == 11)
    with app.app_context():
        pdt = {r_["source_type"] for r_ in query("SELECT * FROM perch_proof_doc_types")}
    for t_ in ("proof_doc_snap", "proof_doc_medicaid", "proof_doc_section_8", "proof_doc_ssi",
               "proof_doc_liheap", "proof_doc_lifeline_usac",
               "proof_doc_free_reduced_school_lunch_letter"):
        check(f"proof doc source_type '{t_}' recorded", t_ in pdt)
    check("self-attestation source types recorded (both accept and reject)",
          {"self_attestation_qualifying_income",
           "self_attestation_qualifying_income_rejected"} <= pdt)

    section("OPENAPI SPEC ALIGNMENT - file size limit")
    from helpers import MAX_UPLOAD_BYTES, validate_upload
    check("upload limit is 4 MB (Perch returns 413 above this)",
          MAX_UPLOAD_BYTES == 4 * 1024 * 1024)
    check("an oversized file is rejected locally",
          validate_upload("bill.pdf", 5 * 1024 * 1024) is not None)
    check("a 3 MB file is accepted", validate_upload("bill.pdf", 3 * 1024 * 1024) is None)

    print(f"\n{'='*74}\nMILESTONE 2 + SPEC ALIGNMENT - ALL CHECKS PASSED\n{'='*74}")


if __name__ == "__main__":
    main()
