"""
A rejected enrollment should say what to fix - without saying who rejected it.

THE PROBLEM THIS ADDRESSES

    Every enrollment rejection collapsed to one sentence:

        "Some of the details were not accepted. Please check the information
         on this step and try again."

    A rep reading that on a screen where everything LOOKS right has nothing to
    act on. Two live cases made it worse than vague:

      * NYSEG Residential, where the program is offered but publishes no
        savings rate of its own. Nothing the rep could type would have helped
        - the program is not enrollable - and they were told to check their
        details.
      * Dominion with no capacity, which has its own clear message and must
        never fall through to a validation sentence about the rep's data.

THE LINE THIS SUITE DEFENDS

    A specific message ONLY when the cause is actually identifiable: from the
    exception type, from a sentence UBS itself wrote, or from Perch's
    structured per-field errors. Everything else keeps the honest fallback,
    because a confident guess about which field is wrong sends a rep to fix
    something that was never broken.

    And whatever the cause: no partner name, no status code, no endpoint, no
    machine code, no exception class, no partner prose. Asserted on every
    sentence this module can produce.

Run: python test/test_rep_error_mapping.py
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

_uploads = _tempfile.TemporaryDirectory(prefix="ubs-rep-errors-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query
import seed
from services import rep_errors
from services.perch.errors import (PerchError, PerchValidationError,
                                   PerchNoCapacityError)

GENERIC = rep_errors.ENROLLMENT_NOT_ACCEPTED


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def perch_422(field, code, message="Perch rejected the enrollment (422)."):
    """A validation error shaped exactly as the client builds one from a 422."""
    exc = PerchValidationError(f"Perch rejected the enrollment (422): {message}")
    exc.http_status = 422
    exc.status_code = 422
    exc.body_json = {"errors": [{"field": field, "code": code,
                                 "message": message}]}
    return exc


def main():
    init_db(reset=True)
    seed.seed()

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE CAUSES UBS CAN NAME")
    # ════════════════════════════════════════════════════════════════════════
    cases = [
        ("no capacity, by exception type",
         PerchNoCapacityError("No open solar project capacity for x in ZIP y."),
         rep_errors.NO_CAPACITY, None),
        ("account number format",
         perch_422("utility_account_number", "invalid"),
         rep_errors.ACCOUNT_NUMBER_FORMAT, "c-acct"),
        ("POD ID format",
         perch_422("secondary_account_identifier", "invalid"),
         rep_errors.POD_ID_FORMAT, "c-pod"),
        ("meter number format",
         perch_422("meter_number", "invalid"),
         rep_errors.METER_NUMBER_FORMAT, "c-meter"),
        # E3 has a sentence for each of these exact pairs, and both are more
        # specific than the category one - they say what to do next, which is
        # re-check availability. The category sentence is the fallback for a
        # program failure E3 has no wording for.
        ("a program that cannot be enrolled",
         perch_422("customer_type", "capacity_unavailable"),
         "The selected savings program no longer has capacity for this "
         "address. Re-check availability and choose from what is offered.",
         None),
        ("a program with no usable savings rate",
         perch_422("savings_percent", "required"),
         "Choose the savings rate for this customer before submitting.",
         None),
        ("a program failure E3 has no wording for",
         perch_422("customer_type", "unenrollable"),
         rep_errors.PROGRAM_UNAVAILABLE, None),
        ("an account Perch says is already enrolled",
         perch_422("utility_account_number", "already_exists"),
         rep_errors.ALREADY_ENROLLED, "c-acct"),
        # A duplicate EMAIL is not a duplicate utility ACCOUNT, and E3's
        # sentence for it says which one it is. Pinned here because the
        # category sentence briefly replaced it.
        ("a duplicate the partner names on the email",
         perch_422("email_address", "already_exists"),
         "An enrollment already exists for this email address.", "c-email"),
        ("missing customer details",
         perch_422("first_name", "required"),
         rep_errors.CUSTOMER_INFO_MISSING, "c-first"),
    ]
    for label, exc, expected, expected_field in cases:
        message, field = rep_errors.for_enrollment_failure(exc)
        check(f"{label}: {message!r}", message == expected)
        check(f"  ...and points at {expected_field or 'no single field'}",
              field == expected_field)

    # ════════════════════════════════════════════════════════════════════════
    section("2. A DUPLICATE IS CLAIMED ONLY WHEN PERCH SAYS SO")
    # ════════════════════════════════════════════════════════════════════════
    # "Already enrolled" sends a rep to look at a customer's existing account.
    # Saying it on a guess is worse than saying nothing.
    msg, _ = rep_errors.for_enrollment_failure(
        perch_422("utility_account_number", "invalid"))
    check("an invalid account is a FORMAT problem, not a duplicate",
          msg == rep_errors.ACCOUNT_NUMBER_FORMAT)
    bare = PerchValidationError("Perch rejected the enrollment (422).")
    bare.http_status = 422
    msg, field = rep_errors.for_enrollment_failure(bare)
    check(f"a bare 422 with no structure stays generic: {msg!r}", msg == GENERIC)
    check("  ...and names no field", field is None)
    plain400 = PerchValidationError("Bad request.")
    plain400.http_status = 400
    plain400.status_code = 400          # as the client marks a real response
    msg, _ = rep_errors.for_enrollment_failure(plain400)
    check("a generic 400 is never read as a duplicate either", msg == GENERIC)
    check("  ...and the partner's own words are not forwarded",
          msg != "Bad request.")

    # The other side of that rule: a sentence UBS raised itself still reaches
    # the rep, because it was written for them and names the real problem.
    ours = PerchValidationError(
        "NYSEG requires a POD ID (15 digits, starting with N01).")
    ours.rep_message = ours.args[0]
    ours.rep_field = "secondary_account_identifier"
    msg, field = rep_errors.for_enrollment_failure(ours)
    check(f"a check UBS wrote is kept whole: {msg!r}", msg == ours.args[0])
    check("  ...and still names the field to correct", field == "c-pod")

    # ════════════════════════════════════════════════════════════════════════
    section("3. THE TWO LIVE FAILURES")
    # ════════════════════════════════════════════════════════════════════════
    # NYSEG Residential: offered, but its own savings rates are empty. UBS
    # refuses to substitute the mass_market rates behind it - that part is
    # correct and unchanged - and the rep must be told the PROGRAM is the
    # problem, not their typing.
    from services.perch import adapter
    stop = None
    try:
        raise adapter._rep_validation_error(
            "This savings program is not currently available for enrollment - "
            "no savings rate is published for it. Please choose another "
            "available program.", "savings_percent")
    except PerchValidationError as e:
        stop = e
    msg, field = rep_errors.for_enrollment_failure(stop)
    check(f"NYSEG no-rate: {msg!r}", "not currently available" in msg)
    check("  ...it names the program, not a field the rep should retype",
          "savings rate" in msg and "account" not in msg.lower())
    check("  ...and carries no partner internals",
          not rep_errors.contains_internals(msg))

    # Dominion no capacity keeps its own message and never degrades into a
    # validation sentence about the rep's data.
    msg, field = rep_errors.for_enrollment_failure(
        PerchNoCapacityError("No open solar project capacity for "
                             "dominion-energy-va in ZIP 23220."))
    check(f"Dominion no-capacity: {msg!r}", msg == rep_errors.NO_CAPACITY)
    check("  ...which is not the generic validation sentence", msg != GENERIC)
    check("  ...and does not mention the partner or the ZIP it was given",
          not rep_errors.contains_internals(msg) and "23220" not in msg)

    # ════════════════════════════════════════════════════════════════════════
    section("3b. THE CAUSES E3 ALREADY WORDED, KEPT")
    # ════════════════════════════════════════════════════════════════════════
    # UBS has had per-(field, code) sentences since E3, in
    # services/perch/client.py. They are more specific than any category here,
    # and reaching for a category first silently replaced them: a duplicate
    # EMAIL was reported as a duplicate utility ACCOUNT, and a ZIP outside the
    # service area as a capacity shortage. Two different facts, one sentence.
    from services.perch import client as perch_client

    def mapped(field, code, message=""):
        item = {"field": field, "code": code, "message": message}
        exc = PerchValidationError(perch_client._field_error_text(item))
        exc.http_status = 422
        exc.status_code = 422
        exc.body_json = {"errors": [item]}
        return rep_errors.for_enrollment_failure(exc)

    msg, field = mapped("email_address", "already_exists")
    check(f"a duplicate EMAIL still says so: {msg!r}",
          msg == "An enrollment already exists for this email address.")
    check("  ...and is not reported as a duplicate utility account",
          msg != rep_errors.ALREADY_ENROLLED)
    check("  ...pointing at the email field", field == "c-email")

    msg, _ = mapped("zip_code", "not_in_service_area")
    check(f"a ZIP outside the service area says THAT: {msg!r}",
          "outside the service area" in msg)
    check("  ...not that capacity has run out", msg != rep_errors.NO_CAPACITY)

    msg, _ = mapped("utility_name", "invalid")
    check(f"an unsupported utility says so: {msg!r}",
          "utility is not valid" in msg)

    msg, _ = mapped("business_name", "invalid_characters")
    check(f"a code E3 knows on a field this module does not: {msg!r}",
          "characters that are not allowed" in msg)
    check("  ...rather than the generic fallback", msg != GENERIC)

    # The identifiers a rep reads off a bill keep THIS module's guidance,
    # which tells them where to look; E3's per-code sentence would only say
    # the value "is not valid".
    msg, field = mapped("utility_account_number", "invalid")
    check(f"an invalid account number still points at the bill: {msg!r}",
          msg == rep_errors.ACCOUNT_NUMBER_FORMAT and field == "c-acct")

    # ════════════════════════════════════════════════════════════════════════
    section("3c. THE DOCUMENT CAUSES")
    # ════════════════════════════════════════════════════════════════════════
    msg, field = mapped("utility_accounts[0][utility_bills]", "too_many_files")
    check(f"too many bill pages: {msg!r}", msg == rep_errors.BILL_PAGES_REJECTED)
    check("  ...and points at the bill upload", field == "bill-file")
    msg, field = mapped("utility_accounts[0][utility_bills]", "file_too_large")
    check(f"a bill file that was refused: {msg!r}",
          msg == rep_errors.BILL_FILE_REJECTED and field == "bill-file")
    msg, field = mapped("utility_accounts[0][utility_bills]", "invalid_file_type")
    check("  ...as is an unsupported file type",
          msg == rep_errors.BILL_FILE_REJECTED and field == "bill-file")

    for lmi_field in ("lmi_documents", "proof_doc", "lmi_source_selection"):
        msg, field = mapped(lmi_field, "required")
        check(f"missing LMI proof ({lmi_field}): {msg!r}",
              msg == rep_errors.LMI_DOCUMENT_REQUIRED)
        check("  ...and points at the proof-document upload", field == "lmi-file")

    # ════════════════════════════════════════════════════════════════════════
    section("3d. THE DUPLICATE-EMAIL PATH IS UNTOUCHED")
    # ════════════════════════════════════════════════════════════════════════
    # The confirmed duplicate-email rejection drives provisional-enrollment
    # cleanup (routes/perch_routes.py). It decides from the EXCEPTION, not
    # from the rep-facing sentence, so no change to the message layer may
    # affect it - asserted here because this suite is what would notice.
    # The real wording: a marker from _IN_PROGRESS_MARKERS plus the word
    # "email", which is what makes the detector narrow enough to be safe.
    dup = PerchValidationError(
        "Perch rejected the enrollment (422): email address has already "
        "been taken.")
    dup.http_status = 422
    dup.status_code = 422
    check("a duplicate-email rejection is still recognised",
          perch_client.is_duplicate_email_error(dup) is True)
    other = PerchValidationError(
        "Perch rejected the enrollment (422): utility account number already "
        "exists on another enrollment.")
    other.http_status = 422
    other.status_code = 422
    check("  ...and a duplicate ACCOUNT is still not mistaken for one",
          perch_client.is_duplicate_email_error(other) is False)
    check("  ...the decision reads the exception, not the rep-facing copy",
          "str(exc).lower()" in open(
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.abspath(__file__))), "services", "perch", "client.py"),
              encoding="utf-8").read())

    # The split that exposed E3's tables to this module must not have changed
    # what _field_error_text() produces - it is what the exception message is
    # built from, and the duplicate-email detector reads that message.
    samples = [
        {"field": "email_address", "code": "already_exists", "message": ""},
        {"field": "zip_code", "code": "not_in_service_area", "message": ""},
        {"field": "phone_number", "code": "invalid_format", "message": ""},
        {"field": "business_name", "code": "invalid_characters", "message": ""},
        {"field": "mystery_field", "code": "", "message": "must be 10 digits"},
        {"field": "utility_accounts[0]", "code": "too_many_accounts",
         "message": "", "utility_account_number": "1234567890"},
    ]
    expected_shapes = [
        "An enrollment already exists for this email address.",
        "This ZIP code is outside the service area for the selected utility.",
        "Enter a 10-digit phone number with no country code.",
        "Business name contains characters that are not allowed.",
        "Mystery field: must be 10 digits.",
        "Utility account has more accounts than this program allows. "
        "(account 1234567890)",
    ]
    for item, want in zip(samples, expected_shapes):
        got = perch_client._field_error_text(item)
        check(f"unchanged: {got[:52]!r}", got == want)

    # ════════════════════════════════════════════════════════════════════════
    section("4. NOTHING THE REP SEES CARRIES MACHINERY")
    # ════════════════════════════════════════════════════════════════════════
    every_sentence = [
        rep_errors.NO_CAPACITY, rep_errors.ACCOUNT_NUMBER_FORMAT,
        rep_errors.POD_ID_FORMAT, rep_errors.METER_NUMBER_FORMAT,
        rep_errors.PROGRAM_UNAVAILABLE, rep_errors.ALREADY_ENROLLED,
        rep_errors.CUSTOMER_INFO_MISSING, rep_errors.ENROLLMENT_NOT_ACCEPTED,
        rep_errors.VALIDATION_FAILED, rep_errors.BILL_PAGES_REJECTED,
        rep_errors.BILL_FILE_REJECTED, rep_errors.LMI_DOCUMENT_REQUIRED,
    ]
    for sentence in every_sentence:
        check(f"clean: {sentence[:54]}...",
              not rep_errors.contains_internals(sentence))

    # Partner prose must not reach the rep even when it is the only text there.
    leaky = PerchValidationError(
        "Perch returned 422 from /affiliate_partners/v2/enrollments/enroll: "
        "utility_account_number invalid_format")
    leaky.http_status = 422
    msg, _ = rep_errors.for_enrollment_failure(leaky)
    check(f"a message full of internals is replaced, not redacted: {msg!r}",
          msg == GENERIC and "422" not in msg and "perch" not in msg.lower())

    # ...but a structured error inside the same exception still names the cause.
    leaky.body_json = {"errors": [{"field": "secondary_account_identifier",
                                   "code": "invalid"}]}
    msg, field = rep_errors.for_enrollment_failure(leaky)
    check("  ...while its structured field error is still read",
          msg == rep_errors.POD_ID_FORMAT and field == "c-pod")

    # ════════════════════════════════════════════════════════════════════════
    section("5. THROUGH THE ROUTE, AS THE BROWSER RECEIVES IT")
    # ════════════════════════════════════════════════════════════════════════
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    def enrollment_rejected_with(exc, tag):
        """Drive a real /enroll whose adapter call raises `exc`."""
        import io
        from PIL import Image
        from services.perch import adapter as ad
        eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                     json={"email": f"{tag}@example.com", "zip_code": "12901",
                           "utility_name": "nyseg"}).get_json()["enrollment_id"]
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Dana", "last_name": "Reyes",
                         "email": f"{tag}@example.com", "phone": "6075550142"},
            "service_address": {"street": "742 Example Way", "city": "Plattsburgh",
                                "state": "NY", "zip": "12901"},
            "billing_address": {"same_as_service": True},
            "utility_account": {"utility_name": "nyseg",
                                "account_number": "90000000011",
                                "secondary_account_identifier": "N01000000000001"}})
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (240, 244, 250)).save(buf, "PNG")
        buf.seek(0)
        doc = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                     data={"category": "utility_bill", "files": (buf, "bill.png")},
                     content_type="multipart/form-data"
                     ).get_json()["files"][0]["document_id"]
        real = ad.create_enrollment

        def boom(enrollment_id, payload, user_id=None):
            raise exc

        ad.create_enrollment = boom
        try:
            r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                       json={"document_id": doc})
        finally:
            ad.create_enrollment = real
        return r, r.get_json() or {}

    r, body = enrollment_rejected_with(
        perch_422("secondary_account_identifier", "invalid"), "podfmt")
    check(f"an invalid POD comes back as guidance ({r.status_code})",
          body.get("error") == rep_errors.POD_ID_FORMAT)
    check("  ...with the field to correct", body.get("field") == "c-pod")
    check("  ...and no partner wording anywhere in the body",
          not rep_errors.contains_internals(body.get("error")))

    r, body = enrollment_rejected_with(
        perch_422("utility_account_number", "invalid"), "acctfmt")
    check("an invalid account number names the account number",
          body.get("error") == rep_errors.ACCOUNT_NUMBER_FORMAT
          and body.get("field") == "c-acct")

    r, body = enrollment_rejected_with(
        PerchNoCapacityError("No open solar project capacity."), "nocap")
    check("a no-capacity outcome keeps the capacity message",
          body.get("error") == rep_errors.NO_CAPACITY)
    check("  ...and claims no field", "field" not in body)

    unknown = PerchValidationError("Perch rejected the enrollment (422).")
    unknown.http_status = 422
    r, body = enrollment_rejected_with(unknown, "unknown")
    check(f"an unexplained rejection keeps the honest fallback: "
          f"{body.get('error')!r}", body.get("error") == GENERIC)
    check("  ...and points at no field", "field" not in body)

    # ════════════════════════════════════════════════════════════════════════
    section("6. THE DIAGNOSTICS SURVIVE, WITHOUT THE CUSTOMER'S DETAILS")
    # ════════════════════════════════════════════════════════════════════════
    rows = query("SELECT details_json FROM audit_logs "
                 "WHERE action LIKE 'perch_%_failed'")
    blob = " ".join((r["details_json"] or "") for r in rows)
    check(f"the failures were recorded ({len(rows)})", len(rows) >= 4)
    check("  ...keeping the detail an engineer needs",
          "422" in blob or "PerchValidationError" in blob)
    check("  ...and the exception class, for triage",
          "PerchNoCapacityError" in blob)
    for secret in ("N01000000000001", "90000000011", "Dana", "Reyes",
                   "742 Example Way"):
        check(f"  ...and no customer value: {secret[:14]}", secret not in blob)

    print("\n" + "=" * 72)
    print("REP ERROR MAPPING - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
