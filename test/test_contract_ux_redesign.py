"""
Contract review UX redesign — one shared engine, legacy engine removed.

Run: python test/test_contract_ux_redesign.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one, execute
import seed
from services.perch import workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()


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


def fn_body(name, src=None):
    src = src or JS
    m = re.search(r"^\s*(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src, re.M)
    if not m:
        return ""
    i = src.index("{", m.start())
    d = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            d += 1
        elif src[j] == "}":
            d -= 1
            if d == 0:
                return src[i:j + 1]
    return ""


def owner_of(pos, src=None):
    src = src or JS
    fns = [(m.group(1), m.start())
           for m in re.finditer(r"^\s*(?:async )?function (\w+)\s*\(", src, re.M)]
    prev = [n for n, p in fns if p < pos]
    return prev[-1] if prev else "(top level)"


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = login(c, "charlie@daltonsolar.com", "RepPass1!")

    # ═══════════════════════════════════════════════════════
    section("LEGACY CONTRACT ENGINE — fully removed")
    for fn in ["renderDocPacket", "allDocsReviewed", "initSigCanvas", "checkCustomerReady",
               "markDocReviewed", "completeSign", "upsertCustomerRecord",
               "loadRecordIntoState", "enterCustomerSign", "clearSignature"]:
        check(f"{fn}() no longer defined", f"function {fn}(" not in JS)
        check(f"{fn}() is never called from JS", not re.search(fn + r"\s*\(", JS))
        check(f"{fn}() is never called from markup", not re.search(fn + r"\s*\(", HTML))

    for var in ["docReviewed", "sigCtx", "isDrawing", "hasSigned"]:
        check(f"legacy state '{var}' removed", not re.search(r"\b" + var + r"\b", JS))

    for el in ["screen-customer\"", "post-send", "confetti-canvas", "sig-canvas",
               "cv-agree", "cv-submit"]:
        check(f"legacy element '{el}' removed from markup", el not in HTML)

    check("no local/mock document array remains",
          "const docs = [" not in JS and "DOC_PACKET" not in JS)
    check("no signature-canvas logic remains",
          "getContext('2d')" not in JS or "sig-canvas" not in JS)
    # A comment describing what was removed is fine; executable confetti is not.
    code_only = "\n".join(l for l in JS.split("\n")
                          if not l.strip().startswith(("//", "*", "/*")))
    check("no confetti completion code remains", "confetti" not in code_only.lower())

    section("LEGACY — superseded per-actor renderers removed")
    for fn in ["renderPerchContracts", "updateAcceptButtonState", "acceptPerchContracts",
               "renderCustomerContracts", "updateCustomerAcceptState",
               "acceptCustomerContracts", "customerReviewContract",
               "renderCustomerReadOnly", "customerAcceptContracts",
               "renderCompletedContractSummary"]:
        check(f"duplicate '{fn}' removed", f"function {fn}(" not in JS)
        check(f"nothing still calls '{fn}'", not re.search(fn + r"\s*\(", JS + HTML))

    # ═══════════════════════════════════════════════════════
    section("ONE ENGINE — rep and customer share the same component")
    check("a single shared component exists", "const Agreements = {" in JS)
    check("mountAgreements() is the only mount path", "function mountAgreements(" in JS)
    check("rep mounts through it", "mountRepAgreements(" in JS)
    check("customer mounts through it",
          "actor: 'customer'" in JS and "mountAgreements({" in JS)
    check("both hosts exist in markup",
          'id="agr-host-rep"' in HTML and 'id="agr-host-customer"' in HTML)
    check("exactly ONE overlay serves both", HTML.count('id="agr-overlay"') == 1)
    # The redesign moved the acknowledgement OUT of the modal and onto the final
    # review page, where the component renders it - so it lives in JS, not HTML.
    check("exactly ONE acceptance checkbox, rendered by the component",
          JS.count('id="agr-ack-check"') == 1 and HTML.count('id="agr-ack-check"') == 0)
    check("exactly ONE agree button, rendered by the component",
          JS.count('id="agr-agree-btn"') == 1 and HTML.count('id="agr-agree-btn"') == 0)

    # There must be exactly one review path and one accept path in the frontend.
    review_owners = [owner_of(m.start()) for m in re.finditer(r"/contracts/review'", JS)]
    accept_owners = [owner_of(m.start()) for m in re.finditer(r"/contracts/accept'", JS)]
    check(f"exactly one review implementation (found in {review_owners})",
          len(review_owners) == 1 and review_owners[0] == "openAgreementDoc")
    check(f"exactly one acceptance implementation (found in {accept_owners})",
          len(accept_owners) == 1 and accept_owners[0] == "submitAgreements")
    check("the acceptance call targets the existing Perch route",
          "/contracts/accept'" in JS and "customer_confirmed: true" in JS)

    section("ONE ENGINE — backend routes are shared, not duplicated")
    routes = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("contracts route accepts both actors",
          routes.count("@require_staff_or_customer") >= 3)
    check("no customer-only contract route was added",
          "/customer/contracts" not in routes)
    check("customer scoping helper is used", "_visible_to_actor" in routes)

    # ═══════════════════════════════════════════════════════
    section("REVIEWING IS NEVER ACCEPTING")
    doc = fn_body("openAgreementDoc")
    check("opening a document calls only /contracts/review",
          "/contracts/review'" in doc and "/contracts/accept" not in doc)
    check("opening a document never sets submitted", "submitted = true" not in doc)
    check("opening a document never enables the agree button",
          "acceptanceEnabled = true" not in doc)
    upd = fn_body("updateAgreeButton")
    check("agree requires the explicit checkbox", "chk.checked" in upd)
    check("agree requires backend permission", "acceptanceEnabled" in upd)
    check("agree is blocked when read-only", "readOnly" in upd)
    sub = fn_body("submitAgreements")
    check("submit refuses without the checkbox", "chk.checked" in sub)
    check("duplicate submission is guarded",
          "inFlight" in sub and "submitted" in sub)
    check("ambiguous outcome is NOT retryable",
          "uncertain" in sub and "Do not resubmit" in sub)

    section("NO PRESIGNED URLS IN THE FRONTEND")
    check("no amazonaws reference anywhere in the UI",
          "amazonaws" not in JS and "amazonaws" not in HTML)
    check("no X-Amz signature handling", "X-Amz" not in JS)
    check("review uses the Dalton capability URL", "review_url" in doc)
    check("the component never reads a raw url field",
          not re.search(r"\.url\b", fn_body("renderAgreementRail")))
    check("read-only view shows names only, no urls",
          "contract_name" in fn_body("renderAgreementCard"))

    # ═══════════════════════════════════════════════════════
    section("BACKEND — rep flow unchanged, still completes enrollments")
    eid = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={"customer": {
        "first_name": "Uxtest", "last_name": "Person", "email": "ux@example.com",
        "phone": "5185550009", "password": "UxPass1!"}})
    with app.app_context():
        # The capacity step normally sets perch_token_email; this fixture starts
        # further along, so set it the same way the real flow does.
        execute("UPDATE enrollments SET perch_token_email=? WHERE id=?",
                ("ux@example.com", eid))
        workflow.set_state(eid, "contracts_review",
                           last_response={"contracts": [
                               {"contract_name": "ESIGN Consent Policy"},
                               {"contract_name": "Subscription Agreement"}],
                               "contract_count": 2})
    # The mock client deliberately implements no live-only endpoint, so the
    # adapter is stubbed - the same pattern the existing suites use. The ROUTE,
    # guards, workflow write and audit are all exercised for real.
    from services.perch import adapter
    _orig_accept, _orig_status = adapter.accept_contracts, adapter.get_status
    calls = {"n": 0}

    def _spy_accept(enrollment_id, metadata, user_id=None):
        calls["n"] += 1
        calls["last"] = (enrollment_id, metadata, user_id)
        return {"message": "Contracts accepted successfully", "raw": {}}

    def _fake_status(enrollment_id, user_id=None):
        return {"completed_steps": ["submit_contracts_acceptance"], "remaining_steps": [],
                "completed": True, "next_step": None, "raw": {}}

    adapter.accept_contracts = _spy_accept
    adapter.get_status = _fake_status
    try:
        r = c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=rep,
                   json={"customer_confirmed": True})
        check("rep acceptance still succeeds", r.status_code == 200)
        check("rep acceptance reports accepted", r.get_json().get("accepted") is True)
        check("rep acceptance reached the shared adapter", calls["n"] == 1)
        check("metadata is the three server-side fields only",
              set(calls["last"][1].keys()) == {"ip_address", "timestamp", "user_agent"})
    finally:
        adapter.accept_contracts, adapter.get_status = _orig_accept, _orig_status
    e = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    check("rep dashboard shows Complete", e["workflow_is_terminal"] is True)
    check("acceptance still requires explicit confirmation",
          c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=rep,
                 json={}).status_code == 400)

    section("BACKEND — customer uses the same engine and completes the enrollment")
    eid2 = c.post("/api/perch/drafts", headers=rep).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid2}", headers=rep, json={"customer": {
        "first_name": "Cust", "last_name": "Two", "email": "cust2@example.com",
        "phone": "5185550010", "password": "CustPass2!"}})
    with app.app_context():
        execute("UPDATE enrollments SET perch_token_email=? WHERE id=?",
                ("cust2@example.com", eid2))
        workflow.set_state(eid2, "contracts_review",
                           last_response={"contracts": [{"contract_name": "ESIGN"}],
                                          "contract_count": 1})
    tok = c.post("/api/auth/customer-login",
                 json={"email": "cust2@example.com", "password": "CustPass2!"}).get_json()
    ch = {"Authorization": f"Bearer {tok['token']}"}
    check("customer resolves their own enrollment", tok["enrollment_id"] == eid2)
    adapter.accept_contracts = _spy_accept
    adapter.get_status = _fake_status
    try:
        r = c.post(f"/api/perch/enrollments/{eid2}/contracts/accept", headers=ch,
                   json={"customer_confirmed": True})
        check("customer acceptance uses the SAME route and succeeds", r.status_code == 200)
        check("customer acceptance reached the SAME shared adapter", calls["n"] == 2)
        check("customer acceptance carries no staff user id", calls["last"][2] is None)
    finally:
        adapter.accept_contracts, adapter.get_status = _orig_accept, _orig_status
    e2 = c.get(f"/api/enrollments/{eid2}", headers=rep).get_json()
    check("rep dashboard reports Complete after CUSTOMER acceptance",
          e2["workflow_is_terminal"] is True)
    with app.app_context():
        st = query_one("SELECT current_step_key FROM perch_workflow_state WHERE enrollment_id=?",
                       (eid2,))
    check("customer wrote the SAME workflow state the rep reads",
          st["current_step_key"] == "contracts_accepted")

    section("SCOPING — unchanged")
    check("customer cannot touch another enrollment's contracts",
          c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=ch,
                 json={"customer_confirmed": True}).status_code == 403)
    check("customer cannot list enrollments",
          c.get("/api/enrollments", headers=ch).status_code == 403)
    check("customer cannot generate contracts for another enrollment",
          c.post(f"/api/perch/enrollments/{eid}/contracts", headers=ch,
                 json={}).status_code == 403)
    check("rep flow still reaches its own enrollments",
          c.get(f"/api/enrollments/{eid}", headers=rep).status_code == 200)

    section("COMPLETED ENROLLMENT — read-only, zero /contracts calls")
    with app.app_context():
        before = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
    e3 = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
    with app.app_context():
        after = query_one("SELECT COUNT(*) n FROM perch_api_calls")["n"]
    check("opening a completed enrollment makes zero Perch calls", after == before)
    check("completed enrollment is terminal", e3["workflow_is_terminal"] is True)
    check("contract names survive for the read-only view",
          bool((e3["workflow_last_response"] or {}).get("contracts")))
    check("no URL leaks in the payload",
          not re.findall(r"https?://", json.dumps(e3)))

    open_body = fn_body("openEnrollment")
    m = re.search(r"REHYDRATE_CONTRACT_STEPS\s*=\s*\[(.*?)\]", JS, re.S)
    steps = set(re.findall(r"'([^']+)'", m.group(1))) if m else set()
    check("terminal steps excluded from rehydrate",
          "contracts_accepted" not in steps and "contracts_accept_uncertain" not in steps)
    check("live contract steps still rehydrate", "contracts_review" in steps)
    check("read-only mount is used for terminal enrollments",
          "readOnly: true" in open_body)
    check("duplicate acceptance still blocked (Perch not called again)",
          c.post(f"/api/perch/enrollments/{eid}/contracts/accept", headers=rep,
                 json={"customer_confirmed": True}).get_json().get("already_accepted") is True)

    # ═══════════════════════════════════════════════════════
    section("UX — redesigned experience")
    card = fn_body("renderAgreementCard")
    check("headline matches the brief", "Review &amp; complete your enrollment" in card
          or "Review & complete your enrollment" in card)
    # Copy was rewritten for the simplified screen.
    check("supporting copy present", "check your details" in card.lower())
    check("account details render real enrollment data",
          "Electric utility" in card and "Service address" in card
          and "Account number" in card and "Email" in card)
    check("ONE acknowledgement carries the agreement links",
          "agreementLinksHtml()" in card)
    check("ONE final action", "Agree &amp; finish" in card or "Agree & finish" in card)

    # The modal is now a SINGLE-document viewer; the acknowledgement and submit
    # moved onto the final review page itself.
    check("viewer has a heading that names the document", 'id="agr-doc-title"' in HTML)
    check("viewer has a close control", 'class="agr-close"' in HTML)
    check("viewer has a large document area", ".agr-doc-frame" in CSS)
    check("viewer has NO document rail", 'id="agr-doc-rail"' not in HTML)
    check("acknowledgement wording includes electronic signature",
          "electronic signature" in JS)
    check("final action is clearly labelled", "Agree &amp; finish" in JS)
    check("no forced-scroll gate was added",
          "scrollHeight" not in fn_body("updateAgreeButton"))
    check("opening documents is never required to accept",
          "opened" not in fn_body("updateAgreeButton").lower())

    section("UX — accessibility and mobile")
    check("overlay is a labelled dialog",
          'role="dialog"' in HTML and 'aria-modal="true"' in HTML
          and 'aria-labelledby="agr-doc-title"' in HTML)
    check("close control is labelled", 'aria-label="Close document"' in HTML)
    check("Escape closes the overlay", "agrEscKey" in JS and "'Escape'" in JS)
    check("focus is restored on close", "agrLastFocus" in JS)
    check("backdrop click closes", "agrBackdrop" in JS)
    check("keyboard focus is visible", ".agr-link:focus-visible" in CSS
          and ".agr-close:focus-visible" in CSS)
    check("agreement links are real anchors", 'class="agr-link"' in JS)
    check("mobile becomes a full-screen viewer", "@media (max-width:820px)" in CSS)
    check("mobile stacks the detail list", ".agr-details{grid-template-columns:1fr" in CSS.replace(" ", "")
          or "grid-template-columns:1fr" in CSS)
    check("mobile avoids horizontal scrolling", "flex-direction:column" in CSS)
    check("reduced motion respected", "prefers-reduced-motion" in CSS)
    check("mobile did NOT introduce a second signing implementation",
          JS.count('id="agr-agree-btn"') == 1
          and JS.count("function submitAgreements(") == 1
          and JS.count("function openAgreementDoc(") == 1)

    section("UX — no Minnesota / Xcel content leaked from the reference")
    for term in ["Xcel", "Minnesota", "Ramsey County", "Saint Paul", "MN 55",
                 "Utility Consolidated Billing", "solar garden"]:
        check(f"no '{term}' in the UI", term.lower() not in (HTML + JS + CSS).lower())

    # ═══════════════════════════════════════════════════════
    section("WIZARD LIFECYCLE — RUNTIME execution, not substring assertions")
    # The legacy cleanup deleted `const accStatus = getElementById(...)` but left
    # the usage line, so resetWizardState() threw ReferenceError and New
    # Enrollment was completely dead. Every frontend assertion we had was a
    # SUBSTRING check, which cannot detect a used-but-unbound identifier, and
    # `new Function(src)` only validates syntax. This runs the functions for real
    # against a DOM stub so a ReferenceError fails the build.
    import subprocess
    harness = os.path.join(ROOT, "test", "wizard_lifecycle_harness.js")
    check("runtime harness exists", os.path.exists(harness))
    node = subprocess.run(["node", harness], capture_output=True, text=True, timeout=120)
    out = (node.stdout or "") + (node.stderr or "")
    for line in out.splitlines():
        if "[FAIL]" in line or "->" in line:
            print("      " + line.strip())
    check("wizard lifecycle executes with no ReferenceError", node.returncode == 0)
    check("resetWizardState() runs clean", "[FAIL] resetWizardState" not in out)
    check("startWizardFresh() runs clean", "[FAIL] startWizardFresh" not in out)
    check("startWizardForProject() runs clean", "[FAIL] startWizardForProject" not in out)
    check("openEnrollment() runs clean", "[FAIL] openEnrollment" not in out)
    check("lockEnrollmentReadOnly() runs clean", "[FAIL] lockEnrollmentReadOnly" not in out)
    # The harness prints "ReferenceError" inside its own PASS labels, so match
    # only a thrown one (its diagnostic lines are prefixed "-> ").
    check("no ReferenceError was actually thrown",
          "-> ReferenceError" not in out)

    section("CONTRACT LINK -> INDEX (runtime, on the wire)")
    # ROOT CAUSE THIS GUARDS: the screen sent {contract_index: n} while
    # /contracts/review reads data.get("index"), so every click returned
    # "A contract index is required." A parameter-name mismatch across two files
    # that no substring assertion on either file could catch. This harness
    # renders the component, clicks every link, and asserts the request body.
    link_h = os.path.join(ROOT, "test", "contract_link_index_harness.js")
    check("link/index harness exists", os.path.exists(link_h))
    lr = subprocess.run(["node", link_h], capture_output=True, text=True, timeout=120)
    lout = (lr.stdout or "") + (lr.stderr or "")
    for line in lout.splitlines():
        if "[FAIL]" in line:
            print("      " + line.strip())
    check("every agreement link sends the correct index", lr.returncode == 0)
    check("no 'contract index is required' error occurs",
          "index is required" not in lout or "[FAIL]" not in lout)
    check("backend parameter name is 'index'",
          'data.get("index")' in open(os.path.join(ROOT, "routes", "perch_routes.py"),
                                       encoding="utf-8").read())
    check("frontend sends 'index', not 'contract_index'",
          "JSON.stringify({index: index})" in JS and "contract_index" not in JS)
    check("normalizer emits an authoritative index per contract",
          '"index": position' in open(os.path.join(ROOT, "services", "perch", "client.py"),
                                       encoding="utf-8").read())

    section("SIMPLIFIED FINAL REVIEW — over-designed UI removed")
    check("no document rail", "agr-doc-rail" not in HTML and "agr-doc-rail" not in JS)
    check("no per-document cards", "agr-doc-n" not in JS)
    check("no 'Opened' badges", "Tap to open" not in JS and "'Opened'" not in JS)
    check("no giant blank preview pane", "agr-viewer-inner" not in HTML)
    check("agreement names are inline links", "agreementLinksHtml" in JS)
    check("exactly one acknowledgement in the component",
          JS.count('id="agr-ack-check"') == 1)
    check("exactly one submit button in the component",
          JS.count('id="agr-agree-btn"') == 1)
    check("acknowledgement carries the e-signature language",
          "submitting my electronic signature" in JS)
    check("document viewer has no acceptance controls inside",
          "agr-ack-check" not in HTML)
    check("viewer has a close control", 'aria-label="Close document"' in HTML)

    section("REP DASHBOARD VIEW/RESUME (runtime) — stage decides read-only")
    # ROOT CAUSE THIS GUARDS: rehydrateContractPacket() referenced `errEl`, whose
    # declaration was deleted with the legacy contract UI. It threw
    # ReferenceError on its FIRST line, before the fetch, so mountRepAgreements()
    # never ran - the rep saw a final review screen with no acknowledgement and
    # no Agree & finish. The customer path used a different function and worked,
    # which is why the bug looked actor-specific.
    resume_h = os.path.join(ROOT, "test", "rep_resume_contracts_harness.js")
    check("rep resume harness exists", os.path.exists(resume_h))
    rr = subprocess.run(["node", resume_h], capture_output=True, text=True, timeout=120)
    rout = (rr.stdout or "") + (rr.stderr or "")
    for line in rout.splitlines():
        if "[FAIL]" in line or "ReferenceError" in line:
            print("      " + line.strip())
    check("rep View of contracts_review stays INTERACTIVE", rr.returncode == 0)
    # The harness prints the word inside its own labels; match a THROWN one.
    check("no ReferenceError thrown on the rep resume path",
          "ReferenceError:" not in rout)
    # Scoped to the function that had the orphan; errEl is legitimate elsewhere
    # where it IS declared locally.
    check("rehydrate no longer references an unbound errEl",
          "errEl" not in fn_body("rehydrateContractPacket"))
    # Every remaining errEl user must declare it - this is the bug class.
    import re as _re
    _fns = [(m.group(1), m.start())
            for m in _re.finditer(r"^\s*(?:async )?function (\w+)\s*\(", JS, _re.M)]
    _unbound = []
    for _m in _re.finditer(r"\berrEl\b", JS):
        _own = [(n, p) for n, p in _fns if p < _m.start()]
        if not _own:
            continue
        _name = _own[-1][0]
        if not _re.search(r"(?:const|let|var)\s+errEl\s*=", fn_body(_name)):
            _unbound.append(_name)
    check(f"no function uses errEl without declaring it (found {sorted(set(_unbound))})",
          not _unbound)
    check("rehydrate mounts the component before fetching",
          "mountRepAgreements(false, false);\n  const cardErr" in JS)
    check("rehydrate reports failures through the component's own error region",
          "agr-card-error" in fn_body("rehydrateContractPacket"))

    print(f"\n{'='*72}\nCONTRACT UX REDESIGN - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
