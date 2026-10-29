"""
UBS 2.0 Phase E1: Perch v2 transport, next_step normalization, error handling.

WHAT E1 CHANGED, AND WHY EACH PART IS PINNED HERE

1. BASE URLS AND VERSION SELECTION
   Perch versions each endpoint family on its own. The enrollment-session
   endpoints exist on v1 and v2; the market lookups stay on v1 - except
   market capacity, which moves to /v2/markets/capacity, a SIBLING of
   /v1/markets rather than a child of it. One markets base URL could never
   reach it, so capacity now has a base of its own.

   The test that matters most here is the one that proves NOTHING MOVED: with
   no new environment variables set, every base URL resolves to exactly the v1
   string the previous release used. v2 is opt-in per environment.

2. next_step NORMALIZATION
   v1 returns a full URL (.../v1/enrollments/lmi/proof_docs). v2 returns a
   bare path (/lmi/proof_docs). Before E1 every entry in the lookup table
   contained the segment `/enrollments/`, so under v2 EVERY enrollment would
   have resolved to an unrecognized step and been parked on "Needs review" -
   the single highest-impact v2 breakage found in the E0 audit. Both shapes
   resolve now, and an unknown step still fails loudly.

   Perch's next_step stays authoritative. This layer translates it. It never
   infers a step, and no route guesses one.

3. ERROR ENVELOPES
   v1 and v2 share a JSON SHAPE that means two different things:

       v1  {"error": "24112 is not valid for the given utility"}
       v2  {"error": "unprocessable_entity", "message": "Email is not valid"}

   On v1 `error` IS the sentence. On v2 it is a machine code. Joining both -
   which is what the old formatter did unconditionally - puts the token
   `unprocessable_entity` in front of a rep. The code is dropped from the
   user-facing text and kept in the body attached for the audit record.

NO NETWORK, NO DATABASE. requests is replaced with a stub and the environment
is restored after every case, so this suite is offline, order-independent and
deterministic.

Run: python test/test_e1_v2_transport.py
"""
import json
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

from services.perch import client as _client
from services.perch import config as _config
from services.perch import workflow
from services.perch.client import PerchHTTPClient, PATH_MARKETS_CAPACITY

PERCH_ENV_KEYS = (
    "PERCH_API_VERSION",
    "PERCH_PARTNER_BASE_URL",
    "PERCH_ENROLLMENT_BASE_URL",
    "PERCH_MARKETS_BASE_URL",
    "PERCH_MARKETS_CAPACITY_BASE_URL",
)

PROD_ROOT = "https://api.perchenergy.com/affiliate_partners"
STAGING_ROOT = "https://staging.api.perchenergy.com/affiliate_partners"


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


@contextmanager
def perch_env(**overrides):
    """Run a case with EXACTLY the given PERCH_* variables set.

    Every key in PERCH_ENV_KEYS is cleared first, so a case can assert what
    happens with nothing configured without inheriting the shell's values, and
    nothing leaks into the next case.
    """
    saved = {k: os.environ.get(k) for k in PERCH_ENV_KEYS}
    try:
        for k in PERCH_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (
            "" if payload is None else json.dumps(payload))
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeRequests:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if not self._responses:
            raise AssertionError("FakeRequests ran out of queued responses")
        return self._responses.pop(0)


def err(payload=None, text=None):
    """A response object shaped like the one _msg() receives."""
    return FakeResponse(422, payload, text)


# ─────────────────────────── 1. BASE URLS / VERSION ───────────────────────────

def test_version_and_base_urls():
    section("1. Version selection and base URLs")

    print("\n-- 1a. nothing configured: byte-identical to the v1 release --")
    with perch_env():
        check("version defaults to v1 (v2 is opt-in, never inherited)",
              _config.get_api_version() == "v1")
        check(f"enrollments -> {PROD_ROOT}/v1/enrollments",
              _config.get_enrollment_base_url() == f"{PROD_ROOT}/v1/enrollments")
        check(f"market lookups -> {PROD_ROOT}/v1/markets",
              _config.get_markets_base_url() == f"{PROD_ROOT}/v1/markets")
        check("capacity shares the v1 lookup base on v1",
              _config.get_markets_capacity_base_url()
              == _config.get_markets_base_url())
        check("  ...which still matches the documented v1 defaults",
              _config.get_enrollment_base_url() == _config.DEFAULT_ENROLLMENT_BASE_URL
              and _config.get_markets_base_url() == _config.DEFAULT_MARKETS_BASE_URL)

    print("\n-- 1b. an existing deployment's old variables keep working --")
    # This is the case that must not regress: a staging host configured before
    # E1 existed, with only the two v1 variables set and no version flag.
    with perch_env(
            PERCH_ENROLLMENT_BASE_URL=f"{STAGING_ROOT}/v1/enrollments",
            PERCH_MARKETS_BASE_URL=f"{STAGING_ROOT}/v1/markets"):
        check("the explicit enrollment URL is honoured verbatim",
              _config.get_enrollment_base_url() == f"{STAGING_ROOT}/v1/enrollments")
        check("the explicit markets URL is honoured verbatim",
              _config.get_markets_base_url() == f"{STAGING_ROOT}/v1/markets")
        check("the partner root is DERIVED from it - no new variable needed",
              _config.get_partner_base_url() == STAGING_ROOT)
        check("capacity stays on the v1 base while the version is v1",
              _config.get_markets_capacity_base_url() == f"{STAGING_ROOT}/v1/markets")

    print("\n-- 1c. opting a single environment into v2 --")
    with perch_env(
            PERCH_API_VERSION="v2",
            PERCH_ENROLLMENT_BASE_URL=f"{STAGING_ROOT}/v1/enrollments",
            PERCH_MARKETS_BASE_URL=f"{STAGING_ROOT}/v1/markets"):
        check("version reads as v2", _config.get_api_version() == "v2")
        check("enrollments move to /v2/enrollments",
              _config.get_enrollment_base_url() == f"{STAGING_ROOT}/v2/enrollments")
        check("market LOOKUPS stay on /v1/markets (they did not move)",
              _config.get_markets_base_url() == f"{STAGING_ROOT}/v1/markets")
        check("market CAPACITY moves to /v2/markets - a sibling, not a child",
              _config.get_markets_capacity_base_url() == f"{STAGING_ROOT}/v2/markets")
        check("  ...so the full capacity URL is .../v2/markets/capacity",
              _config.get_markets_capacity_base_url() + PATH_MARKETS_CAPACITY
              == f"{STAGING_ROOT}/v2/markets/capacity")

    print("\n-- 1d. the version flag is tolerant of how an operator writes it --")
    for raw, expected in (("v2", "v2"), ("2", "v2"), ("V2", "v2"), (" v2 ", "v2"),
                          ("v1", "v1"), ("1", "v1"), ("", "v1"), ("v3", "v1")):
        with perch_env(PERCH_API_VERSION=raw):
            got = _config.get_api_version()
            check(f"PERCH_API_VERSION={raw!r} -> {expected}", got == expected)

    print("\n-- 1e. a v2 URL with the version flag back on v1 --")
    # Rolling an environment back must actually roll the path back, or a
    # 'revert' would keep calling v2.
    with perch_env(PERCH_API_VERSION="v1",
                   PERCH_ENROLLMENT_BASE_URL=f"{STAGING_ROOT}/v2/enrollments"):
        check("the path segment follows the flag back to v1",
              _config.get_enrollment_base_url() == f"{STAGING_ROOT}/v1/enrollments")

    print("\n-- 1f. explicit overrides win over everything --")
    with perch_env(PERCH_API_VERSION="v2",
                   PERCH_PARTNER_BASE_URL=STAGING_ROOT,
                   PERCH_MARKETS_CAPACITY_BASE_URL="https://pinned.invalid/markets"):
        check("an explicit capacity base is used as given",
              _config.get_markets_capacity_base_url()
              == "https://pinned.invalid/markets")
        check("  ...without disturbing the derived enrollment URL",
              _config.get_enrollment_base_url() == f"{STAGING_ROOT}/v2/enrollments")

    print("\n-- 1g. the capacity call actually uses the capacity base --")
    # A base URL nothing reads is a comment. This proves the wiring.
    c = PerchHTTPClient(
        enrollment_base_url=f"{STAGING_ROOT}/v2/enrollments",
        markets_base_url=f"{STAGING_ROOT}/v1/markets",
        markets_capacity_base_url=f"{STAGING_ROOT}/v2/markets",
        api_version="v2", api_key="k", secret_key="s", timeout=7)
    fake = FakeRequests([FakeResponse(200, {"capacity": []})])
    c._requests = lambda: fake          # noqa: E731 - test seam, instance only
    c.get_market_capacity("24112", "dominion-energy-va")
    url = fake.calls[0]["url"]
    check(f"GET went to the v2 capacity path ({url.split('?')[0]})",
          url.startswith(f"{STAGING_ROOT}/v2/markets/capacity?"))
    check("  ...and NOT to the v1 lookup base",
          "/v1/markets/capacity" not in url)

    # The same client on v1 must keep hitting the v1 path.
    c1 = PerchHTTPClient(
        enrollment_base_url=f"{STAGING_ROOT}/v1/enrollments",
        markets_base_url=f"{STAGING_ROOT}/v1/markets",
        api_key="k", secret_key="s", timeout=7)
    fake1 = FakeRequests([FakeResponse(200, {"capacity": []})])
    c1._requests = lambda: fake1        # noqa: E731 - test seam, instance only
    c1.get_market_capacity("12345", "national-grid-ny")
    check("a v1 client (capacity base omitted) still calls /v1/markets/capacity",
          fake1.calls[0]["url"].startswith(f"{STAGING_ROOT}/v1/markets/capacity?"))


# ──────────────────────── 2. next_step NORMALIZATION ────────────────────────

V1_FULL_URLS = {
    f"{STAGING_ROOT}/v1/enrollments/capacity": "service_area",
    f"{STAGING_ROOT}/v1/enrollments/enroll": "enroll",
    f"{STAGING_ROOT}/v1/enrollments/lmi/proof_docs": "proof_docs",
    f"{STAGING_ROOT}/v1/enrollments/lmi/self_attestation": "self_attestation",
    f"{STAGING_ROOT}/v1/enrollments/lmi/self_attestation/accept":
        "self_attestation_accept",
    f"{STAGING_ROOT}/v1/enrollments/contracts": "contracts",
    f"{STAGING_ROOT}/v1/enrollments/contracts/accept": "contracts_accept",
    f"{STAGING_ROOT}/v1/enrollments/status": "status",
}

# Exactly the paths the v2 spec documents, written exactly as it writes them.
V2_BARE_PATHS = {
    "/capacity": "service_area",
    "/enroll": "enroll",
    "/lmi/proof_docs": "proof_docs",
    "/lmi/self_attestation": "self_attestation",
    "/lmi/self_attestation/accept": "self_attestation_accept",
    "/contracts": "contracts",
    "/contracts/accept": "contracts_accept",
    "/status": "status",
}


def test_next_step_normalization():
    section("2. next_step normalization - v1 URLs and v2 bare paths")

    print("\n-- 2a. every known v1 full URL --")
    for url, expected in V1_FULL_URLS.items():
        key, recognized = workflow.resolve_next_step_key(url)
        tail = url.split("/v1/enrollments")[-1] or "/"
        check(f"v1 {tail} -> {expected}", key == expected and recognized)

    print("\n-- 2b. every documented v2 bare path --")
    for path, expected in V2_BARE_PATHS.items():
        key, recognized = workflow.resolve_next_step_key(path)
        check(f"v2 {path} -> {expected}", key == expected and recognized)

    print("\n-- 2c. the two prefix traps --")
    # /contracts/accept must not match the /contracts entry and send an
    # enrollment BACK a step; same for the self-attestation pair. Longest
    # suffix wins, and that is computed rather than left to dict order.
    check("v2 /contracts/accept is not read as /contracts",
          workflow.resolve_next_step_key("/contracts/accept")[0] == "contracts_accept")
    check("v2 /lmi/self_attestation/accept is not read as /lmi/self_attestation",
          workflow.resolve_next_step_key("/lmi/self_attestation/accept")[0]
          == "self_attestation_accept")
    check("v1 .../contracts/accept is not read as .../contracts",
          workflow.resolve_next_step_key(
              f"{STAGING_ROOT}/v1/enrollments/contracts/accept")[0]
          == "contracts_accept")

    print("\n-- 2d. shapes that are the same step --")
    variants = {
        "/contracts/": "contracts",                       # trailing slash
        "/lmi/proof_docs/": "proof_docs",
        f"{STAGING_ROOT}/v1/enrollments/status/": "status",
        f"{STAGING_ROOT}/v2/enrollments/contracts": "contracts",  # v2 full URL
        "/enroll?foo=bar": "enroll",                      # query string
        f"{STAGING_ROOT}/v1/enrollments/capacity?x=1": "service_area",
    }
    for raw, expected in variants.items():
        key, recognized = workflow.resolve_next_step_key(raw)
        check(f"{raw!r} -> {expected}", key == expected and recognized)

    print("\n-- 2e. an unknown next step still fails LOUDLY --")
    for unknown in ("/lmi/income_verification", "/payment_method",
                    f"{STAGING_ROOT}/v2/enrollments/something_new",
                    "/enrollments", "https://example.invalid/", "garbage"):
        key, recognized = workflow.resolve_next_step_key(unknown)
        check(f"{unknown!r} is NOT recognized and gets no step key",
              key is None and recognized is False)

    print("\n-- 2f. an absent next step is terminal, not an error --")
    for empty in (None, "", 0):
        key, recognized = workflow.resolve_next_step_key(empty)
        check(f"{empty!r} -> no step, and that is fine",
              key is None and recognized is True)

    print("\n-- 2g. resolve_next_step() reads either spelling --")
    # `next_step` is documented on v1 and is the ONLY spelling on v2.
    # `next_step_url` was observed on Perch staging v1. Routes must not care.
    doc = workflow.resolve_next_step({"next_step": "/contracts"})
    check("a v2 `next_step` resolves", doc[0] == "contracts" and doc[1] is True)
    check("  ...and the raw value is handed back for the audit record",
          doc[2] == "/contracts")
    alias = workflow.resolve_next_step(
        {"next_step_url": f"{STAGING_ROOT}/v1/enrollments/contracts"})
    check("a v1 `next_step_url` resolves to the same step", alias[0] == "contracts")
    both = workflow.resolve_next_step(
        {"next_step": "/status", "next_step_url": "/contracts"})
    check("when both are present `next_step` wins", both[0] == "status")
    for blank in ({}, None, {"next_step": None, "next_step_url": None}):
        key, recognized, raw = workflow.resolve_next_step(blank)
        check(f"{blank!r} -> no step, recognized, raw None",
              key is None and recognized is True and raw is None)

    print("\n-- 2h. the client reports WHICH spelling a response used --")
    for body, expected_shape, expected_value in (
            ({"next_step": "/contracts"}, "documented", "/contracts"),
            ({"next_step_url": "/contracts"}, "staging_alias", "/contracts"),
            ({}, "no_next_step", None)):
        value, shape = _client.extract_next_step(body)
        check(f"{body!r} -> shape {expected_shape!r}",
              shape == expected_shape and value == expected_value)
    check("extract_next_step tolerates None", _client.extract_next_step(None)
          == (None, "no_next_step"))

    print("\n-- 2i. the table covers the v2 spec and nothing is missing --")
    for path in V2_BARE_PATHS:
        check(f"{path} is in NEXT_STEP_PATH_MAP",
              path in workflow.NEXT_STEP_PATH_MAP)
    check("no table entry resolves to a step key of None",
          all(workflow.NEXT_STEP_PATH_MAP.values()))


# ───────────────────────────── 3. ERROR ENVELOPES ─────────────────────────────

# Every machine code the v2 spec defines. None of these tokens may appear in
# text a rep or a customer can read.
V2_CODES = ("bad_request", "unauthorized", "forbidden", "not_found", "conflict",
            "unprocessable_entity", "too_many_requests",
            "internal_server_error", "service_unavailable")


def test_error_envelopes():
    section("3. Error envelopes - v1, v2, and Enroll 422 field errors")

    print("\n-- 3a. v1: `error` IS the sentence --")
    only = _client._msg(err({"error": "24112 is not valid for the given utility"}))
    check(f"an error-only v1 body formats as just that sentence ({only!r})",
          only == "24112 is not valid for the given utility")
    check("  ...with no trailing ': None' impersonating a value we sent",
          "None" not in only)
    both = _client._msg(err({"error": "Invalid zip_code or utility_name",
                             "message": "24112 is not valid"}))
    check("a v1 body whose `error` is prose still joins both parts",
          both == "Invalid zip_code or utility_name: 24112 is not valid")
    check("a message-only body formats as just that message",
          _client._msg(err({"message": "nope"})) == "nope")

    print("\n-- 3b. v2: `error` is a CODE and `message` is the sentence --")
    v2 = _client._msg(err({"error": "unprocessable_entity",
                           "message": "Email is not valid"}))
    check(f"the human message is what comes out ({v2!r})",
          v2 == "Email is not valid")
    check("  ...and the machine code is not in it", "unprocessable_entity" not in v2)

    print("\n-- 3c. v2 with NO message: the code is still never printed raw --")
    for code in V2_CODES:
        text = _client._msg(err({"error": code}))
        check(f"{code} -> a sentence, not the token ({text!r})",
              code not in text and len(text) > len(code) and text.endswith("."))
    unknown = _client._msg(err({"error": "lmi_docs_required"}))
    check(f"a code the spec adds LATER is softened too ({unknown!r})",
          "lmi_docs_required" not in unknown and unknown.endswith("."))

    print("\n-- 3d. the code/prose decision --")
    for code in V2_CODES + ("lmi_docs_required", "some_new_code"):
        check(f"{code!r} reads as a machine code",
              _client._looks_like_machine_code(code) is True)
    for prose in ("24112 is not valid for the given utility",
                  "Invalid zip_code or utility_name",
                  "Email is not valid", "Not found", None, 42, "", "   "):
        check(f"{prose!r} does NOT read as a machine code",
              _client._looks_like_machine_code(prose) is False)

    print("\n-- 3e. Enroll 422 field errors (ValidationErrorsResponse) --")
    # The exact 422 captured from live staging.
    observed = _client._msg(err({"errors": [{
        "field": "customer_type", "code": "capacity_unavailable",
        "message": "Residential or Small CS capacity is not available. "
                   "Contact Perch Energy and fetch capacity using the "
                   "/capacity endpoint"}]}))
    check("it does not render as the useless 'None: None'",
          observed != "None: None")
    # E3 rewrote this envelope for the rep. The field key and the code are
    # gone and so is Perch's integrator instruction; what remains is what a
    # rep can act on. E1's own guarantee - that no machine code reaches the
    # UI - is what this now asserts, which is stronger than before.
    check(f"the rep gets a plain-language sentence ({observed!r})",
          observed.startswith("The selected savings program"))
    check("  ...carrying no field key, code or endpoint name",
          all(tok not in observed for tok in
              ("customer_type", "capacity_unavailable", "/capacity",
               "Contact Perch")))

    multi = _client._msg(err({"errors": [
        {"field": "customer_type", "code": "too_many_utility_accounts",
         "message": "Only one utility account can be processed when LMI "
                    "capacity is available"},
        {"utility_account_number": "1234567890", "field": "Utility bill size",
         "code": "file_too_large",
         "message": "Utility bill size must be less than 4MB"}]}))
    check(f"every field error is reported, not just the first ({multi!r})",
          "utility account" in multi.lower()
          and "too large to upload" in multi.lower())
    check("an account-scoped error names the account it belongs to",
          "1234567890" in multi)
    # E3: the multi-account refusal is now stated as the actionable thing it
    # is rather than echoed, and the file-size one keeps Perch's own wording
    # because it was already plain. Both facts still reach the rep.
    check("  ...and both facts survive, as sentences",
          "only one utility account" in multi.lower()
          and "too large to upload" in multi.lower())
    check("  ...with no machine code in either",
          "too_many_utility_accounts" not in multi
          and "file_too_large" not in multi)

    print("\n-- 3f. nothing crashes on a body that is not what we expected --")
    check("an empty errors list falls through rather than raising",
          isinstance(_client._msg(err({"errors": []})), str))
    check("a non-dict item in errors is stringified, not fatal",
          "plain" in _client._msg(err({"errors": ["plain"]})))
    check("an errors item with no field or message still produces text",
          isinstance(_client._msg(err({"errors": [{"code": "x"}]})), str))
    check("a non-JSON body falls back to the response text",
          _client._msg(err(None, text="<html>gateway error</html>"))
          == "<html>gateway error</html>")
    check("an empty body does not raise", _client._msg(err(None, text="")) == "")
    check("a JSON list body does not raise",
          isinstance(_client._msg(err([1, 2, 3])), str))


def test_no_machine_codes_reach_the_ui():
    section("4. No machine error code reaches a rep or a customer")

    print("\n-- 4a. the top-level v2 envelope, with and without a message --")
    for code in V2_CODES:
        with_msg = _client._msg(err({"error": code,
                                     "message": "Something is wrong with that."}))
        without = _client._msg(err({"error": code}))
        check(f"{code} never appears in the text either way",
              code not in with_msg and code not in without)

    print("\n-- 4b. the whole exception string a route hands the UI --")
    # str(e) is put straight into the JSON `error` field by the routes, so the
    # assembled sentence is what a rep reads - not just _msg()'s fragment.
    for code in V2_CODES:
        assembled = (f"Perch rejected the enrollment (422): "
                     f"{_client._msg(err({'error': code, 'message': 'Email is not valid'}))}")
        check(f"the assembled message for {code} carries no token",
              code not in assembled)

    print("\n-- 4c. the raw code is NOT lost - it stays available for audit --")
    # Dropping the token from the text must not drop it from the record. The
    # body is attached to the exception, which is what
    # is_step_already_completed_error() and the audit log read.
    resp = err({"error": "unprocessable_entity",
                "message": "This step has already been completed."})
    exc = _client.attach_response_diagnostics(Exception("x"), resp)
    check("the full body is attached to the exception",
          getattr(exc, "body_json", None) == resp.json())
    check("  ...so the machine code is still readable by code, not by people",
          exc.body_json.get("error") == "unprocessable_entity")


def main():
    test_version_and_base_urls()
    test_next_step_normalization()
    test_error_envelopes()
    test_no_machine_codes_reach_the_ui()
    print("\nAll E1 checks passed.")


if __name__ == "__main__":
    main()
