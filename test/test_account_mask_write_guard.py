"""
A display mask must never be written back as data.

THE DEFECT THIS PINS  (found in Phase D, 2026-10-28)

    _serialize_enrollment() masks utility_account.account_number for a
    sales_rep, so the rep-facing enrollment detail carries "******7890". The
    wizard hydrates its Customer & Bill form from that payload, which put the
    MASK into the editable account field - and the next save read the field
    back and wrote it. Save & Exit, reopen, Continue was enough to replace the
    real account number with "******7890" in utility_accounts.

    That is the one value Perch matches the customer on, destroyed by the most
    ordinary sequence a rep performs, on the market that is launching. The
    stored value was confirmed corrupted in a real browser against the real
    application before this guard existed.

    Perch itself would have rejected the mask - adapter.create_enrollment
    requires all digits of the exact expected length - so the damage was
    confined to the UBS record rather than reaching the partner. The UBS
    record is the authoritative one.

WHAT IS NOT CHANGED
    Masking itself. A sales_rep still sees the account number masked on the
    detail endpoint, which test_phase_a_reliability.py asserts; this suite
    asserts it too, so the fix cannot be mistaken for permission to stop
    masking. The guard is on the WRITE, where a display artefact has no
    business being accepted.

Run: python test/test_account_mask_write_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed
from helpers import mask_account_number, looks_masked


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def stored(enrollment_id):
    """The row as DB truth - the API masks it for a rep by design."""
    return query_one(
        "SELECT ua.* FROM utility_accounts ua "
        "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
        (enrollment_id,))


def main():
    init_db(reset=True)
    seed.seed()

    with app.test_client() as c:
        rep = login(c, "charlie@daltonsolar.com", "RepPass1!")
        admin = login(c, "admin@daltonsolar.com", "AdminPass1!")

        section("1. THE HELPER RECOGNISES A MASK, AND ONLY A MASK")
        check("a mask is recognised", looks_masked(mask_account_number("1234567890")))
        check("  ...whatever its length", looks_masked(mask_account_number("123456789012")))
        check("a real account number is not", not looks_masked("1234567890"))
        check("  ...nor a 12-digit one", not looks_masked("123456789012"))
        check("empty is not a mask", not looks_masked("") and not looks_masked(None))

        section("2. A REP STILL SEES THE ACCOUNT NUMBER MASKED  (unchanged)")
        eid = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
        r = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Dana", "last_name": "Reyes",
                         "email": "dana.mask@example.com"},
            "service_address": {"street": "12 Elm St", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": "national-grid-ny",
                                "account_number": "1234567890",
                                "meter_number": "9876543210"}})
        check(f"the account saves (HTTP {r.status_code})", r.status_code == 200)
        check("the database holds the real number",
              stored(eid)["account_number"] == "1234567890")
        seen = (c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
                .get("utility_account") or {}).get("account_number")
        check(f"a rep sees it masked ({seen})", looks_masked(seen))
        check("  ...with the last four still readable", str(seen).endswith("7890"))
        admin_seen = (c.get(f"/api/enrollments/{eid}", headers=admin).get_json()
                      .get("utility_account") or {}).get("account_number")
        check("an admin still sees the full number", admin_seen == "1234567890")

        section("3. SAVING THE MASK BACK DOES NOT OVERWRITE THE REAL VALUE")
        # Exactly what the wizard did: hydrate from the rep payload, save.
        r = c.patch(f"/api/enrollments/{eid}", headers=rep,
                    json={"utility_account": {"utility_name": "national-grid-ny",
                                              "account_number": seen}})
        check(f"the save is accepted, not rejected ({r.status_code})",
              r.status_code == 200)
        check("THE REAL ACCOUNT NUMBER SURVIVES",
              stored(eid)["account_number"] == "1234567890")
        check("  ...and the mask was not stored",
              not looks_masked(stored(eid)["account_number"]))

        section("3b. A MASKED METER NUMBER IS REFUSED THE SAME WAY")
        r = c.patch(f"/api/enrollments/{eid}", headers=rep,
                    json={"utility_account": {"meter_number": "******3210"}})
        check(f"the save is accepted ({r.status_code})", r.status_code == 200)
        check("the real meter number survives",
              stored(eid)["meter_number"] == "9876543210")

        section("4. A GENUINE EDIT STILL WRITES NORMALLY")
        r = c.patch(f"/api/enrollments/{eid}", headers=rep,
                    json={"utility_account": {"account_number": "2222222222"}})
        check(f"the edit is accepted ({r.status_code})", r.status_code == 200)
        check("the new account number is stored",
              stored(eid)["account_number"] == "2222222222")
        check("  ...and the meter number was left alone",
              stored(eid)["meter_number"] == "9876543210")

        section("5. A MASK CANNOT BECOME THE ACCOUNT NUMBER ON A FIRST WRITE")
        # The INSERT path, where there is no stored value to fall back to.
        e2 = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
        c.patch(f"/api/enrollments/{e2}", headers=rep, json={
            "customer": {"first_name": "Lee", "last_name": "Ng",
                         "email": "lee.mask@example.com"},
            "service_address": {"street": "9 Oak Ave", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": "national-grid-ny",
                                "account_number": "******4321"}})
        row = stored(e2)
        # No usable account number means no utility-account row yet. The
        # column is NOT NULL, so the alternative is an IntegrityError - a 500
        # on an ordinary save, which is what a naive "store None instead"
        # produced when this guard was first written.
        check("no utility account is invented from a mask", row is None)
        check("  ...and the rest of the save still succeeded",
              query_one("SELECT first_name FROM customers c JOIN enrollments e "
                        "ON e.customer_id = c.id WHERE e.id = ?",
                        (e2,))["first_name"] == "Lee")
        # And a later save carrying a REAL number creates it properly.
        c.patch(f"/api/enrollments/{e2}", headers=rep,
                json={"utility_account": {"utility_name": "national-grid-ny",
                                          "account_number": "7778889990"}})
        check("  ...while a real number creates the row",
              (stored(e2) or {})["account_number"] == "7778889990")

        section("6. THE WIZARD'S OWN REOPEN SEQUENCE, END TO END")
        # hydrate -> save unchanged -> hydrate again. The exact loop that
        # destroyed the value.
        e3 = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
        c.patch(f"/api/enrollments/{e3}", headers=rep, json={
            "customer": {"first_name": "Sam", "last_name": "Patel",
                         "email": "sam.mask@example.com"},
            "service_address": {"street": "3 Pine St", "city": "Albany",
                                "state": "NY", "zip": "12207"},
            "utility_account": {"utility_name": "national-grid-ny",
                                "account_number": "5551234567"}})
        for round_trip in range(3):
            detail = c.get(f"/api/enrollments/{e3}", headers=rep).get_json()
            acct_as_shown = (detail.get("utility_account") or {}).get("account_number")
            c.patch(f"/api/enrollments/{e3}", headers=rep,
                    json={"utility_account": {"utility_name": "national-grid-ny",
                                              "account_number": acct_as_shown}})
            check(f"round trip {round_trip + 1}: the account number is intact",
                  stored(e3)["account_number"] == "5551234567")

    print("\n" + "=" * 72)
    print("ACCOUNT MASK WRITE GUARD - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
