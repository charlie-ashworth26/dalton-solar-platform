"""
Market-rules client + HMAC canonical-query signing.

PHASE 1. Covers the signing correction and the five read-only market-rules
client methods. Does NOT cover the Virginia UI or enrollment flow - those are
later phases and are deliberately absent here.

WHY THIS SUITE EXISTS
    services/perch/hmac_auth.py used to sign the URL-ENCODED query string.
    The spec requires the RAW string ("the value is signed as-is, not
    URL-encoded (a space stays a space)"). The bug was invisible for New York,
    because no NY query value contains a character urlencode rewrites - and it
    is a hard 401 for Virginia, because every multi-word county name has a
    space.

    Confirmed against Perch staging on 2026-10-25 with
    county=Martinsville city&state=VA:
        signed RAW         -> HTTP 200
        signed urlencode() -> HTTP 401

    So the fix is evidence-backed, not a reading of the spec, and this suite
    locks it down in both directions: raw must be signed, and a revert to
    urlencode must FAIL rather than quietly pass.

NO NETWORK. requests is replaced with a stub, so the whole suite is offline and
deterministic. Nothing touches the database.

Run: python test/test_market_rules_signing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from urllib.parse import urlencode

from services.perch import hmac_auth
from services.perch.client import (
    PerchHTTPClient,
    PATH_MARKETS_UTILITIES,
    PATH_MARKETS_UTILITY_ACCOUNT_FIELDS,
    PATH_MARKETS_COUNTIES,
    PATH_MARKETS_COUNTY_INCOME_LIMITS,
    PATH_MARKETS_SELF_ATTESTATION_OPTIONS,
)
from services.perch.errors import (
    PerchAuthError, PerchValidationError, PerchUnavailableError,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HMAC_PATH = os.path.join(ROOT, "services", "perch", "hmac_auth.py")
CLIENT_PATH = os.path.join(ROOT, "services", "perch", "client.py")
HMAC_SRC = open(HMAC_PATH, encoding="utf-8").read()
CLIENT_SRC = open(CLIENT_PATH, encoding="utf-8").read()


def executable_source(path):
    """The module's source with every docstring removed.

    The 'nothing is hardcoded' checks below are about market values reaching
    EXECUTABLE code - a slug or county used as data instead of being fetched.
    A value named in a docstring is documentation: it illustrates a rule and no
    code path can depend on it. Signing examples in particular need a realistic
    spaced value to be worth reading.

    So the guard strips docstrings and asserts against what actually runs. That
    is stricter where it matters than a plain substring search over the whole
    file, because it cannot be satisfied by moving a literal into a comment.
    """
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))

MARKETS_BASE = "https://staging.api.perchenergy.com/affiliate_partners/v1/markets"
KEY = "test-api-key"
SECRET = "test-shared-secret"
TS = "1700000000"          # fixed, so signatures are reproducible


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


# ───────────────────────── offline transport stub ─────────────────────────

class FakeResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (
            "" if payload is None else __import__("json").dumps(payload))
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeRequests:
    """Captures every call so the URL and headers can be asserted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeRequests ran out of queued responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def one_call(fake, what):
    """Assert exactly one HTTP call happened, and hand it back.

    Guards the case where a method returns a value WITHOUT calling Perch - a
    hardcoded market value. Without this, the URL assertion below would blow up
    with IndexError instead of naming the actual problem.
    """
    check(f"{what} made exactly one HTTP call to Perch (nothing hardcoded)",
          len(fake.calls) == 1)
    return fake.calls[0]


def client_with(responses):
    c = PerchHTTPClient(enrollment_base_url="https://example.invalid/v1/enrollments",
                        markets_base_url=MARKETS_BASE,
                        api_key=KEY, secret_key=SECRET, timeout=7)
    fake = FakeRequests(responses)
    c._requests = lambda: fake          # noqa: E731 - test seam, instance only
    return c, fake


# ───────────────────────── confirmed staging fixtures ─────────────────────────
# Values marked CONFIRMED were reported back from the read-only staging probe on
# 2026-10-25. Where the probe's exact figures were not captured (the income
# amounts), the fixture uses clearly synthetic numbers and the assertions test
# STRUCTURE only - no invented value is ever presented as confirmed.

VA_UTILITIES = {"state": "VA", "utilities": ["dominion-energy-va"]}   # CONFIRMED slug

VA_ACCOUNT_FIELDS = {                                                 # CONFIRMED
    "utility_name": "dominion-energy-va",
    "fields": [
        {"field_type": "account_number", "field_name": "Account Number",
         "help_text": None, "patterns": ["^[0-9]{10}$", "^[0-9]{12}$"]},
        {"field_type": "meter_number", "field_name": "Meter Number",
         "help_text": "10 digits", "patterns": [r"^\d{10}$"]},
    ],
}

# CONFIRMED: 133 counties, 38 of them independent cities. This fixture is a
# representative slice - the assertions below are about verbatim handling and
# the independent-city spelling, not about the full list.
VA_COUNTIES = {"state": "VA",
               "counties": ["Accomack County", "Albemarle County",
                            "Martinsville city", "Richmond city"]}

VA_SELF_ATTESTATION_OPTIONS = {                                       # CONFIRMED
    "state": "VA",
    "source_types": ["self_attestation_qualifying_income"],
    "required_source_types": ["self_attestation_qualifying_income"],
    "single_source_type": True,
    "lmi_programs": "optional",
    "programs": [
        {"value": "self_attestation_snap", "label": "SNAP"},
        {"value": "self_attestation_tanf", "label": "TANF"},
        {"value": "self_attestation_medicaid", "label": "Medicaid"},
        {"value": "self_attestation_famis", "label": "FAMIS"},
        {"value": "self_attestation_liheap", "label": "LIHEAP"},
        {"value": "self_attestation_weatherization_assistance_program",
         "label": "Weatherization Assistance Program"},
        {"value": "self_attestation_hvac", "label": "HVAC Health and Safety Program"},
        {"value": "self_attestation_hcvp", "label": "Housing Choice Voucher (HCV)"},
        {"value": "self_attestation_virginia_rent_relief_program",
         "label": "Virginia Rent Relief Program"},
    ],
    "status": [],
}

NY_SELF_ATTESTATION_OPTIONS = {                                       # CONFIRMED
    "state": "NY",
    "source_types": ["self_attestation_qualifying_income"],
    "required_source_types": ["self_attestation_qualifying_income"],
    "single_source_type": True,
    "lmi_programs": "none",
    "programs": [],
    "status": ["accepted", "rejected"],
}

# SYNTHETIC amounts - structure only. Real VA limits come from the API at runtime.
VA_INCOME_LIMITS = {"state": "VA", "county": "Martinsville city",
                    "income_limits": [{"house_occupancy": 1, "household_income": 11111.0},
                                      {"house_occupancy": 2, "household_income": 22222.0}]}


def main():
    print("=" * 72)
    print("MARKET RULES + HMAC CANONICAL QUERY SIGNING  (Phase 1, offline)")
    print("=" * 72)

    # ══════════════════ 1. raw signing, values with spaces ══════════════════
    print("\n== 1. canonical_query_string signs RAW values ==")

    va = {"county": "Martinsville city", "state": "VA"}
    raw = hmac_auth.canonical_query_string(va)
    enc = hmac_auth.encoded_query_string(va)

    check(f"raw keeps the space verbatim: {raw!r}",
          raw == "county=Martinsville city&state=VA")
    check("  ...the space is a literal 0x20, not '+' and not '%20'",
          " " in raw and "+" not in raw and "%20" not in raw)
    check(f"encoded form percent-encodes it: {enc!r}",
          enc == "county=Martinsville%20city&state=VA")
    check("  ...as %20, NOT '+' (matches the spec's --data-urlencode examples)",
          "%20" in enc and "+" not in enc)
    check("the two forms differ for a spaced value", raw != enc)
    check("county sorts before state, per the spec note",
          raw.index("county=") < raw.index("state="))

    # multi-word county with several spaces, and an independent city
    for name in ["Martinsville city", "King and Queen County", "Isle of Wight County"]:
        r = hmac_auth.canonical_query_string({"state": "VA", "county": name})
        check(f"  {name!r} signs with every space intact",
              r == f"county={name}&state=VA")

    check("None-valued params are dropped, never sent as the string 'None'",
          hmac_auth.canonical_query_string({"state": "VA", "county": None})
          == "state=VA")

    # ══════════════════ 2. the old encoded assumption must FAIL ══════════════════
    print("\n== 2. the previous urlencode() assumption is rejected ==")

    # This is the mutation guard. If canonical_query_string is ever reverted to
    # urlencode(items), the signature below becomes the ENCODED one and these
    # checks fail. That is the whole point - staging answers 401 for it.
    sig_over_raw = hmac_auth.compute_signature(
        SECRET, hmac_auth.build_signed_payload(TS, raw))
    sig_over_encoded = hmac_auth.compute_signature(
        SECRET, hmac_auth.build_signed_payload(TS, urlencode(sorted(va.items()))))

    check("the raw and encoded signatures are genuinely different",
          sig_over_raw != sig_over_encoded)

    headers, sent = hmac_auth.sign_query_request(KEY, SECRET, va, timestamp=TS)
    check("sign_query_request signs over the RAW string (staging: HTTP 200)",
          headers[hmac_auth.HEADER_SIGNATURE] == sig_over_raw)
    check("  ...and NOT over the urlencoded string (staging: HTTP 401)",
          headers[hmac_auth.HEADER_SIGNATURE] != sig_over_encoded)
    check(f"it returns the ENCODED string to transmit: {sent!r}",
          sent == "county=Martinsville%20city&state=VA")
    check("  ...which is not what was signed", sent != raw)

    check("signature is 64 lowercase hex chars",
          len(sig_over_raw) == 64 and sig_over_raw == sig_over_raw.lower()
          and all(ch in "0123456789abcdef" for ch in sig_over_raw))
    check("Content-Type is NOT sent on a GET (spec: no request body)",
          "Content-Type" not in headers)
    check("the three HMAC headers are present",
          {hmac_auth.HEADER_API_KEY, hmac_auth.HEADER_SIGNATURE,
           hmac_auth.HEADER_TIMESTAMP} <= set(headers))
    check("no enrollment token leaks onto an HMAC call",
          not any(h.lower() == "x-enrollment-token" for h in headers))

    # source-level guard: the broken call must not reappear
    check("hmac_auth.py no longer signs urlencode() output",
          "return urlencode(items)" not in HMAC_SRC)
    check("  ...and documents the raw rule explicitly",
          "raw" in HMAC_SRC.lower() and "a space stays a space" in HMAC_SRC)

    # ══════════════════ 3. NY is byte-identical — no regression ══════════════════
    print("\n== 3. existing NY behaviour is unchanged ==")

    ny = {"zip_code": "10001", "utility_name": "consolidated-edison-ny"}
    ny_raw = hmac_auth.canonical_query_string(ny)
    ny_enc = hmac_auth.encoded_query_string(ny)
    ny_old = urlencode(sorted(ny.items()))        # what the code produced before

    check(f"NY signed string matches the spec example: {ny_raw!r}",
          ny_raw == "utility_name=consolidated-edison-ny&zip_code=10001")
    check("NY raw == NY encoded (no value needs encoding)", ny_raw == ny_enc)
    check("NY raw == the PREVIOUS urlencode() output -> signature unchanged",
          ny_raw == ny_old)

    ny_headers, ny_sent = hmac_auth.sign_query_request(KEY, SECRET, ny, timestamp=TS)
    check("  ...so NY's signature is bit-for-bit what it was before the fix",
          ny_headers[hmac_auth.HEADER_SIGNATURE]
          == hmac_auth.compute_signature(SECRET,
                                         hmac_auth.build_signed_payload(TS, ny_old)))
    check("  ...and NY's transmitted query is unchanged too", ny_sent == ny_old)

    # the JSON-body path (POST /token, PATCH /refresh_token) is untouched
    j_headers, j_body = hmac_auth.sign_json_request(
        KEY, SECRET, {"email": "john.doe@example.com"}, timestamp=TS)
    check("sign_json_request still emits compact JSON",
          j_body == '{"email":"john.doe@example.com"}')
    check("  ...with Content-Type: application/json",
          j_headers.get("Content-Type") == "application/json")
    check("  ...and a signature over timestamp + newline + body",
          j_headers[hmac_auth.HEADER_SIGNATURE]
          == hmac_auth.compute_signature(SECRET, f"{TS}\n{j_body}"))

    # ══════════════════ 4. parsing the confirmed VA responses ══════════════════
    print("\n== 4. market-rules methods parse the confirmed VA responses ==")

    # 4a. utilities -> Dominion slug, read not guessed
    c, fake = client_with([FakeResponse(200, VA_UTILITIES)])
    out = c.get_market_utilities("VA")
    call = one_call(fake, "get_market_utilities")
    check("get_market_utilities returns Perch's body verbatim", out == VA_UTILITIES)
    check("  ...carrying the confirmed slug 'dominion-energy-va'",
          "dominion-energy-va" in out["utilities"])
    check(f"  ...URL hits {PATH_MARKETS_UTILITIES} on the MARKETS base",
          call["url"] == f"{MARKETS_BASE}{PATH_MARKETS_UTILITIES}?state=VA")
    check("  ...signed with the three HMAC headers, no Content-Type",
          hmac_auth.HEADER_SIGNATURE in call["headers"]
          and "Content-Type" not in call["headers"])
    check("  ...and the configured timeout is honoured", call["timeout"] == 7)

    # 4b. account fields -> two account patterns, meter required, no secondary id
    c, fake = client_with([FakeResponse(200, VA_ACCOUNT_FIELDS)])
    out = c.get_market_utility_account_fields("dominion-energy-va")
    call = one_call(fake, "get_market_utility_account_fields")
    types = [f["field_type"] for f in out["fields"]]
    acct = next(f for f in out["fields"] if f["field_type"] == "account_number")
    check("account_number is first, as the spec guarantees", types[0] == "account_number")
    check("  ...with BOTH confirmed patterns, in order",
          acct["patterns"] == ["^[0-9]{10}$", "^[0-9]{12}$"])
    check("  ...so a 10- AND a 12-digit number are both valid (any-one-matches)",
          len(acct["patterns"]) == 2)
    check("meter_number IS required for Dominion", "meter_number" in types)
    check("  ...10 digits", next(f for f in out["fields"]
                                 if f["field_type"] == "meter_number")["patterns"]
          == [r"^\d{10}$"])
    check("secondary_account_identifier is NOT required for Dominion",
          "secondary_account_identifier" not in types)
    check(f"  ...URL uses ?utility=, not ?state=",
          call["url"]
          == f"{MARKETS_BASE}{PATH_MARKETS_UTILITY_ACCOUNT_FIELDS}?utility=dominion-energy-va")

    # 4c. counties -> independent cities survive verbatim
    c, fake = client_with([FakeResponse(200, VA_COUNTIES)])
    out = c.get_market_counties("VA")
    one_call(fake, "get_market_counties")
    check("get_market_counties returns the list verbatim",
          out["counties"] == VA_COUNTIES["counties"])
    check("independent cities keep their lowercase 'city' spelling",
          "Martinsville city" in out["counties"])
    check("  ...not title-cased to 'Martinsville City'",
          "Martinsville City" not in out["counties"])
    check("  ...and are not rewritten as 'County'",
          not any(c2.endswith("city County") for c2 in out["counties"]))
    cities = [c2 for c2 in out["counties"] if not c2.endswith("County")]
    check(f"county-equivalents are distinguishable from counties ({len(cities)} here)",
          len(cities) == 2)

    # 4d. income limits -> the signed string is raw, county before state
    c, fake = client_with([FakeResponse(200, VA_INCOME_LIMITS)])
    out = c.get_market_county_income_limits("VA", "Martinsville city")
    call = one_call(fake, "get_market_county_income_limits")
    check("get_market_county_income_limits parses the structure",
          [r["house_occupancy"] for r in out["income_limits"]] == [1, 2])
    check("  ...household_income stays a float (Perch sends dollars, not cents)",
          all(isinstance(r["household_income"], float) for r in out["income_limits"]))
    check("  ...county echoed verbatim", out["county"] == "Martinsville city")
    check("  ...URL percent-encodes the space as %20",
          call["url"]
          == f"{MARKETS_BASE}{PATH_MARKETS_COUNTY_INCOME_LIMITS}"
             f"?county=Martinsville%20city&state=VA")
    check("  ...but the SIGNATURE is over the raw string",
          call["headers"][hmac_auth.HEADER_SIGNATURE]
          == hmac_auth.compute_signature(
              SECRET,
              hmac_auth.build_signed_payload(
                  call["headers"][hmac_auth.HEADER_TIMESTAMP],
                  "county=Martinsville city&state=VA")))

    # 4e. self-attestation options -> VA status is empty
    c, fake = client_with([FakeResponse(200, VA_SELF_ATTESTATION_OPTIONS)])
    va_opts = c.get_market_self_attestation_options("VA")
    one_call(fake, "get_market_self_attestation_options(VA)")
    check("VA status array is EMPTY", va_opts["status"] == [])
    check("VA lmi_programs is 'optional'", va_opts["lmi_programs"] == "optional")
    check("VA single_source_type is true", va_opts["single_source_type"] is True)
    check("VA source_types is exactly the qualifying-income value",
          va_opts["source_types"] == ["self_attestation_qualifying_income"])
    check("  ...and it is also the required one",
          va_opts["required_source_types"] == ["self_attestation_qualifying_income"])
    check("VA offers 9 assistance programs", len(va_opts["programs"]) == 9)
    check("  ...including the Virginia Rent Relief Program",
          any(p["value"] == "self_attestation_virginia_rent_relief_program"
              for p in va_opts["programs"]))

    # 4f. NY control -> status non-empty, so NY keeps sending it
    c, fake = client_with([FakeResponse(200, NY_SELF_ATTESTATION_OPTIONS)])
    ny_opts = c.get_market_self_attestation_options("NY")
    one_call(fake, "get_market_self_attestation_options(NY)")
    check("NY status array is ['accepted', 'rejected']",
          ny_opts["status"] == ["accepted", "rejected"])
    check("NY lmi_programs is 'none'", ny_opts["lmi_programs"] == "none")
    check("  ...and NY offers no programs", ny_opts["programs"] == [])

    # The decision rule Phase 2+ depends on, asserted here so it is already
    # tested before any caller exists. Driven by the response, never by a
    # hardcoded state name.
    print("\n== 4g. the status-key decision rule (drives Phase 2) ==")
    send_status = lambda opts: bool(opts.get("status"))     # noqa: E731
    check("VA -> OMIT the status key", send_status(va_opts) is False)
    check("NY -> SEND a status value", send_status(ny_opts) is True)
    check("  ...the rule reads the response, it does not compare state names",
          send_status({"state": "ZZ", "status": []}) is False
          and send_status({"state": "ZZ", "status": ["accepted"]}) is True)

    # ══════════════════ 5. status-code handling ══════════════════
    print("\n== 5. market-endpoint status codes ==")

    c, _ = client_with([FakeResponse(404, {"error": "not_found",
                                          "message": "state not found"})])
    try:
        c.get_market_counties("ZZ")
        raise AssertionError("404 should have raised")
    except PerchValidationError as e:
        check("404 -> PerchValidationError (a bad county is not an auth failure)",
              "market data" in str(e))
    except PerchAuthError:
        raise AssertionError("404 must NOT surface as PerchAuthError")

    c, _ = client_with([FakeResponse(401, {"error": "unauthorized"})])
    try:
        c.get_market_counties("VA")
        raise AssertionError("401 should have raised")
    except PerchAuthError:
        check("401 -> PerchAuthError, mentioning the signature", True)

    c, _ = client_with([FakeResponse(422, {"error": "unprocessable_entity",
                                           "message": "state must be a two-letter state abbreviation"})])
    try:
        c.get_market_self_attestation_options("VIRGINIA")
        raise AssertionError("422 should have raised")
    except PerchValidationError:
        check("422 -> PerchValidationError (unconfigured state / bad param)", True)

    c, _ = client_with([ConnectionError("dns")])
    try:
        c.get_market_utilities("VA")
        raise AssertionError("transport failure should have raised")
    except PerchUnavailableError:
        check("transport failure -> PerchUnavailableError", True)

    # ══════════════════ 6. nothing is hardcoded ══════════════════
    print("\n== 6. no market values are hardcoded in production source ==")

    for path, name in ((CLIENT_PATH, "client.py"), (HMAC_PATH, "hmac_auth.py")):
        code = executable_source(path)
        low = code.lower()
        check(f"{name}: no Dominion slug literal in executable code",
              "dominion-energy-va" not in low)
        check(f"{name}: no Dominion account-number pattern in executable code",
              "[0-9]{10}" not in code and "[0-9]{12}" not in code)
        check(f"{name}: no Virginia county literal in executable code",
              "martinsville" not in low)
        check(f"{name}: no hardcoded state list in executable code",
              '"VA"' not in code and "'VA'" not in code)

    # Belt and braces: the slug must not appear ANYWHERE in client.py, docstrings
    # included, because that is the one value a future caller is most likely to
    # copy out of a comment instead of calling get_market_utilities().
    check("client.py never names the Dominion slug at all, not even in prose",
          "dominion-energy-va" not in CLIENT_SRC.lower())
    check("client.py declares the five market paths as constants, not inline strings",
          all(p in CLIENT_SRC for p in (
              'PATH_MARKETS_UTILITIES = "/utilities"',
              'PATH_MARKETS_UTILITY_ACCOUNT_FIELDS = "/utility_account_fields"',
              'PATH_MARKETS_COUNTIES = "/counties"',
              'PATH_MARKETS_COUNTY_INCOME_LIMITS = "/counties/income_limits"',
              'PATH_MARKETS_SELF_ATTESTATION_OPTIONS = "/self_attestation_options"')))

    # ══════════════════ 7. existing NY market call untouched ══════════════════
    print("\n== 7. get_market_capacity still behaves as before ==")

    c, fake = client_with([FakeResponse(200, {"project_details": {}})])
    c.get_market_capacity("10001", "consolidated-edison-ny")
    call = one_call(fake, "get_market_capacity")
    check("NY market-capacity URL is unchanged",
          call["url"] == f"{MARKETS_BASE}/capacity"
          "?utility_name=consolidated-edison-ny&zip_code=10001")
    check("  ...and it inherits the signing fix for free",
          call["headers"][hmac_auth.HEADER_SIGNATURE]
          == hmac_auth.compute_signature(
              SECRET,
              hmac_auth.build_signed_payload(
                  call["headers"][hmac_auth.HEADER_TIMESTAMP],
                  "utility_name=consolidated-edison-ny&zip_code=10001")))

    print(f"\n{'=' * 72}")
    print("MARKET RULES + SIGNING - ALL CHECKS PASSED")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
