"""
An account number read off a bill is never shortened, padded or invented.

THE DEFECT THIS PINS  (traced 2026-11-03)

    Both bill readers asked for EXACTLY ten digits, unanchored:

        ACCOUNT NUMBER\\s*(\\d{5}-\\d{5}|\\d{10})

    On an eleven-digit account that matched the first ten and dropped the
    eleventh. Silently - and what came back still looked like an account
    number, so nothing downstream questioned it.

    Orange & Rockland made it invisible. O&R issues BOTH ten- and
    eleven-digit numbers, so the rule accepts ten, so the truncated value
    PASSED validation and was autofilled as if it were correct. The rep saw a
    plausible number that belonged to nobody.

    And it compounded: the browser's assignments were unconditional, so a rep
    who added the missing digit and then uploaded a second page had their
    correction overwritten by the short value again.

WHAT IS ASSERTED

    Every digit survives, at every layer and for every utility that publishes
    a length. A run that is not a length the utility issues is LEFT FOR THE
    REP - never trimmed to fit, never padded, never guessed. And a value
    already on the form is never replaced by a later read.

Run: python test/test_account_number_integrity.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py

os.environ.setdefault("PERCH_API_MODE", "mock")

import tempfile as _tempfile

from routes import document_routes as _document_routes
import helpers as _helpers

_uploads = _tempfile.TemporaryDirectory(prefix="ubs-acct-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query_one
import seed
from services import bill_parsers, extraction
from services.perch import adapter, utilities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

OR = "orange-and-rockland"
OR_TEN = "1234567890"
OR_ELEVEN = "12345678901"


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def bill_text(number, name="JANE SAMPLE", city="SUFFERN", zip_code="10901"):
    """A bill in the layout whose label the legacy reader matches."""
    return (f"ACCOUNT NUMBER {number} SERVICE FOR {name} "
            f"1 EXAMPLE ST {city} NY {zip_code} Amount Due $50.00")


def read(number, hint, **kw):
    parsed = extraction.parse_utility_bill(bill_text(number, **kw),
                                           utility_hint=hint)
    return ((parsed.get("account_number") or {}).get("value"),
            (parsed.get("_rejected_fields") or {}).get("value") or [])


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE FULL NUMBER SURVIVES THE SERVER READER")
    # ════════════════════════════════════════════════════════════════════════
    got, _ = read(OR_ELEVEN, OR)
    check(f"an 11-digit O&R account keeps all 11 digits ({got})",
          got == OR_ELEVEN)
    check("  ...and is not the first ten of it", got != OR_TEN)
    got, _ = read(OR_TEN, OR)
    check(f"a 10-digit O&R account is unchanged ({got})", got == OR_TEN)

    # The exact shape National Grid prints, which must behave as it always has.
    got, _ = read("12345-67890", "national-grid-ny", city="ALBANY",
                  zip_code="12207")
    check(f"National Grid's dashed 10-digit form still reads ({got})",
          got == OR_TEN)
    got, _ = read(OR_TEN, "national-grid-ny", city="ALBANY", zip_code="12207")
    check("  ...as does its plain 10-digit form", got == OR_TEN)

    # ════════════════════════════════════════════════════════════════════════
    section("2. A LENGTH THE UTILITY DOES NOT ISSUE IS LEFT FOR THE REP")
    # ════════════════════════════════════════════════════════════════════════
    # The alternative - trimming to fit - is what caused this bug.
    got, rejected = read(OR_ELEVEN, "national-grid-ny", city="ALBANY",
                         zip_code="12207")
    check("National Grid handed 11 digits offers nothing", got is None)
    check("  ...rather than the first ten of them", got != OR_TEN)
    check(f"  ...and says the field still needs entering ({rejected})",
          "account_number" in rejected)
    got, _ = read("123456789012", OR)
    check("a 12-digit run is not offered for O&R either", got is None)
    got, _ = read("123456789", OR)
    check("  ...nor a 9-digit one", got is None)

    # With no utility known there is no rule to judge against, so the value is
    # passed through WHOLE rather than measured against a guess.
    got, _ = read(OR_ELEVEN, None)
    check(f"with no utility known the whole run is kept ({got})",
          got == OR_ELEVEN)

    # ════════════════════════════════════════════════════════════════════════
    section("3. EVERY UTILITY WITH A PUBLISHED LENGTH")
    # ════════════════════════════════════════════════════════════════════════
    # Widening O&R must not have widened anything else. These are the lengths
    # the centralized rule publishes - the copy the input field, the API
    # boundary and the parser all read.
    expectations = [
        ("national-grid-ny", (10,)),
        ("nyseg", (11,)),
        ("rochester-gas-electric", (11,)),
        (OR, (10, 11)),
    ]
    for slug, lengths in expectations:
        check(f"{slug} accepts exactly {lengths}",
              utilities.account_number_lengths(slug) == lengths)
        for n in range(8, 14):
            err = utilities.validate_account_number(slug, "9" * n)
            should_pass = n in lengths
            check(f"  {n:2} digits -> {'accepted' if should_pass else 'refused'}",
                  (err is None) == should_pass)
    check("O&R is the ONLY utility granted a second length",
          set(utilities.ACCOUNT_LENGTH_EXCEPTIONS) == {OR})

    # Where the parser carries its own default AND the table publishes one,
    # the two must agree - a second copy of a rule is how one side comes to
    # accept what another rejects.
    for slug in sorted(bill_parsers.DEFAULT_RULES):
        local = tuple((bill_parsers.DEFAULT_RULES.get(slug) or {})
                      .get("account_lengths") or ())
        published = utilities.account_number_lengths(slug)
        if local and published:
            check(f"{slug}: parser default {local} agrees with published {published}",
                  local == published)
        elif local:
            # Dominion sits here: it issues 10 OR 12 digits, which the table's
            # single-length column cannot express, so the parser default is the
            # only copy. Asserted so a later table entry cannot contradict it
            # unnoticed.
            check(f"{slug}: parser-only rule {local}, table publishes none",
                  published == ())

    # An empty published rule must mean "nothing to add", never "accept
    # anything" - otherwise centralizing the rule would have QUIETLY DELETED
    # every parser default for the utilities the table is silent on.
    for slug in sorted(bill_parsers.DEFAULT_RULES):
        local = tuple((bill_parsers.DEFAULT_RULES.get(slug) or {})
                      .get("account_lengths") or ())
        if not local:
            continue
        merged = tuple(bill_parsers.rules_for(
            slug, extraction._published_rules(slug)).get("account_lengths") or ())
        published = utilities.account_number_lengths(slug)
        check(f"{slug}: the merged rule stays {local or published} ({merged})",
              merged == (published or local))
        check("  ...and never widens to accept anything", bool(merged))

    # Proven at the parser, not only at the merge: Dominion still refuses an
    # off-length run, and offers no account number rather than a trimmed one.
    got, rejected = read("12345678901", "dominion-energy-va", city="RICHMOND",
                         zip_code="24101")
    check("Dominion handed 11 digits offers nothing", got is None)
    check(f"  ...and flags the field for the rep ({rejected})",
          "account_number" in rejected)

    # ════════════════════════════════════════════════════════════════════════
    section("4. THE BROWSER READER KEEPS EVERY DIGIT TOO")
    # ════════════════════════════════════════════════════════════════════════
    # Runtime behaviour is proven in test/account_number_harness.js; these are
    # the structural facts that stop the fixed-length pattern coming back.
    check("the browser no longer asks for exactly ten digits",
          "(\\d{5}-\\d{5}|\\d{10})" not in JS)
    check("  ...it captures the whole run",
          "/ACCOUNT\\s+NUMBER\\s*:?\\s*(\\d[\\d-]*\\d)\\b/i" in JS)
    check("the length is judged against the utility's rule before filling",
          "acctLengthOk(currentUtilityRule(), parsed.acct)" in JS)
    check("  ...and the field is only filled when it is empty",
          "fillIfEmpty('c-acct', parsed.acct)" in JS)
    # The account assignment, isolated: the ONLY thing done to what was read
    # is removing dashes. No slice, substring, padding or fixed length.
    acct_stmt = re.search(r"if\(m\)\s*result\.acct\s*=\s*([^\r\n]+)",
                          JS[JS.index("function parseUtilityBill"):])
    check(f"the only transformation is dash removal ({acct_stmt.group(1).strip()})",
          acct_stmt.group(1).strip() == "m[1].replace(/-/g,'');")
    for banned in (".slice(", ".substring(", ".substr(", ".padStart(", ".padEnd("):
        check(f"  ...no {banned.strip('.(')} on the account value",
              banned not in acct_stmt.group(1))

    # The field itself: no markup cap, and the JS cap is the longest length the
    # utility publishes - never a hardcoded ten.
    html = open(os.path.join(ROOT, "templates", "index.html"),
                encoding="utf-8").read()
    acct_tag = re.search(r"<input[^>]*\bid=\"c-acct\"[^>]*>", html)
    check(f"the account input exists and carries no maxlength ({acct_tag.group(0)[:60]}...)",
          acct_tag is not None and "maxlength" not in acct_tag.group(0).lower())
    check("the only cap comes from the centralized rule",
          "acct.maxLength = longest || 20;" in JS)
    check("  ...and no cap of ten is hardcoded anywhere near it",
          "maxLength = 10" not in JS and "maxLength=10" not in JS)

    # ════════════════════════════════════════════════════════════════════════
    section("5. END TO END: EXTRACTION -> API -> DATABASE -> PERCH")
    # ════════════════════════════════════════════════════════════════════════
    def enrollment_with(number, tag):
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": f"acct.{tag}@example.com",
                           "zip_code": "10901",
                           "utility_name": OR}).get_json()["enrollment_id"]
        offered = (c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep)
                   .get_json() or {}).get("available_programs") or []
        enrollable = [p for p in offered if p.get("savings_percent") is not None]
        if len(offered) > 1 and enrollable:
            c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                   json={"customer_type": enrollable[0]["customer_type"]})
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Jane", "last_name": "Sample",
                         "email": f"acct.{tag}@example.com",
                         "phone": "8455550142"},
            "service_address": {"street": "1 Example St", "city": "Suffern",
                                "state": "NY", "zip": "10901"},
            "billing_address": {"same_as_service": True},
            "utility_account": {"utility_name": OR, "account_number": number}})
        return eid

    for number, label in ((OR_ELEVEN, "11-digit"), (OR_TEN, "10-digit")):
        eid = enrollment_with(number, label)
        stored = query_one(
            "SELECT ua.account_number AS a FROM utility_accounts ua "
            "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
            (eid,))["a"]
        check(f"{label}: the database holds every digit ({stored})",
              stored == number)

        captured = {}
        real = adapter.create_enrollment

        def spy(enrollment_id, payload, user_id=None):
            captured["acct"] = (payload["utility_accounts"][0]
                                .get("utility_account_number"))
            return {"customer_type": "Residential",
                    "next_step_url": "https://example.invalid/contracts"}

        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (242, 245, 250)).save(buf, "PNG")
        buf.seek(0)
        doc = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                     data={"category": "utility_bill", "files": (buf, "bill.png")},
                     content_type="multipart/form-data"
                     ).get_json()["files"][0]["document_id"]
        adapter.create_enrollment = spy
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                       json={"document_id": doc})
        finally:
            adapter.create_enrollment = real
        check(f"{label}: it enrolls ({r.status_code})", r.status_code == 200)
        check(f"{label}: the partner is handed every digit ({captured.get('acct')})",
              captured.get("acct") == number)

    # ════════════════════════════════════════════════════════════════════════
    section("6. A RE-READ CANNOT SHORTEN WHAT IS ALREADY STORED")
    # ════════════════════════════════════════════════════════════════════════
    # The rep corrected the number; a later upload reads the short one. The
    # correction is what must survive - the server never writes extraction
    # output, so this asserts the property end to end rather than a guard.
    eid = enrollment_with(OR_ELEVEN, "correction")
    import io
    from PIL import Image
    for attempt in range(2):
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (240, 244, 250)).save(buf, "PNG")
        buf.seek(0)
        c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
               data={"category": "utility_bill", "files": (buf, f"b{attempt}.png")},
               content_type="multipart/form-data")
    stored = query_one(
        "SELECT ua.account_number AS a FROM utility_accounts ua "
        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
        (eid,))["a"]
    check(f"two more uploads leave the stored number alone ({stored})",
          stored == OR_ELEVEN)
    detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    shown = (detail.get("utility_account") or {}).get("account_number")
    check(f"  ...and a rep reopening sees it masked, not shortened ({shown})",
          shown and shown.endswith(OR_ELEVEN[-4:]))

    print("\n" + "=" * 72)
    print("ACCOUNT NUMBER INTEGRITY - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
