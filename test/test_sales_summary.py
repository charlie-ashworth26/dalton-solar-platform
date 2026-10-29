"""
The Sales Summary panel: completed sales only, counted in the business day.

WHAT THIS SUITE IS REALLY DEFENDING

  1. THE DEFINITION. "Completed" is not a new idea invented for a dashboard.
     It is perch_workflow.TERMINAL_STEP_KEYS - the one terminal step UBS has,
     labelled "Complete" - and this suite asserts the panel reads THAT rather
     than a list of status strings that would drift the moment the workflow
     gains a step.

  2. THE DAY BOUNDARY. Completion times are stored in UTC. A sale closed at
     8pm in New York is already tomorrow in UTC, so counting on the stored
     string files it under the wrong day - and on the last of the month, the
     wrong month. The cases below are written at exactly those hours, because
     a test that only uses midday values passes whether or not the timezone
     work was done at all.

  3. RECONCILIATION. Utility counts must sum to the total, and each utility's
     programs must sum to that utility. Asserted on a deliberately awkward
     mix: several utilities, several programs inside one of them, and sales
     whose program was never explicitly recorded.

  4. THE EXISTING REPORT. The summary is additive. The Sales Report table and
     its CSV must answer exactly as they did before.

Run: python test/test_sales_summary.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py

os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, execute, query_one
import seed
from routes import admin_routes
from services.perch import workflow as perch_workflow
from services.perch.client import acceptance_timezone

ET = acceptance_timezone()


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


# ── Building enrollments at an exact business-clock moment ──────────────────
# The panel reads persisted state, so these write persisted state directly.
# Nothing here goes near enrollment creation, Perch, or any status transition.

_seq = [0]


def make_enrollment(utility, step_key, when_et=None, customer_type=None,
                    discarded=False, enrolled_type=None):
    """One enrollment at a known step, completed at a known Eastern moment."""
    _seq[0] += 1
    code = f"ENR-SUM-{_seq[0]:05d}"
    execute(
        "INSERT INTO enrollments (enrollment_code, status, utility_name, "
        "selected_customer_type, discarded_at) VALUES (?, ?, ?, ?, ?)",
        (code, "Draft", utility, customer_type,
         "2026-01-01 00:00:00" if discarded else None))
    eid = query_one("SELECT id FROM enrollments WHERE enrollment_code = ?",
                    (code,))["id"]
    if step_key:
        stored = None
        if when_et is not None:
            stored = when_et.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        execute(
            "INSERT INTO perch_workflow_state (enrollment_id, current_step_key, "
            "updated_at) VALUES (?, ?, COALESCE(?, datetime('now')))",
            (eid, step_key, stored))
    if enrolled_type:
        # What was actually sent to and accepted by Perch, recorded at the
        # moment of enrollment - the fallback when no explicit choice exists.
        import json
        execute(
            "INSERT INTO perch_api_calls (enrollment_id, operation, http_method, "
            "endpoint, request_json, status_code, api_mode) "
            "VALUES (?, 'create_enrollment', 'POST', '/enroll', ?, 200, 'mock')",
            (eid, json.dumps({"customer_type": enrolled_type})))
    return eid


def summary(c, headers):
    r = c.get("/api/admin/sales-summary", headers=headers)
    assert r.status_code == 200, r.data
    return r.get_json()


def main():
    init_db(reset=True)
    seed.seed()

    with app.test_client() as c:
        admin = login(c, "admin@daltonsolar.com", "AdminPass1!")
        rep = login(c, "charlie@daltonsolar.com", "RepPass1!")
        qa = login(c, "qa@daltonsolar.com", "QaPass1!")

        # ════════════════════════════════════════════════════════════════════
        section("1. THE DEFINITION COMES FROM THE WORKFLOW, NOT FROM THIS PANEL")
        # ════════════════════════════════════════════════════════════════════
        check("UBS has exactly one terminal step",
              perch_workflow.TERMINAL_STEP_KEYS == {"contracts_accepted"})
        check("  ...and it is the one labelled Complete",
              perch_workflow.step_label("contracts_accepted") == "Complete")
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "routes", "admin_routes.py"),
            encoding="utf-8").read()
        check("the panel reads that set rather than listing statuses itself",
              "perch_workflow.TERMINAL_STEP_KEYS" in src
              and "'contracts_accepted'" not in src.split("def _completed_sales_rows")[1][:900])
        check("it groups on the workflow timestamp, not when work started",
              "w.updated_at AS completed_at" in src
              and "e.created_at AS completed_at" not in src)

        # ════════════════════════════════════════════════════════════════════
        section("2. ZERO STATE")
        # ════════════════════════════════════════════════════════════════════
        s = summary(c, admin)
        check("all three ranges are present",
              sorted(s["ranges"]) == ["month", "today", "week"])
        check("today is zero", s["ranges"]["today"]["total"] == 0)
        check("  ...with nothing to break down",
              s["ranges"]["today"]["by_utility"] == []
              and s["ranges"]["today"]["by_program"] == [])
        check("  ...and no top utility or program is invented",
              s["ranges"]["today"]["top_utility"] is None
              and s["ranges"]["today"]["top_program"] is None)
        check("the window says which timezone it used",
              "New_York" in s["ranges"]["today"]["window"]["timezone"])

        # ════════════════════════════════════════════════════════════════════
        section("3. ONE SALE TODAY")
        # ════════════════════════════════════════════════════════════════════
        now_et = datetime.now(ET)
        noon_today = now_et.replace(hour=12, minute=0, second=0, microsecond=0)
        # Midday today, unless it is not yet midday - then one minute ago.
        today_moment = noon_today if noon_today <= now_et else now_et - timedelta(minutes=1)
        make_enrollment("national-grid-ny", "contracts_accepted", today_moment,
                        customer_type="Residential")
        s = summary(c, admin)
        check("today counts it", s["ranges"]["today"]["total"] == 1)
        check("  ...and so do this week and this month",
              s["ranges"]["week"]["total"] >= 1 and s["ranges"]["month"]["total"] >= 1)
        u = s["ranges"]["today"]["by_utility"][0]
        check(f"the utility is the display name, not the slug ({u['utility']})",
              u["utility"] == "National Grid NY")
        check("  ...with the whole share", u["count"] == 1 and u["share"] == 100.0)
        check("  ...and the program is the rep-facing label",
              u["programs"][0]["program"] == "Residential")
        check("one utility is active", s["ranges"]["today"]["utilities_active"] == 1)
        check("the top utility is that utility",
              s["ranges"]["today"]["top_utility"]["utility"] == "National Grid NY")

        # ════════════════════════════════════════════════════════════════════
        section("4. THE DAY BOUNDARY IS EASTERN, NOT UTC")
        # ════════════════════════════════════════════════════════════════════
        # 8pm Eastern yesterday is already TODAY in UTC. Counting the stored
        # string would pull this sale into today's number.
        yest_8pm = (now_et - timedelta(days=1)).replace(
            hour=20, minute=0, second=0, microsecond=0)
        make_enrollment("nyseg", "contracts_accepted", yest_8pm,
                        customer_type="LMI")
        s = summary(c, admin)
        check(f"a sale at 8pm Eastern yesterday "
              f"(stored as {yest_8pm.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC) "
              f"is NOT in today", s["ranges"]["today"]["total"] == 1)
        check("  ...and today still has only the one utility",
              s["ranges"]["today"]["utilities_active"] == 1)

        # 11pm Eastern TODAY is tomorrow in UTC, and must still be today.
        late_today = now_et.replace(hour=23, minute=0, second=0, microsecond=0)
        if late_today > now_et:
            # Before 11pm locally: the sale is "later today", which is exactly
            # the stored-UTC trap in the other direction. The half-open window
            # runs to tomorrow's midnight, so it belongs to today either way.
            pass
        make_enrollment("rochester-gas-electric", "contracts_accepted", late_today,
                        customer_type="LMI_IRA")
        s = summary(c, admin)
        check(f"a sale at 11pm Eastern today "
              f"(stored as {late_today.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC) "
              f"IS in today", s["ranges"]["today"]["total"] == 2)

        # ════════════════════════════════════════════════════════════════════
        section("5. ONLY COMPLETED SALES COUNT")
        # ════════════════════════════════════════════════════════════════════
        before = summary(c, admin)["ranges"]["month"]["total"]
        make_enrollment("national-grid-ny", None, None)                    # draft
        make_enrollment("national-grid-ny", "service_area", today_moment)  # capacity only
        make_enrollment("national-grid-ny", "contracts_review", today_moment)
        make_enrollment("national-grid-ny", "contracts_accept", today_moment)
        make_enrollment("national-grid-ny", "contracts_accept_uncertain", today_moment)
        make_enrollment("national-grid-ny", "enroll_outcome_uncertain", today_moment)
        make_enrollment("national-grid-ny", "lmi_not_qualified", today_moment)
        make_enrollment("national-grid-ny", "proof_docs", today_moment)
        after = summary(c, admin)["ranges"]["month"]["total"]
        check(f"eight non-sales changed nothing ({before} -> {after})",
              before == after)

        # A DISCARDED enrollment that did reach the terminal step is still not
        # a sale: it is a failed attempt, excluded exactly as every other
        # listing excludes it.
        make_enrollment("national-grid-ny", "contracts_accepted", today_moment,
                        customer_type="Residential", discarded=True)
        check("a discarded enrollment is excluded even at the terminal step",
              summary(c, admin)["ranges"]["month"]["total"] == after)

        # ════════════════════════════════════════════════════════════════════
        section("6. SEVERAL UTILITIES, SEVERAL PROGRAMS, AND THEY RECONCILE")
        # ════════════════════════════════════════════════════════════════════
        for _ in range(3):
            make_enrollment("national-grid-ny", "contracts_accepted", today_moment,
                            customer_type="LMI")
        make_enrollment("national-grid-ny", "contracts_accepted", today_moment,
                        customer_type="LMI_IRA")
        make_enrollment("dominion-energy-va", "contracts_accepted", today_moment,
                        customer_type="LMI")
        # No explicit choice: a single-program location resolved server-side,
        # and what Perch accepted was recorded on the enrollment call.
        make_enrollment("nyseg", "contracts_accepted", today_moment,
                        customer_type=None, enrolled_type="Residential")
        # Neither source: counted, named honestly, never folded into another.
        make_enrollment("nyseg", "contracts_accepted", today_moment,
                        customer_type=None)

        s = summary(c, admin)["ranges"]["today"]
        check(f"today now has {s['total']} completed sales", s["total"] == 9)
        check("utility counts sum to the total",
              sum(u["count"] for u in s["by_utility"]) == s["total"])
        check("program counts sum to the total",
              sum(p["count"] for p in s["by_program"]) == s["total"])
        for u in s["by_utility"]:
            check(f"  {u['utility']}: its programs sum to its own count "
                  f"({u['count']})",
                  sum(p["count"] for p in u["programs"]) == u["count"])
        ng = [u for u in s["by_utility"] if u["utility"] == "National Grid NY"][0]
        check("one utility carries several programs", len(ng["programs"]) >= 2)
        check("  ...with the rep-facing IRA label, never the wire value",
              any(p["program"] == "Residential LMI (IRA)" for p in ng["programs"])
              and not any("LMI_IRA" in p["program"] for p in ng["programs"]))
        names = {u["utility"] for u in s["by_utility"]}
        check(f"utilities are grouped dynamically from the data ({len(names)})",
              {"National Grid NY", "NYSEG", "Dominion Energy"} <= names)
        check("the utility list is ordered by count, highest first",
              [u["count"] for u in s["by_utility"]]
              == sorted((u["count"] for u in s["by_utility"]), reverse=True))

        nyseg = [u for u in s["by_utility"] if u["utility"] == "NYSEG"][0]
        progs = {p["program"]: p["count"] for p in nyseg["programs"]}
        check("a program resolved only from the enrollment call is named",
              progs.get("Residential") == 1)
        check("  ...and one with no record anywhere says exactly that",
              progs.get(admin_routes.UNRECORDED_PROGRAM) == 1)
        check("shares are percentages of the range total",
              abs(sum(u["share"] for u in s["by_utility"]) - 100.0) < 0.5)
        check("no enrollment is counted twice",
              sum(u["count"] for u in s["by_utility"]) == s["total"]
              and s["total"] == 9)

        # ════════════════════════════════════════════════════════════════════
        section("7. THE WEEK AND THE MONTH")
        # ════════════════════════════════════════════════════════════════════
        s = summary(c, admin)
        check("the week starts on Monday, and says so",
              s["week_starts"] == "monday")
        week_start = datetime.fromisoformat(s["ranges"]["week"]["window"]["start_local"])
        check(f"  ...so the window starts on a Monday ({week_start:%a %d %b})",
              week_start.weekday() == 0)
        check("  ...at midnight", (week_start.hour, week_start.minute) == (0, 0))
        month_start = datetime.fromisoformat(s["ranges"]["month"]["window"]["start_local"])
        check(f"the month window starts on the 1st ({month_start:%d %b})",
              month_start.day == 1 and (month_start.hour, month_start.minute) == (0, 0))
        check("today is never larger than the week",
              s["ranges"]["today"]["total"] <= s["ranges"]["week"]["total"])
        check("  ...and the week is never larger than the month",
              s["ranges"]["week"]["total"] <= s["ranges"]["month"]["total"])

        # A sale from the previous month is in none of the three.
        prev_month_end = now_et.replace(day=1, hour=12, minute=0, second=0,
                                        microsecond=0) - timedelta(days=1)
        make_enrollment("nyseg", "contracts_accepted", prev_month_end,
                        customer_type="Residential")
        s2 = summary(c, admin)
        check("last month's sale is in none of the three ranges",
              s2["ranges"]["month"]["total"] == s["ranges"]["month"]["total"])

        # ...but a sale earlier THIS month that is not this week counts once.
        month_start_noon = month_start.replace(hour=12)
        if month_start_noon < week_start:
            make_enrollment("nyseg", "contracts_accepted", month_start_noon,
                            customer_type="Residential")
            s3 = summary(c, admin)
            check("an earlier sale this month is in the month but not the week",
                  s3["ranges"]["month"]["total"] == s["ranges"]["month"]["total"] + 1
                  and s3["ranges"]["week"]["total"] == s["ranges"]["week"]["total"])
        else:
            check("the month began inside this week, so that case cannot arise "
                  "today - the month/week assertions above still hold", True)

        # ════════════════════════════════════════════════════════════════════
        section("8. ONLY AN ADMIN CAN SEE IT")
        # ════════════════════════════════════════════════════════════════════
        # The same boundary the Sales Report itself enforces: the route, not
        # the hidden nav link.
        check(f"a sales rep is refused "
              f"({c.get('/api/admin/sales-summary', headers=rep).status_code})",
              c.get("/api/admin/sales-summary", headers=rep).status_code == 403)
        check("a QA reviewer is refused",
              c.get("/api/admin/sales-summary", headers=qa).status_code == 403)
        check("an unauthenticated request is refused",
              c.get("/api/admin/sales-summary").status_code in (401, 403))
        check("an admin is allowed",
              c.get("/api/admin/sales-summary", headers=admin).status_code == 200)

        # ════════════════════════════════════════════════════════════════════
        section("9. THE EXISTING SALES REPORT IS UNTOUCHED")
        # ════════════════════════════════════════════════════════════════════
        r = c.get("/api/admin/sales-report", headers=admin)
        check(f"the table still answers ({r.status_code})", r.status_code == 200)
        body = r.get_json()
        check("  ...with its original columns, in order",
              body["columns"] == ["Agent Name", "Customer Name", "Enrollment #",
                                  "Customer Email", "Status", "Utility",
                                  "Added", "Last Modified"])
        check("  ...and it still lists EVERY enrollment, not just sales",
              body["count"] > summary(c, admin)["ranges"]["month"]["total"])
        csv_resp = c.get("/api/admin/sales-report.csv", headers=admin)
        check(f"the CSV export still downloads ({csv_resp.status_code})",
              csv_resp.status_code == 200
              and csv_resp.headers["Content-Disposition"].startswith("attachment"))
        check("  ...with one line per enrollment plus the header",
              len(csv_resp.get_data(as_text=True).strip().splitlines())
              == body["count"] + 1)

        # ════════════════════════════════════════════════════════════════════
        section("10. READING THE PANEL CHANGES NOTHING")
        # ════════════════════════════════════════════════════════════════════
        before_rows = query_one("SELECT COUNT(*) n FROM enrollments")["n"]
        before_state = query_one(
            "SELECT COUNT(*) n FROM perch_workflow_state "
            "WHERE current_step_key = 'contracts_accepted'")["n"]
        before_calls = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
        for _ in range(3):
            summary(c, admin)
        check("no enrollment was created, changed or removed",
              query_one("SELECT COUNT(*) n FROM enrollments")["n"] == before_rows)
        check("no workflow state moved",
              query_one("SELECT COUNT(*) n FROM perch_workflow_state "
                        "WHERE current_step_key = 'contracts_accepted'")["n"]
              == before_state)
        check("no partner call was made",
              query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"] == before_calls)

    print("\n" + "=" * 72)
    print("SALES SUMMARY - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
