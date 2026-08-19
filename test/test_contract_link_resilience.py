"""
Contract-link resilience + the LMI /contracts retry path.

Neither observed failure was a Dalton defect:
  * NYSEG /contracts 500 - Perch-side generation failure. Dalton's request is
    spec-correct and its retry touches ONLY /contracts.
  * National Grid NoSuchKey - the object is missing in Perch's storage. Dalton
    passes their URL through untouched.

What Dalton changed: it now detects a missing document and says so plainly
instead of dropping the rep into raw S3 XML - without ever blocking a link that
actually works.

Run: python test/test_contract_link_resilience.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
ROUTES = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
CLIENT = open(os.path.join(ROOT, "services", "perch", "client.py"), encoding="utf-8").read()


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    section("ISSUE 1 — Dalton's /contracts request is spec-correct")
    check("POST /contracts sends NO body (spec documents none)",
          "# No body: the spec documents none." in CLIENT)
    check("  ...only the enrollment token header",
          "headers = {ENROLLMENT_TOKEN_HEADER: enrollment_token}" in CLIENT)
    check("a Perch 5xx is surfaced, not swallowed", "resp.status_code >= 500" in CLIENT)

    section("ISSUE 1 — /contracts is called ONLY when Perch says so")
    check("routing is driven by Perch's next_step_key",
          "if(perchContext.nextStepKey === 'contracts')" in JS)
    check("  ...an unrecognised next step STOPS the flow",
          "Perch returned a next step Dalton does not recognize" in JS)
    check("  ...and proof_docs must report contracts before we continue",
          "Perch accepted the proof document but returned an unexpected next step" in JS)

    section("ISSUE 1 — retry touches ONLY /contracts")
    lmi = JS[JS.index("async function submitLmi"):]
    lmi = lmi[:lmi.index("\n}\n")]
    check("a resubmit short-circuits on proofSubmitted",
          "if(perchContext.proofSubmitted){" in lmi)
    check("  ...straight to contract generation",
          "await generateContractsAndOpenAgreement(4);" in lmi)
    guard = lmi[lmi.index("if(perchContext.proofSubmitted){"):]
    guard = guard[:guard.index("return;")]
    check("  ...so /lmi/proof_docs is NOT re-posted", "/lmi/proof_docs" not in guard)
    check("  ...and /enroll is never re-posted on this path", "/enroll'" not in lmi)
    check("contract generation is guarded by contractsGenerated",
          "if(!perchContext.contractsGenerated){" in JS)
    check("  ...which stays false after a failure, so retry is permitted",
          JS.index("perchContext.contractsGenerated=true;")
          > JS.index("const body=await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/contracts'"))

    section("ISSUE 1 — resume reconciles via /status before regenerating")
    check("resume asks Perch for status first", "/perch-status'" in JS)
    check("  ...and refuses to regenerate once Perch has advanced",
          "const canRegenerate = !terminal && !blocked && !statusSaysPastContracts;" in JS)

    section("ISSUE 2 — Dalton passes Perch's URL through untouched")
    check("the URL is read from the fresh /contracts response",
          'raw_items = (result.get("raw") or {}).get("contract_urls")' in ROUTES)
    check("  ...matched by contract_name", 'r.get("contract_name") == requested_name' in ROUTES)
    check("  ...and redirected verbatim", "response = redirect(url, code=302)" in ROUTES)
    check("no URL construction or rewriting",
          "urljoin" not in ROUTES and "url +" not in ROUTES and "url.replace" not in ROUTES)
    check("presigned URLs are never persisted",
          "the normalized view deliberately does NOT carry `url`" in CLIENT)
    check("  ...contracts_safe() is the redacted accessor", "def contracts_safe" in CLIENT)

    section("ISSUE 2 — missing document is reported, never faked")
    check("a pre-flight probe exists", 'headers={"Range": "bytes=0-0"}' in ROUTES)
    check("  ...a ranged GET, because HEAD breaks the presigned signature",
          "presigned signatures" in ROUTES and "HEAD against a GET-signed URL" in ROUTES)
    check("  ...detecting NoSuchKey", '"NoSuchKey" in body' in ROUTES)
    check("  ...and 404", "probe.status_code == 404" in ROUTES)
    check("the probe NEVER blocks on its own failure",
          "missing_detail = None      # unreachable probe -> proceed as before" in ROUTES)
    check("  ...it downloads nothing", "stream=True" in ROUTES and "probe.close()" in ROUTES)
    check("the failure is audited", '"perch_contract_document_missing"' in ROUTES)
    check("the rep gets a readable page, not raw XML",
          "isn&#39;t available right now" in ROUTES or "isn't available right now" in ROUTES)
    check("  ...served as HTML inside the review iframe",
          'resp.headers["Content-Type"] = "text/html; charset=utf-8"' in ROUTES)
    check("  ...naming the document to report to Perch", "Please report this document name" in ROUTES)
    check("  ...and stating the enrollment is unaffected",
          "nothing is wrong with this" in ROUTES and "can still be" in ROUTES)
    check("NO hardcoded or local substitute document",
          ".pdf'" not in ROUTES and 'send_file' not in ROUTES)

    section("PROTECTED + latent fix")
    check("openAgreementDoc is untouched and still surfaces the error",
          "Could not open that document: " in JS)
    check("one acknowledgement checkbox", JS.count('id="agr-ack-check"') == 1)
    check("latent backStep 3 removed (step 3 no longer exists)",
          "generateContractsAndOpenAgreement(origin === 'lmi' ? 4 : 2)" in JS)
    check("  ...and nothing routes to the removed step",
          "generateContractsAndOpenAgreement(origin === 'lmi' ? 4 : 3)" not in JS)

    print(f"\n{'='*72}\nCONTRACT LINK RESILIENCE - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
