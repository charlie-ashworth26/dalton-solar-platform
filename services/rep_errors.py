"""
Rep-facing error copy: one place that decides what a rep is allowed to read.

WHY THIS EXISTS (Phase D, item D-2)

    Several rep-facing failures handed the rep our plumbing:

        "Perch returned 503 for this utility and ZIP, which means no open
         solar project capacity."
        "Perch market data is temporarily unavailable: county not found:
         'Atlantis'"

    A rep cannot act on a vendor name, an HTTP status, an endpoint path or a
    machine code - and none of it is theirs to see. Those strings came from
    `str(exc)` being passed straight into a response body, so every new error
    path inherited the problem by default.

WHAT IT DOES
    rep_message() returns something a rep can act on. A message that is
    ALREADY written for a rep passes through unchanged - that matters, because
    the 422 field translations built in E3 ("That county isn't recognized for
    this state. Please check the selected county and try again.") are far more
    useful than any generic sentence, and must not be flattened. A message
    carrying internals is REPLACED, not edited: partial redaction produces
    text like "returned  for this utility", which is worse than a clean
    sentence that says what to do next.

WHAT IT DELIBERATELY DOES NOT DO
    It does not touch diagnostics. audit.log, application logs, the developer
    and admin payloads and the stored Perch API-call records all keep the full
    original text. This is the last few inches before a sales rep's screen,
    and nothing else.
"""
import re

#: Partner and upstream names. A rep works for Dalton Solar; who Dalton
#: integrates with is not their concern and changes without their involvement.
_VENDOR_NAMES = ("perch", "arcadia", "solstice")

#: Things that are unmistakably machinery rather than guidance.
_INTERNAL_PATTERNS = (
    re.compile(r"\b[1-5]\d{2}\b"),                       # HTTP status codes
    re.compile(r"/(enroll|capacity|contracts|status|token|markets|lmi)\b"),
    re.compile(r"\b[a-z]+(?:_[a-z]+){1,}\b"),            # machine_codes
    re.compile(r"\b[A-Za-z]*(?:Error|Exception)\b"),     # exception classes
    re.compile(r"traceback|sqlite|psycopg", re.I),
    re.compile(r"\.py\b"),
)


def contains_internals(text) -> bool:
    """Whether this text would show a rep something that is not theirs."""
    raw = str(text or "")
    if not raw.strip():
        return True
    low = raw.lower()
    if any(name in low for name in _VENDOR_NAMES):
        return True
    return any(p.search(raw) for p in _INTERNAL_PATTERNS)


def rep_message(source, fallback):
    """The sentence a rep should read.

    `source` may be an exception or a string. `fallback` is the vendor-neutral,
    actionable sentence for THIS situation - the caller knows what the rep was
    trying to do, so the caller supplies it rather than this module guessing.
    """
    text = str(getattr(source, "args", None) and source.args[0] or source or "").strip()
    if not text:
        return fallback
    # Over-long text is machinery by another name: no rep-facing sentence we
    # write runs past a couple of lines.
    if len(text) > 240:
        return fallback
    return fallback if contains_internals(text) else text


# ── The standard situations, worded once ────────────────────────────────────
# Each says what happened in the rep's terms and what to do next. None names a
# partner, a status code, an endpoint or a code.

AVAILABILITY_UNAVAILABLE = (
    "Availability could not be checked right now. Please try again.")

MARKET_DATA_UNAVAILABLE = (
    "We couldn't load the available county information. Please try again.")

UTILITY_LIST_UNAVAILABLE = (
    "We couldn't load the utility list for this area. Please try again.")

ACCOUNT_FIELDS_UNAVAILABLE = (
    "We couldn't load this utility's account requirements. You can still "
    "enter the account details and continue.")

ENROLLMENT_FAILED = (
    "This enrollment could not be submitted right now. Please try again, and "
    "if it keeps happening let an admin know.")

DOCUMENT_SUBMIT_FAILED = (
    "The document could not be submitted right now. It is still attached to "
    "this enrollment. Please try again.")

CONTRACTS_FAILED = (
    "The agreement could not be prepared right now. Please try again.")

STATUS_UNAVAILABLE = (
    "This enrollment's status could not be checked right now. Please try "
    "again in a moment.")

#: Which sentence suits which workflow operation. Anything not listed falls
#: back to the general enrollment wording.
BY_OPERATION = {
    "capacity": AVAILABILITY_UNAVAILABLE,
    "service_area": AVAILABILITY_UNAVAILABLE,
    "enroll": ENROLLMENT_FAILED,
    "proof_docs": DOCUMENT_SUBMIT_FAILED,
    "self_attestation": DOCUMENT_SUBMIT_FAILED,
    "self_attestation_accept": DOCUMENT_SUBMIT_FAILED,
    "contracts": CONTRACTS_FAILED,
    "contracts_accept": CONTRACTS_FAILED,
    "status": STATUS_UNAVAILABLE,
}


#: A correctable problem is NOT an infrastructure problem, and must never be
#: worded as one. "Please try again" sends a rep round the same loop when what
#: is actually needed is a change to the form. E3's 422 translations name the
#: exact field and pass through untouched; this is only for a validation
#: message that still carries machinery (an unmapped field/code), where the
#: honest answer is "something you entered was refused", not "retry later".
VALIDATION_FAILED = (
    "Some of the details were not accepted. Please check the information on "
    "this step and try again.")


def for_operation(operation, source=None):
    """Rep-facing copy for a named workflow operation.

    A VALIDATION failure keeps validation wording even when its text has to be
    replaced; everything else gets the operation's infrastructure sentence.
    """
    if source is None:
        return BY_OPERATION.get(operation, ENROLLMENT_FAILED)
    status = getattr(source, "http_status", None)
    is_validation = (type(source).__name__ in
                     ("PerchValidationError", "PerchNoCapacityError")
                     or (isinstance(status, int) and 400 <= status < 500
                         and status not in (401, 403, 404, 408, 429)))
    fallback = (VALIDATION_FAILED if is_validation
                else BY_OPERATION.get(operation, ENROLLMENT_FAILED))
    return rep_message(source, fallback)
