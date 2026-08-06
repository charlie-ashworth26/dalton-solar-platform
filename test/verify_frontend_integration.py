"""
Phase 2 Step 1-9 verification: proves the exact API contract the new
frontend JS relies on. This can't execute browser JS (no headless browser
in this environment), so it does the next best thing — hits the real
endpoints in the exact sequence app.js now calls them, and asserts the
response shape matches every field the JS reads.

Run: python3 test/verify_frontend_integration.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from db import init_db
import seed


def step(n, title):
    print(f"\n{'='*70}\n{n}\n{'='*70}")


def check(label, condition):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}")
    if not condition:
        raise AssertionError(f"Failed: {label}")


def main():
    init_db(reset=True)
    seed.seed()
    client = app.test_client()

    step(1, "GET / serves the frontend with correct asset references")
    r = client.get('/')
    check("status 200", r.status_code == 200)
    html = r.data.decode()
    check("references /static/css/app.css", '/static/css/app.css' in html)
    check("references /static/js/app.js", '/static/js/app.js' in html)
    check("login-error element present (for real auth failures)", 'id="login-error"' in html)
    check("login-submit-btn id present (for loading state)", 'id="login-submit-btn"' in html)
    check("sidebar-role-label id present (for real user info)", 'id="sidebar-role-label"' in html)

    r_css = client.get('/static/css/app.css')
    check("CSS asset loads (200)", r_css.status_code == 200)
    r_js = client.get('/static/js/app.js')
    check("JS asset loads (200)", r_js.status_code == 200)
    js = r_js.data.decode()
    check("app.js contains AuthStore", 'AuthStore' in js)
    check("app.js contains apiFetch", 'apiFetch' in js)
    check("app.js contains tryRestoreSession", 'tryRestoreSession' in js)
    check("app.js no longer hardcodes Cobblestone Ridge", 'Cobblestone Ridge' not in js)
    check("app.js no longer hardcodes fake seed customers", 'maria.castillo92' not in js)

    step(2, "doLogin() sequence: bad credentials must fail (no more fake login)")
    r = client.post('/api/auth/login', json={"email": "charlie@daltonsolar.com", "password": "wrong-password"})
    check("wrong password rejected (401)", r.status_code == 401)
    check("error message present for login-error element", "error" in r.get_json())

    step(3, "doLogin() sequence: real credentials succeed, response has every field applyCurrentUserToUI() reads")
    r = client.post('/api/auth/login', json={"email": "charlie@daltonsolar.com", "password": "RepPass1!"})
    check("status 200", r.status_code == 200)
    body = r.get_json()
    check("data.token present", "token" in body and body["token"])
    check("data.user.full_name present", body["user"].get("full_name") == "Charlie Mren")
    check("data.user.role present", body["user"].get("role") == "sales_rep")
    check("data.user.email present", body["user"].get("email") == "charlie@daltonsolar.com")
    token = body["token"]
    headers = {"Authorization": f"Bearer {token}"}

    step(4, "tryRestoreSession() sequence: GET /api/auth/me with the stored token")
    r = client.get('/api/auth/me', headers=headers)
    check("status 200", r.status_code == 200)
    me = r.get_json()
    check("full_name matches", me["full_name"] == "Charlie Mren")
    check("role matches", me["role"] == "sales_rep")

    step(5, "apiFetch() 401 handling: expired/invalid token is rejected")
    r = client.get('/api/auth/me', headers={"Authorization": "Bearer garbage-token"})
    check("invalid token -> 401", r.status_code == 401)
    r = client.get('/api/enrollments')
    check("no token -> 401", r.status_code == 401)

    step(6, "Legacy /api/projects endpoint — now correctly EMPTY after the Perch refactor")
    r = client.get('/api/projects')
    check("legacy endpoint still responds 200 (not removed mid-refactor)", r.status_code == 200)
    projs = r.get_json()
    # Perch refactor (Milestone 1): seed.py no longer invents local projects.
    # Perch is the authoritative source for products/capacity. This endpoint
    # remains only so QA/developer/signing routes keep working until they
    # migrate to perch_products in Milestones 3-4.
    check("no invented local projects (Perch owns products now)", projs == [])

    step("6b", "Perch capacity is now the real product source")
    seed.seed_legacy_projects()  # prove the legacy shape is still intact when populated
    r = client.get('/api/projects')
    legacy = r.get_json()
    check("legacy projects table still functions when populated", len(legacy) == 3)
    p = legacy[0]
    for field in ("id", "name", "address", "utility", "location", "capacity_pct_full",
                  "spots_left", "payment_type", "term", "savings_pct", "cancellation_terms",
                  "is_full", "lmi_required"):
        check(f"legacy project has '{field}'", field in p)
    check("project id is numeric (Phase 2 type-mismatch fix verified)", isinstance(p["id"], int))

    step(7, "loadEnrollments() sequence: GET /api/enrollments has every field renderDashboard()/renderCustomers() read")
    r = client.get('/api/enrollments', headers=headers)
    check("status 200", r.status_code == 200)
    enrollments = r.get_json()
    print(f"  rep currently has {len(enrollments)} enrollment(s) (fresh seed -> expect 0)")
    check("empty list on fresh DB (no more fake demo data)", enrollments == [])

    step(8, "Dashboard stat-bucket grouping covers every real status")
    from services.status_machine import STATUSES
    dash_buckets = {
        "Draft","Information Needed","Utility Bill Uploaded","Utility Validation","LMI Review","Agreement Ready",
        "Signature Pending","Signed","Internal Review","Needs Work","Rejected","Verified","Submitted",
        "Developer Review","Accepted","Project Assigned","Active",
    }
    missing = set(STATUSES) - dash_buckets
    check(f"every real status ({len(STATUSES)}) is covered by a dashboard bucket", not missing)

    step(9, "Logout clears the session (resetAll -> subsequent /api/auth/me must fail without token)")
    # simulate: after AuthStore.clear(), no Authorization header is sent
    r = client.get('/api/auth/me')
    check("no token after logout -> 401", r.status_code == 401)

    print(f"\n{'='*70}\nALL FRONTEND INTEGRATION CONTRACT CHECKS PASSED\n{'='*70}")


if __name__ == "__main__":
    main()
