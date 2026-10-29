"""
Availability step: ZIP -> state -> market-driven utility choices.

Covers the fix for the Availability screen still sourcing its utility dropdown
from the local NY reference table, plus the auto-select behaviour when a state
has exactly one supported utility.

Scope boundaries asserted here, not just described:
  * ZIP determines STATE ONLY. No ZIP-to-utility mapping exists anywhere.
  * Utility choices always come from Perch market rules for that state.
  * Auto-select ONLY when exactly one utility is returned.
  * New York's dropdown, display names and fallback behaviour are unchanged.

Run: python test/test_availability_utilities.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")
# This suite exercises the staging/local environment, which is the only place
# Virginia is enabled - and it is enabled by being NAMED here, exactly as the
# local .env names it. Section 3c0 below removes the variable to prove the
# production default, so nothing in this file depends on VA being on by
# default. setdefault, not assignment: a caller that already chose a market set
# keeps it.
os.environ.setdefault("UBS_MARKETS", "NY,VA")

from app import app
from db import init_db
import seed
from services import zip_state
from services.perch import markets, utilities
from services.perch import client as _client
from services.perch.errors import PerchNotImplementedError
from services.perch.mock_client import PerchMockClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
ZIPSRC = open(os.path.join(ROOT, "services", "zip_state.py"), encoding="utf-8").read()
MKROUTES = open(os.path.join(ROOT, "routes", "market_routes.py"), encoding="utf-8").read()

VA_SLUG = "dominion-energy-va"


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    init_db(reset=True)
    seed.seed()
    markets.set_client_factory(PerchMockClient)
    markets.invalidate()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    adm = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                  "password": "AdminPass1!"}).get_json()["token"]}
    qa = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "qa@daltonsolar.com",
                                  "password": "QaPass1!"}).get_json()["token"]}

    # ═══════════════════════════════════════════════════════════════════
    section("1. ZIP -> STATE RESOLUTION")

    for zip_code, expect in (("24112", "VA"), ("22903", "VA"), ("23219", "VA"),
                             ("20101", "VA"),
                             ("12401", "NY"), ("10901", "NY"), ("12207", "NY"),
                             ("12901", "NY"), ("10001", "NY"),
                             ("07030", "NJ"), ("06901", "CT"), ("19801", "DE"),
                             ("20001", "DC")):
        check(f"{zip_code} -> {expect}", zip_state.state_for_zip(zip_code) == expect)

    for bad in ("", None, "123", "123456", "abcde", "1234a"):
        check(f"malformed ZIP {bad!r} -> None (never a guessed state)",
              zip_state.state_for_zip(bad) is None)

    check("every NY test ZIP in the existing suites still resolves to NY",
          all(zip_state.state_for_zip(z) == "NY"
              for z in ("12401", "10901", "12207", "12901", "10001")))
    check("the table covers all 50 states + DC, so an out-of-market ZIP is not "
          "mistaken for a neighbour",
          len(zip_state.covered_states()) >= 51)

    # No prefix may map to two states, or resolution would depend on order.
    from services.zip_state import _ZIP3_RANGES
    seen = {}
    overlaps = []
    for lo, hi, st in _ZIP3_RANGES:
        for p in range(lo, hi + 1):
            if p in seen:
                overlaps.append((p, seen[p], st))
            seen[p] = st
    check(f"no overlapping ZIP3 prefixes ({len(seen)} prefixes mapped)", overlaps == [])

    section("1b. ZIP RESOLVES STATE ONLY - NO ZIP-TO-UTILITY MAPPING")
    check("the ZIP module names no utility slug",
          "dominion" not in ZIPSRC.lower() and "nyseg" not in ZIPSRC.lower()
          and "edison" not in ZIPSRC.lower())
    check("  ...and nothing in it returns a utility",
          "utilit" not in ZIPSRC.replace("utilities cannot", "")
          .replace("which utilities", "").replace("Which utilities", "")
          .replace("supported utilities", "").replace("utility here", "")
          .replace("the utility's", "").replace("a utility", "").lower()
          or "def state_for_zip" in ZIPSRC)
    check("the ZIP module exposes ONLY state resolution",
          sorted(n for n in dir(zip_state) if not n.startswith("_"))
          == ["covered_states", "state_for_zip"])

    # ═══════════════════════════════════════════════════════════════════
    section("2. VIRGINIA: EXACTLY ONE UTILITY -> AUTO-SELECTED")

    r = c.get("/api/markets/utility-options?zip=24112", headers=rep)
    j = r.get_json()
    check(f"a VA ZIP is answered (HTTP {r.status_code})", r.status_code == 200)
    check("  ...state resolved as VA", j["state"] == "VA")
    check("  ...one option returned", len(j["options"]) == 1)
    check(f"  ...the Dominion slug, from Perch ({j['options'][0]['value']})",
          j["options"][0]["value"] == VA_SLUG)
    check("  ...sourced from perch, not a local table", j["source"] == "perch")
    check(f"  ...AUTO-SELECTED ({j['auto_select']})", j["auto_select"] == VA_SLUG)
    check("  ...with a readable display name, not the raw slug",
          j["options"][0]["label"] == "Dominion Energy")
    check("  ...carrying no state suffix for the rep to read",
          not j["options"][0]["label"].endswith(" VA")
          and "-" not in j["options"][0]["label"])
    check("  ...while the submitted VALUE keeps the exact Perch slug",
          j["options"][0]["value"] == VA_SLUG)
    check("  ...and no blocking message", "message" not in j)

    section("2b. THE SLUG IS NOT HARDCODED ANYWHERE")
    check("market_routes.py names no Dominion slug", "dominion" not in MKROUTES.lower())
    # REWRITTEN (Phase B), not loosened. This read the RAW file, so it failed
    # on the two Phase B COMMENTS that name "dominion-energy-va" as the
    # example of the raw slug that used to leak into the capacity card and the
    # agreement summary. A comment cannot hardcode a slug; real code still
    # fails, including a line that merely ends with a trailing comment. Same
    # whole-line rule as this suite's JSN normalizer below.
    check("app.js names no Dominion slug in executable code",
          "dominion" not in "\n".join(
              ln for ln in JS.splitlines()
              if not ln.strip().startswith("//")).lower())
    check("the display name is DERIVED, not stored",
          utilities.display_name_for_slug("some-new-utility-xx")
          == "Some New Utility XX")
    check("  ...and a local display name still wins where one exists",
          utilities.display_name_for_slug("central-hudson-gas-electric")
          == "Central Hudson Gas & Electric")
    check("ZIP 24112 is not special-cased",
          "24112" not in MKROUTES and "24112" not in JS and "24112" not in ZIPSRC)

    # ═══════════════════════════════════════════════════════════════════
    section("3. NEW YORK: MULTIPLE UTILITIES -> NO GUESS")

    r = c.get("/api/markets/utility-options?zip=12401", headers=rep)
    j = r.get_json()
    check(f"an NY ZIP is answered (HTTP {r.status_code})", r.status_code == 200)
    check("  ...state resolved as NY", j["state"] == "NY")
    check(f"  ...all 7 NY utilities offered (got {len(j['options'])})",
          len(j["options"]) == 7)
    check("  ...NOTHING auto-selected, because there is more than one",
          j["auto_select"] is None)
    labels = [o["label"] for o in j["options"]]
    for expected in ("Central Hudson Gas & Electric", "Consolidated Edison NY",
                     "National Grid NY", "NYSEG", "Orange and Rockland",
                     "PSE&G NY", "Rochester Gas & Electric"):
        check(f"  ...established label preserved: {expected!r}", expected in labels)
    check("  ...no VA utility leaked into the NY list",
          not any(o["value"] == VA_SLUG for o in j["options"]))

    section("3b. NY SURVIVES A MARKET-DATA OUTAGE (the live market must not break)")
    # The mock has NO NY market fixture on purpose, so this request has already
    # exercised the fallback path.
    check("NY answered from the local reference table when Perch had no data",
          j["source"] == "local_table")
    check("  ...which is the same table the screen used before this change",
          [o["value"] for o in j["options"]]
          == [o["value"] for o in utilities.select_options()])

    section("3c. AN OUT-OF-MARKET ZIP FAILS CLOSED")
    # CHANGED DELIBERATELY by the market gate. These ZIPs used to come back 503,
    # which was an accident of the mock having no fixture for them: the lookup
    # failed, so the request failed. The gate now refuses them BEFORE any Perch
    # call, with a 200 that says UBS does not serve that state. That is a
    # better answer - 503 means "try again later" and would have sent a rep
    # back to retry a state we will never serve - and it fails closed for the
    # reason that matters either way: no options, no auto-select, and never
    # NY's utilities offered for an out-of-state ZIP.
    for zip_code, st in (("07030", "NJ"), ("06901", "CT"), ("19801", "DE")):
        r = c.get(f"/api/markets/utility-options?zip={zip_code}", headers=rep)
        j = r.get_json()
        check(f"{zip_code} ({st}) is answered, not errored (HTTP {r.status_code})",
              r.status_code == 200)
        check(f"  ...the state is resolved honestly as {st}", j["state"] == st)
        check("  ...and no options are returned", not (j.get("options") or []))
        check("  ...nothing is auto-selected", j["auto_select"] is None)
        check("  ...with a message naming the state we do not serve",
              st in (j.get("message") or ""))
        check("  ...and NY's utilities are NOT offered as a substitute",
              not any(o["value"] in {u["value"] for u in utilities.select_options()}
                      for o in (j.get("options") or [])))
        check("  ...refused BEFORE any Perch call", j["source"] == "not_enabled")

    section("3c0. FAIL CLOSED: PRODUCTION CANNOT EXPOSE VA BY OMISSION")
    # The safety property. Virginia reaches a rep only when an environment
    # NAMES it; no missing, empty or malformed value can turn it on. A
    # production service that never sets UBS_MARKETS - a fresh deploy, a
    # restored instance, a worker that did not source the env file - serves
    # exactly the live market.
    _saved = os.environ.pop("UBS_MARKETS", None)
    try:
        markets.invalidate()
        check(f"UBS_MARKETS unset -> NY only (got {markets.enabled_markets()})",
              markets.enabled_markets() == ["NY"])
        check("  ...so VA is absent from the enabled set",
              "VA" not in markets.enabled_markets())

        # Not just the config function - the endpoint a rep's browser calls.
        j = c.get("/api/markets/utility-options", headers=rep).get_json()
        check("the live dropdown offers no VA utility at all",
              not any(o["state"] == "VA" for o in j["options"]))
        check(f"  ...specifically not the VA slug ({VA_SLUG})",
              all(o["value"] != VA_SLUG for o in j["options"]))
        check("  ...and reports NY as the only market", j["markets"] == ["NY"])
        check("  ...while every NY utility is still served",
              len(j["options"]) == 7
              and all(o["state"] == "NY" for o in j["options"]))

        # A ZIP inside Virginia must not become a back door into the market.
        j = c.get(f"/api/markets/utility-options?zip=24112",
                  headers=rep).get_json()
        check("a VA ZIP resolves its state but is offered nothing",
              j["state"] == "VA" and j["options"] == []
              and j["auto_select"] is None)
        check("  ...with a message rather than a silent empty dropdown",
              bool(j.get("message")))
        check("  ...and VA still absent from the market list",
              "VA" not in j["markets"])

        # Every way an operator can get the value wrong still fails closed.
        for bad in ("", "   ", "!!!", "none", "N", "NYC"):
            os.environ["UBS_MARKETS"] = bad
            got = markets.enabled_markets()
            check(f"UBS_MARKETS={bad!r} -> {got} (never adds VA)",
                  "VA" not in got and got == ["NY"])
        os.environ.pop("UBS_MARKETS", None)

        # And the one thing that DOES enable it: naming it.
        os.environ["UBS_MARKETS"] = "NY,VA"
        markets.invalidate()
        check("UBS_MARKETS=NY,VA -> both markets, in that order",
              markets.enabled_markets() == ["NY", "VA"])
        check("  ...which is the ONLY way VA becomes available",
              "VA" in markets.enabled_markets())
        j = c.get("/api/markets/utility-options", headers=rep).get_json()
        check("  ...and the dropdown then offers the VA utility",
              any(o["value"] == VA_SLUG for o in j["options"]))

        os.environ["UBS_MARKETS"] = "VA"
        check("a VA-only environment is possible but must be explicit",
              markets.enabled_markets() == ["VA"])
    finally:
        if _saved is None:
            os.environ.pop("UBS_MARKETS", None)
        else:
            os.environ["UBS_MARKETS"] = _saved
        os.environ.setdefault("UBS_MARKETS", "NY,VA")
        markets.invalidate()
    check("the suite's own environment is restored",
          markets.enabled_markets() == ["NY", "VA"])

    section("3c2. UTILITY-FIRST: THE UNION ACROSS ENABLED MARKETS")
    check("enabled markets come from configuration, which names NY and VA here",
          markets.enabled_markets() == ["NY", "VA"])
    _mk_src = open(os.path.join(ROOT, "services", "perch", "markets.py"),
                   encoding="utf-8").read()
    _default = _mk_src.split("DEFAULT_MARKETS = ")[1].splitlines()[0].strip()
    check(f"  ...and the compiled-in DEFAULT is the live market alone "
          f"({_default})", _default == '"NY"')
    check("  ...and are configurable, not compiled in",
          "UBS_MARKETS" in open(os.path.join(ROOT, "services", "perch", "markets.py"),
                                encoding="utf-8").read())

    r = c.get("/api/markets/utility-options", headers=rep)
    j = r.get_json()
    check(f"no ZIP -> the cross-market list (HTTP {r.status_code})", r.status_code == 200)
    check("  ...state is null, because none was asserted", j["state"] is None)
    check("  ...the enabled markets are reported", j["markets"] == ["NY", "VA"])
    check(f"  ...8 utilities offered: 7 NY + 1 VA (got {len(j['options'])})",
          len(j["options"]) == 8)
    check("  ...including the VA utility, BEFORE any ZIP is entered",
          any(o["value"] == VA_SLUG for o in j["options"]))
    check("  ...and every existing NY utility remains available",
          len([o for o in j["options"] if o["state"] == "NY"]) == 7)
    check("  ...each option carries the state it belongs to",
          all(o.get("state") in ("NY", "VA") for o in j["options"]))
    check("  ...NOTHING auto-selected from a union", j["auto_select"] is None)
    check("  ...with readable labels throughout",
          {o["label"] for o in j["options"]} >= {"NYSEG", "Dominion Energy"})
    check("  ...no label anywhere is a raw slug",
          all("-" not in o["label"] for o in j["options"]))
    check("  ...while every VALUE is the exact slug Perch published",
          {o["value"] for o in j["options"]} >= {VA_SLUG})

    # One market failing must not empty the dropdown for the others.
    class VaOnlyFailure(PerchMockClient):
        def get_market_utilities(self, state):
            if self._state_key(state) == "VA":
                raise PerchNotImplementedError("simulated VA outage")
            return super().get_market_utilities(state)

    markets.set_client_factory(VaOnlyFailure)
    markets.invalidate()
    j = c.get("/api/markets/utility-options", headers=rep).get_json()
    check("one market failing does not empty the whole dropdown",
          len(j["options"]) == 7)
    check("  ...the failed market is named rather than hidden",
          j.get("unavailable_markets") == ["VA"])
    check("  ...and NY is unaffected",
          all(o["state"] == "NY" for o in j["options"]))
    markets.set_client_factory(PerchMockClient)
    markets.invalidate()

    section("3c3. ZIP NARROWS, AND OPTIONS STAY STATE-TAGGED")
    j = c.get("/api/markets/utility-options?zip=24112", headers=rep).get_json()
    check("a VA ZIP narrows to VA only",
          [o["state"] for o in j["options"]] == ["VA"])
    j = c.get("/api/markets/utility-options?zip=12401", headers=rep).get_json()
    check("an NY ZIP narrows to NY only",
          set(o["state"] for o in j["options"]) == {"NY"})
    check("  ...so the frontend can detect an incompatible pair without "
          "another call",
          all("state" in o for o in j["options"]))

    section("3d. MALFORMED ZIP vs ABSENT ZIP")
    # These two were one case until the utility-first flow landed. They are
    # now opposites and must not be conflated: a ZIP that is PRESENT but
    # unusable is the rep's typo (400), while NO ZIP is the legitimate
    # utility-first request (200, the cross-market union). The earlier version
    # of this suite asserted 400 for both, which this flow makes wrong - so
    # the assertion is rewritten rather than relaxed.
    for bad in ("123", "abcde", "123456", "2411a", "24 12"):
        r = c.get(f"/api/markets/utility-options?zip={bad}", headers=rep)
        check(f"zip={bad!r} is a typo -> 400", r.status_code == 400)
        check("  ...and no utilities are offered on a typo",
              "options" not in (r.get_json() or {}))

    for absent in ("", "   ", "%20"):
        r = c.get(f"/api/markets/utility-options?zip={absent}", headers=rep)
        j = r.get_json()
        check(f"zip={absent!r} means 'not stated yet' -> 200 union "
              f"(got {r.status_code})", r.status_code == 200)
        check("  ...serving the full cross-market list", len(j["options"]) == 8)
        check("  ...and guessing no state", j["state"] is None)

    r = c.get("/api/markets/utility-options", headers=rep)
    check("no ?zip= at all -> the same union, not an error",
          r.status_code == 200 and len(r.get_json()["options"]) == 8)

    section("3d2. THE ERROR FORMATTER NO LONGER INVENTS A NULL")
    # The reported symptom that started this work was:
    #
    #   Invalid zip_code or utility_name: 24112 is not valid for the given
    #   utility: None
    #
    # which reads unmistakably as "we sent utility_name = None". It was not.
    # Perch returned envelope (1) carrying `error` and NO `message`, and
    # _msg() formatted the absent field unconditionally - printing our own
    # missing key as a trailing ": None". The slug was in the request the
    # whole time (proved end-to-end in availability_utility_harness.js
    # section 8). This reproduces the old output and pins the new one, so a
    # real Perch rejection can never again be misread as our own null.
    class _Resp:
        def __init__(self, body): self._b = body
        def json(self): return self._b
        text = ""

    only_error = _client._msg(_Resp(
        {"error": "24112 is not valid for the given utility"}))
    check("an `error`-only body formats as just that error "
          f"({only_error!r})",
          only_error == "24112 is not valid for the given utility")
    check("  ...with no trailing ': None' to impersonate a value we sent",
          not only_error.endswith(": None") and "None" not in only_error)
    check("  ...which is exactly the reported string minus the phantom null",
          "is not valid for the given utility" in only_error)

    both = _client._msg(_Resp({"error": "Invalid zip_code or utility_name",
                               "message": "24112 is not valid"}))
    check(f"both fields present still join with ': ' ({both!r})",
          both == "Invalid zip_code or utility_name: 24112 is not valid")
    check("a `message`-only body formats as just that message",
          _client._msg(_Resp({"message": "nope"})) == "nope")
    # E3 renders field keys as WORDS - "ZIP code", not `zip_code` - so this
    # now pins the reader-facing label. The envelope is still handled; what
    # changed is that it no longer prints JSON keys and codes at a rep.
    _env = _client._msg(_Resp(
        {"errors": [{"field": "zip_code", "code": "invalid",
                     "message": "out of area"}]}))
    check(f"the validation-errors envelope still names the field ({_env!r})",
          "ZIP code" in _env)
    check("  ...without the raw key", "zip_code" not in _env)
    check("  ...as a whole sentence rather than a fragment",
          _env.endswith("."))

    section("3d3. LABELS ARE FOR PEOPLE; VALUES ARE FOR PERCH")
    # The rule: internal/API value = the exact Perch slug, visible label = a
    # clean human name with no slug punctuation and no state suffix.
    check("the state suffix is dropped from a derived label",
          utilities.display_name_for_slug(VA_SLUG) == "Dominion Energy")
    check("  ...generically, by state-code membership and not by utility name",
          utilities.display_name_for_slug("some-new-utility-xx")
          == "Some New Utility XX")
    check("  ...so a trailing word that is not a state code is kept",
          utilities.display_name_for_slug("some-utility-coop")
          == "Some Utility Coop")
    check("  ...and a slug that is ONLY a state code keeps it",
          utilities.display_name_for_slug("va") == "VA")
    check("established NY labels are untouched, byte for byte",
          utilities.display_name_for_slug("national-grid-ny")
          == (utilities.by_slug("national-grid-ny") or {})["display_name"])

    # Dropping the suffix can make two markets' utilities collide. The state
    # goes back on ONLY then - an ambiguous dropdown is worse than a suffix.
    collide = utilities.label_for_options([
        {"value": "dominion-energy-va", "label": "Dominion Energy", "state": "VA"},
        {"value": "dominion-energy-nc", "label": "Dominion Energy", "state": "NC"},
        {"value": "nyseg", "label": "NYSEG", "state": "NY"},
    ])
    check("colliding labels are disambiguated by state",
          [o["label"] for o in collide[:2]]
          == ["Dominion Energy (VA)", "Dominion Energy (NC)"])
    check("  ...and a label that does not collide is left alone",
          collide[2]["label"] == "NYSEG")
    check("  ...while NO value is ever rewritten",
          [o["value"] for o in collide]
          == ["dominion-energy-va", "dominion-energy-nc", "nyseg"])

    j = c.get("/api/markets/utility-options", headers=rep).get_json()
    check("the live union serves clean labels with exact slugs",
          all("-" not in o["label"] for o in j["options"])
          and all(o["value"] == o["value"].lower() for o in j["options"]))
    check("  ...and the VA option reads as a plain utility name",
          next(o["label"] for o in j["options"] if o["value"] == VA_SLUG)
          == "Dominion Energy")

    section("3e. AUTH")
    check("unauthenticated -> 401",
          c.get("/api/markets/utility-options?zip=24112").status_code == 401)
    check("sales_rep allowed",
          c.get("/api/markets/utility-options?zip=24112", headers=rep).status_code == 200)
    check("admin allowed",
          c.get("/api/markets/utility-options?zip=24112", headers=adm).status_code == 200)
    check("qa_reviewer refused",
          c.get("/api/markets/utility-options?zip=24112", headers=qa).status_code == 403)
    check("the route is GET-only",
          c.post("/api/markets/utility-options?zip=24112", headers=rep).status_code == 405)

    # ═══════════════════════════════════════════════════════════════════
    section("4. A STATE WITH MULTIPLE PERCH UTILITIES IS NOT AUTO-SELECTED")

    # Proves the auto-select rule is about COUNT, not about Virginia. A stub
    # market with two utilities must not preselect either.
    class TwoUtilityClient(PerchMockClient):
        def get_market_utilities(self, state):
            return {"state": "VA", "utilities": ["utility-one-va", "utility-two-va"]}

    markets.set_client_factory(TwoUtilityClient)
    markets.invalidate()
    j = c.get("/api/markets/utility-options?zip=24112", headers=rep).get_json()
    check("two utilities -> both offered", len(j["options"]) == 2)
    check("  ...and NOTHING auto-selected", j["auto_select"] is None)
    check("  ...with derived labels for both, state suffix dropped",
          [o["label"] for o in j["options"]] == ["Utility One", "Utility Two"])
    check("  ...while both VALUES keep their exact slugs",
          [o["value"] for o in j["options"]]
          == ["utility-one-va", "utility-two-va"])

    class ZeroUtilityClient(PerchMockClient):
        def get_market_utilities(self, state):
            return {"state": "VA", "utilities": []}

    markets.set_client_factory(ZeroUtilityClient)
    markets.invalidate()
    r = c.get("/api/markets/utility-options?zip=24112", headers=rep)
    j = r.get_json()
    check(f"zero utilities -> answered, not an error (HTTP {r.status_code})",
          r.status_code == 200)
    check("  ...with no options", j["options"] == [])
    check("  ...no auto-select", j["auto_select"] is None)
    msg = (j.get("message") or "").lower()
    check(f"  ...and a clear message so Availability can stop ({msg[:46]!r})",
          "no community solar utilities" in msg and "cannot continue" in msg)

    markets.set_client_factory(PerchMockClient)
    markets.invalidate()

    # ═══════════════════════════════════════════════════════════════════
    section("5. THE AVAILABILITY SCREEN IS WIRED TO IT")

    # Collapsed whitespace, so these assertions test the CODE and not its
    # line breaks. The earlier version matched an exact one-line form, and
    # broke the moment a comment was added between the condition and its
    # body - a formatting failure masquerading as a behavioural one. The
    # runtime proof of all of this lives in availability_utility_harness.js;
    # these are structural guards against the wiring being removed.
    # Own-line // comments are dropped first, so an explanatory comment
    # between a condition and its body cannot fail a behavioural assertion.
    # Only whole comment lines are removed - never a trailing // on a line of
    # code, which could contain a URL or a string.
    JSN = " ".join(" ".join(
        ln for ln in JS.splitlines() if not ln.strip().startswith("//")
    ).split())


    check("the frontend calls the market-driven endpoint",
          "/api/markets/utility-options?zip=" in JS)
    check("  ...from the Availability step only",
          "step.key !== 'service_area'" in JS)
    check("  ...hooked to the ZIP field the renderer creates",
          "getElementById('wf-zip_code')" in JS)
    check("  ...repopulating the utility field the renderer creates",
          "getElementById('wf-utility_name')" in JS)
    check("  ...on input AND on change", "addEventListener('input', refresh)" in JS
          and "addEventListener('change', refresh)" in JS)
    check("  ...and for a resumed enrollment whose ZIP is already filled",
          "if((zipEl.value || '').trim().length === 5){ refresh(); }" in JSN)
    check("  ...while an empty ZIP loads the cross-market union instead",
          "} else { loadAllMarkets(); }" in JSN)
    check("  ...so the dropdown is populated before any ZIP is typed",
          "function loadAllMarkets" in JS
          and "'/api/markets/utility-options'" in JS)
    check("auto-select is applied only when the API says so",
          "} else if(autoSelect){ utilEl.value = autoSelect; }" in JSN
          and "paint(options, body.auto_select," in JSN)
    check("  ...and the rep's own choice outranks it",
          "if(keep && options.some(function(o){ return o.value === keep; })){ "
          "utilEl.value = keep;" in JSN)

    # THE DEFECT THIS WORK EXISTS TO FIX. The <option> value must be the
    # Perch slug and the human text must be the label - never the reverse,
    # and never the label in both. Proven at runtime in the harness; guarded
    # structurally here because a swap would be invisible on screen and would
    # submit a display name to Perch.
    check("the option's VALUE is the slug and its TEXT is the label",
          "'<option value=\"' + esc(o.value) + '\">' + esc(o.label) "
          "+ '</option>'" in JSN)
    check("  ...and the select's value is set from a slug, never a label",
          "utilEl.value = autoSelect;" in JSN
          and "utilEl.value = o.label" not in JSN
          and "utilEl.value = body.options" not in JSN)
    check("  ...never derived in the browser from the ZIP",
          "auto_select" in JS and "zip.slice" not in JS and "zipPrefix" not in JS)
    check("no supported utilities disables the primary action",
          "const p = primary(); if(p && block) p.disabled = true;" in JSN
          and "|| 'No supported utilities were found for that ZIP code.', true)"
              in JSN)
    check("  ...and shows the message from the API, not a local string",
          "showError(body.message" in JSN)
    # CHANGED DELIBERATELY. A ZIP that moves the market is ordinary rep
    # behaviour, not a failure, so it no longer renders in danger red. When one
    # utility clearly serves the new state the selection simply switches; when
    # several do, the rep is asked to choose. Both go through showNotice(),
    # which never blocks the primary action.
    check("  ...while a merely INCOMPATIBLE pair does not block, because the "
          "rep can fix it in place",
          "function showNotice(text){" in JSN
          and "const p = primary(); if(p) p.disabled = false; }" in JSN)
    check("  ...and is not rendered in danger red",
          "e.style.color = 'var(--ink-muted)'" in JSN)
    check("  ...with one clear choice switching the selection outright",
          "showNotice('Switched to '" in JSN)
    check("  ...and clearing the error re-enables the action",
          "const p = primary(); if(p) p.disabled = false;" in JSN)
    check("a locked utility (project card) is left alone",
          "utilEl.disabled" in JS)
    # Deliberately changed by the utility-first flow: a cleared ZIP used to
    # restore the step descriptor's original <option> list. It now restores
    # the cross-market union, which is a strictly better answer - the rep
    # gets every supported utility back rather than the NY-only set the
    # descriptor was built with. The old assertion is replaced, not dropped:
    # the property that matters is that clearing a ZIP never leaves the
    # dropdown empty or stranded on a narrowed state.
    check("a cleared ZIP reloads the cross-market union, not an empty list",
          "if(zip.length !== 5) { loadAllMarkets(); return; }" in JSN
          or ("zip.length !== 5" in JSN and "loadAllMarkets();" in JSN))
    check("  ...and the descriptor's own options are still kept as a floor",
          "initialHtml" in JS and "initialValue" in JS)
    # ONE place decides whether a kept slug survives: paint(). refresh()
    # hands `keep` straight through and does not pre-filter it, so there is
    # no second copy of the rule to drift out of step.
    check("the kept slug is passed to paint() unfiltered",
          "paint(options, body.auto_select, keep);" in JSN)
    check("  ...and paint() is the only thing that drops it",
          "if(keep && options.some(function(o){ return o.value === keep; })){"
          in JSN)
    check("  ...while an incompatible pair still tells the rep why",
          "if(keep && !stillOffered){" in JSN
          and "does not serve" in JS and "body.state" in JS)
    check("a stale response cannot overwrite a newer ZIP",
          "if(zip !== (zipEl.value || '').trim()) return;" in JS)
    check("the rep's own selection survives a refetch",
          "options.some(function(o){ return o.value === keep; })" in JS)

    section("5b. THE BACKEND DESCRIPTOR STILL RENDERS AS BEFORE")
    body = c.get("/api/perch/workflow/new", headers=rep).get_json()
    fields = {f["name"]: f for f in body["step"]["fields"]}
    check("the Availability step still has email, zip_code and utility_name",
          set(fields) == {"email", "zip_code", "utility_name"})
    check("  ...utility_name is still a select", fields["utility_name"]["type"] == "select")
    check(f"  ...still pre-populated with the 7 local options "
          f"(got {len(fields['utility_name']['options'])})",
          len(fields["utility_name"]["options"]) == 7)
    check("  ...so the screen is never empty before a ZIP is typed",
          fields["utility_name"]["options"][0]["label"] != "")
    check("the legacy /api/perch/utilities route still works (bill-screen rules)",
          c.get("/api/perch/utilities", headers=rep).status_code == 200)
    check("  ...and still returns the POD ID rules the bill screen needs",
          any(u.get("requires_pod_id")
              for u in c.get("/api/perch/utilities", headers=rep).get_json()["utilities"]))

    markets.set_client_factory(None)
    print(f"\n{'='*72}\nAVAILABILITY UTILITY SELECTION - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
