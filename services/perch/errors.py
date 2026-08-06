"""Typed Perch failures, so routes translate errors uniformly regardless of
whether the mock or the live client raised them.

Milestone 2 note: Perch uses HTTP status codes as BUSINESS semantics, not just
transport semantics. 503 means "no capacity here" and 403 means "your token
aged out, refresh and retry" - neither is an error to show a rep as a failure.
The classes below encode that distinction so routes don't have to.
"""


class PerchError(Exception):
    """Base for everything this package raises."""
    http_status = 502  # Bad Gateway - upstream problem, by default


class PerchAuthError(PerchError):
    """Token acquisition failed, or credentials were rejected outright.
    Not recoverable by refreshing."""
    http_status = 502


class PerchTokenExpiredError(PerchError):
    """Documented 403: the enrollment_token is expired or invalid.

    Perch's own guidance: call PATCH /refresh_token, then retry the request.
    The adapter does this automatically, once. This should almost never reach a
    route - if it does, refresh-and-retry itself failed.
    """
    http_status = 401


class PerchUnavailableError(PerchError):
    """Network failure, timeout, or a 5xx that is NOT the documented 503."""
    http_status = 503


class PerchValidationError(PerchError):
    """Perch rejected our request (4xx) - bad ZIP, bad utility slug, bad POD ID."""
    http_status = 400


class PerchNoCapacityError(PerchError):
    """Documented 503 on POST /capacity: no open solar project capacity exists
    for this utility and ZIP.

    This is a BUSINESS OUTCOME, not a failure. The adapter converts it into a
    structured "capacity_available: false" result rather than letting it surface
    as an error. Perch's guidance: do not proceed to POST /enroll.
    """
    http_status = 200


class PerchNotImplementedError(PerchError):
    """We deliberately have not implemented this call because the contract is
    not published. Raised instead of guessing. See PERCH_OPEN_ITEMS.md."""
    http_status = 501


class PerchNotFoundError(PerchError):
    """Documented 404. On PATCH /refresh_token this means Perch has no
    in-progress enrollment for that email - so there is nothing to resume and
    the caller must start a fresh POST /token instead."""
    http_status = 404
