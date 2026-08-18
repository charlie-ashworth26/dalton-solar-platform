"""
Seed script — creates one test user per role.

Product/project seeding was removed in the Perch refactor (Milestone 1):
Perch is now the authoritative source for available products, capacity,
savings percentages, and LMI requirements. See ARCHITECTURE_REVIEW.md §2.

Run: python3 seed.py
"""
import os

from db import init_db, execute, query_one
from auth import hash_password


def upsert_user(email, password, role, full_name):
    existing = query_one("SELECT * FROM users WHERE email = ?", (email,))
    if existing:
        return existing["id"]
    cur = execute(
        "INSERT INTO users (email, password_hash, role, full_name) VALUES (?, ?, ?, ?)",
        (email, hash_password(password), role, full_name),
    )
    return cur.lastrowid


def seed():
    init_db(reset=True)

    admin_id = upsert_user("admin@daltonsolar.com", "AdminPass1!", "admin", "ADMIN ACCOUNT")
    rep_user_id = upsert_user("charlie@daltonsolar.com", "RepPass1!", "sales_rep", "Charlie Mren")
    qa_id = upsert_user("qa@daltonsolar.com", "QaPass1!", "qa_reviewer", "Sam Rivera")
    dev_id = upsert_user("developer@perchenergy.com", "DevPass1!", "developer", "Arcadia Review Team")

    rep = query_one("SELECT * FROM sales_reps WHERE user_id = ?", (rep_user_id,))
    if not rep:
        execute("INSERT INTO sales_reps (user_id, rep_code, phone, team) VALUES (?, ?, ?, ?)",
                (rep_user_id, "REP-001", "(315) 555-0100", "Field Sales"))

    # NOTE (Perch refactor, Milestone 1): project/product seeding removed.
    # Perch is now the authoritative source for available products, capacity,
    # savings percentages, and LMI requirements — see ARCHITECTURE_REVIEW.md §2.
    # The legacy `projects` table still exists and is still referenced by the
    # QA, developer, signing, and submission routes; it is deliberately left
    # in place but is no longer populated with invented data. Those call sites
    # migrate to perch_products in Milestones 3-4, after which the table drops.

    print("Seed complete.")
    print("  admin@daltonsolar.com        / AdminPass1!   (admin)")
    print("  charlie@daltonsolar.com      / RepPass1!     (sales_rep)")
    print("  qa@daltonsolar.com           / QaPass1!      (qa_reviewer)")
    print("  developer@perchenergy.com    / DevPass1!     (developer)")
    print()
    print("  Products/capacity now come from Perch (PERCH_API_MODE=%s)."
          % (os.environ.get("PERCH_API_MODE") or "mock"))


def seed_legacy_projects():
    """Legacy project rows, kept ONLY so the existing Phase 1 end-to-end test
    (which exercises the pre-Perch agreement/QA/submission path) still has
    data to run against. Not called by seed() — call it explicitly from a test.
    Deleted once those routes migrate to perch_products."""
    projects = [
        ("Cobblestone Ridge", "4410 County Route 22, Hartwick, NY 13348", "National Grid", "Hartwick, NY",
         "CDG", 90, 41, "ACH", "20 years", 10, "Cancel anytime with 60 days notice", "2020-05-08", 0, 1),
        ("Birchfield Commons", "118 Fenwick Rd, Amsterdam, NY 12010", "National Grid", "Amsterdam, NY",
         "CDG", 5, 1023, "Not required", "Life of the program", 5, "No fees", "2022-03-31", 1, 0),
        ("Otter Creek Solar", "762 Otter Creek Rd, Malone, NY 12953", "National Grid", "Malone, NY",
         "CDG", 62, 210, "ACH", "25 years", 8, "Cancel anytime with 30 days notice", "2023-01-14", 1, 0),
    ]
    for p in projects:
        existing = query_one("SELECT * FROM projects WHERE name = ?", (p[0],))
        if not existing:
            execute(
                """INSERT INTO projects (name, address, utility, location, program_type, capacity_pct_full,
                   spots_left, payment_type, term, savings_pct, cancellation_terms, commercial_operation_date,
                   lmi_required, is_full)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                p,
            )


if __name__ == "__main__":
    seed()
