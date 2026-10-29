"""
Which PRODUCTS a market may offer.

A product decision, not market data, and deliberately in a module of its own.

WHY NOT IN adapter.py OR markets.py
    Both are guarded - by test_virginia_enrollment.py and
    test_market_rules_service.py - against holding a state-name comparison.
    That guard is right and is not being worked around here. Its purpose is
    that the REQUEST PATH must never branch on a state name: Phase 3's rule is
    "read the answer from the market, never compare state names", which is why
    _sends_status() asks self_attestation_options(state) instead of testing
    for "NY".

    This module is the source such a question is answered FROM, not a branch
    in the path. adapter.py asks "may this market offer non-income-qualified
    programs?" exactly as perch_routes.py asks "does this market send status?"
    - neither compares a state name; both read an answer. The difference is
    only where the answer comes from, and that difference is forced: Perch
    publishes capacity, not which of it a partner is permitted to sell.

WHY IT EXISTS
    Real Perch staging for Dominion / ZIP 22401, read on 2026-10-28 by the E4
    read-only pass, returns:

        residential               unavailable
        lmi                       AVAILABLE, 10%
        lmi_ira                   unavailable
        mass_market               AVAILABLE, 10%
        commercial_and_industrial unavailable

    The v2 spec allows a `Residential` customer onto residential OR mass
    market capacity, so UBS's generic mapping reported a Residential program -
    available on mass-market capacity, with no published rate of its own.
    Correct about Perch, wrong about our product: Virginia's Dominion
    programme is state LMI, and a Virginia rep must not be offered a non-LMI
    residential enrollment.

WHAT IT DOES AND DOES NOT DO
    It withholds a PROGRAM from the rep. It does not touch the capacity model,
    does not alter what Perch returned, does not invent a savings rate, and
    does not change what mass-market capacity means anywhere else - New York's
    Central Hudson and Orange & Rockland both return residential=false with
    mass_market=true and remain standard residential enrollments.

IT NAMES THE MARKETS IT RESTRICTS
    A market UBS cannot identify carries no restriction. Restricting an
    unidentified market would be inventing a rule rather than applying one,
    and would take New York's residential programme down the moment a ZIP
    failed to resolve.

    That is the OPPOSITE default from markets.enabled_markets(), deliberately.
    That one decides whether a market is served at all, where silence must
    mean "no". This one decides which products an already-served market
    offers, where silence must mean "no special restriction".
"""
import os

# Markets where only income-qualified programs may be sold.
#
# Override with UBS_LMI_ONLY_MARKETS (comma or semicolon separated). An
# explicitly empty value means "no market is restricted" and is honoured as
# written - unlike enabled_markets(), where an empty value falls back to the
# default, because there the failure mode is taking a live market down and
# here it is only removing a restriction that an operator asked to remove.
DEFAULT_LMI_ONLY_MARKETS = "VA"

ENV_VAR = "UBS_LMI_ONLY_MARKETS"


def income_qualified_only_markets():
    """The set of states that may offer income-qualified programs only."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        raw = DEFAULT_LMI_ONLY_MARKETS
    out = set()
    for part in raw.replace(";", ",").split(","):
        code = part.strip().upper()
        if len(code) == 2 and code.isalpha():
            out.add(code)
    return frozenset(out)


def is_income_qualified_only(state):
    """Whether this market may offer income-qualified programs only.

    An unknown or unresolvable market returns False - no restriction. See the
    module docstring for why that is the right default here and the wrong one
    for enabled_markets().
    """
    code = (state or "").strip().upper()
    return bool(code) and code in income_qualified_only_markets()
