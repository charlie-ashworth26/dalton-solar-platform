import json
from db import execute


def log(action, enrollment_id=None, user_id=None, details=None, ip_address=None):
    """Write one row to audit_logs. Call this on document access, status changes,
    and edits to enrollment information (per the security requirements)."""
    execute(
        """INSERT INTO audit_logs (enrollment_id, user_id, action, details_json, ip_address)
           VALUES (?, ?, ?, ?, ?)""",
        (enrollment_id, user_id, action, json.dumps(details or {}), ip_address),
    )
