-- Magic-link customer access tokens.
--
-- NOT a second auth system. A validated token results in a call to the EXISTING
-- auth.issue_customer_token(), producing the same customer JWT normal login
-- issues. No new scope, no new permission model.
--
-- The RAW token is never stored, logged or audited - only SHA-256(token). The
-- raw value exists exactly twice: in the response that hands it to the rep, and
-- in the request body the customer's browser posts back. It is delivered via a
-- URL FRAGMENT, which browsers never send to the server, so it cannot appear in
-- reverse-proxy or access logs.
CREATE TABLE customer_access_tokens (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  -- SHA-256 hex of the raw token. UNIQUE so a lookup is an indexed equality
  -- match on the hash and never a scan over candidates.
  token_hash          TEXT    NOT NULL UNIQUE,
  -- Bound to exactly ONE enrollment AND the customer on it. Redemption checks
  -- both; a mismatch fails rather than falling back to anything.
  enrollment_id       INTEGER NOT NULL REFERENCES enrollments(id),
  customer_id         INTEGER NOT NULL REFERENCES customers(id),
  expires_at          TEXT    NOT NULL,
  used_at             TEXT,
  revoked_at          TEXT,
  created_by_user_id  INTEGER REFERENCES users(id),
  created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_customer_access_tokens_enrollment
  ON customer_access_tokens(enrollment_id);
