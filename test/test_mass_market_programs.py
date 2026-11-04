"""
E5 - Mass market is a program a rep can sell, and it reaches Perch correctly.

THE PRODUCTION BUG

    NYSEG, Orange & Rockland and Central Hudson return mass-market capacity
    with a valid savings rate and NO true residential capacity. UBS offered
    Residential anyway - it is available on mass-market capacity - but read its
    rates from the `residential` entry, which is empty. The card arrived with
    no rate, flagged `savings_unresolved`, and the percentage Perch had
    actually published sat unreachable in `savings_candidates`. The rep saw an
    Open program they could not enroll.

WHY THE FIX IS A NEW PROGRAM RATHER THAN A FALLBACK

    Reading the mass-market rate into the Residential program would have fixed
    the symptom by reintroducing what E2.1 removed on purpose: a rate
    substituted across capacity types - a number Perch never quoted for that
    program, in a figure a rep reads out to a customer.

    So the program is named after the capacity that prices it. The rate is read
    from the entry Perch published it on, nothing is substituted anywhere, and
    when a market returns both types the two stay distinct with their own
    rates.

WHAT IS A PROGRAM KEY AND WHAT IS A WIRE VALUE

    `mass_market` is a CAPACITY type. POST /enroll takes only Residential /
    Business / LMI / LMI_IRA, and the v2 spec maps "residential or mass market"
    capacity to customer_type Residential. So MassMarket is a UBS PROGRAM KEY -
    selected, persisted, and used to resolve the rate - that goes on the wire
    as `Residential`. Asserting both halves is most of this file.

Every value below is synthetic. Run: python test/test_mass_market_programs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database

os.environ.setdefault("PERCH_API_MODE", "mock")
# PERCH_API_VERSION IS NOT SET HERE. This suite needs v2, but the variable is
# process-wide and several suites in the same run assert v1 behaviour - an
# import-time assignment silently repointed them and broke four of them. It is
# scoped to main() instead, and restored, like test_e2_capacity_model does it.

import tempfile as _tempfile

from routes import document_routes as _document_routes
import helpers as _helpers

# Its OWN temporary storage. Other document suites repoint these module
# globals and do not restore them, so a suite that relies on the default paths
# passes alone and fails in a full run. BACKEND_ROOT matters too: the enroll
# path resolves the stored bill through helpers.resolve_stored_path().
_uploads = _tempfile.TemporaryDirectory(prefix="ubs-massmkt-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
_helpers.BACKEND_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query_one
import seed
from services.perch import adapter, capacity as cap, mock_client

RES = adapter.CUSTOMER_TYPE_RESIDENTIAL
MASS = adapter.PROGRAM_MASS_MARKET
LMI = adapter.CUSTOMER_TYPE_LMI
LMI_IRA = adapter.CUSTOMER_TYPE_LMI_IRA

# ZIP -> the shape Perch returns there, from the v2 mock fixtures.
Z_OR = "10901"          # 109: mass market only, + lmi_ira
Z_CENTRAL_HUDSON = "12401"   # 124: mass market only
Z_NYSEG = "11701"       # 117: mass market only, + lmi   (E5 fixture)
Z_NG_SAME = "13348"     # 133: residential + mass market at the SAME rate
Z_NG_DIFF = "11601"     # 116: residential 10 / mass market 7, + lmi + lmi_ira
Z_NG_RES_ONLY = "12001"  # 120: true residential only
Z_CON_ED = "10001"      # 100: residential + mass market + lmi

UTIL = {Z_OR: "orange-and-rockland",
        Z_CENTRAL_HUDSON: "central-hudson-gas-electric",
        Z_NYSEG: "nyseg",
        Z_NG_SAME: "national-grid-ny",
        Z_NG_DIFF: "national-grid-ny",
        Z_NG_RES_ONLY: "national-grid-ny",
        Z_CON_ED: "consolidated-edison-ny"}

# Per-utility synthetic account numbers, each a length its utility issues:
# the enroll path validates these for real, so one wrong length would fail for
# a reason that has nothing to do with programs.
ACCOUNT = {
    "orange-and-rockland": "90000000011",          # 10 or 11 digits
    "central-hudson-gas-electric": "900000000011",
    "nyseg": "90000000011",                        # 11 digits
    "national-grid-ny": "9000000001",              # 10 digits
    "consolidated-edison-ny": "900000000011",
}

import contextlib


@contextlib.contextmanager
def api_version(value):
    """Run a block with PERCH_API_VERSION set, then restore it."""
    saved = os.environ.get("PERCH_API_VERSION")
    try:
        os.environ["PERCH_API_VERSION"] = value
        yield
    finally:
        if saved is None:
            os.environ.pop("PERCH_API_VERSION", None)
        else:
            os.environ["PERCH_API_VERSION"] = saved


_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def main():
    """Scoped, so nothing this suite needs leaks into the rest of the run."""
    from services.perch.mock_client import PerchMockClient
    enforce = PerchMockClient.enforce_enroll_contract
    with api_version("v2"):
        try:
            _run()
        finally:
            # Opt-in and off by default for a reason: other suites read
            # "not implemented" as "UBS validation passed". Restored exactly.
            PerchMockClient.enforce_enroll_contract = enforce


def _run():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    counter = {"n": 0}

    def start(zip_code):
        counter["n"] += 1
        email = f"e5.{counter['n']}.{zip_code}@example.com"
        r = c.post("/api/perch/enrollments/capacity", headers=rep,
                   json={"email": email, "zip_code": zip_code,
                         "utility_name": UTIL[zip_code]})
        assert r.status_code == 200, (zip_code, r.status_code, r.get_json())
        return r.get_json()["enrollment_id"], email

    def programs(eid):
        return (c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep)
                .get_json()["available_programs"])

    def select(eid, key, rate=None):
        """Choose the program, and - separately - the rate.

        Two endpoints, as the app has them: /program takes the PROGRAM KEY and
        /savings-rate takes a number, validated against the rates that program
        offers. The split matters here: the rate is validated against the
        SELECTED PROGRAM, so choosing mass market must make the mass-market
        rate the valid one.
        """
        r = c.post(f"/api/perch/enrollments/{eid}/program",
                   headers=rep, json={"customer_type": key})
        if rate is None or r.status_code != 200:
            return r
        return c.post(f"/api/perch/enrollments/{eid}/savings-rate",
                      headers=rep, json={"savings_percent": rate})

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE REPORTED BUG: MASS MARKET IS NOW SELLABLE")
    # ════════════════════════════════════════════════════════════════════════
    for zip_code, name, rate in ((Z_NYSEG, "NYSEG", 5.0),
                                 (Z_OR, "Orange & Rockland", 5.0),
                                 (Z_CENTRAL_HUDSON, "Central Hudson", 5.0)):
        eid, _ = start(zip_code)
        offered = programs(eid)
        by_key = {p["customer_type"]: p for p in offered}
        check(f"{name}: the Open program is offered ({sorted(by_key)})",
              MASS in by_key)
        p = by_key[MASS]
        check(f"  ...quoting the rate Perch published ({p['savings_percents']})",
              p["savings_percents"] == [rate])
        check("  ...no longer flagged unresolved",
              p["savings_unresolved"] is False)
        check("  ...with nothing stranded in savings_candidates",
              p["savings_candidates"] == [])
        check("  ...read from the mass_market entry, not substituted",
              p["savings_source"] == cap.MASS_MARKET)
        check("  ...and it ENROLLS as Residential",
              p["perch_customer_type"] == RES)
        check(f"  ...a rep can actually select it",
              select(eid, MASS).status_code == 200)
        check("  ...and true Residential is NOT offered, having no capacity",
              RES not in by_key)

    # ════════════════════════════════════════════════════════════════════════
    section("2. TRUE RESIDENTIAL IS UNTOUCHED")
    # ════════════════════════════════════════════════════════════════════════
    eid, _ = start(Z_NG_RES_ONLY)
    offered = programs(eid)
    check(f"residential-only still offers exactly Residential "
          f"({[p['customer_type'] for p in offered]})",
          [p["customer_type"] for p in offered] == [RES])
    check("  ...labelled plainly, with no mass-market wording",
          offered[0]["label"] == "Residential")
    check("  ...quoting its own rate", offered[0]["savings_percents"] == [8.0])
    check("  ...sourced from the residential entry",
          offered[0]["savings_source"] == cap.RESIDENTIAL)
    check("  ...and selectable as before", select(eid, RES).status_code == 200)

    # ════════════════════════════════════════════════════════════════════════
    section("3. BOTH TYPES, DIFFERENT RATES: NEITHER HIDES THE OTHER")
    # ════════════════════════════════════════════════════════════════════════
    eid, _ = start(Z_NG_DIFF)
    offered = programs(eid)
    by_key = {p["customer_type"]: p for p in offered}
    check(f"all four programs are offered ({[p['customer_type'] for p in offered]})",
          sorted(by_key) == sorted([RES, MASS, LMI, LMI_IRA]))
    check("  ...Residential quotes its own rate",
          by_key[RES]["savings_percents"] == [10.0])
    check("  ...Mass Market quotes its own, different rate",
          by_key[MASS]["savings_percents"] == [7.0])
    check("  ...LMI still works", by_key[LMI]["savings_percents"] == [20.0])
    check("  ...LMI IRA still works",
          by_key[LMI_IRA]["savings_percents"] == [25.0])
    check("  ...every rate is distinct - none borrowed from another program",
          len({tuple(p["savings_percents"]) for p in offered}) == 4)
    check("  ...each program reads only its own capacity type",
          all(len(p["capacity_types"]) == 1 for p in offered))
    check("  ...no two cards share a label",
          len({p["label"] for p in offered}) == 4)
    check("  ...and the two Open programs are distinguishable to a rep",
          by_key[RES]["label"] != by_key[MASS]["label"])
    check("  ...while both enroll as Residential",
          by_key[RES]["perch_customer_type"] == RES
          and by_key[MASS]["perch_customer_type"] == RES)
    check("  ...and the income-qualified ones keep their own wire values",
          by_key[LMI]["perch_customer_type"] == LMI
          and by_key[LMI_IRA]["perch_customer_type"] == LMI_IRA)

    # ════════════════════════════════════════════════════════════════════════
    section("4. BOTH TYPES, IDENTICAL RATE: ONE CARD, NO LOST CAPACITY")
    # ════════════════════════════════════════════════════════════════════════
    for zip_code, name, rate, kw in ((Z_NG_SAME, "National Grid", 10.0, 7400.0),
                                     (Z_CON_ED, "Con Edison", 5.0, 7400.0)):
        eid, _ = start(zip_code)
        offered = programs(eid)
        keys = [p["customer_type"] for p in offered]
        check(f"{name}: one Open card, not two ({keys})",
              keys.count(RES) == 1 and MASS not in keys)
        res = next(p for p in offered if p["customer_type"] == RES)
        check(f"  ...quoting {rate}%", res["savings_percents"] == [rate])
        check("  ...with BOTH capacity types still reported",
              [t["capacity_type"] for t in res["capacity_types"]]
              == [cap.RESIDENTIAL, cap.MASS_MARKET])
        check(f"  ...and the kW of both, none dropped ({kw})",
              sum(t["capacity_available_kw"] for t in res["capacity_types"]) == kw)
        check("  ...LMI is never folded into anything",
              LMI in keys)

    # ════════════════════════════════════════════════════════════════════════
    section("5. SELECTION PERSISTS, AND SURVIVES A REOPEN")
    # ════════════════════════════════════════════════════════════════════════
    eid, _ = start(Z_NG_DIFF)
    check("selecting Mass Market at its own rate is accepted",
          select(eid, MASS, 7.0).status_code == 200)
    row = query_one("SELECT selected_customer_type AS t, "
                    "selected_savings_percent AS r FROM enrollments WHERE id=?",
                    (eid,))
    check(f"the PROGRAM KEY is what persists ({row['t']})", row["t"] == MASS)
    check(f"  ...with the rate the rep chose ({row['r']})", row["r"] == 7.0)
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("reopening reports the same program",
          detail.get("selected_customer_type") == MASS)
    again = programs(eid)
    check("  ...and the program is still among those offered",
          MASS in [p["customer_type"] for p in again])
    # Switching to the other Open program must take the OTHER rate, not keep
    # the one that belonged to the program the rep moved away from.
    check("switching to true Residential is accepted",
          select(eid, RES, 10.0).status_code == 200)
    row = query_one("SELECT selected_customer_type AS t, "
                    "selected_savings_percent AS r FROM enrollments WHERE id=?",
                    (eid,))
    check(f"  ...and both the program and its rate move together "
          f"({row['t']}, {row['r']})",
          row["t"] == RES and row["r"] == 10.0)
    # A rate that belongs to the OTHER Open program must not be accepted for
    # this one - that is how a customer gets quoted a number they cannot have.
    check("a rate from the other Open program is refused",
          select(eid, RES, 7.0).status_code >= 400)

    # ════════════════════════════════════════════════════════════════════════
    section("6. THE ENROLL BOUNDARY: WIRE VALUE AND RATE")
    # ════════════════════════════════════════════════════════════════════════
    import io
    from PIL import Image
    from services.perch.mock_client import PerchMockClient

    # THE MOCK'S OWN v2 ENROLL CONTRACT, opt-in and off by default. It models
    # exactly the fields E3/E5 are about - customer_type, county,
    # savings_percent - and returns the spec's 422 bodies for each documented
    # failure. That makes it the right check here: a program key leaking onto
    # the wire is REFUSED rather than quietly accepted by a stub that agrees
    # with whatever it is handed.
    PerchMockClient.enforce_enroll_contract = True

    def enroll_and_capture(zip_code, key, rate):
        eid, email = start(zip_code)
        assert select(eid, key, rate).status_code == 200, (key, rate)
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Sam", "last_name": "Sample",
                         "email": email, "phone": "5185550142"},
            "service_address": {"street": "9 Example Rd", "city": "Anytown",
                                "state": "NY", "zip": zip_code},
            "billing_address": {"same_as_service": True},
            "utility_account": dict(
                {"utility_name": UTIL[zip_code],
                 "account_number": ACCOUNT[UTIL[zip_code]]},
                # NYSEG and RG&E require a POD ID. Synthetic, and shaped as the
                # utility's own rule demands (N01 + 12 digits), so the enroll
                # path's real validation runs rather than being worked around.
                **({"secondary_account_identifier": "N01000000000001"}
                   if UTIL[zip_code] == "nyseg" else {}))})
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (244, 246, 250)).save(buf, "PNG")
        buf.seek(0)
        doc = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                     data={"category": "utility_bill", "files": (buf, "b.png")},
                     content_type="multipart/form-data"
                     ).get_json()["files"][0]["document_id"]
        # SPIED AT THE PERCH BOUNDARY ITSELF: the multipart form that is sent.
        # Wrapping the real builder rather than replacing the call means the
        # payload is the one Perch would actually receive, validation and all -
        # a stub returning a canned form could agree with itself while the
        # real one sent something else.
        seen = {}
        real = adapter.build_enrollment_multipart

        def spy(payload, *a, **kw):
            form, files_out = real(payload, *a, **kw)
            seen["customer_type"] = form.get("customer_type")
            # Multipart values are strings on the wire; compared as numbers.
            raw_rate = form.get("savings_percent")
            seen["savings_percent"] = (None if raw_rate in (None, "")
                                       else float(raw_rate))
            return form, files_out

        adapter.build_enrollment_multipart = spy
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                       json={"document_id": doc})
        finally:
            adapter.build_enrollment_multipart = real
        return r, seen, eid

    r, seen, eid = enroll_and_capture(Z_NYSEG, MASS, 5.0)
    check(f"a mass-market enrollment succeeds ({r.status_code} "
          f"{r.get_json() if r.status_code != 200 else ''})",
          r.status_code == 200)
    check(f"  ...and Perch is sent customer_type=Residential "
          f"({seen.get('customer_type')})",
          seen.get("customer_type") == RES)
    check("  ...NEVER the program key", seen.get("customer_type") != MASS)
    check(f"  ...with the mass-market rate ({seen.get('savings_percent')})",
          seen.get("savings_percent") == 5.0)
    check("  ...while the stored program keeps its exact identity",
          query_one("SELECT selected_customer_type AS t FROM enrollments "
                    "WHERE id=?", (eid,))["t"] == MASS)

    r, seen, _ = enroll_and_capture(Z_NG_DIFF, RES, 10.0)
    check(f"a true-residential enrollment still sends Residential "
          f"({seen.get('customer_type')})",
          r.status_code == 200 and seen.get("customer_type") == RES)
    check(f"  ...with ITS rate, not the mass-market one "
          f"({seen.get('savings_percent')})",
          seen.get("savings_percent") == 10.0)

    r, seen, _ = enroll_and_capture(Z_NG_DIFF, MASS, 7.0)
    check(f"and the mass-market program at the same location sends its own "
          f"rate ({seen.get('savings_percent')})",
          r.status_code == 200 and seen.get("savings_percent") == 7.0)
    check("  ...on the same wire customer_type",
          seen.get("customer_type") == RES)

    r, seen, _ = enroll_and_capture(Z_NG_DIFF, LMI, 20.0)
    check(f"LMI still sends LMI ({seen.get('customer_type')})",
          r.status_code == 200 and seen.get("customer_type") == LMI)
    r, seen, _ = enroll_and_capture(Z_NG_DIFF, LMI_IRA, 25.0)
    check(f"LMI_IRA still sends LMI_IRA ({seen.get('customer_type')})",
          r.status_code == 200 and seen.get("customer_type") == LMI_IRA)

    PerchMockClient.enforce_enroll_contract = False

    # ════════════════════════════════════════════════════════════════════════
    section("7. NOTHING A REP SEES CARRIES PERCH VOCABULARY")
    # ════════════════════════════════════════════════════════════════════════
    eid, _ = start(Z_NG_DIFF)
    import json as _json
    blob = _json.dumps(programs(eid))
    for raw in ("mass_market", "MassMarket", "lmi_ira", "LMI_IRA",
                "commercial_and_industrial"):
        # The program KEY and the capacity type travel in named fields, which
        # downstream logic needs; what must never appear is either of them
        # inside a LABEL or a DESCRIPTION a rep reads.
        offered = programs(eid)
        texts = [p["label"] for p in offered] + [p["description"] for p in offered] \
            + [t["label"] for p in offered for t in p["capacity_types"]]
        check(f"no rep-facing text contains {raw!r}",
              not any(raw in t for t in texts))
    check("the Open programs read as English",
          {p["label"] for p in programs(eid)} >= {"Residential",
                                                  "Residential (Mass Market)"})
    check("  ...and the raw key is still carried for downstream logic",
          MASS in [p["customer_type"] for p in programs(eid)])

    # ════════════════════════════════════════════════════════════════════════
    section("8. THE AVAILABILITY PANEL AGREES WITH THE CARDS")
    # ════════════════════════════════════════════════════════════════════════
    # The panel and the cards derive from one place now. A row the cards do not
    # offer must not appear beside them as "Available".
    for zip_code, name in ((Z_NG_SAME, "National Grid, merged"),
                           (Z_NG_DIFF, "National Grid, distinct"),
                           (Z_NYSEG, "NYSEG, mass market only")):
        eid, _ = start(zip_code)
        wf = c.get(f"/api/perch/enrollments/{eid}/workflow",
                   headers=rep).get_json()
        panel = next(p for p in wf["step"]["panels"]
                     if p["type"] == "capacity_summary")
        rows = {s["label"] for s in panel["segments"] if s["available"]}
        cards = {p["label"] for p in programs(eid)}
        check(f"{name}: every available row is a card the rep can pick "
              f"({sorted(rows)})", rows == cards)
        metrics = {m["label"] for m in panel["metrics"]}
        check("  ...and a savings metric per card, no more",
              metrics == {f"{lbl} savings" for lbl in cards})

    # ════════════════════════════════════════════════════════════════════════
    section("9. V1 IS UNCHANGED - MASS MARKET IS A V2 PROGRAM")
    # ════════════════════════════════════════════════════════════════════════
    # v1 publishes ONE combined rate for residential and commercial customers,
    # so the two cannot be priced apart and there is nothing to choose between.
    # Residential keeps covering both, exactly as it always has.
    v1 = mock_client._FIXTURES
    for prefix, expected in (("109", [RES, LMI]), ("133", [RES, LMI]),
                             ("120", [RES]), ("241", [LMI])):
        got = [p["customer_type"] for p in adapter.available_programs(
            v1[prefix]["project_details"],
            market_state=("VA" if prefix == "241" else "NY"))]
        check(f"v1 {prefix}: {got}", got == expected)
        check(f"  ...and no mass-market program on v1", MASS not in got)
    v1_or = adapter.available_programs(v1["109"]["project_details"])
    check("v1 still quotes the combined res/commercial field",
          v1_or[0]["savings_percents"] == [5.0]
          and v1_or[0]["savings_source"] == "v1_residential_and_commercial_field")
    check("  ...unflagged, because that field is not a substitution",
          v1_or[0]["savings_unresolved"] is False)
    v1_model = cap.parse(v1["133"]["project_details"])
    check("the v1 shape is what gates it",
          v1_model.shape == cap.SHAPE_V1_FLAT
          and adapter.PROGRAM_RULES[MASS].get("v2_only") is True)

    # ════════════════════════════════════════════════════════════════════════
    section("10. VIRGINIA GAINS NO OPEN PROGRAM")
    # ════════════════════════════════════════════════════════════════════════
    # VA staging returns mass-market capacity WITH a rate, so this is the one
    # place the new program could have leaked a non-LMI option into a
    # state-LMI market. The market product rule is now the only thing
    # withholding it, so it is asserted directly.
    va = mock_client._V2_FIXTURES["241"]["project_details"]
    got = [p["customer_type"] for p in
           adapter.available_programs(va, market_state="VA")]
    check(f"VA offers income-qualified only ({got})", got == [LMI])
    check("  ...no Open program of either kind",
          MASS not in got and RES not in got)
    va_model = adapter.capacity_model_for(
        {"zip_code": "22401", "capacity_available": True,
         "capacity_entries": cap.parse(va).as_dict()})
    for key in (MASS, RES):
        try:
            adapter.resolve_customer_type(va_model, requested=key)
            check(f"  ...and a tampered client cannot select {key}", False)
        except Exception:
            check(f"  ...and a tampered client cannot select {key}", True)
    check("  ...while the capacity model still records mass market as real",
          cap.parse(va).has_capacity(cap.MASS_MARKET) is True)

    print("\n" + "=" * 72)
    print(f"E5 MASS MARKET - {len(_results)}/{len(_results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
