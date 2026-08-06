"""
Utility slug translation and per-utility validation rules.

Perch matches utilities by slug (`consolidated-edison-ny`), never by display
name. A mismatch does not raise a helpful error — the capacity check simply
fails to find a project. This module is the single translation point, backed by
the perch_utilities reference table seeded in migration 002.

POD ID rules live here too. They are documented and stable, so they belong in
reference data now even though the collection UI is Milestone 3. Validating
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
    return match["slug"] if match else None


def select_options():
    """Options for the workflow renderer's utility dropdown. Value is always the
    slug so the frontend never has to know about translation."""
    return [
        {"value": u["slug"], "label": u["display_name"]}
        for u in all_utilities()
    ]


# ─────────────── POD ID (secondary identifier) validation ───────────────
# Consumed in Milestone 3 when the enroll step collects it. Defined here now
# because the rules are published and belong with the rest of the utility data.

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
