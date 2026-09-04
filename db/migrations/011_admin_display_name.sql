-- Correct the admin account's DISPLAY NAME on existing databases.
--
-- WHY THIS IS NEEDED: seed.py's upsert_user() returns early when a user already
-- exists, so it never updates full_name. An earlier seed created this row with
-- "Jordan Ellis"; seed.py was corrected later, but deployed databases kept the
-- stale value because nothing rewrites an existing row.
--
-- Scope is deliberately minimal: ONE column, ONE row, matched on both email and
-- role. Email, password_hash, role, permissions and every other user are
-- untouched. No account is created.
UPDATE users
   SET full_name = 'Charles Ashworth'
 WHERE email = 'admin@daltonsolar.com'
   AND role  = 'admin';
