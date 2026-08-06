-- Dalton Solar Enrollment Platform — SQLite schema (prototype)
-- Portable version for Postgres lives in schema_postgres.sql — see README for migration notes.
-- Design notes:
--   * Every table has created_at; mutable tables also have updated_at.
--   * Money/percentages are stored as REAL (fine for a prototype; use NUMERIC in Postgres).
--   * JSON columns are used ONLY for genuinely flexible/variable-shape metadata
--     (extracted bill fields, validation reasons, audit details) — never as a substitute
--     for relational modeling of core entities.
--   * signing_sessions is an addition beyond the spec's table list, added because
--     "expiring signing session" (requirement #6) needs somewhere to live.

PRAGMA foreign_keys = ON;

-- ─────────────────────────── Identity & staff ───────────────────────────

CREATE TABLE users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT NOT NULL UNIQUE,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('sales_rep','qa_reviewer','admin','developer')),
  full_name       TEXT NOT NULL,
  is_active       INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sales_reps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  rep_code    TEXT NOT NULL UNIQUE,
  phone       TEXT,
  team        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────── Customer & site ───────────────────────────

CREATE TABLE customers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  first_name      TEXT NOT NULL,
  last_name       TEXT NOT NULL,
  email           TEXT NOT NULL,
  phone           TEXT,
  password_hash   TEXT,                          -- set once the rep issues portal credentials
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_customers_email ON customers(email);

CREATE TABLE service_addresses (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  street        TEXT NOT NULL,
  unit          TEXT,
  city          TEXT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'NY',
  zip           TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE utility_accounts (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id           INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  service_address_id    INTEGER NOT NULL REFERENCES service_addresses(id) ON DELETE CASCADE,
  utility_name          TEXT NOT NULL,
  account_number        TEXT NOT NULL,             -- full value; API masks in list views
  meter_number           TEXT,
  rate_class            TEXT,
  monthly_usage_kwh     REAL,
  historical_usage_json TEXT,                       -- flexible: month->kWh map
  existing_cs_credits   TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────── Projects ───────────────────────────

CREATE TABLE projects (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  name                        TEXT NOT NULL,
  address                     TEXT NOT NULL,
  utility                     TEXT NOT NULL,
  location                    TEXT NOT NULL,
  program_type                TEXT NOT NULL DEFAULT 'CDG' CHECK (program_type IN ('CDG','VDER')),
  capacity_pct_full           REAL NOT NULL DEFAULT 0,
  spots_left                  INTEGER NOT NULL DEFAULT 0,
  payment_type                TEXT,
  term                        TEXT,
  savings_pct                 REAL NOT NULL DEFAULT 5,
  cancellation_terms          TEXT,
  commercial_operation_date   TEXT,
  lmi_required                INTEGER NOT NULL DEFAULT 0,
  is_full                     INTEGER NOT NULL DEFAULT 0,
  created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────── Enrollment core ───────────────────────────

CREATE TABLE enrollments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_code     TEXT NOT NULL UNIQUE,          -- e.g. ENR-2026-000001
  customer_id         INTEGER REFERENCES customers(id),
  service_address_id  INTEGER REFERENCES service_addresses(id),
  utility_account_id  INTEGER REFERENCES utility_accounts(id),
  project_id          INTEGER REFERENCES projects(id),
  sales_rep_id        INTEGER REFERENCES sales_reps(id),
  status              TEXT NOT NULL DEFAULT 'Draft',
  lmi_path            TEXT CHECK (lmi_path IN ('document','self_attestation','not_applicable') OR lmi_path IS NULL),
  created_by_user_id  INTEGER REFERENCES users(id),
  updated_by_user_id  INTEGER REFERENCES users(id),
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_enrollments_status ON enrollments(status);
CREATE INDEX idx_enrollments_rep ON enrollments(sales_rep_id);

CREATE TABLE lmi_qualifications (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id         INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  path                  TEXT NOT NULL CHECK (path IN ('document','self_attestation','not_applicable')),
  qualification_type    TEXT,       -- e.g. 'SNAP award letter', 'Medicaid award letter'
  program_name          TEXT,
  customer_name_on_doc  TEXT,
  issuing_agency        TEXT,
  effective_date        TEXT,
  expiration_date       TEXT,
  document_id           INTEGER REFERENCES documents(id),
  review_result         TEXT,       -- likely_valid / needs_manual_review / likely_invalid
  validation_notes      TEXT,
  household_size        INTEGER,
  income_threshold      REAL,
  attestation_response  TEXT CHECK (attestation_response IN ('below','above') OR attestation_response IS NULL),
  attestation_date      TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE documents (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id         INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  doc_category          TEXT NOT NULL CHECK (doc_category IN (
                           'utility_bill','lmi_document','generated_agreement',
                           'signature_certificate','submission_package_pdf',
                           'submission_package_zip','other'
                         )),
  original_filename     TEXT,
  stored_path           TEXT NOT NULL,          -- relative path only; never exposed raw via API
  mime_type             TEXT,
  file_size             INTEGER,
  uploaded_by_user_id   INTEGER REFERENCES users(id),
  extracted_data_json   TEXT,                    -- raw extraction output
  extraction_confidence REAL,
  corrected_data_json   TEXT,                    -- rep-corrected values, kept separate from raw extraction
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE agreements (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id            INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  document_type            TEXT NOT NULL CHECK (document_type IN (
                              'subscription_agreement','cdg_disclosure','income_survey',
                              'esign_consent','credit_contact_consent','terms_privacy'
                            )),
  template_version         TEXT NOT NULL DEFAULT 'v1',
  effective_date           TEXT,
  generated_at             TEXT,
  generated_document_id    INTEGER REFERENCES documents(id),
  status                   TEXT NOT NULL DEFAULT 'generated' CHECK (status IN ('generated','signed')),
  created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────── Signing ───────────────────────────

CREATE TABLE signing_sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id   INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  token           TEXT NOT NULL UNIQUE,
  signer_name     TEXT,
  signer_email    TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','expired')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at      TEXT NOT NULL,
  completed_at    TEXT
);

CREATE TABLE signatures (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id      INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  agreement_id       INTEGER REFERENCES agreements(id),
  signing_session_id INTEGER REFERENCES signing_sessions(id),
  field_key          TEXT NOT NULL,          -- e.g. 'subscription_signature', 'disclosure_initial_1'
  field_type         TEXT NOT NULL CHECK (field_type IN ('signature','initial')),
  method             TEXT CHECK (method IN ('typed','drawn','adopted')),
  value_text         TEXT,                    -- typed/adopted signature text
  value_image_path   TEXT,                    -- drawn signature, saved as PNG
  signer_name        TEXT NOT NULL,
  signer_email       TEXT NOT NULL,
  page_label         TEXT,
  completed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE signature_events (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  signing_session_id  INTEGER REFERENCES signing_sessions(id),
  event_type          TEXT NOT NULL CHECK (event_type IN (
                         'session_opened','document_viewed','field_completed',
                         'session_completed','session_expired'
                       )),
  field_key           TEXT,
  metadata_json        TEXT,
  ip_address          TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────── Validation / QA / submission ───────────────────────────

CREATE TABLE validation_results (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id        INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  document_id          INTEGER REFERENCES documents(id),
  validation_type      TEXT NOT NULL CHECK (validation_type IN ('utility_bill','lmi_document')),
  classification       TEXT CHECK (classification IN ('likely_valid','needs_manual_review','likely_invalid')),
  confidence           REAL,
  reasons_json         TEXT,
  missing_info_json    TEXT,
  mismatch_warnings_json TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE qa_reviews (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  reviewer_user_id    INTEGER NOT NULL REFERENCES users(id),
  decision            TEXT NOT NULL CHECK (decision IN ('approved','rejected','needs_work')),
  correction_reason   TEXT,      -- standardized code, e.g. 'address_mismatch','lmi_doc_expired'
  notes               TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE submissions (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id              INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  submitted_by_user_id       INTEGER REFERENCES users(id),
  package_zip_document_id    INTEGER REFERENCES documents(id),
  package_pdf_document_id    INTEGER REFERENCES documents(id),
  submitted_at               TEXT NOT NULL DEFAULT (datetime('now')),
  developer_status           TEXT NOT NULL DEFAULT 'submitted' CHECK (developer_status IN (
                                'submitted','accepted','rejected','needs_work'
                              )),
  developer_reviewer_user_id INTEGER REFERENCES users(id),
  developer_notes            TEXT,
  assigned_project_id        INTEGER REFERENCES projects(id),
  decided_at                 TEXT
);

-- ─────────────────────────── History & audit ───────────────────────────

CREATE TABLE status_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id     INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  previous_status   TEXT,
  new_status        TEXT NOT NULL,
  changed_by_user_id INTEGER REFERENCES users(id),
  reason            TEXT,
  notes             TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id  INTEGER REFERENCES enrollments(id),
  user_id        INTEGER REFERENCES users(id),
  action         TEXT NOT NULL,        -- document_accessed / status_changed / enrollment_edited / login / ...
  details_json   TEXT,
  ip_address     TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_enrollment ON audit_logs(enrollment_id);
