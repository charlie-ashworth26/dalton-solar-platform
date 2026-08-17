"""
Rep identity + enrollment ownership.

BEFORE THIS MILESTONE: eleven enrollment-scoped routes had role gating but NO
ownership check, so any sales_rep could act on any enrollment by changing the id
in the URL. Demonstrated during the audit: Rep A PATCHed Rep B's enrollment and
overwrote the customer's name.

Run: python test/test_enrollment_ownership.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from auth import hash_password

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


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


def make_rep(email, name, code):
    with app.app_context():
        uid = execute("INSERT INTO users (email, password_hash, role, full_name) "
                      "VALUES (?,?,?,?)",
                      (email, hash_password("RepPass1!"), "sales_rep", name)).lastrowid
        execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)", (uid, code))
        return uid


def ops(c, headers, eid, marker="Zed"):
    """Every enrollment-scoped operation, as (label, callable).
    One list drives both the allowed and the denied assertions.

    `marker` distinguishes a legitimate owner's write from an attacker's, so the
    side-effect assertions can tell them apart. (An earlier version reused one
    name for both, and the owner's own successful update made the attacker look
    successful.)"""
    return [
        ("read",              lambda: c.get(f"/api/enrollments/{eid}", headers=headers)),
        ("update",            lambda: c.patch(f"/api/enrollments/{eid}", headers=headers,
                                              json={"customer": {"first_name": marker,
                                                                 "last_name": "Q",
                                                                 "email": "z@example.com"}})),
        ("status-change",     lambda: c.post(f"/api/enrollments/{eid}/status", headers=headers,
                                             json={"new_status": "Information Needed"})),
        ("lmi",               lambda: c.post(f"/api/enrollments/{eid}/lmi", headers=headers,
                                             json={"path": "not_applicable"})),
        ("upload",            lambda: c.post(f"/api/enrollments/{eid}/documents", headers=headers,
                                             data={"category": "utility_bill",
                                                   "file": (io.BytesIO(PNG), "x.png")},
                                             content_type="multipart/form-data")),
        ("correct",           lambda: c.post(f"/api/enrollments/{eid}/documents/1/correct",
                                             headers=headers, json={"corrected_fields": {}})),
        ("document-set",      lambda: c.post(f"/api/enrollments/{eid}/document-sets",
                                             headers=headers,
                                             data={"category": "utility_bill",
                                                   "files": [(io.BytesIO(PNG), "y.png")]},
                                             content_type="multipart/form-data")),
        ("document-set read", lambda: c.get(f"/api/enrollments/{eid}/document-sets/1",
                                            headers=headers)),
        ("generate-agreements", lambda: c.post(f"/api/enrollments/{eid}/agreements/generate",
                                               headers=headers, json={})),
        ("signing-session",   lambda: c.post(f"/api/enrollments/{eid}/signing-session",
                                             headers=headers, json={})),
        ("submit",            lambda: c.post(f"/api/enrollments/{eid}/submit", headers=headers,
                                             json={})),
        ("package",           lambda: c.get(f"/api/enrollments/{eid}/package", headers=headers)),
        ("resume/workflow",   lambda: c.get(f"/api/perch/enrollments/{eid}/workflow",
                                            headers=headers)),
    ]


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()

    make_rep("repa@daltonsolar.com", "Rep A", "REP-A")
    make_rep("repb@daltonsolar.com", "Rep B", "REP-B")
    A = login(c, "repa@daltonsolar.com", "RepPass1!")
    B = login(c, "repb@daltonsolar.com", "RepPass1!")
    ADMIN = login(c, "admin@daltonsolar.com", "AdminPass1!")
    QA = login(c, "qa@daltonsolar.com", "QaPass1!")
    DEV = login(c, "developer@perchenergy.com", "DevPass1!")

    eid_a = c.post("/api/perch/drafts", headers=A).get_json()["enrollment_id"]
    eid_b = c.post("/api/perch/drafts", headers=B).get_json()["enrollment_id"]
    check("Rep A and Rep B own different enrollments", eid_a != eid_b)

    # ═══════════════════════════════════════════════════════
    section("OWNERSHIP IS STAMPED FROM THE AUTHENTICATED SESSION")
    with app.app_context():
        row_a = query_one("SELECT * FROM enrollments WHERE id = ?", (eid_a,))
        rep_a = query_one("SELECT sr.* FROM sales_reps sr JOIN users u ON u.id = sr.user_id "
                          "WHERE u.email = ?", ("repa@daltonsolar.com",))
        user_a = query_one("SELECT * FROM users WHERE email = ?", ("repa@daltonsolar.com",))
    check("sales_rep_id is the creating rep", row_a["sales_rep_id"] == rep_a["id"])
    check("created_by_user_id is the authenticated user",
          row_a["created_by_user_id"] == user_a["id"])
    check("updated_by_user_id is set", row_a["updated_by_user_id"] == user_a["id"])
    check("sales_rep_id references sales_reps.id, NOT users.id",
          row_a["sales_rep_id"] != user_a["id"] or rep_a["id"] == user_a["id"])

    section("FRONTEND CANNOT SPOOF OWNERSHIP")
    spoof = c.post("/api/perch/drafts", headers=A,
                   json={"sales_rep_id": 9999, "created_by_user_id": 9999,
                         "assigned_rep_user_id": 9999, "updated_by_user_id": 9999})
    sid = spoof.get_json()["enrollment_id"]
    with app.app_context():
        srow = query_one("SELECT * FROM enrollments WHERE id = ?", (sid,))
    check("spoofed sales_rep_id ignored", srow["sales_rep_id"] == rep_a["id"])
    check("spoofed created_by_user_id ignored", srow["created_by_user_id"] == user_a["id"])
    check("spoofed updated_by_user_id ignored", srow["updated_by_user_id"] == user_a["id"])
    # And it cannot be changed after the fact through the update route.
    c.patch(f"/api/enrollments/{eid_a}", headers=A,
            json={"sales_rep_id": 9999, "created_by_user_id": 9999,
                  "customer": {"first_name": "Ann", "last_name": "A", "email": "a@example.com"}})
    with app.app_context():
        after = query_one("SELECT * FROM enrollments WHERE id = ?", (eid_a,))
    check("PATCH cannot reassign sales_rep_id", after["sales_rep_id"] == rep_a["id"])
    check("PATCH cannot rewrite created_by_user_id (immutable)",
          after["created_by_user_id"] == user_a["id"])

    # ═══════════════════════════════════════════════════════
    section("REP A CAN FULLY OPERATE ON ENROLLMENT A")
    for label, fn in ops(c, A, eid_a):
        code = fn().status_code
        check(f"A -> A  {label:20} not denied (got {code})", code not in (401, 403))

    section("REP B CAN FULLY OPERATE ON ENROLLMENT B")
    for label, fn in ops(c, B, eid_b):
        code = fn().status_code
        check(f"B -> B  {label:20} not denied (got {code})", code not in (401, 403))

    section("REP A CANNOT TOUCH ENROLLMENT B  (every route -> 403)")
    for label, fn in ops(c, A, eid_b, marker="ATTACKER_A"):
        check(f"A -> B  {label:20} DENIED", fn().status_code == 403)

    section("REP B CANNOT TOUCH ENROLLMENT A  (every route -> 403)")
    for label, fn in ops(c, B, eid_a, marker="ATTACKER_B"):
        check(f"B -> A  {label:20} DENIED", fn().status_code == 403)

    section("DENIAL IS REAL — no side effects leaked through")
    with app.app_context():
        cust_b = query_one("SELECT c.first_name FROM customers c "
                           "JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?", (eid_b,))
        cust_a = query_one("SELECT c.first_name FROM customers c "
                           "JOIN enrollments e ON e.customer_id = c.id WHERE e.id = ?", (eid_a,))
        # Rep B legitimately uploaded to B, so count only documents attributable
        # to Rep A - that is what a successful attack would have created.
        foreign_b = query_one(
            "SELECT COUNT(*) n FROM documents WHERE enrollment_id = ? "
            "AND uploaded_by_user_id = ?", (eid_b, user_a["id"]))
        foreign_a = query_one(
            "SELECT COUNT(*) n FROM documents d JOIN users u ON u.id = d.uploaded_by_user_id "
            "WHERE d.enrollment_id = ? AND u.email = ?", (eid_a, "repb@daltonsolar.com"))
    check("Rep A's PATCH did not modify Rep B's customer",
          cust_b is None or cust_b["first_name"] != "ATTACKER_A")
    check("Rep B's PATCH did not modify Rep A's customer",
          cust_a is None or cust_a["first_name"] != "ATTACKER_B")
    check("no document uploaded by Rep A exists in Rep B's enrollment",
          foreign_b["n"] == 0)
    check("no document uploaded by Rep B exists in Rep A's enrollment",
          foreign_a["n"] == 0)

    # ═══════════════════════════════════════════════════════
    section("PRIVILEGED ROLES RETAIN GLOBAL ACCESS (unchanged)")
    for name, h in (("admin", ADMIN), ("qa_reviewer", QA), ("developer", DEV)):
        check(f"{name:12} reads enrollment A",
              c.get(f"/api/enrollments/{eid_a}", headers=h).status_code == 200)
        check(f"{name:12} reads enrollment B",
              c.get(f"/api/enrollments/{eid_b}", headers=h).status_code == 200)
        check(f"{name:12} resumes A (workflow)",
              c.get(f"/api/perch/enrollments/{eid_a}/workflow", headers=h).status_code == 200)
        check(f"{name:12} is not blocked on B's document set",
              c.get(f"/api/enrollments/{eid_b}/document-sets/1",
                    headers=h).status_code != 403)

    section("DASHBOARD LIST SCOPING (unchanged)")
    ids_a = {e["id"] for e in c.get("/api/enrollments", headers=A).get_json()}
    ids_b = {e["id"] for e in c.get("/api/enrollments", headers=B).get_json()}
    check("Rep A sees A", eid_a in ids_a)
    check("Rep A does NOT see B", eid_b not in ids_a)
    check("Rep B sees B", eid_b in ids_b)
    check("Rep B does NOT see A", eid_a not in ids_b)
    for name, h in (("admin", ADMIN), ("qa_reviewer", QA), ("developer", DEV)):
        ids = {e["id"] for e in c.get("/api/enrollments", headers=h).get_json()}
        check(f"{name:12} list includes both", {eid_a, eid_b} <= ids)

    section("HISTORICAL OWNERSHIP SURVIVES DEACTIVATION")
    with app.app_context():
        execute("UPDATE users SET is_active = 0 WHERE email = ?", ("repa@daltonsolar.com",))
        still = query_one("SELECT * FROM enrollments WHERE id = ?", (eid_a,))
    check("sales_rep_id preserved after the rep is deactivated",
          still["sales_rep_id"] == rep_a["id"])
    check("created_by_user_id preserved", still["created_by_user_id"] == user_a["id"])
    check("the deactivated rep can no longer log in",
          c.post("/api/auth/login", json={"email": "repa@daltonsolar.com",
                                          "password": "RepPass1!"}).status_code != 200)
    check("admin can still access the historical enrollment",
          c.get(f"/api/enrollments/{eid_a}", headers=ADMIN).status_code == 200)
    with app.app_context():
        execute("UPDATE users SET is_active = 1 WHERE email = ?", ("repa@daltonsolar.com",))

    section("A REP WITH NO sales_reps ROW OWNS NOTHING")
    with app.app_context():
        execute("INSERT INTO users (email, password_hash, role, full_name) VALUES (?,?,?,?)",
                ("orphan@daltonsolar.com", hash_password("RepPass1!"), "sales_rep", "Orphan"))
    ORPH = login(c, "orphan@daltonsolar.com", "RepPass1!")
    check("sees no enrollments", c.get("/api/enrollments", headers=ORPH).get_json() == [])
    check("cannot read A", c.get(f"/api/enrollments/{eid_a}", headers=ORPH).status_code == 403)
    check("cannot update A",
          c.patch(f"/api/enrollments/{eid_a}", headers=ORPH,
                  json={"customer": {"first_name": "X", "last_name": "Y",
                                     "email": "x@y.com"}}).status_code == 403)

    section("UNAUTHENTICATED ACCESS")
    for label, fn in ops(c, {}, eid_a):
        check(f"anon {label:20} rejected", fn().status_code == 401)

    section("NONEXISTENT ENROLLMENT -> 404, NOT 403")
    check("admin gets 404 for a missing enrollment",
          c.get("/api/enrollments/999999", headers=ADMIN).status_code == 404)
    check("rep gets 404 for a missing enrollment (no existence probing via 403)",
          c.get("/api/enrollments/999999", headers=A).status_code == 404)

    section("HELPER BEHAVIOUR")
    from services import authz
    check("global-access roles are admin, qa_reviewer, developer",
          set(authz.GLOBAL_ACCESS_ROLES) == {"admin", "qa_reviewer", "developer"})
    with app.test_request_context():
        from flask import g as _g
        _g.current_user = {"id": user_a["id"], "role": "sales_rep"}
        check("owner can access", authz.can_access_enrollment(row_a) is True)
        _g.current_user = {"id": 99999, "role": "sales_rep"}
        check("non-owner rep denied", authz.can_access_enrollment(row_a) is False)
        for role in ("admin", "qa_reviewer", "developer"):
            _g.current_user = {"id": 99999, "role": role}
            check(f"{role} allowed", authz.can_access_enrollment(row_a) is True)
        _g.current_user = {"id": 99999, "role": "some_future_role"}
        check("an unknown role is DENIED by default",
              authz.can_access_enrollment(row_a) is False)
        _g.current_user = {"id": user_a["id"], "role": "sales_rep"}
        check("None enrollment is denied", authz.can_access_enrollment(None) is False)

    section("EVERY ENROLLMENT-SCOPED ROUTE IS GUARDED")
    import re
    unguarded = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "routes"))):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, "routes", fn), encoding="utf-8").read().replace("\r\n", "\n")
        for m in re.finditer(r"@bp\.route\((.*?)\)\n((?:@\w+[^\n]*\n)*)def (\w+)", src):
            route, name = m.group(1), m.group(3)
            if "enrollment_id" not in route:
                continue
            nxt = src.find("@bp.route", m.end())
            body = src[m.end():nxt if nxt != -1 else len(src)]
            guarded = any(k in body for k in
                          ("visible_enrollment(", "_visible(", "_visible_to_actor(",
                           "_document_visible_to_user(", "sales_reps"))
            if not guarded:
                unguarded.append(f"{fn}::{name}")
    check(f"no enrollment-scoped route lacks an ownership check (found {unguarded})",
          not unguarded)

    print(f"\n{'='*72}\nENROLLMENT OWNERSHIP - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
