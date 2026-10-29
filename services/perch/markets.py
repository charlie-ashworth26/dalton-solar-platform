"""
Market-rules cache and fallback layer.

WHAT THIS IS FOR
    Per-market rules - utility slugs, account-number formats, counties, income
    limits, self-attestation options - are NOT in the OpenAPI document. The spec
    is explicit:

        "Rules that differ by state or utility ... are not listed in this
         document. Fetch them from the GET /markets/* endpoints."

    So this module is the single place the application asks for those values. No
    caller anywhere hardcodes a utility slug, an account-number pattern, a
    county name or a status array. They are read from Perch at runtime and
    cached here.

WHY A CACHE
    The values change rarely (a state's county list essentially never), but they
    are needed on nearly every screen. Without a cache, picking a county would
    mean an HMAC-signed round trip per keystroke, and a Perch outage would stall
    the rep. The cache is process-local and in-memory: no new table, no
    migration, nothing to keep in sync across deployments.

WHAT "SAFE FALLBACK" MEANS HERE
    New York is live in production and must not regress, so a market-data
    outage must never break an NY enrollment. NY county lookups therefore fall
    back to the `ny_counties` reference table that has always backed them, and
    the result says so via `source`.

    Every other market fails CLOSED: callers get MarketDataUnavailable rather
    than an empty list or an invented value. An empty county dropdown is a
    confusing bug; an explicit "unavailable" is an honest one. Virginia in
    particular has no local reference data and must never appear to.

MOCK MODE
    PerchMockClient does not implement the market endpoints, so in mock mode
    everything here raises MarketDataUnavailable except the NY county fallback.
    That is deliberate: the mock must not fabricate market data that would then
    disagree with staging. Tests inject a stub client via set_client_factory().

THREAD SAFETY
    Flask serves concurrently, so the cache is guarded by a reentrant lock. The
    lock is held only around dict access, never across a network call, so a slow
    Perch response cannot block other readers.
"""
import os
import threading
import time

from services.perch.config import get_perch_client
from services.perch.errors import (
    PerchError, PerchNotImplementedError, PerchUnavailableError,
    PerchValidationError,
)


class MarketDataUnavailable(Exception):
    """Perch market data could not be obtained and there is no safe fallback.

    Deliberately NOT a PerchError: callers must not confuse "we could not read
    the rules" with "Perch rejected the enrollment". The first is a degraded
    read; the second is a business outcome.
    """


# Market rules are near-static. 15 minutes keeps a county list fresh enough
# while making repeated screen loads free.
def _ttl():
    try:
        return int(os.environ.get("PERCH_MARKETS_CACHE_TTL_SECONDS", "900"))
    except (TypeError, ValueError):
        return 900


# A transport failure is cached BRIEFLY so a Perch outage does not turn one rep
# refreshing a page into a 20-second stall per click. Validation failures (an
# unknown state, a mistyped county) are NOT cached: they are deterministic and
# cheap, and a rep who corrects a typo must get an immediate answer rather than
# the stale rejection of their previous attempt.
def _negative_ttl():
    try:
        return int(os.environ.get("PERCH_MARKETS_NEGATIVE_TTL_SECONDS", "30"))
    except (TypeError, ValueError):
        return 30


_lock = threading.RLock()
_cache = {}            # key -> (expires_at, value)
_negative = {}         # key -> (expires_at, message)
_stats = {"hits": 0, "misses": 0, "fetches": 0, "failures": 0, "fallbacks": 0}

# Test seam. Production never sets this, so get_perch_client() is used.
_client_factory = None


def set_client_factory(factory):
    """Inject a client factory (tests only). Pass None to restore the default."""
    global _client_factory
    with _lock:
        _client_factory = factory
        _cache.clear()
        _negative.clear()


def _client():
    return (_client_factory or get_perch_client)()


def invalidate(prefix=None):
    """Drop cached entries. No argument clears everything."""
    with _lock:
        if prefix is None:
            _cache.clear()
            _negative.clear()
            return
        for store in (_cache, _negative):
            for k in [k for k in store if k[0] == prefix]:
                store.pop(k, None)


def stats():
    """Cache counters, for diagnostics. Never includes market values."""
    with _lock:
        return dict(_stats, entries=len(_cache), negative_entries=len(_negative))


def _cached(key, fetch):
    """Return a cached value, or fetch and cache it.

    `key` is a tuple whose first element names the endpoint, so invalidate()
    can target one endpoint. The network call happens OUTSIDE the lock.
    """
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _stats["hits"] += 1
            return hit[1]
        neg = _negative.get(key)
        if neg and neg[0] > now:
            _stats["hits"] += 1
            raise MarketDataUnavailable(neg[1])
        _stats["misses"] += 1

    try:
        with _lock:
            _stats["fetches"] += 1
        value = fetch()
    except PerchValidationError:
        # The caller sent something Perch does not recognise (unknown state,
        # county or utility). Deterministic, so never cached, and re-raised
        # unchanged so the route can answer 400 rather than 503.
        raise
    except PerchNotImplementedError as e:
        # Mock mode. Not a failure worth negative-caching; it will never
        # succeed until the client changes.
        with _lock:
            _stats["failures"] += 1
        raise MarketDataUnavailable(
            "Market rules are not available from the current Perch client "
            f"(mode does not implement them): {e}") from e
    except (PerchUnavailableError, PerchError) as e:
        msg = f"Perch market data is temporarily unavailable: {e}"
        with _lock:
            _stats["failures"] += 1
            _negative[key] = (time.time() + _negative_ttl(), msg)
        raise MarketDataUnavailable(msg) from e

    with _lock:
        _cache[key] = (time.time() + _ttl(), value)
    return value


def enabled_markets():
    """The states UBS currently offers enrollments in, in display order.

    Perch publishes no "list my markets" endpoint - every /markets/* route
    answers for ONE state you already named - so the set of markets UBS serves
    is a product decision and has to live on this side. It is configuration,
    not market data: which utilities each market has is still Perch's answer,
    fetched at runtime and never written down here.

    FAILS CLOSED. The default is New York alone - the only market UBS serves in
    production today. A market is added by NAMING it in UBS_MARKETS, never by
    the absence of configuration, so an environment that forgets the variable
    (a fresh deploy, a restored service, a new worker, a shell that did not
    source the env file) offers exactly the live market and nothing more.
    Virginia is enabled where it is being tested, by setting UBS_MARKETS=NY,VA
    there.

    The inverse default would be wrong in precisely the way that matters: a
    missing variable would silently expose an unreleased market to every rep,
    and nothing in the request path would reveal it.

    Set UBS_MARKETS to change it (comma or semicolon separated, e.g.
    "NY,VA,MA"). Anything that is not a two-letter alphabetic code is ignored
    rather than guessed at, and duplicates collapse while order is preserved,
    because this order is the order the rep sees.
    """
    DEFAULT_MARKETS = "NY"
    raw = (os.environ.get("UBS_MARKETS") or DEFAULT_MARKETS)
    seen, out = set(), []
    for part in raw.replace(";", ",").split(","):
        st = part.strip().upper()
        if len(st) == 2 and st.isalpha() and st not in seen:
            seen.add(st)
            out.append(st)
    # A value that parses to nothing - whitespace, punctuation, "none" - is a
    # misconfiguration, and treating it as "serve no markets" would take the
    # LIVE market down while looking like a deliberate setting. Fall back to
    # the default, which can only ever re-add New York: this line cannot
    # introduce a market that was not named, so it fails closed exactly like
    # the default itself.
    return out or [DEFAULT_MARKETS]


def _norm_state(state):
    s = (state or "").strip().upper()
    if len(s) != 2 or not s.isalpha():
        raise PerchValidationError(
            "A two-letter state abbreviation is required.")
    return s


# ───────────────────────────── the five lookups ─────────────────────────────

def utilities(state):
    """Utility slugs for a state.

    Returns {"state", "utilities": [slug, ...], "source"}. The slugs are the
    ONLY sanctioned values for `utility_name`.
    """
    st = _norm_state(state)
    body = _cached(("utilities", st), lambda: _client().get_market_utilities(st))
    return {"state": body.get("state", st),
            "utilities": list(body.get("utilities") or []),
            "source": "perch"}


def utility_account_fields(utility_slug):
    """Required account fields and their validation patterns for one utility.

    Returns {"utility_name", "fields": [...], "source"}. Each field carries
    `field_type`, `field_name`, `help_text` and `patterns`.
    """
    slug = (utility_slug or "").strip()
    if not slug:
        raise PerchValidationError("A utility slug is required.")
    body = _cached(("utility_account_fields", slug),
                   lambda: _client().get_market_utility_account_fields(slug))
    return {"utility_name": body.get("utility_name", slug),
            "fields": list(body.get("fields") or []),
            "source": "perch"}


def counties(state):
    """County names for a state, exactly as Perch stores them.

    Returns {"state", "counties": [name, ...], "source"} where `source` is
    "perch" normally, or "fallback_db" when Perch was unreachable and a local
    reference table could answer (New York only - see the module docstring).

    Names are never normalized. Virginia mixes counties with independent cities
    spelled with a lowercase "city" suffix, and Perch validates against the
    exact string it gave us.
    """
    st = _norm_state(state)
    try:
        body = _cached(("counties", st), lambda: _client().get_market_counties(st))
        return {"state": body.get("state", st),
                "counties": list(body.get("counties") or []),
                "source": "perch"}
    except MarketDataUnavailable:
        fallback = _county_fallback(st)
        if fallback is None:
            raise
        with _lock:
            _stats["fallbacks"] += 1
        return {"state": st, "counties": fallback, "source": "fallback_db"}


def county_income_limits(state, county):
    """Household income limit per household size (1-8) for one county.

    Returns {"state", "county", "income_limits": [...], "source"}.
    `household_income` stays the float dollar amount Perch sent; it is not
    converted to cents here.
    """
    st = _norm_state(state)
    name = (county or "").strip()
    if not name:
        raise PerchValidationError("A county name is required.")
    body = _cached(("county_income_limits", st, name),
                   lambda: _client().get_market_county_income_limits(st, name))
    return {"state": body.get("state", st),
            # Perch echoes the name AS STORED, which may differ in case from
            # what we sent. Prefer Perch's spelling.
            "county": body.get("county", name),
            "income_limits": list(body.get("income_limits") or []),
            "source": "perch"}


def self_attestation_options(state):
    """What a state's self-attestation payload must contain.

    Returns Perch's fields plus "source". The important one is `status`: when
    it is non-empty a `status` value must be sent, and when it is empty the key
    must be omitted entirely. See sends_status().
    """
    st = _norm_state(state)
    body = _cached(("self_attestation_options", st),
                   lambda: _client().get_market_self_attestation_options(st))
    return {
        "state": body.get("state", st),
        "source_types": list(body.get("source_types") or []),
        "required_source_types": list(body.get("required_source_types") or []),
        "single_source_type": bool(body.get("single_source_type")),
        "lmi_programs": body.get("lmi_programs"),
        "programs": list(body.get("programs") or []),
        "status": list(body.get("status") or []),
        "source": "perch",
    }


# ───────────────────────────── derived helpers ─────────────────────────────

def sends_status(state):
    """True when this state's self-attestation payload includes `status`.

    Spec: "when not empty, send one of these values as status; otherwise do not
    send status."

    Driven entirely by the API response - it never compares state names. New
    York returns ["accepted", "rejected"] so it keeps sending status; Virginia
    returns [] so the key is omitted. A third market needs no code change.

    NOT yet wired into the enrollment path: that is a later phase. It is defined
    and tested here so the rule exists in exactly one place when it is.
    """
    return bool(self_attestation_options(state)["status"])


def status_values(state):
    """The accepted `status` values for a state. Empty list means omit the key."""
    return list(self_attestation_options(state)["status"])


def account_field(utility_slug, field_type):
    """One field definition for a utility, or None when it is not required.

    Spec: `fields` always starts with account_number, and includes
    meter_number / secondary_account_identifier ONLY for utilities that require
    them. So None means "this utility does not require this field".
    """
    for f in utility_account_fields(utility_slug)["fields"]:
        if f.get("field_type") == field_type:
            return f
    return None


def field_is_required(utility_slug, field_type):
    """True when a utility requires this field at all."""
    return account_field(utility_slug, field_type) is not None


def validate_account_value(utility_slug, field_type, value):
    """Validate a value against the utility's own patterns. Returns an error
    string, or None when valid.

    Spec: "A value is valid when it matches ANY ONE of the field's patterns. An
    empty patterns list means the field has no format rule." account_number
    patterns are matched case-insensitively; the others case-sensitively.

    Returns None when the utility does not require the field, so a caller can
    validate optional input without first asking whether it applies.

    This is additive. The existing NY POD-ID rules in services/perch/utilities.py
    are untouched and still govern the NY path.
    """
    import re
    field = account_field(utility_slug, field_type)
    if field is None:
        return None
    raw = (value or "").strip()
    patterns = list(field.get("patterns") or [])
    label = field.get("field_name") or field_type.replace("_", " ")
    if not raw:
        return f"{label} is required for this utility."
    if not patterns:
        return None          # no format rule published
    flags = re.IGNORECASE if field_type == "account_number" else 0
    for pat in patterns:
        try:
            if re.fullmatch(pat, raw, flags):
                return None
        except re.error:
            # A malformed pattern from upstream must not 500 the request. Skip
            # it; another pattern may still match.
            continue
    hint = field.get("help_text")
    return (f"{label} does not match the format this utility requires"
            + (f" ({hint})." if hint else "."))


# ───────────────────────────── NY-only fallback ─────────────────────────────

def _county_fallback(state):
    """Local county list for a state, or None when there is no local source.

    Only New York has one: the `ny_counties` reference table, which has backed
    the NY self-attestation county dropdown since migration 009 and is the
    reason an NY enrollment can survive a market-data outage.

    This is intentionally a single named case rather than a generic mechanism.
    Inventing a per-state fallback table would create a second source of truth
    that can silently drift from Perch, which is exactly what the spec's
    fetch-at-runtime rule is designed to prevent. Virginia has no local list and
    must not acquire one.
    """
    if state != "NY":
        return None
    try:
        from db import query
        rows = query("SELECT name FROM ny_counties WHERE is_active = 1 ORDER BY name")
        names = [r["name"] for r in rows]
        return names or None
    except Exception:
        # No app context, no table, no database - there is simply no fallback.
        # Never raise from the fallback path; the caller's original
        # MarketDataUnavailable is the more useful error.
        return None
