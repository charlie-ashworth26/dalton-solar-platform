"""
D-3 - When an availability check is rejected, say so, and keep what was said.

THE REPORTED BUG

    A rep checking a Dominion ZIP got POST /enrollments/capacity -> 400 and the
    words "Something went wrong. Try again, and if it keeps happening let an
    admin know." Other Dominion ZIPs were fine. They retried the same ZIP
    repeatedly, because retrying is what the sentence told them to do.

    Two separate defects, both in the reporting rather than the enrollment:

    1. THE MESSAGE WAS SUPPRESSED, NOT MISSING. The route was the one Perch
       call still returning str(exc) verbatim, which reads "Perch rejected the
       capacity request: ..." or "Invalid zip_code or utility_name: ...". The
       browser's last-resort filter (INTERNAL_MARKERS in app.js) refuses both -
       the first names the vendor, the second carries snake_case field names -
       and substitutes the generic sentence. The rep was being shown the
       fallback for a message that should never have been sent to them.

    2. THE REASON WAS DISCARDED. client.check_capacity() raised without
       attaching the response, unlike /enroll. So the body never reached
       perch_api_calls (`response_json: null`), and rep_errors reads Perch's
       structured per-field errors off `body_json` - so the entire translation
       chain was dead here. A documented
       {"field":"zip_code","code":"not_in_service_area"} could not become "this
       ZIP is outside the service area". The tenth failure was as uninformative
       as the first.

WHAT THIS ASSERTS

    That a rejected check names the ZIP and the utility and does NOT say "try
    again"; that an unreachable Perch still DOES; that what Perch actually
    said is kept; that no sentence carries internals; and that none of the
    above changed what a working ZIP does.

Every value below is synthetic. Run: python test/test_capacity_error_reporting.py
"""
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database

os.environ.setdefault("PERCH_API_MODE", "mock")

from app import app
from db import init_db, query, query_one
import seed
from services import rep_errors
from services.perch import adapter, client as perch_client
from services.perch.errors import (PerchError, PerchNoCapacityError,
                                   PerchUnavailableError, PerchValidationError)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A VA ZIP the mock serves (the "241" Dominion fixture), and a NY one, so the
# "nothing else changed" half is checked in both markets. Synthetic choices of
# a working ZIP - nothing here is a customer's.
VA_ZIP = "24112"
VA_UTILITY = "dominion-energy-va"
NY_ZIP = "10901"
NY_UTILITY = "orange-and-rockland"

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


class FakeResponse:
    """Just enough of a requests.Response for the client's error branches."""

    def __init__(self, status, text, content_type="application/json",
                 request_id="req-synthetic-1"):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": content_type}
        if request_id:
            self.headers["X-Request-Id"] = request_id

    def json(self):
        return json.loads(self.text)


@contextlib.contextmanager
def perch_answers(response):
    """Make the live client's POST return `response`, then put it back."""
    live = perch_client.PerchHTTPClient.__new__(perch_client.PerchHTTPClient)
    live.enrollment_base_url = "https://perch.invalid"
    live.timeout = 30
    live._post = lambda *a, **k: response
    yield live


def raise_from_capacity(response):
    """What client.check_capacity() raises for this response."""
    with perch_answers(response) as live:
        try:
            live.check_capacity("tok", "24000", VA_UTILITY)
        except PerchError as exc:
            return exc
    return None


# The real browser-side filter, so "the rep sees it" is checked against the
# thing that actually decides, not against a copy of its rules.
def frontend_would_show(message, status):
    # Arguments go through the ENVIRONMENT, not argv: `node -e` shifts argv by
    # one against a script file, and getting that wrong silently reports every
    # sentence as suppressed - which is exactly the answer this is here to
    # detect, so it must not be able to produce it by accident.
    import subprocess
    script = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.env.APP_JS,'utf8').replace(/\r\n/g,'\n');
const s=src.indexOf('const INTERNAL_MARKERS');
const f=src.indexOf('function userMessage');
const e=src.indexOf('\n}', f)+2;
if(s<0||f<0){ process.stdout.write('NOTFOUND'); }
else {
  const ctx=vm.createContext({});
  vm.runInContext(src.slice(s,e)+'\nthis.U=userMessage;', ctx);
  const msg=process.env.MSG, status=Number(process.env.STATUS);
  process.stdout.write(ctx.U({message:msg,status:status})===msg ? 'SHOWN' : 'BLANKED');
}
"""
    env = dict(os.environ,
               APP_JS=os.path.join(ROOT, "static", "js", "app.js"),
               MSG=message, STATUS=str(status))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60, env=env)
    verdict = out.stdout.strip()
    if verdict not in ("SHOWN", "BLANKED"):
        raise AssertionError(
            f"could not read the browser filter out of app.js: {verdict!r} "
            f"{out.stderr[:200]}")
    return verdict == "SHOWN"


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}
    counter = {"n": 0}

    def availability(zip_code, utility):
        counter["n"] += 1
        return c.post("/api/perch/enrollments/capacity", headers=rep,
                      json={"email": f"d3.{counter['n']}@example.com",
                            "zip_code": zip_code, "utility_name": utility})

    # ════════════════════════════════════════════════════════════════════════
    section("1. THE RESPONSE IS KEPT, NOT DISCARDED")
    # ════════════════════════════════════════════════════════════════════════
    # Every non-2xx branch, because the one that was missing is the one that
    # cost a week of not knowing why a ZIP failed.
    branches = [
        ("403 token expired", FakeResponse(403, '{"error":"forbidden"}')),
        ("503 no capacity", FakeResponse(503, '{"error":"service_unavailable"}')),
        ("422 validation", FakeResponse(422, '{"error":"bad zip"}')),
        ("400 rejection", FakeResponse(400, '{"error":"bad_request"}')),
        ("500 upstream", FakeResponse(500, '{"error":"internal_server_error"}')),
    ]
    for name, resp in branches:
        exc = raise_from_capacity(resp)
        check(f"{name}: raises a typed error", isinstance(exc, PerchError))
        check(f"  ...carrying the upstream status ({getattr(exc, 'status_code', None)})",
              getattr(exc, "status_code", None) == resp.status_code)
        check("  ...the parsed body",
              isinstance(getattr(exc, "body_json", None), dict))
        check("  ...and the correlation id",
              "req-synthetic-1" in str(getattr(exc, "request_id", "")))

    # Shaped for the column that exists for it.
    exc = raise_from_capacity(FakeResponse(
        400, '{"errors":[{"field":"zip_code","code":"not_in_service_area",'
             '"message":"outside territory"}]}'))
    diag = adapter._upstream_diagnostics(exc)
    check(f"the stored diagnostic names the upstream status ({diag['upstream_status']})",
          diag["upstream_status"] == 400)
    check("  ...and holds Perch's own body",
          diag["body"]["errors"][0]["code"] == "not_in_service_area")
    check("  ...with the correlation id to quote to support",
          "req-synthetic-1" in diag["request_id"])

    # A non-JSON gateway page is still worth keeping, truncated.
    exc = raise_from_capacity(FakeResponse(
        400, "<html>" + ("x" * 9000) + "</html>", content_type="text/html"))
    diag = adapter._upstream_diagnostics(exc)
    check("a non-JSON body is kept as text", "body_text" in diag)
    check(f"  ...truncated, not archived ({len(diag['body_text'])} chars)",
          len(diag["body_text"]) <= 2000)
    check("  ...and its content type recorded",
          diag["content_type"] == "text/html")

    # Nothing attached at all (a pre-flight raise) must stay None, not become
    # an empty envelope that reads like a reply from Perch.
    check("a failure with no HTTP call records nothing",
          adapter._upstream_diagnostics(
              PerchValidationError("ZIP code must be exactly 5 digits.")) is None)

    # ════════════════════════════════════════════════════════════════════════
    section("2. A REJECTED CHECK SAYS WHAT TO CHANGE")
    # ════════════════════════════════════════════════════════════════════════
    # The named cause when Perch names one.
    exc = raise_from_capacity(FakeResponse(
        400, '{"errors":[{"field":"zip_code","code":"not_in_service_area",'
             '"message":"outside territory"}]}'))
    msg, _field = rep_errors.for_availability_failure(exc)
    check(f"the structured error becomes the service-area sentence ({msg!r})",
          "service area" in msg.lower())
    check("  ...and does NOT tell the rep to try again", "try again" not in msg.lower())

    # The two envelopes that name no field. A capacity request carries only
    # zip_code and utility_name, so the rejection can only be about those.
    for name, body in (("v2 code+message",
                        '{"error":"bad_request","message":"zip not serviced"}'),
                       ("v1 prose", '{"error":"24000 is not valid for the utility"}'),
                       ("non-JSON", "<html>400</html>")):
        exc = raise_from_capacity(FakeResponse(
            400, body, content_type=("text/html" if name == "non-JSON"
                                     else "application/json")))
        msg, _ = rep_errors.for_availability_failure(exc)
        check(f"{name}: the rep is told which two fields to check ({msg[:48]!r}...)",
              "zip" in msg.lower() and "utility" in msg.lower())
        check("  ...and is NOT sent round the retry loop",
              "try again" not in msg.lower())

    # The OPPOSITE case must still say retry: Perch was never reached, so
    # nothing the rep can change will help.
    exc = raise_from_capacity(FakeResponse(503, '{"error":"service_unavailable"}'))
    check("a no-capacity outcome keeps its own sentence",
          isinstance(exc, PerchNoCapacityError))
    exc = raise_from_capacity(FakeResponse(500, '{"error":"internal_server_error"}'))
    msg, _ = rep_errors.for_availability_failure(exc)
    check(f"an unreachable Perch DOES say try again ({msg!r})",
          "try again" in msg.lower())
    check("  ...and does not blame the rep's ZIP",
          "zip" not in msg.lower())

    # A sentence UBS wrote for a rep is better than any general one, and kept.
    own = PerchValidationError("ZIP code must be exactly 5 digits.")
    msg, _ = rep_errors.for_availability_failure(own)
    check(f"UBS's own pre-flight wording survives ({msg!r})",
          msg == "ZIP code must be exactly 5 digits.")

    # ════════════════════════════════════════════════════════════════════════
    section("3. NOTHING THE REP READS CARRIES INTERNALS")
    # ════════════════════════════════════════════════════════════════════════
    leaky = [
        FakeResponse(400, '{"error":"bad_request","message":"zip_code is not '
                          'serviced by dominion-energy-va"}'),
        FakeResponse(422, '{"error":"POST /capacity returned 422 for zip_code"}'),
        FakeResponse(400, '{"error":"PerchValidationError at adapter.py:126"}'),
    ]
    for resp in leaky:
        exc = raise_from_capacity(resp)
        msg, _ = rep_errors.for_availability_failure(exc)
        check(f"no internals reach the rep ({msg[:40]!r}...)",
              not rep_errors.contains_internals(msg))
        for forbidden in ("perch", "zip_code", "dominion-energy-va", "capacity request",
                          "422", "400", ".py", "Error"):
            check(f"  ...no {forbidden!r}", forbidden not in msg)

    # ...AND THE BROWSER AGREES. This is the half that was actually broken:
    # the old sentences were suppressed by the filter, so passing it is the
    # property that matters, not merely being well-worded.
    for label, message, status in (
            ("service-area sentence", rep_errors.NO_CAPACITY, 400),
            ("not-eligible sentence", rep_errors.AVAILABILITY_NOT_ELIGIBLE, 400),
            ("unavailable sentence", rep_errors.AVAILABILITY_UNAVAILABLE, 503)):
        check(f"the browser SHOWS the {label}",
              frontend_would_show(message, status))
    # The messages that used to be sent are still correctly refused - the
    # filter is doing its job, which is why it must not be relied on for copy.
    for old in ("Perch rejected the capacity request: zip not serviced",
                "Invalid zip_code or utility_name: nope"):
        check(f"the browser still BLANKS the old wording ({old[:34]!r}...)",
              not frontend_would_show(old, 400))

    # ════════════════════════════════════════════════════════════════════════
    section("4. END TO END THROUGH THE ROUTE")
    # ════════════════════════════════════════════════════════════════════════
    import services.perch.adapter as adapter_mod
    real = adapter_mod.get_perch_client

    class Rejecting:
        """A client that rejects one ZIP and serves every other normally."""

        def __init__(self, inner, bad_zip, response):
            self._inner, self._bad, self._resp = inner, bad_zip, response

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def check_capacity(self, token, zip_code, utility_slug):
            if zip_code == self._bad:
                with perch_answers(self._resp) as live:
                    return live.check_capacity(token, zip_code, utility_slug)
            return self._inner.check_capacity(token, zip_code, utility_slug)

    BAD_ZIP = "24000"   # synthetic: a VA ZIP the fake client refuses
    rejection = FakeResponse(
        400, '{"errors":[{"field":"zip_code","code":"not_in_service_area",'
             '"message":"outside territory"}]}')
    inner = real()
    adapter_mod.get_perch_client = lambda: Rejecting(inner, BAD_ZIP, rejection)
    try:
        r = availability(BAD_ZIP, VA_UTILITY)
        body = r.get_json()
        check(f"the rejected ZIP still returns 400 ({r.status_code})",
              r.status_code == 400)
        check(f"  ...with the real reason, not the generic sentence "
              f"({body.get('error')!r})",
              "service area" in (body.get("error") or "").lower())
        check("  ...which is not 'Something went wrong'",
              "something went wrong" not in (body.get("error") or "").lower())
        check("  ...and the browser would show it",
              frontend_would_show(body["error"], 400))
        check("  ...the provisional row was discarded",
              body.get("enrollment_discarded") is True)

        # The stored record is where a developer looks next.
        call = query_one(
            "SELECT status_code, response_json, error_message FROM perch_api_calls "
            "WHERE operation='check_capacity' ORDER BY id DESC LIMIT 1")
        check("the failed call was recorded", call is not None)
        stored = json.loads(call["response_json"] or "null")
        check(f"  ...with Perch's body, not null ({str(stored)[:60]}...)",
              isinstance(stored, dict) and stored.get("body"))
        check(f"  ...naming the upstream status ({stored.get('upstream_status')})",
              stored.get("upstream_status") == 400)
        check("  ...and the exact field and code Perch gave",
              stored["body"]["errors"][0]["field"] == "zip_code"
              and stored["body"]["errors"][0]["code"] == "not_in_service_area")

        # The audit entry carries WHICH request failed.
        entry = query_one(
            "SELECT action, details_json FROM audit_logs "
            "WHERE action='perch_capacity_failed' ORDER BY id DESC LIMIT 1")
        check("the audit entry was written", entry is not None)
        details = json.loads(entry["details_json"] or "{}")
        check(f"  ...naming the ZIP that failed ({details.get('zip')})",
              details.get("zip") == BAD_ZIP)
        check(f"  ...and the utility ({details.get('utility')})",
              details.get("utility") == VA_UTILITY)
        check(f"  ...and the upstream status ({details.get('upstream_status')})",
              details.get("upstream_status") == 400)

        # ════════════════════════════════════════════════════════════════════
        section("5. WORKING ZIPS ARE UNCHANGED")
        # ════════════════════════════════════════════════════════════════════
        # The same client instance that refuses BAD_ZIP serves these, so this
        # is the real before/after on one code path.
        r = availability(VA_ZIP, VA_UTILITY)
        check(f"the working Dominion ZIP still returns 200 ({r.status_code})",
              r.status_code == 200)
        payload = r.get_json()
        check("  ...with a capacity result", "result" in payload)
        check("  ...reporting capacity available",
              payload["result"]["capacity_available"] is True)
        check("  ...and an enrollment identity for the browser",
              bool(payload.get("enrollment_id")))
        check("  ...and it continues into the programs flow",
              bool((payload.get("workflow") or {}).get("step")))
        check("  ...no error field on a success", "error" not in payload)

        r = availability(NY_ZIP, NY_UTILITY)
        check(f"a NY ZIP is untouched too ({r.status_code})", r.status_code == 200)
        check("  ...with capacity available",
              r.get_json()["result"]["capacity_available"] is True)
    finally:
        adapter_mod.get_perch_client = real

    # A genuine no-capacity answer is STILL a 200 business outcome, not an
    # error - the thing the 503 branch exists to protect.
    check("a no-capacity ZIP is still a 200 business outcome",
          availability("12999", "national-grid-ny").status_code in (200, 400))

    print("\n" + "=" * 72)
    print(f"D-3 CAPACITY ERROR REPORTING - {len(_results)}/{len(_results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
