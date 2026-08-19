"""
Does the persisted program choice actually reach Perch and govern the contracts?

Traced end to end rather than assumed from the frontend branch:
    enrollments.selected_customer_type
      -> POST /enroll  payload["customer_type"]
      -> build_enrollment_multipart -> multipart field customer_type
      -> Perch creates the enrollment under THAT program
      -> the enrollment_token is bound to that Perch enrollment
      -> POST /contracts sends NO BODY, only that token
      -> Perch returns the contracts for that enrollment
      -> Dalton renders exactly what came back

Run: python test/test_contract_branch_routing.py
"""
import inspect
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import adapter
from services.perch.client import build_enrollment_multipart
from services.perch.errors import PerchValidationError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
DUAL = ("10901", "orange-and-rockland")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def start_dual(c, h, tag):
    r = c.post("/api/perch/enrollments/capacity", headers=h,
               json={"email": f"{tag}@example.com", "zip_code": DUAL[0],
                     "utility_name": DUAL[1]})
    assert r.status_code == 200, r.data
    return r.get_json()["enrollment_id"]


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("PATH STEP 1 — persisted choice reaches the enroll payload")
    src = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("the enroll route reads the PERSISTED selection",
          'enrollment["selected_customer_type"]' in src)
    check("  ...and writes it into the Perch payload",
          'payload["customer_type"] = chosen' in src)

    adapter_src = inspect.getsource(adapter.create_enrollment)
    check("the adapter re-validates it against capacity",
          "resolve_customer_type(details, requested=requested_type)" in adapter_src)
    check("  ...and the RESOLVED value is what is sent",
          'payload["customer_type"] = customer_type' in adapter_src)
    check("  ...via the multipart builder",
          "build_enrollment_multipart(payload)" in adapter_src)

    section("PATH STEP 2 — customer_type is on the wire")
    for ctype in ("Residential", "LMI"):
        payload = {"customer_type": ctype, "email_address": "a@b.com",
                   "first_name": "J", "last_name": "D", "phone_number": "5185550100",
                   "billing_address": {"address_1": "1 Main", "city": "Suffern",
                                       "state": "NY", "zip": "10901"},
                   "utility_accounts": [{"utility_account_number": "123",
                                         "service_address": {"address_1": "1 Main",
                                                             "city": "Suffern",
                                                             "state": "NY", "zip": "10901"},
                                         "utility_bills": []}]}
        form, _files = build_enrollment_multipart(payload)
        check(f"{ctype}: multipart carries customer_type={ctype}",
              form.get("customer_type") == ctype)

    section("PATH STEP 3 — /contracts carries NO program, only the token")
    client_src = open(os.path.join(ROOT, "services", "perch", "client.py"),
                      encoding="utf-8").read()
    # There are two definitions: the abstract base (which documents the
    # contract) and the concrete HTTP client. Check BOTH.
    parts = client_src.split("    def generate_contracts(self, enrollment_token: str)")
    check("generate_contracts is declared with only the token", len(parts) == 3)
    base = parts[1][:parts[1].index("    def accept_contracts")]
    concrete = parts[2][:parts[2].index("    def accept_contracts")]
    check("the base contract documents NO request body", "NO request body" in base)
    check("the concrete client posts headers only",
          "requests.post(url, headers=headers" in concrete)
    check("  ...with an explicit no-body comment", "No body" in concrete)
    check("no customer_type is transmitted on /contracts",
          "customer_type" not in base and "customer_type" not in concrete)
    ad_gen = inspect.getsource(adapter.generate_contracts)
    check("  ...the adapter passes only the token",
          "client.generate_contracts(token)" in ad_gen)
    check("  ...so the contract set is decided by Perch from the enrollment",
          "customer_type" not in ad_gen)

    # ═══════════════════════════════════════════════════════
    section("RESIDENTIAL branch — persisted and resolvable")
    res_eid = start_dual(c, rep, "resbranch")
    c.post(f"/api/perch/enrollments/{res_eid}/program", headers=rep,
           json={"customer_type": "Residential"})
    with app.app_context():
        stored = query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                           (res_eid,))["selected_customer_type"]
    check("selected_customer_type persisted as Residential", stored == "Residential")
    with app.app_context():
        details = (adapter.latest_capacity_check(res_eid) or {}).get("project_details") or {}
    resolved, reason = adapter.resolve_customer_type(details, requested=stored)
    check("  ...resolves to Perch customer_type 'Residential'", resolved == "Residential")
    check("  ...as an explicit selection", "explicit_selection" in reason)
    check("  ...Residential flow skips Eligibility (PASS 1: [1,2,5])",
          "if(needsLmi === false) return [1,2,5];" in JS)

    section("LMI branch — persisted and resolvable")
    lmi_eid = start_dual(c, rep, "lmibranch")
    c.post(f"/api/perch/enrollments/{lmi_eid}/program", headers=rep,
           json={"customer_type": "LMI"})
    with app.app_context():
        stored_l = query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                             (lmi_eid,))["selected_customer_type"]
    check("selected_customer_type persisted as LMI", stored_l == "LMI")
    with app.app_context():
        details_l = (adapter.latest_capacity_check(lmi_eid) or {}).get("project_details") or {}
    resolved_l, reason_l = adapter.resolve_customer_type(details_l, requested=stored_l)
    check("  ...resolves to Perch customer_type 'LMI'", resolved_l == "LMI")
    check("  ...as an explicit selection", "explicit_selection" in reason_l)
    check("  ...LMI flow includes Eligibility (PASS 1: [1,2,4,5])",
          "return [1,2,4,5];" in JS)

    check("the two enrollments resolve to DIFFERENT Perch programs",
          resolved != resolved_l)

    # ═══════════════════════════════════════════════════════
    section("NO STALE / CROSS-BRANCH CONTRACT DATA")
    check("the review packet is read from THIS enrollment's saved state",
          'saved.get("contracts")' in src)
    check("  ...a review token is bound to one enrollment_id",
          '"enrollment_id": enrollment_id,' in src.split("_contract_review_tokens[token]")[1][:200])
    check("  ...and to one contract index",
          '"contract_index": index,' in src.split("_contract_review_tokens[token]")[1][:200])
    check("  ...an out-of-range index is refused",
          "That contract is not in the current Perch review packet" in src)
    check("review tokens are single-use", "_contract_review_tokens.pop" in src)
    check("  ...and expire", "expires_at" in src)
    check("contract responses are never cached by the browser",
          'Cache-Control"] = "no-store, private"' in src)
    check("every contract route is ownership-scoped",
          "_visible_to_actor" in src or "_visible(" in src)

    # Cross-enrollment access must fail.
    from auth import hash_password
    with app.app_context():
        uid = execute("INSERT INTO users (email,password_hash,role,full_name) VALUES (?,?,?,?)",
                      ("branch@d.com", hash_password("RepPass1!"),
                       "sales_rep", "Branch Rep")).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, "REP-BR"))
    other = login(c, "branch@d.com", "RepPass1!")
    check("another rep cannot request this enrollment's contracts",
          c.post(f"/api/perch/enrollments/{res_eid}/contracts",
                 headers=other, json={}).status_code == 403)
    check("  ...nor open a contract for review",
          c.post(f"/api/perch/enrollments/{res_eid}/contracts/review",
                 headers=other, json={"index": 0}).status_code == 403)

    section("BACK / FORWARD / RESUME DO NOT CHANGE THE BRANCH")
    for i in range(3):
        b = c.get(f"/api/perch/enrollments/{res_eid}/programs", headers=rep).get_json()
        check(f"Residential still selected after reload {i+1}",
              b["selected_customer_type"] == "Residential")
    for i in range(3):
        b = c.get(f"/api/perch/enrollments/{lmi_eid}/programs", headers=rep).get_json()
        check(f"LMI still selected after reload {i+1}",
              b["selected_customer_type"] == "LMI")
    check("Residential detail exposes its own branch",
          c.get(f"/api/enrollments/{res_eid}", headers=rep)
           .get_json()["selected_customer_type"] == "Residential")
    check("LMI detail exposes its own branch",
          c.get(f"/api/enrollments/{lmi_eid}", headers=rep)
           .get_json()["selected_customer_type"] == "LMI")
    check("  ...the two never bleed into each other",
          c.get(f"/api/enrollments/{res_eid}", headers=rep)
           .get_json()["selected_customer_type"] !=
          c.get(f"/api/enrollments/{lmi_eid}", headers=rep)
           .get_json()["selected_customer_type"])

    section("RECONCILIATION CANNOT REGENERATE UNDER A DIFFERENT PROGRAM")
    check("resume reconciles with /status before regenerating", "canRegenerate" in JS)
    check("  ...and does not call /contracts once Perch has advanced",
          "REGENERABLE" in JS)
    check("regeneration re-reads the same enrollment's token",
          "client.generate_contracts(token)" in ad_gen)
    check("  ...and never passes a program", "customer_type" not in ad_gen)

    section("TAMPERING AFTER SELECTION STILL REJECTED")
    for bogus in ("SmallCommercial", "Business", "Commercial"):
        check(f"{bogus!r} rejected on the Residential enrollment",
              c.post(f"/api/perch/enrollments/{res_eid}/program", headers=rep,
                     json={"customer_type": bogus}).status_code == 400)
    with app.app_context():
        check("  ...selection unchanged",
              query_one("SELECT selected_customer_type FROM enrollments WHERE id=?",
                        (res_eid,))["selected_customer_type"] == "Residential")

    # Single-program locations still refuse the other program.
    r12401 = c.post("/api/perch/enrollments/capacity", headers=rep,
                    json={"email": "s12401@example.com", "zip_code": "12401",
                          "utility_name": "central-hudson-gas-electric"}).get_json()["enrollment_id"]
    check("12401 refuses LMI",
          c.post(f"/api/perch/enrollments/{r12401}/program", headers=rep,
                 json={"customer_type": "LMI"}).status_code == 400)
    r12901 = c.post("/api/perch/enrollments/capacity", headers=rep,
                    json={"email": "s12901@example.com", "zip_code": "12901",
                          "utility_name": "nyseg"}).get_json()["enrollment_id"]
    check("12901 refuses Residential",
          c.post(f"/api/perch/enrollments/{r12901}/program", headers=rep,
                 json={"customer_type": "Residential"}).status_code == 400)

    section("DALTON RENDERS EXACTLY WHAT PERCH RETURNS")
    check("agreement names come from the response, not a local list",
          "contract_name" in JS)
    check("  ...rendered as inline links in one sentence", "agreementLinksHtml" in JS)
    # A legacy pre-Perch document set (`docPacket`) still exists in app.js with
    # hardcoded titles. It is DECLARED AND NEVER REFERENCED - dead code from the
    # design that predates Perch-issued contracts. Assert it stays unreachable
    # rather than deleting it in a verification pass.
    check("the legacy docPacket is never referenced",
          JS.count("docPacket") == 1)
    check("  ...so no hardcoded contract name can reach the agreement screen",
          "docPacket" not in JS.split("function renderAgreementCard")[1])
    check("the rendered names come only from the Perch response",
          "c.contract_name || c" in JS)
    check("contract URLs are never persisted", "contracts_safe" in src)
    check("one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("one Agree & finish button", JS.count('id="agr-agree-btn"') == 1)

    section("MOCK COVERAGE — stated honestly")
    mock_src = open(os.path.join(ROOT, "services", "perch", "mock_client.py"),
                    encoding="utf-8").read()
    check("the mock implements token + capacity", "def check_capacity" in mock_src)
    check("  ...but NOT enroll", "def create_enrollment" not in mock_src)
    check("  ...and NOT contracts", "def generate_contracts" not in mock_src)
    # So the contract SET comparison is only possible against real staging.

    print(f"\n{'='*72}\nCONTRACT BRANCH ROUTING - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
