"""
Utility slug translation and per-utility validation rules.

Perch matches utilities by slug (`consolidated-edison-ny`), never by display
name. A mismatch does not raise a helpful error — the capacity check simply
fails to find a project. This module is the single translation point, backed by
the perch_utilities reference table seeded in migration 002.

POD ID rules live here too. They are documented and stable, and the existing
Dalton bill screen now reveals the secondary identifier only when the selected
utility requires it. Validating
before the API call means a rep sees "NYSEG needs 15 digits, that's 14" instead
of an opaque rejection — which is exactly the example Perch walked through on
the engineering call.
"""
import re

from db import query, query_one


def all_utilities(active_only=True):
    sql = "SELECT * FROM perch_utilities"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY display_name"
    return [dict(r) for r in query(sql)]


def by_slug(slug):
    row = query_one("SELECT * FROM perch_utilities WHERE slug = ?", (slug,))
    return dict(row) if row else None


def by_display_name(name):
    row = query_one("SELECT * FROM perch_utilities WHERE display_name = ?", (name,))
    return dict(row) if row else None


def resolve_slug(value):
    """Accepts either a slug or a display name and returns the canonical slug.
    Returns None if neither matches — callers must treat that as a validation
    error rather than passing the raw value through to Perch."""
    if not value:
        return None
    value = value.strip()
    if by_slug(value):
        return value
    match = by_display_name(value)
    if match:
        return match["slug"]

    # Legacy Dalton project cards predate the Perch slug table and use the
    # shorter label "National Grid". This is a UI compatibility alias to the
    # published NY slug, not a guessed Perch value.
    aliases = {
        "national grid": "national-grid-ny",
        "con edison": "consolidated-edison-ny",
        "pseg": "pse-g-ny",
    }
    return aliases.get(value.lower())


def display_name_for_slug(slug):
    """A rep-facing label for a utility slug.

    Prefers the display_name in perch_utilities, so every existing NY utility
    keeps its exact established label ("Central Hudson Gas & Electric").

    For a slug that is NOT in the local table - a new market's utility, read
    from GET /markets/utilities at runtime - the label is derived GENERICALLY
    from the slug. No per-utility name is written into UBS.

    A TRAILING STATE CODE IS DROPPED, not upper-cased. Perch namespaces its
    slugs by state, which the API needs and a rep does not: the slug is the
    value we transmit, and the label is only ever for a human to read. Showing
    the state token made the screen read like the raw slug, which is the one
    thing a label must never do. The token is recognised by membership in the
    state table UBS already has (services/zip_state), so this names no state
    and no utility; a slug whose last token is not a state code keeps it.

        consolidated-edison-ny -> "Consolidated Edison NY"   (from the table,
                                                              left untouched)
        some-new-utility-xx    -> "Some New Utility"         (derived, XX dropped)
        some-utility-coop      -> "Some Utility Coop"        (not a state code)

    Two markets can have utilities whose names differ ONLY by that token, and
    the cross-market dropdown would then show the same label twice. Callers
    that build such a list disambiguate with label_for_options() below rather
    than putting the state back on every label.
    """
    row = by_slug(slug)
    if row and row.get("display_name"):
        return row["display_name"]
    parts = [p for p in str(slug or "").replace("_", "-").split("-") if p]
    if not parts:
        return str(slug or "")
    # Only ever drop the LAST token, and never the only one: a slug that is
    # just "va" is a utility named VA, not an empty label.
    if len(parts) > 1 and parts[-1].upper() in _STATE_CODES:
        parts = parts[:-1]
    return " ".join(p.upper() if len(p) == 2 else p.capitalize() for p in parts)


def _state_codes():
    """The state codes UBS knows, from the ZIP table it already maintains.

    Imported lazily so this module keeps no import-time dependency on
    zip_state, and falling back to an empty set means a trailing token is
    simply kept - the label is uglier, never wrong.
    """
    try:
        from services import zip_state
        return set(zip_state.covered_states())
    except Exception:
        return set()


_STATE_CODES = _state_codes()


def label_for_options(options):
    """Disambiguate labels across a multi-state option list, in place.

    Every option is {value, label, state, ...}. Labels are left exactly as
    they are unless the SAME label appears for more than one state - then each
    colliding label gets its state appended, so the rep can tell two
    identically-named utilities apart. This is the only place a state reaches
    a label, and only when the alternative is an ambiguous dropdown.
    """
    seen = {}
    for o in options or []:
        seen.setdefault(o.get("label"), set()).add(o.get("state"))
    for o in options or []:
        states = seen.get(o.get("label")) or set()
        if len(states) > 1 and o.get("state"):
            o["label"] = f"{o['label']} ({o['state']})"
    return options


def select_options():
    """Options for the workflow renderer's utility dropdown. Value is always the
    slug so the frontend never has to know about translation."""
    return [
        {"value": u["slug"], "label": u["display_name"]}
        for u in all_utilities()
    ]


# ─────────────── POD ID (secondary identifier) validation ───────────────
# Consumed by the existing Dalton bill/account screen before /enroll. The rules
# are published reference data and belong with the rest of the utility data.

def pod_id_rule(slug):
    """Returns the POD ID rule for a utility, or None if it doesn't require one."""
    u = by_slug(slug)
    if not u or not u["requires_pod_id"]:
        return None
    return {
        "required": True,
        "length": u["pod_id_length"],
        "prefix": u["pod_id_prefix"],
        "description": _pod_id_description(u),
    }


def _pod_id_description(u):
    parts = [f"{u['pod_id_length']} digits"]
    if u["pod_id_prefix"]:
        parts.append(f"starting with {u['pod_id_prefix']}")
    return ", ".join(parts)


def validate_pod_id(slug, pod_id):
    """Returns an error message, or None when valid / not required."""
    rule = pod_id_rule(slug)
    if not rule:
        return None
    pod_id = (pod_id or "").strip()
    if not pod_id:
        return f"This utility requires a POD ID ({rule['description']})."
    if rule["prefix"]:
        if not pod_id.upper().startswith(rule["prefix"]):
            return f"POD ID for this utility must start with {rule['prefix']}."
        digits = pod_id[len(rule["prefix"]):]
        if not digits.isdigit():
            return "POD ID must contain only digits after the prefix."
        if len(pod_id) != rule["length"]:
            return f"POD ID must be {rule['length']} characters — this one is {len(pod_id)}."
    else:
        if not pod_id.isdigit():
            return "POD ID must contain only digits."
        if len(pod_id) != rule["length"]:
            return f"POD ID must be {rule['length']} digits — this one is {len(pod_id)}."
    return None


def unconfirmed_slugs():
    """Utilities whose slug we INFERRED rather than read from Perch's table.
    Surfaced in diagnostics so an inferred value can't quietly become load-bearing."""
    return [dict(r) for r in query("SELECT * FROM perch_utilities WHERE slug_confirmed = 0")]
