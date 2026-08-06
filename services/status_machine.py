"""
Enrollment status machine.

Statuses are exactly the 18 the spec lists. Transitions are modeled as a
directed graph rather than a strict linear sequence, because real enrollments
branch (Needs Work sends things backward; Rejected can happen at QA or at the
developer stage; LMI Review is skipped entirely for non-LMI projects).

transition() is the only way enrollment.status should change — it validates
the move, updates the row, and writes both status_history and an audit log
entry in the same call, so those two tables can never drift out of sync with
the enrollment's actual status.
"""
from db import execute, query_one
from services import audit

STATUSES = [
    "Draft", "Information Needed", "Utility Bill Uploaded", "Utility Validation",
    "LMI Review", "Agreement Ready", "Signature Pending", "Signed",
    "Internal Review", "Needs Work", "Verified", "Submitted", "Developer Review",
    "Accepted", "Rejected", "Project Assigned", "Active",
]

# status -> set of statuses it may move to next
ALLOWED_TRANSITIONS = {
    "Draft":                {"Information Needed", "Utility Bill Uploaded"},
    "Information Needed":   {"Utility Bill Uploaded", "Draft"},
    "Utility Bill Uploaded": {"Utility Validation", "Information Needed"},
    "Utility Validation":   {"LMI Review", "Agreement Ready", "Information Needed"},
    "LMI Review":           {"Agreement Ready", "Information Needed"},
    "Agreement Ready":      {"Signature Pending"},
    "Signature Pending":    {"Signed", "Agreement Ready"},
    "Signed":               {"Internal Review"},
    "Internal Review":      {"Needs Work", "Verified", "Rejected"},
    "Needs Work":           {"Information Needed", "Utility Validation", "LMI Review", "Agreement Ready", "Internal Review"},
    "Verified":             {"Submitted"},
    "Submitted":            {"Developer Review"},
    "Developer Review":     {"Accepted", "Rejected", "Needs Work"},
    "Accepted":             {"Project Assigned"},
    "Rejected":             {"Needs Work", "Information Needed"},
    "Project Assigned":     {"Active"},
    "Active":               set(),
}


class InvalidTransition(Exception):
    pass


def transition(enrollment_id, new_status, user_id=None, reason=None, notes=None, force=False, ip_address=None):
    enrollment = query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    if not enrollment:
        raise ValueError(f"No enrollment with id {enrollment_id}")

    previous_status = enrollment["status"]

    if new_status not in STATUSES:
        raise ValueError(f"'{new_status}' is not a recognized status")

    if not force and new_status not in ALLOWED_TRANSITIONS.get(previous_status, set()):
        raise InvalidTransition(
            f"Cannot move enrollment {enrollment_id} from '{previous_status}' to '{new_status}'. "
            f"Allowed next statuses: {sorted(ALLOWED_TRANSITIONS.get(previous_status, set()))}"
        )

    execute(
        "UPDATE enrollments SET status = ?, updated_at = datetime('now'), updated_by_user_id = ? WHERE id = ?",
        (new_status, user_id, enrollment_id),
    )
    execute(
        """INSERT INTO status_history (enrollment_id, previous_status, new_status, changed_by_user_id, reason, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (enrollment_id, previous_status, new_status, user_id, reason, notes),
    )
    audit.log(
        "status_changed", enrollment_id=enrollment_id, user_id=user_id,
        details={"from": previous_status, "to": new_status, "reason": reason},
        ip_address=ip_address,
    )
    return new_status
