"""
Unified login: ONE form authenticates staff and customers.

UNIFIED LOGIN, NOT UNIFIED PERMISSIONS. The same tokens are issued by the same
functions as before (issue_token -> scope "staff", issue_customer_token ->
scope "customer"), so require_auth and require_customer_auth keep rejecting each
other's scope. No route becomes reachable that was not reachable before.

Run: python test/test_unified_login.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query_one
import seed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()

STAFF = [("charlie@daltonsolar.com", "RepPass1!", "sales_rep"),
         ("admin@daltonsolar.com", "AdminPass1!", "admin"),
         ("qa@daltonsolar.com", "QaPass1!", "qa_reviewer"),
         ("developer@perchenergy.com", "DevPass1!", "developer")]
GENERIC = "Invalid email or password."


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def make_customer(c, rep_headers, email, password):
    eid = c.post("/api/perch/enrollments/capacity", headers=rep_headers,
                 json={"email": email, "zip_code": "12401",
                       "utility_name": "central-hudson-gas-electric"}).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid}", headers=rep_headers,
            json={"customer": {"first_name": "Jane", "last_name": "Doe", "email": email,
                               "phone": "5185550100", "password": password}})
    return eid


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()

    section("1 + 2. STAFF ROLES THROUGH THE SINGLE FORM")
    for email, pw, role in STAFF:
        r = c.post("/api/auth/signin", json={"email": email, "password": pw})
        b = r.get_json()
        check(f"{role} signs in", r.status_code == 200)
        check("  ...identified as staff", b["account_type"] == "staff")
        check(f"  ...role reported as {role}", b["user"]["role"] == role)
        check("  ...routed to a destination", bool(b.get("destination")))
        check("  ...token issued", bool(b.get("token")))
        check("  ...no password echoed", "password" not in str(b).lower())

    section("ROUTING IS SERVER-AUTHORITATIVE")
    r = c.post("/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                         "password": "RepPass1!",
                                         "role": "admin", "account_type": "admin"})
    check("a client-supplied role is IGNORED", r.get_json()["user"]["role"] == "sales_rep")
    check("  ...and account_type comes from the server",
          r.get_json()["account_type"] == "staff")
    check("the frontend never sends a role",
          "'/api/auth/signin'" in JS
          and "JSON.stringify({ email, password: pass })" in JS)
    check("  ...and obeys the server's account_type",
          "data.account_type === 'customer'" in JS)

    section("3. CUSTOMER THROUGH THE SAME FORM")
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/login", json={"email": "charlie@daltonsolar.com",
                                 "password": "RepPass1!"}).get_json()["token"]}
    eid = make_customer(c, rep, "unified.cust@example.com", "CustPass1!")
    r = c.post("/api/auth/signin", json={"email": "unified.cust@example.com",
                                         "password": "CustPass1!"})
    cb = r.get_json()
    check("customer signs in on the SAME endpoint", r.status_code == 200)
    check("  ...identified as customer", cb["account_type"] == "customer")
    check("  ...routed to the portal", cb["destination"] == "customer_portal")
    check("  ...scoped to their enrollment", cb["enrollment_id"] == eid)
    check("  ...enrollment code returned", bool(cb.get("enrollment_code")))
    cust = {"Authorization": "Bearer " + cb["token"]}

    section("4. INVALID CREDENTIALS — ONE GENERIC FAILURE")
    cases = [("charlie@daltonsolar.com", "WRONG", "staff, bad password"),
             ("unified.cust@example.com", "WRONG", "customer, bad password"),
             ("nobody@nowhere.example", "x", "unknown email"),
             ("", "x", "no email"),
             ("x@y.com", "", "no password")]
    seen = set()
    for email, pw, label in cases:
        rr = c.post("/api/auth/signin", json={"email": email, "password": pw})
        check(f"{label} is rejected", rr.status_code in (400, 401))
        if rr.status_code == 401:
            seen.add(rr.get_json()["error"])
    check("every 401 returns the SAME message (no enumeration)", seen == {GENERIC})

    # A customer with a valid password but no enrollment must not be confirmable.
    from auth import hash_password
    from db import execute
    with app.app_context():
        execute("INSERT INTO customers (first_name,last_name,email,password_hash) "
                "VALUES (?,?,?,?)", ("No", "Enrollment", "orphan@example.com",
                                     hash_password("OrphanPass1!")))
    ro = c.post("/api/auth/signin", json={"email": "orphan@example.com",
                                          "password": "OrphanPass1!"})
    check("correct password + no enrollment still returns the generic failure",
          ro.status_code == 401 and ro.get_json()["error"] == GENERIC)

    section("5. CUSTOMER CANNOT REACH REP/ADMIN ROUTES")
    for path, label in [("/api/enrollments", "enrollment list"),
                        ("/api/admin/reps", "admin reps"),
                        ("/api/auth/me", "staff identity")]:
        check(f"customer blocked from {label}",
              c.get(path, headers=cust).status_code in (401, 403))
    check("customer blocked from another enrollment's detail",
          c.get(f"/api/enrollments/{eid}", headers=cust).status_code in (401, 403))
    check("  ...but CAN read their own agreement",
          c.get("/api/auth/customer-me", headers=cust).status_code == 200)

    section("6. STAFF GAINS NO CUSTOMER AUTHORIZATION")
    check("rep blocked from the customer identity route",
          c.get("/api/auth/customer-me", headers=rep).status_code in (401, 403))
    admin = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "admin@daltonsolar.com",
                                  "password": "AdminPass1!"}).get_json()["token"]}
    check("admin blocked from the customer identity route",
          c.get("/api/auth/customer-me", headers=admin).status_code in (401, 403))
    check("  ...admin retains its own routes",
          c.get("/api/admin/reps", headers=admin).status_code == 200)
    check("  ...rep does NOT reach admin routes",
          c.get("/api/admin/reps", headers=rep).status_code == 403)

    section("SCOPES ARE UNCHANGED")
    import jwt as _jwt
    from auth import JWT_SECRET
    staff_payload = _jwt.decode(c.post("/api/auth/signin",
        json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"}
        ).get_json()["token"], JWT_SECRET, algorithms=["HS256"])
    cust_payload = _jwt.decode(cb["token"], JWT_SECRET, algorithms=["HS256"])
    check("staff token keeps scope 'staff'", staff_payload["scope"] == "staff")
    check("customer token keeps scope 'customer'", cust_payload["scope"] == "customer")
    check("  ...and carries its enrollment id", cust_payload.get("enrollment_id") == eid)
    check("staff token has NO enrollment scope", "enrollment_id" not in staff_payload)

    section("7. LOGOUT / SESSION SEPARATION")
    check("the two client stores are distinct",
          "dalton_customer_token" in JS and JS.count("KEY: 'dalton_customer_token'") == 1)
    check("signing in as a customer clears any staff token",
          "AuthStore.clear();\n    currentUser = null;" in JS)
    check("signing in as staff clears any customer token", "CustomerAuth.clear();" in JS)
    check("both stores expose clear()",
          JS.count("clear(){ try { sessionStorage.removeItem(this.KEY); } catch(e){} }") == 2)

    section("8. NOTHING REMOVED")
    check("/api/auth/login still works",
          c.post("/api/auth/login", json={"email": "charlie@daltonsolar.com",
                                          "password": "RepPass1!"}).status_code == 200)
    check("/api/auth/customer-login still works",
          c.post("/api/auth/customer-login",
                 json={"email": "unified.cust@example.com",
                       "password": "CustPass1!"}).status_code == 200)
    check("the customer login screen is retained for direct entry",
          'id="screen-customer-login"' in HTML)
    check("no role-choice link is needed on the main form",
          "Sign in to your agreement</a>" not in HTML)
    check("  ...and the form no longer says 'Rep sign in'", "Rep sign in" not in HTML)
    check("magic links were NOT implemented",
          "magic" not in JS.lower() and "magic_link" not in open(
              os.path.join(ROOT, "routes", "auth_routes.py"), encoding="utf-8").read().lower())

    section("PASSWORD HANDLING UNCHANGED")
    auth_src = open(os.path.join(ROOT, "routes", "auth_routes.py"), encoding="utf-8").read()
    check("verification still uses verify_password", "verify_password(password," in auth_src)
    check("staff issuance still uses issue_token", "token = issue_token(user)" in auth_src)
    check("customer issuance still uses issue_customer_token",
          "issue_customer_token(customer, enrollment[\"id\"])" in auth_src)
    check("no plaintext password is stored by signin",
          "password_hash = ?" not in auth_src.split("def unified_signin")[1])
    with app.app_context():
        row = query_one("SELECT password_hash FROM customers WHERE lower(trim(email)) = ?",
                        ("unified.cust@example.com",))
    check("stored customer credential is a hash, not the password",
          row["password_hash"] != "CustPass1!" and len(row["password_hash"]) > 20)

    print(f"\n{'='*72}\nUNIFIED LOGIN - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
