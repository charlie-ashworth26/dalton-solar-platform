import json
import os
import re
from datetime import datetime

from db import query_one, execute

BACKEND_ROOT = os.path.dirname(os.path.abspath(__file__))


def resolve_stored_path(stored_path: str) -> str:
    """Every documents.stored_path value is relative to the backend root
    (e.g. 'uploads/3/utility_bill_x.pdf' or 'storage/generated/3/cdg-disclosure.pdf').
    This is the ONLY place that turns one into an absolute path — every route
    should call this instead of joining paths itself, so the convention can't
    drift between upload and generation code paths again."""
    return os.path.join(BACKEND_ROOT, stored_path)


def mask_account_number(acct: str) -> str:
    if not acct:
        return acct
    digits = re.sub(r"\D", "", acct)
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def next_enrollment_code():
    year = datetime.now().year
    row = query_one(
        "SELECT COUNT(*) as n FROM enrollments WHERE enrollment_code LIKE ?", (f"ENR-{year}-%",)
    )
    seq = (row["n"] if row else 0) + 1
    return f"ENR-{year}-{seq:06d}"


def json_or_none(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def to_json(value):
    return json.dumps(value, default=str) if value is not None else None


def enrollment_or_404(enrollment_id):
    return query_one("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))


ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4MB - Perch returns 413 above this (OpenAPI spec)


def validate_upload(filename, file_size):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return f"File type '{ext}' is not allowed. Accepted types: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
    if file_size > MAX_UPLOAD_BYTES:
        return f"File is too large ({file_size} bytes). Maximum is {MAX_UPLOAD_BYTES} bytes."
    return None
