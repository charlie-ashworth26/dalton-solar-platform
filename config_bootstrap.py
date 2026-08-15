"""
Configuration loading and startup reporting.

WHY THIS EXISTS
---------------
A VS Code restart created a fresh PowerShell with no Perch environment
variables. app.py did not load .env, and services/perch/config.py silently
defaults PERCH_API_MODE to "mock". The server came up pointed at fixtures with
no visible signal, and a live reconciliation call returned a confusing
501 "Not implemented by this client." (the mock has no get_status).

Three defects combined:
  1. .env was never loaded automatically
  2. an unset PERCH_API_MODE fell back to mock silently
  3. startup printed nothing about which mode was active

This module fixes all three without adding a dependency: python-dotenv is used
when present, otherwise an equivalent stdlib parser runs.

PRECEDENCE
----------
Real OS environment variables ALWAYS win. .env only fills in values that are
not already set, so production - which supplies real environment variables and
ships no .env - is unaffected.
"""
import os

# Keys we report on at startup. Values are NEVER printed.
_SECRET_KEYS = ("PERCH_API_KEY", "PERCH_SECRET_KEY", "JWT_SECRET")

VALID_API_MODES = ("mock", "live")


def _parse_env_file(path):
    """Minimal .env parser: KEY=VALUE, # comments, optional quotes, `export`.

    Deliberately stdlib-only so a missing python-dotenv cannot reintroduce the
    exact failure this module exists to prevent.
    """
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    values[key] = val
    except OSError:
        return {}
    return values


def load_env_file(path=None, override=False):
    """Load .env into os.environ. Returns (loaded_path, keys_applied).

    override=False keeps real OS environment variables authoritative, so this is
    safe to call in production: if the platform supplies the variables, nothing
    from a stray .env can displace them.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return None, []

    # Prefer python-dotenv when installed; fall back to the parser above.
    try:
        from dotenv import dotenv_values  # type: ignore
        values = dotenv_values(path) or {}
    except Exception:
        values = _parse_env_file(path)

    applied = []
    for key, val in values.items():
        if val is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
            applied.append(key)
    return path, applied


class ConfigError(RuntimeError):
    """Configuration is invalid in a way that would cause confusing behavior."""


def resolve_api_mode(raw=None, strict=True):
    """Normalize PERCH_API_MODE and refuse silently-confusing values.

    Unset -> "mock", which is correct for tests and local work, and is now
    printed loudly at startup rather than being invisible.

    A value that is neither mock nor live (a typo such as "LIVE " or "prod")
    raises rather than silently degrading to mock - that degradation is exactly
    what produced the 501.
    """
    if raw is None:
        raw = os.environ.get("PERCH_API_MODE")
    if raw is None or str(raw).strip() == "":
        return "mock", True  # (mode, defaulted)
    mode = str(raw).strip().lower()
    if mode in VALID_API_MODES:
        return mode, False
    if strict:
        raise ConfigError(
            f"PERCH_API_MODE={raw!r} is not valid. Use exactly 'mock' or 'live'. "
            f"Refusing to start rather than silently falling back to mock - a "
            f"silent fallback previously caused a live reconciliation call to "
            f"return 501 'Not implemented by this client.'")
    return "mock", True


def _host_of(url):
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.netloc or None
    except Exception:
        return None


def perch_config_report():
    """Non-sensitive configuration summary. Never includes secret values."""
    mode, defaulted = resolve_api_mode(strict=False)
    enrollment_url = os.environ.get("PERCH_ENROLLMENT_BASE_URL")
    markets_url = os.environ.get("PERCH_MARKETS_BASE_URL")
    host = _host_of(enrollment_url)
    environment = "unknown"
    if host:
        low = host.lower()
        if "staging" in low or "dev" in low:
            environment = "staging"
        elif "perchenergy.com" in low:
            environment = "PRODUCTION"
    elif mode == "mock":
        environment = "n/a (mock fixtures)"
    return {
        "api_mode": mode,
        "api_mode_defaulted": defaulted,
        "enrollment_host": host,
        "markets_host": _host_of(markets_url),
        "environment": environment,
        # Presence only - values are never surfaced.
        "api_key_configured": bool(os.environ.get("PERCH_API_KEY")),
        "secret_key_configured": bool(os.environ.get("PERCH_SECRET_KEY")),
        "jwt_secret_configured": bool(os.environ.get("JWT_SECRET")),
    }


def format_startup_banner(report, env_path=None, applied_keys=None):
    """Human-readable startup banner. Contains no secret values."""
    mode = report["api_mode"].upper()
    lines = []
    lines.append("=" * 62)
    lines.append(f"  Perch mode        : {mode}"
                 + ("   (defaulted - PERCH_API_MODE was not set)"
                    if report["api_mode_defaulted"] else ""))
    lines.append(f"  Environment       : {report['environment']}")
    lines.append(f"  Enrollment host   : {report['enrollment_host'] or '(default / not set)'}")
    lines.append(f"  Markets host      : {report['markets_host'] or '(default / not set)'}")
    lines.append(f"  API key           : {'configured' if report['api_key_configured'] else 'NOT SET'}")
    lines.append(f"  Signing key       : {'configured' if report['secret_key_configured'] else 'NOT SET'}")
    if env_path:
        lines.append(f"  .env              : loaded ({len(applied_keys or [])} value(s) applied)")
    else:
        lines.append("  .env              : none found (using OS environment only)")

    if report["api_mode"] == "mock":
        lines.append("")
        lines.append("  >> MOCK MODE: no request will reach Perch. Fixtures only.")
        lines.append("     Live-only endpoints (GET /status, POST /enroll, /contracts,")
        lines.append("     /contracts/accept) will raise PerchNotImplementedError.")
    else:
        missing = [k for k, ok in (("PERCH_API_KEY", report["api_key_configured"]),
                                    ("PERCH_SECRET_KEY", report["secret_key_configured"]))
                   if not ok]
        if missing:
            lines.append("")
            lines.append(f"  >> WARNING: LIVE mode but {', '.join(missing)} not set.")
            lines.append("     Perch calls will fail authentication.")
        if report["environment"] == "PRODUCTION":
            lines.append("")
            lines.append("  >> PRODUCTION host detected. Calls affect real customers.")
    lines.append("=" * 62)
    return "\n".join(lines)


def init_configuration(verbose=True, strict=True):
    """Load .env (OS env wins), validate the mode, and report. Returns the report."""
    env_path, applied = load_env_file()
    mode, _ = resolve_api_mode(strict=strict)   # raises on an invalid value
    os.environ["PERCH_API_MODE"] = mode          # normalized for everything downstream
    report = perch_config_report()
    if verbose:
        print(format_startup_banner(report, env_path, applied))
    return report
