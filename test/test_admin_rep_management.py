"""
Admin rep account management.

Run: python test/test_admin_rep_management.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    return ({"Authorization": f"Bearer {r.get_json()['token']}"}
            if r.status_code == 200 else None), r.status_code


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()

    ADMIN, _ = login(c, "admin@daltonsolar.com", "AdminPass1!")
    REP, _ = login(c, "charlie@daltonsolar.com", "RepPass1!")
    QA, _ = login(c, "qa@daltonsolar.com", "QaPass1!")
    DEV, _ = login(c, "developer@perchenergy.com", "DevPass1!")

    # ═══════════════════════════════════════════════════════
    section("ACCESS CONTROL — /api/admin/* is admin only")
    endpoints = [
        ("GET", "/api/admin/reps", None),
        ("POST", "/api/admin/reps", {"full_name": "X", "email": "x@d.com",
                                      "password": "Password123"}),
        ("PATCH", "/api/admin/reps/1", {"phone": "555"}),
        ("POST", "/api/admin/reps/1/password", {"password": "Password123"}),
        ("POST", "/api/admin/reps/1/activate", {}),
        ("POST", "/api/admin/reps/1/deactivate", {}),
    ]
    for name, h in (("sales_rep", REP), ("qa_reviewer", QA), ("developer", DEV)):
        for method, url, body in endpoints:
            fn = getattr(c, method.lower())
            r = fn(url, headers=h, json=body) if body is not None else fn(url, headers=h)
            check(f"{name:12} {method:6} {url:34} -> 403", r.status_code == 403)
    for method, url, body in endpoints:
        fn = getattr(c, method.lower())
        r = fn(url, json=body) if body is not None else fn(url)
        check(f"{'anonymous':12} {method:6} {url:34} -> 401", r.status_code == 401)

    # ═══════════════════════════════════════════════════════
    section("CREATE — both rows, atomically")
    r = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Dana Rep", "email": "  Dana.Rep@Example.COM  ",
        "password": "InitialPass123", "phone": "518-555-0100", "team": "Capital District"})
    check("admin can create a rep", r.status_code == 201)
    body = r.get_json()
    uid = body["user_id"]
    check("  ...returns the new user id", isinstance(uid, int))
    check("  ...email normalized (trim + lowercase)", body["email"] == "dana.rep@example.com")
    check("  ...full name stored", body["full_name"] == "Dana Rep")
    check("  ...phone stored", body["phone"] == "518-555-0100")
    check("  ...team stored", body["team"] == "Capital District")
    check("  ...active by default", body["is_active"] == 1)
    check("  ...enrollment count starts at 0", body["enrollment_count"] == 0)
    check("  ...NO password hash in the response",
          "password_hash" not in body and "password" not in body)

    with app.app_context():
        u = query_one("SELECT * FROM users WHERE id = ?", (uid,))
        sr = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (uid,))
    check("users row created", u is not None)
    check("sales_reps row created", sr is not None)
    check("role is sales_rep", u["role"] == "sales_rep")
    check("password is hashed with the existing PBKDF2 helper",
          u["password_hash"].startswith("pbkdf2_sha256$"))
    check("plaintext password is NOT stored", "InitialPass123" not in u["password_hash"])

    section("CREATE — role can never be spoofed")
    for bad_role in ("admin", "developer", "qa_reviewer"):
        rr = c.post("/api/admin/reps", headers=ADMIN, json={
            "full_name": f"Spoof {bad_role}", "email": f"spoof-{bad_role}@d.com",
            "password": "Password123", "role": bad_role, "is_active": 0})
        check(f"posting role={bad_role!r} still creates a sales_rep",
              rr.status_code == 201 and rr.get_json()["role"] == "sales_rep")
        with app.app_context():
            row = query_one("SELECT role, is_active FROM users WHERE id = ?",
                            (rr.get_json()["user_id"],))
        check(f"  ...stored role is sales_rep, not {bad_role}", row["role"] == "sales_rep")
        check("  ...spoofed is_active ignored (created active)", row["is_active"] == 1)
    with app.app_context():
        admins = query_one("SELECT COUNT(*) n FROM users WHERE role = 'admin'")["n"]
    check("no extra admin accounts were created", admins == 1)

    section("CREATE — rep code auto-generation")
    with app.app_context():
        codes = [x["rep_code"] for x in query("SELECT rep_code FROM sales_reps")]
    check("auto-generated codes follow REP-nnn",
          any(x.startswith("REP-") for x in codes))
    r1 = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Auto One", "email": "auto1@d.com", "password": "Password123"})
    r2 = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Auto Two", "email": "auto2@d.com", "password": "Password123"})
    c1, c2 = r1.get_json()["rep_code"], r2.get_json()["rep_code"]
    check(f"sequential and unique ({c1} then {c2})", c1 != c2)
    check("  ...both match REP-nnn",
          c1.startswith("REP-") and c2.startswith("REP-"))
    with app.app_context():
        allcodes = [x["rep_code"] for x in query("SELECT rep_code FROM sales_reps")]
    check("all rep codes are unique", len(allcodes) == len(set(allcodes)))

    explicit = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Explicit Code", "email": "explicit@d.com",
        "password": "Password123", "rep_code": "NORTH-7"})
    check("an explicit rep code is honoured",
          explicit.get_json()["rep_code"] == "NORTH-7")

    section("CREATE — duplicate and invalid input")
    dup = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Dupe", "email": "dana.rep@example.com", "password": "Password123"})
    check("duplicate email -> 409", dup.status_code == 409)
    check("  ...with a clear message", "already exists" in dup.get_json()["error"])
    dup_case = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Dupe", "email": "DANA.REP@EXAMPLE.COM", "password": "Password123"})
    check("duplicate email in DIFFERENT CASE also -> 409", dup_case.status_code == 409)
    dupcode = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Dupe Code", "email": "unique-x@d.com",
        "password": "Password123", "rep_code": "NORTH-7"})
    check("duplicate rep code -> 409", dupcode.status_code == 409)
    check("  ...with a clear message", "already in use" in dupcode.get_json()["error"])
    for label, payload in [
        ("missing name", {"email": "a@d.com", "password": "Password123"}),
        ("missing email", {"full_name": "A", "password": "Password123"}),
        ("bad email", {"full_name": "A", "email": "notanemail", "password": "Password123"}),
        ("short password", {"full_name": "A", "email": "b@d.com", "password": "short"}),
        ("no password", {"full_name": "A", "email": "c@d.com"}),
        ("bad rep code", {"full_name": "A", "email": "d@d.com",
                          "password": "Password123", "rep_code": "bad code!"}),
    ]:
        check(f"{label} -> 400",
              c.post("/api/admin/reps", headers=ADMIN, json=payload).status_code == 400)

    section("CREATE — a failure leaves NEITHER row (atomicity)")
    with app.app_context():
        before_u = query_one("SELECT COUNT(*) n FROM users")["n"]
        before_r = query_one("SELECT COUNT(*) n FROM sales_reps")["n"]
    # A rep code that collides makes the SECOND insert fail after the first.
    fail = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Atomic Test", "email": "atomic@d.com",
        "password": "Password123", "rep_code": "NORTH-7"})
    check("conflicting create is rejected", fail.status_code == 409)
    with app.app_context():
        after_u = query_one("SELECT COUNT(*) n FROM users")["n"]
        after_r = query_one("SELECT COUNT(*) n FROM sales_reps")["n"]
        orphan = query_one("SELECT * FROM users WHERE email = 'atomic@d.com'")
    check("no users row was left behind", after_u == before_u)
    check("no sales_reps row was left behind", after_r == before_r)
    check("no half-created user exists", orphan is None)

    # Prove the transaction itself rolls back, not just the pre-check.
    from db import transaction
    with app.app_context():
        try:
            with transaction() as tx:
                tx.execute("INSERT INTO users (email,password_hash,role,full_name) "
                           "VALUES ('rollback@d.com','h','sales_rep','RB')")
                tx.execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (99999, 'X-1')")
        except Exception:
            pass
        left = query_one("SELECT * FROM users WHERE email = 'rollback@d.com'")
    check("a mid-transaction failure rolls the users row back", left is None)

    section("NO ORPHANS ANYWHERE")
    with app.app_context():
        orphans = query("SELECT u.id FROM users u LEFT JOIN sales_reps sr ON sr.user_id = u.id "
                        "WHERE u.role = 'sales_rep' AND sr.id IS NULL")
    check("every sales_rep user has a sales_reps row", len(orphans) == 0)

    # ═══════════════════════════════════════════════════════
    section("LIST")
    lst = c.get("/api/admin/reps", headers=ADMIN)
    check("admin can list reps", lst.status_code == 200)
    reps = lst.get_json()
    check("the new rep appears", any(x["user_id"] == uid for x in reps))
    one = [x for x in reps if x["user_id"] == uid][0]
    for f in ("full_name", "email", "rep_code", "phone", "team",
              "is_active", "enrollment_count"):
        check(f"  ...includes {f}", f in one)
    check("NO password hash anywhere in the list",
          "password_hash" not in lst.get_data(as_text=True))
    check("only sales reps are listed (no admin/QA/developer)",
          all(x.get("role", "sales_rep") == "sales_rep" for x in reps)
          and not any(x["email"] == "admin@daltonsolar.com" for x in reps))

    section("LOGIN + ENROLLMENT COUNT")
    NEW, code = login(c, "dana.rep@example.com", "InitialPass123")
    check("the new rep can log in", code == 200)
    eid = c.post("/api/perch/drafts", headers=NEW).get_json()["enrollment_id"]
    check("the new rep can create an enrollment", isinstance(eid, int))
    again = [x for x in c.get("/api/admin/reps", headers=ADMIN).get_json()
             if x["user_id"] == uid][0]
    check("enrollment count reflects it", again["enrollment_count"] == 1)

    # ═══════════════════════════════════════════════════════
    section("UPDATE — contact details only")
    up = c.patch(f"/api/admin/reps/{uid}", headers=ADMIN,
                 json={"phone": "518-555-0199", "team": "Hudson Valley"})
    check("phone and team update", up.status_code == 200)
    check("  ...phone changed", up.get_json()["phone"] == "518-555-0199")
    check("  ...team changed", up.get_json()["team"] == "Hudson Valley")
    upc = c.patch(f"/api/admin/reps/{uid}", headers=ADMIN, json={"rep_code": "CAP-01"})
    check("rep code updates", upc.get_json()["rep_code"] == "CAP-01")
    check("duplicate rep code on update -> 409",
          c.patch(f"/api/admin/reps/{uid}", headers=ADMIN,
                  json={"rep_code": "NORTH-7"}).status_code == 409)
    check("blank rep code rejected",
          c.patch(f"/api/admin/reps/{uid}", headers=ADMIN,
                  json={"rep_code": "  "}).status_code == 400)

    # Privileged fields must be ignored, not applied.
    c.patch(f"/api/admin/reps/{uid}", headers=ADMIN,
            json={"role": "admin", "email": "hijack@d.com", "is_active": 0,
                  "password_hash": "x", "phone": "111"})
    with app.app_context():
        row = query_one("SELECT * FROM users WHERE id = ?", (uid,))
    check("PATCH cannot change role", row["role"] == "sales_rep")
    check("PATCH cannot change email", row["email"] == "dana.rep@example.com")
    check("PATCH cannot change is_active", row["is_active"] == 1)
    check("PATCH cannot overwrite the password hash", row["password_hash"] != "x")
    check("empty PATCH -> 400",
          c.patch(f"/api/admin/reps/{uid}", headers=ADMIN, json={}).status_code == 400)

    # ═══════════════════════════════════════════════════════
    section("DEACTIVATE")
    d = c.post(f"/api/admin/reps/{uid}/deactivate", headers=ADMIN, json={})
    check("deactivate succeeds", d.status_code == 200)
    check("  ...is_active is 0", d.get_json()["is_active"] == 0)
    _, code = login(c, "dana.rep@example.com", "InitialPass123")
    check("deactivated rep CANNOT log in", code != 200)
    check("the ALREADY-ISSUED token stops working",
          c.get("/api/enrollments", headers=NEW).status_code == 401)
    with app.app_context():
        u2 = query_one("SELECT * FROM users WHERE id = ?", (uid,))
        sr2 = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (uid,))
        enr = query_one("SELECT * FROM enrollments WHERE id = ?", (eid,))
    check("users row preserved", u2 is not None)
    check("sales_reps row preserved", sr2 is not None)
    check("enrollment ownership preserved", enr["sales_rep_id"] == sr2["id"])
    check("creator history preserved", enr["created_by_user_id"] == uid)
    check("admin can still see the historical enrollment",
          c.get(f"/api/enrollments/{eid}", headers=ADMIN).status_code == 200)
    check("the deactivated rep still appears in the admin list",
          any(x["user_id"] == uid for x in c.get("/api/admin/reps",
                                                  headers=ADMIN).get_json()))

    section("REACTIVATE")
    a = c.post(f"/api/admin/reps/{uid}/activate", headers=ADMIN, json={})
    check("activate succeeds", a.status_code == 200)
    check("  ...is_active is 1", a.get_json()["is_active"] == 1)
    BACK, code = login(c, "dana.rep@example.com", "InitialPass123")
    check("reactivated rep can log in again", code == 200)
    check("  ...and still owns their enrollment",
          c.get(f"/api/enrollments/{eid}", headers=BACK).status_code == 200)

    section("ADMIN CANNOT DEACTIVATE THEMSELVES")
    with app.app_context():
        admin_id = query_one("SELECT id FROM users WHERE email = ?",
                             ("admin@daltonsolar.com",))["id"]
    self_off = c.post(f"/api/admin/reps/{admin_id}/deactivate", headers=ADMIN, json={})
    check("self-deactivation is refused", self_off.status_code in (400, 404))
    with app.app_context():
        still = query_one("SELECT is_active FROM users WHERE id = ?", (admin_id,))
    check("  ...the admin remains active", still["is_active"] == 1)
    check("admin can still log in",
          login(c, "admin@daltonsolar.com", "AdminPass1!")[1] == 200)

    section("NON-REP ACCOUNTS ARE NOT MANAGEABLE HERE")
    with app.app_context():
        qa_id = query_one("SELECT id FROM users WHERE email = ?",
                          ("qa@daltonsolar.com",))["id"]
    for path, body in ((f"/api/admin/reps/{qa_id}/deactivate", {}),
                       (f"/api/admin/reps/{qa_id}/password", {"password": "Password123"})):
        check(f"QA account not manageable via {path.split('/')[-1]} -> 404",
              c.post(path, headers=ADMIN, json=body).status_code == 404)
    check("QA account not patchable -> 404",
          c.patch(f"/api/admin/reps/{qa_id}", headers=ADMIN,
                  json={"phone": "1"}).status_code == 404)
    with app.app_context():
        qa_row = query_one("SELECT is_active, role FROM users WHERE id = ?", (qa_id,))
    check("  ...QA remains active and unchanged",
          qa_row["is_active"] == 1 and qa_row["role"] == "qa_reviewer")

    # ═══════════════════════════════════════════════════════
    section("PASSWORD RESET")
    pr = c.post(f"/api/admin/reps/{uid}/password", headers=ADMIN,
                json={"password": "BrandNewPass456"})
    check("admin can reset a password", pr.status_code == 200)
    check("  ...no hash returned", "password_hash" not in pr.get_data(as_text=True))
    _, old_code = login(c, "dana.rep@example.com", "InitialPass123")
    check("the OLD password stops working", old_code != 200)
    NEWPW, new_code = login(c, "dana.rep@example.com", "BrandNewPass456")
    check("the NEW password works", new_code == 200)
    check("  ...and the rep still owns their enrollment",
          c.get(f"/api/enrollments/{eid}", headers=NEWPW).status_code == 200)
    check("short reset password -> 400",
          c.post(f"/api/admin/reps/{uid}/password", headers=ADMIN,
                 json={"password": "abc"}).status_code == 400)
    with app.app_context():
        h = query_one("SELECT password_hash FROM users WHERE id = ?", (uid,))["password_hash"]
    check("the stored hash uses the existing PBKDF2 format",
          h.startswith("pbkdf2_sha256$"))
    check("plaintext is not stored", "BrandNewPass456" not in h)

    section("MISSING REPS")
    for method, path, body in (("post", "/api/admin/reps/999999/activate", {}),
                               ("post", "/api/admin/reps/999999/deactivate", {}),
                               ("post", "/api/admin/reps/999999/password",
                                {"password": "Password123"}),
                               ("patch", "/api/admin/reps/999999", {"phone": "1"})):
        check(f"{path.split('/')[-1]:12} on a missing rep -> 404",
              getattr(c, method)(path, headers=ADMIN, json=body).status_code == 404)

    # ═══════════════════════════════════════════════════════
    section("NO DELETE, NO ROLE CHANGE")
    rules = [r for r in app.url_map.iter_rules() if "/api/admin" in str(r)]
    check("no DELETE method on any admin route",
          not any("DELETE" in r.methods for r in rules))
    check("no route mentions role", not any("role" in str(r) for r in rules))
    src = open(os.path.join(ROOT, "routes", "admin_routes.py"), encoding="utf-8").read()
    check("admin_routes never issues a DELETE statement", "DELETE FROM" not in src.upper())
    check("admin_routes never updates users.role", "SET role" not in src)
    check("role is hardcoded to sales_rep", 'MANAGED_ROLE = "sales_rep"' in src)
    check("role is never read from the request body",
          'data.get("role")' not in src and "data.get('role')" not in src)
    check("delete on a rep endpoint -> 405",
          c.delete(f"/api/admin/reps/{uid}", headers=ADMIN).status_code == 405)

    section("AUDIT TRAIL")
    with app.app_context():
        actions = {x["action"] for x in query(
            "SELECT action FROM audit_logs WHERE action LIKE 'admin_rep_%'")}
    for a in ("admin_rep_created", "admin_rep_updated", "admin_rep_password_reset",
              "admin_rep_activated", "admin_rep_deactivated"):
        check(f"{a} is audit-logged", a in actions)
    with app.app_context():
        leak = query("SELECT details_json FROM audit_logs WHERE action LIKE 'admin_rep_%'")
    check("no password or hash appears in the audit trail",
          not any("Password123" in (x["details_json"] or "")
                  or "pbkdf2" in (x["details_json"] or "") for x in leak))

    section("OWNERSHIP ENFORCEMENT UNCHANGED")
    other = c.post("/api/admin/reps", headers=ADMIN, json={
        "full_name": "Other Rep", "email": "other@d.com", "password": "Password123"})
    OTHER, _ = login(c, "other@d.com", "Password123")
    check("a rep created here cannot read another rep's enrollment",
          c.get(f"/api/enrollments/{eid}", headers=OTHER).status_code == 403)
    check("  ...nor update it",
          c.patch(f"/api/enrollments/{eid}", headers=OTHER,
                  json={"customer": {"first_name": "X", "last_name": "Y",
                                     "email": "x@y.com"}}).status_code == 403)
    check("  ...but sees their own (empty) dashboard",
          c.get("/api/enrollments", headers=OTHER).get_json() == [])

    section("ADMIN UI")
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("a Rep management view exists", 'id="view-reps"' in html)
    check("nav entry exists", 'id="nav-reps"' in html)
    check("nav entry is hidden by default", 'id="nav-reps"' in html
          and 'style="display:none;"' in html.split('id="nav-reps"')[1][:120])
    check("nav is revealed only for admins",
          "currentUser.role === 'admin'" in js and "syncAdminNav" in js)
    check("syncAdminNav actually runs on session start", js.count("syncAdminNav") >= 2)
    for f in ("rep-new-name", "rep-new-email", "rep-new-pass",
              "rep-new-code", "rep-new-phone", "rep-new-team"):
        check(f"add form has {f}", f'id="{f}"' in html)
    check("rep table container exists", 'id="reps-table"' in html)
    for fn in ("loadReps", "createRep", "editRep", "resetRepPassword", "toggleRep"):
        check(f"{fn}() defined", f"function {fn}(" in js)
    check("the list view calls the admin API", "'/api/admin/reps'" in js)
    check("no delete action in the UI",
          "method:'DELETE'" not in js.replace(" ", "") or "/api/admin/reps" not in js.split("method:'DELETE'")[0][-120:])
    check("the UI never sends a role", '"role":' not in js.split("function createRep(")[1][:1200])
    check("deactivation warns that history is kept",
          "history are kept" in js or "history is kept" in js)
    check("the admin is told the password is not emailed",
          "not emailed" in js)

    print(f"\n{'='*72}\nADMIN REP MANAGEMENT - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
