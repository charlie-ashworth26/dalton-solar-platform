"""
Virginia enrollment-flow support.

PHASE 3. Covers the minimum Virginia behaviour needed before a first real
staging enrollment: the selected state reaching the backend, market-driven
account fields, market-driven counties and income limits, the market-driven
`status` rule, and the income-qualification hard stop.

Does NOT perform a real Perch call. Perch is stubbed at the CLIENT boundary, so
the adapter builds its payload for real and the exact dict that would be
serialised is asserted - which is the only way to prove a key is ABSENT rather
than merely None.

Every Virginia value used here was confirmed against Perch staging on
2026-10-25. Nothing about Dominion is invented.

Run: python test/test_virginia_enrollment.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed
from services.perch import adapter, markets, workflow
from services.perch.mock_client import PerchMockClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
ADAPTER = open(os.path.join(ROOT, "services", "perch", "adapter.py"),
               encoding="utf-8").read()
ROUTES = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()

BASE = "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments"

# CONFIRMED staging values (2026-10-25).
VA_SLUG = "dominion-energy-va"
VA_ZIP = "24112"                 # Martinsville VA - the mock's "241" fixture
VA_CITY_COUNTY = "Martinsville city"


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


SENT = []
_real_call_with_refresh = adapter._call_with_refresh


class CapturingClient:
    """Captures the EXACT entry dict the adapter hands to the client."""

    def submit_self_attestation(self, token, entry):
        SENT.append({"op": "generate", "entry": entry})
        return {"next_step_url": BASE + "/lmi/self_attestation/accept",
                "document_available": True,
                "raw": {"self_attestation": {
                    "url": "https://s3/presigned?X-Amz-Expires=3600"}}}

    def accept_self_attestation(self, token, metadata):
        SENT.append({"op": "accept", "metadata": metadata})
        return {"next_step_url": BASE + "/contracts", "message": "ok"}


def stub_client_boundary():
    """Replace only the transport. adapter.submit_self_attestation - including
    the code that decides whether `status` belongs in the payload - runs for
    real, which is the whole point of this suite."""
    def fake(eid, uid, op, path, method, meta, fn, files=None):
        return fn(CapturingClient(), "stub-token"), False, 1
    adapter._call_with_refresh = fake


def restore_client_boundary():
    adapter._call_with_refresh = _real_call_with_refresh


def last(op):
    return [x for x in SENT if x["op"] == op][-1]


def main():
    """Wrapped so the adapter stub is ALWAYS removed.

    stub_client_boundary() replaces adapter._call_with_refresh for this suite.
    The restore used to sit at the end of the body, so the FIRST failing check
    left the stub installed process-wide - and every later suite in the same
    pytest run then got a CapturingClient with no create_enrollment on it.
    One real failure here became a dozen unrelated ones, which is how a run
    stops being readable. A finally makes the cleanup unconditional.
    """
    try:
        _main()
    finally:
        restore_client_boundary()
        markets.set_client_factory(None)


def _main():
    init_db(reset=True)
    seed.seed()
    markets.set_client_factory(PerchMockClient)
    markets.invalidate()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def enroll(tag, state, zip_code, slug, city, county_for_addr,
               acct="1234567890", meter=None):
        email = f"{tag}@example.com"
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": email, "zip_code": zip_code,
                           "utility_name": slug}).get_json()["enrollment_id"]
        ua = {"utility_name": slug, "account_number": acct}
        if meter is not None:
            ua["meter_number"] = meter
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Jane", "last_name": "Doe", "email": email,
                         "phone": "5185550100", "password": "CustPass1!"},
            "service_address": {"street": "20 Main St", "city": city,
                                "state": state, "zip": zip_code},
            "utility_account": ua})
        return eid, email

    # ═══════════════════════════════════════════════════════════════════
    section("1. THE SELECTED STATE REACHES THE BACKEND")

    check("the enroll payload sends the SELECTED state, not a literal",
          "state:state.address.state||'NY'" in JS.replace(" ", ""))
    check("  ...for the billing address too",
          "state:state.billing.state||state.address.state||'NY'" in JS.replace(" ", ""))
    check("  ...and the old hardcoded form is GONE",
          "city:state.address.city,state:'NY'" not in JS.replace(" ", ""))
    check("NY remains the default in the initial state",
          "state:'NY'" in JS.replace(" ", ""))
    check("  ...and remains the selected option in the markup",
          '<option value="NY" selected>NY</option>' in HTML)
    check("VA is selectable", '<option value="VA">VA</option>' in HTML)

    eid_va, _ = enroll("va1", "VA", VA_ZIP, VA_SLUG, "Martinsville", VA_CITY_COUNTY,
                       acct="1234567890", meter="0123456789")
    with app.app_context():
        row = query_one(
            "SELECT sa.state FROM service_addresses sa "
            "JOIN enrollments e ON e.service_address_id = sa.id WHERE e.id = ?",
            (eid_va,))
    check(f"a VA enrollment persists state='VA' (got {row['state']!r})",
          row["state"] == "VA")
    check("  ...NOT overwritten to NY", row["state"] != "NY")

    # ═══════════════════════════════════════════════════════════════════
    section("2. VIRGINIA MARKET DATA COMES FROM THE MARKET-RULES LAYER")

    r = c.get("/api/markets/utilities?state=VA", headers=rep)
    check("VA utilities come from market rules", r.status_code == 200)
    check(f"  ...and name the Dominion slug {VA_SLUG!r}",
          r.get_json()["utilities"] == [VA_SLUG])

    r = c.get(f"/api/markets/utility-account-fields?utility={VA_SLUG}", headers=rep)
    fields = r.get_json()["fields"]
    types = [f["field_type"] for f in fields]
    check("Dominion account fields come from market rules", r.status_code == 200)
    check(f"  ...account number + meter number (got {types})",
          types == ["account_number", "meter_number"])

    section("2b. DOMINION ACCOUNT / METER VALIDATION, FROM PUBLISHED PATTERNS")
    v = markets.validate_account_value
    check("10-digit account accepted", v(VA_SLUG, "account_number", "1234567890") is None)
    check("12-digit account accepted", v(VA_SLUG, "account_number", "123456789012") is None)
    check("11-digit account REJECTED (matches neither published pattern)",
          v(VA_SLUG, "account_number", "12345678901") is not None)
    check("10-digit meter accepted", v(VA_SLUG, "meter_number", "0123456789") is None)
    check("9-digit meter rejected", v(VA_SLUG, "meter_number", "012345678") is not None)
    check("11-digit meter rejected", v(VA_SLUG, "meter_number", "01234567890") is not None)
    check("blank meter rejected - Dominion requires it",
          v(VA_SLUG, "meter_number", "") is not None)
    check("NO secondary identifier for Dominion",
          markets.field_is_required(VA_SLUG, "secondary_account_identifier") is False)
    check("  ...so validating one is a no-op rather than an error",
          v(VA_SLUG, "secondary_account_identifier", "") is None)
    check("the meter number is persisted on the enrollment",
          query_one("SELECT ua.meter_number FROM utility_accounts ua JOIN enrollments e "
                    "ON e.utility_account_id = ua.id WHERE e.id = ?",
                    (eid_va,))["meter_number"] == "0123456789")
    check("the enroll payload forwards it as meter_numbers",
          '"meter_numbers": ([acct["meter_number"]]' in ROUTES)

    section("2d. adapter.create_enrollment ENFORCES THE PUBLISHED PATTERNS")
    # Called directly, because the /enroll ROUTE requires a saved utility bill
    # first and would reject for that reason instead - a 400 that would look
    # like a pass while proving nothing. This exercises the market-rules
    # validation inside the adapter, which is the code under test.
    from services.perch.errors import PerchValidationError as _PVE

    def adapter_enroll(acct_num, meter, email):
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": email, "zip_code": VA_ZIP,
                           "utility_name": VA_SLUG}).get_json()["enrollment_id"]
        payload = {
            "email_address": email, "first_name": "Jane", "last_name": "Doe",
            "phone_number": "5185550100", "customer_type": "LMI",
            "billing_address": {"address_1": "20 Main St", "city": "Martinsville",
                                "state": "VA", "zip": VA_ZIP},
            "utility_accounts": [{
                "utility_account_number": acct_num,
                "meter_numbers": ([meter] if meter else []),
                "service_address": {"address_1": "20 Main St", "city": "Martinsville",
                                    "state": "VA", "zip": VA_ZIP},
                "utility_bills": [],
            }],
        }
        # A value that PASSES validation reaches the transport, which the mock
        # does not implement for /enroll - so PerchNotImplementedError means
        # "validation passed", and only PerchValidationError means "rejected".
        from services.perch.errors import PerchNotImplementedError as _PNI
        try:
            with app.app_context():
                adapter.create_enrollment(eid, payload)
            return None
        except _PVE as e:
            return str(e)
        except _PNI:
            return None

    msg = adapter_enroll("12345678901", "0123456789", "vabad@example.com")
    check(f"an 11-digit Dominion account is refused ({(msg or '')[:48]!r})",
          msg is not None)
    check("  ...naming the published format, not a generic error",
          "format this utility requires" in (msg or "") or "10 or 12" in (msg or ""))
    check("a 2-digit meter number is refused",
          adapter_enroll("1234567890", "99", "vameter@example.com") is not None)
    check("a MISSING meter number is refused - Dominion requires it",
          adapter_enroll("1234567890", None, "vanometer@example.com") is not None)
    check("a VALID 10-digit account + 10-digit meter passes this validation",
          adapter_enroll("1234567890", "0123456789", "vaok@example.com") is None)
    check("  ...and so does the 12-digit account form",
          adapter_enroll("123456789012", "0123456789", "vaok2@example.com") is None)

    section("2e. AN UNKNOWN SLUG IS STILL REJECTED")
    # The market layer is what confirms a slug that is absent from the local
    # table. A slug Perch publishes nothing for must still be refused, or the
    # fallback would accept anything.
    r = c.post("/api/perch/enrollments/capacity", headers=rep,
               json={"email": "bogus@example.com", "zip_code": VA_ZIP,
                     "utility_name": "not-a-real-utility-slug"})
    check(f"an unpublished slug is refused (HTTP {r.status_code})",
          r.status_code == 400)
    check("  ...with the recognised-utility wording",
          "not a recognized utility" in (r.get_json().get("error") or ""))

    section("2c. THE FORM RENDERS THE FIELDS PERCH PUBLISHES")
    check("a meter-number input exists", 'id="c-meter"' in HTML)
    check("  ...hidden by default, like the POD ID field",
          'id="meter-number-field" style="display:none;"' in HTML)
    check("the frontend reads the fields from market rules",
          "/api/markets/utility-account-fields?utility=" in JS)
    check("  ...shows the meter field only when meter_number is listed",
          "f.field_type === 'meter_number'" in JS)
    check("  ...and hides a secondary identifier the utility does not list",
          "f.field_type === 'secondary_account_identifier'" in JS)
    check("the frontend validates against ALL published patterns, any-one-matches",
          "for(let i = 0; i < patterns.length; i++)" in JS)
    check("  ...account_number case-insensitively, per the spec",
          "fieldType === 'account_number' ? 'i' : ''" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("3. VA COUNTIES AND INCOME LIMITS")

    ref = c.get(f"/api/perch/enrollments/{eid_va}/lmi/self-attestation/reference",
                headers=rep).get_json()
    check("the VA reference reports its state", ref["state"] == "VA")
    check("  ...counties come from Perch, not ny_counties",
          ref["counties_source"] == "perch")
    check("  ...and include an independent city", VA_CITY_COUNTY in ref["counties"])
    check(f"  ...spelled EXACTLY as returned ({VA_CITY_COUNTY!r})",
          "Martinsville City" not in ref["counties"]
          and "Martinsville County" not in ref["counties"])
    check("  ...no NY county leaked in", "Albany County" not in ref["counties"])
    check("income limits are per-county for VA", ref["income_basis"] == "per_county")
    check("  ...so no statewide table is shipped", ref["thresholds"] == []
          and ref["version_label"] is None)

    il = c.get("/api/markets/county-income-limits?state=VA&county=" + VA_CITY_COUNTY,
               headers=rep).get_json()
    check("the VA county income limit is available for household sizes 1-8",
          [x["house_occupancy"] for x in il["income_limits"]] == list(range(1, 9)))
    check("  ...as float dollars, not cents",
          isinstance(il["income_limits"][0]["household_income"], float))
    check("  ...county echoed in Perch's stored spelling", il["county"] == VA_CITY_COUNTY)
    check("the frontend fetches it when the county changes",
          "/api/markets/county-income-limits?state=" in JS)
    check("  ...and formats DOLLARS, never reusing the cents formatter",
          "function formatUsdDollars(dollars)" in JS
          and "formatUsdDollars(hit.household_income)" in JS)
    check("  ...clearing the previous county's limit first, so no stale figure shows",
          "selfAttestation.countyIncomeLimits = null;" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("4. VIRGINIA SELF-ATTESTATION: NO status KEY")

    check("VA publishes an empty status array",
          markets.self_attestation_options("VA")["status"] == [])
    check("  ...so sends_status is False", markets.sends_status("VA") is False)
    check("the reference endpoint passes that to the frontend",
          ref["sends_status"] is False)

    stub_client_boundary()
    SENT.clear()
    r = c.post(f"/api/perch/enrollments/{eid_va}/lmi/self-attestation", headers=rep,
               json={"occupancy": 3, "county": VA_CITY_COUNTY, "qualifies": True})
    check(f"VA YES is accepted (HTTP {r.status_code})", r.status_code == 200)
    check("  ...and is NOT a not_qualified response",
          not r.get_json().get("not_qualified"))
    entry = last("generate")["entry"]
    check(f"the payload sent to Perch is {sorted(entry)}", True)
    check("  ...'status' is ABSENT from the payload", "status" not in entry)
    serialised = json.dumps({"self_attestation": entry})
    check("  ...absent from the SERIALISED body too, not merely null",
          '"status"' not in serialised)
    check("  ...lmi_source_selection is the single confirmed value",
          entry["lmi_source_selection"] == ["self_attestation_qualifying_income"])
    check("  ...county sent verbatim, independent city intact",
          entry["county"] == VA_CITY_COUNTY)
    check("  ...occupancy as an int", entry["occupancy"] == 3)
    check("  ...account number is the enrolled one", entry["utility_account_number"] == "1234567890")
    check("  ...and lmi_programs is omitted for this staging pass",
          "lmi_programs" not in entry)
    check("VA DID continue: the acceptance call followed",
          any(x["op"] == "accept" for x in SENT))

    # ═══════════════════════════════════════════════════════════════════
    section("5. VIRGINIA NO: HARD STOP")

    eid_no, _ = enroll("vano", "VA", VA_ZIP, VA_SLUG, "Martinsville", VA_CITY_COUNTY,
                       meter="0123456789")
    SENT.clear()
    r = c.post(f"/api/perch/enrollments/{eid_no}/lmi/self-attestation", headers=rep,
               json={"occupancy": 2, "county": VA_CITY_COUNTY, "qualifies": False})
    body = r.get_json()
    check(f"VA NO returns a definite answer (HTTP {r.status_code})", r.status_code == 200)
    check("  ...flagged not_qualified", body.get("not_qualified") is True)
    check("  ...with a clear customer-facing message",
          "does not qualify" in (body.get("message") or "").lower())
    check("NO PERCH CALL WAS MADE AT ALL", SENT == [])
    check("  ...no /lmi/self_attestation", not any(x["op"] == "generate" for x in SENT))
    check("  ...no /lmi/self_attestation/accept", not any(x["op"] == "accept" for x in SENT))
    check("no attestation row was written",
          query_one("SELECT COUNT(*) n FROM perch_self_attestation_submissions "
                    "WHERE enrollment_id = ?", (eid_no,))["n"] == 0)
    with app.app_context():
        st = workflow.get_state(eid_no)
    check("the stop is recorded in the workflow state",
          st["current_step_key"] == "lmi_not_qualified")
    check("  ...and that state is BLOCKED", workflow.is_blocked("lmi_not_qualified"))
    check("  ...with a rep-facing label, never a bare key",
          workflow.step_label("lmi_not_qualified")
          == "Does not qualify for the available program")

    section("5b. A STOPPED ENROLLMENT CANNOT REACH CONTRACTS")
    r = c.post(f"/api/perch/enrollments/{eid_no}/contracts", headers=rep, json={})
    check(f"contract generation is refused (HTTP {r.status_code})", r.status_code == 409)
    check("  ...for the stated reason", r.get_json().get("not_qualified") is True)
    check("NO PERCH CALL was made for contracts either", SENT == [])
    r = c.post(f"/api/perch/enrollments/{eid_no}/contracts/accept", headers=rep,
               json={"customer_confirmed": True})
    check(f"agreement completion is refused (HTTP {r.status_code})", r.status_code == 409)
    # Attestation acceptance is CUSTOMER-scoped, so a rep is refused by the
    # actor check (403) before the blocked guard is even reached. Either way it
    # is refused and no Perch call is made, which is the requirement. The
    # blocked guard is what refuses it for a customer actor, and is asserted
    # structurally below.
    r = c.post(f"/api/perch/enrollments/{eid_no}/lmi/self-attestation/accept",
               headers=rep, json={})
    check(f"self-attestation acceptance is refused (HTTP {r.status_code})",
          400 <= r.status_code < 500)
    check("  ...and still no Perch call", SENT == [])
    check("  ...the blocked guard also covers the customer-scoped accept route",
          "_blocked_guard(enrollment_id)" in
          ROUTES.split("def accept_self_attestation")[1][:900])
    check("the guard is server-side, not only in the UI", "_blocked_guard" in ROUTES)
    check("  ...and is applied to contracts, accept and attestation-accept",
          ROUTES.count("blocked = _blocked_guard(enrollment_id)") == 3)

    section("5c. THE UI PRESENTS THE STOP AND OFFERS NO WAY ON")
    check("a terminal not-qualified panel exists", 'id="sa-not-qualified"' in HTML)
    check("  ...hidden by default",
          'id="sa-not-qualified" style="display:none;"' in HTML)
    check("  ...offering only 'Back to dashboard'",
          "renderLmiNotQualified" in JS)
    check("  ...and the frontend routes a not_qualified response to it",
          "if(body.not_qualified){" in JS)
    check("the reset clears it, so the next enrollment is not shown this one's refusal",
          "if(saNQ) saNQ.style.display = 'none';" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("6. NEW YORK IS UNCHANGED")

    restore_client_boundary()
    markets.invalidate()
    eid_ny, _ = enroll("ny1", "NY", "12401", "central-hudson-gas-electric",
                       "Kingston", "Ulster County")
    with app.app_context():
        check("an NY enrollment still persists state='NY'",
              query_one("SELECT sa.state FROM service_addresses sa JOIN enrollments e "
                        "ON e.service_address_id = sa.id WHERE e.id = ?",
                        (eid_ny,))["state"] == "NY")

    ny_ref = c.get(f"/api/perch/enrollments/{eid_ny}/lmi/self-attestation/reference",
                   headers=rep).get_json()
    check("NY counties still come from ny_counties",
          ny_ref["counties_source"] == "ny_counties")
    check(f"  ...all 62 of them (got {len(ny_ref['counties'])})",
          len(ny_ref["counties"]) == 62)
    check("  ...with the established spelling", "Albany County" in ny_ref["counties"])
    check("  ...and no VA county leaked in",
          VA_CITY_COUNTY not in ny_ref["counties"])
    check("NY still ships the statewide table",
          ny_ref["income_basis"] == "statewide_table")
    check("  ...versioned as before", ny_ref["version_label"] == "NY-SMI-80-2026")
    check("  ...with 8 household sizes", len(ny_ref["thresholds"]) == 8)
    check("  ...and its caption", bool(ny_ref["table_caption"]))
    check("NY still sends status", ny_ref["sends_status"] is True)

    stub_client_boundary()
    SENT.clear()
    r = c.post(f"/api/perch/enrollments/{eid_ny}/lmi/self-attestation", headers=rep,
               json={"occupancy": 4, "county": "Ulster County", "status": "accepted"})
    check("NY self-attestation succeeds", r.status_code == 200)
    ny_entry = last("generate")["entry"]
    check("  ...and DOES send status", "status" in ny_entry)
    check(f"  ...as 'accepted' (got {ny_entry.get('status')!r})",
          ny_entry["status"] == "accepted")
    check("  ...with the same single source type",
          ny_entry["lmi_source_selection"] == ["self_attestation_qualifying_income"])

    eid_ny2, _ = enroll("ny2", "NY", "12401", "central-hudson-gas-electric",
                        "Kingston", "Ulster County")
    SENT.clear()
    c.post(f"/api/perch/enrollments/{eid_ny2}/lmi/self-attestation", headers=rep,
           json={"occupancy": 1, "county": "Albany County", "status": "rejected"})
    check("NY 'above the limit' still sends status='rejected' to Perch",
          last("generate")["entry"]["status"] == "rejected")
    check("  ...and is NOT turned into a hard stop",
          workflow.get_state(eid_ny2)["current_step_key"] != "lmi_not_qualified")

    section("6b. NY REJECTS A MISSING/BAD status EXACTLY AS BEFORE")
    eid_ny3, _ = enroll("ny3", "NY", "12401", "central-hudson-gas-electric",
                        "Kingston", "Ulster County")
    r = c.post(f"/api/perch/enrollments/{eid_ny3}/lmi/self-attestation", headers=rep,
               json={"occupancy": 2, "county": "Albany County"})
    check(f"no status -> 400 (HTTP {r.status_code})", r.status_code == 400)
    r = c.post(f"/api/perch/enrollments/{eid_ny3}/lmi/self-attestation", headers=rep,
               json={"occupancy": 2, "county": "Albany County", "status": "maybe"})
    check(f"invalid status -> 400 (HTTP {r.status_code})", r.status_code == 400)
    r = c.post(f"/api/perch/enrollments/{eid_ny3}/lmi/self-attestation", headers=rep,
               json={"occupancy": 2, "county": "Nowhere County", "status": "accepted"})
    check(f"unknown NY county -> 400 (HTTP {r.status_code})", r.status_code == 400)
    check("  ...with the original NY wording",
          "New York county" in (r.get_json().get("error") or ""))
    check("a VA enrollment with no qualification answer -> 400",
          c.post(f"/api/perch/enrollments/{eid_va}/lmi/self-attestation", headers=rep,
                 json={"occupancy": 3, "county": VA_CITY_COUNTY}).status_code
          in (400, 409))

    section("6c. NYSEG POD BEHAVIOUR UNCHANGED")
    # The mock deliberately carries NO NY market fixture, so an NY utility's
    # field rules are unavailable offline. That is the proof NY was not silently
    # repointed at a second source of truth: its POD behaviour still comes from
    # the local perch_utilities rules, asserted immediately below.
    try:
        markets.utility_account_fields("nyseg")
        nyseg_from_market = True
    except markets.MarketDataUnavailable:
        nyseg_from_market = False
    check("NY utility rules are NOT served from the market mock",
          nyseg_from_market is False)
    with app.app_context():
        from services.perch import utilities as ny_utils
        rule = ny_utils.pod_id_rule("nyseg")
        check("the local NYSEG POD rule is intact", rule and rule["length"] == 15)
        check("  ...with the N01 prefix", rule["prefix"] == "N01")
        check("  ...and still validates a correct POD ID",
              ny_utils.validate_pod_id("nyseg", "N01123456789012") is None)
        check("  ...and still rejects a wrong one",
              ny_utils.validate_pod_id("nyseg", "N02123456789012") is not None)
        check("  ...and still rejects a short one",
              ny_utils.validate_pod_id("nyseg", "N0112345678901") is not None)
    check("the POD ID field markup is untouched",
          'id="pod-id-field" style="display:none;"' in HTML and 'id="c-pod"' in HTML)

    section("6d. STRICT next_step ROUTING AND BILL HANDLING UNCHANGED")
    check("the branch is still chosen only from Perch's next_step_key",
          "perchContext.nextStepKey ===" in JS)
    check("proof_docs still routes to the proof flow",
          "if(perchContext.nextStepKey === 'proof_docs'){" in JS)
    check("contracts still bypasses LMI entirely",
          "if(perchContext.nextStepKey === 'contracts'){" in JS)
    # STALE ASSERTION, corrected in Phase D (D-2). This pinned the literal
    # "Perch returned a next step we do not recognize", which was rep-facing
    # copy naming the vendor - exactly what D-2 removed. The BEHAVIOUR is what
    # this check is about and is unchanged: an unrecognised next_step throws
    # rather than guessing, and the enrollment is not advanced.
    check("an unrecognised next_step still fails safe",
          "could not be advanced automatically" in JS
          and "It has not changed" in JS)
    check("Virginia is NOT forced through self-attestation",
          "if(ref.sends_status === false)" in JS
          and "force" not in JS.split("function buildSelfAttestationPayload")[1][:600])
    check("multi-page bills still cap at Perch's 6 per account",
          "Math.min(pdf.numPages, 6)" in JS)
    check("  ...and every page is still checked against the 4 MB limit",
          "4 MB per page" in JS or "up to 4 MB" in JS)
    check("the adapter still omits status only when it is None",
          "if status is not None:" in ADAPTER)
    check("  ...and still validates a supplied status against the enum",
          "if status not in SELF_ATTESTATION_STATUSES:" in ADAPTER)

    section("7. NOTHING VIRGINIA-SPECIFIC IS HARDCODED IN THE FLOW")
    import ast
    def exec_src(text):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            b = node.body
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                node.body = b[1:] or [ast.Pass()]
        return ast.unparse(ast.fix_missing_locations(tree))

    routes_code = exec_src(ROUTES)
    adapter_code = exec_src(ADAPTER)
    check("perch_routes.py names no Dominion slug in executable code",
          "dominion" not in routes_code.lower())
    check("adapter.py names no Dominion slug in executable code",
          "dominion" not in adapter_code.lower())
    check("neither names a Virginia county",
          "martinsville" not in routes_code.lower()
          and "martinsville" not in adapter_code.lower())
    check("neither hardcodes a 'VA' comparison",
          '"VA"' not in routes_code and "'VA'" not in routes_code
          and '"VA"' not in adapter_code and "'VA'" not in adapter_code)
    check("the status rule is read from the market, not a state name",
          'markets.self_attestation_options(state)["status"]' in ROUTES)
    # REWRITTEN (Phase B), not loosened. The two sibling checks above test
    # "executable code" and get there by unparsing the AST with docstrings
    # dropped. This one read the RAW file, so it also failed on a COMMENT -
    # and Phase B added two, both naming "dominion-energy-va" as the example
    # of the raw slug that used to leak into the capacity card and the
    # agreement summary. A comment cannot hardcode a slug.
    #
    # Whole-line // comments only, exactly like this suite's JSN normalizer:
    # a slug written into real code still fails, including on a line that
    # merely ends with a trailing comment.
    JS_CODE = "\n".join(ln for ln in JS.splitlines()
                        if not ln.strip().startswith("//"))
    check("app.js names no Dominion slug in executable code",
          "dominion" not in JS_CODE.lower())
    check("app.js branches on sends_status, not on a state name",
          "ref.sends_status === false" in JS)
    # The deliberate state comparisons, documented as such. There are TWO
    # call sites - the self-attestation reference endpoint and the submit path
    # - and the label used to claim one, which was simply wrong. Both now
    # route their non-NY lookup through _market_counties(); what remains at
    # each site is only the branch choosing NY's own source.
    check("perch_routes.py compares NY at exactly the two county call sites",
          routes_code.count('!= "NY"') + routes_code.count("!= 'NY'") == 2)

    section("8. THE MOCK CLIENT CANNOT MAKE A LIVE CALL")
    # Mock mode must be incapable of reaching Perch, not merely unlikely to.
    # The two self-attestation methods were defined INSIDE the abstract base,
    # shadowing that class's own stubs, so PerchMockClient inherited live HTTP
    # bodies; the only thing stopping a request was the mock happening to lack
    # a `_requests` attribute. That is luck, not a boundary.
    import inspect
    from services.perch.client import PerchClient, PerchHTTPClient
    from services.perch.errors import PerchNotImplementedError

    base_dups = {}
    for cls in (PerchClient, PerchHTTPClient):
        seen = {}
        for node in ast.walk(ast.parse(inspect.getsource(cls))):
            if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
                for b in node.body:
                    if isinstance(b, ast.FunctionDef):
                        seen.setdefault(b.name, []).append(b.lineno)
        base_dups[cls.__name__] = {k: v for k, v in seen.items() if len(v) > 1}
    check(f"PerchClient defines each method once ({base_dups['PerchClient'] or 'no dups'})",
          not base_dups["PerchClient"])
    check("PerchHTTPClient defines each method once",
          not base_dups["PerchHTTPClient"])

    LMI = ("submit_self_attestation", "accept_self_attestation", "submit_proof_docs")
    for name in LMI:
        fn = getattr(PerchMockClient, name)
        src = inspect.getsource(fn)
        check(f"mock.{name} resolves to a stub, not an implementation "
              f"({fn.__qualname__})",
              "PerchNotImplementedError" in src)
        check(f"  ...and its body makes no request",
              "requests.post" not in src and "requests.get" not in src)

    mock = PerchMockClient()
    for name, args in (("submit_self_attestation", ("tok", {})),
                       ("accept_self_attestation", ("tok", {})),
                       ("submit_proof_docs", ("tok", {}))):
        try:
            getattr(mock, name)(*args)
            check(f"mock.{name} refuses explicitly", False)
        except PerchNotImplementedError:
            check(f"mock.{name} raises PerchNotImplementedError", True)
        except Exception as e:
            check(f"mock.{name} raises the RIGHT error, not "
                  f"{type(e).__name__}", False)

    check("the mock holds no HTTP plumbing at all",
          not any(hasattr(mock, a) for a in
                  ("_requests", "enrollment_base_url", "markets_base_url",
                   "api_key", "secret_key")))
    # Every method a caller could reach on the mock, checked wholesale rather
    # than naming the three we know about.
    leaked = []
    for name in dir(PerchMockClient):
        if name.startswith("__"):
            continue
        fn = getattr(PerchMockClient, name, None)
        if not callable(fn) or not hasattr(fn, "__code__"):
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "requests.post" in src or "requests.get" in src:
            leaked.append(f"{name} -> {fn.__qualname__}")
    check(f"NO method reachable on the mock contains an HTTP call "
          f"({leaked or 'none'})", not leaked)

    # Live behaviour preserved: the real implementations still exist, on the
    # HTTP client, and still issue the documented request.
    for name in ("submit_self_attestation", "accept_self_attestation"):
        fn = PerchHTTPClient.__dict__.get(name)
        check(f"PerchHTTPClient defines {name} itself", fn is not None)
        src = inspect.getsource(fn)
        check(f"  ...and it still POSTs to Perch", "requests.post" in src)
        check(f"  ...wrapping the payload as the spec requires",
              '"self_attestation"' in src)

    section("9. ONE COUNTY-SOURCE LOOKUP, NOT TWO COPIES")
    exec_routes = exec_src(routes_code)
    check("the non-NY county lookup exists in exactly one place",
          exec_routes.count("markets.counties(") == 1)
    check("  ...behind a named helper", "_market_counties" in routes_code)
    # E2.1 added a THIRD consumer: the Availability step's County field, which
    # filters county-restricted v2 capacity. It goes through the same helper,
    # which is the point of the helper - the lookup itself still exists in
    # exactly one place, as the check above asserts.
    check("  ...which every call site uses",
          exec_routes.count("_market_counties(") == 4)
    # COMMENTS LIVE ONLY IN THE RAW SOURCE. routes_code is ast.unparse output,
    # which drops every comment - so asserting a comment against it always
    # passes, whatever the file says. These two read ROUTES directly. (The
    # first version of this check used routes_code and was a false pass.)
    check("the stale 'one place' claim is gone from the comment",
          "the one place Phase 3 compares a state name" not in ROUTES)
    check("  ...replaced by one that says there are two call sites",
          "TWO call sites" in ROUTES)
    check("  ...and the vacuous form of this check cannot come back",
          "TWO call sites" not in routes_code)
    # The two endpoints' error payloads differ, and that difference is the
    # reason the helper takes a flag rather than being inlined twice.
    check("the helper preserves both error shapes",
          "with_state_in_error" in routes_code)

    restore_client_boundary()
    markets.set_client_factory(None)
    print(f"\n{'='*72}\nVIRGINIA ENROLLMENT SUPPORT - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
