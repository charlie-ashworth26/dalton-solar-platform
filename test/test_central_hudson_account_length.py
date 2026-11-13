"""
Central Hudson account numbers are eleven digits, and all eleven reach Perch.

THE BUG

    Migration 003 recorded central-hudson-gas-electric as 10 digits, citing
    Perch's OpenAPI specification. Perch's partner training guide says 11. A
    rep entering a real 11-digit Central Hudson account number was refused -
    "account numbers must be 10 digits" - and the customer could not be
    enrolled at all.

    One wrong value in one column. Everything downstream was already correct
    and already read that value: the browser's maxlength and length check, the
    rep-facing help text, the bill reader's accepted lengths, and the
    pre-flight check on the enrollment payload all derive from
    account_number_lengths(). Migration 016 corrects the value; nothing else
    had to change, and this file is largely a proof of that.

ELEVEN ONLY, DELIBERATELY

    Not "ten or eleven". Orange & Rockland genuinely issues both and is
    modelled that way; Central Hudson was simply recorded wrong. The
    distinction earns its keep: a bill reader that drops a final digit turns an
    11-digit number into a plausible 10-digit one, and for O&R that truncated
    value passes validation BECAUSE both lengths are legal (see
    test_account_number_integrity.py). A single length catches the truncation
    instead of enrolling against an account belonging to nobody.

Every value below is synthetic. Run: python test/test_central_hudson_account_length.py
"""
import contextlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database

os.environ.setdefault("PERCH_API_MODE", "mock")

import tempfile as _tempfile

from routes import document_routes as _document_routes
import helpers as _helpers

# Its own storage: other document suites repoint these and do not restore them.
_uploads = _tempfile.TemporaryDirectory(prefix="ubs-ch-acct-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
_helpers.BACKEND_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query_one
import seed
from services import extraction
from services.perch import adapter, utilities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CH = "central-hudson-gas-electric"
CH_ZIP = "12401"
# Synthetic 11-digit Central Hudson numbers. The second begins with zeros, so
# any numeric cast anywhere in the chain would show up as a shortened value.
ELEVEN = "90000000011"
ELEVEN_LEADING_ZEROS = "00000000011"
TEN = "9000000001"          # what the old rule demanded, and what a bill
                            # reader produces when it drops the last digit
# Central Hudson also requires a POD ID: 10 digits, no prefix. Synthetic, and
# unrelated to the account-number rule this suite is about - it is here only so
# the enrollment can legitimately complete.
CH_POD = "9000000002"

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def main():
    """Scoped so nothing this suite needs leaks into the rest of the run."""
    from services.perch.mock_client import PerchMockClient
    enforce = PerchMockClient.enforce_enroll_contract
    try:
        _run()
    finally:
        PerchMockClient.enforce_enroll_contract = enforce


def _run():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    counter = {"n": 0}

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE PUBLISHED RULE")
    # ════════════════════════════════════════════════════════════════════════
    check(f"Central Hudson accepts exactly 11 digits "
          f"({utilities.account_number_lengths(CH)})",
          utilities.account_number_lengths(CH) == (11,))
    check(f"  ...and the rep is told so "
          f"({utilities.account_length_description(CH)!r})",
          utilities.account_length_description(CH) == "11 digits")
    check("an 11-digit number is accepted",
          utilities.validate_account_number(CH, ELEVEN) is None)
    check("  ...including one with leading zeros",
          utilities.validate_account_number(CH, ELEVEN_LEADING_ZEROS) is None)
    for n in (9, 10, 12, 13):
        err = utilities.validate_account_number(CH, "9" * n)
        check(f"  ...and {n} digits is refused", err is not None)
    check("the refusal names the right length, not the old one",
          "11 digits" in utilities.validate_account_number(CH, TEN))

    # Central Hudson is NOT modelled as a multi-length utility: that is what
    # lets a truncated 11-digit number be caught rather than enrolled.
    check("Central Hudson is not an accepted-lengths exception",
          CH not in utilities.ACCOUNT_LENGTH_EXCEPTIONS)
    check("  ...and O&R still is, with both of its lengths",
          utilities.ACCOUNT_LENGTH_EXCEPTIONS.get("orange-and-rockland")
          == (10, 11))

    # ════════════════════════════════════════════════════════════════════════
    section("2. NO OTHER UTILITY MOVED")
    # ════════════════════════════════════════════════════════════════════════
    for slug, expected in (("national-grid-ny", (10,)),
                           ("consolidated-edison-ny", (11,)),
                           ("nyseg", (11,)),
                           ("rochester-gas-electric", (11,)),
                           ("orange-and-rockland", (10, 11)),
                           ("pse-g-ny", (11,))):
        check(f"{slug} is still {expected}",
              utilities.account_number_lengths(slug) == expected)
    # National Grid is the one that shares Central Hudson's old value, so it is
    # the one a careless UPDATE would have taken with it.
    check("National Grid still refuses 11 digits",
          utilities.validate_account_number("national-grid-ny", ELEVEN) is not None)
    check("  ...and still accepts its own 10",
          utilities.validate_account_number("national-grid-ny", TEN) is None)

    # ════════════════════════════════════════════════════════════════════════
    section("3. THE BROWSER DERIVES THE RULE, IT DOES NOT CARRY ONE")
    # ════════════════════════════════════════════════════════════════════════
    # The whole reason this was a one-value fix. Asserted at the source so a
    # hardcoded cap cannot reappear.
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    acct_tag = re.search(r"<input[^>]*\bid=\"c-acct\"[^>]*>", html)
    check("the account input carries no markup maxlength",
          acct_tag is not None and "maxlength" not in acct_tag.group(0).lower())
    check("  ...and is a text field, so leading zeros are not eaten",
          'type="text"' in acct_tag.group(0))
    check("the only cap is the longest length the utility publishes",
          "acct.maxLength = longest || 20;" in js)
    check("  ...with no utility-specific number hardcoded beside it",
          not re.search(r"maxLength\s*=\s*1[01]\b", js))
    check("the length check reads the published rule",
          "function acctLengthOk(rule, value)" in js
          and "lengths.indexOf(raw.length)" in js)
    check("  ...and no Central Hudson special case exists in the browser",
          "central-hudson" not in js)

    # What the browser is actually handed for Central Hudson.
    published = c.get("/api/perch/utilities", headers=rep).get_json()
    row = next((u for u in (published.get("utilities") or published)
                if u.get("slug") == CH), None)
    check("the API publishes Central Hudson to the browser", row is not None)
    check(f"  ...as 11 ({row.get('account_number_length')})",
          row.get("account_number_length") == 11)
    check(f"  ...and as a list the browser prefers "
          f"({row.get('account_number_lengths')})",
          row.get("account_number_lengths") == [11])
    check(f"  ...with rep-facing wording ({row.get('account_number_description')!r})",
          row.get("account_number_description") == "11 digits")

    # ════════════════════════════════════════════════════════════════════════
    section("4. THE BILL READER KEEPS ALL ELEVEN")
    # ════════════════════════════════════════════════════════════════════════
    def read_bill(number):
        text = (f"ACCOUNT NUMBER {number} SERVICE FOR PAT SAMPLE "
                f"1 EXAMPLE ST KINGSTON NY {CH_ZIP} AMOUNT DUE $50.00")
        parsed = extraction.parse_utility_bill(text, utility_hint=CH)
        return ((parsed.get("account_number") or {}).get("value"),
                (parsed.get("_rejected_fields") or {}).get("value") or [])

    got, rejected = read_bill(ELEVEN)
    check(f"an 11-digit Central Hudson number is read whole ({got})",
          got == ELEVEN)
    check("  ...and nothing is flagged for the rep", not rejected)
    got, _ = read_bill(ELEVEN_LEADING_ZEROS)
    check(f"leading zeros survive the read ({got})",
          got == ELEVEN_LEADING_ZEROS)
    # The truncation case this single length exists to catch.
    got, rejected = read_bill(TEN)
    check("a 10-digit read is NOT offered for Central Hudson", got is None)
    check(f"  ...it is flagged for the rep instead ({rejected})",
          "account_number" in rejected)

    # ════════════════════════════════════════════════════════════════════════
    section("5. THE FULL ENROLLMENT FLOW, END TO END")
    # ════════════════════════════════════════════════════════════════════════
    # The mock's own v2 enroll contract, opt-in and off by default: it checks
    # the fields the payload must carry, so a value that would be refused at
    # the real boundary is refused here rather than accepted by a stub that
    # agrees with whatever it is handed. main() restores the flag.
    from services.perch.mock_client import PerchMockClient
    PerchMockClient.enforce_enroll_contract = True

    def enroll(account_number):
        """Capacity -> program -> customer -> bill -> enroll, as a rep does it.

        Returns (final response, what reached the Perch payload, enrollment id).
        """
        counter["n"] += 1
        email = f"ch.{counter['n']}@example.com"
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": email, "zip_code": CH_ZIP,
                           "utility_name": CH}).get_json()["enrollment_id"]
        offered = (c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep)
                   .get_json() or {}).get("available_programs") or []
        # Whichever program this location offers, so the suite does not depend
        # on which API version is in force.
        sellable = [p for p in offered if p.get("savings_percents")]
        if sellable:
            c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                   json={"customer_type": sellable[0]["customer_type"]})
        patched = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Pat", "last_name": "Sample",
                         "email": email, "phone": "8455550142"},
            "service_address": {"street": "1 Example St", "city": "Kingston",
                                "state": "NY", "zip": CH_ZIP},
            "billing_address": {"same_as_service": True},
            "utility_account": {"utility_name": CH,
                                "account_number": account_number,
                                # Central Hudson requires a POD ID (10 digits,
                                # no prefix). Synthetic, and shaped to its own
                                # rule, so the enroll path's real validation
                                # runs instead of being worked around.
                                "secondary_account_identifier": CH_POD}})
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (243, 246, 250)).save(buf, "PNG")
        buf.seek(0)
        doc = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                     data={"category": "utility_bill", "files": (buf, "bill.png")},
                     content_type="multipart/form-data"
                     ).get_json()["files"][0]["document_id"]

        # Spied at the Perch boundary: the multipart form that is actually
        # sent, built by the real builder rather than a stub that could agree
        # with itself while the real one sent something shorter.
        seen = {}
        real_builder = adapter.build_enrollment_multipart

        def spy(payload, *a, **kw):
            form, files_out = real_builder(payload, *a, **kw)
            seen["account_number"] = (
                (payload.get("utility_accounts") or [{}])[0]
                .get("utility_account_number"))
            seen["form_keys_present"] = "customer_type" in form
            return form, files_out

        adapter.build_enrollment_multipart = spy
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                       json={"document_id": doc})
        finally:
            adapter.build_enrollment_multipart = real_builder
        return r, seen, eid, patched

    r, seen, eid, patched = enroll(ELEVEN)
    check(f"the 11-digit enrollment is accepted at every step "
          f"({patched.status_code} then {r.status_code}"
          f"{'' if r.status_code == 200 else ' - ' + str(r.get_json())})",
          patched.status_code in (200, 204) and r.status_code == 200)
    stored = query_one(
        "SELECT ua.account_number AS a FROM utility_accounts ua "
        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
        (eid,))["a"]
    check(f"the database holds all eleven digits ({stored})", stored == ELEVEN)
    check(f"  ...and ALL ELEVEN REACH PERCH ({seen.get('account_number')})",
          seen.get("account_number") == ELEVEN)
    check("  ...not a shortened value", seen.get("account_number") != TEN)
    check(f"  ...of the right length ({len(seen.get('account_number') or '')})",
          len(seen.get("account_number") or "") == 11)
    check("  ...as a string, not a number",
          isinstance(seen.get("account_number"), str))

    r, seen, eid, _ = enroll(ELEVEN_LEADING_ZEROS)
    check(f"an account number beginning with zeros enrolls ({r.status_code})",
          r.status_code == 200)
    check(f"  ...and every zero reaches Perch ({seen.get('account_number')})",
          seen.get("account_number") == ELEVEN_LEADING_ZEROS)
    check("  ...with nothing stripped from the front",
          (seen.get("account_number") or "").startswith("0000"))
    stored = query_one(
        "SELECT ua.account_number AS a FROM utility_accounts ua "
        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
        (eid,))["a"]
    check(f"  ...and the database kept them too ({stored})",
          stored == ELEVEN_LEADING_ZEROS)

    # ════════════════════════════════════════════════════════════════════════
    section("6. A TEN-DIGIT NUMBER IS REFUSED, WITH SOMETHING USEFUL TO SAY")
    # ════════════════════════════════════════════════════════════════════════
    r, seen, _eid, _ = enroll(TEN)
    check(f"the enrollment is refused ({r.status_code})", r.status_code >= 400)
    body = r.get_json() or {}
    message = body.get("error") or ""
    check(f"  ...telling the rep the length it needs ({message!r})",
          "11 digits" in message)
    check("  ...and pointing at the account-number field",
          body.get("field") in ("utility_account_number", "c-acct", "account_number"))
    check("  ...nothing was sent to Perch", not seen.get("account_number"))
    check("  ...and the message carries no partner internals",
          "perch" not in message.lower())

    PerchMockClient.enforce_enroll_contract = False

    print("\n" + "=" * 72)
    print(f"CENTRAL HUDSON ACCOUNT LENGTH - {len(_results)}/{len(_results)} PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
