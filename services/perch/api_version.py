"""
Which Perch API version this environment talks to.

A module of its own purely to break an import cycle. The version flag was
defined in config.py (E1), but config.py imports both client implementations
for its factory - including mock_client, which from E2 needs to know the
version so it can emit the shape that version actually returns. mock_client
importing config would close the loop.

So the flag lives here, where nothing else does, and config.py re-exports it:
`config.get_api_version()` keeps working for every existing caller, and
mock_client imports from here instead.

Nothing in this module touches credentials or base URLs - that is still
config.py's job.
"""
import os

API_V1 = "v1"
API_V2 = "v2"


def get_api_version() -> str:
    """Which version of the ENROLLMENT endpoints to call. v1 unless asked.

    Deliberately defaulting to v1: v2 is opt-in per environment, so staging
    can run it while production stays where it is until explicitly moved.

    Read from the environment on EVERY call rather than cached at import. A
    cached value would freeze whatever happened to be set when the process
    started, and tests flip this between cases.
    """
    v = (os.environ.get("PERCH_API_VERSION") or API_V1).strip().lower()
    return API_V2 if v in (API_V2, "2") else API_V1
