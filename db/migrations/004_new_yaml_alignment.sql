-- 004_new_yaml_alignment.sql
-- Aligns reference data with the NEWEST Perch OpenAPI specification.
--
-- Additive/corrective only. Migration 003 is NOT edited: it may already be
-- applied on a machine, and rewriting an applied migration would leave two
-- databases claiming the same schema version with different contents.

-- ─────────────── Retire self_attestation_qualifying_income_rejected ───────────────
-- OLD YAML: lmi_source_selection enum was
--   [self_attestation_qualifying_income, self_attestation_qualifying_income_rejected]
-- NEW YAML: the enum is [self_attestation_qualifying_income] ONLY. Sending the
-- _rejected value now returns 422:
--   "self_attestation_qualifying_income_rejected is not a valid source type in NY"
--
-- The accept/reject distinction moved to a separate `status` field on the
-- self-attestation request. Marking the old value inactive rather than deleting
-- it preserves the audit trail for anything already recorded against it.
ALTER TABLE perch_proof_doc_types ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE perch_proof_doc_types ADD COLUMN retired_note TEXT;

UPDATE perch_proof_doc_types
SET is_active = 0,
    retired_note = 'Retired by the newest OpenAPI spec. lmi_source_selection now '
                || 'accepts only self_attestation_qualifying_income; the accept/reject '
                || 'distinction moved to the separate self_attestation.status field '
                || '(accepted|rejected). Sending this value returns 422.'
WHERE source_type = 'self_attestation_qualifying_income_rejected';

-- ─────────────── Self-attestation status vocabulary (NEW YAML) ───────────────
-- Consumed in Milestone 5. Recorded now so the vocabulary is not re-derived
-- later, and so the retired source type above has a documented replacement.
CREATE TABLE perch_self_attestation_status (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  status_value TEXT NOT NULL UNIQUE,
  meaning      TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO perch_self_attestation_status (status_value, meaning) VALUES
  ('accepted', 'Household income is at or below the HUD income limit for the county and occupancy.'),
  ('rejected', 'Household income is above the HUD income limit for the county and occupancy.');
