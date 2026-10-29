"""
Phase E4 - real Perch v2 staging verification.

Runs the E4 scope IN ORDER against Perch staging, checks every assumption E1,
E2, E2.1, E3 and E3.1 were built on, and writes a transcript of every real
response so a mismatch can be read rather than guessed at.

    # 1. read-only pass. No enrollment is created.
    python scripts/verify_e4_v2_staging.py

    # 2. full pass, including real enrollments.
    python scripts/verify_e4_v2_staging.py --write

    # one scenario at a time
    python scripts/verify_e4_v2_staging.py --write --only=va

WHY READ-ONLY IS THE DEFAULT
    Items 1-5 of the E4 scope (token, capacity, program mapping, county,
    savings) can all be answered WITHOUT creating anything, and those are the
    items most likely to expose a wrong assumption. Running them first means a
    bad assumption is found before a single enrollment exists at Perch.
    `--write` then adds items 6-13, which genuinely require one.

SAFETY
    * PERCH_API_MODE must be `live` and PERCH_API_VERSION must be `v2`.
      Refuses to run otherwise.
    * REFUSES TO RUN AGAINST A PRODUCTION-LOOKING HOST. Staging only.
    * Calls the CLIENT directly, never the adapter: nothing is written to the
      Dalton database, so this cannot disturb real sales records.
    * A fresh UUID email per scenario per run, so a previous run's enrollment
      cannot interfere and the documented duplicate-email 422 cannot hit.
    * Test/example customer data only. No real customer is ever sent.
    * NEVER prints the API key, the signing key, or an enrollment token.
      Credentials are reported as SET / NOT SET, exactly as every other
      verifier in this directory does.

WHAT IT PRODUCES
    * a PASS / MISMATCH line for every assumption, so differences surface
      automatically instead of being eyeballed
    * `e4_staging_transcript_<timestamp>.json` next to this script, holding
      every real response body and every real error body
    * a closing summary laid out as the E4 report asks for it

A MISMATCH IS NOT A LICENCE TO REDESIGN. Each one prints the exact expected
and actual value so the smallest possible fix can be made against evidence.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_BILL = os.path.join(REPO_ROOT, "test", "sample_utility_bill.pdf")

# Hosts that must never be touched by this script.
#
# Matched against the HOSTNAME, exactly - never as a substring of the URL.
# `staging.api.perchenergy.com` contains `api.perchenergy.com`, so a substring
# test refuses the very host E4 is meant to run against. (Found by this
# script's own guard test, which is why the test exists.)
PRODUCTION_HOSTS = {"api.perchenergy.com", "perchenergy.com",
                    "www.perchenergy.com"}

# A host must POSITIVELY look like staging. "Not obviously production" is not
# the same as "safe", so anything unrecognised is refused rather than guessed
# at. Matched against the hostname's dot-separated labels, so `staging.api...`
# qualifies and a company called `testco.example` does not.
STAGING_LABELS = {"staging", "stage", "sandbox", "test", "dev", "qa",
                  "preview", "uat"}

# The five v2 capacity types and the five keys each entry must carry.
EXPECTED_CAPACITY_TYPES = {"residential", "lmi", "lmi_ira", "mass_market",
                           "commercial_and_industrial"}
EXPECTED_ENTRY_KEYS = {"customer_type", "county_restriction",
                       "capacity_available", "capacity_available_kw",
                       "savings_percents"}

# The v2 next_step paths UBS resolves. Anything else is a finding.
KNOWN_NEXT_STEPS = {"/capacity", "/enroll", "/lmi/proof_docs",
                    "/lmi/self_attestation", "/lmi/self_attestation/accept",
                    "/contracts", "/contracts/accept", "/status"}

# Scenarios. Each is a (zip, utility, label) the E4 scope names explicitly.
#
# VA is 22401 because that is the path REAL staging has already proved clean
# through /lmi/self_attestation/accept. An earlier value of 24112
# (Martinsville) was never the one the write passes ran against, and
# verifying a different ZIP than the one that was proved would re-open a
# question that is already answered. Override with --zip if a second VA
# location ever needs checking.
SCENARIOS = {
    "ny":  ("12202", "national-grid-ny", "New York / National Grid"),
    "va":  ("22401", "dominion-energy-va", "Virginia / Dominion"),
}

# The customer city that goes with each scenario's default ZIP. Kept beside
# the ZIP so the two cannot drift apart; --city overrides it when --zip does.
SCENARIO_CITY = {"ny": "Albany", "va": "Fredericksburg"}

# --program values -> the Perch customer_type they select.
PROGRAM_BY_FLAG = {
    "residential": "Residential",
    "lmi": "LMI",
    "lmi_ira": "LMI_IRA",
}

TEST_CUSTOMER = {
    "first_name": "Dalton",
    "last_name": "Testcustomer",
    "phone_number": "5185550142",
    "address_1": "123 Main St",
    "city": "Albany",
    "state": "NY",
    "address_zip": "12207",
    "account_number": "1234567890",
}

# Virginia's Dominion requires a 10-or-12 digit account AND a 10-digit meter.
# address_zip/city are filled in from the scenario at run time so the
# customer's address can never disagree with the ZIP being serviced.
VA_CUSTOMER = dict(TEST_CUSTOMER, state="VA", account_number="1234567890",
                   meter_number="0123456789")

RESULTS = []        # (section, label, status, detail)
TRANSCRIPT = {"started_at": datetime.now().isoformat(), "calls": [],
              "findings": []}


# ─────────────────────────── reporting ───────────────────────────

def rule(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def record(section, label, ok, detail=""):
    status = "PASS" if ok else "MISMATCH"
    RESULTS.append((section, label, status, detail))
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        TRANSCRIPT["findings"].append({"section": section, "label": label,
                                       "detail": detail})
    return ok


def note(section, label, detail):
    """A fact worth recording that is not pass/fail - e.g. 'staging has no
    lmi_ira capacity here'. These become the 'not verified' list."""
    RESULTS.append((section, label, "NOTE", detail))
    print(f"  [NOTE] {label}" + (f"  -- {detail}" if detail else ""))


def capture(name, payload):
    """Store a real response verbatim for later analysis."""
    TRANSCRIPT["calls"].append({"call": name, "at": datetime.now().isoformat(),
                                "response": _safe(payload)})


def _safe(value):
    """Strip anything secret before it reaches the transcript.

    Enrollment tokens are session credentials. They are replaced by their
    length so a shape problem is still visible without writing a usable token
    to a file on disk.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("enrollment_token", "token", "api_key", "secret_key"):
                out[k] = f"<redacted len={len(str(v))}>"
            else:
                out[k] = _safe(v)
        return out
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def error_body(exc):
    """Everything Perch returned on an error. Response only - never request
    headers, which carry the credentials."""
    body = {
        "exception": type(exc).__name__,
        "message": str(exc),
        "http_status": getattr(exc, "status_code", None),
        "content_type": getattr(exc, "content_type", None),
        "request_id": getattr(exc, "request_id", None),
        "body_json": getattr(exc, "body_json", None),
        "body_text": (getattr(exc, "body_text", None) or "")[:4000] or None,
    }
    TRANSCRIPT["calls"].append({"call": "ERROR", "at": datetime.now().isoformat(),
                                "response": _safe(body)})
    print("\n  --- REAL ERROR BODY ---")
    print("  " + json.dumps(_safe(body), indent=2)[:3000].replace("\n", "\n  "))
    return body


# ─────────────────────────── preflight ───────────────────────────

def preflight():
    """Refuse to run unless this is unmistakably v2 staging."""
    from services.perch.config import (get_api_mode, get_api_version,
                                       get_enrollment_base_url,
                                       get_markets_base_url,
                                       get_markets_capacity_base_url)
    rule("PREFLIGHT")

    mode, version = get_api_mode(), get_api_version()
    print(f"  PERCH_API_MODE        : {mode}")
    print(f"  PERCH_API_VERSION     : {version}")
    if mode != "live":
        print("\n  PERCH_API_MODE is not 'live'. Refusing to run.")
        sys.exit(2)
    if version != "v2":
        print("\n  PERCH_API_VERSION is not 'v2'. E4 verifies the v2 contract;"
              "\n  set PERCH_API_VERSION=v2. Refusing to run.")
        sys.exit(2)

    enrollment = get_enrollment_base_url()
    markets = get_markets_base_url()
    capacity_base = get_markets_capacity_base_url()
    print(f"  enrollment base       : {enrollment}")
    print(f"  markets base          : {markets}")
    print(f"  markets capacity base : {capacity_base}")

    # Credentials: SET / NOT SET only. Never the values.
    print(f"  PERCH_API_KEY         : "
          f"{'SET' if os.environ.get('PERCH_API_KEY') else 'NOT SET'}")
    print(f"  PERCH_SECRET_KEY      : "
          f"{'SET' if os.environ.get('PERCH_SECRET_KEY') else 'NOT SET'}")
    if not (os.environ.get("PERCH_API_KEY") and os.environ.get("PERCH_SECRET_KEY")):
        print("\n  Credentials are not set. Refusing to run.")
        sys.exit(2)

    # THE PRODUCTION GUARD. Both tests run on the HOSTNAME, not the URL text.
    for url in (enrollment, markets, capacity_base):
        host = (urlparse(url).hostname or "").lower()
        if host in PRODUCTION_HOSTS:
            print(f"\n  {url}\n  is a PRODUCTION host. Refusing to run. E4 is "
                  f"staging only.")
            sys.exit(2)
        if not (set(host.split(".")) & STAGING_LABELS):
            print(f"\n  {url}\n  does not look like a staging host "
                  f"(no staging label in {host!r}). Refusing to run rather "
                  f"than guessing.")
            sys.exit(2)

    print("\n  Staging confirmed. v2 confirmed. Credentials present.")
    TRANSCRIPT["bases"] = {"enrollment": enrollment, "markets": markets,
                           "markets_capacity": capacity_base,
                           "api_version": version}
    return True


# ───────────────────── 1. token ─────────────────────

def step_token(client, email):
    rule("1. POST /token  (E4 item 1)")
    from services.perch.errors import PerchError
    print(f"  email : {email}")
    try:
        tok = client.request_token(email)
    except PerchError as e:
        record("token", "POST /token succeeded", False, f"{type(e).__name__}")
        error_body(e)
        return None
    capture("POST /token", tok)

    token = tok.get("enrollment_token")
    record("token", "a token was returned", bool(token),
           f"length {len(token) if token else 0}")
    record("token", "response_shape is reported", "response_shape" in tok,
           str(tok.get("response_shape")))

    # The v2 token response documents next_step. E1 normalises both spellings;
    # this records which one staging actually uses.
    raw_next = tok.get("next_step") or tok.get("next_step_url")
    note("token", "real next_step from /token", repr(raw_next))
    if raw_next:
        record("token", "next_step is a BARE PATH on v2 (not a full URL)",
               not str(raw_next).lower().startswith("http"),
               f"got {raw_next!r}")
        record("token", "next_step is one UBS resolves",
               _resolves(raw_next), f"got {raw_next!r}")
    return token


def _resolves(raw):
    from services.perch.workflow import resolve_next_step_key
    key, recognized = resolve_next_step_key(raw)
    return bool(recognized and key)


# ───────────────────── 2. capacity ─────────────────────

def step_capacity(client, token, zip_code, utility):
    rule(f"2. POST /capacity  (E4 item 2)   zip={zip_code} utility={utility}")
    from services.perch.errors import PerchError
    try:
        cap = client.check_capacity(token, zip_code, utility)
    except PerchError as e:
        record("capacity", "POST /capacity succeeded", False,
               f"{type(e).__name__}")
        error_body(e)
        return None
    capture("POST /capacity", cap)

    details = cap.get("project_details")
    print(f"\n  --- REAL project_details ---")
    print("  " + json.dumps(details, indent=2)[:4000].replace("\n", "\n  "))

    # THE assumption E2 is built on.
    is_list = isinstance(details, list)
    record("capacity", "project_details is an ARRAY (v2 shape)", is_list,
           f"got {type(details).__name__}")
    if not is_list:
        note("capacity", "v2 returned the v1 flat shape",
             "every E2 expectation below is unverifiable for this ZIP")
        return cap

    seen = {e.get("customer_type") for e in details if isinstance(e, dict)}
    note("capacity", "customer types actually returned", sorted(x for x in seen if x))
    record("capacity", "every documented customer type is present",
           EXPECTED_CAPACITY_TYPES.issubset(seen),
           f"missing {sorted(EXPECTED_CAPACITY_TYPES - seen)}")
    unknown = seen - EXPECTED_CAPACITY_TYPES - {None}
    record("capacity", "no UNDOCUMENTED customer type appeared", not unknown,
           f"unexpected {sorted(unknown)}")

    for i, entry in enumerate(details):
        if not isinstance(entry, dict):
            record("capacity", f"entry {i} is an object", False,
                   f"got {type(entry).__name__}")
            continue
        missing = EXPECTED_ENTRY_KEYS - set(entry)
        record("capacity", f"entry {i} ({entry.get('customer_type')}) has every "
               f"documented key", not missing, f"missing {sorted(missing)}")

        counties = entry.get("county_restriction")
        record("capacity", f"entry {i} county_restriction is null or a list",
               counties is None or isinstance(counties, list),
               f"got {type(counties).__name__}")

        rates = entry.get("savings_percents")
        if isinstance(rates, list):
            numeric = all(isinstance(r, (int, float)) for r in rates)
            record("capacity", f"entry {i} savings_percents are numbers",
                   numeric, f"got {rates!r}")
            if numeric:
                record("capacity", f"entry {i} savings_percents sorted ascending",
                       list(rates) == sorted(rates), f"got {rates!r}")
        else:
            record("capacity", f"entry {i} savings_percents is a list", False,
                   f"got {type(rates).__name__}")

    # County restrictions, reported either way - their presence is what E2.1
    # keys off, and their ABSENCE in staging is itself a finding.
    restricted = [e for e in details
                  if isinstance(e, dict) and e.get("county_restriction")]
    if restricted:
        note("capacity", "county-RESTRICTED entries found",
             json.dumps([{e.get("customer_type"): e.get("county_restriction")}
                         for e in restricted])[:600])
    else:
        note("capacity", "NO county-restricted capacity at this ZIP",
             "E2.1's restricted path cannot be proven here; try another ZIP")

    multi = [e for e in details if isinstance(e, dict)
             and len(e.get("savings_percents") or []) > 1]
    if multi:
        note("capacity", "entries with MULTIPLE savings rates",
             json.dumps([{e.get("customer_type"): e.get("savings_percents")}
                         for e in multi])[:600])
    else:
        note("capacity", "NO multi-rate entry at this ZIP",
             "E3's required-savings_percent path cannot be proven here")

    raw_next = cap.get("next_step")
    note("capacity", "real next_step from /capacity", repr(raw_next))
    if raw_next:
        record("capacity", "next_step is a BARE PATH on v2",
               not str(raw_next).lower().startswith("http"), f"got {raw_next!r}")
        record("capacity", "next_step is one UBS resolves", _resolves(raw_next),
               f"got {raw_next!r}")
    return cap


# ───────────────────── 3. program mapping ─────────────────────

def step_program_mapping(cap, zip_code):
    rule("3. Program mapping  (E4 item 3)")
    from services.perch import adapter, capacity as cap_module
    details = (cap or {}).get("project_details")

    # THROUGH PRODUCTION'S OWN HYDRATION PATH, not a bare parse.
    #
    # capacity_model_for() is what every real caller uses, and it is what
    # annotates the model with its MARKET - which is what applies the
    # market product rules (adapter.offerable_customer_types).
    #
    # An earlier version of this script called cap_module.parse() directly.
    # That silently skipped the annotation, so for Virginia it mapped the real
    # staging response to [Residential, LMI] and would have gone on to enrol a
    # NON-LMI Virginia customer - the exact case the E4 fix exists to prevent.
    # A verifier that does not go through the code under test verifies nothing.
    model = adapter.capacity_model_for({"zip_code": zip_code,
                                        "capacity_available": True,
                                        "project_details": details})
    record("mapping", "UBS parses the real response as the v2 entry shape",
           model.shape == cap_module.SHAPE_V2_ENTRIES, f"got {model.shape}")
    note("mapping", "market resolved from the ZIP", repr(model.market_state))

    programs = adapter.available_programs(model)
    print("\n  --- programs UBS would offer a rep ---")
    for p in programs:
        print(f"    {p['label']:26} customer_type={p['customer_type']:12} "
              f"rates={p['savings_percents']}  source={p['savings_source']}"
              + ("  UNRESOLVED" if p["savings_unresolved"] else ""))
    if not programs:
        note("mapping", "no sellable program at this ZIP",
             "capacity exists but not on a type UBS offers")

    # Which programs this MARKET allows, so a program that is absent because
    # of a product rule is distinguishable from one absent for lack of
    # capacity. Virginia is income-qualified only.
    allowed = adapter.offerable_customer_types(model.market_state)
    note("mapping", "programs this market may offer", str(list(allowed)))
    withheld = [t for t in adapter.SELECTABLE_CUSTOMER_TYPES
                if t not in allowed]
    if withheld:
        note("mapping", "withheld by the market product rule", str(withheld))
        for t in withheld:
            record("mapping", f"{t} is NOT offered in this market",
                   t not in [p["customer_type"] for p in programs])

    for p in programs:
        record("mapping", f"{p['customer_type']} label is rep-facing",
               "_" not in p["label"], p["label"])
    record("mapping", "Business is never offered",
           all(p["customer_type"] != "Business" for p in programs))

    unresolved = [p["customer_type"] for p in programs if p["savings_unresolved"]]
    if unresolved:
        note("mapping", "programs with NO authoritative rate", str(unresolved)
             + " - E2.1 leaves these empty rather than substituting")
    return programs, model


# ───────────────────── 4/5. county + savings (read-only) ─────────────────────

def step_markets(client, state):
    rule(f"4. Market county list  (E4 item 4)   state={state}")
    from services.perch.errors import PerchError
    try:
        found = client.get_market_counties(state)
    except PerchError as e:
        record("county", f"GET /markets/counties?state={state}", False,
               type(e).__name__)
        error_body(e)
        return []
    capture(f"GET /markets/counties?state={state}", found)
    counties = found.get("counties") or []
    note("county", f"{state} county count", str(len(counties)))
    note("county", f"{state} first few, EXACT spelling", json.dumps(counties[:6]))
    record("county", "county names are non-empty strings",
           all(isinstance(c, str) and c.strip() for c in counties))
    return counties


def step_county_cross_check(model, counties):
    """Do the county names in the capacity response match the market list?

    This is the join E2.1 and E3 depend on: a restriction names a county, the
    rep picks from the market list, and the two must be the same strings.
    """
    rule("4b. County spelling cross-check  (E4 item 4)")
    from services.perch import capacity as cap_module
    named = sorted({c for t in cap_module.CAPACITY_TYPES
                    for c in model.counties_for(t)})
    if not named:
        note("county", "no county appears in this capacity response",
             "nothing to cross-check at this ZIP")
        return
    note("county", "counties named by capacity restrictions", json.dumps(named))
    lower = {c.strip().lower() for c in counties}
    for name in named:
        record("county", f"restriction county {name!r} is in the market list",
               name.strip().lower() in lower,
               "EXACT-SPELLING MISMATCH - E3 sends the market spelling")


# ───────────────────── 6. enroll ─────────────────────

def step_enroll(client, token, cap, model, zip_code, utility, email,
                customer, county=None, wanted_program=None, wanted_rate=None):
    rule("6. POST /enroll  (E4 item 6)")
    from services.perch import adapter
    from services.perch.client import build_enrollment_multipart
    from services.perch.errors import PerchError, PerchValidationError

    programs = adapter.available_programs(model, county=county)
    if not programs:
        note("enroll", "skipped", "no sellable program for this ZIP/county")
        return None, None

    offered = [p["customer_type"] for p in programs]

    # WHICH PROGRAM - stated, never assumed.
    #
    # An earlier version took programs[0]. That is fine when a location offers
    # one program and silently arbitrary when it offers three, which is
    # exactly the kind of choice a verification run must not make on its own:
    # the enrollment it creates at Perch would not be the one anybody asked
    # for.
    if wanted_program:
        if wanted_program not in offered:
            record("enroll", f"--program={wanted_program} is offered here",
                   False, f"this location offers {offered}")
            return None, None
        chosen = next(p for p in programs if p["customer_type"] == wanted_program)
    elif len(programs) == 1:
        chosen = programs[0]
        note("enroll", "one program offered, so no choice to make",
             chosen["customer_type"])
    else:
        record("enroll", "a program was chosen", False,
               f"this location offers {offered} - re-run with --program="
               + "|".join(k for k, v in PROGRAM_BY_FLAG.items() if v in offered))
        note("enroll", "STOPPED rather than choosing",
             "no enrollment was created")
        return None, None
    customer_type = chosen["customer_type"]

    # WHICH RATE - likewise.
    rates = chosen["savings_percents"]
    note("enroll", f"real rates published for {customer_type}", str(rates))
    if wanted_rate is not None:
        if wanted_rate not in rates:
            record("enroll", f"--rate={wanted_rate} is published for "
                   f"{customer_type}", False, f"real rates are {rates}")
            note("enroll", "STOPPED rather than sending an unpublished rate",
                 "no enrollment was created")
            return None, None
        selected_rate = wanted_rate
    elif len(rates) > 1:
        record("enroll", "a savings rate was chosen", False,
               f"{customer_type} publishes {rates} - re-run with --rate=<one "
               f"of those>")
        note("enroll", "STOPPED rather than choosing between real rates",
             "no enrollment was created")
        return None, None
    else:
        selected_rate = None          # 0 or 1 rate: nothing to choose

    try:
        rate, why = adapter.resolve_savings_percent(
            model, customer_type, county=county, selected=selected_rate)
    except PerchValidationError as e:
        record("enroll", "UBS could resolve a savings rate", False, str(e))
        return None, None
    note("enroll", "savings decision", f"{rate!r} ({why})")
    if wanted_rate is not None:
        record("enroll", "the REQUESTED rate is what UBS resolved",
               rate == wanted_rate, f"asked {wanted_rate}, resolved {rate}")

    payload = {
        "email_address": email,
        "first_name": customer["first_name"],
        "last_name": customer["last_name"],
        "phone_number": customer["phone_number"],
        "customer_type": customer_type,
        "utility_name": utility,
        "zip_code": zip_code,
        "billing_address": {"address_1": customer["address_1"],
                            "city": customer["city"],
                            "state": customer["state"],
                            "zip": customer["address_zip"]},
        "utility_accounts": [{
            "utility_account_number": customer["account_number"],
            "service_address": {"address_1": customer["address_1"],
                                "city": customer["city"],
                                "state": customer["state"],
                                "zip": customer["address_zip"]},
            "utility_bills": [SAMPLE_BILL] if os.path.exists(SAMPLE_BILL) else [],
        }],
    }
    if customer.get("meter_number"):
        payload["utility_accounts"][0]["meter_numbers"] = [customer["meter_number"]]
    if county:
        payload["county"] = county
    if rate is not None:
        payload["savings_percent"] = rate

    form, files = build_enrollment_multipart(payload)
    print("\n  --- the multipart UBS is about to send (field names only) ---")
    print(f"  fields : {sorted(form)}")
    print(f"  files  : {sorted(files)}")
    record("enroll", "county is in the payload when UBS has one",
           ("county" in form) == bool(county),
           f"county={'present' if 'county' in form else 'absent'}")
    record("enroll", "savings_percent is in the payload when UBS resolved one",
           ("savings_percent" in form) == (rate is not None))

    try:
        result = client.create_enrollment(token, form, files)
    except PerchError as e:
        record("enroll", "Perch ACCEPTED the payload", False, type(e).__name__)
        body = error_body(e)
        _compare_error_to_translation(body)
        return None, None
    capture("POST /enroll", result)

    record("enroll", "Perch ACCEPTED the payload", True,
           f"customer_type={customer_type} county={county!r} rate={rate!r}")
    raw_next = result.get("next_step")
    note("enroll", "real next_step from /enroll", repr(raw_next))
    record("enroll", "next_step is one UBS resolves", _resolves(raw_next),
           f"got {raw_next!r}")
    return result, customer_type


def _compare_error_to_translation(body):
    """Hold a real staging error against E3's translation table."""
    from services.perch import client as perch_client
    errors = (body.get("body_json") or {}).get("errors")
    if not isinstance(errors, list):
        note("errors", "no field-error list in this body",
             "E3's errors[] path is not exercised by this response")
        return
    print("\n  --- E3 translation of this REAL error ---")
    for item in errors:
        if not isinstance(item, dict):
            continue
        field, code = item.get("field"), item.get("code")
        mapped = (perch_client._FIELD_ERROR_TEXT.get((field, code))
                  or perch_client._FIELD_ERROR_TEXT.get((field, None)))
        tier = ("field+code" if mapped
                else ("code-default" if code in perch_client._CODE_DEFAULT_TEXT
                      else "message fallback"))
        text = perch_client._field_error_text(item)
        print(f"    ({field!r}, {code!r})  via {tier}\n      -> {text}")
        record("errors", f"({field}, {code}) produces rep-facing text",
               bool(text) and "_" not in text.split(":")[0],
               f"tier={tier}")
        if tier == "message fallback":
            note("errors", f"({field}, {code}) is NOT in E3's table",
                 "add it only if this is a real recurring case")


# ───────────────────── 7-12. the rest of the flow ─────────────────────

# How many next_step hops to follow before calling it a loop. Perch's
# documented v2 chain is at most enroll -> self_attestation ->
# self_attestation/accept -> contracts -> contracts/accept -> status.
MAX_HOPS = 8

# The hops that CHANGE something irreversibly at Perch. Each is refused
# unless --accept was typed. Generating a document is not on this list:
# generation is repeatable, acceptance is a signature.
ACCEPTANCE_STEPS = ("self_attestation_accept", "contracts_accept")


def _acceptance_metadata():
    """The acceptance metadata, in production's shape.

    Built from client.acceptance_timestamp() - the same single source of
    truth the Dalton route uses - so this verifies production's timestamp
    behavior rather than a near-copy that can drift. Perch 422'd a
    timestamp generated at the exact instant of submission; that skew
    allowance lives in that function, not here.
    """
    from services.perch.client import acceptance_timestamp
    return {
        "ip_address": "198.51.100.10",      # RFC 5737 documentation range
        "timestamp": acceptance_timestamp(),
        "user_agent": "DaltonSolar-E4-StagingVerifier/1.0",
    }


def _hop_self_attestation(client, token, state, county, accept):
    rule("10. Self-attestation  (E4 item 10)")
    from services.perch import adapter, markets
    from services.perch.errors import PerchError

    # SOURCE TYPE AND STATUS COME FROM THE MARKET, NOT FROM THIS SCRIPT.
    #
    # An earlier version of this block hardcoded
    # lmi_source_selection=["self_attestation"] - a literal invented here,
    # not the value the market publishes - and real VA staging refused it:
    #     "self_attestation is not a valid source type in VA"
    # VA publishes ["self_attestation_qualifying_income"], which is exactly
    # what production sends (adapter.SELF_ATTESTATION_SOURCE). The
    # PRODUCTION PATH WAS NEVER WRONG; only this verifier was, and it hid
    # the county question behind a rejection it caused itself.
    try:
        options = markets.self_attestation_options(state)
    except Exception as e:                       # noqa: BLE001
        record("attestation", "market self-attestation options fetched",
               False, f"{type(e).__name__}: {e}")
        note("attestation", "county question NOT reached",
             "cannot build a valid payload without the market's options")
        return None
    capture(f"GET /markets/self_attestation_options?state={state}", options)

    source_types = options.get("source_types") or []
    status_values = options.get("status") or []
    note("attestation", f"{state} source_types (REAL)", json.dumps(source_types))
    note("attestation", f"{state} status values (REAL)", json.dumps(status_values))
    note("attestation", f"{state} single_source_type",
         str(options.get("single_source_type")))

    # The check that would have caught this, and will catch a future
    # divergence between production's constant and the live market.
    record("attestation",
           "production's source type is one the market accepts",
           adapter.SELF_ATTESTATION_SOURCE in source_types,
           f"production sends {adapter.SELF_ATTESTATION_SOURCE!r}; "
           f"{state} accepts {source_types}")

    if not source_types:
        note("attestation", "the market published no source type",
             "nothing valid to send; county question not reached")
        return None
    chosen_source = (adapter.SELF_ATTESTATION_SOURCE
                     if adapter.SELF_ATTESTATION_SOURCE in source_types
                     else source_types[0])
    # single_source_type is true in both markets, so it is sent as a
    # one-element list - the same shape production builds.
    selection = [chosen_source]

    entry = {
        "utility_account_number": TEST_CUSTOMER["account_number"],
        "occupancy": 2,
        "lmi_source_selection": selection,
    }
    # STATUS: sent only when the market defines values for it. VA
    # publishes [], so the key is omitted entirely - not null, not empty
    # string, ABSENT. This mirrors adapter.submit_self_attestation.
    if status_values:
        entry["status"] = status_values[0]
        note("attestation", "status INCLUDED", repr(entry["status"]))
    else:
        note("attestation", "status OMITTED",
             f"{state} publishes an empty status list")

    if not county:
        note("attestation", "no county was bound at /enroll",
             "v2 requires one here; skipping rather than inventing one")
        return None

    note("attestation", "county OMITTED from the entry",
         f"bound at /enroll as {county!r}; the v2 spec says Perch reuses it")
    print(f"\n  --- the self-attestation entry being sent ---")
    print("  " + json.dumps(entry, indent=2).replace("\n", "\n  "))

    try:
        sa = client.submit_self_attestation(token, entry)
    except PerchError as e:
        body = error_body(e)
        _compare_error_to_translation(body)
        # ONLY a rejection ON THE COUNTY FIELD is evidence about county.
        # Anything else means the request failed before the county
        # question was reached, and saying otherwise would turn a payload
        # bug into false evidence that the YAML is wrong - which is
        # exactly what the hardcoded source type did on the first run.
        fields = {item.get("field") for item in
                  ((body.get("body_json") or {}).get("errors") or [])
                  if isinstance(item, dict)}
        if "county" in fields:
            record("attestation",
                   "Perch accepted the entry WITHOUT a county "
                   "(E3.1 assumption)", False,
                   "REJECTED ON county - this IS county evidence")
            note("attestation", "ACTION",
                 "Perch requires county here, contradicting the YAML - "
                 "report the body above BEFORE changing code")
        else:
            record("attestation", "the self-attestation payload was valid",
                   False, f"rejected on {sorted(f for f in fields if f)}")
            note("attestation", "COUNTY QUESTION NOT REACHED",
                 "this rejection is NOT about county; it says nothing "
                 "about whether omitting county works")
        return None

    capture("POST /lmi/self_attestation", sa)
    record("attestation",
           "Perch accepted the entry WITHOUT a county (E3.1 assumption)",
           True, "the enrolled county was reused, as documented")
    note("attestation", "real next_step", repr(sa.get("next_step")))
    return sa.get("next_step") or sa.get("next_step_url")


def _hop_self_attestation_accept(client, token, state, county, accept):
    rule("10b. Self-attestation ACCEPTANCE")
    from services.perch.errors import PerchError
    if not accept:
        note("attestation", "ACCEPTANCE NOT ATTEMPTED",
             "Perch's real next_step is /lmi/self_attestation/accept; "
             "re-run with --accept to accept it")
        return None
    metadata = _acceptance_metadata()
    note("attestation", "acceptance timestamp (production's own helper)",
         repr(metadata["timestamp"]))
    try:
        accepted = client.accept_self_attestation(token, metadata)
    except PerchError as e:
        record("attestation", "POST /lmi/self_attestation/accept succeeded",
               False, type(e).__name__)
        _compare_error_to_translation(error_body(e))
        return None
    capture("POST /lmi/self_attestation/accept", accepted)
    record("attestation", "self-attestation was ACCEPTED", True,
           "Perch accepted production's metadata shape")
    nxt = accepted.get("next_step") or accepted.get("next_step_url")
    note("attestation", "REAL next_step after acceptance", repr(nxt))
    record("attestation", "that next_step is one UBS resolves",
           _resolves(nxt) if nxt else True, f"got {nxt!r}")
    return nxt


def _hop_contracts(client, token, state, county, accept):
    rule("11. Contracts generation  (E4 item 11)")
    from services.perch.errors import PerchError
    try:
        contracts = client.generate_contracts(token)
    except PerchError as e:
        record("contracts", "POST /contracts succeeded", False,
               type(e).__name__)
        _compare_error_to_translation(error_body(e))
        return None
    capture("POST /contracts", contracts)
    items = contracts.get("contracts") or []
    record("contracts", "a contract list was returned", bool(items),
           f"{len(items)} contract(s)")
    note("contracts", "contract names", json.dumps(
        [c.get("contract_name") if isinstance(c, dict) else str(c)
         for c in items])[:500])
    nxt = contracts.get("next_step") or contracts.get("next_step_url")
    note("contracts", "REAL next_step", repr(nxt))
    return nxt


def _hop_contracts_accept(client, token, state, county, accept):
    rule("11b. Contract ACCEPTANCE")
    from services.perch.errors import PerchError
    if not accept:
        note("contracts", "ACCEPTANCE NOT ATTEMPTED",
             "Perch's real next_step is /contracts/accept; re-run with "
             "--accept to sign them")
        return None
    metadata = _acceptance_metadata()
    note("contracts", "acceptance timestamp (production's own helper)",
         repr(metadata["timestamp"]))
    try:
        accepted = client.accept_contracts(token, metadata)
    except PerchError as e:
        record("contracts", "POST /contracts/accept succeeded", False,
               type(e).__name__)
        _compare_error_to_translation(error_body(e))
        return None
    capture("POST /contracts/accept", accepted)
    record("contracts", "contracts were ACCEPTED", True,
           "Perch accepted production's metadata shape")
    nxt = accepted.get("next_step") or accepted.get("next_step_url")
    note("contracts", "REAL next_step after acceptance", repr(nxt))
    return nxt


def _hop_proof_docs(client, token, state, county, accept):
    rule("8. LMI proof-document routing  (E4 item 8)")
    note("flow", "staging routed to /lmi/proof_docs",
         "ROUTING PROVEN: this market really does ask for proof documents")
    record("proof_docs", "Perch routed this program to proof documents", True,
           "real next_step was /lmi/proof_docs - routing verified")
    note("proof_docs", "UPLOAD NOT ATTEMPTED - deliberately",
         "a proof document is a real qualifying income document. This "
         "runner has no staging-safe fixture and will not fabricate one, "
         "so routing is as far as it goes.")
    return None                      # stop here, by instruction


def _hop_status(client, token, state, county, accept):
    note("flow", "Perch's next_step is /status",
         "the enrollment has no further action - verified below")
    return None


HOPS = {
    "self_attestation": _hop_self_attestation,
    "self_attestation_accept": _hop_self_attestation_accept,
    "contracts": _hop_contracts,
    "contracts_accept": _hop_contracts_accept,
    "proof_docs": _hop_proof_docs,
    "status": _hop_status,
}


def step_after_enroll(client, token, next_step, state, county, accept=False):
    """Follows Perch's REAL next_step, hop by hop, until it runs out.

    PERCH'S next_step IS THE ROUTER. This function never predicts the chain
    and never assumes the documented order: it resolves whatever Perch just
    returned, runs the handler for that step, and resolves whatever THAT
    returned. An unrecognized step stops the run and is reported as a
    finding rather than guessed at.

    Nothing in ACCEPTANCE_STEPS runs unless accept=True, which only
    --accept sets.
    """
    rule("7-11. Post-enroll flow, following Perch's real next_step")
    from services.perch.workflow import resolve_next_step_key

    note("flow", "acceptance steps",
         "ENABLED (--accept)" if accept
         else "DISABLED - documents will be generated, nothing accepted")

    visited = []
    step = next_step
    for hop in range(1, MAX_HOPS + 1):
        if not step:
            note("flow", f"hop {hop}", "Perch returned no further next_step")
            break
        key, recognized = resolve_next_step_key(step)
        note("flow", f"hop {hop}: Perch said", f"{step!r} -> {key!r}")
        if not recognized or key not in HOPS:
            record("flow", f"next_step {step!r} is one UBS implements", False,
                   f"resolved to {key!r} - THIS IS A FINDING, stopping")
            break
        if key in visited:
            record("flow", "the next_step chain terminates", False,
                   f"{key!r} was already visited: {visited + [key]}")
            break
        visited.append(key)
        if key in ACCEPTANCE_STEPS and not accept:
            HOPS[key](client, token, state, county, accept)
            note("flow", "STOPPED before acceptance",
                 "--accept was not given, which is why this stops here")
            break
        step = HOPS[key](client, token, state, county, accept)
    else:
        record("flow", "the next_step chain terminates", False,
               f"still going after {MAX_HOPS} hops: {visited}")

    note("flow", "steps actually walked", " -> ".join(visited) or "(none)")
    return visited


def step_status(client, token, expect_completed=False):
    rule("12. GET /status and resume  (E4 item 12)")
    from services.perch.errors import PerchError
    try:
        status = client.get_status(token)
    except PerchError as e:
        record("status", "GET /status succeeded", False, type(e).__name__)
        error_body(e)
        return False
    capture("GET /status", status)
    print("  " + json.dumps(_safe(status), indent=2)[:2000].replace("\n", "\n  "))
    raw_next = status.get("next_step") or status.get("next_step_url")
    note("status", "real next_step from /status", repr(raw_next))
    if raw_next:
        record("status", "next_step is one UBS resolves", _resolves(raw_next),
               f"got {raw_next!r}")

    # COMPLETION. Only asserted when acceptance was actually performed -
    # a run without --accept is SUPPOSED to stop short, and calling that
    # incomplete enrollment a mismatch would be reporting the flag as a bug.
    completed = status.get("completed")
    note("status", "completed", repr(completed))
    if expect_completed:
        record("status", "the enrollment reports completed=true",
               completed is True,
               f"after full acceptance, /status says completed={completed!r}")
    else:
        note("status", "completion NOT asserted",
             "this run stopped before acceptance, so an incomplete "
             "enrollment is the expected outcome")
    # Returned so the resume step can ask Perch the right question. What
    # /status actually SAYS decides this, not what the run intended.
    return completed is True


# What Perch says when asked to resume an enrollment that has finished.
# Observed live on 2026-10-28 after the completed NY and VA runs:
#     PerchNotFoundError: "Perch has no in-progress enrollment for this
#     email address."
#
# Recognised by EXCEPTION TYPE first. PerchNotFoundError is production's own
# documented 404 for this endpoint - errors.py states that on
# PATCH /refresh_token it means "Perch has no in-progress enrollment for
# that email - so there is nothing to resume". That is a contract, where a
# message substring is only wording Perch could reword tomorrow. The phrase
# is still accepted as a second route in, so a differently-typed but
# identically-worded answer is not mistaken for a defect.
NO_IN_PROGRESS = "no in-progress enrollment"


def _is_nothing_to_resume(exc, body):
    from services.perch.errors import PerchNotFoundError
    if isinstance(exc, PerchNotFoundError):
        return True
    haystack = " ".join(str(body.get(k) or "") for k in
                        ("message", "body_text", "body_json")).lower()
    return NO_IN_PROGRESS in haystack


def step_resume(client, email, completed=False):
    """Token refresh / resume.

    REFRESH IS A QUESTION ABOUT AN IN-PROGRESS ENROLLMENT.

    Once /status reports completed=true there is nothing in progress to
    resume, and Perch says so with its documented 404. Treating that answer
    as a mismatch was the verifier asking the wrong question of a finished
    enrollment and then reporting the correct answer as a defect - the one
    blemish on two otherwise clean completed runs. Production was never
    involved: no route refreshes a completed enrollment, and errors.py
    already documents this 404 as "nothing to resume".

    For an INCOMPLETE enrollment nothing below has changed: a refresh must
    still succeed, must still return a token, and any failure is still a
    mismatch with the real body printed.
    """
    rule("12b. Token refresh / resume  (E4 item 12)")
    from services.perch.errors import PerchError

    if completed:
        note("resume", "this enrollment is COMPLETE",
             "resume applies to in-progress enrollments; Perch is expected "
             "to report that there is nothing to resume")
    try:
        refreshed = client.refresh_token(email)
    except PerchError as e:
        body = error_body(e)
        if completed and _is_nothing_to_resume(e, body):
            record("resume",
                   "a completed enrollment reports nothing left to resume",
                   True, f"{type(e).__name__}: the documented "
                         f"completed-state answer, not a mismatch")
            return None
        # Unchanged for an in-progress enrollment, and still reported for a
        # completed one that failed for some OTHER reason.
        record("resume", "PATCH /refresh_token succeeded", False,
               type(e).__name__)
        return None
    capture("PATCH /refresh_token", refreshed)
    token = refreshed.get("enrollment_token")
    if completed:
        note("resume", "Perch returned a token for a COMPLETED enrollment",
             "harmless, and a note rather than a check: what Perch does with "
             "a finished enrollment is its decision, not a UBS contract")
        return token
    record("resume", "a token was returned for the in-progress enrollment",
           bool(token), f"length {len(token) if token else 0}")
    note("resume", "real next_step from refresh",
         repr(refreshed.get("next_step") or refreshed.get("next_step_url")))
    return token


# ─────────────────────────── driver ───────────────────────────

def run_scenario(client, key, write, counties_cache,
                 wanted_program=None, wanted_rate=None, accept=False,
                 zip_override=None, city_override=None):
    zip_code, utility, label = SCENARIOS[key]
    if zip_override:
        zip_code = zip_override
    rule(f"SCENARIO: {label}  (zip {zip_code}, {utility})")
    email = f"dalton.e4.{key}.{uuid.uuid4().hex[:10]}@example.com"
    state = "VA" if key == "va" else "NY"
    base = VA_CUSTOMER if key == "va" else TEST_CUSTOMER

    # The test customer's own address follows the ZIP being serviced. Perch
    # has no reason to accept a Virginia enrollment whose customer lives in
    # a different ZIP than the one whose capacity was just checked, and a
    # 422 caused by that would look like a finding about something else.
    customer = dict(base, address_zip=zip_code,
                    city=(city_override or SCENARIO_CITY.get(key)
                          or base.get("city")))
    note("scenario", "test customer address",
         f"{customer['city']}, {customer['state']} {customer['address_zip']}")

    token = step_token(client, email)
    if not token:
        return
    cap = step_capacity(client, token, zip_code, utility)
    if not cap:
        return
    programs, model = step_program_mapping(cap, zip_code)

    if state not in counties_cache:
        counties_cache[state] = step_markets(client, state)
    step_county_cross_check(model, counties_cache[state])

    # A county to send: prefer one the capacity actually restricts to, so the
    # restricted path is exercised; otherwise the first market county.
    county = None
    from services.perch import capacity as cap_module
    restricted = [c for t in cap_module.CAPACITY_TYPES
                  for c in model.counties_for(t)]
    if restricted:
        county = restricted[0]
        note("county", "sending a RESTRICTED county at /enroll", repr(county))
    elif counties_cache[state]:
        county = counties_cache[state][0]
        note("county", "no restricted capacity; sending an ordinary county",
             repr(county))

    if not write:
        note("enroll", "SKIPPED (read-only pass)",
             "re-run with --write to create a real staging enrollment")
        return

    result, customer_type = step_enroll(client, token, cap, model, zip_code,
                                        utility, email, customer, county,
                                        wanted_program=wanted_program,
                                        wanted_rate=wanted_rate)
    if not result:
        return
    walked = step_after_enroll(client, token, result.get("next_step"),
                              state, county, accept=accept)

    # Completion is only expected when acceptance ran AND the chain actually
    # reached the end of it - a run that stopped at proof_docs is complete
    # as far as this runner goes, but the enrollment is not.
    finished = bool(accept) and "contracts_accept" in walked
    completed = step_status(client, token, expect_completed=finished)
    step_resume(client, email, completed=completed)


def summarise():
    rule("E4 SUMMARY")
    passed = [r for r in RESULTS if r[2] == "PASS"]
    mismatched = [r for r in RESULTS if r[2] == "MISMATCH"]
    notes = [r for r in RESULTS if r[2] == "NOTE"]
    print(f"  checks passed   : {len(passed)}")
    print(f"  MISMATCHES      : {len(mismatched)}")
    print(f"  notes/unverified: {len(notes)}")

    if mismatched:
        print("\n  --- MISMATCHES (smallest possible fix, each retested) ---")
        for section, labelled, _s, detail in mismatched:
            print(f"    [{section}] {labelled}")
            if detail:
                print(f"        {detail}")
    print("\n  --- NOTES / NOT VERIFIED BY STAGING ---")
    for section, labelled, _s, detail in notes:
        print(f"    [{section}] {labelled}: {detail}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"e4_staging_transcript_"
                       f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    TRANSCRIPT["finished_at"] = datetime.now().isoformat()
    TRANSCRIPT["summary"] = {"passed": len(passed), "mismatches": len(mismatched),
                             "notes": len(notes)}
    TRANSCRIPT["results"] = [{"section": s, "label": l, "status": st,
                              "detail": d} for s, l, st, d in RESULTS]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(TRANSCRIPT, fh, indent=2)
    print(f"\n  Transcript written to:\n    {out}")
    print("  Send that file back - it holds every real response body.")
    return 1 if mismatched else 0


def main():
    ap = argparse.ArgumentParser(description="Phase E4 Perch v2 staging verification")
    ap.add_argument("--write", action="store_true",
                    help="create real staging enrollments (items 6-13)")
    ap.add_argument("--only", choices=sorted(SCENARIOS),
                    help="run one scenario only")
    ap.add_argument("--program", choices=sorted(PROGRAM_BY_FLAG),
                    help="which program to enroll as: residential, lmi, "
                         "lmi_ira. Without it, a location offering more than "
                         "one program STOPS rather than choosing for you.")
    ap.add_argument("--rate", type=float,
                    help="the savings rate to send, e.g. --rate=20. Required "
                         "when the chosen program publishes more than one; "
                         "the runner never picks between real rates.")
    ap.add_argument("--zip", dest="zip_code",
                    help="service ZIP to use instead of the scenario default "
                         "(ny=12202, va=22401). The test customer's address "
                         "follows it.")
    ap.add_argument("--city",
                    help="test customer's city, when --zip needs one that "
                         "is not the scenario default")
    ap.add_argument("--accept", action="store_true",
                    help="perform the ACCEPTANCE steps - self-attestation "
                         "acceptance and contract acceptance. Without this "
                         "the runner generates documents and stops before "
                         "anything is accepted.")
    args = ap.parse_args()

    preflight()
    from services.perch.config import get_perch_client
    client = get_perch_client()

    if not args.write:
        rule("READ-ONLY PASS - nothing will be created at Perch")
    else:
        rule("WRITE PASS - REAL staging enrollments WILL be created")

    # --accept without --write has nothing to accept, and letting it pass
    # silently would make the flag look as though it had been honoured.
    # --zip applies to ONE scenario. Applying a Virginia ZIP to the New York
    # scenario as well would enroll a New York customer at a Virginia ZIP and
    # report whatever came back as a finding about something else.
    if (args.zip_code or args.city) and not args.only:
        print("\n  --zip/--city need --only=ny or --only=va: they describe one "
              "location, not both scenarios.")
        return 2
    if args.accept and not args.write:
        print("\n  --accept needs --write: there is no enrollment to accept "
              "in a read-only pass.")
        return 2
    if args.accept:
        rule("ACCEPT PASS - self-attestation and contracts WILL BE ACCEPTED")
    elif args.write:
        note("run", "acceptance disabled",
             "documents will be generated; nothing will be accepted")

    program = PROGRAM_BY_FLAG.get(args.program) if args.program else None
    if program:
        note("run", "program requested on the command line",
             f"--program={args.program} -> customer_type={program}")
    if args.rate is not None:
        note("run", "savings rate requested on the command line",
             f"--rate={args.rate}")

    counties_cache = {}
    for key in ([args.only] if args.only else sorted(SCENARIOS)):
        try:
            run_scenario(client, key, args.write, counties_cache,
                         wanted_program=program, wanted_rate=args.rate,
                         accept=args.accept, zip_override=args.zip_code,
                         city_override=args.city)
        except Exception as e:          # noqa: BLE001 - a scenario must not
            # kill the run; the remaining scenarios still carry information.
            record(key, "scenario completed without an unexpected error", False,
                   f"{type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    return summarise()


if __name__ == "__main__":
    sys.exit(main())
