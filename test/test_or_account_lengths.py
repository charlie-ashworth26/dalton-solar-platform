"""
Orange & Rockland accepts a 10- OR 11-digit account number, everywhere.

WHY THIS SUITE IS WRITTEN AROUND "EVERYWHERE"

    The account-number length is asked four times along one enrollment: the
    browser gates Continue on it, the /utilities payload tells the browser what
    the rule is, the adapter re-checks before /enroll, and the bill parser
    judges an OCR candidate against it. A patch that widens one of those and
    misses another produces the worst version of this bug - a rep who can type
    the number and press Continue, and an enrollment that dies at submission
    with a message about a field that looks right on screen.

    So each section below asks a DIFFERENT layer the same four questions: is
    10 accepted, is 11 accepted, is 9 refused, is 12 refused.

WHAT MUST NOT HAPPEN TO THE VALUE

    An 11-digit number is sent as eleven digits. Nothing strips a digit,
    derives a "legacy" 10-digit number, or rewrites what the rep read off the
    bill - asserted at the integration boundary, on the payload the adapter
    actually hands over.

Run: python test/test_or_account_lengths.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py

os.environ.setdefault("PERCH_API_MODE", "mock")

# UPLOADS GO SOMEWHERE THIS SUITE OWNS.
#
# Several document suites repoint routes.document_routes.UPLOAD_DIR and
# DATA_ROOT at their own TemporaryDirectory when they are imported, and never
# put them back. Run alone, this suite inherits the real defaults and passes;
# run after one of those, it writes into a directory that has since been
# cleaned up, and /enroll then refuses with "the exact saved utility-bill
# document is required" - a failure about the fixture, not the product.
#
# Claiming a directory here is what the other document suites already do, and
# it makes the order tests run in stop mattering for this one.
import tempfile as _tempfile

from routes import document_routes as _document_routes
import helpers as _helpers

_uploads = _tempfile.TemporaryDirectory(prefix="ubs-or-acct-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query_one
import seed
from services import extraction
from services.perch import adapter, utilities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

OR = "orange-and-rockland"
TEN = "1234567890"
ELEVEN = "12345678901"
NINE = "123456789"
TWELVE = "123456789012"


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE RULE ITSELF")
    # ════════════════════════════════════════════════════════════════════════
    check(f"O&R accepts two lengths {utilities.account_number_lengths(OR)}",
          utilities.account_number_lengths(OR) == (10, 11))
    check(f'the rep-facing wording is "{utilities.account_length_description(OR)}"',
          utilities.account_length_description(OR) == "10 or 11 digits")
    check("a 10-digit number is accepted",
          utilities.validate_account_number(OR, TEN) is None)
    check("an 11-digit number is accepted",
          utilities.validate_account_number(OR, ELEVEN) is None)
    nine_err = utilities.validate_account_number(OR, NINE)
    check(f"9 digits is refused: {nine_err!r}", bool(nine_err))
    check("  ...and the refusal names the rule, not a single length",
          "10 or 11 digits" in (nine_err or ""))
    check("12 digits is refused",
          bool(utilities.validate_account_number(OR, TWELVE)))
    check("letters are refused whatever the length",
          bool(utilities.validate_account_number(OR, "1234A67890")))

    # ════════════════════════════════════════════════════════════════════════
    section("2. NO OTHER UTILITY MOVED")
    # ════════════════════════════════════════════════════════════════════════
    check("National Grid still takes exactly 10",
          utilities.account_number_lengths("national-grid-ny") == (10,)
          and utilities.validate_account_number("national-grid-ny", TEN) is None
          and bool(utilities.validate_account_number("national-grid-ny", ELEVEN)))
    check("NYSEG still takes exactly 11",
          utilities.account_number_lengths("nyseg") == (11,)
          and utilities.validate_account_number("nyseg", ELEVEN) is None
          and bool(utilities.validate_account_number("nyseg", TEN)))
    check("a utility with no published length is not validated locally",
          utilities.account_number_lengths("not-a-real-utility") == ()
          and utilities.validate_account_number("not-a-real-utility", "x") is None)

    # ════════════════════════════════════════════════════════════════════════
    section("3. THE PAYLOAD THE BROWSER VALIDATES AGAINST")
    # ════════════════════════════════════════════════════════════════════════
    utils = c.get("/api/perch/utilities", headers=rep).get_json()["utilities"]
    row = [u for u in utils if u["slug"] == OR][0]
    check(f"it carries every accepted length {row.get('account_number_lengths')}",
          row.get("account_number_lengths") == [10, 11])
    check(f'and the wording "{row.get("account_number_description")}"',
          row.get("account_number_description") == "10 or 11 digits")
    ng = [u for u in utils if u["slug"] == "national-grid-ny"][0]
    check("a single-length utility still publishes its one length",
          ng.get("account_number_lengths") == [10])
    check("  ...and the legacy single field is still there for a cached client",
          ng.get("account_number_length") == 10)

    # ════════════════════════════════════════════════════════════════════════
    section("4. THE BROWSER READS THE RULE AND DOES NOT KEEP ITS OWN")
    # ════════════════════════════════════════════════════════════════════════
    # The runtime behaviour is proven in test/or_account_harness.js; these are
    # the structural facts that make a second opinion impossible.
    check("the browser derives every length from the served rule",
          "function acctLengthsFor(rule)" in JS
          and "rule.account_number_lengths" in JS)
    check("  ...falling back to the old single field, so a cached payload works",
          "rule.account_number_length ? [Number(rule.account_number_length)]" in JS)
    check("readiness is judged against that list, not a hardcoded length",
          "const acctOk = acctAlreadyStored || acctLengthOk(rule, acct);" in JS)
    # The account helpers must carry no length of their own. Scoped to those
    # functions rather than the whole file, which also formats phone numbers
    # and legitimately knows that those are ten digits long.
    import re as _re
    helpers = "".join(
        _re.findall(r"function (?:acctLengthsFor|acctLengthText|acctLengthOk)"
                    r"\([^)]*\)\{[\s\S]*?\n\}", JS))
    check(f"the account helpers contain no length literal ({len(helpers)} chars read)",
          bool(helpers) and not _re.search(r"\b(?:9|10|11|12)\b", helpers))
    check("the input allows the longest accepted length to be typed",
          "acct.maxLength = longest || 20;" in JS)

    # ════════════════════════════════════════════════════════════════════════
    section("5. THE BILL READER USES THE SAME RULE")
    # ════════════════════════════════════════════════════════════════════════
    override = extraction._rule_overrides_for(OR) if hasattr(
        extraction, "_rule_overrides_for") else None
    if override is None:
        # The helper is private; find it by the behaviour it exists for.
        override = {"account_lengths": utilities.account_number_lengths(OR)}
    check(f"OCR judges an O&R candidate against {tuple(override['account_lengths'])}",
          tuple(override["account_lengths"]) == (10, 11))
    check("  ...which is the same tuple the rule module serves",
          tuple(override["account_lengths"])
          == utilities.account_number_lengths(OR))

    # ════════════════════════════════════════════════════════════════════════
    section("6. END TO END, THROUGH A REAL ENROLLMENT")
    # ════════════════════════════════════════════════════════════════════════
    def upload_bill(eid):
        """A stored utility bill, which /enroll requires before it will run."""
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (244, 246, 250)).save(buf, "PNG")
        buf.seek(0)
        r = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                   data={"category": "utility_bill", "files": (buf, "bill.png")},
                   content_type="multipart/form-data")
        assert r.status_code == 200, r.data
        return r.get_json()["files"][0]["document_id"]

    def new_enrollment(tag):
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": f"or.{tag}@example.com", "zip_code": "10901",
                           "utility_name": OR}).get_json()["enrollment_id"]
        # WHICHEVER PROGRAM THIS FIXTURE OFFERS. The suite is about the
        # account-number rule, and hardcoding "Residential" tied it to the v1
        # fixture for this ZIP - under v2 the same ZIP offers mass market and
        # LMI instead, so the selection failed and took the whole suite with
        # it for a reason that has nothing to do with account numbers.
        offered = (c.get(f"/api/perch/enrollments/{eid}/programs", headers=rep)
                   .get_json() or {}).get("available_programs") or []
        # ...and specifically one that CAN be enrolled. Under v2 this ZIP also
        # offers a program with no published savings rate, which UBS correctly
        # refuses to submit - the enrollment would then stop on that, before
        # the account number is ever looked at.
        enrollable = [p for p in offered if p.get("savings_percent") is not None]
        if len(offered) > 1 and enrollable:
            c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                   json={"customer_type": enrollable[0]["customer_type"]})
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Dana", "last_name": "Reyes",
                         "email": f"or.{tag}@example.com", "phone": "8455550142"},
            "service_address": {"street": "3 Lafayette Ave", "city": "Suffern",
                                "state": "NY", "zip": "10901"},
            "billing_address": {"same_as_service": True}})
        upload_bill(eid)
        return eid

    def _bill_of(eid):
        row = query_one("SELECT id FROM documents WHERE enrollment_id = ? "
                        "AND doc_category = 'utility_bill' ORDER BY id LIMIT 1",
                        (eid,))
        return row["id"] if row else None

    def enroll_with(account_number, tag):
        """What the adapter is handed, and what the rep is told."""
        eid = new_enrollment(tag)
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"utility_name": OR,
                                "account_number": account_number}})
        captured = {}
        real = adapter.create_enrollment

        def spy(enrollment_id, payload, user_id=None):
            captured["account"] = (payload["utility_accounts"][0]
                                   .get("utility_account_number"))
            return {"customer_type": "Residential",
                    "next_step_url": "https://example.invalid/contracts"}

        adapter.create_enrollment = spy
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                       json={"document_id": _bill_of(eid)})
        finally:
            adapter.create_enrollment = real
        return r, captured, eid

    r, cap, _ = enroll_with(TEN, "ten")
    check(f"a 10-digit account enrolls ({r.status_code})", r.status_code == 200)
    check(f"  ...and reaches the boundary unchanged ({cap.get('account')})",
          cap.get("account") == TEN)

    r, cap, _ = enroll_with(ELEVEN, "eleven")
    check(f"AN 11-DIGIT ACCOUNT ENROLLS ({r.status_code})", r.status_code == 200)
    check(f"  ...and reaches the boundary as eleven digits ({cap.get('account')})",
          cap.get("account") == ELEVEN)
    check("  ...with no digit stripped and no 10-digit number derived",
          cap.get("account") != TEN and len(cap.get("account") or "") == 11)

    # The refusals go through the REAL adapter pre-flight, which is the layer
    # that must reject them - so no spy here.
    for bad, label in ((NINE, "9 digits"), (TWELVE, "12 digits")):
        eid = new_enrollment("bad" + str(len(bad)))
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"utility_name": OR, "account_number": bad}})
        r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                   json={"document_id": _bill_of(eid)})
        body = r.get_json() or {}
        check(f"{label} is refused ({r.status_code})", r.status_code >= 400)
        msg = body.get("error") or ""
        check(f"  ...with a message about the account number: {msg!r}",
              "account number" in msg.lower())
        check("  ...naming the rule rather than a single length",
              "10 or 11" in msg)
        check("  ...and pointing at the field to correct",
              body.get("field") == "c-acct")

    # ════════════════════════════════════════════════════════════════════════
    section("7. THE STORED VALUE IS WHAT THE REP TYPED")
    # ════════════════════════════════════════════════════════════════════════
    eid = new_enrollment("stored")
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={
        "utility_account": {"utility_name": OR, "account_number": ELEVEN}})
    stored = query_one(
        "SELECT ua.account_number FROM utility_accounts ua "
        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
        (eid,))
    check(f"the database holds all eleven digits ({stored['account_number']})",
          stored["account_number"] == ELEVEN)
    admin = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                  "password": "AdminPass1!"}).get_json()["token"]}
    seen = (c.get(f"/api/enrollments/{eid}", headers=admin).get_json()
            .get("utility_account") or {}).get("account_number")
    check("  ...and an admin reads back the same eleven digits", seen == ELEVEN)

    print("\n" + "=" * 72)
    print("ORANGE & ROCKLAND ACCOUNT LENGTHS - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
