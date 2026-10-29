"""
UBS 2.0 PHASE B - routing, the read-only enrollment experience, the customer
magic-link completion view, and one utility display name.

WHAT THIS SUITE IS FOR

  1. DEEP LINKS. UBS is one page and, until Phase B, also one URL. Browser
     Back, Forward, refresh and a pasted link all landed on the dashboard.
     Flask now serves index.html for the in-app paths, while /api/* must still
     answer JSON - a catch-all would have turned every API typo into an HTML
     page.

  2. THE READ-ONLY EXPERIENCE. renderAgreementCard()'s read-only branch was
     reached by terminal AND blocked enrollments and hardcoded "Your enrollment
     is complete ... Nothing further is needed." So an acceptance Perch could
     not confirm was reported to the rep as finished. The copy is now chosen by
     the authoritative workflow key, and the invariant below makes it
     impossible to add a blocked key in Python without giving it honest copy.

  3. THE CUSTOMER VIEW. openCustomerContracts() read me.utility_name,
     me.service_address, me.project_name and
     me.workflow_last_response.contracts. /api/auth/customer-me serialized
     none of them, so every row the customer saw was blank and a completed
     customer was shown an agreement card listing no agreements.

  4. ONE DISPLAY NAME. Both payloads resolve the utility slug through the one
     shared resolver, so neither a rep nor a customer can be shown
     "dominion-energy-va".

Runtime proof of the frontend half lives in test/phase_b_readonly_harness.js,
which drives openEnrollment() against a DOM stub and asserts what lands on the
page. The checks here cover the backend and guard the structure the harness
exercises.

    python test/test_phase_b_experience.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PERCH_API_MODE"] = "mock"

import db
import helpers

_temp = tempfile.TemporaryDirectory(prefix="dalton-phase-b-test-")
db.DB_PATH = os.path.join(_temp.name, "dalton_test.db")
helpers.BACKEND_ROOT = _temp.name
helpers.DATA_ROOT = _temp.name
from db import init_db, query_one, execute

init_db(reset=True)

from app import app
import seed
from services.perch import workflow as perch_workflow
from services.perch import utilities as _utilities

JS = open(ROOT / "static" / "js" / "app.js", encoding="utf-8").read()
HTML = open(ROOT / "templates" / "index.html", encoding="utf-8").read()
APPPY = open(ROOT / "app.py", encoding="utf-8").read()
AUTHROUTES = open(ROOT / "routes" / "auth_routes.py", encoding="utf-8").read()

# Collapsed whitespace with whole-line // comments dropped, so these
# assertions test the code and not its formatting.
JSN = " ".join(" ".join(
    ln for ln in JS.splitlines() if not ln.strip().startswith("//")
).split())

VA_SLUG = "dominion-energy-va"
VA_ZIP = "22401"

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


def read_only_copy_keys():
    """The keys of READ_ONLY_STATUS_COPY, read out of app.js.

    Parsed rather than hardcoded, so this suite reports what the frontend
    actually offers copy for.
    """
    body = JS.split("const READ_ONLY_STATUS_COPY = {", 1)[1]
    body = body.split("\n};", 1)[0]
    # Top-level keys only: each sits at exactly two spaces of indentation.
    return set(re.findall(r"^  ([a-z_]+): \{", body, re.M))


def main():
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════════════════
    section("1. DEEP LINKS RESOLVE, AND /api STILL ANSWERS JSON")

    for path in ("/", "/dashboard", "/enrollments", "/enrollments/9",
                 "/customers", "/reps", "/sales-report"):
        r = c.get(path)
        check(f"GET {path} serves the app (HTTP {r.status_code})",
              r.status_code == 200
              and "text/html" in r.headers.get("Content-Type", ""))

    r = c.get("/api/health")
    check(f"GET /api/health is still JSON (HTTP {r.status_code})",
          r.status_code == 200 and r.is_json)
    r = c.get("/api/definitely-not-a-route")
    check(f"an unknown /api path is a JSON 404, not the app (HTTP {r.status_code})",
          r.status_code == 404 and r.is_json)
    check("  ...because the routes are an EXPLICIT list, not a catch-all",
          "<path:" not in APPPY and '"/<' not in APPPY)
    check("the in-app paths the router can produce are all served",
          all(f'"{p}"' in APPPY for p in
              ("/dashboard", "/enrollments", "/customers", "/reps", "/sales-report")))
    check("  ...including an enrollment deep link",
          '"/enrollments/<int:enrollment_id>"' in APPPY)
    check("the frontend parses the same shapes it pushes",
          "/^\\/enrollments\\/(\\d+)\\/?$/" in JS
          and "if(view === 'wizard')" in JSN)
    check("  ...and a deep link to a missing enrollment falls back honestly",
          "if(ok === false){" in JSN and "this.replace('dashboard')" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("2. A VIRGINIA ENROLLMENT, CARRIED THROUGH TO COMPLETION")

    eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                 json={"email": "dana@example.com", "zip_code": VA_ZIP,
                       "utility_name": VA_SLUG}).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={
        "customer": {"first_name": "Dana", "last_name": "Reyes",
                     "email": "dana@example.com", "phone": "5405551234",
                     "password": "CustPass1!"},
        "service_address": {"street": "14 Caroline St", "city": "Fredericksburg",
                            "state": "VA", "zip": VA_ZIP},
        "utility_account": {"utility_name": VA_SLUG, "account_number": "900077781"},
    })

    # The customer's own session is established BEFORE completion, because a
    # magic link to a completed enrollment is refused by design at both
    # creation and redemption (access_routes.py). That refusal is pinned in
    # section 3b; the completed customer view is reached by their password
    # login, or in-session the moment they accept.
    link = c.post(f"/api/enrollments/{eid}/customer-link", headers=rep)
    check(f"the rep can mint a customer link while work remains (HTTP {link.status_code})",
          link.status_code == 201)
    raw = link.get_json()["url"].split("#", 1)[1]
    red = c.post("/api/customer-access/redeem", json={"token": raw})
    check(f"the customer redeems it (HTTP {red.status_code})", red.status_code == 200)
    cust = {"Authorization": "Bearer " + red.get_json()["token"]}

    with app.app_context():
        perch_workflow.set_state(
            eid, "contracts_accepted", next_step_url=None, recognized=True,
            last_response={"contracts": [{"contract_name": "Community Solar Agreement",
                                          "contract_url": "https://perch.example/signed/abc"}]})

    d = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("the detail payload reports the terminal workflow state",
          d["workflow_is_terminal"] is True and d["workflow_is_blocked"] is False)
    check(f"  ...with the backend's own label ({d['workflow_step_label']!r})",
          d["workflow_step_label"] == "Complete")
    check("  ...and WHEN that status was recorded",
          isinstance(d.get("workflow_updated_at"), str)
          and len(d["workflow_updated_at"]) >= 10)
    check(f"the utility is the published name ({d['utility_name']!r})",
          d["utility_name"] == "Dominion Energy")
    check("  ...resolved by the ONE shared resolver",
          d["utility_name"] == _utilities.display_name_for_slug(VA_SLUG))
    check("  ...with the raw slug still available for Perch calls",
          d["utility_slug"] == VA_SLUG)
    check("the service address keeps its own state",
          d["service_address"]["state"] == "VA")

    # ═══════════════════════════════════════════════════════════════════
    section("3. THE CUSTOMER'S OWN VIEW IS POPULATED, NOT BLANK")
    # Every field below is one openCustomerContracts() already read and never
    # received.
    me = c.get("/api/auth/customer-me", headers=cust).get_json()
    check(f"the customer is told their utility ({me.get('utility_name')!r})",
          me.get("utility_name") == "Dominion Energy")
    check("  ...never the raw slug",
          VA_SLUG not in json.dumps(me))
    check(f"  ...and their service address ({me.get('service_address')!r})",
          me.get("service_address") == f"14 Caroline St, Fredericksburg, VA {VA_ZIP}")
    check("  ...with the address's own state, not a hardcoded NY",
          ", VA " in me["service_address"] and ", NY " not in me["service_address"])
    check("the project name is serialized (or honestly null)",
          "project_name" in me)
    check("the agreement NAMES reach the customer",
          [x["contract_name"] for x in me["workflow_last_response"]["contracts"]]
          == ["Community Solar Agreement"])
    check("  ...and not one document URL",
          "contract_url" not in json.dumps(me)
          and "perch.example" not in json.dumps(me)
          and "http" not in json.dumps(me))
    check("the customer sees the same authoritative status key as the rep",
          me["workflow_step_key"] == d["workflow_step_key"]
          and me["workflow_is_terminal"] is True)
    check("  ...and when it was recorded",
          me.get("workflow_updated_at") == d.get("workflow_updated_at"))
    check("the customer still cannot read the rep detail payload",
          c.get(f"/api/enrollments/{eid}", headers=cust).status_code in (401, 403, 404))

    section("3b. ...AND THEY CAN REACH IT BY THEIR OWN LOGIN")
    # The magic link is deliberately dead once the work is done, so the
    # password login is the route back to a completed enrollment. Pinned here
    # because the completion view is useless if only a magic link could open
    # it.
    again = c.post(f"/api/enrollments/{eid}/customer-link", headers=rep)
    check(f"a link to a COMPLETED enrollment is refused (HTTP {again.status_code})",
          again.status_code == 409)
    cl = c.post("/api/auth/customer-login",
                json={"email": "dana@example.com", "password": "CustPass1!"})
    check(f"the customer signs in with their own password (HTTP {cl.status_code})",
          cl.status_code == 200)
    pw_cust = {"Authorization": "Bearer " + cl.get_json()["token"]}
    me_pw = c.get("/api/auth/customer-me", headers=pw_cust).get_json()
    check("  ...and sees the same completed view",
          me_pw["workflow_is_terminal"] is True
          and me_pw["utility_name"] == "Dominion Energy"
          and me_pw["service_address"] == me["service_address"])

    # ═══════════════════════════════════════════════════════════════════
    section("4. A BLOCKED ENROLLMENT IS NEVER DESCRIBED AS COMPLETE")

    with app.app_context():
        perch_workflow.set_state(eid, "contracts_accept_uncertain")
    d2 = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("blocked is reported as blocked, not terminal",
          d2["workflow_is_blocked"] is True and d2["workflow_is_terminal"] is False)
    me2 = c.get("/api/auth/customer-me", headers=cust).get_json()
    check("  ...to the customer as well",
          me2["workflow_is_blocked"] is True and me2["workflow_is_terminal"] is False)

    # THE CROSS-LANGUAGE INVARIANT. The frontend chooses its read-only copy by
    # workflow key. If a new terminal or blocked key is added in workflow.py
    # and no copy is written for it, the card falls back to the backend's
    # step_label - safe, but mute. This check makes that visible at once
    # instead of in the browser.
    copy_keys = read_only_copy_keys()
    expected = set(perch_workflow.TERMINAL_STEP_KEYS) | set(perch_workflow.BLOCKED_STEP_KEYS)
    check(f"every terminal/blocked key has read-only copy ({sorted(expected)})",
          expected <= copy_keys)
    check(f"  ...and no copy exists for a key that is neither ({sorted(copy_keys - expected)})",
          copy_keys <= expected)
    check("only the TERMINAL key claims completion",
          perch_workflow.TERMINAL_STEP_KEYS == {"contracts_accepted"}
          and "contracts_accepted: { eyebrow: 'Complete'," in JSN)

    section("4b. THE READ-ONLY CARD IS DRIVEN BY THE KEY, NOT HARDCODED")
    check("the old unconditional completion copy is gone",
          "<h2>Your enrollment is complete</h2>" not in JSN)
    check("  ...replaced by a lookup on the authoritative key",
          "const copy = readOnlyStatusCopy(Agreements.statusKey)" in JSN)
    check("  ...which a blocked enrollment cannot satisfy with `submitted`",
          "(Agreements.submitted ? READ_ONLY_STATUS_COPY.contracts_accepted : null)" in JSN)
    check("the key is carried on EVERY mount, not just one",
          JSN.count("statusKey:") >= 3)
    check("the status copy is never shown for a live enrollment",
          "if(Agreements.readOnly){" in JSN)
    check("the blocked copy tells the rep not to re-submit",
          "do not re-submit" in JS)
    check("the local-only outcome does NOT send the rep to Perch",
          "was not sent to Perch" in JS)

    section("4c. THE STEP-5 FRAMING IS RESTORED FOR THE NEXT ENROLLMENT")
    check("resetWizardState() restores it",
          "unlockEnrollmentControls(); restoreFinalStepFraming();" in JSN)
    check("  ...from the MARKUP's own wording, captured once",
          "finalStepLiveCopy[id] = el.textContent;" in JSN
          and "if(finalStepLiveCopy) return;" in JSN)
    check("  ...and the markup really is the source of that wording",
          'id="agr-eyebrow">Final step<' in HTML
          and 'id="agr-title">Review &amp; finish<' in HTML)
    check("the status pill has ONE owner on a re-framed page",
          "if(pill && !finalStepFramed){" in JSN)
    check("a read-only enrollment always opens on the status step",
          "if(terminal || blocked){ target = 5;" in JSN)
    check("  ...and the live routing map is left describing live routing",
          "enroll_outcome_uncertain: 2," in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("5. THE SELECT DEFAULT NO LONGER OVERWRITES A RESTORED STATE")
    # #a-state is a <select> whose markup default is NY, so take()'s "empty
    # means nothing was entered" rule did not hold for it: leaving step 2
    # before hydrateStep(2) had run wrote NY over a restored VA.
    check("the state select is only captured when the address form is in use",
          "const addressFormInUse = !!(v('a-street') || v('a-city') || v('a-zip'));" in JSN
          and "if(addressFormInUse) take('a-state'," in JSN)
    check("  ...and the billing select gets the same guard",
          "if(billingFormInUse) take('b-state'," in JSN)
    check("  ...so neither is taken unconditionally any more",
          "take('a-state', function(x){ state.address.state = x; }); take('a-zip'" not in JSN)
    check("the markup default this protects against is real",
          '<option value="NY" selected>' in HTML)

    # ═══════════════════════════════════════════════════════════════════
    section("5b. THE UPLOAD LIST REPORTS REAL PERSISTENCE")
    check("the page count is derived from stored document ids, not guessed",
          "set.files.filter(function(x){ return !!x.documentId; }).length" in JSN
          and "' of ' + total + ' pages attached'" in JSN)
    check("each row carries its own state",
          "docset-state is-stored" in JSN and ">Uploading<" in JSN)
    check("the chip no longer says 'Uploaded' before an upload resolves",
          "<div class=\"fc-size\">Uploaded</div>" not in JS)
    check("  ...it reports 'Attached' only where the document id arrives",
          "showBillChip(state.bill.fileName, 'Attached');" in JSN)
    check("  ...and names a failed upload honestly",
          "showBillChip(state.bill.fileName, 'Not uploaded');" in JSN)
    check("the new states have styling, so they are visible as states",
          ".docset-state" in open(ROOT / "static" / "css" / "app.css",
                                 encoding="utf-8").read())

    section("5c. THE RESUME BANNER NAMES THE SCREEN, NOT JUST THE PERCH STEP")
    check("the landing step is passed to the banner",
          "renderResumeBanner(e, key, terminal, blocked, target, mappedStep);" in JSN)
    check("  ...and the pre-fallback step with it, so a redirect can be explained",
          "const mappedStep = target;" in JSN)
    check("the banner says where the rep landed",
          '<span class="rb-step">Resumed on ' in JSN
          and "esc(STEP_LABEL_TEXT[landedStep])" in JSN)
    check("  ...and why, when that is not the step the workflow names",
          "' is not part of the selected program</span>'" in JSN
          and "if(mappedStep && mappedStep !== landedStep" in JSN)
    check("read-only enrollments are excluded from that line",
          "if(!terminal && !blocked && STEP_LABEL_TEXT[landedStep]){" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("6. THE DELETION BUG IS RECORDED HONESTLY - NOW AS FIXED")
    # Recorded at the user's explicit direction. This check used to require
    # that KNOWN_ISSUES.md marked the photo-removal bug OPEN, because Phase B
    # must not describe it as fixed or hide it behind a UI change.
    #
    # It was genuinely fixed on 2026-10-28: two hydration paths disagreed and
    # the second discarded the document-set id, so docSetRemove() bailed out
    # without ever issuing its DELETE. The requirement therefore inverts - the
    # entry must now record the resolution AND its cause, so a future reader
    # is not left guessing whether it lapsed or was closed quietly. What has
    # NOT changed is the rest of this section: the removal control must still
    # be on screen, one per page, still issuing a real DELETE.
    known = (ROOT / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    check("KNOWN_ISSUES.md records the photo-removal bug as resolved",
          "Removing a previously saved bill / proof photo - RESOLVED" in known)
    check("  ...under the non-blocking section, not as an open issue",
          known.index("# No longer blocking")
          < known.index("Removing a previously saved bill / proof photo - RESOLVED"))
    check("  ...naming the actual cause rather than just a date",
          "TWO HYDRATION PATHS" in known and "rehydrateDocumentsFromDetail" in known)
    check("  ...and pointing at the coverage that fails if it returns",
          "test_document_removal_persistence.py" in known
          and "document_removal_harness.js" in known)
    check("the removal control is still present - the symptom is not hidden",
          "docSetRemove(" in JS)
    check("  ...one per page, and still the real DELETE",
          "class=\"docset-remove\"" in JSN
          and "'/document-sets/' +" in JSN or "document-sets" in JS)
    check("  ...and the legacy chip's control still calls removeBill()",
          "uploadChipHtml(name, status, 'removeBill()')" in JS
          and "onclick=\"' + removeCall + '\"" in JS)
    check("Phase B's upload changes are additive: no remove path was altered",
          "async function removeBill(){" in JS
          and "await docSetRemove('utility_bill', stored.key);" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("7. THE CUSTOMER COMPLETION SCREEN")
    # Runtime proof - what actually renders, and that a blocked customer never
    # wears this copy - lives in test/phase_b_readonly_harness.js section 9.
    # These guard the approved copy itself and the two standing constraints.
    CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    approved = [
        "You're All Set!",
        "Your enrollment has been submitted successfully.",
        "Thank you for completing your enrollment. No further action is needed "
        "at this time.",
        "What Happens Next",
        "Your utility and the program team will review your information and "
        "handle the next steps. If anything else is needed, they'll contact you "
        "using the details you provided.",
        "You can safely close this window.",
    ]
    # The renderer concatenates source strings, so each line is compared
    # against the JOINED text rather than against a single literal.
    body = JS[JS.index("function renderCustomerComplete(){"):]
    body = body[:body.index("\n}")]
    # Join the concatenated fragments, then unescape the JS apostrophe so
    # "You're" compares as the word a customer reads, not as "You\'re".
    rendered = " ".join(re.sub(r"'\s*\+\s*'", "", body).split()).replace("\\'", "'")
    for line in approved:
        check(f"approved copy, verbatim: {line[:46]!r}", line in rendered)
    check("the word 'solar' appears NOWHERE in the customer completion render",
          "solar" not in rendered.lower())
    check("nothing on that screen navigates",
          "onclick" not in rendered and "<button" not in rendered
          and "showView(" not in rendered
          and "finishCustomerEnrollment" not in rendered)
    check("a COMPLETED customer is routed to it, returning or finishing now",
          "if(me.workflow_is_terminal === true){ renderCustomerComplete(); return; }"
          in JSN)
    check("  ...and a BLOCKED customer is not - they keep the honest card",
          "if(me.workflow_is_blocked === true){" in JSN)
    check("the screen is styled from the UBS tokens, not ad-hoc colours",
          ".cdone-title" in CSS and "var(--brand-tint)" in CSS
          and "var(--brand-dark)" in CSS)
    check("  ...and has an explicit phone layout",
          ".card-cdone{padding:var(--s6)" in CSS.replace("\r", ""))
    # CLASS COLLISION GUARD. .done-mark / .done-title / .done-sub already
    # belong to the REP completion screen in the markup; the customer screen
    # is namespaced cdone-* so it cannot restyle that one by accident.
    check("the rep completion screen keeps its own classes",
          'class="done-mark complete-check"' in HTML
          and 'class="done-title"' in HTML)
    check("  ...and the customer screen borrows none of them",
          'class="done-mark"' not in rendered
          and 'class="done-title"' not in rendered
          and 'class="done-sub"' not in rendered)

    # ═══════════════════════════════════════════════════════════════════
    section("8. VIRGINIA SELF-ATTESTATION IS NO LONGER CARRIED AS BLOCKED")
    PERCHROUTES = (ROOT / "routes" / "perch_routes.py").read_text(encoding="utf-8")
    check("the resolution is recorded where a future session will find it",
          "Virginia self-attestation" in known and "RESOLVED" in known)
    check("  ...under a heading that is NOT an open-issues heading",
          "# No longer blocking" in known
          and known.index("# No longer blocking") > known.index("**Status:** OPEN"))
    check("  ...naming what Perch actually confirmed",
          "Perch updated staging." in known
          and "tested successfully" in known
          and "good to move toward production" in known)
    check("VA's DEPLOYMENT status is explicitly NOT changed by that note",
          "remains staging-only until explicitly released" in known)
    check("no code comment still waits on Perch to exercise VA",
          "once VA has been exercised end to end" not in PERCHROUTES)
    check("  ...the follow-up it gated is recorded as unblocked instead",
          "now that VA HAS been exercised end to end" in PERCHROUTES)

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
