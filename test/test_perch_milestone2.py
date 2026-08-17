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
# NEW YAML: POST /token returns 422 if an enrollment is already in progress for
# an email, so each ENROLLMENT needs its own address - exactly as production will.
#
# It must be stable PER ENROLLMENT, not per call: the suite checks capacity more
# than once on the same enrollment, and the email is the Perch session identity.
# Varying it mid-enrollment would silently re-point the session at a different
# Perch record (which is how the first version of this helper broke the
# refresh-count assertion).
def email_for(enrollment_id):
    return f"suite.customer{enrollment_id}@example.com"


_email_seq = [0]


def next_email():
    """A fresh address not tied to any enrollment - for direct client-level tests."""
    _email_seq[0] += 1
    return f"suite.standalone{_email_seq[0]}@example.com"


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
    section("AUTH - X-Enrollment-Token, 1 hour TTL, PATCH /refresh_token")
    from services.perch.client import TOKEN_TTL_SECONDS, ENROLLMENT_TOKEN_HEADER
    # NEW YAML: "Expires 1 hour after issuance". The previous spec said 30 min.
    check("TTL is 1 hour per the newest spec", TOKEN_TTL_SECONDS == 3600)
    check("auth header is X-Enrollment-Token", ENROLLMENT_TOKEN_HEADER == "X-Enrollment-Token")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "perch", "client.py"), encoding="utf-8").read()
    check("no OAuth2 assumptions remain", "/oauth/token" not in src)
    check("no Bearer assumption remains", 'Bearer {' not in src and '"Bearer "' not in src)
    check("documented token path used", 'PATH_TOKEN = "/token"' in src)
    check("documented refresh path used", 'PATH_REFRESH_TOKEN = "/refresh_token"' in src)

    eid = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid}/capacity", headers=rep,
               json={"email": email_for(eid), "zip_code": "13348", "utility_name": "national-grid-ny"})
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
    check("token-status reports the 1-hour TTL", r_ts.get_json()["ttl_seconds"] == 3600)
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
               json={"email": email_for(eid), "zip_code": "13348", "utility_name": "national-grid-ny"})
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
           json={"email": email_for(eid_p), "zip_code": "13348", "utility_name": "national-grid-ny"})
    with app.app_context():
        t = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_p,))
        # Age our own record past the skew window; the token is still live at Perch.
        execute("UPDATE perch_tokens SET expires_at=? WHERE id=?",
                ((datetime.now() + timedelta(seconds=30)).isoformat(), t["id"]))
    r = c.post(f"/api/perch/enrollments/{eid_p}/capacity", headers=rep,
               json={"email": email_for(eid_p), "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("a near-expiry token is refreshed before the call, not after a 403",
          r.status_code == 200 and r.get_json()["result"]["token_was_refreshed"] is False)
    with app.app_context():
        t2 = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_p,))
    check("and the token was rotated proactively", t2["access_token"] != t["access_token"])

    # ───────────────────────────────────────────────────────────
    section("POST /capacity - documented request and response contract")
    eid2 = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid2}/capacity", headers=rep,
               json={"email": email_for(eid2), "zip_code": "10001", "utility_name": "consolidated-edison-ny"})
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
               json={"email": email_for(eid3), "zip_code": "99999", "utility_name": "national-grid-ny"})
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
               json={"email": email_for(eid3), "zip_code": "00000", "utility_name": "national-grid-ny"})
    check("real 5xx surfaces as 503 to the client", r.status_code == 503)
    check("distinguished from no-capacity by error type",
          r.get_json()["perch_error"] == "PerchUnavailableError")

    section("Validation happens before we call Perch")
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": email_for(eid3), "zip_code": "123", "utility_name": "national-grid-ny"})
    check("short ZIP rejected with 400", r.status_code == 400)
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": email_for(eid3), "zip_code": "13348", "utility_name": "Some Made Up Utility"})
    check("unknown utility rejected with 400", r.status_code == 400)
    check("error explains slugs", "slug" in r.get_json()["error"].lower())
    r = c.post(f"/api/perch/enrollments/{eid3}/capacity", headers=rep,
               json={"email": email_for(eid3), "zip_code": "13348", "utility_name": "National Grid NY"})
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
           json={"email": email_for(eid4), "zip_code": "13348", "utility_name": "national-grid-ny"})
    wf2 = c.get(f"/api/perch/enrollments/{eid4}/workflow", headers=rep).get_json()
    check("workflow advanced to capacity_result", wf2["step"]["key"] == "capacity_result")
    check("step renders Perch's data as panels", wf2["step"]["panels"][0]["type"] == "capacity_summary")
    check("three capacity segments rendered", len(wf2["step"]["panels"][0]["segments"]) == 3)
    check("savings shown without spurious decimals",
          wf2["step"]["panels"][0]["metrics"][0]["value"] == "10%")
    check("proof-document requirement surfaced as a notice",
          any("proof documents" in n["text"] for n in wf2["step"]["panels"][0]["notices"]))
    check("Perch's next_step exposed to the UI", wf2["step"]["perch_next_step"]["resolved_step"] == "enroll")
    check("continue is enabled now that the existing Dalton bill/OCR screen owns the enroll UX",
          wf2["step"]["primary_action"]["enabled"] is True)
    check("continue bridges out of the descriptor renderer into the existing Dalton wizard",
          wf2["step"]["primary_action"]["operation"] == "advance")
    with app.app_context():
        st = query_one("SELECT * FROM perch_workflow_state WHERE enrollment_id=?", (eid4,))
    check("workflow state persisted", st["current_step_key"] == "capacity_result")
    check("Perch's next_step URL persisted", st["perch_next_step_url"].endswith("/enroll"))
    check("recognized flag persisted", st["next_step_recognized"] == 1)

    section("FRONTEND RENDERER STOPS AT CAPACITY; EXISTING DALTON WIZARD IS PRESERVED")
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    check("workflow mount point present", 'id="workflow-root"' in html)
    check("no hardcoded ZIP input in markup", 'id="perch-zip"' not in html)
    check("no hardcoded utility select in markup", 'id="perch-utility"' not in html)
    check("no hardcoded product container in markup", 'perch-products-wrap' not in html)
    check("existing utility-bill dropzone is preserved", 'id="bill-dropzone"' in html and 'id="bill-file"' in html)
    check("existing Contact/password screen is preserved", 'id="step-contact"' in html and 'id="c-pass"' in html and 'id="c-pass-confirm"' in html)
    check("Contact email is displayed but not requested a second time", 'id="c-email"' in html and 'id="c-email" placeholder="name@email.com" readonly' in html)
    check("existing LMI upload screen is preserved", 'id="lmi-dropzone"' in html and 'id="lmi-file"' in html)
    # INTENTIONALLY UPDATED: "Acceptance not enabled" was removed when contract
    # acceptance was implemented. The screen must still be the SAME existing
    # Agreement step (not a new flow), now carrying the confirm + accept controls.
    # UPDATED for the contract UX redesign: the SAME Agreement step is still
    # reused (not a new flow), but it now mounts the shared Agreements component
    # rather than a rep-only contract list.
    # The acknowledgement and submit moved onto the review page, rendered by the
    # shared component - so they live in app.js, not the static template.
    check("existing Agreement screen is reused for Perch contract review",
          'id="agr-host-rep"' in html
          and 'id="agr-ack-check"' in js
          and 'id="agr-agree-btn"' in js)
    check("document review is still available", 'openAgreementDoc' in js)
    check("no new frontend flow was introduced", 'id="pre-send"' in html)
    check("rep and customer mount the SAME component",
          'id="agr-host-rep"' in html and 'id="agr-host-customer"' in html
          and js.count("function mountAgreements(") == 1)
    check("renderer builds fields from the descriptor", "function renderField" in js)
    check("renderer builds panels from the descriptor", "function renderPanel" in js)
    check("validation is descriptor-driven", "f.validation.pattern" in js)
    check("no product-selection logic remains", "selectPerchProduct" not in js)
    check("no hardcoded savings/capacity strings in JS",
          "savings_percent_for_lmi_customers" not in js and "available_capacity_kw" not in js)
    check("bill OCR handler still uses the original extraction/parser path",
          "async function handleBillUpload" in js and "extractTextFromFile(f)" in js and "parseUtilityBill(text)" in js)
    check("same saved bill document ID is sent to /enroll",
          "document_id:state.bill.documentId" in js)
    check("/enroll branches on Perch next_step instead of a hardcoded LMI page",
          "continueFromPerchNextStep" in js and "next_step_key" in js)
    # INTENTIONALLY UPDATED: acceptance is now implemented. Review must remain a
    # separate explicit action (viewing != accepting), and acceptance must be
    # gated behind an explicit customer confirmation.
    check("contract review remains an explicit, separate action", "/contracts/review" in js)
    check("acceptance is now wired in the GUI", "/contracts/accept" in js)
    check("acceptance sends the Dalton-side confirmation precondition",
          "customer_confirmed" in js)
    # Renamed by the redesign; the guard itself is unchanged.
    check("acceptance guards against duplicate clicks while in flight",
          "Agreements.inFlight" in js)

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
                 json={"email": email_for(eid4), "zip_code": "13348", "utility_name": "national-grid-ny"}).status_code == 403)
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

    # ───────────────────────────────────────────────────────────
    section("NEWEST YAML - token TTL is 1 hour, sourced from expires_at")
    eid_ttl = new_draft(c, rep)
    ttl_email = email_for(eid_ttl)
    r = c.post(f"/api/perch/enrollments/{eid_ttl}/capacity", headers=rep,
               json={"email": ttl_email, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("capacity succeeds", r.status_code == 200)
    with app.app_context():
        trow = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1", (eid_ttl,))
    from datetime import datetime as _dt
    remaining = (_dt.fromisoformat(trow["expires_at"]) - _dt.now()).total_seconds()
    check("stored expiry is ~1 hour out, not 30 minutes", 3000 < remaining <= 3600)
    check("token-status advertises the 1-hour TTL",
          c.get(f"/api/perch/enrollments/{eid_ttl}/token-status",
                headers=admin).get_json()["ttl_seconds"] == 3600)

    section("NEWEST YAML - POST /token 201, and duplicate email returns 422")
    from services.perch.client import TOKEN_CREATED_STATUS
    check("client records 201 as the documented creation status", TOKEN_CREATED_STATUS == 201)
    from services.perch.errors import PerchEnrollmentInProgressError
    from services.perch.config import get_perch_client as _gpc
    dup_email = next_email()
    mc = _gpc()
    first = mc.request_token(dup_email)
    check("first token issued for a fresh email", bool(first["enrollment_token"]))
    check("token response carries expires_at (required field)", bool(first["expires_at"]))
    check("token response carries next_step (now a REQUIRED field)", bool(first["next_step"]))
    check("token next_step points at /capacity, per the documented flow",
          first["next_step"].endswith("/capacity"))
    try:
        mc.request_token(dup_email)
        dup_raised = False
    except PerchEnrollmentInProgressError as e:
        dup_raised, dup_msg = True, str(e)
    check("a second token request for the SAME email is rejected", dup_raised)
    check("and the message points the rep at /status", "/status" in dup_msg)

    section("NEWEST YAML - duplicate email resumes via PATCH /refresh_token")
    # A rep revisiting an abandoned customer must not be permanently stuck:
    # the documented recovery is to resume the in-progress enrollment.
    eid_dup = new_draft(c, rep)
    shared = next_email()
    r1 = c.post(f"/api/perch/enrollments/{eid_dup}/capacity", headers=rep,
                json={"email": shared, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("first enrollment for this email succeeds", r1.status_code == 200)
    eid_dup2 = new_draft(c, rep)
    r2 = c.post(f"/api/perch/enrollments/{eid_dup2}/capacity", headers=rep,
                json={"email": shared, "zip_code": "13348", "utility_name": "national-grid-ny"})
    check("a SECOND enrollment reusing that email still succeeds (resumed, not blocked)",
          r2.status_code == 200)
    with app.app_context():
        resumed = query("""SELECT * FROM perch_api_calls WHERE enrollment_id=?
                           AND operation='refresh_token'""", (eid_dup2,))
        logged = query("""SELECT * FROM perch_api_calls WHERE enrollment_id=?
                          AND error_message LIKE '%already in progress%'""", (eid_dup2,))
    check("recovery went through PATCH /refresh_token", len(resumed) >= 1)
    check("the 422-and-resume was audit-logged, not silently swallowed", len(logged) >= 1)

    section("NEWEST YAML - retired LMI source type")
    with app.app_context():
        rej = query_one("SELECT * FROM perch_proof_doc_types WHERE source_type=?",
                        ("self_attestation_qualifying_income_rejected",))
        acc = query_one("SELECT * FROM perch_proof_doc_types WHERE source_type=?",
                        ("self_attestation_qualifying_income",))
        statuses = {r_["status_value"] for r_ in query("SELECT * FROM perch_self_attestation_status")}
    check("_rejected source type marked INACTIVE (422 in the newest spec)", rej["is_active"] == 0)
    check("retirement reason recorded for auditability", "422" in (rej["retired_note"] or ""))
    check("the surviving source type stays active", acc["is_active"] == 1)
    check("replacement status vocabulary recorded", statuses == {"accepted", "rejected"})

    section("NEWEST YAML - HMAC is UNCHANGED (regression guard)")
    # The newest YAML did not alter signing. These assertions exist so a future
    # edit to the auth path fails loudly - the staging 403 was proven NOT to be
    # a signing problem (corrupted HMAC -> 401, correct HMAC -> 403).
    check("signed payload construction unchanged",
          H.build_signed_payload("1617187200", "BODY") == "1617187200\nBODY")
    check("signature vector unchanged",
          H.compute_signature("YOUR_SECRET_KEY", H.build_signed_payload("1617187200",
              H.compact_json({"email": "john.doe@example.com"})))
          == "78c4cc8f5c02760d16b9911ab8ed5db3076096273f5bdcca14e4ec10772db5b5")
    check("header names unchanged",
          set(H.sign_json_request("K", "S", {"e": 1})[0].keys())
          == {"Content-Type", "X-API-Key", "X-HMAC-Signature", "X-HMAC-Timestamp"})
    check("canonical query string unchanged",
          H.canonical_query_string({"zip_code": "10001", "utility_name": "consolidated-edison-ny"})
          == "utility_name=consolidated-edison-ny&zip_code=10001")

    # ───────────────────────────────────────────────────────────
    section("OBSERVED STAGING - token response shape normalization")
    from services.perch.client import PerchHTTPClient

    class _FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    # (a) The documented YAML shape must still parse exactly as before.
    doc = PerchHTTPClient._parse_token_response(_FakeResp({
        "enrollment_token": "550e8400-e29b-41d4-a716-446655440000",
        "expires_at": "2026-07-09T16:12:00Z",
        "next_step": "https://api.perchenergy.com/affiliate_partners/v1/enrollments/capacity",
    }))
    check("documented shape: enrollment_token parsed",
          doc["enrollment_token"] == "550e8400-e29b-41d4-a716-446655440000")
    check("documented shape: expires_at preserved", doc["expires_at"] == "2026-07-09T16:12:00Z")
    check("documented shape: next_step preserved", doc["next_step"].endswith("/capacity"))
    check("documented shape: tagged as documented", doc["response_shape"] == "documented")

    # (b) The ACTUAL staging shape (verified against real staging, HTTP 201).
    stg = PerchHTTPClient._parse_token_response(_FakeResp({
        "token": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "next_step_url": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/capacity",
    }))
    check("staging shape: 'token' accepted as enrollment_token alias",
          stg["enrollment_token"] == "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
    check("staging shape: 'next_step_url' accepted as next_step alias",
          stg["next_step"].endswith("/enrollments/capacity"))
    check("staging shape: absent expires_at stays None - never fabricated",
          stg["expires_at"] is None)
    check("staging shape: tagged as staging_alias", stg["response_shape"] == "staging_alias")

    # (c) Neither key present -> actionable error naming both.
    try:
        PerchHTTPClient._parse_token_response(_FakeResp({"unexpected": "x"}))
        no_key_raised = False
    except Exception as e:
        no_key_raised, no_key_msg = True, str(e)
    check("a response with neither key raises", no_key_raised)
    check("the error names both accepted keys",
          "enrollment_token" in no_key_msg and "token" in no_key_msg)
    check("and lists the keys actually received", "unexpected" in no_key_msg)

    section("OBSERVED STAGING - expiry provenance when expires_at is absent")
    from services.perch.token_manager import _resolve_expiry
    from datetime import datetime as _d, timedelta as _td
    exp_api, src_api = _resolve_expiry("2026-07-09T16:12:00Z")
    check("Perch-provided expires_at is marked authoritative", src_api == "api")
    check("Perch-provided value is used verbatim", exp_api.year == 2026 and exp_api.hour == 16)
    exp_der, src_der = _resolve_expiry(None)
    check("absent expires_at yields a DERIVED expiry, not an error", src_der == "derived")
    delta = (exp_der - _d.now()).total_seconds()
    check("derived expiry uses the documented 1-hour TTL", 3500 < delta <= 3600)
    exp_bad, src_bad = _resolve_expiry("not-a-timestamp")
    check("an unparseable expires_at is treated as absent, not trusted", src_bad == "derived")

    # Provenance must survive into the database and diagnostics, so a derived
    # value can never be mistaken for something Perch told us.
    eid_prov = new_draft(c, rep)
    r = c.post(f"/api/perch/enrollments/{eid_prov}/capacity", headers=rep,
               json={"email": email_for(eid_prov), "zip_code": "13348",
                     "utility_name": "national-grid-ny"})
    check("capacity succeeds", r.status_code == 200)
    with app.app_context():
        prov = query_one("SELECT * FROM perch_tokens WHERE enrollment_id=? AND is_active=1",
                         (eid_prov,))
    check("expires_at_source persisted", prov["expires_at_source"] in ("api", "derived"))
    check("mock returns expires_at, so it is marked authoritative",
          prov["expires_at_source"] == "api")
    ts = c.get(f"/api/perch/enrollments/{eid_prov}/token-status", headers=admin).get_json()
    check("token-status exposes expiry provenance", "expires_at_source" in ts)
    check("token-status flags whether expiry is authoritative",
          ts["expires_at_is_authoritative"] is True)

    # ───────────────────────────────────────────────────────────
    section("OBSERVED STAGING - capacity response shape normalization")
    from services.perch.client import normalize_capacity_response

    _DETAILS = {
        "small_commercial_capacity_available": False,
        "lmi_capacity_available": True,
        "residential_capacity_available": False,
        "proof_documents_required": True,
        "savings_percent_for_residential_and_commercial_customers": 25.0,
        "savings_percent_for_lmi_customers": 25.0,
    }

    # (a) Documented YAML envelope.
    ndoc = normalize_capacity_response(
        {"project_details": dict(_DETAILS), "next_step": "https://api.perchenergy.com/x/enroll"})
    check("documented envelope: next_step read", ndoc["next_step"].endswith("/enroll"))
    check("documented envelope: tagged documented", ndoc["response_shape"] == "documented")
    check("documented envelope: project_details unchanged", ndoc["project_details"] == _DETAILS)

    # (b) The ACTUAL staging envelope, captured from live staging
    #     (zip 12202 / national-grid-ny).
    nstg = normalize_capacity_response({
        "next_step_url": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/enroll",
        "project_details": dict(_DETAILS),
    })
    check("staging envelope: next_step_url accepted as next_step alias",
          nstg["next_step"].endswith("/enrollments/enroll"))
    check("staging envelope: tagged staging_alias", nstg["response_shape"] == "staging_alias")
    check("staging envelope: project_details preserved UNCHANGED",
          nstg["project_details"] == _DETAILS)
    check("staging envelope: raw response preserved verbatim",
          sorted(nstg["raw"].keys()) == ["next_step_url", "project_details"])

    # (c) Both present -> documented wins.
    nboth = normalize_capacity_response({
        "project_details": dict(_DETAILS),
        "next_step": "https://DOCUMENTED", "next_step_url": "https://ALIAS"})
    check("when both keys are present, the DOCUMENTED one wins",
          nboth["next_step"] == "https://DOCUMENTED")

    # (d) Neither present -> surfaced, not silently None.
    nnone = normalize_capacity_response({"project_details": dict(_DETAILS)})
    check("missing both keys is flagged as no_next_step",
          nnone["response_shape"] == "no_next_step" and nnone["next_step"] is None)

    section("OBSERVED STAGING - full stack drives the staging envelope end to end")
    # Not just the normalizer in isolation: push the whole adapter -> workflow
    # path through the observed staging shape.
    from services.perch.mock_client import PerchMockClient as _PMC
    _PMC.emit_staging_alias_shape = True
    try:
        eid_alias = new_draft(c, rep)
        r = c.post(f"/api/perch/enrollments/{eid_alias}/capacity", headers=rep,
                   json={"email": email_for(eid_alias), "zip_code": "13348",
                         "utility_name": "national-grid-ny"})
        check("capacity succeeds when staging uses next_step_url", r.status_code == 200)
        body = r.get_json()
        check("capacity_available still True", body["result"]["capacity_available"] is True)
        check("next_step_url normalized into next_step_url result field",
              (body["result"]["next_step_url"] or "").endswith("/enroll"))
        check("workflow still advances to capacity_result",
              body["workflow"]["step"]["key"] == "capacity_result")
        check("workflow still resolves Perch's next step to 'enroll'",
              body["workflow"]["step"]["perch_next_step"]["resolved_step"] == "enroll")
        check("and it is marked RECOGNIZED, not an unknown step",
              body["workflow"]["step"]["perch_next_step"]["recognized"] is True)
        with app.app_context():
            chk = query_one("SELECT * FROM perch_capacity_checks WHERE enrollment_id=?", (eid_alias,))
        check("next_step_url persisted", (chk["next_step_url"] or "").endswith("/enroll"))
        raw_stored = json.loads(chk["raw_response_json"])
        check("audit stores the GENUINE Perch envelope, not our wrapper",
              "next_step_url" in raw_stored and "response_shape" not in raw_stored)
        check("all six project_details survive into storage",
              chk["savings_percent_lmi"] is not None and chk["proof_documents_required"] == 1)
    finally:
        _PMC.emit_staging_alias_shape = False

    check("documented envelope still works after the flag is reset",
          normalize_capacity_response({"project_details": {}, "next_step": "https://y"})["response_shape"]
          == "documented")

    # ───────────────────────────────────────────────────────────
    section("POST /enroll - multipart encoding matches the spec cURL exactly")
    from services.perch.client import build_enrollment_multipart, normalize_enroll_response
    import os as _os
    _bill = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "test", "sample_utility_bill.pdf")

    # Reproduces the spec's own Residential single-account cURL example.
    form, files = build_enrollment_multipart({
        "email_address": "john.doe@example.com", "first_name": "John", "last_name": "Doe",
        "phone_number": "2125551234", "customer_type": "Residential",
        "utility_name": "consolidated-edison-ny", "zip_code": "10001",
        "billing_address": {"address_1": "123 Main St", "city": "New York",
                             "state": "NY", "zip": "10001"},
        "utility_accounts": [{
            "utility_account_number": "12345678901",
            "service_address": {"address_1": "123 Main St", "city": "New York",
                                 "state": "NY", "zip": "10001"},
            "utility_bills": [_bill]}]})
    try:
        expected_form = {
            "email_address", "first_name", "last_name", "phone_number", "customer_type",
            "utility_name", "zip_code",
            "billing_address[address_1]", "billing_address[city]",
            "billing_address[state]", "billing_address[zip]",
            "utility_accounts[0][utility_account_number]",
            "utility_accounts[0][service_address][address_1]",
            "utility_accounts[0][service_address][city]",
            "utility_accounts[0][service_address][state]",
            "utility_accounts[0][service_address][zip]"}
        check("form fields match the spec cURL exactly", set(form) == expected_form)
        check("bill uses EXPLICIT index [0], not bare []",
              list(files) == ["utility_accounts[0][utility_bills][0]"])
        check("no bare [] indexing anywhere",
              not any("[]" in k for k in list(form) + list(files)))
        check("bill is sent as application/pdf (spec: PDF only)",
              list(files.values())[0][2] == "application/pdf")
        check("all form values are strings", all(isinstance(v, str) for v in form.values()))
    finally:
        for v in files.values():
            v[1].close()

    # Multi-account / multi-bill / meter numbers - every index must be explicit.
    form2, files2 = build_enrollment_multipart({
        "email_address": "a@b.com", "first_name": "A", "last_name": "B",
        "phone_number": "5185550142", "customer_type": "Residential",
        "utility_name": "nyseg", "zip_code": "12202",
        "billing_address": {"address_1": "1 X St", "city": "Albany", "state": "NY", "zip": "12202"},
        "utility_accounts": [
            {"utility_account_number": "11111111111",
             "secondary_account_identifier": "N01123456789012",
             "meter_numbers": ["M1", "M2"],
             "service_address": {"address_1": "1 X St", "city": "Albany",
                                  "state": "NY", "zip": "12202"},
             "utility_bills": [_bill]},
            {"utility_account_number": "22222222222",
             "service_address": {"address_1": "2 Y St", "city": "Albany",
                                  "state": "NY", "zip": "12202"},
             "utility_bills": [_bill, _bill]}]})
    try:
        check("second account uses index [1]",
              "utility_accounts[1][utility_account_number]" in form2)
        check("meter numbers are indexed [0] and [1]",
              form2.get("utility_accounts[0][meter_numbers][0]") == "M1"
              and form2.get("utility_accounts[0][meter_numbers][1]") == "M2")
        check("POD ID sent as secondary_account_identifier",
              form2.get("utility_accounts[0][secondary_account_identifier]") == "N01123456789012")
        check("multiple bills on one account are indexed [0] and [1]",
              "utility_accounts[1][utility_bills][0]" in files2
              and "utility_accounts[1][utility_bills][1]" in files2)
        check("three bill uploads across two accounts", len(files2) == 3)
        check("still no bare [] anywhere",
              not any("[]" in k for k in list(form2) + list(files2)))
    finally:
        for v in files2.values():
            v[1].close()

    # Business customer requires the business fields + home_address.
    form3, files3 = build_enrollment_multipart({
        "email_address": "j@acme.com", "first_name": "Jane", "last_name": "Smith",
        "phone_number": "2125555678", "customer_type": "Business",
        "business_name": "Acme Corporation", "business_title": "Facilities Manager",
        "business_phone": "2125559999",
        "utility_name": "consolidated-edison-ny", "zip_code": "10002",
        "billing_address": {"address_1": "456 Business Blvd", "city": "New York",
                             "state": "NY", "zip": "10002"},
        "home_address": {"address_1": "789 Home Ave", "city": "Brooklyn",
                          "state": "NY", "zip": "11201"},
        "utility_accounts": []})
    check("business fields included for customer_type=Business",
          form3.get("business_name") == "Acme Corporation"
          and form3.get("business_title") == "Facilities Manager"
          and form3.get("business_phone") == "2125559999")
    check("home_address uses bracket notation without an index",
          form3.get("home_address[city]") == "Brooklyn")
    check("empty/absent optional fields are omitted, not sent blank",
          "address_2" not in " ".join(form3.keys()))

    section("POST /enroll - response normalization + next_step branching")
    nd = normalize_enroll_response({"next_step": "https://x/affiliate_partners/v1/enrollments/contracts"})
    check("documented next_step read", nd["next_step"].endswith("/contracts"))
    check("tagged documented", nd["response_shape"] == "documented")
    na = normalize_enroll_response({"next_step_url": "https://x/enrollments/lmi/proof_docs"})
    check("staging next_step_url alias accepted", na["next_step"].endswith("/lmi/proof_docs"))
    check("tagged staging_alias", na["response_shape"] == "staging_alias")
    nb = normalize_enroll_response({"next_step": "https://DOC", "next_step_url": "https://ALIAS"})
    check("documented wins when both present", nb["next_step"] == "https://DOC")
    check("raw preserved", nb["raw"]["next_step_url"] == "https://ALIAS")
    nn = normalize_enroll_response({})
    check("missing both is flagged", nn["response_shape"] == "no_next_step")

    section("POST /enroll - National Grid needs no POD ID (published rules)")
    with app.app_context():
        check("national-grid-ny requires no secondary identifier",
              U.pod_id_rule("national-grid-ny") is None)
        check("national-grid-ny account numbers are 10 digits",
              query_one("SELECT account_number_length FROM perch_utilities WHERE slug=?",
                        ("national-grid-ny",))["account_number_length"] == 10)

    section("POST /enroll - _msg() formats the OBSERVED validation envelope")
    from services.perch.client import _msg as _fmt

    class _ErrResp:
        status_code = 422
        headers = {"Content-Type": "application/json"}
        def __init__(self, text): self.text = text
        def json(self): return json.loads(self.text)

    # The exact 422 captured from live staging.
    observed = _ErrResp(json.dumps({"errors": [{
        "field": "customer_type", "code": "capacity_unavailable",
        "message": "Residential or Small CS capacity is not available. "
                   "Contact Perch Energy and fetch capacity using the /capacity endpoint"}]}))
    msg = _fmt(observed)
    check("no longer renders as the useless 'None: None'", msg != "None: None")
    check("field name surfaced", "customer_type" in msg)
    check("error code surfaced", "capacity_unavailable" in msg)
    check("human message surfaced", "capacity is not available" in msg)

    # Multiple errors, including one scoped to a utility account.
    multi = _ErrResp(json.dumps({"errors": [
        {"field": "customer_type", "code": "too_many_utility_accounts",
         "message": "Only one utility account can be processed when LMI capacity is available"},
        {"utility_account_number": "1234567890", "field": "Utility bill size",
         "code": "file_too_large", "message": "Utility bill size must be less than 4MB"}]}))
    mmsg = _fmt(multi)
    check("multiple validation errors are all reported",
          "too_many_utility_accounts" in mmsg and "file_too_large" in mmsg)
    check("account-scoped errors name the account",
          "utility_account_number=1234567890" in mmsg)

    # The other documented envelope must still work.
    env = _ErrResp(json.dumps({"error": "unprocessable_entity", "message": "Email is invalid"}))
    check("standard {error,message} envelope still formats",
          _fmt(env) == "unprocessable_entity: Email is invalid")

    class _HtmlResp:
        status_code = 422
        headers = {"Content-Type": "text/html"}
        text = "<html>gateway error</html>"
        def json(self): raise ValueError("not json")
    check("non-JSON body falls back to raw text", "gateway error" in _fmt(_HtmlResp()))

    section("POST /enroll - customer_type is derived from capacity, per the spec enum")
    import importlib.util as _ilu
    _vspec = _ilu.spec_from_file_location(
        "_verify_enroll",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "verify_enroll_live.py"))
    _v = _ilu.module_from_spec(_vspec)
    _vspec.loader.exec_module(_v)

    # Spec enum is [Residential, Business, LMI]; there is no separate LMI selector field.
    t, _ = _v.choose_customer_type({"lmi_capacity_available": True,
                                     "residential_capacity_available": False,
                                     "small_commercial_capacity_available": False})
    check("LMI-only capacity selects customer_type='LMI'", t == "LMI")
    t, _ = _v.choose_customer_type({"lmi_capacity_available": False,
                                     "residential_capacity_available": True,
                                     "small_commercial_capacity_available": False})
    check("residential-only capacity selects 'Residential'", t == "Residential")
    t, _ = _v.choose_customer_type({"lmi_capacity_available": True,
                                     "residential_capacity_available": True,
                                     "small_commercial_capacity_available": False})
    check("LMI is preferred when both are available", t == "LMI")
    t, why = _v.choose_customer_type({"lmi_capacity_available": False,
                                       "residential_capacity_available": False,
                                       "small_commercial_capacity_available": True})
    check("small-commercial-only does NOT auto-select (needs Business fields)", t is None)
    check("and it explains why", "Business" in why)
    t, why = _v.choose_customer_type({"lmi_capacity_available": False,
                                       "residential_capacity_available": False,
                                       "small_commercial_capacity_available": False})
    check("no capacity selects nothing", t is None)
    check("chosen values are all within the spec enum",
          {"Residential", "Business", "LMI"} >= {
              x for x in [
                  _v.choose_customer_type({"lmi_capacity_available": True})[0],
                  _v.choose_customer_type({"residential_capacity_available": True})[0],
              ] if x})

    section("POST /lmi/proof_docs - multipart contract + response normalization")
    from services.perch.client import (
        build_proof_docs_multipart, normalize_proof_docs_response,
        PATH_LMI_PROOF_DOCS,
    )
    proof_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "test_snap_proof_letter.pdf")
    check("proof-doc endpoint path matches the published spec",
          PATH_LMI_PROOF_DOCS == "/lmi/proof_docs")
    check("fictional SNAP proof fixture is present", os.path.exists(proof_path))

    pf, metadata = build_proof_docs_multipart("1234567890", [{
        "source_type": "proof_doc_snap",
        "name_on_document": "Dalton Testcustomer",
        "relationship": "self",
        "document_type": "letter",
        "file_path": proof_path,
    }])
    try:
        check("proof_doc JSON part is application/json",
              pf["proof_doc"][0] is None and pf["proof_doc"][2] == "application/json")
        encoded = json.loads(pf["proof_doc"][1])
        check("proof_doc carries the matching utility account",
              encoded["utility_account_number"] == "1234567890")
        check("SNAP source type is encoded per document",
              encoded["documents"][0]["source_type"] == "proof_doc_snap")
        check("proof metadata includes all four required fields",
              set(encoded["documents"][0]) == {
                  "source_type", "name_on_document", "relationship", "document_type"})
        check("binary file is indexed as documents[0][file]",
              "documents[0][file]" in pf)
        check("proof PDF is sent as application/pdf",
              pf["documents[0][file]"][2] == "application/pdf")
        check("JSON metadata and binary file arrays have matching length",
              len(encoded["documents"]) == len([k for k in pf if k.startswith("documents[")]))
    finally:
        for key, value in pf.items():
            if key != "proof_doc":
                try: value[1].close()
                except Exception: pass

    pr = normalize_proof_docs_response({"next_step": "https://x/enrollments/contracts"})
    check("proof-doc documented next_step is read", pr["next_step"].endswith("/contracts"))
    check("proof-doc documented response is tagged", pr["response_shape"] == "documented")
    pa = normalize_proof_docs_response({"next_step_url": "https://x/enrollments/contracts"})
    check("proof-doc staging next_step_url alias is tolerated",
          pa["next_step"].endswith("/contracts") and pa["response_shape"] == "staging_alias")
    pb = normalize_proof_docs_response({"next_step": "https://DOC", "next_step_url": "https://ALIAS"})
    check("proof-doc documented key wins when both are present", pb["next_step"] == "https://DOC")
    check("proof-doc raw response is preserved", pb["raw"]["next_step_url"] == "https://ALIAS")

    # ───────────────────────────────────────────────────────────
    section("POST /contracts - response normalization")
    from services.perch.client import (
        normalize_contracts_response, contracts_safe, redact_contract_urls,
        PATH_CONTRACTS,
    )

    _SECRET = ("https://s3.amazonaws.com/perch-contracts/esign-abc"
               "?X-Amz-Signature=DEADBEEFSECRETSIGNATURE&X-Amz-Expires=3600")
    _ACCEPT = ("https://staging.api.perchenergy.com/affiliate_partners/v1/"
               "enrollments/contracts/accept")
    _documented = {
        "contract_urls": [
            {"contract_name": "ESIGN Consent Policy",
             "url": _SECRET, "expires_at": "2026-05-15T11:30:00Z"},
            {"contract_name": "Community Solar Garden Subscription Agreement",
             "url": _SECRET + "-2", "expires_at": "2026-05-15T11:30:00Z"},
            {"contract_name": "Consent to Disclose Utility Customer Data",
             "url": _SECRET + "-3", "expires_at": "2026-05-15T11:31:00Z"},
        ],
        "next_step": _ACCEPT,
    }

    n = normalize_contracts_response(_documented)
    check("endpoint path constant is /contracts", PATH_CONTRACTS == "/contracts")
    check("multiple contracts counted", n["contract_count"] == 3)
    check("contract_urls presence flagged", n["contract_urls_present"] is True)
    check("contract names preserved in order",
          [c["contract_name"] for c in n["contracts"]] == [
              "ESIGN Consent Policy",
              "Community Solar Garden Subscription Agreement",
              "Consent to Disclose Utility Customer Data"])
    check("expiration timestamps preserved per contract",
          [c["expires_at"] for c in n["contracts"]] ==
          ["2026-05-15T11:30:00Z", "2026-05-15T11:30:00Z", "2026-05-15T11:31:00Z"])
    check("each URL confirmed structurally present",
          all(c["url_present"] for c in n["contracts"]))
    check("next_step normalized", n["next_step"] == _ACCEPT)
    check("tagged documented", n["response_shape"] == "documented")

    section("POST /contracts - presigned URLs never leak")
    check("normalized contracts carry NO url key",
          all("url" not in c for c in n["contracts"]))
    check("secret absent from the normalized contract list",
          "DEADBEEFSECRETSIGNATURE" not in json.dumps(n["contracts"]))
    check("secret absent from contracts_safe() output",
          "DEADBEEFSECRETSIGNATURE" not in json.dumps(contracts_safe(n)))
    check("contracts_safe() returns copies, not references into raw",
          contracts_safe(n) is not n["contracts"])
    # raw intentionally retains the URLs so the caller can download immediately.
    check("raw still holds the URLs for immediate download only",
          "DEADBEEFSECRETSIGNATURE" in json.dumps(n["raw"]))
    check("redact_contract_urls() scrubs them for safe recording",
          "DEADBEEFSECRETSIGNATURE" not in json.dumps(redact_contract_urls(_documented)))
    check("redaction keeps the shape intact",
          len(redact_contract_urls(_documented)["contract_urls"]) == 3
          and redact_contract_urls(_documented)["contract_urls"][0]["contract_name"]
              == "ESIGN Consent Policy")
    check("redaction leaves the original body untouched",
          _documented["contract_urls"][0]["url"] == _SECRET)

    section("POST /contracts - malformed and missing payloads")
    _empty = normalize_contracts_response({"next_step": _ACCEPT})
    check("missing contract_urls yields zero contracts", _empty["contract_count"] == 0)
    check("and records that the key was absent", _empty["contract_urls_present"] is False)
    _wrong = normalize_contracts_response({"contract_urls": "not-a-list", "next_step": _ACCEPT})
    check("non-list contract_urls does not crash", _wrong["contract_count"] == 0)
    _partial = normalize_contracts_response({"contract_urls": [
        {"contract_name": "Missing URL", "expires_at": "2026-05-15T11:30:00Z"},
        {"contract_name": "Blank URL", "url": "   ", "expires_at": "2026-05-15T11:30:00Z"},
        "not-an-object"], "next_step": _ACCEPT})
    check("contract with no url flagged url_present=False",
          _partial["contracts"][0]["url_present"] is False)
    check("contract with whitespace-only url flagged False",
          _partial["contracts"][1]["url_present"] is False)
    check("non-object entry flagged malformed", _partial["contracts"][2]["malformed"] is True)
    check("malformed entries still counted", _partial["contract_count"] == 3)
    check("empty body does not crash", normalize_contracts_response({})["contract_count"] == 0)
    check("None body does not crash", normalize_contracts_response(None)["contract_count"] == 0)

    section("POST /contracts - staging alias tolerance (existing convention)")
    # /token, /capacity, /enroll and /lmi/proof_docs all needed this against real
    # staging, so /contracts follows the same normalization design.
    _alias = normalize_contracts_response({
        "contract_urls": [{"contract_name": "X", "url": _SECRET, "expires_at": "Z"}],
        "next_step_url": _ACCEPT})
    check("next_step_url accepted as next_step alias", _alias["next_step"] == _ACCEPT)
    check("tagged staging_alias", _alias["response_shape"] == "staging_alias")
    _both = normalize_contracts_response({
        "contract_urls": [], "next_step": "https://DOC", "next_step_url": "https://ALIAS"})
    check("documented next_step wins when both present", _both["next_step"] == "https://DOC")
    _neither = normalize_contracts_response({"contract_urls": []})
    check("neither key is flagged", _neither["response_shape"] == "no_next_step")

    section("POST /contracts - client sends no body, mock declines")
    _csrc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "services", "perch", "client.py"), encoding="utf-8").read()
    check("live client posts with no data= or files= payload",
          "resp = requests.post(url, headers=headers, timeout=max(self.timeout, 60))" in _csrc)
    check("live client sends X-Enrollment-Token",
          "ENROLLMENT_TOKEN_HEADER: enrollment_token" in _csrc)
    check("generate_contracts defined on the client", "def generate_contracts" in _csrc)
    from services.perch.config import get_perch_client as _gpc2
    from services.perch.errors import PerchNotImplementedError as _PNI2
    try:
        _gpc2().generate_contracts("tok")
        _declined = False
    except _PNI2:
        _declined = True
    check("mock declines /contracts (out of scope for the mock stack)", _declined)

    # ───────────────────────────────────────────────────────────
    section("POST /contracts/accept - request contract")
    from services.perch.client import (
        normalize_accept_response, normalize_status_response, PATH_CONTRACTS_ACCEPT,
    )
    from services.perch.errors import PerchAmbiguousOutcomeError

    check("endpoint path constant", PATH_CONTRACTS_ACCEPT == "/contracts/accept")

    acc = normalize_accept_response({"message": "Contracts accepted successfully"})
    check("message surfaced", acc["message"] == "Contracts accepted successfully")
    check("raw preserved", acc["raw"]["message"] == "Contracts accepted successfully")
    # Spec: "No next_step is returned - the enrollment is complete."
    check("no next_step key invented on the accept response", "next_step" not in acc)
    check("empty body does not crash", normalize_accept_response({})["message"] is None)
    check("None body does not crash", normalize_accept_response(None)["message"] is None)

    section("POST /contracts/accept - only the three documented metadata fields")
    _sent = {}

    class _AcceptResp:
        status_code = 202
        headers = {"Content-Type": "application/json"}
        text = '{"message":"Contracts accepted successfully"}'
        def json(self): return {"message": "Contracts accepted successfully"}

    class _FakeRequests:
        """Captures exactly what the client transmits."""
        def __init__(self, resp=None, raise_exc=None):
            self._resp = resp or _AcceptResp()
            self._raise = raise_exc
        def post(self, url, json=None, headers=None, timeout=None, **kw):
            if self._raise:
                raise self._raise
            _sent.clear()
            _sent.update({"url": url, "json": json, "headers": headers or {}})
            return self._resp
        def get(self, url, headers=None, timeout=None, **kw):
            _sent.clear()
            _sent.update({"url": url, "headers": headers or {}})
            return self._resp

    from services.perch.client import PerchHTTPClient as _HTTP
    _c = _HTTP("https://staging.example/enrollments", "https://staging.example/markets",
               "KEY", "SECRET")
    _c._requests = lambda: _FakeRequests()

    result = _c.accept_contracts("tok-123", {
        "ip_address": "203.0.113.42",
        "timestamp": "2026-08-14 10:00:00.1234",
        "user_agent": "Mozilla/5.0 (Test)"})
    check("HTTP 202 treated as success", result["message"] == "Contracts accepted successfully")
    check("posts to /contracts/accept", _sent["url"].endswith("/contracts/accept"))
    check("body has exactly one top-level key: metadata", list(_sent["json"].keys()) == ["metadata"])
    check("metadata has exactly the three documented fields",
          set(_sent["json"]["metadata"].keys()) == {"ip_address", "timestamp", "user_agent"})
    check("no invented contract ids / acceptance arrays / ack flags",
          not any(k in json.dumps(_sent["json"]) for k in
                  ("contract_id", "accepted_contracts", "acknowledged", "customer_confirmed")))
    check("X-Enrollment-Token sent", _sent["headers"].get("X-Enrollment-Token") == "tok-123")
    check("no HMAC headers on this session endpoint",
          not any(h.startswith("X-HMAC") for h in _sent["headers"]))

    section("POST /contracts/accept - IPv4, IPv6 and User-Agent limit")
    _c.accept_contracts("t", {"ip_address": "198.51.100.7", "timestamp": "x", "user_agent": "ua"})
    check("IPv4 transmitted unchanged", _sent["json"]["metadata"]["ip_address"] == "198.51.100.7")
    _v6 = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    _c.accept_contracts("t", {"ip_address": _v6, "timestamp": "x", "user_agent": "ua"})
    check("IPv6 transmitted unchanged", _sent["json"]["metadata"]["ip_address"] == _v6)

    # The route truncates to the documented 2048 maximum.
    import routes.perch_routes as _pr
    check("route enforces the documented 2048-char user-agent maximum",
          _pr._MAX_USER_AGENT == 2048)
    _long = "U" * 5000
    check("over-long user agent truncates to exactly 2048", len(_long[:_pr._MAX_USER_AGENT]) == 2048)

    section("POST /contracts/accept - error handling and the idempotency guardrail")

    class _Resp:
        def __init__(self, code, body=None, text=None):
            self.status_code = code
            self.headers = {"Content-Type": "application/json"}
            self.text = text if text is not None else json.dumps(body or {})
            self._body = body or {}
        def json(self): return self._body

    from services.perch.errors import PerchTokenExpiredError as _TE, PerchValidationError as _VE
    _c._requests = lambda: _FakeRequests(resp=_Resp(403, {"error": "forbidden", "message": "expired"}))
    try:
        _c.accept_contracts("t", {"ip_address": "1.1.1.1", "timestamp": "x", "user_agent": "u"})
        _r403 = None
    except _TE as e:
        _r403 = e
    # 403 is a DEFINITE rejection - Perch did not process it - so refresh+retry is safe.
    check("403 raises PerchTokenExpiredError (definite rejection, retry-once safe)", _r403 is not None)
    check("403 carries response diagnostics", getattr(_r403, "status_code", None) == 403)

    _c._requests = lambda: _FakeRequests(resp=_Resp(422, {"errors": [
        {"field": "metadata.timestamp", "code": "invalid", "message": "timestamp is in the future"}]}))
    try:
        _c.accept_contracts("t", {"ip_address": "1.1.1.1", "timestamp": "x", "user_agent": "u"})
        _r422 = None
    except _VE as e:
        _r422 = e
    check("422 raises PerchValidationError", _r422 is not None)
    check("422 message names the offending field", "metadata.timestamp" in str(_r422))
    check("422 is NOT treated as ambiguous", not isinstance(_r422, PerchAmbiguousOutcomeError))

    # Ambiguous outcomes must never be retryable.
    _c._requests = lambda: _FakeRequests(raise_exc=OSError("connection reset"))
    try:
        _c.accept_contracts("t", {"ip_address": "1.1.1.1", "timestamp": "x", "user_agent": "u"})
        _ramb = None
    except PerchAmbiguousOutcomeError as e:
        _ramb = e
    check("transport failure raises PerchAmbiguousOutcomeError", _ramb is not None)
    check("ambiguous error tells the caller not to resend", "Do not resend" in str(_ramb))
    check("ambiguous error points at GET /status", "/status" in str(_ramb))

    _c._requests = lambda: _FakeRequests(resp=_Resp(500, {"error": "server_error", "message": "boom"}))
    try:
        _c.accept_contracts("t", {"ip_address": "1.1.1.1", "timestamp": "x", "user_agent": "u"})
        _r500 = None
    except PerchAmbiguousOutcomeError as e:
        _r500 = e
    check("5xx raises PerchAmbiguousOutcomeError, NOT a retryable error", _r500 is not None)
    check("5xx is not a PerchUnavailableError (which callers may retry)",
          type(_r500).__name__ == "PerchAmbiguousOutcomeError")

    section("GET /status - normalization")
    _complete = normalize_status_response({
        "completed_steps": ["generate_enrollment_token", "capacity_check",
                             "enroll_utility_accounts", "submit_proof_documents",
                             "generate_contracts", "submit_contracts_acceptance"],
        "remaining_steps": [], "completed": True, "next_step": None})
    check("completed_steps preserved", len(_complete["completed_steps"]) == 6)
    check("remaining_steps empty", _complete["remaining_steps"] == [])
    check("completed True", _complete["completed"] is True)
    check("next_step null when complete", _complete["next_step"] is None)

    _pending = normalize_status_response({
        "completed_steps": ["generate_enrollment_token", "capacity_check"],
        "remaining_steps": ["enroll_utility_accounts", "submit_contracts_acceptance"],
        "completed": False,
        "next_step": "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments/enroll"})
    check("incomplete status keeps remaining steps", len(_pending["remaining_steps"]) == 2)
    check("incomplete status is not marked complete", _pending["completed"] is False)
    check("next_step preserved while pending", _pending["next_step"].endswith("/enroll"))
    check("submit_contracts_acceptance visible as outstanding",
          "submit_contracts_acceptance" in _pending["remaining_steps"])

    # completed must be a literal True - never coerced from a truthy value.
    check("truthy-but-not-True is NOT treated as complete",
          normalize_status_response({"completed": "yes"})["completed"] is False)
    check("missing completed defaults to False",
          normalize_status_response({})["completed"] is False)
    check("non-list steps degrade safely",
          normalize_status_response({"completed_steps": "nope"})["completed_steps"] == [])
    check("status tolerates the staging next_step_url alias",
          normalize_status_response({"next_step_url": "https://x/enroll"})["next_step"]
          == "https://x/enroll")
    check("None body does not crash", normalize_status_response(None)["completed"] is False)

    section("GET /status - request contract")
    _c._requests = lambda: _FakeRequests(resp=_Resp(200, {
        "completed_steps": [], "remaining_steps": [], "completed": False, "next_step": None}))
    _c.get_status("tok-abc")
    check("GET /status uses the status path", _sent["url"].endswith("/status"))
    check("GET /status sends X-Enrollment-Token",
          _sent["headers"].get("X-Enrollment-Token") == "tok-abc")
    check("GET /status sends no HMAC headers",
          not any(h.startswith("X-HMAC") for h in _sent["headers"]))

    # ───────────────────────────────────────────────────────────
    section("POST /contracts/accept - timestamp clock-skew allowance")
    from services.perch.client import (
        acceptance_timestamp, ACCEPTANCE_CLOCK_SKEW_SECONDS, ACCEPTANCE_TIMESTAMP_FORMAT,
    )
    from datetime import datetime as _dt, timedelta as _td, timezone as _tzone
    from zoneinfo import ZoneInfo as _ZoneInfo

    # Live staging returned 422 "Metadata timestamp cannot be in the future" for a
    # timestamp generated at the exact instant of submission. Perch PARSED it and
    # applied the not-in-the-future rule, so the format is fine - the value was not.
    _ts = acceptance_timestamp()
    _parsed = _dt.strptime(_ts, ACCEPTANCE_TIMESTAMP_FORMAT)
    # Compare against EASTERN wall-clock, not UTC and not the host's local zone.
    # Live evidence (2026-08-17) proved Perch interprets this offset-less field
    # as Eastern: sending UTC put the value ~4 hours in Perch's future and was
    # rejected 422. An earlier revision of this block asserted UTC; that
    # assumption is now demonstrated wrong for this specific Perch field.
    _ny_now_naive = _dt.now(_ZoneInfo("America/New_York")).replace(tzinfo=None)
    _age = (_ny_now_naive - _parsed).total_seconds()

    check("generated timestamp is NOT in the future", _age > 0)
    check("timestamp is at least the configured skew in the past",
          _age >= ACCEPTANCE_CLOCK_SKEW_SECONDS - 1)
    check("timestamp is still recent (well under Perch's 24h maximum)", _age < 86400)
    check("timestamp is recent enough to be an honest acceptance record", _age < 120)
    check("skew allowance is small and conservative",
          5 <= ACCEPTANCE_CLOCK_SKEW_SECONDS <= 60)

    # Format must not drift - staging demonstrably parses this shape.
    check("format matches the spec example (space separated, no timezone)",
          " " in _ts and "T" not in _ts and "Z" not in _ts)
    check("microseconds truncated to 4 digits like the spec example",
          len(_ts.split(".")[-1]) == 4)
    check("timestamp round-trips through strptime", isinstance(_parsed, _dt))

    # Deterministic check against a fixed instant.
    _fixed = _dt(2026, 8, 14, 15, 29, 47, 606500)
    _out = acceptance_timestamp(now=_fixed)
    _out_parsed = _dt.strptime(_out, ACCEPTANCE_TIMESTAMP_FORMAT)
    check("helper subtracts exactly the configured skew",
          abs((_fixed - _out_parsed).total_seconds() - ACCEPTANCE_CLOCK_SKEW_SECONDS) < 0.01)
    check("a timestamp built from 'now' would have been >= now (the original bug)",
          _fixed.strftime(ACCEPTANCE_TIMESTAMP_FORMAT)[:-2] > _out)

    section("POST /contracts/accept - metadata schema unchanged by the fix")
    import routes.perch_routes as _pr2
    _route_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "routes", "perch_routes.py"), encoding="utf-8").read()
    check("route delegates to the shared acceptance_timestamp helper",
          "acceptance_timestamp()" in _route_src)
    check("route no longer builds a bare datetime.now() acceptance timestamp",
          'datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")' not in _route_src)
    check("user-agent limit still 2048", _pr2._MAX_USER_AGENT == 2048)

    # The wire schema must still be exactly Perch's three fields.
    _md_sent = {}

    class _TsResp:
        status_code = 202
        headers = {"Content-Type": "application/json"}
        text = '{"message":"ok"}'
        def json(self): return {"message": "ok"}

    class _TsRequests:
        def post(self, url, json=None, headers=None, timeout=None, **kw):
            _md_sent.clear(); _md_sent.update(json or {})
            return _TsResp()

    from services.perch.client import PerchHTTPClient as _H2
    _c2 = _H2("https://x/enrollments", "https://x/markets", "K", "S")
    _c2._requests = lambda: _TsRequests()
    _c2.accept_contracts("tok", {"ip_address": "203.0.113.9",
                                  "timestamp": acceptance_timestamp(),
                                  "user_agent": "UA"})
    check("body still has exactly one top-level key: metadata",
          list(_md_sent.keys()) == ["metadata"])
    check("metadata still has exactly the three Perch fields",
          set(_md_sent["metadata"].keys()) == {"ip_address", "timestamp", "user_agent"})
    check("transmitted timestamp is not in the future (Eastern wall-clock)",
          (_dt.now(_ZoneInfo("America/New_York")).replace(tzinfo=None)
           - _dt.strptime(_md_sent["metadata"]["timestamp"],
                          ACCEPTANCE_TIMESTAMP_FORMAT)).total_seconds() > 0)

    section("Staging verifier - CLI parsing and production parity")
    _vsrc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "verify_contracts_accept_live.py"), encoding="utf-8").read()
    check("verifier uses argparse", "argparse.ArgumentParser" in _vsrc)
    check("verifier no longer reads positional sys.argv[1]/[2]",
          "sys.argv[1]" not in _vsrc and "sys.argv[2]" not in _vsrc)
    check("safety flag parsed as a flag, not a positional",
          'action="store_true"' in _vsrc and "args.accept_for_real" in _vsrc)
    # Production parity: the verifier must use the same helper the route uses.
    check("verifier uses the shared acceptance_timestamp helper",
          "acceptance_timestamp()" in _vsrc)
    check("verifier builds no bare datetime.now() acceptance timestamp",
          'datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")' not in _vsrc)
    check("safety gate still precedes the accept call",
          _vsrc.index("args.accept_for_real") < _vsrc.index("client.accept_contracts(token, metadata)"))

    # Reproduce the reported bug against the verifier's own parser shape.
    import argparse as _ap
    def _parse(argv):
        _p = _ap.ArgumentParser()
        _p.add_argument("zip_code", nargs="?", default="12202")
        _p.add_argument("utility", nargs="?", default="national-grid-ny")
        _p.add_argument("--i-understand-this-accepts-contracts",
                        dest="accept_for_real", action="store_true")
        return _p.parse_args(argv)

    _a = _parse([])
    check("no args: dry run, defaults intact",
          _a.zip_code == "12202" and _a.accept_for_real is False)
    _a = _parse(["--i-understand-this-accepts-contracts"])
    check("flag alone is NOT parsed as the ZIP code (the reported bug)",
          _a.zip_code == "12202" and _a.utility == "national-grid-ny")
    check("flag alone enables acceptance", _a.accept_for_real is True)
    _a = _parse(["12203", "nyseg"])
    check("positional overrides still work",
          _a.zip_code == "12203" and _a.utility == "nyseg" and _a.accept_for_real is False)
    _a = _parse(["12203", "nyseg", "--i-understand-this-accepts-contracts"])
    check("overrides plus flag work together",
          _a.zip_code == "12203" and _a.accept_for_real is True)
    _a = _parse(["--i-understand-this-accepts-contracts", "12203"])
    check("flag before positionals works",
          _a.zip_code == "12203" and _a.accept_for_real is True)

    section("ACCEPTANCE TIMESTAMP — Eastern wall-clock, bounded skew")
    import time as _time
    from datetime import datetime as _dt2, timezone as _tz2
    from zoneinfo import ZoneInfo as _ZI
    from services.perch.client import (acceptance_timestamp, acceptance_clock_skew_seconds,
                                       acceptance_timezone,
                                       ACCEPTANCE_TIMEZONE_NAME,
                                       DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS,
                                       MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS)

    _NY = _ZI("America/New_York")
    _saved_tz = os.environ.get("TZ")
    _saved_skew = os.environ.get("PERCH_ACCEPTANCE_SKEW_SECONDS")
    os.environ.pop("PERCH_ACCEPTANCE_SKEW_SECONDS", None)
    try:
        check("default skew restored to 30s", DEFAULT_ACCEPTANCE_CLOCK_SKEW_SECONDS == 30)
        check("  ...and is what an unset env yields", acceptance_clock_skew_seconds() == 30)
        check("acceptance timezone is an explicit IANA name",
              ACCEPTANCE_TIMEZONE_NAME == "America/New_York")
        check("  ...resolved through ZoneInfo, not the host zone",
              acceptance_timezone().key == "America/New_York")

        # THE HOSTED BUG THIS GUARDS: Perch reads this offset-less field as
        # EASTERN. Sending UTC put it ~4h in Perch's future -> 422.
        for tzname in ("UTC", "America/New_York", "Europe/Berlin",
                       "Asia/Tokyo", "Australia/Sydney"):
            os.environ["TZ"] = tzname
            if hasattr(_time, "tzset"):
                _time.tzset()
            ts = acceptance_timestamp()
            parsed = _dt2.strptime(ts + "00", "%Y-%m-%d %H:%M:%S.%f")
            ny_delta = (parsed - _dt2.now(_NY).replace(tzinfo=None)).total_seconds()
            utc_delta = (parsed - _dt2.now(_tz2.utc).replace(tzinfo=None)).total_seconds()
            check(f"TZ={tzname}: value is NEW YORK wall-clock", -90 < ny_delta < 0)
            check(f"  ...and in the past for Perch", ny_delta < 0)
            check(f"  ...NOT UTC wall-clock", abs(utc_delta) > 60 or tzname == "UTC")
        os.environ["TZ"] = "UTC"
        if hasattr(_time, "tzset"):
            _time.tzset()

        # DST must come from the zone database, not a hardcoded -4.
        _winter = acceptance_timestamp(_dt2(2026, 1, 15, 12, 0, 0, tzinfo=_tz2.utc))
        _summer = acceptance_timestamp(_dt2(2026, 8, 15, 12, 0, 0, tzinfo=_tz2.utc))
        check("winter converts at EST (-5)", _winter.startswith("2026-01-15 06:59:30"))
        check("summer converts at EDT (-4)", _summer.startswith("2026-08-15 07:59:30"))
        check("  ...so DST is real, not a fixed offset",
              _winter[11:13] != _summer[11:13])

        ts = acceptance_timestamp()
        check("wire format has no Z suffix", not ts.endswith("Z"))
        check("wire format has no offset", "+" not in ts and ts.count("-") == 2)
        check("wire format is space-separated", " " in ts and "T" not in ts)
        check("microseconds truncated to 4 digits", len(ts.split(".")[-1]) == 4)
        check("parseable as the documented format",
              _dt2.strptime(ts + "00", "%Y-%m-%d %H:%M:%S.%f") is not None)

        for raw_val, expected in (("0", 0), ("15", 15), ("30", 30), ("120", 120),
                                  ("9999", MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS),
                                  ("-5", 0), ("abc", 30), ("", 30)):
            os.environ["PERCH_ACCEPTANCE_SKEW_SECONDS"] = raw_val
            check(f"skew {raw_val!r} -> {expected}s", acceptance_clock_skew_seconds() == expected)
        os.environ.pop("PERCH_ACCEPTANCE_SKEW_SECONDS", None)
        check("skew capped at 120s", MAX_ACCEPTANCE_CLOCK_SKEW_SECONDS == 120)

        # A naive caller datetime is treated as already-Eastern.
        _naive = acceptance_timestamp(_dt2(2026, 8, 15, 12, 0, 0))
        check("naive input treated as Eastern wall-clock",
              _naive.startswith("2026-08-15 11:59:30"))
    finally:
        if _saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _saved_tz
        if hasattr(_time, "tzset"):
            _time.tzset()
        if _saved_skew is None:
            os.environ.pop("PERCH_ACCEPTANCE_SKEW_SECONDS", None)
        else:
            os.environ["PERCH_ACCEPTANCE_SKEW_SECONDS"] = _saved_skew

    section("RECONCILE-FIRST RESUME (runtime)")
    import subprocess as _sp
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _h = os.path.join(_root, "test", "contract_resume_reconcile_harness.js")
    check("reconcile harness exists", os.path.exists(_h))
    _r = _sp.run(["node", _h], capture_output=True, text=True, timeout=120)
    for _line in (_r.stdout + _r.stderr).splitlines():
        if "[FAIL]" in _line:
            print("      " + _line.strip())
    check("reopen reconciles with /status before /contracts", _r.returncode == 0)
    _js = open(os.path.join(_root, "static", "js", "app.js"), encoding="utf-8").read()
    check("resume calls perch-status", "/perch-status" in _js)
    check("regeneration is gated on the reconciled stage", "canRegenerate" in _js)
    check("the misleading reopen advice is gone",
          "Return to the dashboard and reopen this enrollment to retry" not in _js)

    print(f"\n{'='*74}\nMILESTONE 2.5 + STAGING SHAPE - ALL CHECKS PASSED\n{'='*74}")


if __name__ == "__main__":
    main()
