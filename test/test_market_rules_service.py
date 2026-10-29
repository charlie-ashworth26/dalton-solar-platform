"""
Market-rules service layer, cache, fallback and HTTP exposure.

PHASE 2. Covers services/perch/markets.py and routes/market_routes.py:
caching, the NY-only outage fallback, fail-closed behaviour for every other
market, the derived rules, and the five read-only endpoints.

DOES NOT cover the Virginia enrollment UI or the self-attestation flow - those
are later phases. Nothing here wires market rules into an enrollment.

NO NETWORK. A stub client is injected via markets.set_client_factory(), so the
suite is offline and deterministic. Fixture values are the ones confirmed
against Perch staging on 2026-10-25.

Run: python test/test_market_rules_service.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, execute
import seed
from services.perch import markets
from services.perch.errors import (
    PerchValidationError, PerchUnavailableError, PerchNotImplementedError,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_SRC_PATH = os.path.join(ROOT, "services", "perch", "markets.py")
ROUTES_SRC_PATH = os.path.join(ROOT, "routes", "market_routes.py")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def raises(exc, fn):
    """True when fn() raises exc. Lets fail-closed behaviour be asserted with
    check() so a regression prints a named [FAIL] line instead of an opaque
    traceback from a bare `raise AssertionError` inside a try block."""
    try:
        fn()
        return False
    except exc:
        return True


def executable_source(path):
    """Module source with docstrings stripped - see Phase 1 suite for why."""
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


# ───────────────── confirmed staging fixtures (2026-10-25) ─────────────────

VA_UTILITIES = {"state": "VA", "utilities": ["dominion-energy-va"]}

VA_ACCOUNT_FIELDS = {
    "utility_name": "dominion-energy-va",
    "fields": [
        {"field_type": "account_number", "field_name": "Account Number",
         "help_text": "10 or 12 digits", "patterns": ["^[0-9]{10}$", "^[0-9]{12}$"]},
        {"field_type": "meter_number", "field_name": "Meter Number",
         "help_text": "10 digits", "patterns": [r"^\d{10}$"]},
    ],
}

NY_ACCOUNT_FIELDS = {
    "utility_name": "nyseg",
    "fields": [
        {"field_type": "account_number", "field_name": "Account Number",
         "help_text": None, "patterns": ["^[0-9]{11}$"]},
        {"field_type": "secondary_account_identifier", "field_name": "POD ID",
         "help_text": "15 digits, starts with N01", "patterns": ["^N01[0-9]{12}$"]},
    ],
}

# CONFIRMED: 133 VA counties including 38 independent cities. Built to that
# shape so the count assertions are meaningful rather than decorative.
_VA_PLAIN = [f"County{n:03d} County" for n in range(95)]
_VA_CITIES = ["Martinsville city", "Richmond city"] + [
    f"City{n:03d} city" for n in range(36)]
VA_COUNTIES = {"state": "VA", "counties": sorted(_VA_PLAIN + _VA_CITIES)}

VA_OPTIONS = {
    "state": "VA",
    "source_types": ["self_attestation_qualifying_income"],
    "required_source_types": ["self_attestation_qualifying_income"],
    "single_source_type": True,
    "lmi_programs": "optional",
    "programs": [{"value": f"self_attestation_p{n}", "label": f"P{n}"}
                 for n in range(9)],
    "status": [],
}

NY_OPTIONS = {
    "state": "NY",
    "source_types": ["self_attestation_qualifying_income"],
    "required_source_types": ["self_attestation_qualifying_income"],
    "single_source_type": True,
    "lmi_programs": "none",
    "programs": [],
    "status": ["accepted", "rejected"],
}

VA_INCOME = {"state": "VA", "county": "Martinsville city",
             "income_limits": [{"house_occupancy": n, "household_income": 10000.0 + n}
                               for n in range(1, 9)]}


class StubClient:
    """Records calls and serves the fixtures. Raises what it is told to."""

    mode = "stub"

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    def get_market_utilities(self, state):
        self.calls.append(("utilities", state)); self._maybe_fail()
        return dict(VA_UTILITIES, state=state)

    def get_market_utility_account_fields(self, utility_slug):
        self.calls.append(("fields", utility_slug)); self._maybe_fail()
        return NY_ACCOUNT_FIELDS if utility_slug == "nyseg" else VA_ACCOUNT_FIELDS

    def get_market_counties(self, state):
        self.calls.append(("counties", state)); self._maybe_fail()
        return dict(VA_COUNTIES, state=state)

    def get_market_county_income_limits(self, state, county):
        self.calls.append(("income", state, county)); self._maybe_fail()
        return dict(VA_INCOME, state=state)

    def get_market_self_attestation_options(self, state):
        self.calls.append(("options", state)); self._maybe_fail()
        return NY_OPTIONS if state == "NY" else VA_OPTIONS


def use(stub):
    markets.set_client_factory(lambda: stub)
    return stub


def main():
    print("=" * 72)
    print("MARKET RULES SERVICE + CACHE + HTTP EXPOSURE  (Phase 2, offline)")
    print("=" * 72)

    init_db(reset=True)
    seed.seed()
    c = app.test_client()

    def token(email, pw):
        r = c.post("/api/auth/login", json={"email": email, "password": pw})
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    rep = token("charlie@daltonsolar.com", "RepPass1!")
    adm = token("admin@daltonsolar.com", "AdminPass1!")
    qa = token("qa@daltonsolar.com", "QaPass1!")

    # ══════════════════ 1. the five lookups ══════════════════
    print("\n== 1. the five market lookups ==")
    s = use(StubClient())

    u = markets.utilities("VA")
    check("utilities('VA') returns the slug list", u["utilities"] == ["dominion-energy-va"])
    check("  ...tagged source=perch", u["source"] == "perch")
    check("  ...state normalized to upper case", markets.utilities("va")["state"] == "VA")

    f = markets.utility_account_fields("dominion-energy-va")
    check("utility_account_fields returns both account patterns",
          f["fields"][0]["patterns"] == ["^[0-9]{10}$", "^[0-9]{12}$"])

    co = markets.counties("VA")
    check(f"counties('VA') returns 133 names (got {len(co['counties'])})",
          len(co["counties"]) == 133)
    cities = [x for x in co["counties"] if not x.endswith("County")]
    check(f"  ...38 of them independent cities (got {len(cities)})", len(cities) == 38)
    check("  ...'Martinsville city' verbatim, lowercase 'city'",
          "Martinsville city" in co["counties"])
    check("  ...no name was title-cased", "Martinsville City" not in co["counties"])

    il = markets.county_income_limits("VA", "Martinsville city")
    check("county_income_limits covers household sizes 1-8",
          [r["house_occupancy"] for r in il["income_limits"]] == list(range(1, 9)))
    check("  ...household_income stays a float", isinstance(
        il["income_limits"][0]["household_income"], float))
    check("  ...county echoed as Perch stores it", il["county"] == "Martinsville city")

    o = markets.self_attestation_options("VA")
    check("self_attestation_options('VA') status is EMPTY", o["status"] == [])
    check("  ...lmi_programs optional", o["lmi_programs"] == "optional")
    check("  ...single_source_type true", o["single_source_type"] is True)
    check("  ...9 programs", len(o["programs"]) == 9)

    # ══════════════════ 2. derived rules ══════════════════
    print("\n== 2. derived rules (read from the response, never the state name) ==")
    check("sends_status('VA') is False -> omit the status key",
          markets.sends_status("VA") is False)
    check("sends_status('NY') is True  -> send a status value",
          markets.sends_status("NY") is True)
    check("status_values('NY') == ['accepted', 'rejected']",
          markets.status_values("NY") == ["accepted", "rejected"])
    check("status_values('VA') == []", markets.status_values("VA") == [])

    check("Dominion requires meter_number",
          markets.field_is_required("dominion-energy-va", "meter_number") is True)
    check("Dominion does NOT require secondary_account_identifier",
          markets.field_is_required(
              "dominion-energy-va", "secondary_account_identifier") is False)
    check("NYSEG DOES require secondary_account_identifier (unchanged)",
          markets.field_is_required("nyseg", "secondary_account_identifier") is True)

    print("\n== 2b. account-value validation, any-one-pattern-matches ==")
    v = lambda slug, ft, val: markets.validate_account_value(slug, ft, val)  # noqa: E731
    check("Dominion 10-digit account accepted", v("dominion-energy-va", "account_number", "1234567890") is None)
    check("Dominion 12-digit account accepted", v("dominion-energy-va", "account_number", "123456789012") is None)
    check("Dominion 11-digit account REJECTED (matches neither pattern)",
          v("dominion-energy-va", "account_number", "12345678901") is not None)
    check("Dominion 9-digit account rejected",
          v("dominion-energy-va", "account_number", "123456789") is not None)
    check("  ...the message carries the published help text",
          "10 or 12 digits" in (v("dominion-energy-va", "account_number", "1") or ""))
    check("blank account number rejected with a required message",
          "required" in (v("dominion-energy-va", "account_number", "") or ""))
    check("Dominion meter 10 digits accepted", v("dominion-energy-va", "meter_number", "0123456789") is None)
    check("Dominion meter 9 digits rejected", v("dominion-energy-va", "meter_number", "012345678") is not None)
    check("a field the utility does not require validates as None (not an error)",
          v("dominion-energy-va", "secondary_account_identifier", "") is None)
    check("NYSEG POD ID still matches its own published pattern",
          v("nyseg", "secondary_account_identifier", "N01123456789012") is None)
    check("  ...and a wrong NYSEG POD ID is rejected",
          v("nyseg", "secondary_account_identifier", "N02123456789012") is not None)
    check("account_number matching is case-insensitive per the spec",
          v("nyseg", "account_number", "12345678901") is None)

    # ══════════════════ 3. caching ══════════════════
    print("\n== 3. caching ==")
    markets.invalidate()
    s = use(StubClient())
    markets.counties("VA"); markets.counties("VA"); markets.counties("VA")
    check(f"3 calls -> 1 fetch (got {len(s.calls)})", len(s.calls) == 1)
    markets.counties("NY")
    check("a different state is a different cache key", len(s.calls) == 2)
    markets.invalidate("counties")
    markets.counties("VA")
    check("invalidate('counties') forces a refetch", len(s.calls) == 3)

    st = markets.stats()
    check("stats report hits and misses", st["hits"] > 0 and st["misses"] > 0)
    check("  ...and expose no market values",
          not any(k in st for k in ("counties", "utilities", "values")))

    markets.invalidate()
    os.environ["PERCH_MARKETS_CACHE_TTL_SECONDS"] = "0"
    s = use(StubClient())
    markets.counties("VA"); markets.counties("VA")
    check("TTL=0 means every read refetches (no stale-forever cache)", len(s.calls) == 2)
    os.environ.pop("PERCH_MARKETS_CACHE_TTL_SECONDS", None)

    # ══════════════════ 4. fallback and fail-closed ══════════════════
    print("\n== 4. outage behaviour ==")
    markets.invalidate()
    use(StubClient(fail_with=PerchUnavailableError("connection reset")))

    with app.app_context():
        ny = markets.counties("NY")
        check(f"NY counties survive a Perch outage ({len(ny['counties'])} from the DB)",
              len(ny["counties"]) == 62)
        check("  ...flagged source=fallback_db, never passed off as perch",
              ny["source"] == "fallback_db")
        check("  ...and the values are the ny_counties spelling",
              "Albany County" in ny["counties"])

        check("VA counties FAIL CLOSED - no fallback, no empty list, no invention",
              raises(markets.MarketDataUnavailable, lambda: markets.counties("VA")))
        va_leak = None
        try:
            va_leak = markets.counties("VA")
        except markets.MarketDataUnavailable:
            pass
        check("  ...and VA is NEVER served New York's county list",
              va_leak is None or "Albany County" not in va_leak.get("counties", []))

        for fn, label in ((lambda: markets.utilities("VA"), "utilities"),
                          (lambda: markets.self_attestation_options("VA"), "options"),
                          (lambda: markets.county_income_limits("VA", "Martinsville city"), "income limits"),
                          (lambda: markets.utility_account_fields("dominion-energy-va"), "account fields")):
            check(f"  {label} fail closed too",
                  raises(markets.MarketDataUnavailable, fn))

    print("\n== 4b. which failures get cached ==")
    markets.invalidate()
    s = use(StubClient(fail_with=PerchUnavailableError("down")))
    for _ in range(4):
        try:
            markets.utilities("VA")
        except markets.MarketDataUnavailable:
            pass
    check(f"a transport outage is negative-cached, not retried per call (got {len(s.calls)})",
          len(s.calls) == 1)

    markets.invalidate()
    s = use(StubClient(fail_with=PerchValidationError("state not found")))
    seen = []
    for _ in range(3):
        try:
            markets.utilities("ZZ")
        except Exception as e:                 # deliberately broad: the point of
            seen.append(type(e).__name__)      # this check is WHICH type escapes
    check(f"a validation error is NOT cached - a corrected typo retries immediately "
          f"(got {len(s.calls)} calls)", len(s.calls) == 3)
    check(f"  ...and every attempt raises PerchValidationError, never "
          f"MarketDataUnavailable (got {seen})",
          seen == ["PerchValidationError"] * 3)
    check("  ...which is what lets the route answer 400 rather than 503",
          "MarketDataUnavailable" not in seen)

    markets.invalidate()
    use(StubClient(fail_with=PerchNotImplementedError("mock has no market data")))
    check("mock mode reports unavailable rather than fabricating data",
          raises(markets.MarketDataUnavailable, lambda: markets.utilities("VA")))

    print("\n== 4c. bad input is rejected before any network call ==")
    markets.invalidate()
    s = use(StubClient())
    for bad in ("", None, "V", "VAA", "1A"):
        check(f"  malformed state {bad!r} rejected",
              raises(PerchValidationError, lambda b=bad: markets.utilities(b)))
    check(f"malformed state is rejected locally, 0 Perch calls (got {len(s.calls)})",
          len(s.calls) == 0)
    check("blank county rejected locally too",
          raises(PerchValidationError,
                 lambda: markets.county_income_limits("VA", "  ")))

    # ══════════════════ 5. HTTP exposure ══════════════════
    print("\n== 5. the read-only endpoints ==")
    markets.invalidate()
    use(StubClient())

    r = c.get("/api/markets/utilities?state=VA", headers=rep)
    check("GET /api/markets/utilities -> 200", r.status_code == 200)
    check("  ...carries the slug", r.get_json()["utilities"] == ["dominion-energy-va"])
    check("  ...and a source field", r.get_json()["source"] == "perch")

    r = c.get("/api/markets/utility-account-fields?utility=dominion-energy-va", headers=rep)
    check("GET /api/markets/utility-account-fields -> 200", r.status_code == 200)
    check("  ...fields start with account_number",
          r.get_json()["fields"][0]["field_type"] == "account_number")

    r = c.get("/api/markets/counties?state=VA", headers=rep)
    check("GET /api/markets/counties -> 200 with 133 names",
          r.status_code == 200 and len(r.get_json()["counties"]) == 133)

    r = c.get("/api/markets/county-income-limits?state=VA&county=Martinsville city", headers=rep)
    check("GET /api/markets/county-income-limits -> 200 (county with a space)",
          r.status_code == 200 and len(r.get_json()["income_limits"]) == 8)

    r = c.get("/api/markets/self-attestation-options?state=VA", headers=rep)
    j = r.get_json()
    check("GET /api/markets/self-attestation-options -> 200", r.status_code == 200)
    check("  ...status is []", j["status"] == [])
    check("  ...and sends_status is derived as False", j["sends_status"] is False)
    j_ny = c.get("/api/markets/self-attestation-options?state=NY", headers=rep).get_json()
    check("  ...NY reports sends_status True", j_ny["sends_status"] is True)

    print("\n== 5b. status codes ==")
    markets.invalidate(); use(StubClient())
    check("missing ?state= -> 400",
          c.get("/api/markets/utilities", headers=rep).status_code == 400)
    check("malformed state -> 400",
          c.get("/api/markets/utilities?state=VIRGINIA", headers=rep).status_code == 400)
    check("missing ?utility= -> 400",
          c.get("/api/markets/utility-account-fields", headers=rep).status_code == 400)

    markets.invalidate(); use(StubClient(fail_with=PerchValidationError("state not found")))
    check("Perch rejecting the input -> 400, not 503",
          c.get("/api/markets/utilities?state=ZZ", headers=rep).status_code == 400)

    markets.invalidate(); use(StubClient(fail_with=PerchUnavailableError("down")))
    r = c.get("/api/markets/counties?state=VA", headers=rep)
    check("Perch outage -> 503, not 500", r.status_code == 503)
    check("  ...with an explicit market_data_unavailable flag",
          r.get_json().get("market_data_unavailable") is True)
    r = c.get("/api/markets/counties?state=NY", headers=rep)
    check("  ...but NY still answers 200 from the fallback",
          r.status_code == 200 and r.get_json()["source"] == "fallback_db")

    print("\n== 5c. auth ==")
    markets.invalidate(); use(StubClient())
    check("unauthenticated -> 401", c.get("/api/markets/utilities?state=VA").status_code == 401)
    check("sales_rep allowed", c.get("/api/markets/utilities?state=VA", headers=rep).status_code == 200)
    check("admin allowed", c.get("/api/markets/utilities?state=VA", headers=adm).status_code == 200)
    check("qa_reviewer refused (staff-only, not everyone)",
          c.get("/api/markets/utilities?state=VA", headers=qa).status_code == 403)
    check("cache stats are admin-only",
          c.get("/api/markets/cache", headers=rep).status_code == 403
          and c.get("/api/markets/cache", headers=adm).status_code == 200)
    check("cache clear is admin-only and reports success",
          c.delete("/api/markets/cache", headers=rep).status_code == 403
          and c.delete("/api/markets/cache", headers=adm).get_json()["cleared"] is True)

    print("\n== 5d. every market route is a READ ==")
    rules = [r for r in app.url_map.iter_rules() if str(r.rule).startswith("/api/markets")]
    urls = sorted(set(str(r.rule) for r in rules))
    # Flask registers one Rule per view, so /api/markets/cache contributes two
    # (GET stats, DELETE clear) for one URL: 7 URLs across 8 rules.
    #
    # utility-options was added with the Availability fix: it answers
    # ZIP -> state -> utilities in one round trip for that screen. Counted here
    # so a route cannot be added to this blueprint without this suite noticing.
    check(f"7 market URLs across 8 rules (got {len(urls)} / {len(rules)})",
          len(urls) == 7 and len(rules) == 8)
    check("  ...the data endpoints are the expected ones",
          urls == ["/api/markets/cache", "/api/markets/counties",
                   "/api/markets/county-income-limits",
                   "/api/markets/self-attestation-options",
                   "/api/markets/utilities", "/api/markets/utility-account-fields",
                   "/api/markets/utility-options"])
    writes = [(str(r.rule), sorted(r.methods - {"HEAD", "OPTIONS"}))
              for r in rules
              if (r.methods - {"HEAD", "OPTIONS", "GET"}) - {"DELETE"}]
    check("no market route accepts POST, PUT or PATCH", writes == [])
    check("the only non-GET is DELETE /api/markets/cache (in-memory only)",
          sorted(set(str(r.rule) for r in rules if "DELETE" in r.methods))
          == ["/api/markets/cache"])
    check("the five data endpoints are GET-only",
          all(r.methods - {"HEAD", "OPTIONS"} == {"GET"}
              for r in rules if str(r.rule) != "/api/markets/cache"))
    check("no market route takes an enrollment id",
          not any("<" in str(r.rule) for r in rules))

    # ══════════════════ 6. NY enrollment behaviour untouched ══════════════════
    print("\n== 6. NY enrollment behaviour is untouched ==")
    # PHASE 3 UPDATED THIS DELIBERATELY.
    #
    # In Phase 2 the market layer was a pure addition and NOTHING imported it,
    # which this suite asserted. Phase 3 was approved to integrate it, so two
    # files now import it on purpose: adapter.py (market-published account
    # field rules) and perch_routes.py (the county source and the status rule).
    #
    # The guarantee this check exists to protect - that NY behaviour is
    # unchanged - is now asserted directly and far more strongly in
    # test_virginia_enrollment.py: NY counties still come from ny_counties, NY
    # still sends status, and the market lookups fail safe toward today's
    # behaviour. So the list below becomes "which files must STILL never reach
    # the market layer", which is the part that remains load-bearing.
    def imports_markets(text):
        # Substring-robust: an import buried in a multi-name import line
        # ("from services.perch import adapter, workflow, markets") must count.
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(("import ", "from ")):
                continue
            if "markets" in line and "perch" in line:
                return True
        return False

    must_not = {}
    for sub, names in (("routes", ["enrollment_routes.py", "document_routes.py",
                                   "report_routes.py"]),
                       ("services", ["documents.py", "lmi_validation.py"]),
                       (os.path.join("services", "perch"),
                        ["client.py", "workflow.py", "utilities.py",
                         "token_manager.py", "config.py", "hmac_auth.py"])):
        for n in names:
            must_not[f"{sub}/{n}"] = open(os.path.join(ROOT, sub, n),
                                          encoding="utf-8").read()
    for name, text in must_not.items():
        check(f"{name} still does not import the markets layer",
              not imports_markets(text))

    # The two Phase 3 integration points, asserted POSITIVELY so the
    # integration cannot silently disappear either.
    for name in ("services/perch/adapter.py", "routes/perch_routes.py"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        check(f"{name} imports the markets layer (Phase 3 integration)",
              imports_markets(text))
    check("markets.py does not import back into the routes or adapter",
          not any(k in open(MARKETS_SRC_PATH, encoding="utf-8").read()
                  for k in ("from routes", "import adapter")))

    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    check("app.py registers market_routes", "market_routes.bp" in app_src)
    check("  ...and no existing blueprint registration was removed",
          all(f"{b}.bp" in app_src for b in (
              "auth_routes", "enrollment_routes", "document_routes", "agreement_routes",
              "signing_routes", "access_routes", "qa_routes", "developer_routes",
              "submission_routes", "report_routes", "project_routes", "perch_routes",
              "admin_routes")))

    existing = set(str(r.rule) for r in app.url_map.iter_rules()
                   if not str(r.rule).startswith("/api/markets"))
    market = set(str(r.rule) for r in app.url_map.iter_rules()
                 if str(r.rule).startswith("/api/markets"))
    check(f"no market URL shadows an existing one ({len(existing)} pre-existing URLs)",
          existing.isdisjoint(market))
    check("  ...and /api/markets was previously unused",
          not any(u.startswith("/api/markets") for u in existing))
    check("the NY self-attestation route is still registered and unchanged in shape",
          any(str(r.rule).endswith("/lmi/self-attestation") for r in app.url_map.iter_rules()))

    # the NY county table is still the live source for the EXISTING NY route
    with app.app_context():
        from db import query_one
        row = query_one("SELECT COUNT(*) AS n FROM ny_counties WHERE is_active = 1")
        check(f"ny_counties still holds all 62 NY counties (got {row['n']})", row["n"] == 62)

    # ══════════════════ 7. nothing hardcoded ══════════════════
    print("\n== 7. no market values hardcoded in Phase 2 source ==")
    for path, name in ((MARKETS_SRC_PATH, "markets.py"), (ROUTES_SRC_PATH, "market_routes.py")):
        code = executable_source(path)
        low = code.lower()
        check(f"{name}: no Dominion slug in executable code", "dominion" not in low)
        check(f"{name}: no account-number pattern in executable code",
              "[0-9]{10}" not in code and "[0-9]{12}" not in code and r"\d{10}" not in code)
        check(f"{name}: no Virginia county literal in executable code",
              "martinsville" not in low)
        check(f"{name}: no 'VA' literal in executable code", '"VA"' not in code and "'VA'" not in code)
    mk_code = executable_source(MARKETS_SRC_PATH)
    # TWO documented NY literals now, not one. The second is the fail-closed
    # default market: enabled_markets() returns NY alone unless an environment
    # NAMES more, so the live market is what an unconfigured deploy serves.
    # The count is kept exact rather than relaxed to ">= 1" - that is what
    # makes a third literal, or a VA literal, fail here.
    check(f"markets.py has exactly the two documented NY literals "
          f"(got {mk_code.count(chr(39) + 'NY' + chr(39)) + mk_code.count(chr(34) + 'NY' + chr(34))})",
          mk_code.count("'NY'") + mk_code.count('"NY"') == 2)
    check("  ...one guarding the ny_counties read", "ny_counties" in mk_code)
    check("  ...the other the default enabled market",
          "DEFAULT_MARKETS" in mk_code)
    check("  ...and the default names NO market but the live one",
          "VA" not in mk_code.split("DEFAULT_MARKETS")[1].splitlines()[0])
    check("the status rule is computed from the response, not a state comparison",
          "bool(self_attestation_options(state)['status'])" in mk_code.replace('"', "'"))

    markets.set_client_factory(None)
    print(f"\n{'=' * 72}")
    print("MARKET RULES SERVICE - ALL CHECKS PASSED")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
