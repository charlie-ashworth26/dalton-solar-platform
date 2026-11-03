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
# ═══════════ Naming the cause, when the cause is actually known ═══════════
#
# WHY THIS EXISTS
#
#     "Some of the details were not accepted. Please check the information on
#     this step and try again."
#
#     That sentence is honest and useless in equal measure. It is correct for
#     a rejection UBS cannot explain, and it was being shown for several it
#     can: a POD ID in the wrong format, a program with no published savings
#     rate, an account number the utility's own rule refuses. The rep re-reads
#     a screen that looks fine, tries again, and gets the same sentence.
#
# WHAT IS ALLOWED TO PRODUCE A SPECIFIC MESSAGE
#
#     Only evidence that actually identifies the cause:
#
#       1. the exception TYPE, where the type is the answer
#          (PerchNoCapacityError means exactly one thing);
#       2. a message UBS ITSELF wrote, carried on the exception as
#          `rep_message` - safe by construction, because we wrote it for a
#          rep in the first place;
#       3. Perch's STRUCTURED field errors - the (field, code) pairs in a
#          ValidationErrorsResponse, which name the field and why.
#
#     Everything else keeps a generic sentence. A bare 400 or 422 with no
#     structure is NOT evidence of anything in particular, and guessing from
#     it is how a rep ends up chasing the wrong field.
#
# WHAT IS STILL NEVER SHOWN
#     The partner's name, HTTP status codes, endpoint paths, machine codes,
#     exception classes, stack traces, raw partner prose. contains_internals()
#     above still guards every sentence that is not one of ours, and the full
#     diagnostic text keeps going to the audit record and perch_api_calls
#     untouched - that is where debugging happens.

NO_CAPACITY = (
    "No enrollment capacity is currently available for this utility and ZIP. "
    "Try again later, or select another eligible utility or ZIP code.")

ACCOUNT_NUMBER_FORMAT = (
    "The utility account number does not match the required format. "
    "Please check the bill and try again.")

POD_ID_FORMAT = (
    "The POD ID is missing or does not match the required format. "
    "Please check the utility bill.")

METER_NUMBER_FORMAT = (
    "The meter number does not match the required format. "
    "Please check the bill.")

PROGRAM_UNAVAILABLE = (
    "This savings program is not currently available for enrollment. "
    "Please choose another available program.")

ALREADY_ENROLLED = (
    "This utility account appears to already be enrolled or associated with "
    "an existing program. Please review the account before trying again.")

BILL_PAGES_REJECTED = (
    "The utility bill has more pages or files than this enrollment accepts. "
    "Remove the extra pages and upload the bill again.")

BILL_FILE_REJECTED = (
    "The utility bill file was not accepted. Check that each page is a clear "
    "PDF or photo within the size limit, then upload it again.")

LMI_DOCUMENT_REQUIRED = (
    "Proof of income qualification is required for this program. Upload the "
    "qualifying document before continuing.")

CUSTOMER_INFO_MISSING = (
    "Some required customer information is missing. "
    "Please review the highlighted fields.")

#: The fallback when the cause genuinely cannot be identified. Deliberately
#: says what failed and what to do, and claims nothing about why.
ENROLLMENT_NOT_ACCEPTED = (
    "We couldn't complete the enrollment with the information provided. "
    "Please review the account details and try again.")


#: Perch's field names for the identifiers on the Customer & Bill screen, and
#: the UBS field each maps to for highlighting. The value is the id of the
#: input a rep would correct - the browser already owns those ids, so this
#: names a field without inventing a second vocabulary for it.
FIELD_TO_INPUT = {
    "utility_bills": "bill-file",
    "proof_doc": "lmi-file",
    "lmi_documents": "lmi-file",
    "lmi_document": "lmi-file",
    "lmi_source_selection": "lmi-file",
    "proof_docs": "lmi-file",
    "utility_account_number": "c-acct",
    "account_number": "c-acct",
    "secondary_account_identifier": "c-pod",
    "pod_id": "c-pod",
    "meter_number": "c-meter",
    "email_address": "c-email",
    "phone_number": "c-phone",
    "first_name": "c-first",
    "last_name": "c-last",
}

#: (field, code) -> sentence. The code matters: `already_exists` on an email
#: is Perch explicitly saying this is a duplicate, while `invalid` on the same
#: field is a format problem and must not be reported as one.
_BY_FIELD_AND_CODE = {
    ("email_address", "already_exists"): ALREADY_ENROLLED,
    ("utility_account_number", "already_exists"): ALREADY_ENROLLED,
    ("account_number", "already_exists"): ALREADY_ENROLLED,
    ("customer_type", "capacity_unavailable"): PROGRAM_UNAVAILABLE,
    ("savings_percent", "required"): PROGRAM_UNAVAILABLE,
    ("savings_percent", "invalid"): PROGRAM_UNAVAILABLE,
    ("zip_code", "not_in_service_area"): NO_CAPACITY,
}

#: field -> sentence, for any code on that field. Consulted after the pair
#: table, so a code with its own meaning is never flattened into this one.
_BY_FIELD = {
    # The income-qualified programs refuse without proof, and "Lmi documents
    # is required for this enrollment" - the per-code sentence this replaces -
    # names a field the rep cannot see and no action they can take.
    "lmi_documents": LMI_DOCUMENT_REQUIRED,
    "lmi_document": LMI_DOCUMENT_REQUIRED,
    "proof_doc": LMI_DOCUMENT_REQUIRED,
    "proof_docs": LMI_DOCUMENT_REQUIRED,
    "lmi_source_selection": LMI_DOCUMENT_REQUIRED,
    "utility_account_number": ACCOUNT_NUMBER_FORMAT,
    "account_number": ACCOUNT_NUMBER_FORMAT,
    "secondary_account_identifier": POD_ID_FORMAT,
    "pod_id": POD_ID_FORMAT,
    "meter_number": METER_NUMBER_FORMAT,
    "customer_type": PROGRAM_UNAVAILABLE,
    "savings_percent": PROGRAM_UNAVAILABLE,
}

#: Codes Perch returns about the FILES on an enrollment rather than a value a
#: rep typed. They arrive on a field like `utility_accounts[0][utility_bills]`,
#: which is a path expression rather than a name, so they are matched on the
#: code plus the presence of the document part in the field.
_DOCUMENT_CODES = {
    "too_many_files": BILL_PAGES_REJECTED,
    "too_many_pages": BILL_PAGES_REJECTED,
    "file_too_large": BILL_FILE_REJECTED,
    "invalid_file_type": BILL_FILE_REJECTED,
    "missing_file": BILL_FILE_REJECTED,
}

#: Fields that are plain customer details. A `required` on any of them means
#: the same thing and reads better as one sentence than as a list.
_CUSTOMER_FIELDS = frozenset({
    "first_name", "last_name", "email_address", "phone_number",
    "street_address", "city", "state", "postal_code",
})


def _ubs_sentence(item):
    """E3's own sentence for this field error, and how specific it is.

    Asked of services.perch.client, which has owned these tables since E3 and
    is covered by its own suites. Importing it here rather than copying the
    tables means there is still ONE place a field's wording is decided.

    Imported inside the function: this module is also used by routes that have
    no reason to load the transport layer, and an import failure must not take
    a rep-facing message down with it.
    """
    try:
        from services.perch.client import ubs_sentence_for
    except Exception:
        return None, None
    try:
        text, specificity = ubs_sentence_for(item)
    except Exception:
        return None, None
    # A sentence from those tables is UBS prose, but it is worth re-checking:
    # the tables are edited by hand and a partner name could be typed into one.
    if text and contains_internals(text):
        return None, None
    return text, specificity


def _structured_errors(source):
    """Perch's per-field errors, or []. Never raises on an odd body."""
    body = getattr(source, "body_json", None)
    if not isinstance(body, dict):
        return []
    errors = body.get("errors")
    if not isinstance(errors, list):
        return []
    return [e for e in errors if isinstance(e, dict)]


def classify_validation(source):
    """(message, field_id) for a failure whose cause is identifiable.

    Returns (None, None) when it is not - the caller then keeps the generic
    sentence. Never returns partner text: every sentence it can return is
    written above, in this file.
    """
    if source is None:
        return None, None

    # 1. THE TYPE IS THE ANSWER. A documented no-capacity outcome needs no
    #    interpretation, and must never be reported as a validation problem
    #    with the rep's data.
    if type(source).__name__ == "PerchNoCapacityError":
        return NO_CAPACITY, None

    # 2. A SENTENCE UBS WROTE. Our own pre-flight checks raise with wording
    #    already aimed at a rep ("NYSEG requires a POD ID (15 characters
    #    starting N01)"), which is more useful than any general sentence here
    #    - and is ours, so it cannot leak a partner's internals.
    own = getattr(source, "rep_message", None)
    if own and not contains_internals(own):
        return str(own), FIELD_TO_INPUT.get(getattr(source, "rep_field", None))

    # 3. PERCH'S STRUCTURED FIELD ERRORS. The one partner-supplied evidence
    #    that actually identifies a cause, because it names the field and the
    #    reason as data rather than prose.
    #
    #    FOUR TIERS, MOST SPECIFIC FIRST. The order is the whole point, and
    #    getting it wrong is how a working message gets replaced by a vaguer
    #    one: UBS has had per-(field, code) sentences since E3
    #    (services/perch/client.py), and reaching for a category sentence
    #    first turned "An enrollment already exists for this email address."
    #    into a sentence about the utility account, and "This ZIP code is
    #    outside the service area" into one about capacity. Two different
    #    facts, reported as the same thing.
    #
    #      a. the document codes below - about the FILES, not a typed value
    #      b. E3's exact (field, code) sentence
    #      c. this module's (field, code) pairs, then its per-field guidance,
    #         which is better than "is not valid" for the three identifiers a
    #         rep reads off a bill
    #      d. E3's per-code sentence, which at least names the field
    # A `required` on ordinary customer details, taken together and FIRST.
    # Named before the tiers below because it is about the set rather than any
    # one field: three missing details read better as one sentence than as a
    # sentence about whichever happened to be first in the list. The field id
    # still points at one of them, so "review the highlighted fields" is
    # literally what the rep sees.
    missing = [str(i.get("field") or "") for i in _structured_errors(source)
               if str(i.get("code") or "") == "required"]
    if missing and all(f in _CUSTOMER_FIELDS for f in missing):
        return CUSTOMER_INFO_MISSING, FIELD_TO_INPUT.get(missing[0])

    for item in _structured_errors(source):
        field = str(item.get("field") or item.get("name") or "").strip()
        code = str(item.get("code") or "").strip()
        base = field.split("[")[0]
        inner = field.lower()

        if code in _DOCUMENT_CODES and ("utility_bills" in inner
                                        or "proof_doc" in inner
                                        or "document" in inner):
            target = "lmi-file" if "proof_doc" in inner else "bill-file"
            return _DOCUMENT_CODES[code], target

        exact, specificity = _ubs_sentence(item)
        if exact and specificity == "pair":
            return exact, FIELD_TO_INPUT.get(field) or FIELD_TO_INPUT.get(base)

        text = _BY_FIELD_AND_CODE.get((field, code)) or _BY_FIELD.get(field)
        if text:
            return text, FIELD_TO_INPUT.get(field) or FIELD_TO_INPUT.get(base)

        if exact:
            return exact, FIELD_TO_INPUT.get(field) or FIELD_TO_INPUT.get(base)

    return None, None


def came_from_partner(source):
    """Whether this error was built from a PARTNER RESPONSE rather than by us.

    The client sets `status_code` and `body_json` on an error it builds from an
    HTTP response (services/perch/client.py), and nothing else does. A check
    UBS raises itself - "Customer email must match the email used to open this
    Perch enrollment session." - carries neither.

    `http_status` is NOT the signal: it is a class attribute on every Perch
    error, so it is present on ours too.
    """
    return (getattr(source, "status_code", None) is not None
            or getattr(source, "body_json", None) is not None)


def for_enrollment_failure(source):
    """(message, field_id) for a failed enrollment attempt.

    The named cause when there is one, and the honest fallback when there is
    not. Used by the routes in place of the bare VALIDATION_FAILED sentence.
    """
    message, field = classify_validation(source)
    if message:
        return message, field

    # NO IDENTIFIABLE CAUSE, so the question is only whose words these are.
    #
    # A sentence UBS wrote is worth keeping: it was aimed at a rep in the first
    # place and often names the exact thing to fix. Partner text is not, even
    # when it happens to read cleanly - "Bad request." carries no internals by
    # any pattern here, and would have gone to the rep verbatim. Forwarding a
    # partner's message is the thing this layer exists to prevent, and a
    # harmless-looking one is simply the case where the leak is hardest to see.
    if came_from_partner(source):
        return ENROLLMENT_NOT_ACCEPTED, None
    return rep_message(source, ENROLLMENT_NOT_ACCEPTED), None
