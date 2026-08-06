-- 003_openapi_spec_alignment.sql
-- Aligns reference data with Perch's official OpenAPI specification.
-- Additive and data-only: no table is dropped or restructured.

-- The spec publishes SEVEN NY utilities. Migration 002 seeded six, and NYSEG's
-- slug was inferred. The spec confirms 'nyseg' was correct and adds PSE&G NY.
UPDATE perch_utilities SET slug_confirmed = 1 WHERE slug = 'nyseg';

INSERT INTO perch_utilities (slug, display_name, requires_pod_id, pod_id_length, pod_id_prefix, slug_confirmed)
SELECT 'pse-g-ny', 'PSE&G NY', 0, NULL, NULL, 1
WHERE NOT EXISTS (SELECT 1 FROM perch_utilities WHERE slug = 'pse-g-ny');

-- Utility account number formats - published in the spec, not previously held.
-- Validating locally turns an opaque Perch 422 into a field-level message.
ALTER TABLE perch_utilities ADD COLUMN account_number_length INTEGER;

UPDATE perch_utilities SET account_number_length = 11 WHERE slug IN
  ('consolidated-edison-ny','nyseg','rochester-gas-electric','pse-g-ny');
UPDATE perch_utilities SET account_number_length = 10 WHERE slug IN
  ('national-grid-ny','central-hudson-gas-electric','orange-and-rockland');

-- Published proof-document source types. Perch requires the partner to send the
-- correct source_type per uploaded document; these are the only valid values.
-- Consumed in Milestone 5; stored now so the vocabulary is not re-derived later.
CREATE TABLE perch_proof_doc_types (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type  TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  category     TEXT NOT NULL DEFAULT 'proof_doc',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO perch_proof_doc_types (source_type, display_name, category) VALUES
  ('proof_doc_free_reduced_school_lunch_letter', 'Free/reduced school lunch letter', 'proof_doc'),
  ('proof_doc_lifeline_usac',                    'Lifeline USAC',                    'proof_doc'),
  ('proof_doc_liheap',                           'LIHEAP',                           'proof_doc'),
  ('proof_doc_medicaid',                         'Medicaid',                         'proof_doc'),
  ('proof_doc_section_8',                        'Section 8',                        'proof_doc'),
  ('proof_doc_snap',                             'SNAP',                             'proof_doc'),
  ('proof_doc_ssi',                              'Supplemental Security Income (SSI)','proof_doc'),
  ('self_attestation_qualifying_income',         'Self-attestation - qualifying income',          'self_attestation'),
  ('self_attestation_qualifying_income_rejected','Self-attestation - income above threshold',     'self_attestation');

-- The customer email is required by POST /token, so it must be captured before
-- any Perch call. Stored on the enrollment so a draft can be resumed and so
-- PATCH /refresh_token (which is keyed on email) can be issued later.
ALTER TABLE enrollments ADD COLUMN perch_token_email TEXT;
