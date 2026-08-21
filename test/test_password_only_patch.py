"""
Password-only customer PATCH.

ROOT CAUSE: the customer branch of update_enrollment wrote all four identity
columns unconditionally -
    UPDATE customers SET first_name=?, last_name=?, email=?, phone=? ...
using c.get(...). A password-only payload made every one of them None, so
first_name was set to NULL and the write died on
"NOT NULL constraint failed: customers.first_name" - before the separate
password statement below ever ran.

FIX: write only the identity columns actually PRESENT in the payload. Absent is
now distinct from empty.

Run: python test/test_password_only_patch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed
from services.perch import workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "routes", "enrollment_routes.py"), encoding="utf-8").read()


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def snapshot(eid):
    with app.app_context():
        return dict(query_one(
            "SELECT c.first_name, c.last_name, c.email, c.phone, c.password_hash "
            "FROM customers c JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?",
            (eid,)))


def identity(snap):
    return {k: v for k, v in snap.items() if k != "password_hash"}


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                 json={"email": "pwonly@example.com", "zip_code": "10901",
                       "utility_name": "orange-and-rockland"}).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={"customer": {
        "first_name": "Jon", "last_name": "Smith", "email": "pwonly@example.com",
        "phone": "5185550100", "password": "OldPass1!"}})
    before = snapshot(eid)

    # ═══════════════════════════════════════════════════════
    section("1. PASSWORD-ONLY UPDATE SUCCEEDS")
    r = c.patch(f"/api/enrollments/{eid}", headers=rep,
                json={"customer": {"password": "NewPass1!"}})
    check("password-only PATCH returns 200", r.status_code == 200)
    check("  ...no longer a 500", r.status_code != 500)

    section("2. EXISTING FIELDS UNCHANGED")
    after = snapshot(eid)
    check("identity is byte-identical", identity(before) == identity(after))
    check("  ...first_name kept", after["first_name"] == "Jon")
    check("  ...last_name kept", after["last_name"] == "Smith")
    check("  ...email kept", after["email"] == "pwonly@example.com")
    check("  ...phone kept", after["phone"] == "5185550100")
    check("  ...nothing was nulled", all(v is not None for v in identity(after).values()))

    section("3. PASSWORD HASH CHANGES")
    check("hash changed", before["password_hash"] != after["password_hash"])
    check("  ...still present", bool(after["password_hash"]))

    section("NO PLAINTEXT STORED")
    check("the new password is not in the hash", "NewPass1!" not in str(after["password_hash"]))
    with app.app_context():
        row = query_one("SELECT * FROM customers WHERE id = "
                        "(SELECT customer_id FROM enrollments WHERE id = ?)", (eid,))
        every = " ".join(str(v) for v in dict(row).values())
    check("  ...nor anywhere else on the row", "NewPass1!" not in every and "OldPass1!" not in every)
    check("hashing goes through hash_password", "hash_password(c[\"password\"])" in SRC)

    section("4. OLD PASSWORD STOPS WORKING / NEW ONE WORKS")
    check("old password rejected",
          c.post("/api/auth/signin", json={"email": "pwonly@example.com",
                                           "password": "OldPass1!"}).status_code == 401)
    ok = c.post("/api/auth/signin", json={"email": "pwonly@example.com",
                                          "password": "NewPass1!"})
    check("new password accepted", ok.status_code == 200)
    check("  ...routed as a customer", ok.get_json()["account_type"] == "customer")
    check("  ...scoped to this enrollment", ok.get_json()["enrollment_id"] == eid)
    check("legacy customer-login also works",
          c.post("/api/auth/customer-login",
                 json={"email": "pwonly@example.com",
                       "password": "NewPass1!"}).status_code == 200)

    section("PARTIAL IDENTITY UPDATES ARE ALSO SELECTIVE")
    r2 = c.patch(f"/api/enrollments/{eid}", headers=rep,
                 json={"customer": {"phone": "5185559999"}})
    check("phone-only PATCH succeeds", r2.status_code == 200)
    s2 = snapshot(eid)
    check("  ...phone updated", s2["phone"] == "5185559999")
    check("  ...name untouched",
          s2["first_name"] == "Jon" and s2["last_name"] == "Smith")
    check("  ...email untouched", s2["email"] == "pwonly@example.com")
    check("  ...password untouched", s2["password_hash"] == after["password_hash"])
    check("an explicitly EMPTY value is still honoured (absent != empty)",
          c.patch(f"/api/enrollments/{eid}", headers=rep,
                  json={"customer": {"phone": ""}}).status_code == 200
          and snapshot(eid)["phone"] == "")
    c.patch(f"/api/enrollments/{eid}", headers=rep,
            json={"customer": {"phone": "5185550100"}})

    section("A CREDENTIAL ALONE CANNOT CREATE A CUSTOMER")
    fresh = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    rc = c.patch(f"/api/enrollments/{fresh}", headers=rep,
                 json={"customer": {"password": "SomePass1!"}})
    check("password-only on an enrollment with no customer is refused",
          rc.status_code == 400)
    check("  ...with a clear reason",
          "Customer details are required" in rc.get_json()["error"])
    with app.app_context():
        check("  ...and no orphan customer row was created",
              query_one("SELECT customer_id FROM enrollments WHERE id = ?",
                        (fresh,))["customer_id"] is None)

    # ═══════════════════════════════════════════════════════
    section("5. COMMIT BOUNDARY UNCHANGED")
    with app.app_context():
        workflow.set_state(eid, "contracts_review")
        check("enrollment is committed", workflow.perch_committed(eid) is True)
    committed_before = snapshot(eid)

    rp = c.patch(f"/api/enrollments/{eid}", headers=rep,
                 json={"customer": {"password": "PostCommit1!"}})
    check("password-only STILL allowed after commit (Dalton-local)", rp.status_code == 200)
    check("  ...identity untouched",
          identity(snapshot(eid)) == identity(committed_before))
    check("  ...and it really took effect",
          c.post("/api/auth/signin", json={"email": "pwonly@example.com",
                                           "password": "PostCommit1!"}).status_code == 200)

    for payload, label in [
            ({"customer": {"first_name": "John"}}, "name"),
            ({"customer": {"email": "other@example.com"}}, "email"),
            ({"customer": {"phone": "5180000000"}}, "phone"),
            ({"customer": {"first_name": "John", "password": "X1!"}}, "name + password"),
            ({"service_address": {"street": "9 New St"}}, "service address"),
            ({"utility_account": {"account_number": "9999999999"}}, "utility account")]:
        rr = c.patch(f"/api/enrollments/{eid}", headers=rep, json=payload)
        check(f"committed {label} edit still refused (409)", rr.status_code == 409)
    check("committed Perch data is untouched after every refusal",
          identity(snapshot(eid)) == identity(committed_before))

    section("PROGRAM LOCK UNAFFECTED")
    pr = c.post(f"/api/perch/enrollments/{eid}/program", headers=rep,
                json={"customer_type": "Residential"})
    check("program change still refused after commit", pr.status_code == 409)

    section("THE FIX ITSELF")
    check("identity columns are whitelisted",
          'IDENTITY_FIELDS = ("first_name", "last_name", "email", "phone")' in SRC)
    check("  ...and only the PROVIDED ones are written",
          "provided = [k for k in IDENTITY_FIELDS if k in c]" in SRC)
    check("  ...with no unconditional four-column UPDATE",
          "UPDATE customers SET first_name=?, last_name=?, email=?, phone=?" not in SRC)
    check("  ...the identity write is skipped when nothing was supplied",
          "if provided:" in SRC)
    check("column names never come from caller input",
          "cannot inject" in SRC)

    print(f"\n{'='*72}\nPASSWORD-ONLY PATCH - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
