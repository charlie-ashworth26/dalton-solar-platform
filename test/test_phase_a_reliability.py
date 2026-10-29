"""
UBS 2.0 PHASE A - reliability fixes.

Three defects, one of which had three symptoms:

  1. ENROLLMENT HYDRATION. The email a rep types at Availability is persisted as
     enrollments.perch_token_email; customers.email is written much later, by the
     merged Continue on the Customer & Bill step. The detail payload serialized
     only the second, so an enrollment abandoned between the two reopened with no
     email - and the guard on the later step told the rep to "Re-run the
     availability check before continuing." on a screen with no way to do it.
     The same payload withheld the raw utility slug and the capacity ZIP, and the
     frontend never restored the service address STATE, so reopening a Virginia
     enrollment re-saved it as New York.

  2. ADMIN NAVIGATION. syncAdminNav() ran only on login. #nav-reps and
     #nav-sales-report are display:none in the markup, so a refresh left an admin
     with no admin nav until they logged out and back in.

  3. SALES REPORT DATE FILTER. One `where` string is reused by four queries in
     /api/reports/summary, the last of which joins signing_sessions to
     enrollments. `created_at` exists on both tables, so an unqualified
     `created_at >= ?` made that query ambiguous: 500 on any date filter, while
     every other filter worked.

Runtime proof of the frontend half lives in test/phase_a_state_harness.js. The
checks here cover the backend and guard the structure the harness exercises.

    python test/test_phase_a_reliability.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-phase-a-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query, query_one, execute

init_db(reset=True)

from app import app
import seed
from services.perch import utilities as _utilities
ADAPTER = open(os.path.join(ROOT, 'services', 'perch', 'adapter.py'),
               encoding='utf-8').read()
VA_SLUG = "dominion-energy-va"

JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
REPORTS = open(os.path.join(ROOT, "routes", "report_routes.py"), encoding="utf-8").read()

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def login(c, email, password):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.data
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


# Collapsed whitespace with whole-line // comments dropped, so these assertions
# test the code and not its formatting. A comment between a condition and its
# body must not fail a behavioural check.
JSN = " ".join(" ".join(
    ln for ln in JS.splitlines() if not ln.strip().startswith("//")
).split())


def main():
    seed.seed()
    c = app.test_client()
    admin = login(c, "admin@daltonsolar.com", "AdminPass1!")
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════════════════
    section("1. SALES REPORT DATE FILTER NO LONGER 500s")

    # Two enrollments either side of the filter window, plus a signing session
    # on one of them so the join that caused the 500 is actually exercised.
    rep_row = query_one("SELECT id FROM sales_reps LIMIT 1")
    rep_id = rep_row["id"] if rep_row else None
    old_id = execute(
        "INSERT INTO enrollments (enrollment_code, status, sales_rep_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("PHA-OLD", "Draft", rep_id, "2020-03-04 10:00:00")).lastrowid
    new_id = execute(
        "INSERT INTO enrollments (enrollment_code, status, sales_rep_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("PHA-NEW", "Draft", rep_id, "2026-03-04 10:00:00")).lastrowid
    # created_at on the SIGNING SESSION is deliberately outside the window the
    # test filters on, so a query that filtered the wrong table's column would
    # give a different answer and fail here.
    execute("INSERT INTO signing_sessions "
            "(enrollment_id, token, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id, "pha-token-1", "active", "2019-01-01 00:00:00",
             "2030-01-01 00:00:00"))

    cases = [
        ("", 200), ("?date_from=2026-01-01", 200), ("?date_to=2026-12-31", 200),
        ("?date_from=2026-01-01&date_to=2026-12-31", 200),
        ("?sales_rep_id=%s" % rep_id, 200), ("?lmi=true", 200), ("?lmi=false", 200),
        ("?date_from=2026-01-01&date_to=2026-12-31&lmi=false", 200),
        ("?date_from=2030-01-01", 200),
        ("?date_from=not-a-date", 200),
    ]
    for qs, expected in cases:
        r = c.get("/api/reports/summary" + qs, headers=admin)
        check(f"GET /api/reports/summary{qs or ' (no filters)'} -> {r.status_code}",
              r.status_code == expected)

    # It must FILTER, not merely survive. This is the part a bare "no longer
    # 500s" assertion would miss.
    allr = c.get("/api/reports/summary", headers=admin).get_json()
    window = c.get("/api/reports/summary?date_from=2026-01-01&date_to=2026-12-31",
                   headers=admin).get_json()
    before = c.get("/api/reports/summary?date_to=2021-01-01", headers=admin).get_json()
    check(f"unfiltered counts both enrollments ({allr['enrollments_started']})",
          allr["enrollments_started"] >= 2)
    check(f"the 2026 window counts only the 2026 one ({window['enrollments_started']})",
          window["enrollments_started"] == 1)
    check(f"a pre-2021 window counts only the 2020 one "
          f"({before['enrollments_started']})",
          before["enrollments_started"] == 1)
    check("the two windows do not overlap",
          window["enrollments_started"] + before["enrollments_started"]
          <= allr["enrollments_started"])

    # The join is filtered by the ENROLLMENT's date, not the signing session's.
    check(f"the signing-link join filters on the enrollment date "
          f"({window['signature_links_created']})",
          window["signature_links_created"] == 1)
    check("  ...and excludes it outside that window",
          before["signature_links_created"] == 0)

    check("every filter column is table-qualified in the shared clause",
          'filters_sql = ["enrollments.discarded_at IS NULL"]' in REPORTS
          and 'enrollments.created_at >= ?' in REPORTS
          and 'enrollments.created_at <= ?' in REPORTS)
    check("  ...and no bare created_at filter remains",
          '"created_at >= ?"' not in REPORTS and '"created_at <= ?"' not in REPORTS)
    check("the join exposes the table name the clause qualifies with",
          "JOIN enrollments ON enrollments.id = signing_sessions.enrollment_id"
          in REPORTS)
    check("  ...so no alias shadows it", "JOIN enrollments e ON" not in REPORTS)
    check("the exception is fixed, not swallowed",
          "except Exception" not in REPORTS and "try:" not in REPORTS)

    execute("DELETE FROM signing_sessions WHERE enrollment_id = ?", (new_id,))
    execute("DELETE FROM enrollments WHERE id IN (?, ?)", (old_id, new_id))

    # ═══════════════════════════════════════════════════════════════════
    section("2. THE DETAIL PAYLOAD CARRIES THE AVAILABILITY STATE")

    r = c.post("/api/enrollments", json={}, headers=rep)
    check(f"a draft enrollment is created (HTTP {r.status_code})",
          r.status_code in (200, 201))
    eid = r.get_json()["id"]

    # Exactly the state Availability persists, and nothing the later steps write.
    execute("UPDATE enrollments SET perch_token_email=?, utility_name=?, service_zip=? "
            "WHERE id=?",
            ("avail@example.com", "dominion-energy-va", "22408", eid))

    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("perch_token_email is serialized",
          d.get("perch_token_email") == "avail@example.com")
    check("the RAW utility slug is serialized",
          d.get("utility_slug") == "dominion-energy-va")
    # KNOWN PHASE B DEFECT, pinned here so it is not mistaken for new.
    # routes/enrollment_routes.py::_utility_display() is a SECOND display-name
    # formatter: it looks the slug up in the local perch_utilities table and
    # returns the RAW SLUG for anything not in it. dominion-energy-va is not in
    # that NY table, so the dashboard renders "dominion-energy-va" - the ugly
    # label already reported. services/perch/utilities.display_name_for_slug()
    # already derives "Dominion Energy" generically; the fix is to delegate to
    # it, which is the "one consistent display-name system" of Phase B item 6
    # and deliberately NOT done in Phase A.
    #
    # This asserts TODAY's behaviour on purpose. When Phase B lands, this check
    # flips to "Dominion Energy" - and until then it stops the leak being
    # forgotten.
    # PHASE B landed: _utility_display() now delegates to the shared resolver,
    # so this flipped from the raw slug to the clean name exactly as the note
    # here said it would.
    check(f"  ...and the display name is clean, not the slug "
          f"({d.get('utility_name')})",
          d.get("utility_name") == "Dominion Energy")
    check("  ...matching the shared resolver, not a second formatter",
          d.get("utility_name")
          == _utilities.display_name_for_slug("dominion-energy-va"))
    check("the capacity ZIP is serialized", d.get("service_zip") == "22408")
    check("no customer row exists yet", d.get("customer") is None)
    check("  ...so this is exactly the state that used to dead-end",
          d.get("perch_token_email") and not d.get("customer"))

    # ═══════════════════════════════════════════════════════════════════
    section("3. PARTIAL CUSTOMER DATA PERSISTS AND REOPENS")

    # customers.first_name/last_name/email are all NOT NULL. A name-only
    # payload therefore cannot create the row - and that used to surface as an
    # opaque 500 from the constraint, which is the wrong answer to "not enough
    # yet". It is now a 400 that NAMES the missing field.
    r = c.patch(f"/api/enrollments/{eid}",
                json={"customer": {"first_name": "Dominion", "last_name": "Customer"}},
                headers=rep)
    check(f"a name-only PATCH is refused clearly, not with a 500 "
          f"(HTTP {r.status_code})", r.status_code == 400)
    check("  ...and says which field is missing",
          (r.get_json() or {}).get("missing_fields") == ["email"])

    # The availability email is what makes the save possible, which is the
    # whole point of restoring it: the rep never retypes it.
    r = c.patch(f"/api/enrollments/{eid}",
                json={"customer": {"first_name": "Dominion", "last_name": "Customer",
                                   "email": "avail@example.com"}},
                headers=rep)
    check(f"name + the availability email is accepted (HTTP {r.status_code})",
          r.status_code == 200)
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("reopening restores the name",
          (d["customer"] or {}).get("first_name") == "Dominion"
          and (d["customer"] or {}).get("last_name") == "Customer")
    check("  ...carrying the availability email, not a retyped one",
          (d["customer"] or {}).get("email") == "avail@example.com")
    check("  ...and the availability email is still there",
          d.get("perch_token_email") == "avail@example.com")

    # A later partial PATCH must not blank what the first one wrote.
    r = c.patch(f"/api/enrollments/{eid}",
                json={"customer": {"phone": "5551234567"}}, headers=rep)
    check(f"a phone-only PATCH is accepted (HTTP {r.status_code})",
          r.status_code == 200)
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("  ...and leaves the name intact",
          (d["customer"] or {}).get("first_name") == "Dominion")
    check("  ...while saving the phone",
          (d["customer"] or {}).get("phone") == "5551234567")

    # The address, including the STATE - the field the frontend never restored.
    r = c.patch(f"/api/enrollments/{eid}",
                json={"service_address": {"street": "2003 Plank Rd", "unit": "",
                                          "city": "Fredericksburg", "state": "VA",
                                          "zip": "22401"}},
                headers=rep)
    check(f"the service address saves (HTTP {r.status_code})", r.status_code == 200)
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("the address state round-trips as VA",
          (d["service_address"] or {}).get("state") == "VA")
    check("  ...and is NOT silently rewritten to NY",
          (d["service_address"] or {}).get("state") != "NY")
    check("  ...while the capacity ZIP stays distinct from the address ZIP",
          d.get("service_zip") == "22408"
          and (d["service_address"] or {}).get("zip") == "22401")

    # ═══════════════════════════════════════════════════════════════════
    section("4. ENROLLMENTS STAY ISOLATED FROM EACH OTHER")

    other = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
    c.patch(f"/api/enrollments/{other}",
            json={"customer": {"first_name": "Other", "last_name": "Person",
                               "email": "other@example.com"}},
            headers=rep)
    a = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    b = c.get(f"/api/enrollments/{other}", headers=rep).get_json()
    check("enrollment A keeps its own customer",
          a["customer"]["first_name"] == "Dominion")
    check("enrollment B keeps its own customer",
          b["customer"]["first_name"] == "Other")
    check("  ...and B inherited no availability state from A",
          not b.get("perch_token_email") and not b.get("service_zip"))
    check("  ...nor A's utility", not b.get("utility_slug"))

    # ═══════════════════════════════════════════════════════════════════
    section("5. ADMIN NAV IS DERIVED FROM THE SESSION, NOT SET ONCE")

    check("the nav sync runs when the current user is applied to the UI",
          "function applyCurrentUserToUI(){ syncAdminNav(); if(!currentUser) return;"
          in JSN)
    check("  ...BEFORE the null guard, so a lost session hides the controls",
          JSN.index("syncAdminNav(); if(!currentUser) return;")
          < JSN.index("state.rep.name = currentUser.full_name"))
    # REWRITTEN (Phase B), not loosened. The old form pinned syncAdminNav() to
    # the ADJACENT statement - "...loadSalesReport(); syncAdminNav(); }" - so
    # it broke the moment Phase B added the URL push between them, even though
    # the property it names was untouched. That is a formatting coupling, the
    # same class this suite has rewritten before.
    #
    # REWRITTEN AGAIN (Phase C3), for the second time and for the same reason:
    # the previous form pinned syncAdminNav() to being the LAST statement, so
    # it broke the moment C3 appended closeNav() after it to dismiss the mobile
    # drawer on navigation - a statement that cannot affect whether the nav was
    # re-derived, because the re-derivation already happened on the line above.
    # "Last statement" was only ever a proxy for the property that matters.
    #
    # The property is: syncAdminNav() runs on EVERY view change, exactly once,
    # never under a condition. That is asserted structurally - the call sits at
    # the top level of showView's body (brace depth 0 relative to it), so it
    # cannot be inside an if, a loop, or a callback - and exactly once, so it
    # cannot be moved into a branch and "compensated" elsewhere. Where it sits
    # among its unconditional siblings is formatting, and is not asserted.
    _sv = JSN[JSN.index("function showView(name){"):]
    _nxt = _sv.find("function ", 1)
    if _nxt != -1:
        _sv = _sv[:_nxt]
    _body = _sv[_sv.index("{") + 1:]
    _depth, _top = 0, []
    for _pos, _ch in enumerate(_body):
        if _ch == "{":
            _depth += 1
        elif _ch == "}":
            _depth -= 1
        elif _depth == 0 and _body.startswith("syncAdminNav()", _pos):
            # Brace depth 0 rules out an if/loop/callback WITH braces; the
            # preceding token rules out a braceless one, which would leave a
            # ')' there rather than the ';' or '{' that ends a statement.
            if _body[:_pos].rstrip()[-1:] in (";", "{", ""):
                _top.append(_pos)
    check("the nav is re-derived on every view change - unconditionally, "
          f"at the top level of showView ({len(_top)} call)",
          len(_top) == 1 and _sv.count("syncAdminNav()") == 1)
    check("a 401 hides the admin controls with the session",
          "currentUser = null; syncAdminNav();" in JSN)
    check("visibility is still decided by the role and nothing else",
          "const isAdmin = !!(currentUser && currentUser.role === 'admin');" in JSN)
    check("  ...and covers exactly the two reported buttons",
          "['nav-reps', 'nav-sales-report'].forEach" in JSN)
    check("the buttons still start hidden in the markup, so nothing leaks "
          "pre-auth",
          'id="nav-reps" style="display:none;"' in HTML.replace("\n", " ")
          or 'id="nav-reps"' in HTML and 'display:none' in HTML)

    # The security boundary is the backend, and it has not moved.
    check("a rep is refused the reps list", c.get("/api/admin/reps",
          headers=rep).status_code == 403)
    check("a rep is refused the sales report", c.get("/api/admin/sales-report",
          headers=rep).status_code == 403)
    check("an admin is allowed both",
          c.get("/api/admin/reps", headers=admin).status_code == 200
          and c.get("/api/admin/sales-report", headers=admin).status_code == 200)
    check("an unauthenticated caller is refused",
          c.get("/api/admin/sales-report").status_code == 401)

    # ═══════════════════════════════════════════════════════════════════
    section("6. THE FRONTEND READS EVERY PLACE A VALUE IS PERSISTED")

    check("the Perch email falls back to the availability email",
          "perchContext.email = (e.customer && e.customer.email) "
          "|| e.perch_token_email || '';" in JSN)
    check("the utility slug falls back to the enrollment's own slug",
          "perchContext.utilitySlug = e.utility_slug" in JSN)
    check("the capacity ZIP is restored",
          "perchContext.capacityZip = e.service_zip" in JSN)
    check("the service address STATE is restored",
          "state.address.state = e.service_address.state || 'NY';" in JSN)
    check("the meter number is restored",
          "state.customer.meterNumber = e.utility_account.meter_number || '';" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("7. THE AVAILABILITY DEAD END IS A RECOVERY, NOT A MESSAGE")

    check("the guard now returns the rep to Availability",
          "return requireAvailabilityAgain(" in JSN)
    check("  ...and no longer just renders the old message",
          "wizardFieldError(errEl, 'Re-run the availability check before continuing.')"
          not in JSN)
    check("the recovery navigates to the Availability step",
          "function requireAvailabilityAgain(reason){" in JSN
          and "goStep(1);" in JSN)
    check("  ...relabels the action so the thing to press is obvious",
          "primary.textContent = 'Check Availability Again'" in JSN)
    check("  ...and explains why the rep moved",
          "banner.style.display = 'block';" in JSN
          and "Check Availability Again" in JSN)
    check("the banner exists outside the renderer's container",
          'id="availability-rerun-banner"' in HTML
          and HTML.index('id="availability-rerun-banner"')
              < HTML.index('id="workflow-root"'))
    check("  ...and is styled from the existing warning tokens",
          ".wf-rerun-banner{" in CSS and "var(--warn-tint)" in CSS)
    check("the notice clears once the rep is past Availability",
          "if(n !== 1) clearAvailabilityRerunNotice();" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("8. SAVING IS CONSISTENT, AND REFUSES GARBAGE")

    check("one builder decides what a progress save contains",
          "function buildCustomerProgress(){" in JSN)
    check("  ...and one sender delivers it",
          "async function saveCustomerProgress(){" in JSN)
    check("leaving the wizard captures the form and saves first",
          "captureCustomerFields(); await saveCustomerProgress(); "
          "progressSavedByCaller = true;" in JSN)
    check("  ...and tells showView not to save it a second time",
          "const alreadySaved = progressSavedByCaller; "
          "progressSavedByCaller = false;" in JSN)
    check("moving off the customer step also saves",
          "if(currentStep === 2 && n !== 2){ captureCustomerFields(); "
          "saveCustomerProgress(); }" in JSN)
    check("one stray character is NOT a customer",
          "if(first.length < 2 || last.length < 2 || !email) return null;" in JSN)
    check("  ...and the email comes from the availability session, not a retype",
          "const email = (state.customer.email || perchContext.email || '').trim();"
          in JSN)
    check("a committed enrollment is never autosaved (PATCH answers 409 there)",
          "if(perchContext.enrollmentSubmitted) return null;" in JSN)
    check("a failed autosave never blocks the rep",
          "} catch(err){ return false; } }" in JSN)
    check("the DOM is read back into state before saving",
          "function captureCustomerFields(){" in JSN)
    check("  ...which is what Back used to skip",
          "take('c-first', function(x){ state.customer.first = x; });" in JSN)
    check("capture is ADDITIVE: an empty input never clears restored state",
          "const take = function(id, apply){ const got = v(id); if(got) apply(got); };"
          in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("8b. EVERY EXIT PATH SAVES, NOT ONE PARTICULAR BUTTON")
    # The sidebar links call showView() from inline onclick handlers and never
    # went near exitWizard(), so leaving by the nav saved nothing. The hook
    # belongs in showView(), which every in-app navigation passes through.
    check("the save hook lives in showView, the one shared exit",
          "function showView(name){ const leavingWizard = name !== 'wizard'" in JSN)
    check("  ...and fires only when actually leaving the wizard",
          "if(leavingWizard && !alreadySaved){ captureCustomerFields(); "
          "saveCustomerProgress(); }" in JSN)
    check("Save & Exit exists as a clearer label for the same thing",
          "async function saveAndExit(){" in JSN
          and 'id="btn-bill-save-exit"' in HTML)
    check("  ...and Back is still there, so it is not the only way out",
          'onclick="backFromCustomer()">Back</button>' in HTML)
    check("the browser's own navigation gets a best-effort save",
          "function flushCustomerProgressOnUnload(){" in JSN
          and "keepalive: true," in JSN)
    check("  ...wired to pagehide, which fires on back/forward",
          "window.addEventListener('pagehide'" in JSN)

    section("8c. THE WHOLE FORM IS CAPTURED, NOT JUST THE NAME")
    for fid, prop in (("c-acct", "state.customer.acct"),
                      ("c-pod", "state.customer.podId"),
                      ("c-meter", "state.customer.meterNumber"),
                      ("b-street", "state.billing.street"),
                      ("b-zip", "state.billing.zip")):
        check(f"{fid} is captured into {prop}",
              f"take('{fid}'" in JSN.replace("  ", " ") or f"take('{fid}'," in JSN)
    check("the utility account is part of a progress save",
          "payload.utility_account = {" in JSN
          and "meter_number: (state.customer.meterNumber" in JSN)
    check("  ...only once it has something to attach to",
          "if(acct && slug && haveAddress){" in JSN)
    check("the meter number is painted back onto the form",
          "if(meterEl) meterEl.value = state.customer.meterNumber || '';" in JSN)

    section("8d. A PARTIAL ACCOUNT PAYLOAD NO LONGER NULLS THE REST")
    r = c.patch(f"/api/enrollments/{eid}",
                json={"utility_account": {"utility_name": "dominion-energy-va",
                                          "account_number": "1234567890",
                                          "meter_number": "9876543210"}},
                headers=rep)
    check(f"the account saves (HTTP {r.status_code})", r.status_code == 200)
    def stored_account(enrollment_id):
        """The row as DB truth. The API masks account_number for a sales_rep,
        so reading the response here would test the mask, not the write."""
        return query_one(
            "SELECT ua.* FROM utility_accounts ua "
            "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
            (enrollment_id,))

    ua = stored_account(eid)
    check("account number round-trips", ua["account_number"] == "1234567890")
    check("meter number round-trips", ua["meter_number"] == "9876543210")
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("  ...while a rep still sees the account number masked",
          (d["utility_account"] or {}).get("account_number") != "1234567890")
    # Now a payload that omits meter_number must LEAVE IT ALONE.
    r = c.patch(f"/api/enrollments/{eid}",
                json={"utility_account": {"account_number": "2222222222"}},
                headers=rep)
    ua = stored_account(eid)
    check("a partial account payload updates what it names",
          ua["account_number"] == "2222222222")
    check("  ...and does NOT null what it omits",
          ua["meter_number"] == "9876543210")
    check("  ...nor the columns the UI never sends",
          ua["utility_name"] == "dominion-energy-va")

    section("8e. A SET PASSWORD IS REPORTED, NEVER RETURNED")
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("no password is set yet", d.get("customer_has_password") is False)
    c.patch(f"/api/enrollments/{eid}",
            json={"customer": {"password": "s3cret-portal-pw"}}, headers=rep)
    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("the flag reports that one now exists",
          d.get("customer_has_password") is True)
    blob = json.dumps(d)
    check("  ...and the password itself never crosses the wire",
          "s3cret-portal-pw" not in blob)
    check("  ...nor its hash", "password_hash" not in blob)
    check("the UI says so in words instead of showing empty boxes",
          'id="c-pass-set-note"' in HTML and "function renderPasswordState(){" in JSN)

    section("8f. A NEW ENROLLMENT CANNOT INHERIT THE LAST ONE'S FORM")
    check("the reset blanks the Customer & Bill controls",
          "function clearCustomerBillForm(){" in JSN
          and "clearCustomerBillForm();" in JSN)
    # Anchored to the RESET, not the declaration. "marketAccountFields = null;"
    # alone also matches `let marketAccountFields = null;` at module scope, so
    # the check passed whether or not the reset line existed - a false pass a
    # mutation found.
    check("  ...and the previous market's account rules",
          "marketAccountFields = null; perchContext = freshPerchContext();" in JSN)
    check("  ...and the remembered Availability descriptor",
          "availabilityWorkflow = null;" in JSN)
    check("  ...and the last bill parse", "lastBillParse = null;" in JSN)

    section("8g. BACK RENDERS AVAILABILITY, NOT THE CAPACITY RESULT")
    check("a dedicated reference holds the service_area descriptor",
          "let availabilityWorkflow = null;" in JSN)
    check("  ...captured whenever one is drawn",
          "if(step && step.key === 'service_area') availabilityWorkflow = wf;" in JSN)
    check("  ...and preferred by Back",
          "const wf = availabilityWorkflow || currentWorkflow;" in JSN)
    # Narrow on purpose: renderWorkflowStep(currentWorkflow) is still correct
    # elsewhere (starting a fresh wizard, for one). What must not happen is
    # BACK rendering it.
    check("  ...so Back renders that reference, not the live workflow state",
          "const wf = availabilityWorkflow || currentWorkflow; if(wf){ "
          "goStep(1); renderWorkflowStep(wf);" in JSN)

    section("8h. THE ZIP ASSISTS INSTEAD OF ACCUSING")
    check("a neutral notice exists alongside the error",
          "function showNotice(text){" in JSN
          and "e.style.color = 'var(--ink-muted)'" in JSN)
    check("  ...and the incompatible-pair case uses it, not the red one",
          "showNotice('Switched to '" in JSN)
    check("  ...naming the utility in human words",
          "esc(labelFor(body.auto_select))" in JSN)
    check("the dropdown is never disabled by this code",
          "utilEl.disabled = true" not in JSN)

    section("8i. EVERY NON-PASSWORD FIELD ROUND-TRIPS")
    # The audit, executed rather than asserted on paper: write every field,
    # read it back, and check the DATABASE column behind each one.
    audit_id = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
    execute("UPDATE enrollments SET perch_token_email=?, utility_name=?, "
            "service_zip=? WHERE id=?",
            ("round@trip.test", VA_SLUG, "22408", audit_id))
    FULL = {
        "customer": {"first_name": "Round", "last_name": "Trip",
                     "email": "round@trip.test", "phone": "5559876543"},
        "service_address": {"street": "2003 Plank Rd", "unit": "Apt 4",
                            "city": "Fredericksburg", "state": "VA",
                            "zip": "22401"},
        "billing_address": {"same_as_service": False, "street": "99 Billing Way",
                            "unit": "Suite 2", "city": "Richmond",
                            "state": "VA", "zip": "23220"},
        "utility_account": {"utility_name": VA_SLUG,
                            "account_number": "123456789012",
                            "secondary_account_identifier": None,
                            "meter_number": "0123456789"},
    }
    r = c.patch(f"/api/enrollments/{audit_id}", json=FULL, headers=admin)
    check(f"the whole form saves in one PATCH (HTTP {r.status_code})",
          r.status_code == 200)

    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    cust = query_one("SELECT * FROM customers WHERE id = ("
                     "SELECT customer_id FROM enrollments WHERE id = ?)", (audit_id,))
    svc = query_one("SELECT * FROM service_addresses WHERE id = ("
                    "SELECT service_address_id FROM enrollments WHERE id = ?)",
                    (audit_id,))
    enr = query_one("SELECT * FROM enrollments WHERE id = ?", (audit_id,))
    acc = query_one("SELECT * FROM utility_accounts WHERE id = ?",
                    (enr["utility_account_id"],))

    # frontend field -> DB column -> value back out of the API
    ROWS = [
        ("c-first", cust["first_name"], (d["customer"] or {}).get("first_name"), "Round"),
        ("c-last", cust["last_name"], (d["customer"] or {}).get("last_name"), "Trip"),
        ("c-email", cust["email"], (d["customer"] or {}).get("email"), "round@trip.test"),
        ("c-phone", cust["phone"], (d["customer"] or {}).get("phone"), "5559876543"),
        ("a-street", svc["street"], (d["service_address"] or {}).get("street"), "2003 Plank Rd"),
        ("a-unit", svc["unit"], (d["service_address"] or {}).get("unit"), "Apt 4"),
        ("a-city", svc["city"], (d["service_address"] or {}).get("city"), "Fredericksburg"),
        ("a-state", svc["state"], (d["service_address"] or {}).get("state"), "VA"),
        ("a-zip", svc["zip"], (d["service_address"] or {}).get("zip"), "22401"),
        ("b-street", enr["billing_street"], (d["billing_address"] or {}).get("street"), "99 Billing Way"),
        ("b-unit", enr["billing_unit"], (d["billing_address"] or {}).get("unit"), "Suite 2"),
        ("b-city", enr["billing_city"], (d["billing_address"] or {}).get("city"), "Richmond"),
        ("b-state", enr["billing_state"], (d["billing_address"] or {}).get("state"), "VA"),
        ("b-zip", enr["billing_zip"], (d["billing_address"] or {}).get("zip"), "23220"),
        ("c-acct", acc["account_number"], None, "123456789012"),
        ("c-meter", acc["meter_number"], None, "0123456789"),
        ("(utility)", acc["utility_name"], d.get("utility_slug"), VA_SLUG),
    ]
    for field, stored, served, expected in ROWS:
        check(f"{field:11s} stored as {expected!r}", stored == expected)
        if served is not None:
            check(f"  ...and served back unchanged", served == expected)
    check("billing is kept as a DIFFERENT address when it differs",
          (d["billing_address"] or {}).get("same_as_service") is False)
    check("the capacity ZIP stays separate from the address ZIP",
          d.get("service_zip") == "22408" and svc["zip"] == "22401")

    section("8j. THE PERCH PAYLOAD READS FROM THOSE SAME COLUMNS")
    # Structural: the enroll payload is assembled from customers /
    # service_addresses / utility_accounts, not from anything the browser
    # re-sends at submit time.
    PR = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    for perch_field, source in (
            ('"email_address": customer["email"]', "customers.email"),
            ('"first_name": customer["first_name"]', "customers.first_name"),
            ('"last_name": customer["last_name"]', "customers.last_name"),
            ('"utility_account_number": acct["account_number"]', "utility_accounts"),
            ('"secondary_account_identifier": acct["secondary_account_identifier"]', "utility_accounts"),
            ('"address_1": svc["street"]', "service_addresses.street"),
            ('"city": svc["city"], "state": svc["state"], "zip": svc["zip"]', "service_addresses"),
    ):
        check(f"Perch {source} <- {perch_field.split(':')[0]}", perch_field in PR)
    check("the meter number is sent only when the market asks for one",
          '"meter_numbers": ([acct["meter_number"]]' in PR
          and 'if acct["meter_number"] else []' in PR)
    check("the utility slug and ZIP come from the CAPACITY record, not the form",
          'payload["utility_name"] = capacity["utility_slug"]' in ADAPTER
          and 'payload["zip_code"] = capacity["zip_code"]' in ADAPTER)

    section("8k. ATTACHED DOCUMENTS SURVIVE LEAVE AND REOPEN")
    import io as _io
    def upload(cat, names, set_id=None):
        data = {"category": cat}
        if set_id:
            data["document_set_id"] = str(set_id)
        data["files"] = [(_io.BytesIO(b"%PDF-1.4 fake page " + n.encode()), n)
                         for n in names]
        return c.post(f"/api/enrollments/{audit_id}/document-sets",
                      data=data, headers=admin,
                      content_type="multipart/form-data")

    r = upload("utility_bill", ["page1.pdf", "page2.pdf", "page3.pdf"])
    check(f"a three-page bill uploads (HTTP {r.status_code})", r.status_code in (200, 201))
    bill_set = r.get_json().get("document_set_id")
    check("  ...as ONE set", bool(bill_set))

    # "Leave and reopen" is exactly this: read the enrollment again.
    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    ds = (d.get("document_sets") or {}).get("utility_bill") or {}
    check(f"reopening reports the set ({ds.get('file_count')} files)",
          ds.get("file_count") == 3)
    check("  ...the same set id", ds.get("document_set_id") == bill_set)
    check("  ...in page order",
          [f["original_filename"] for f in ds.get("files", [])]
          == ["page1.pdf", "page2.pdf", "page3.pdf"])
    check("  ...and the rows really are associated in the database",
          query_one("SELECT COUNT(*) AS n FROM documents WHERE document_set_id = ?",
                    (bill_set,))["n"] == 3)

    # Remove one. It must STAY removed across a reopen.
    doc2 = ds["files"][1]["id"]
    r = c.delete(f"/api/enrollments/{audit_id}/document-sets/{bill_set}/files/{doc2}",
                 headers=admin)
    check(f"a page can be removed (HTTP {r.status_code})", r.status_code in (200, 204))
    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    ds = (d.get("document_sets") or {}).get("utility_bill") or {}
    names = [f["original_filename"] for f in ds.get("files", [])]
    check(f"the removed page stays removed ({names})", "page2.pdf" not in names)
    check("  ...and the others stay", names == ["page1.pdf", "page3.pdf"])

    # Add another. It must join the SAME set, not start a rival one.
    r = upload("utility_bill", ["page4.pdf"], set_id=bill_set)
    check(f"a page can be added back (HTTP {r.status_code})", r.status_code in (200, 201))
    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    ds = (d.get("document_sets") or {}).get("utility_bill") or {}
    names = [f["original_filename"] for f in ds.get("files", [])]
    check(f"the new page is attached ({names})", "page4.pdf" in names)
    check("  ...to the same set", ds.get("document_set_id") == bill_set)
    check("  ...and the removed one did NOT come back", "page2.pdf" not in names)

    section("8l. PROOF DOCUMENTS BEHAVE THE SAME WAY")
    r = upload("lmi_document", ["proof1.pdf", "proof2.pdf"])
    lmi_set = r.get_json().get("document_set_id")
    check(f"proof documents upload as their own set (HTTP {r.status_code})",
          r.status_code in (200, 201) and lmi_set != bill_set)
    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    lds = (d.get("document_sets") or {}).get("lmi_document") or {}
    check("reopening reports the proof set too", lds.get("file_count") == 2)
    check("  ...kept separate from the bill set",
          lds.get("document_set_id") != bill_set)
    doc = lds["files"][0]["id"]
    c.delete(f"/api/enrollments/{audit_id}/document-sets/{lmi_set}/files/{doc}",
             headers=admin)
    d = c.get(f"/api/enrollments/{audit_id}", headers=admin).get_json()
    lds = (d.get("document_sets") or {}).get("lmi_document") or {}
    check("a removed proof document stays removed", lds.get("file_count") == 1)
    check("  ...and the bill set is untouched by it",
          ((d.get("document_sets") or {}).get("utility_bill") or {}).get("file_count") == 3)

    section("8m. THE FRONTEND RESTORES THEM AS ALREADY-SAVED")
    check("the browser hydrates its file list from the payload",
          "function hydrateDocSets(sets){" in JSN
          and "hydrateDocSets(e.document_sets);" in JSN)
    check("  ...marking each entry with its stored document id",
          "documentId: f.id," in JSN)
    check("  ...which is what stops it being re-uploaded",
          "const unsaved = (DocSets.utility_bill.files || []).filter("
          "function(x){ return !x.documentId; });" in JSN)
    check("  ...and restores the pointer the bill step gates on",
          "state.bill.documentId = bill.files[0].id;" in JSN)

    section("8n. A BLANK PASSWORD KEEPS THE EXISTING ONE")
    check("the form only demands a password when there is not one already",
          "const keepExistingPassword = perchContext.customerHasPassword "
          "&& !pass && !passConfirm;" in JSN)
    check("  ...and omits the key entirely in that case, rather than sending ''",
          "if(!keepExistingPassword) customerPayload.password = "
          "state.customer.password;" in JSN)
    check("the backend only writes a password when one is supplied",
          'if c.get("password"):' in open(
              os.path.join(ROOT, "routes", "enrollment_routes.py"),
              encoding="utf-8").read())

    section("8o. THE REPORTED REGRESSION SEQUENCE, END TO END")
    # A. upload A, B, C   B. save & exit   C. reopen   D. remove B
    # E. add D            F. save & exit   G. reopen
    # H. the set is A, C, D   I. B is gone and is never submitted
    #
    # Against the REAL persistence model - the same endpoints the browser now
    # calls - not a stub.
    seq_id = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]

    def seq_upload(cat, names, set_id=None):
        data = {"category": cat}
        if set_id:
            data["document_set_id"] = str(set_id)
        data["files"] = [(_io.BytesIO(b"%PDF-1.4 " + n.encode()), n) for n in names]
        return c.post(f"/api/enrollments/{seq_id}/document-sets", data=data,
                      headers=admin, content_type="multipart/form-data")

    def seq_files(cat):
        """What a REOPEN would show: exactly what the detail payload reports."""
        d = c.get(f"/api/enrollments/{seq_id}", headers=admin).get_json()
        s = (d.get("document_sets") or {}).get(cat) or {}
        return s.get("document_set_id"), [f["original_filename"] for f in s.get("files", [])]

    def seq_ids(cat):
        d = c.get(f"/api/enrollments/{seq_id}", headers=admin).get_json()
        s = (d.get("document_sets") or {}).get(cat) or {}
        return {f["original_filename"]: f["id"] for f in s.get("files", [])}

    for cat, label in (("utility_bill", "bill"), ("lmi_document", "proof")):
        r = seq_upload(cat, ["A.pdf", "B.pdf", "C.pdf"])
        sid = r.get_json().get("document_set_id")
        check(f"[{label}] A, B, C upload as one set (HTTP {r.status_code})",
              r.status_code in (200, 201) and bool(sid))

        got_sid, names = seq_files(cat)
        check(f"[{label}] reopening shows A, B, C ({names})",
              names == ["A.pdf", "B.pdf", "C.pdf"])

        # D. Remove B, through the endpoint the Remove button now calls.
        b_id = seq_ids(cat)["B.pdf"]
        r = c.delete(f"/api/enrollments/{seq_id}/document-sets/{sid}/files/{b_id}",
                     headers=admin)
        check(f"[{label}] B is removed (HTTP {r.status_code})",
              r.status_code in (200, 204))

        # E. Add D to the SAME set, which is what the uploader does.
        r = seq_upload(cat, ["D.pdf"], set_id=sid)
        check(f"[{label}] D is added (HTTP {r.status_code})",
              r.status_code in (200, 201))
        check(f"[{label}]   ...to the same set, not a rival one",
              r.get_json().get("document_set_id") == sid)

        # G/H. Reopen.
        got_sid, names = seq_files(cat)
        check(f"[{label}] the set is exactly A, C, D ({names})",
              names == ["A.pdf", "C.pdf", "D.pdf"])
        check(f"[{label}]   ...B did NOT come back", "B.pdf" not in names)
        check(f"[{label}]   ...and it is still the same set", got_sid == sid)

        # I. B is gone from the database, so it cannot reach Perch.
        check(f"[{label}] B has no row at all",
              query_one("SELECT COUNT(*) AS n FROM documents WHERE id = ?",
                        (b_id,))["n"] == 0)
        check(f"[{label}]   ...and nothing in the set points at it",
              b_id not in set(seq_ids(cat).values()))
        check(f"[{label}]   ...page order is dense after the removal",
              [f["page_order"] for f in
               ((c.get(f"/api/enrollments/{seq_id}", headers=admin).get_json()
                 .get("document_sets") or {}).get(cat) or {}).get("files", [])]
              == [0, 1, 2])

    # The two sets stayed isolated throughout.
    _, bill_names = seq_files("utility_bill")
    _, lmi_names = seq_files("lmi_document")
    check("the bill set and the proof set are independent",
          bill_names == ["A.pdf", "C.pdf", "D.pdf"]
          and lmi_names == ["A.pdf", "C.pdf", "D.pdf"])
    bill_sid, _ = seq_files("utility_bill")
    lmi_sid, _ = seq_files("lmi_document")
    check("  ...with different set ids", bill_sid != lmi_sid)
    check("  ...and no document belongs to both",
          not (set(seq_ids("utility_bill").values())
               & set(seq_ids("lmi_document").values())))

    # What Perch would be sent is built from the SET, so B cannot be in it.
    perch_docs = query(
        "SELECT original_filename FROM documents WHERE document_set_id = ? "
        "ORDER BY page_order ASC, id ASC", (bill_sid,))
    check("the bill pages Perch would receive are A, C, D",
          [r["original_filename"] for r in perch_docs] == ["A.pdf", "C.pdf", "D.pdf"])
    check("  ...and B is not among them",
          "B.pdf" not in [r["original_filename"] for r in perch_docs])

    section("8p. THE BROWSER ACTUALLY ISSUES THE REMOVAL")
    # The missing link. The endpoint above always worked; nothing called it.
    check("Remove persists the deletion for a stored file",
          "async function docSetRemove(category, key){" in JSN
          and "base + '/document-sets/' + setId + '/files/' + entry.documentId" in JSN
          and "{method: 'DELETE'}" in JSN)
    # THE URL IS NOW BUILT FROM THE ENTRY'S OWN SET ID, and the old literal
    # ("'/document-sets/' + set.id + '/files/'") is deliberately gone.
    #
    # This check used to also require:
    #     if(!entry.documentId || !set.id || !currentDraft
    #        || !currentDraft.enrollment_id) return;
    # That `!set.id` bail WAS the bug in KNOWN_ISSUES #1. Every reopen ran
    # rehydrateDocumentsFromDetail(), which replaced the uploader state with
    # `id: null`, so the guard fired on every saved photo: the x spliced the
    # entry out, repainted, and sent nothing. Pinning that line meant this
    # suite asserted the defect was present. The INTENT - nothing is sent for
    # a file that was never uploaded - is what is checked now, against the
    # shapes that implement it.
    check("  ...only when the file is actually stored",
          "if(!entry.documentId) return;" in JSN)
    check("  ...addressed by the entry's own set, falling back to the category's",
          "const setId = entry.setId || set.id || null;" in JSN)
    check("  ...and a set-less legacy document goes to the per-document route",
          "base + '/documents/' + entry.documentId" in JSN)
    check("  ...with no enrollment to address, the entry is put back",
          "if(!currentDraft || !currentDraft.enrollment_id){" in JSN)
    # C2 kept the sentence and dropped the colon: the reason is now appended
    # as its own sentence, filtered through userMessage(), instead of the
    # backend's raw words being glued on after a colon.
    check("  ...restoring it and saying so if the server refuses",
          "set.files.splice(idx, 0, entry);" in JSN
          and "That file could not be removed. " in JSN)
    check("  ...and never leaving the bill pointer on a removed file",
          "function syncPrimaryDocPointer(category){" in JSN)

    section("8q. A TYPED PASSWORD SURVIVES SAVE & EXIT")
    # The actual cause of "it still asks me for the password": a progress save
    # carried no password at all, so one typed before Save & Exit was never
    # stored and customer_has_password came back false.
    check("a progress save carries a confirmed password",
          "const password = (state.customer.password || '').trim(); "
          "if(password) customer.password = password;" in JSN)
    check("  ...captured only when complete AND confirmed",
          "if(pw && pw === pw2 && pw.length >= 6) state.customer.password = pw;"
          in JSN)
    check("  ...and the flag flips immediately, not on the next reopen",
          "perchContext.customerHasPassword = true; renderPasswordState();" in JSN)

    pw_id = c.post("/api/enrollments", json={}, headers=rep).get_json()["id"]
    r = c.patch(f"/api/enrollments/{pw_id}",
                json={"customer": {"first_name": "Pass", "last_name": "Word",
                                   "email": "pw@test.io", "password": "portalpass1"}},
                headers=rep)
    check(f"the progress payload is accepted with a password (HTTP {r.status_code})",
          r.status_code == 200)
    d = c.get(f"/api/enrollments/{pw_id}", headers=rep).get_json()
    check("  ...and reopening reports a password IS set",
          d.get("customer_has_password") is True)
    check("  ...without returning it", "portalpass1" not in json.dumps(d))
    # And a later save with no password must not wipe it.
    c.patch(f"/api/enrollments/{pw_id}",
            json={"customer": {"phone": "5551110000"}}, headers=rep)
    d = c.get(f"/api/enrollments/{pw_id}", headers=rep).get_json()
    check("a later save without a password leaves it in place",
          d.get("customer_has_password") is True)

    section("9. THE DASHBOARD STOPS LYING ABOUT CUSTOMER INFO")

    check("one resolver decides the label", "function customerLabel(e){" in JSN)
    # RE-PINNED (Phase C5). This counted THREE call sites, because three
    # renderers each built a row. C5 collapsed the dashboard and Customers
    # lists onto the one shared row builder, so there is now a single display
    # call site - fewer, not more, and the opposite of a regression. Counting
    # callers was only ever a proxy for "no renderer builds this label by
    # hand", so that is what is asserted: one definition, one display caller,
    # and no other place assembling a name from first/last for display.
    check("  ...called from the one shared row builder",
          JSN.count("customerLabel(e)") == 2
          and "const custName = customerLabel(e);" in JSN)
    _hand = [ln for ln in JSN.splitlines()
             if "first_name+' '+" in ln and "customerLabel" not in ln]
    # The only survivor may be the Customers SEARCH haystack, which joins the
    # name and the email to match against - not a label anyone sees.
    check(f"  ...and nothing else builds a display name ({len(_hand)})",
          all("e.customer.email" in ln for ln in _hand))
    check("  ...and the inline ternary is gone",
          "e.customer ? (e.customer.first_name+' '+e.customer.last_name) "
          ": '(no customer info yet)'" not in JSN)
    check("the placeholder is the LAST resort, after name and email",
          JSN.index("if(name) return name;")
          < JSN.index("if(email) return email;")
          < JSN.index("return '(no customer info yet)';"))

    print(f"\n{'=' * 72}\nPHASE A RELIABILITY - ALL {len(_results)} CHECKS PASSED\n{'=' * 72}")


if __name__ == "__main__":
    main()
