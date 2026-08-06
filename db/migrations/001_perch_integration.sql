-- 001_perch_integration.sql
-- Milestone 1: Perch Partner Enrollment API integration.
--
-- Additive only. Nothing is dropped or rewritten — existing enrollments,
-- documents, QA reviews, and signatures are untouched. The legacy `projects`
-- table is intentionally left in place (see ARCHITECTURE_REVIEW.md §2): it is
-- referenced by 9 call sites that are already tested and working, and is
-- demoted to read-only-legacy rather than removed mid-refactor.
--
-- Dialect note: written in the intersection of SQLite and PostgreSQL syntax so
-- the same file runs on both. `TEXT` timestamps here match the existing
-- schema_sqlite.sql convention; schema_postgres.sql uses TIMESTAMPTZ and the
-- Postgres variant of this migration mirrors that.

-- ─────────────── Perch auth tokens (server-side only, never sent to a browser) ───────────────
CREATE TABLE perch_tokens (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  access_token   TEXT NOT NULL,
  token_type     TEXT NOT NULL DEFAULT 'Bearer',
  scope          TEXT,
  issued_at      TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at     TEXT NOT NULL,
  is_active      INTEGER NOT NULL DEFAULT 1,
  api_mode       TEXT NOT NULL DEFAULT 'mock',   -- 'mock' | 'live'; prevents a mock token being reused against staging
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_perch_tokens_active ON perch_tokens(is_active, expires_at);

-- ─────────────── Every Perch request/response, against the Dalton enrollment ID ───────────────
-- This is the audit spine required by the architecture: "Dalton stores every
-- request, response, identifier, timestamp, and error against the internal
-- Enrollment ID."
CREATE TABLE perch_api_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id     INTEGER REFERENCES enrollments(id) ON DELETE CASCADE,
  operation         TEXT NOT NULL,          -- 'get_token' | 'get_capacity' | ...
  http_method       TEXT,
  endpoint          TEXT,
  request_json      TEXT,                   -- credentials/tokens are redacted before write
  response_json     TEXT,
  status_code       INTEGER,
  duration_ms       INTEGER,
  error_message     TEXT,
  api_mode          TEXT NOT NULL DEFAULT 'mock',
  initiated_by_user_id INTEGER REFERENCES users(id),
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_perch_calls_enrollment ON perch_api_calls(enrollment_id);
CREATE INDEX idx_perch_calls_operation ON perch_api_calls(operation);

-- ─────────────── Raw capacity response snapshots (audit; never live truth) ───────────────
-- Architecture requirement: "Store snapshots of API responses for audit
-- purposes, but always revalidate capacity before final submission."
CREATE TABLE perch_capacity_snapshots (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id     INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  perch_api_call_id INTEGER REFERENCES perch_api_calls(id),
  zip_code          TEXT NOT NULL,
  utility_name      TEXT NOT NULL,
  raw_response_json TEXT NOT NULL,
  api_mode          TEXT NOT NULL DEFAULT 'mock',
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_capacity_snapshots_enrollment ON perch_capacity_snapshots(enrollment_id);

-- ─────────────── Normalized products from a snapshot ───────────────
-- Perch owns these values; Dalton stores them only so the UI can render them
-- and an enrollment can reference a selection. Refreshed on every capacity call.
CREATE TABLE perch_products (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id               INTEGER NOT NULL REFERENCES perch_capacity_snapshots(id) ON DELETE CASCADE,
  perch_product_id          TEXT NOT NULL,        -- Perch's identifier, NOT ours
  name                      TEXT,
  customer_type             TEXT,
  savings_percentage        REAL,
  available_capacity_kw     REAL,
  lmi_required              INTEGER NOT NULL DEFAULT 0,
  proof_documents_json      TEXT,                 -- variable-shape per product; JSON is correct here
  next_step                 TEXT,
  utility_name              TEXT,
  raw_product_json          TEXT,                 -- full unmodified product object
  created_at                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_perch_products_snapshot ON perch_products(snapshot_id);

-- ─────────────── Enrollment correlation columns ───────────────
ALTER TABLE enrollments ADD COLUMN service_zip TEXT;
ALTER TABLE enrollments ADD COLUMN utility_name TEXT;
ALTER TABLE enrollments ADD COLUMN selected_perch_product_id INTEGER REFERENCES perch_products(id);
ALTER TABLE enrollments ADD COLUMN perch_enrollment_ref TEXT;
ALTER TABLE enrollments ADD COLUMN perch_customer_ref TEXT;
