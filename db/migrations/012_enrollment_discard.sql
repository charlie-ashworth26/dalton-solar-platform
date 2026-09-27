-- Soft-discard for provisional enrollments abandoned by a failed attempt.
--
-- WHY SOFT: a provisional row is cheap to hide and impossible to recover once
-- DELETEd. Setting discarded_at removes it from every normal listing while
-- keeping the audit trail, so a mistaken cleanup is reversible with an UPDATE.
--
-- The dashboard/list serializer, the QA queue and the report counts all filter
-- on discarded_at IS NULL, so this is server-side removal - not a row hidden
-- by JavaScript.
--
-- ── SCHEMA ONLY. THIS MIGRATION CLASSIFIES NOTHING. ───────────────────────
-- It deliberately contains no UPDATE and no DELETE. It adds two columns and an
-- index; every existing row keeps discarded_at NULL and is therefore visible
-- exactly as it was before.
--
-- WHY NO HISTORICAL SWEEP:
--   The runtime predicate in services/enrollment_cleanup.py is safe because it
--   knows one more thing than any query can: that THIS request just created
--   THIS row and THIS request then failed. A historical row carries no such
--   evidence. A legitimate enrollment that was started, passed capacity, and
--   was simply left unfinished before customer information was entered looks
--   identical to a duplicate-email orphan, so any predicate run over history
--   would discard real paused work.
--
--   Existing orphans are therefore inspected and soft-discarded separately
--   after deployment, against explicit verified enrollment ids/codes:
--       UPDATE enrollments
--          SET discarded_at = datetime('now'), discarded_reason = '<reason>'
--        WHERE id IN (<verified ids>) AND discarded_at IS NULL;
--   That statement does not belong here, because a migration runs
--   unattended and cannot verify anything.
ALTER TABLE enrollments ADD COLUMN discarded_at TEXT;
ALTER TABLE enrollments ADD COLUMN discarded_reason TEXT;

CREATE INDEX idx_enrollments_discarded ON enrollments(discarded_at);
