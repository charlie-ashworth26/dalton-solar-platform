"""
Admin-only Sales Report: every enrollment on UBS with the rep who created it.

ATTRIBUTION comes from what UBS already stores -
    enrollments.sales_rep_id -> sales_reps.id -> users.full_name
the same chain Rep Management reads. There is no second attribution system,
nothing is typed by a rep, and the agent is never inferred from the customer.

    python test/test_sales_report.py
"""
import csv
import io
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-sales-report-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query, query_one, execute
init_db(reset=True)

from app import app
import seed
from auth import hash_password

EXPECTED_HEADERS = ["Agent Name", "Customer Name", "Enrollment #", "Customer Email",
                    "Status", "Utility", "Added", "Last Modified"]
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def login(c, email, password):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def main():
    seed.seed()
    c = app.test_client()
    admin = login(c, "admin@daltonsolar.com", "AdminPass1!")
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ── Two reps, each with their own enrollments ─────────────────────────
    charlie = query_one("SELECT sr.id AS rep_id, u.full_name FROM sales_reps sr "
                        "JOIN users u ON u.id = sr.user_id WHERE u.email = ?",
                        ("charlie@daltonsolar.com",))
    second_uid = execute("INSERT INTO users (email, password_hash, role, full_name) "
                         "VALUES (?,?,?,?)",
                         ("dana@daltonsolar.com", hash_password("RepPass1!"),
                          "sales_rep", "Dana Okafor")).lastrowid
    dana_rep_id = execute("INSERT INTO sales_reps (user_id, rep_code) VALUES (?,?)",
                          (second_uid, "REP-DANA")).lastrowid
    dana = login(c, "dana@daltonsolar.com", "RepPass1!")

    made = []

    def make(rep_id, user_id, first, last, email, utility, status, code):
        cust = execute("INSERT INTO customers (first_name, last_name, email) VALUES (?,?,?)",
                       (first, last, email)).lastrowid
        eid = execute(
            """INSERT INTO enrollments (enrollment_code, status, customer_id,
               sales_rep_id, utility_name, created_by_user_id, updated_by_user_id)
               VALUES (?,?,?,?,?,?,?)""",
            (code, status, cust, rep_id, utility, user_id, user_id)).lastrowid
        made.append({"id": eid, "code": code, "email": email, "status": status,
                     "utility": utility, "rep_id": rep_id,
                     "name": f"{first} {last}"})
        return eid

    with app.app_context():
        make(charlie["rep_id"], 2, "Tina", "Bell", "tina@example.com",
             "national-grid-ny", "Draft", "ENR-REPORT-001")
        make(charlie["rep_id"], 2, "Omar", "Diaz", "omar@example.com",
             "nyseg", "Signed", "ENR-REPORT-002")
        make(dana_rep_id, second_uid, "Priya", "Shah", "priya@example.com",
             "central-hudson-gas-electric", "Verified", "ENR-REPORT-003")

    # ═══════════════════════════════════════════════════════
    section("1. AN ADMIN CAN ACCESS THE SALES REPORT")
    r = c.get("/api/admin/sales-report", headers=admin)
    check("the report endpoint responds to an admin", r.status_code == 200)
    body = r.get_json()
    check("  ...with rows", isinstance(body.get("rows"), list) and body["rows"])
    check("  ...and a count matching the rows",
          body["count"] == len(body["rows"]))

    section("2. A NORMAL REP CANNOT")
    check("a sales_rep is refused the report",
          c.get("/api/admin/sales-report", headers=rep).status_code == 403)
    check("  ...explicitly 403, not an empty 200",
          c.get("/api/admin/sales-report", headers=rep).get_json().get("rows") is None)
    check("a second rep is refused too",
          c.get("/api/admin/sales-report", headers=dana).status_code == 403)
    check("an unauthenticated caller is refused",
          c.get("/api/admin/sales-report").status_code == 401)
    for other, pw in (("qa@daltonsolar.com", "QaPass1!"),
                      ("developer@perchenergy.com", "DevPass1!")):
        h = login(c, other, pw)
        check(f"{other.split('@')[0]} is refused",
              c.get("/api/admin/sales-report", headers=h).status_code == 403)

    section("  ...and the URL is enforced on the SERVER, not by hiding the link")
    check("the route carries @require_role('admin')",
          '@require_role("admin")' in open(
              os.path.join(ROOT, "routes", "admin_routes.py"), encoding="utf-8").read())
    check("the nav entry is hidden by default in the markup",
          'id="nav-sales-report"' in HTML and 'style="display:none;"' in
          HTML.split('id="nav-sales-report"')[1][:120])
    check("  ...and revealed only for role 'admin'",
          "currentUser.role === 'admin'" in JS
          and "nav-sales-report" in JS)

    # ═══════════════════════════════════════════════════════
    section("3. THE REPORT SPANS MULTIPLE REPS")
    rows = c.get("/api/admin/sales-report", headers=admin).get_json()["rows"]
    by_code = {r["enrollment_code"]: r for r in rows}
    for m in made:
        check(f"{m['code']} is present", m["code"] in by_code)
    agents = {by_code[m["code"]]["agent_name"] for m in made}
    check("more than one agent appears", len(agents) >= 2)
    check("  ...including both reps' names",
          {"Charlie Mren", "Dana Okafor"} <= agents or
          {charlie["full_name"], "Dana Okafor"} <= agents)

    section("4. AGENT NAME IS THE ENROLLMENT'S ACTUAL OWNER")
    for m in made:
        owner = query_one(
            """SELECT u.full_name FROM enrollments e
                 JOIN sales_reps sr ON sr.id = e.sales_rep_id
                 JOIN users u ON u.id = sr.user_id WHERE e.id = ?""", (m["id"],))
        check(f"{m['code']} -> {owner['full_name']!r}",
              by_code[m["code"]]["agent_name"] == owner["full_name"])
    check("agent is NOT taken from the customer",
          all(by_code[m["code"]]["agent_name"] != by_code[m["code"]]["customer_name"]
              for m in made))

    section("  ...an enrollment with no rep says so rather than guessing")
    with app.app_context():
        orphan_cust = execute("INSERT INTO customers (first_name, last_name, email) "
                              "VALUES (?,?,?)", ("Noah", "Reed", "noah@example.com")).lastrowid
        execute("""INSERT INTO enrollments (enrollment_code, status, customer_id,
                   sales_rep_id, created_by_user_id, updated_by_user_id)
                   VALUES (?,?,?,NULL,1,1)""",
                ("ENR-REPORT-NOREP", "Draft", orphan_cust))
    rows2 = c.get("/api/admin/sales-report", headers=admin).get_json()["rows"]
    norep = next(r for r in rows2 if r["enrollment_code"] == "ENR-REPORT-NOREP")
    check("it is still listed", norep is not None)
    check("  ...as 'Unassigned', not a borrowed name",
          norep["agent_name"] == "Unassigned")

    # ═══════════════════════════════════════════════════════
    section("5. THE CSV HEADERS ARE EXACTLY THE SPECIFIED COLUMNS")
    v = c.get("/api/admin/sales-report.csv", headers=admin)
    check("the export responds to an admin", v.status_code == 200)
    check("  ...as text/csv", "text/csv" in (v.headers.get("Content-Type") or ""))
    parsed = list(csv.reader(io.StringIO(v.get_data(as_text=True))))
    check("the header row matches, in order", parsed[0] == EXPECTED_HEADERS)
    check("  ...and the JSON advertises the same columns",
          c.get("/api/admin/sales-report", headers=admin).get_json()["columns"]
          == EXPECTED_HEADERS)

    section("  ...and the filename is UBS_Sales_Report_YYYY-MM-DD.csv")
    disposition = v.headers.get("Content-Disposition") or ""
    expected_name = f"UBS_Sales_Report_{date.today().isoformat()}.csv"
    check(f"disposition names {expected_name}", expected_name in disposition)
    check("  ...as an attachment", "attachment" in disposition)

    # ═══════════════════════════════════════════════════════
    section("6. THE CSV HOLDS EVERY ENROLLMENT, NOT A PAGE")
    with app.app_context():
        live = query("SELECT enrollment_code FROM enrollments WHERE discarded_at IS NULL")
    codes_in_csv = {r[2] for r in parsed[1:] if r}
    check(f"the DB has {len(live)} live enrollments", len(live) > 3)
    check("  ...and the CSV has a row for every one",
          {r["enrollment_code"] for r in live} == codes_in_csv)
    check("  ...row count matches exactly", len(parsed) - 1 == len(live))
    check("the CSV and the table agree",
          codes_in_csv == {r["enrollment_code"] for r in rows2})

    section("  ...proven at a size no default page would return")
    with app.app_context():
        for i in range(120):
            cu = execute("INSERT INTO customers (first_name, last_name, email) "
                         "VALUES (?,?,?)", ("Bulk", f"Row{i}", f"bulk{i}@example.com")).lastrowid
            execute("""INSERT INTO enrollments (enrollment_code, status, customer_id,
                       sales_rep_id, created_by_user_id, updated_by_user_id)
                       VALUES (?,?,?,?,1,1)""",
                    (f"ENR-BULK-{i:03d}", "Draft", cu, charlie["rep_id"]))
        total = query_one("SELECT COUNT(*) n FROM enrollments WHERE discarded_at IS NULL")["n"]
    big = list(csv.reader(io.StringIO(
        c.get("/api/admin/sales-report.csv", headers=admin).get_data(as_text=True))))
    check(f"with {total} enrollments the CSV still returns them all",
          len(big) - 1 == total)
    check("  ...well past any 25/50/100 page size", total > 100)
    check("  ...and the JSON table does too",
          c.get("/api/admin/sales-report", headers=admin).get_json()["count"] == total)

    section("  ...it is generated server-side, not from the rendered table")
    check("the frontend fetches the CSV from the backend",
          "/api/admin/sales-report.csv" in JS)
    check("  ...rather than serialising DOM rows",
          "sales-report-table" not in JS.split("async function exportSalesReport")[1][:1200])

    # ═══════════════════════════════════════════════════════
    section("7. EVERY FIELD IS CORRECTLY POPULATED")
    rows3 = c.get("/api/admin/sales-report", headers=admin).get_json()["rows"]
    by_code3 = {r["enrollment_code"]: r for r in rows3}
    csv_by_code = {r[2]: r for r in big[1:] if r}
    for m in made:
        row = by_code3[m["code"]]
        src = query_one("SELECT * FROM enrollments WHERE id = ?", (m["id"],))
        cust = query_one("SELECT * FROM customers WHERE id = ?", (src["customer_id"],))
        check(f"{m['code']}: customer name", row["customer_name"] == m["name"])
        check(f"{m['code']}: enrollment #", row["enrollment_code"] == src["enrollment_code"])
        check(f"{m['code']}: email", row["customer_email"] == cust["email"])
        check(f"{m['code']}: status is populated",
              bool(row["status"]) and row["status"] != "None")
        check(f"{m['code']}: utility is resolved from the stored slug",
              bool(row["utility"]))
        check(f"{m['code']}: added == created_at", row["created_at"] == src["created_at"])
        check(f"{m['code']}: modified == updated_at", row["updated_at"] == src["updated_at"])
        # The CSV carries the identical values in the specified column order.
        crow = csv_by_code[m["code"]]
        check(f"{m['code']}: CSV agent matches JSON", crow[0] == row["agent_name"])
        check(f"{m['code']}: CSV customer matches", crow[1] == row["customer_name"])
        check(f"{m['code']}: CSV email matches", crow[3] == row["customer_email"])
        check(f"{m['code']}: CSV status matches", crow[4] == row["status"])
        check(f"{m['code']}: CSV utility matches", crow[5] == row["utility"])
        check(f"{m['code']}: CSV added matches", crow[6] == row["created_at"])
        check(f"{m['code']}: CSV modified matches", crow[7] == row["updated_at"])

    section("  ...the utility is a display name, not a raw slug")
    ng = by_code3["ENR-REPORT-001"]
    check("national-grid-ny renders as a readable name",
          ng["utility"] and ng["utility"] != "national-grid-ny")

    # ═══════════════════════════════════════════════════════
    section("8. THE EXPORT ENDPOINT REJECTS NON-ADMINS INDEPENDENTLY")
    check("a sales_rep is refused the CSV",
          c.get("/api/admin/sales-report.csv", headers=rep).status_code == 403)
    check("  ...and gets no CSV body",
          "Agent Name" not in c.get("/api/admin/sales-report.csv",
                                    headers=rep).get_data(as_text=True))
    check("a second rep is refused",
          c.get("/api/admin/sales-report.csv", headers=dana).status_code == 403)
    check("an unauthenticated caller is refused",
          c.get("/api/admin/sales-report.csv").status_code == 401)
    for other, pw in (("qa@daltonsolar.com", "QaPass1!"),
                      ("developer@perchenergy.com", "DevPass1!")):
        h = login(c, other, pw)
        check(f"{other.split('@')[0]} is refused the CSV",
              c.get("/api/admin/sales-report.csv", headers=h).status_code == 403)
    check("the two endpoints are guarded separately",
          open(os.path.join(ROOT, "routes", "admin_routes.py"), encoding="utf-8")
          .read().count('@require_role("admin")') >= 2)

    # ═══════════════════════════════════════════════════════
    section("EXISTING REP VISIBILITY AND OWNERSHIP ARE UNCHANGED")
    rep_list = c.get("/api/enrollments", headers=rep).get_json()
    rep_ids = {e["id"] for e in rep_list}
    dana_ids = {e["id"] for e in c.get("/api/enrollments", headers=dana).get_json()}
    check("Charlie still sees only his own enrollments",
          all(query_one("SELECT sales_rep_id FROM enrollments WHERE id=?",
                        (i,))["sales_rep_id"] == charlie["rep_id"] for i in rep_ids))
    check("  ...and not Dana's", not (rep_ids & dana_ids))
    check("Dana sees only hers",
          all(query_one("SELECT sales_rep_id FROM enrollments WHERE id=?",
                        (i,))["sales_rep_id"] == dana_rep_id for i in dana_ids))
    check("the report is strictly wider than either rep's view",
          len(rows3) > len(rep_ids) and len(rows3) > len(dana_ids))
    check("no ownership column was altered by the report",
          query_one("SELECT COUNT(*) n FROM enrollments WHERE sales_rep_id IS NULL")["n"] == 1)

    section("  ...and discarded enrollments stay out, as everywhere else")
    with app.app_context():
        dcu = execute("INSERT INTO customers (first_name,last_name,email) VALUES (?,?,?)",
                      ("Gone", "Away", "gone@example.com")).lastrowid
        execute("""INSERT INTO enrollments (enrollment_code, status, customer_id,
                   sales_rep_id, created_by_user_id, updated_by_user_id,
                   discarded_at, discarded_reason)
                   VALUES (?,?,?,?,1,1,datetime('now'),'test')""",
                ("ENR-DISCARDED", "Draft", dcu, charlie["rep_id"]))
    after = c.get("/api/admin/sales-report", headers=admin).get_json()["rows"]
    check("a discarded enrollment is absent from the report",
          "ENR-DISCARDED" not in {r["enrollment_code"] for r in after})
    csv_after = c.get("/api/admin/sales-report.csv", headers=admin).get_data(as_text=True)
    check("  ...and from the CSV", "ENR-DISCARDED" not in csv_after)

    section("THE NAV ENTRY MATCHES THE EXISTING MANAGE PATTERN")
    check("it is a fourth Manage tab", 'data-view="sales-report"' in HTML)
    check("  ...labelled 'Sales Report'", ">\n      Sales Report" in HTML
          or "Sales Report\n    </div>" in HTML or "Sales Report" in HTML)
    check("  ...alongside Dashboard, Customers and Reps",
          all(f'data-view="{v}"' in HTML for v in ("dashboard", "customers", "reps")))
    check("the view exists", 'id="view-sales-report"' in HTML)
    check("  ...with an Export CSV button at the top",
          'id="sales-report-export"' in HTML and "Export CSV" in HTML)
    check("showView loads it", "if(name === 'sales-report') loadSalesReport();" in JS)

    print(f"\n{'='*72}\nSALES REPORT - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
