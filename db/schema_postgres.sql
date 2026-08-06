-- Dalton Solar Enrollment Platform — PostgreSQL schema
-- This mirrors schema_sqlite.sql exactly (same tables, columns, constraints).
-- The prototype runs on SQLite; this file is what you'd run against Postgres
-- when you're ready to migrate. Differences from the SQLite file are only
-- dialect mechanics: SERIAL instead of AUTOINCREMENT, TIMESTAMPTZ instead of
-- TEXT dates, BOOLEAN instead of INTEGER 0/1, JSONB instead of TEXT for JSON
-- columns. No table, column, or relationship was changed.

CREATE TABLE users (
  id              SERIAL PRIMARY KEY,
  email           TEXT NOT NULL UNIQUE,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('sales_rep','qa_reviewer','admin','developer')),
  full_name       TEXT NOT NULL,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sales_reps (
  id          SERIAL PRIMARY KEY,
  user_id     INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  rep_code    TEXT NOT NULL UNIQUE,
  phone       TEXT,
  team        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customers (
  id              SERIAL PRIMARY KEY,
  first_name      TEXT NOT NULL,
  last_name       TEXT NOT NULL,
  email           TEXT NOT NULL,
  phone           TEXT,
  password_hash   TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_customers_email ON customers(email);

CREATE TABLE service_addresses (
  id            SERIAL PRIMARY KEY,
  customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  street        TEXT NOT NULL,
  unit          TEXT,
  city          TEXT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'NY',
  zip           TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE utility_accounts (
  id                    SERIAL PRIMARY KEY,
  customer_id           INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  service_address_id    INTEGER NOT NULL REFERENCES service_addresses(id) ON DELETE CASCADE,
  utility_name          TEXT NOT NULL,
  account_number        TEXT NOT NULL,
  meter_number          TEXT,
  rate_class            TEXT,
  monthly_usage_kwh     NUMERIC,
  historical_usage_json JSONB,
  existing_cs_credits   TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
  id                          SERIAL PRIMARY KEY,
  name                        TEXT NOT NULL,
  address                     TEXT NOT NULL,
  utility                     TEXT NOT NULL,
  location                    TEXT NOT NULL,
  program_type                TEXT NOT NULL DEFAULT 'CDG' CHECK (program_type IN ('CDG','VDER')),
  capacity_pct_full           NUMERIC NOT NULL DEFAULT 0,
  spots_left                  INTEGER NOT NULL DEFAULT 0,
  payment_type                TEXT,
  term                        TEXT,
  savings_pct                 NUMERIC NOT NULL DEFAULT 5,
  cancellation_terms          TEXT,
  commercial_operation_date   DATE,
  lmi_required                BOOLEAN NOT NULL DEFAULT FALSE,
  is_full                     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE enrollments (
  id                  SERIAL PRIMARY KEY,
  enrollment_code     TEXT NOT NULL UNIQUE,
  customer_id         INTEGER REFERENCES customers(id),
  service_address_id  INTEGER REFERENCES service_addresses(id),
  utility_account_id  INTEGER REFERENCES utility_accounts(id),
  project_id          INTEGER REFERENCES projects(id),
  sales_rep_id        INTEGER REFERENCES sales_reps(id),
  status              TEXT NOT NULL DEFAULT 'Draft',
  lmi_path            TEXT CHECK (lmi_path IN ('document','self_attestation','not_applicable')),
  created_by_user_id  INTEGER REFERENCES users(id),
  updated_by_user_id  INTEGER REFERENCES users(id),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_enrollments_status ON enrollments(status);
CREATE INDEX idx_enrollments_rep ON enrollments(sales_rep_id);

CREATE TABLE documents (
  id                    SERIAL PRIMARY KEY,
  enrollment_id         INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  doc_category          TEXT NOT NULL CHECK (doc_category IN (
                           'utility_bill','lmi_document','generated_agreement',
                           'signature_certificate','submission_package_pdf',
                           'submission_package_zip','other'
                         )),
  original_filename     TEXT,
  stored_path           TEXT NOT NULL,
  mime_type             TEXT,
  file_size             INTEGER,
  uploaded_by_user_id   INTEGER REFERENCES users(id),
  extracted_data_json   JSONB,
  extraction_confidence NUMERIC,
  corrected_data_json   JSONB,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE lmi_qualifications (
  id                    SERIAL PRIMARY KEY,
  enrollment_id         INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  path                  TEXT NOT NULL CHECK (path IN ('document','self_attestation','not_applicable')),
  qualification_type    TEXT,
  program_name          TEXT,
  customer_name_on_doc  TEXT,
  issuing_agency        TEXT,
  effective_date        DATE,
  expiration_date       DATE,
  document_id           INTEGER REFERENCES documents(id),
  review_result         TEXT,
  validation_notes      TEXT,
  household_size        INTEGER,
  income_threshold      NUMERIC,
  attestation_response  TEXT CHECK (attestation_response IN ('below','above')),
  attestation_date      TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agreements (
  id                       SERIAL PRIMARY KEY,
  enrollment_id            INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  document_type            TEXT NOT NULL CHECK (document_type IN (
                              'subscription_agreement','cdg_disclosure','income_survey',
                              'esign_consent','credit_contact_consent','terms_privacy'
                            )),
  template_version         TEXT NOT NULL DEFAULT 'v1',
  effective_date           DATE,
  generated_at             TIMESTAMPTZ,
  generated_document_id    INTEGER REFERENCES documents(id),
  status                   TEXT NOT NULL DEFAULT 'generated' CHECK (status IN ('generated','signed')),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE signing_sessions (
  id              SERIAL PRIMARY KEY,
  enrollment_id   INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  token           TEXT NOT NULL UNIQUE,
  signer_name     TEXT,
  signer_email    TEXT,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','expired')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  completed_at    TIMESTAMPTZ
);

CREATE TABLE signatures (
  id                 SERIAL PRIMARY KEY,
  enrollment_id      INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  agreement_id       INTEGER REFERENCES agreements(id),
  signing_session_id INTEGER REFERENCES signing_sessions(id),
  field_key          TEXT NOT NULL,
  field_type         TEXT NOT NULL CHECK (field_type IN ('signature','initial')),
  method             TEXT CHECK (method IN ('typed','drawn','adopted')),
  value_text         TEXT,
  value_image_path   TEXT,
  signer_name        TEXT NOT NULL,
  signer_email       TEXT NOT NULL,
  page_label         TEXT,
  completed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE signature_events (
  id                  SERIAL PRIMARY KEY,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  signing_session_id  INTEGER REFERENCES signing_sessions(id),
  event_type          TEXT NOT NULL CHECK (event_type IN (
                         'session_opened','document_viewed','field_completed',
                         'session_completed','session_expired'
                       )),
  field_key           TEXT,
  metadata_json       JSONB,
  ip_address          TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE validation_results (
  id                      SERIAL PRIMARY KEY,
  enrollment_id           INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  document_id             INTEGER REFERENCES documents(id),
  validation_type         TEXT NOT NULL CHECK (validation_type IN ('utility_bill','lmi_document')),
  classification          TEXT CHECK (classification IN ('likely_valid','needs_manual_review','likely_invalid')),
  confidence               NUMERIC,
  reasons_json             JSONB,
  missing_info_json        JSONB,
  mismatch_warnings_json   JSONB,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE qa_reviews (
  id                  SERIAL PRIMARY KEY,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  reviewer_user_id    INTEGER NOT NULL REFERENCES users(id),
  decision            TEXT NOT NULL CHECK (decision IN ('approved','rejected','needs_work')),
  correction_reason   TEXT,
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE submissions (
  id                         SERIAL PRIMARY KEY,
  enrollment_id              INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  submitted_by_user_id       INTEGER REFERENCES users(id),
  package_zip_document_id    INTEGER REFERENCES documents(id),
  package_pdf_document_id    INTEGER REFERENCES documents(id),
  submitted_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  developer_status           TEXT NOT NULL DEFAULT 'submitted' CHECK (developer_status IN (
                                'submitted','accepted','rejected','needs_work'
                              )),
  developer_reviewer_user_id INTEGER REFERENCES users(id),
  developer_notes            TEXT,
  assigned_project_id        INTEGER REFERENCES projects(id),
  decided_at                 TIMESTAMPTZ
);

CREATE TABLE status_history (
  id                  SERIAL PRIMARY KEY,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  previous_status     TEXT,
  new_status          TEXT NOT NULL,
  changed_by_user_id  INTEGER REFERENCES users(id),
  reason              TEXT,
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_logs (
  id             SERIAL PRIMARY KEY,
  enrollment_id  INTEGER REFERENCES enrollments(id),
  user_id        INTEGER REFERENCES users(id),
  action         TEXT NOT NULL,
  details_json   JSONB,
  ip_address     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_enrollment ON audit_logs(enrollment_id);
