"""
Perch configuration and client factory.

The ONLY place Perch credentials are read. They are never serialized into an API
response, never logged, and never sent to the browser.

Switching from the mock to Perch staging stays purely environmental:

    export PERCH_API_MODE=live
    export PERCH_ENROLLMENT_BASE_URL=https://<staging-host>/affiliate_partners/v1/enrollments
    export PERCH_MARKETS_BASE_URL=https://<staging-host>/affiliate_partners/v1/markets
    export PERCH_API_KEY=...
    export PERCH_SECRET_KEY=...        # HMAC shared secret

No route, service, test, or frontend file changes.

─────────────────────────── PERCH API v2 (E1) ───────────────────────────

Perch versions each endpoint on its own. The enrollment-session endpoints
exist on both v1 (deprecated, unchanged) and v2; the market lookups mostly
stay on v1, with ONE exception - market capacity, which lives at
`/v2/markets/capacity`, a sibling of `/v1/markets`, not a child of it.

That exception is why a single markets base URL is not enough: it points at
`.../v1/markets`, and no suffix of that reaches `.../v2/markets/capacity`.
So the configuration is expressed from the PARTNER ROOT
(`.../affiliate_partners`) and each family is derived from it:

    enrollments   ->  <root>/v1/enrollments   or  <root>/v2/enrollments
    market lookups->  <root>/v1               (utilities, counties, ...)
    market capacity-> <root>/v2/markets/capacity

Which enrollment version is used is environmental and defaults to v1:

    export PERCH_API_VERSION=v2        # opt in; v1 remains the default

NOTHING IS HARD-SWITCHED. With no new variables set, every base URL resolves
to exactly the v1 values this file used before, so an existing deployment is
byte-identical to what it ran yesterday. The explicit PERCH_ENROLLMENT_BASE_URL
and PERCH_MARKETS_BASE_URL overrides still win over everything, so a staging
host configured the old way keeps working untouched.
"""
import os

from services.perch.client import PerchHTTPClient
from services.perch.mock_client import PerchMockClient
# The version flag moved to its own module in E2 and is RE-EXPORTED here, so
# config.get_api_version(), config.API_V1 and config.API_V2 keep working for
# every existing caller. It had to move because the mock now needs the version
# too, and this module imports the mock - see services/perch/api_version.py.
from services.perch.api_version import (  # noqa: F401  - re-exported
    API_V1, API_V2, get_api_version,
)

MODE_MOCK = "mock"
MODE_LIVE = "live"

# Documented production base URLs. Staging hosts differ and are supplied by env.
DEFAULT_ENROLLMENT_BASE_URL = "https://api.perchenergy.com/affiliate_partners/v1/enrollments"
DEFAULT_MARKETS_BASE_URL = "https://api.perchenergy.com/affiliate_partners/v1/markets"
# The partner root everything else is derived from, when it is given.
DEFAULT_PARTNER_BASE_URL = "https://api.perchenergy.com/affiliate_partners"


def get_api_mode() -> str:
    mode = (os.environ.get("PERCH_API_MODE") or MODE_MOCK).strip().lower()
    return MODE_LIVE if mode == MODE_LIVE else MODE_MOCK


def get_partner_base_url() -> str:
    """The partner root, e.g. https://host/affiliate_partners.

    Taken from PERCH_PARTNER_BASE_URL when set. Otherwise derived from an
    existing PERCH_ENROLLMENT_BASE_URL by removing its trailing
    `/vN/enrollments`, so a deployment that only ever set the old variable
    still resolves the v2 locations correctly without new configuration."""
    explicit = (os.environ.get("PERCH_PARTNER_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    enrollment = (os.environ.get("PERCH_ENROLLMENT_BASE_URL") or "").strip().rstrip("/")
    for suffix in ("/v1/enrollments", "/v2/enrollments"):
        if enrollment.endswith(suffix):
            return enrollment[: -len(suffix)]
    markets = (os.environ.get("PERCH_MARKETS_BASE_URL") or "").strip().rstrip("/")
    for suffix in ("/v1/markets", "/v2/markets"):
        if markets.endswith(suffix):
            return markets[: -len(suffix)]
    return DEFAULT_PARTNER_BASE_URL


def get_enrollment_base_url() -> str:
    """Base URL for the enrollment-session endpoints, for the selected version.

    An explicit PERCH_ENROLLMENT_BASE_URL always wins - that is the escape
    hatch an existing environment relies on - EXCEPT when it names v1 while
    PERCH_API_VERSION asks for v2, where the version flag is what the operator
    just asked for and the path segment is swapped."""
    explicit = (os.environ.get("PERCH_ENROLLMENT_BASE_URL") or "").strip().rstrip("/")
    version = get_api_version()
    if explicit:
        if version == API_V2 and explicit.endswith("/v1/enrollments"):
            return explicit[: -len("/v1/enrollments")] + "/v2/enrollments"
        if version == API_V1 and explicit.endswith("/v2/enrollments"):
            return explicit[: -len("/v2/enrollments")] + "/v1/enrollments"
        return explicit
    return f"{get_partner_base_url()}/{version}/enrollments"


def get_markets_base_url() -> str:
    """Base URL for the market LOOKUPS, which stay on v1 in both versions."""
    explicit = (os.environ.get("PERCH_MARKETS_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"{get_partner_base_url()}/{API_V1}/markets"


def get_markets_capacity_base_url() -> str:
    """Base URL for market capacity ONLY.

    v1: `<root>/v1/markets` + `/capacity`
    v2: `<root>/v2/markets` + `/capacity`

    Kept apart from the lookup base because the two families diverge in v2 -
    this is the one market endpoint that moves."""
    override = (os.environ.get("PERCH_MARKETS_CAPACITY_BASE_URL") or "").strip().rstrip("/")
    if override:
        return override
    if get_api_version() == API_V2:
        return f"{get_partner_base_url()}/{API_V2}/markets"
    return get_markets_base_url()


def get_perch_client():
    """Factory. Returns whichever PerchClient implementation the environment
    selects. Callers must not care which one they got."""
    if get_api_mode() == MODE_LIVE:
        return PerchHTTPClient(
            enrollment_base_url=get_enrollment_base_url(),
            markets_base_url=get_markets_base_url(),
            # The one market endpoint that moves in v2. Passed separately so
            # the client never has to reason about versions itself.
            markets_capacity_base_url=get_markets_capacity_base_url(),
            api_version=get_api_version(),
            api_key=os.environ.get("PERCH_API_KEY", ""),
            secret_key=os.environ.get("PERCH_SECRET_KEY", ""),
            timeout=int(os.environ.get("PERCH_TIMEOUT_SECONDS", "20")),
        )
    return PerchMockClient()


# Refresh this far before actual expiry so a token cannot die mid-request.
# NEW YAML: Perch's TTL is 1 hour. 2 minutes of headroom is ~3% of the window.
# Kept at 120s deliberately rather than scaled up: the skew exists to absorb
# clock drift and in-flight latency, both of which are absolute, not
# proportional to the TTL.
TOKEN_REFRESH_SKEW_SECONDS = int(os.environ.get("PERCH_TOKEN_SKEW_SECONDS", "120"))
