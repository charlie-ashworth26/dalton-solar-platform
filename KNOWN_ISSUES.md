# UBS — known unresolved issues

Issues that are **open**, reproducible, and deliberately not fixed yet. Nothing
here is to be described as fixed in release notes, and nothing here may be
papered over with a UI change that merely hides the symptom.

---

## 1. Orphaned upload files are never deleted from disk

**Status:** OPEN, by design, and recorded so it is a decision rather than an
oversight.

Removing a document detaches the REFERENCE: the `documents` row goes and the
file stays on disk. UBS has never physically deleted an uploaded document, and
a rep's remove button is not the right place to introduce that - the bytes may
still be referenced by a submission package, and a mis-click would be
unrecoverable.

So every removal and every replacement leaves a file behind. The count grows
with normal use, not with errors.

**Detecting them** - compare `documents.stored_path` against what is on disk
under `<DATA_ROOT>/uploads/<enrollment_id>/`. A file with no matching row is an
orphan.

**Empty document sets** are the same class of leftover: removing the last file
from a set leaves the `document_sets` row with no files.

```sql
SELECT ds.id, ds.enrollment_id, ds.category
  FROM document_sets ds
  LEFT JOIN documents d ON d.document_set_id = ds.id
 WHERE d.id IS NULL;
```

**Recommendation:** a separate, explicitly scoped maintenance task - an audit
that REPORTS first, and only then a sweep that deletes a file when no row in
`documents` references its path AND the enrollment is not awaiting submission.
It must not be bolted onto the removal path.

---

# No longer blocking

## Multi-file LMI proof uploads - RESOLVED 2026-10-28

Opened the same day while fixing the removal bug, and fixed immediately after.

**The symptom:** the proof picker allows multiple files and the UI listed every
one of them, but `handleLmiUpload()` uploaded only `files[0]` through
`uploadDocumentToDalton()` - the single-file route. A rep who picked three
pages of an award letter saw three rows while one file left the browser; the
other two never existed on the server and stayed `Uploading` indefinitely.

The comment above that line already read "front/back or multi-page proofs form
ONE document set" and the code called `docSetAddFiles()`. The intent was
there; the upload was never moved across. The bill flow had the identical
defect and was fixed by routing through `uploadDocumentSet()`.

**Two things a blind copy of the bill flow would have broken.** Both were
found by tracing rather than by a failing test, and both are now covered:

- **LMI validation.** The single-file route runs
  `lmi_validation.validate_lmi_document()` and writes a `validation_results`
  row; the document-set route never did, because only bills used it. Switching
  proofs across without it would have silently removed LMI validation from the
  enrollment detail payload, the developer/QA review and the submission
  package, with no failed upload anywhere to notice. The set route now does it
  for `lmi_document` - ONE judgement for the set, made from every page's text
  together, because judging page 2 of an award letter on its own returns
  "likely_invalid".
- **Perch submission.** `POST /lmi/proof_docs` takes a LIST of documents, each
  with one file part and its own metadata, and `build_proof_docs_multipart()`
  always looped over it - but the route built a list of ONE. Persisting three
  pages while submitting one would have moved the truncation downstream instead
  of fixing it. The route now expands the named document to every file of its
  set, repeating the rep's single answer (source type, name, relationship,
  format) per file, because the files are the pages of the one proof the rep is
  attesting to. A set-less legacy proof still produces a one-document request,
  byte for byte what it sent before.

`removeLmi()` was also still cosmetic for a stored proof - it cleared the chip
and local state only, so the proof came back on the next reopen. It now routes
through `docSetRemove()`, as `removeBill()` already did.

**Coverage that fails if it returns:** `test/test_lmi_multifile_proof.py`
(real app + database; one file, three at once, reopen, middle removal, later
addition, unrelated save, LMI validation preserved, what Perch receives, bill
behaviour unchanged) and `test/document_removal_harness.js` section 8
(runtime; asserts ONE post to `/document-sets` carrying the whole selection and
none to the single-file route). Both were confirmed to FAIL against the
pre-fix code.

---

## Removing a previously saved bill / proof photo - RESOLVED 2026-10-28

Carried for a long time as "reproduced repeatedly in the real browser after
three attempted fixes". Fixed, with the cause below. Kept in full because the
previous notes ruled out the right things for the wrong reason.

**The symptom:** remove a saved photo, Save & Exit, reopen - and it was back.
Removing and replacing made it worse: four removed-and-replaced photos reopened
as six.

**The actual root cause - TWO HYDRATION PATHS, the second discarding the set
id.** The enrollment detail payload carries attached documents twice:

| source | read by | carried the set id? |
|---|---|---|
| `e.document_sets` | `hydrateDocSets()`, early in `openEnrollment()` | yes |
| `e.documents` | `rehydrateDocumentsFromDetail()`, 59 lines later | **no** |

The second replaced `DocSets` wholesale with `id: null` - its own comment said
"document-set id is not exposed on detail", and it was right, because the
detail route never selected the column. `docSetRemove()` needs that id to
address the DELETE endpoint and **returned silently without one**. So pressing
the x spliced the entry out of the list, repainted the screen, and sent
nothing at all. The row survived; the next reopen listed it again from
`e.documents`. New uploads with a null set id started a RIVAL set instead of
appending, which is how old and new photos came to coexist.

The proof-document flow failed for a SECOND reason: `handleLmiUpload()` uploads
through the legacy single-file route, so an LMI document never has a set at
all, and no set-scoped endpoint could address it even in principle.

**Why the earlier investigations missed it.** Everything they ruled out was
genuinely correct - the DELETE endpoint, the backend association, hydration of
the newest set, set isolation. They inspected the removal path and
`hydrateDocSets()`, both correct. The damage happened BETWEEN them, and the
existing tests could not see it: `test_phase_a_reliability.py` drove the
endpoints directly, and `phase_a_state_harness.js` stubbed the detail payload
with `document_sets` only - so the path that caused the defect never ran in
any test. `test_phase_a_reliability.py` section 8p went further and pinned the
broken guard as a required source string, asserting the defect was present.

**The fix:**
- `routes/enrollment_routes.py` - the detail `documents` rows carry
  `document_set_id` and `page_order`.
- `static/js/app.js` - `rehydrateDocumentsFromDetail()` fills only a category
  `hydrateDocSets()` left empty and never overwrites a restored set; every
  entry carries its own `setId`; `docSetRemove()` addresses the entry's set,
  falls back to the per-document route, and no longer gives up quietly.
- `routes/document_routes.py` - `DELETE /api/enrollments/:id/documents/:doc`
  for set-less documents, which had no removal endpoint; both removal routes
  now detach the FK references first (a legacy upload writes a
  `validation_results` row, and deleting the document raised
  `IntegrityError: FOREIGN KEY constraint failed` - a 500 to the rep).

**Coverage that fails if it returns:** `test/test_document_removal_persistence.py`
(real app + database, both flows, all four lifecycle cases) and
`test/document_removal_harness.js` (runtime; hydrates from a payload carrying
BOTH sources - the condition the old harness lacked - and asserts the set id
survives and the rendered x issues a real DELETE). Both were confirmed to FAIL
against the pre-fix code.

Removal remains reference-only: see open issue 1 above for the files left on
disk.


Recorded here only so a stale blocker is not re-reported from an old note.
Nothing in this section is an open issue.

## Virginia self-attestation — RESOLVED 2026-10-27

Previously carried as "awaiting Perch": the VA state LMI self-attestation path
could not be exercised end to end because Perch staging did not support it yet.

- Perch updated staging.
- The VA state LMI self-attestation flow was tested successfully.
- Perch confirmed Virginia is good to move toward production.

No UBS code change was needed for this. The branch was already driven entirely
by Perch's `next_step`, which is exactly why it could be exercised the moment
Perch enabled it.

This says nothing about Virginia's DEPLOYMENT status, which is unchanged: VA
remains staging-only until explicitly released.
