-- Self-attestation reference data + idempotence ledger.
--
-- Perch requires county and occupancy on POST /lmi/self_attestation, and the
-- official NY_self_attestation_form.pdf presents a STATEWIDE 80% State Median
-- Income table keyed only by household size. County does NOT vary the
-- threshold; it is a separate Perch data field that they validate against the
-- utility's state.
--
-- Nothing here is geo-eligibility logic. Perch decides whether self-attestation
-- is required at all; Dalton only collects what the documented payload needs.

-- ── Threshold table, VERSIONED ────────────────────────────────────────────
-- Values transcribed from the Perch-provided NY_self_attestation_form.pdf
-- ("State Median Income Levels (80%)"). Stored in CENTS to avoid float error.
-- A future Perch form is one new migration: insert a new version_label and
-- flip is_active. The values are never hardcoded in JS.
CREATE TABLE perch_lmi_income_thresholds (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  version_label     TEXT    NOT NULL,
  occupancy         INTEGER NOT NULL,
  income_level_cents INTEGER NOT NULL,
  is_active         INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (version_label, occupancy)
);

CREATE TABLE perch_lmi_threshold_versions (
  version_label   TEXT PRIMARY KEY,
  source_document TEXT NOT NULL,
  table_caption   TEXT NOT NULL,
  effective_from  TEXT,
  is_active       INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO perch_lmi_threshold_versions
  (version_label, source_document, table_caption, effective_from) VALUES
  ('NY-SMI-80-2026', 'NY_self_attestation_form.pdf',
   'State Median Income Levels (80%)', '2026-01-01');

-- occupancy 1-8 only: the API documents occupancy as an integer from 1 to 8,
-- and the form's table stops at 8.
INSERT INTO perch_lmi_income_thresholds (version_label, occupancy, income_level_cents) VALUES
  ('NY-SMI-80-2026', 1, 6175000),
  ('NY-SMI-80-2026', 2, 7055000),
  ('NY-SMI-80-2026', 3, 7935000),
  ('NY-SMI-80-2026', 4, 8815000),
  ('NY-SMI-80-2026', 5, 9525000),
  ('NY-SMI-80-2026', 6, 10230000),
  ('NY-SMI-80-2026', 7, 10935000),
  ('NY-SMI-80-2026', 8, 11640000);

-- ── Controlled NY county list ─────────────────────────────────────────────
-- Perch validates county against the utility's state ("Hudson County is not a
-- recognized county in NY"), so a free-text field would produce avoidable 422s.
-- All 62 NY counties, stored exactly as Perch's examples format them
-- ("Kings County").
CREATE TABLE ny_counties (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  is_active  INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO ny_counties (name) VALUES
  ('Albany County'),
  ('Allegany County'),
  ('Bronx County'),
  ('Broome County'),
  ('Cattaraugus County'),
  ('Cayuga County'),
  ('Chautauqua County'),
  ('Chemung County'),
  ('Chenango County'),
  ('Clinton County'),
  ('Columbia County'),
  ('Cortland County'),
  ('Delaware County'),
  ('Dutchess County'),
  ('Erie County'),
  ('Essex County'),
  ('Franklin County'),
  ('Fulton County'),
  ('Genesee County'),
  ('Greene County'),
  ('Hamilton County'),
  ('Herkimer County'),
  ('Jefferson County'),
  ('Kings County'),
  ('Lewis County'),
  ('Livingston County'),
  ('Madison County'),
  ('Monroe County'),
  ('Montgomery County'),
  ('Nassau County'),
  ('New York County'),
  ('Niagara County'),
  ('Oneida County'),
  ('Onondaga County'),
  ('Ontario County'),
  ('Orange County'),
  ('Orleans County'),
  ('Oswego County'),
  ('Otsego County'),
  ('Putnam County'),
  ('Queens County'),
  ('Rensselaer County'),
  ('Richmond County'),
  ('Rockland County'),
  ('St. Lawrence County'),
  ('Saratoga County'),
  ('Schenectady County'),
  ('Schoharie County'),
  ('Schuyler County'),
  ('Seneca County'),
  ('Steuben County'),
  ('Suffolk County'),
  ('Sullivan County'),
  ('Tioga County'),
  ('Tompkins County'),
  ('Ulster County'),
  ('Warren County'),
  ('Washington County'),
  ('Wayne County'),
  ('Westchester County'),
  ('Wyoming County'),
  ('Yates County');

-- ── Idempotence ledger ────────────────────────────────────────────────────
-- The ONLY purpose of this table is to stop a reload/resume from generating or
-- accepting self-attestation twice.
--
-- document_url_returned is a BOOLEAN, not the URL. Perch's document URLs are
-- presigned and short-lived; the same rule contracts already follow applies
-- here - Dalton records THAT a document was issued, never the link.
CREATE TABLE perch_self_attestation_submissions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id         INTEGER NOT NULL UNIQUE REFERENCES enrollments(id),
  occupancy             INTEGER,
  county                TEXT,
  status                TEXT,
  version_label         TEXT,
  document_url_returned INTEGER NOT NULL DEFAULT 0,
  generated_at          TEXT,
  generated_by_user_id  INTEGER REFERENCES users(id),
  accepted_at           TEXT,
  accepted_next_step    TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_self_attestation_enrollment
  ON perch_self_attestation_submissions(enrollment_id);
