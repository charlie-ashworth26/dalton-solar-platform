-- 002_perch_documented_contract.sql
-- Milestone 2: replace the guessed Perch contract with the documented one.
--
-- WHY perch_products IS DROPPED (and why that doesn't violate our additive-migration rule):
-- perch_products modeled a "list of selectable products" that the real capacity
-- response does not contain. The documented response is a single project_details
-- object with three availability booleans and two savings rates. Leaving the table
-- in place would be worse than dropping it — it would keep advertising a shape that
-- does not exist and invite code to be written against it. It has never held
-- production data (Milestone 1 was never deployed), and nothing references its rows.
-- enrollments.selected_perch_product_id is left in place but permanently unused;
-- SQLite cannot cheaply drop a column, and a later cleanup migration will remove it
-- during the Postgres move.

-- ─────────────── Utility slug registry ───────────────
-- Perch matches utilities by SLUG, not display name. A display-name/slug mismatch
-- silently fails the capacity check, so this is reference data, not free text.
-- Sources: Swagger "Utility name — slug mapping" and "Secondary identifier validation".
CREATE TABLE perch_utilities (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  slug                TEXT NOT NULL UNIQUE,
  display_name        TEXT NOT NULL,
  requires_pod_id     INTEGER NOT NULL DEFAULT 0,
  pod_id_length       INTEGER,          -- exact digit count when required
  pod_id_prefix       TEXT,             -- e.g. 'N01' for NYSEG
  slug_confirmed      INTEGER NOT NULL DEFAULT 1,  -- 0 = inferred, needs Perch confirmation
  is_active           INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Confirmed directly from the published slug-mapping table.
INSERT INTO perch_utilities (slug, display_name, requires_pod_id, pod_id_length, pod_id_prefix, slug_confirmed) VALUES
  ('national-grid-ny',          'National Grid NY',            0, NULL, NULL, 1),
  ('consolidated-edison-ny',    'Consolidated Edison NY',      0, NULL, NULL, 1),
  ('orange-and-rockland',       'Orange and Rockland',         0, NULL, NULL, 1),
  ('central-hudson-gas-electric','Central Hudson Gas & Electric', 1, 10,  NULL, 1),
  ('rochester-gas-electric',    'Rochester Gas & Electric',    1, 15,  'R01', 1);

-- NYSEG appears in the POD ID table but its slug was below the fold in the
-- screenshot. The slug below is INFERRED from the naming convention and is
-- flagged unconfirmed — see PERCH_OPEN_ITEMS.md (Blocking Milestone 2, Q5).
INSERT INTO perch_utilities (slug, display_name, requires_pod_id, pod_id_length, pod_id_prefix, slug_confirmed) VALUES
  ('nyseg', 'NYSEG', 1, 15, 'N01', 0);

-- ─────────────── Capacity checks (documented project_details shape) ───────────────
CREATE TABLE perch_capacity_checks (
  id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id                       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  perch_api_call_id                   INTEGER REFERENCES perch_api_calls(id),
  zip_code                            TEXT NOT NULL,
  utility_slug                        TEXT NOT NULL,
  capacity_available                  INTEGER NOT NULL DEFAULT 0,  -- 0 when Perch returned 503
  residential_capacity_available      INTEGER,
  small_commercial_capacity_available INTEGER,
  lmi_capacity_available              INTEGER,
  proof_documents_required            INTEGER,
  savings_percent_res_commercial      REAL,
  savings_percent_lmi                 REAL,
  next_step_url                       TEXT,
  raw_response_json                   TEXT NOT NULL,
  api_mode                            TEXT NOT NULL DEFAULT 'mock',
  checked_at                          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_capacity_checks_enrollment ON perch_capacity_checks(enrollment_id);

-- ─────────────── Workflow state (next_step URL is Perch's, not ours) ───────────────
-- Perch drives the enrollment state machine via the next_step URL on each response.
-- Dalton stores the last URL it was handed plus the step key it resolved to, so a
-- draft can be resumed and so an UNRECOGNIZED next_step URL is visible rather than
-- silently swallowed.
CREATE TABLE perch_workflow_state (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id         INTEGER NOT NULL UNIQUE REFERENCES enrollments(id) ON DELETE CASCADE,
  current_step_key      TEXT NOT NULL DEFAULT 'service_area',
  perch_next_step_url   TEXT,
  next_step_recognized  INTEGER NOT NULL DEFAULT 1,
  last_response_json    TEXT,
  updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────── Token columns for the documented auth model ───────────────
-- Perch issues an enrollment_token (UUID) sent as X-Enrollment-Token, valid 30
-- minutes, refreshed via PATCH /refresh_token. Tokens are scoped to an enrollment
-- session, so they are tracked per enrollment rather than globally.
ALTER TABLE perch_tokens ADD COLUMN enrollment_id INTEGER REFERENCES enrollments(id);
ALTER TABLE perch_tokens ADD COLUMN refresh_count INTEGER NOT NULL DEFAULT 0;

-- ─────────────── Remove the orphaned product FK, then drop perch_products ───────────────
-- SQLite cannot drop a column in place, and enrollments.selected_perch_product_id
-- is a foreign key into perch_products. Dropping the table while that column
-- exists makes every future INSERT into enrollments fail with
-- "no such table: main.perch_products" (caught during Milestone 2 testing).
-- The documented fix is the create-copy-drop-rename rebuild below. The migration
-- runner disables FK enforcement for the duration and runs PRAGMA
-- foreign_key_check afterwards, so integrity is still verified.

CREATE TABLE enrollments_new (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_code     TEXT NOT NULL UNIQUE,
  customer_id         INTEGER REFERENCES customers(id),
  service_address_id  INTEGER REFERENCES service_addresses(id),
  utility_account_id  INTEGER REFERENCES utility_accounts(id),
  project_id          INTEGER REFERENCES projects(id),
  sales_rep_id        INTEGER REFERENCES sales_reps(id),
  status              TEXT NOT NULL DEFAULT 'Draft',
  lmi_path            TEXT CHECK (lmi_path IN ('document','self_attestation','not_applicable') OR lmi_path IS NULL),
  created_by_user_id  INTEGER REFERENCES users(id),
  updated_by_user_id  INTEGER REFERENCES users(id),
  service_zip         TEXT,
  utility_name        TEXT,
  perch_enrollment_ref TEXT,
  perch_customer_ref  TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO enrollments_new
  (id, enrollment_code, customer_id, service_address_id, utility_account_id, project_id,
   sales_rep_id, status, lmi_path, created_by_user_id, updated_by_user_id,
   service_zip, utility_name, perch_enrollment_ref, perch_customer_ref, created_at, updated_at)
SELECT
   id, enrollment_code, customer_id, service_address_id, utility_account_id, project_id,
   sales_rep_id, status, lmi_path, created_by_user_id, updated_by_user_id,
   service_zip, utility_name, perch_enrollment_ref, perch_customer_ref, created_at, updated_at
FROM enrollments;

DROP TABLE enrollments;
ALTER TABLE enrollments_new RENAME TO enrollments;

CREATE INDEX idx_enrollments_status ON enrollments(status);
CREATE INDEX idx_enrollments_rep ON enrollments(sales_rep_id);

DROP TABLE perch_products;
