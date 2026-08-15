"""
Response hardening - Perch responses are never blindly assumed to be JSON.

Live staging returned a sub-400 status with a non-JSON body on POST /enroll.
resp.json() was unguarded on every success path, so JSONDecodeError escaped as
an uncaught Flask 500 - and because it was not a PerchError, the adapter's
`except PerchError` never fired and nothing was recorded in perch_api_calls.

Run: python test/test_response_hardening.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from services.perch.client import (
    parse_json_response, redact_body, PerchHTTPClient,
    PATH_ENROLL, PATH_CONTRACTS_ACCEPT, PATH_CAPACITY, PATH_STATUS,
)
from services.perch.errors import (
    PerchAmbiguousOutcomeError, PerchUnavailableError, PerchError,
)


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


class Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status=200, body="", content_type="application/json", headers=None):
        self.status_code = status
        self.text = body
        self.headers = {"Content-Type": content_type}
        if headers:
            self.headers.update(headers)

    def json(self):
        return json.loads(self.text)


def expect(fn, exc_type):
    try:
        fn()
    except exc_type as e:
        return e
    except Exception as e:  # pragma: no cover - diagnostic
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def main():
    section("VALID JSON still parses normally on every endpoint")
    ok = Resp(200, '{"next_step":"https://x/contracts"}')
    check("valid JSON returns the decoded object",
          parse_json_response(ok, PATH_ENROLL, ambiguous_on_failure=True)["next_step"]
          == "https://x/contracts")
    check("valid JSON on a safe endpoint parses",
          parse_json_response(Resp(200, '{"completed":true}'), PATH_STATUS)["completed"] is True)
    check("201 Created parses",
          parse_json_response(Resp(201, '{"a":1}'), PATH_CAPACITY)["a"] == 1)
    check("202 Accepted parses",
          parse_json_response(Resp(202, '{"message":"ok"}'),
                              PATH_CONTRACTS_ACCEPT, ambiguous_on_failure=True)["message"] == "ok")

    section("STATE-CHANGING endpoints -> PerchAmbiguousOutcomeError, never retryable")
    # This is the exact live failure: sub-400 status, empty body.
    for label, resp in [
        ("empty 2xx body (the observed live failure)", Resp(200, "")),
        ("204 No Content", Resp(204, "", content_type="")),
        ("whitespace-only body", Resp(200, "   \n  ")),
        ("HTML body on a sub-400 status", Resp(200, "<html><body>Gateway</body></html>",
                                                content_type="text/html")),
        ("plaintext body on a sub-400 status", Resp(200, "OK", content_type="text/plain")),
        ("malformed JSON", Resp(200, '{"next_step": ')),
        ("JSON content-type but truncated body", Resp(200, '{"a":')),
    ]:
        e = expect(lambda r=resp: parse_json_response(r, PATH_ENROLL, ambiguous_on_failure=True),
                   PerchAmbiguousOutcomeError)
        check(f"/enroll {label} -> ambiguous", isinstance(e, PerchAmbiguousOutcomeError))
        check(f"  ... says do not retry", "Do not retry" in str(e))
        check(f"  ... points at GET /status", "/status" in str(e))

    e = expect(lambda: parse_json_response(Resp(202, ""), PATH_CONTRACTS_ACCEPT,
                                            ambiguous_on_failure=True),
               PerchAmbiguousOutcomeError)
    check("/contracts/accept empty body -> ambiguous", isinstance(e, PerchAmbiguousOutcomeError))
    check("existing acceptance ambiguity protection preserved",
          not isinstance(e, PerchUnavailableError))

    section("SAFE/REPEATABLE endpoints -> retryable typed error")
    for path in (PATH_CAPACITY, PATH_STATUS):
        e = expect(lambda p=path: parse_json_response(Resp(200, ""), p), PerchUnavailableError)
        check(f"{path} empty body -> PerchUnavailableError", isinstance(e, PerchUnavailableError))
        check(f"  ... says it is safe to retry", "safe to retry" in str(e))
        check(f"  ... is NOT ambiguous", not isinstance(e, PerchAmbiguousOutcomeError))

    section("DIAGNOSTICS are captured on every parse failure")
    r = Resp(200, "<html>boom</html>", content_type="text/html",
             headers={"X-Request-Id": "req-abc-123"})
    e = expect(lambda: parse_json_response(r, PATH_ENROLL, ambiguous_on_failure=True),
               PerchAmbiguousOutcomeError)
    check("HTTP status captured", getattr(e, "status_code", None) == 200)
    check("Content-Type captured", "text/html" in (getattr(e, "content_type", "") or ""))
    check("request id captured", "req-abc-123" in (getattr(e, "request_id", "") or ""))
    check("body excerpt captured", "boom" in (getattr(e, "body_text", "") or ""))
    check("status appears in the message", "HTTP 200" in str(e))
    check("content-type appears in the message", "text/html" in str(e))
    check("request id appears in the message", "req-abc-123" in str(e))

    section("NO SECRET LEAKAGE in captured bodies")
    secrets = (
        'https://s3.amazonaws.com/perch/x?X-Amz-Signature=DEADBEEFSECRET&X-Amz-Expires=3600 '
        '{"enrollment_token":"550e8400-e29b-41d4-a716-446655440000",'
        '"api_key":"AKIAREALKEY123456","secret":"hunter2hunter2"}'
    )
    red = redact_body(secrets)
    check("presigned URL redacted", "s3.amazonaws.com" not in red)
    check("X-Amz-Signature value redacted", "DEADBEEFSECRET" not in red)
    check("enrollment token redacted", "550e8400-e29b-41d4-a716-446655440000" not in red)
    check("api key redacted", "AKIAREALKEY123456" not in red)
    check("secret redacted", "hunter2hunter2" not in red)
    check("redaction markers present", "[REDACTED" in red)

    r2 = Resp(200, secrets, content_type="text/plain")
    e2 = expect(lambda: parse_json_response(r2, PATH_ENROLL, ambiguous_on_failure=True),
                PerchAmbiguousOutcomeError)
    blob = str(e2) + str(getattr(e2, "body_text", ""))
    for leak in ("DEADBEEFSECRET", "AKIAREALKEY123456", "hunter2hunter2",
                 "550e8400-e29b-41d4-a716-446655440000", "s3.amazonaws.com"):
        check(f"'{leak[:18]}...' absent from the exception", leak not in blob)

    section("BODY EXCERPT is truncated")
    e3 = expect(lambda: parse_json_response(Resp(200, "x" * 50000, content_type="text/plain"),
                                             PATH_ENROLL, ambiguous_on_failure=True),
                PerchAmbiguousOutcomeError)
    check("excerpt is bounded", len(getattr(e3, "body_text", "") or "") <= 50000)
    check("message does not embed the whole body", len(str(e3)) < 6000)

    section("TYPED errors mean adapter logging still fires")
    # The original bug: JSONDecodeError is not a PerchError, so the adapter's
    # `except PerchError` never matched and nothing was recorded.
    check("ambiguous error IS a PerchError (adapter will log it)",
          issubclass(PerchAmbiguousOutcomeError, PerchError))
    check("retryable error IS a PerchError", issubclass(PerchUnavailableError, PerchError))
    import json as _j
    check("raw JSONDecodeError is NOT a PerchError (the original hole)",
          not issubclass(_j.JSONDecodeError, PerchError))

    section("PROGRAMMING BUGS still fail loudly, not mislabelled as API failures")
    class Exploding:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        @property
        def text(self):
            return '{"a":1}'
        def json(self):
            raise AttributeError("genuine bug in our own code")
    e4 = expect(lambda: parse_json_response(Exploding(), PATH_CAPACITY), PerchUnavailableError)
    # A broken response body is an upstream problem; we surface it typed rather
    # than crashing, but the underlying reason is still visible.
    check("unreadable body becomes a typed upstream error", isinstance(e4, PerchError))

    section("EVERY success path in the client is guarded")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "perch", "client.py"), encoding="utf-8").read()
    import re as _re
    unguarded = []
    for m in _re.finditer(r"^(\s*)(return |data = |body = )([A-Za-z_]*\(?)?resp\.json\(\)",
                          src, _re.M):
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_no = src.count("\n", 0, m.start()) + 1
        preceding = src[max(0, m.start() - 400):m.start()]
        if "try:" not in preceding.split("def ")[-1]:
            unguarded.append(line_no)
    check(f"no unguarded resp.json() success path remains (found {unguarded})", not unguarded)
    check("/enroll uses ambiguous parsing",
          "parse_json_response(resp, PATH_ENROLL, ambiguous_on_failure=True)" in src)
    check("/contracts/accept uses ambiguous parsing",
          "parse_json_response(resp, PATH_CONTRACTS_ACCEPT, ambiguous_on_failure=True)" in src)
    for path_const in ("PATH_CAPACITY", "PATH_STATUS", "PATH_CONTRACTS",
                       "PATH_LMI_PROOF_DOCS", "PATH_MARKETS_CAPACITY"):
        check(f"{path_const} parsed defensively",
              f"parse_json_response(resp, {path_const})" in src)
    check("/token parsed defensively", 'parse_json_response(resp, "/token")' in src)

    section("ROUTE returns a controlled message, not a 500")
    routes = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "routes", "perch_routes.py"), encoding="utf-8").read()
    check("/enroll catches the ambiguous case",
          "except PerchAmbiguousOutcomeError" in routes)
    check("GUI receives the required message",
          "Enrollment outcome is uncertain. Check status before retrying." in routes)
    check("response is explicitly not retry-safe", '"retry_safe": False' in routes)
    check("route reconciles via GET /status", "adapter.get_status(enrollment_id" in routes)
    check("uncertain enrollment state is persisted", "enroll_outcome_uncertain" in routes)
    check("uncertainty is audit-logged", "perch_enroll_uncertain" in routes)

    from services.perch.workflow import STEP_LABELS, BLOCKED_STEP_KEYS
    check("enroll_outcome_uncertain is a first-class labelled state",
          "enroll_outcome_uncertain" in STEP_LABELS)
    check("... and reopens blocked", "enroll_outcome_uncertain" in BLOCKED_STEP_KEYS)

    print(f"\n{'='*72}\nRESPONSE HARDENING - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
