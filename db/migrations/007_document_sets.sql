-- 007_document_sets.sql
-- One logical document -> one or more uploaded files.
--
-- A rep photographing a 3-page bill produces three files that are ONE bill.
-- Previously each upload was an independent `documents` row and extraction ran
-- per file, so page 2 of a bill was parsed as if it were a whole bill.
--
-- A set is scoped to exactly one enrollment. Files carry an explicit
-- page_order, so grouping and ordering are deterministic rather than depending
-- on upload timing or filename sorting.

CREATE TABLE document_sets (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
  category            TEXT NOT NULL,        -- utility_bill | lmi_document | other
  -- Extraction outcome for the SET, not per file.
  extraction_status   TEXT,                 -- success|partial|unreadable|ocr_unavailable|unsupported|error
  extraction_provider TEXT,
  extracted_data_json TEXT,
  -- NULL when the provider cannot express a defensible confidence.
  extraction_confidence REAL,
  extraction_issues_json TEXT,
  created_by_user_id  INTEGER REFERENCES users(id),
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_docsets_enrollment ON document_sets(enrollment_id, category);

-- Existing documents rows keep working; new uploads join a set.
ALTER TABLE documents ADD COLUMN document_set_id INTEGER REFERENCES document_sets(id);
ALTER TABLE documents ADD COLUMN page_order INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_documents_set ON documents(document_set_id, page_order);
