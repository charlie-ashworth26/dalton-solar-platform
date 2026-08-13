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
"""
import os

from services.perch.client import PerchHTTPClient
from services.perch.mock_client import PerchMockClient

MODE_MOCK = "mock"
MODE_LIVE = "live"

# Documented production base URLs. Staging hosts differ and are supplied by env.
DEFAULT_ENROLLMENT_BASE_URL = "https://api.perchenergy.com/affiliate_partners/v1/enrollments"
DEFAULT_MARKETS_BASE_URL = "https://api.perchenergy.com/affiliate_partners/v1/markets"


def get_api_mode() -> str:
    mode = (os.environ.get("PERCH_API_MODE") or MODE_MOCK).strip().lower()
    return MODE_LIVE if mode == MODE_LIVE else MODE_MOCK


def get_perch_client():
    """Factory. Returns whichever PerchClient implementation the environment
    selects. Callers must not care which one they got."""
    if get_api_mode() == MODE_LIVE:
        return PerchHTTPClient(
            enrollment_base_url=os.environ.get("PERCH_ENROLLMENT_BASE_URL", DEFAULT_ENROLLMENT_BASE_URL),
            markets_base_url=os.environ.get("PERCH_MARKETS_BASE_URL", DEFAULT_MARKETS_BASE_URL),
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
