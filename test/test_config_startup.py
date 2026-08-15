"""
Configuration / startup behavior.

Guards the exact failure that cost a live reconciliation attempt: a fresh shell
with no Perch environment variables, no .env loading, a silent mock fallback,
and no startup signal - which surfaced as a confusing
501 "Not implemented by this client." on GET /status.

Run: python test/test_config_startup.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_bootstrap import (
    load_env_file, resolve_api_mode, perch_config_report,
    format_startup_banner, init_configuration, ConfigError,
    VALID_API_MODES, _parse_env_file,
)

PERCH_KEYS = ("PERCH_API_MODE", "PERCH_API_KEY", "PERCH_SECRET_KEY",
              "PERCH_ENROLLMENT_BASE_URL", "PERCH_MARKETS_BASE_URL")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


class env_sandbox:
    """Restore the environment afterwards so suites stay independent."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in PERCH_KEYS}
        for k in PERCH_KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def write_env(**values):
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".env")
    with open(p, "w", encoding="utf-8") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v}\n")
    return p


def main():
    section("REQ 1 - .env is loaded automatically")
    with env_sandbox():
        p = write_env(PERCH_API_MODE="live", PERCH_API_KEY="k-from-dotenv")
        path, applied = load_env_file(p)
        check("the file is found and reported", path == p)
        check("values are applied", "PERCH_API_MODE" in applied)
        check("mode comes from .env", os.environ["PERCH_API_MODE"] == "live")
        check("api key comes from .env", os.environ["PERCH_API_KEY"] == "k-from-dotenv")

    section("REQ 2 - real OS environment variables stay authoritative")
    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "live"          # already set by the OS
        p = write_env(PERCH_API_MODE="mock", DALTON_ONLY_IN_DOTENV="filled")
        load_env_file(p)
        check("a .env value cannot override a real OS variable",
              os.environ["PERCH_API_MODE"] == "live")
        check("unset keys are still filled from .env",
              os.environ.get("DALTON_ONLY_IN_DOTENV") == "filled")
        os.environ.pop("DALTON_ONLY_IN_DOTENV", None)
    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "mock"
        p = write_env(PERCH_API_MODE="live")
        load_env_file(p, override=True)
        check("override=True is available when explicitly requested",
              os.environ["PERCH_API_MODE"] == "live")

    section("REQ 3 - production works with no .env present")
    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "live"
        os.environ["PERCH_API_KEY"] = "prod-key"
        os.environ["PERCH_SECRET_KEY"] = "prod-secret"
        os.environ["PERCH_ENROLLMENT_BASE_URL"] = "https://api.perchenergy.com/affiliate_partners/v1/enrollments"
        path, applied = load_env_file("/nonexistent/path/.env")
        check("a missing .env is not an error", path is None and applied == [])
        rep = perch_config_report()
        check("mode still live from the OS", rep["api_mode"] == "live")
        check("keys still detected", rep["api_key_configured"] and rep["secret_key_configured"])
        check("production host is recognised", rep["environment"] == "PRODUCTION")
        banner = format_startup_banner(rep, None, [])
        check("production is called out explicitly", "PRODUCTION host detected" in banner)

    section("REQ 4 - startup reports mode, host and key presence; never secrets")
    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "live"
        os.environ["PERCH_API_KEY"] = "SUPERSECRETKEYVALUE"
        os.environ["PERCH_SECRET_KEY"] = "SUPERSECRETSIGNINGVALUE"
        os.environ["PERCH_ENROLLMENT_BASE_URL"] = "https://staging.api.perchenergy.com/affiliate_partners/v1/enrollments"
        rep = perch_config_report()
        banner = format_startup_banner(rep, "/x/.env", ["PERCH_API_KEY"])
        check("mode printed", "Perch mode" in banner and "LIVE" in banner)
        check("enrollment host printed", "staging.api.perchenergy.com" in banner)
        check("environment identified as staging", rep["environment"] == "staging")
        check("api key reported as configured", "API key           : configured" in banner)
        check("signing key reported as configured", "Signing key       : configured" in banner)
        # The whole point: presence only.
        check("API KEY VALUE never printed", "SUPERSECRETKEYVALUE" not in banner)
        check("SIGNING KEY VALUE never printed", "SUPERSECRETSIGNINGVALUE" not in banner)
        check("report carries no secret values",
              "SUPERSECRETKEYVALUE" not in str(rep)
              and "SUPERSECRETSIGNINGVALUE" not in str(rep))

    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "live"
        os.environ["PERCH_ENROLLMENT_BASE_URL"] = "https://staging.api.perchenergy.com/x"
        banner = format_startup_banner(perch_config_report(), None, [])
        check("live mode without keys warns loudly",
              "WARNING" in banner and "PERCH_API_KEY" in banner)

    section("REQ 5 - a missing/invalid mode cannot silently confuse staging")
    with env_sandbox():
        mode, defaulted = resolve_api_mode()
        check("unset defaults to mock", mode == "mock")
        check("... and the default is FLAGGED, not hidden", defaulted is True)
        banner = format_startup_banner(perch_config_report(), None, [])
        check("banner states the mode was defaulted", "defaulted" in banner)
        check("banner warns that nothing reaches Perch", "MOCK MODE" in banner)
        # This is precisely the confusion that produced the 501.
        check("banner names the endpoints that will fail in mock",
              "GET /status" in banner and "PerchNotImplementedError" in banner)

    for bad in ("prod", "production", "staging", "LIVE!", "true", "1"):
        with env_sandbox():
            os.environ["PERCH_API_MODE"] = bad
            try:
                resolve_api_mode()
                raised = False
            except ConfigError:
                raised = True
            check(f"invalid mode {bad!r} is REFUSED, not degraded to mock", raised)

    for good, expected in (("live", "live"), ("LIVE", "live"), (" live ", "live"),
                           ("mock", "mock"), ("MOCK", "mock")):
        with env_sandbox():
            os.environ["PERCH_API_MODE"] = good
            check(f"{good!r} normalises to {expected!r}", resolve_api_mode()[0] == expected)

    section("REQ 6 - an intentional mock mode is preserved for tests")
    with env_sandbox():
        os.environ["PERCH_API_MODE"] = "mock"
        mode, defaulted = resolve_api_mode()
        check("explicit mock is honoured", mode == "mock")
        check("... and is NOT reported as a default", defaulted is False)
        rep = init_configuration(verbose=False)
        check("init_configuration returns a report", rep["api_mode"] == "mock")
        from services.perch.config import get_perch_client
        check("mock mode still selects the mock client",
              type(get_perch_client()).__name__ == "PerchMockClient")
    check("only mock and live are valid", set(VALID_API_MODES) == {"mock", "live"})

    section("REQ 7 - .env stays out of Git")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gi = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
    check(".env is gitignored", "\n.env" in gi or gi.startswith(".env"))
    check(".env.* variants are gitignored", ".env.*" in gi)

    # A local .env SHOULD exist when a developer uses one - loading it is the
    # whole point of config_bootstrap.py. The security requirement is that Git
    # never tracks it, NOT that it is absent from disk. (An earlier version of
    # this test asserted absence, which contradicted the feature.)
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        print("       (a local .env is present - checking it is untracked, not that it is absent)")
    else:
        print("       (no local .env here - the untracked check still applies if one is added)")

    import subprocess
    def git(*args):
        try:
            r = subprocess.run(["git"] + list(args), cwd=root,
                               capture_output=True, text=True, timeout=15)
            return r.returncode, (r.stdout or "").strip()
        except Exception:
            return None, ""

    rc, _ = git("rev-parse", "--is-inside-work-tree")
    if rc == 0:
        # ls-files lists TRACKED files only. Empty output = not tracked.
        _, tracked = git("ls-files", "--error-unmatch", ".env")
        _, tracked_any = git("ls-files", "--", ".env", ".env.*")
        check(".env is NOT tracked by Git", ".env" not in (tracked_any or ""))
        check("no .env.* variant is tracked by Git",
              not [ln for ln in (tracked_any or "").splitlines() if ln.strip()])

        # check-ignore proves the ignore rule actually matches the path, which
        # a substring test of .gitignore cannot.
        rc_ig, out_ig = git("check-ignore", "-q", ".env")
        if rc_ig is not None:
            check("Git confirms .env matches an ignore rule", rc_ig == 0)

        # Nothing staged that would sneak it in.
        _, staged = git("diff", "--cached", "--name-only")
        check(".env is not staged for commit",
              ".env" not in [ln.strip() for ln in (staged or "").splitlines()])
    else:
        print("       (not a Git work tree here - Git-tracking checks skipped)")
        check(".gitignore rules are present for when Git is initialised",
              ".env" in gi and ".env.*" in gi)

    section("PARSER - handles real .env syntax")
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".env")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# a comment\n\n"
                 "PLAIN=value1\n"
                 'QUOTED="value2"\n'
                 "SINGLE='value3'\n"
                 "export EXPORTED=value4\n"
                 "SPACED  =  value5  \n"
                 "URL=https://example.com/a?b=c&d=e\n"
                 "EMPTY=\n"
                 "NOEQUALS\n")
    v = _parse_env_file(p)
    check("plain values parse", v.get("PLAIN") == "value1")
    check("double quotes stripped", v.get("QUOTED") == "value2")
    check("single quotes stripped", v.get("SINGLE") == "value3")
    check("export prefix handled", v.get("EXPORTED") == "value4")
    check("whitespace trimmed", v.get("SPACED") == "value5")
    check("URLs with = and & survive intact", v.get("URL") == "https://example.com/a?b=c&d=e")
    check("empty values allowed", v.get("EMPTY") == "")
    check("comments ignored", "# a comment" not in v)
    check("malformed lines ignored", "NOEQUALS" not in v)
    check("a missing file returns empty, not an exception",
          _parse_env_file("/nope/.env") == {})

    section("APP WIRING - bootstrap runs before anything reads the environment")
    app_src = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    check("app.py imports the bootstrap", "from config_bootstrap import" in app_src)
    check("init_configuration is called", "init_configuration(" in app_src)
    check("... before the Flask import (so nothing reads os.environ first)",
          app_src.index("init_configuration(") < app_src.index("from flask import"))
    check("the banner prints when serving", "format_startup_banner(" in app_src)
    routes_src = open(os.path.join(root, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("admin diagnostics expose the config report", "perch_config_report" in routes_src)

    print(f"\n{'='*72}\nCONFIG / STARTUP - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
