"""
Read-only market-rules API.

Exposes the per-market values the application must not hardcode - utility
slugs, account-field patterns, counties, income limits and self-attestation
options - to the authenticated frontend.

WHY A SEPARATE BLUEPRINT
    routes/perch_routes.py is ~76 KB and carries the entire enrollment workflow:
    the commit boundary, step reconciliation, the self-attestation chain. New
    York runs through it in production. Adding market lookups there would put
    new code inside the file with the highest regression cost in the repo, for
    no benefit. This blueprint touches nothing that exists.

EVERY ROUTE HERE IS A READ
    GET only. No route creates, updates or advances an enrollment, and none
    accepts an enrollment id or an X-Enrollment-Token. These are HMAC
    pre-enrollment lookups; they have no session and no side effects.

AUTH
    Staff only. The data is not secret, but an unauthenticated proxy to Perch
    would let anyone spend our HMAC request budget, and it would make UBS a
    public mirror of Perch's market configuration.

STATUS CODES
    200 - value returned, with `source` saying where it came from
    400 - the caller sent an unknown or missing state / county / utility
    503 - Perch market data is unavailable and there is no safe fallback

    400 vs 503 matters: 400 means "fix your input", 503 means "nothing is wrong
    with your input, try later". Collapsing them would send a rep hunting for a
    typo during a Perch outage.
"""
from flask import current_app, Blueprint, request, jsonify

from auth import require_auth, require_role
from services import zip_state
from services.perch import markets, utilities
from services import rep_errors
from services.perch.errors import PerchValidationError

bp = Blueprint("market_routes", __name__, url_prefix="/api/markets")

_STAFF = ("sales_rep", "admin")


def _handled(fetch, unavailable=None):
    """Run a lookup and map its failures onto the status codes above.

    `unavailable` is the sentence a REP should read when the lookup fails -
    each caller passes the one that fits what the rep was doing, because
    "we couldn't load the county information" is wrong and confusing on the
    step where the rep is entering an account number.
    """
    try:
        return jsonify(fetch())
    except PerchValidationError as e:
        return jsonify({"error": rep_errors.rep_message(
            e, rep_errors.VALIDATION_FAILED)}), 400
    except markets.MarketDataUnavailable as e:
        # Deliberately NOT 500: nothing is broken in UBS, and the frontend
        # should tell the rep to retry rather than render an empty dropdown.
        # D-2: the rep reads a sentence they can act on. str(e) carries the
        # vendor name and the upstream phrase ("county not found: 'Atlantis'"),
        # which is diagnostic, not guidance - it stays in the log line below.
        current_app.logger.warning("market data unavailable: %s", e)
        return jsonify({"error": unavailable or rep_errors.MARKET_DATA_UNAVAILABLE,
                        "market_data_unavailable": True}), 503


@bp.route("/utilities", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def list_utilities():
    """GET /api/markets/utilities?state=VA

    -> {"state", "utilities": [slug, ...], "source"}

    The returned slug is what must be sent as `utility_name`. UBS never derives
    a slug from a display name or a ZIP for a new market.
    """
    state = request.args.get("state")
    return _handled(lambda: markets.utilities(state),
                    rep_errors.UTILITY_LIST_UNAVAILABLE)


@bp.route("/utility-account-fields", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def utility_account_fields():
    """GET /api/markets/utility-account-fields?utility=<slug>

    -> {"utility_name", "fields": [{field_type, field_name, help_text,
        patterns}], "source"}

    `fields` always starts with account_number. meter_number and
    secondary_account_identifier appear ONLY when the utility requires them, so
    their absence is the answer to "is this field needed?".
    """
    utility = request.args.get("utility")
    return _handled(lambda: markets.utility_account_fields(utility),
                    rep_errors.ACCOUNT_FIELDS_UNAVAILABLE)


@bp.route("/counties", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def list_counties():
    """GET /api/markets/counties?state=VA

    -> {"state", "counties": [name, ...], "source"}

    `source` is "perch", or "fallback_db" when Perch was unreachable and the
    local NY reference table answered instead. The frontend can surface that
    distinction; it must not silently treat a fallback as authoritative for a
    market that has no local data.

    Names are returned verbatim. Independent cities keep their lowercase "city"
    suffix - do not normalize them anywhere downstream.
    """
    state = request.args.get("state")
    return _handled(lambda: markets.counties(state))


@bp.route("/county-income-limits", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def county_income_limits():
    """GET /api/markets/county-income-limits?state=VA&county=<name>

    -> {"state", "county", "income_limits": [{house_occupancy,
        household_income}], "source"}

    `county` in the response is Perch's stored spelling, which may differ in
    case from what was sent. `household_income` is a float dollar amount, not
    cents.
    """
    state = request.args.get("state")
    county = request.args.get("county")
    return _handled(lambda: markets.county_income_limits(state, county))


@bp.route("/self-attestation-options", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def self_attestation_options():
    """GET /api/markets/self-attestation-options?state=VA

    -> {"state", "source_types", "required_source_types", "single_source_type",
        "lmi_programs", "programs", "status", "sends_status", "source"}

    `sends_status` is the derived rule, computed from `status` and nothing else:
    non-empty means a status value must be sent, empty means the key is omitted
    entirely. It is included so the frontend reads one boolean instead of
    re-deriving the rule, and so no caller is tempted to branch on the state
    name.
    """
    state = request.args.get("state")

    def fetch():
        opts = markets.self_attestation_options(state)
        return dict(opts, sends_status=bool(opts["status"]))

    return _handled(fetch)


@bp.route("/utility-options", methods=["GET"])
@require_auth
@require_role(*_STAFF)
def utility_options():
    """GET /api/markets/utility-options[?zip=NNNNN]

    Serves BOTH Availability flows from one endpoint.

    WITHOUT `zip` (utility-first) - every utility across every enabled market,
    so the rep can pick one before typing a ZIP. Each option carries the state
    it belongs to, which is what lets the screen check compatibility later
    without a second call. No auto-select: a union is never "exactly one
    choice" in a meaningful sense, even when only one market is enabled.

    WITH `zip` (ZIP-first) - narrowed to the state that ZIP resolves to, with
    `auto_select` set when that state has exactly one utility.

    -> {"zip", "state", "markets", "options": [{value, label, state}],
        "auto_select", "source"}

    ZIP DETERMINES STATE ONLY. It never selects a utility: the choices always
    come from GET /markets/utilities for a state. ZIP-to-utility mapping
    remains deferred and is implemented nowhere.

    Which markets exist is UBS configuration (markets.enabled_markets); which
    utilities each one has is Perch's answer, fetched at runtime. Nothing here
    names a utility.

    Status codes:
      200 - answered, including the legitimate negative answers (ZIP not
            matched to a state, or a state with no utilities). Those carry a
            `message` for the screen and empty options, which is what stops
            Availability continuing.
      400 - `zip` was supplied but is malformed
      503 - market data is unavailable for the ONE state being asked about.
            The union never 503s on a single market's failure: a market that
            cannot be read is omitted and named in `unavailable_markets`, so
            one bad market does not empty the whole dropdown.
    """
    raw = (request.args.get("zip") or "").strip()
    enabled = markets.enabled_markets()

    # -- utility-first: the union across enabled markets --------------------
    if not raw:
        options, sources, unavailable = [], [], []
        for state in enabled:
            try:
                slugs = markets.utilities(state)["utilities"]
                sources.append("perch")
            except markets.MarketDataUnavailable:
                slugs = _local_fallback_slugs(state)
                if slugs is None:
                    unavailable.append(state)
                    continue
                sources.append("local_table")
            except PerchValidationError:
                unavailable.append(state)
                continue
            for slug in slugs:
                options.append({"value": slug,
                                "label": utilities.display_name_for_slug(slug),
                                "state": state})
        # Labels are clean human names; the VALUE stays the exact Perch
        # slug. This only appends a state when two markets would
        # otherwise show the SAME label, which is the one case where a
        # clean label becomes an ambiguous one.
        utilities.label_for_options(options)
        payload = {
            "zip": None, "state": None, "markets": enabled,
            "options": options,
            # Never preselect from a union - the rep has expressed no location
            # yet, so there is nothing to infer from.
            "auto_select": None,
            "source": "+".join(sorted(set(sources))) or "none",
        }
        if unavailable:
            payload["unavailable_markets"] = unavailable
        if not options:
            payload["message"] = ("No supported utilities could be loaded. "
                                  "Enter a ZIP code or try again shortly.")
        return jsonify(payload)

    # -- ZIP-first: narrowed to the ZIP's state -----------------------------
    if len(raw) != 5 or not raw.isdigit():
        return jsonify({"error": "Enter a 5-digit ZIP code."}), 400

    state = zip_state.state_for_zip(raw)
    if not state:
        return jsonify({
            "zip": raw, "state": None, "markets": enabled, "options": [],
            "auto_select": None, "source": "zip_prefix",
            "message": "That ZIP code could not be matched to a state, so the "
                       "supported utilities cannot be looked up.",
        })

    # THE MARKET GATE. Resolving a ZIP to a state says where the customer is,
    # not that UBS sells there. Without this, a ZIP in an unreleased market
    # would reach markets.utilities() directly, Perch would answer for it, and
    # the rep would be offered a utility this deployment does not enroll -
    # bypassing enabled_markets() entirely. The union path above is gated by
    # construction because it iterates the enabled list; this path has to say
    # so explicitly.
    if state not in enabled:
        return jsonify({
            "zip": raw, "state": state, "markets": enabled, "options": [],
            "auto_select": None, "source": "not_enabled",
            "message": f"UBS does not currently enroll customers in {state}.",
        })

    try:
        found = markets.utilities(state)
        slugs = found["utilities"]
        source = found["source"]
    except PerchValidationError as e:
        return jsonify({"error": str(e), "zip": raw, "state": state}), 400
    except markets.MarketDataUnavailable as e:
        slugs = _local_fallback_slugs(state)
        if slugs is None:
            current_app.logger.warning("utility list unavailable for %s: %s", state, e)
            return jsonify({"error": rep_errors.UTILITY_LIST_UNAVAILABLE,
                            "market_data_unavailable": True,
                            "zip": raw, "state": state}), 503
        source = "local_table"

    options = [{"value": s, "label": utilities.display_name_for_slug(s),
                "state": state} for s in slugs]
    payload = {
        "zip": raw,
        "state": state,
        "markets": enabled,
        "options": options,
        # Exactly one -> preselect. More than one -> the rep decides.
        "auto_select": slugs[0] if len(slugs) == 1 else None,
        "source": source,
    }
    if not options:
        payload["message"] = (
            f"No community solar utilities are currently available in {state}. "
            "This enrollment cannot continue.")
    return jsonify(payload)


def _local_fallback_slugs(state):
    """Locally-known utility slugs for a state, or None when there are none.

    perch_utilities is New York's reference table - every row was seeded from
    Perch's published NY slug mapping, and it is the source the Availability
    screen used before market rules existed. So when market data is
    unavailable, an NY rep sees exactly the dropdown they saw yesterday and
    Availability keeps working through a Perch outage.

    Every other market has no local reference data, so it gets None and fails
    closed: offering New York's utilities for an out-of-state ZIP would be
    worse than offering none. Same deliberate single-state case, for the same
    reason, as the county fallback in services/perch/markets.py.
    """
    if state != "NY":
        return None
    return [o["value"] for o in utilities.select_options()]


@bp.route("/cache", methods=["GET"])
@require_auth
@require_role("admin")
def cache_stats():
    """GET /api/markets/cache - admin diagnostics.

    Counters only: hit/miss/fetch/failure/fallback totals and entry counts.
    Never the cached values themselves, so this cannot be used to read market
    data without the authorization the other routes require.
    """
    return jsonify(markets.stats())


@bp.route("/cache", methods=["DELETE"])
@require_auth
@require_role("admin")
def cache_clear():
    """DELETE /api/markets/cache - drop cached market rules.

    For the case where Perch publishes a correction and waiting out the TTL is
    not acceptable. Affects only this process's in-memory cache: no enrollment,
    no stored record and no Perch state is touched.
    """
    markets.invalidate()
    return jsonify({"cleared": True, "stats": markets.stats()})
