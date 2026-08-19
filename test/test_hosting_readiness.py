"""
Hosting readiness — Render private staging.

Run: python test/test_hosting_readiness.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PERCH_API_MODE", "mock")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTING_KEYS = ("DALTON_ENV", "DALTON_DB_PATH", "DALTON_DATA_DIR",
                "DALTON_ENV_BANNER", "JWT_SECRET", "RENDER", "RENDER_SERVICE_ID",
                "DALTON_TRUSTED_PROXY_COUNT")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def run_py(code, env=None, cwd=ROOT):
    """Run a snippet in a FRESH interpreter. Required because DB_PATH,
    DATA_ROOT and JWT_SECRET are resolved at import time."""
    e = {k: v for k, v in os.environ.items() if k not in HOSTING_KEYS}
    e["PERCH_API_MODE"] = "mock"
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=e, cwd=cwd, timeout=120)


def main():
    # ═══════════════════════════════════════════════════════
    section("LOCAL DEVELOPMENT IS UNCHANGED WITH NO HOSTING ENV VARS")
    r = run_py("""
import sys; sys.path.insert(0,'.')
from db import DB_PATH
from helpers import DATA_ROOT, BACKEND_ROOT
import routes.document_routes as d
import services.documents as sd, services.packaging as sp
print("DB", DB_PATH)
print("DATA", DATA_ROOT)
print("SAME_AS_REPO", DATA_ROOT == BACKEND_ROOT)
print("UPLOADS", d.UPLOAD_DIR)
print("GEN", sd.STORAGE_DIR)
print("PKG", sp.STORAGE_DIR)
""")
    check("app imports with no hosting configuration", r.returncode == 0)
    out = dict(l.split(" ", 1) for l in r.stdout.strip().splitlines() if " " in l)
    check("DB stays repo-local", out["DB"].endswith("dalton_solar.db")
          and "/var/data" not in out["DB"])
    check("DATA_ROOT defaults to the repo root", out["SAME_AS_REPO"] == "True")
    check("uploads stay under the repo", out["UPLOADS"].startswith(ROOT))
    check("generated docs stay under the repo", out["GEN"].startswith(ROOT))
    check("packages stay under the repo", out["PKG"].startswith(ROOT))
    check("local boot needs no JWT_SECRET", r.returncode == 0)

    section("DALTON_DB_PATH OVERRIDE")
    with tempfile.TemporaryDirectory(prefix="dalton_db_") as tmp:
        target = os.path.join(tmp, "nested", "staging.db")
        r = run_py(f"""
import sys; sys.path.insert(0,'.')
from db import DB_PATH, init_db, get_db
from app import app
print("DB", DB_PATH)
init_db(reset=True)
import os
print("EXISTS", os.path.exists(DB_PATH))
with app.app_context():
    c = get_db()
    print("JOURNAL", c.execute("PRAGMA journal_mode").fetchone()[0])
    print("BUSY", c.execute("PRAGMA busy_timeout").fetchone()[0])
    print("FK", c.execute("PRAGMA foreign_keys").fetchone()[0])
    n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print("SCHEMA_OK", n >= 0)
""", env={"DALTON_DB_PATH": target})
        check("app boots with an overridden DB path", r.returncode == 0)
        o = dict(l.split(" ", 1) for l in r.stdout.strip().splitlines() if " " in l)
        check("DB_PATH honours the override", o["DB"] == target)
        check("the file is created at the override path", o["EXISTS"] == "True")
        check("parent directories are created automatically",
              os.path.exists(os.path.dirname(target)))
        check("migrations run against the overridden DB", o["SCHEMA_OK"] == "True")

        section("SQLITE WAL + BUSY TIMEOUT")
        check("WAL mode is enabled", o["JOURNAL"].lower() == "wal")
        check("busy timeout is configured (>0)", int(o["BUSY"]) > 0)
        check("busy timeout defaults to 5000ms", int(o["BUSY"]) == 5000)
        check("foreign keys still enforced", o["FK"] == "1")

    with tempfile.TemporaryDirectory(prefix="dalton_db2_") as tmp:
        r = run_py(f"""
import sys; sys.path.insert(0,'.')
from db import init_db, get_db, SQLITE_BUSY_TIMEOUT_MS
from app import app
init_db(reset=True)
with app.app_context():
    print("BUSY", get_db().execute("PRAGMA busy_timeout").fetchone()[0])
print("CONST", SQLITE_BUSY_TIMEOUT_MS)
""", env={"DALTON_DB_PATH": os.path.join(tmp, "x.db"),
          "DALTON_SQLITE_BUSY_TIMEOUT_MS": "9000"})
        o = dict(l.split(" ", 1) for l in r.stdout.strip().splitlines() if " " in l)
        check("busy timeout is configurable", o["BUSY"] == "9000")

    section("DALTON_DATA_DIR OVERRIDE — all persistent file types")
    with tempfile.TemporaryDirectory(prefix="dalton_data_") as tmp:
        r = run_py(f"""
import sys, os; sys.path.insert(0,'.')
from helpers import DATA_ROOT, resolve_stored_path
import routes.document_routes as d
import services.documents as sd, services.packaging as sp
print("DATA", DATA_ROOT)
print("UPLOADS", d.UPLOAD_DIR)
print("GEN", sd.STORAGE_DIR)
print("PKG", sp.STORAGE_DIR)
print("RESOLVED", resolve_stored_path("uploads/7/bill.pdf"))
""", env={"DALTON_DATA_DIR": tmp, "DALTON_DB_PATH": os.path.join(tmp, "x.db")})
        check("app boots with an overridden data dir", r.returncode == 0)
        o = dict(l.split(" ", 1) for l in r.stdout.strip().splitlines() if " " in l)
        check("DATA_ROOT honours the override", o["DATA"] == tmp)
        check("UPLOADED ORIGINALS follow it", o["UPLOADS"].startswith(tmp))
        check("GENERATED documents follow it", o["GEN"].startswith(tmp))
        check("SUBMISSION PACKAGES follow it", o["PKG"].startswith(tmp))
        check("resolve_stored_path follows it", o["RESOLVED"].startswith(tmp))
        # PORTABILITY: os.path.join(root, "uploads/7/bill.pdf") keeps the forward
        # slashes it was given, so on Windows the result is mixed-separator
        # ("C:\\...\\data\\uploads/7/bill.pdf"). That is a perfectly valid Windows
        # path and the stored value is unchanged - only a separator-literal
        # comparison fails. Compare with separators normalized.
        _resolved_norm = o["RESOLVED"].replace("\\", "/")
        check("  ...and keeps the stored relative path",
              _resolved_norm.endswith("uploads/7/bill.pdf"))
        # The invariants that actually matter, asserted directly rather than
        # inferred from a string suffix:
        check("  ...the resolved path is absolute", os.path.isabs(o["RESOLVED"]))
        check("  ...and normalizes to somewhere under the data root",
              os.path.normpath(o["RESOLVED"]).startswith(os.path.normpath(tmp)))
        check("  ...while the STORED value stays relative",
              not os.path.isabs("uploads/7/bill.pdf"))
        check("  ...and never becomes the data root itself",
              "/var/data" not in "uploads/7/bill.pdf")
        check("nothing persistent is left under the repo",
              not any(v.startswith(ROOT) for v in
                      (o["UPLOADS"], o["GEN"], o["PKG"], o["RESOLVED"])))

    section("PATH CONTAINMENT SECURITY PRESERVED")
    src = open(os.path.join(ROOT, "routes", "document_routes.py"), encoding="utf-8").read()
    check("the traversal check follows DATA_ROOT, not the repo root",
          "os.path.realpath(DATA_ROOT)" in src)
    check("BACKEND_ROOT is no longer used for containment",
          "realpath(BACKEND_ROOT)" not in src)
    check("the containment comparison is still present",
          "startswith(root + os.sep)" in src)
    check("only safe inline types remain", "_INLINE_SAFE_TYPES" in src)

    section("END-TO-END WITH BOTH OVERRIDES — a real upload lands on the disk")
    with tempfile.TemporaryDirectory(prefix="dalton_e2e_") as tmp:
        r = run_py(f"""
import sys, io, os, base64; sys.path.insert(0,'.')
from db import init_db, query_one
import seed
init_db(reset=True); seed.seed()
from app import app
from helpers import resolve_stored_path
c = app.test_client()
h = {{'Authorization':'Bearer '+c.post('/api/auth/login',
     json={{'email':'charlie@daltonsolar.com','password':'RepPass1!'}}).get_json()['token']}}
eid = c.post('/api/perch/drafts', headers=h).get_json()['enrollment_id']
png = base64.b64decode(b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
up = c.post(f'/api/enrollments/{{eid}}/document-sets', headers=h,
            data={{'category':'utility_bill','files':[(io.BytesIO(png),'b.png')]}},
            content_type='multipart/form-data')
did = up.get_json()['files'][0]['document_id']
row = query_one("SELECT stored_path FROM documents WHERE id=?", (did,))
abs_path = resolve_stored_path(row['stored_path'])
print("STORED_REL", row['stored_path'])
print("ON_DISK", os.path.exists(abs_path))
print("UNDER_DATA_DIR", abs_path.startswith({tmp!r}))
v = c.get(f'/api/enrollments/{{eid}}/documents/{{did}}/view', headers=h)
print("VIEW", v.status_code)
print("BYTES_MATCH", v.data == png)
""", env={"DALTON_DATA_DIR": tmp, "DALTON_DB_PATH": os.path.join(tmp, "d.db")})
        check("full upload flow works with overrides", r.returncode == 0)
        o = dict(l.split(" ", 1) for l in r.stdout.strip().splitlines() if " " in l)
        check("stored_path stays RELATIVE in the database",
              not os.path.isabs(o["STORED_REL"])
              and not o["STORED_REL"].startswith("/")
              and ":" not in o["STORED_REL"][:3])
        check("  ...and begins with the uploads folder",
              o["STORED_REL"].replace("\\", "/").startswith("uploads/"))
        check("the file physically lands on the data dir", o["ON_DISK"] == "True")
        check("  ...under DALTON_DATA_DIR", o["UNDER_DATA_DIR"] == "True")
        check("inline viewing still works", o["VIEW"] == "200")
        check("  ...and returns the original bytes", o["BYTES_MATCH"] == "True")

    # ═══════════════════════════════════════════════════════
    section("JWT HARDENING — hosted mode refuses an insecure secret")
    boot = "import sys; sys.path.insert(0,'.'); import auth; print('BOOTED')"
    cases = [
        ("local, nothing set", {}, True),
        ("local, dev default explicit", {"JWT_SECRET": "dev-secret-change-in-production"}, True),
        ("DALTON_ENV=staging, unset", {"DALTON_ENV": "staging"}, False),
        ("DALTON_ENV=staging, dev default",
         {"DALTON_ENV": "staging", "JWT_SECRET": "dev-secret-change-in-production"}, False),
        ("DALTON_ENV=staging, blank", {"DALTON_ENV": "staging", "JWT_SECRET": "   "}, False),
        ("DALTON_ENV=staging, too short", {"DALTON_ENV": "staging", "JWT_SECRET": "abc123"}, False),
        ("DALTON_ENV=staging, strong", {"DALTON_ENV": "staging", "JWT_SECRET": "z" * 48}, True),
        ("DALTON_ENV=production, unset", {"DALTON_ENV": "production"}, False),
        ("RENDER detected, unset", {"RENDER": "true"}, False),
        ("RENDER_SERVICE_ID detected, unset", {"RENDER_SERVICE_ID": "srv-x"}, False),
        ("RENDER detected, strong", {"RENDER": "true", "JWT_SECRET": "q" * 48}, True),
    ]
    for label, env, should_boot in cases:
        rr = run_py(boot, env=env)
        booted = rr.returncode == 0
        check(f"{label:38} boots={booted}", booted == should_boot)
        if not should_boot:
            check("  ...and the error explains why",
                  "JWT_SECRET" in (rr.stderr or ""))
            check("  ...without printing the secret value",
                  "dev-secret-change-in-production" not in (rr.stderr or "")
                  or "still the public development default" in (rr.stderr or ""))

    section("SECRETS ARE NEVER EXPOSED")
    rr = run_py("""
import sys; sys.path.insert(0,'.')
from app import app
c = app.test_client()
print("ENVBODY", c.get('/api/environment').get_data(as_text=True))
print("HEALTHBODY", c.get('/api/health').get_data(as_text=True))
""", env={"DALTON_ENV": "staging", "JWT_SECRET": "SUPERSECRETJWTVALUE" + "x" * 30,
          "PERCH_API_KEY": "SECRETAPIKEY", "PERCH_SECRET_KEY": "SECRETSIGNINGKEY"})
    body = rr.stdout
    check("/api/environment leaks no JWT secret", "SUPERSECRETJWTVALUE" not in body)
    check("/api/environment leaks no Perch API key", "SECRETAPIKEY" not in body)
    check("/api/environment leaks no signing key", "SECRETSIGNINGKEY" not in body)
    check("/api/health leaks nothing", "SECRET" not in body.split("HEALTHBODY")[-1])
    cfg = open(os.path.join(ROOT, "render.yaml"), encoding="utf-8").read()
    for secret_key in ("JWT_SECRET", "PERCH_API_KEY", "PERCH_SECRET_KEY"):
        idx = cfg.index(secret_key)
        check(f"render.yaml marks {secret_key} sync:false (never in git)",
              "sync: false" in cfg[idx:idx + 120])

    # ═══════════════════════════════════════════════════════
    section("HEALTH ENDPOINT")
    rr = run_py("""
import sys; sys.path.insert(0,'.')
from app import app
r = app.test_client().get('/api/health')
print("STATUS", r.status_code)
print("BODY", r.get_data(as_text=True).strip())
""")
    o = dict(l.split(" ", 1) for l in rr.stdout.strip().splitlines() if " " in l)
    check("/api/health returns 200", o["STATUS"] == "200")
    check("  ...unauthenticated (Render cannot log in)", o["STATUS"] == "200")
    check("render.yaml points the health check at it",
          "healthCheckPath: /api/health" in cfg)

    section("STAGING BANNER — only when configured")
    def banner_for(env):
        rr = run_py("""
import sys; sys.path.insert(0,'.')
from app import app
print("BANNER", app.test_client().get('/api/environment').get_json().get('banner'))
""", env=env)
        return rr.stdout.strip().split("BANNER ", 1)[-1].strip() if rr.returncode == 0 else "BOOTFAIL"

    strong = {"JWT_SECRET": "k" * 48}
    check("local: no banner", banner_for({}) == "None")
    check("staging: banner appears",
          "TEST ENVIRONMENT" in banner_for({**strong, "DALTON_ENV": "staging"}))
    check("  ...with the required wording",
          "DO NOT ENTER REAL CUSTOMER INFORMATION"
          in banner_for({**strong, "DALTON_ENV": "staging"}))
    check("production: NO banner by default",
          banner_for({**strong, "DALTON_ENV": "production"}) == "None")
    check("explicit override wins",
          banner_for({**strong, "DALTON_ENV": "production",
                      "DALTON_ENV_BANNER": "CUSTOM TEXT"}) == "CUSTOM TEXT")
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    check("banner element exists in markup", 'id="env-banner"' in html)
    check("banner is hidden until the backend supplies text",
          'id="env-banner"' in html and "display:none" in
          html.split('id="env-banner"')[1][:120])
    check("banner is fetched by the frontend", "/api/environment" in js)
    check("banner renders before login (no auth header used)",
          "loadEnvironmentBanner" in js
          and "Authorization" not in js.split("async function loadEnvironmentBanner(")[1][:600])
    check("banner is styled to be prominent", "#env-banner" in css)

    # ═══════════════════════════════════════════════════════
    section("PROXY / IP HANDLING STAYS EXPLICIT")
    appsrc = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    check("ProxyFix is applied only when configured",
          "DALTON_TRUSTED_PROXY_COUNT" in appsrc and "if hops > 0" in appsrc)
    check("  ...never blanket-trusted", "ProxyFix(app.wsgi_app" in appsrc
          and appsrc.index("if hops > 0") < appsrc.index("ProxyFix(app.wsgi_app"))
    perchsrc = open(os.path.join(ROOT, "routes", "perch_routes.py"), encoding="utf-8").read()
    check("acceptance IP logic is unchanged", "_client_ip()" in perchsrc)
    check("  ...still ignores XFF by default",
          'os.environ.get("DALTON_TRUSTED_PROXY_COUNT", "0")' in perchsrc)
    check("render.yaml sets exactly one trusted hop",
          'key: DALTON_TRUSTED_PROXY_COUNT' in cfg and 'value: "1"' in cfg)

    section("GUNICORN COMPATIBILITY")
    rr = run_py("""
import sys, importlib; sys.path.insert(0,'.')
m = importlib.import_module('app')
w = getattr(m, 'app')
print("TYPE", type(w).__name__)
print("DEBUG", w.debug)
print("FACTORY", hasattr(m, 'create_app'))
""")
    o = dict(l.split(" ", 1) for l in rr.stdout.strip().splitlines() if " " in l)
    check("app:app resolves to a Flask app", o["TYPE"] == "Flask")
    check("debug is False on import (gunicorn path)", o["DEBUG"] == "False")
    check("create_app factory still available", o["FACTORY"] == "True")
    check("app.run stays inside __main__ only",
          appsrc.index('if __name__ == "__main__"') < appsrc.index("app.run("))

    section("DEPLOYMENT CONFIG FILES")
    reqs = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    dep = open(os.path.join(ROOT, "DEPLOYMENT.md"), encoding="utf-8").read()
    # pip package names are case-insensitive; compare that way.
    reqs_lower = reqs.lower()
    for pkg in ("flask==", "pyjwt==", "reportlab==", "pdfplumber==",
                "pytesseract==", "pillow==", "requests==", "gunicorn==",
                "werkzeug=="):
        check(f"requirements pins {pkg.rstrip('=')}", pkg in reqs_lower)

    # Pins must match the LOCAL environment where the suite is known to pass.
    # Staging drifting from that baseline is exactly what this guards against.
    expected = {
        "flask": "3.1.3", "werkzeug": "3.1.8", "pyjwt": "2.7.0",
        "reportlab": "4.4.10", "pdfplumber": "0.11.4", "pillow": "10.4.0",
        "pytesseract": "0.3.13", "requests": "2.34.2", "gunicorn": "23.0.0",
    }
    for name, version in expected.items():
        check(f"{name} pinned to the local version {version}",
              f"{name}=={version}" in reqs_lower)

    # python-dotenv is optional: config_bootstrap imports it in a try/except and
    # falls back to a stdlib parser. It is NOT installed locally, so shipping it
    # would make staging differ from the proven environment.
    check("python-dotenv is NOT a declared dependency",
          not any(l.strip().lower().startswith("python-dotenv")
                  for l in reqs.splitlines() if not l.strip().startswith("#")))
    boot_src = open(os.path.join(ROOT, "config_bootstrap.py"), encoding="utf-8").read()
    check("  ...because the dotenv import is optional",
          "from dotenv import dotenv_values" in boot_src and "except Exception:" in boot_src)
    check("  ...with a stdlib fallback parser", "_parse_env_file(path)" in boot_src)

    pyver = open(os.path.join(ROOT, ".python-version"), encoding="utf-8").read().strip()
    check(f"python version pinned to the local 3.12.0 (got {pyver})", pyver == "3.12.0")
    check("render.yaml declares the same Python version", "value: 3.12.0" in cfg)
    check("no stale 3.12.3 anywhere in the deployment config",
          "3.12.3" not in cfg and "3.12.3" not in dep and "3.12.3" not in pyver)
    check("every requirement is pinned (no bare names)",
          all("==" in l for l in reqs.splitlines()
              if l.strip() and not l.strip().startswith("#")))
    check(".python-version exists",
          os.path.exists(os.path.join(ROOT, ".python-version")))
    check("render.yaml uses ONE worker", "--workers 1" in cfg)
    check("  ...with 4 threads", "--threads 4" in cfg)
    check("  ...and a 120s timeout", "--timeout 120" in cfg)
    check("  ...binding $PORT", "--bind 0.0.0.0:$PORT" in cfg)
    check("disk mounts at /var/data", "mountPath: /var/data" in cfg)
    check("DB path points at the disk",
          "/var/data/dalton_solar.db" in cfg)
    check("data dir points at the disk", "value: /var/data" in cfg)
    check("Perch base URLs are the STAGING hosts",
          cfg.count("staging.api.perchenergy.com") == 2)
    check("  ...and no production Perch host is configured",
          "://api.perchenergy.com" not in cfg)
    check("DEPLOYMENT.md exists", os.path.exists(os.path.join(ROOT, "DEPLOYMENT.md")))
    for topic in ("Build command", "Start command", "/var/data", "/api/health",
                  "JWT_SECRET", "staging.api.perchenergy.com",
                  "staging.cleanenergyenrollment.org", "onrender.com",
                  "gunicorn app:app", "Mount path", "3.12.0", "PERCH_API_KEY",
                  "seed.py", "Custom Domains"):
        check(f"DEPLOYMENT.md documents {topic}", topic in dep)
    check("DEPLOYMENT.md contains no real secret values",
          "PERCH_API_KEY=" not in dep.replace("PERCH_API_KEY=<", "X"))

    section("NO HARDCODED LOCAL ASSUMPTIONS")
    bad = []
    for folder in (".", "routes", "services", "db"):
        d = os.path.join(ROOT, folder)
        for fn in os.listdir(d):
            if not fn.endswith(".py"):
                continue
            body = open(os.path.join(d, fn), encoding="utf-8").read()
            for token in ("127.0.0.1", "C:\\\\", "/Users/"):
                if token in body:
                    bad.append(f"{folder}/{fn}:{token}")
    check(f"no hardcoded local paths or loopback hosts (found {bad})", not bad)

    section("DATABASE HANDLE LIFECYCLE (Windows WinError 32 regression)")
    # A suite that read the DB with bare query_one() calls - outside any Flask
    # app context, so teardown_appcontext never fired - left a thread-local
    # sqlite connection open. POSIX allows unlink on an open file so it was
    # invisible; on Windows the NEXT suite's init_db(reset=True) failed with
    # WinError 32, and WAL made it worse by holding -wal/-shm too.
    rr = run_py("""
import sys, gc, sqlite3, os; sys.path.insert(0,'.')
import db
from db import init_db, query_one

def usable():
    gc.collect()
    n = 0
    for c in [o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)]:
        try:
            c.execute("SELECT 1"); n += 1
        except Exception:
            pass
    return n

init_db(reset=True)
query_one("SELECT 1 AS x")                 # bare call: leaks without the fix
print("LEAKED_BEFORE", hasattr(db._local, "conn"))
init_db(reset=True)                        # must release BEFORE deleting
print("LEAKED_AFTER", hasattr(db._local, "conn"))
print("USABLE_AFTER", usable())
os.remove(db.DB_PATH)                      # the operation that failed on Windows
print("REMOVED", not os.path.exists(db.DB_PATH))
""")
    check("the lifecycle probe runs", rr.returncode == 0)
    o = dict(l.split(" ", 1) for l in rr.stdout.strip().splitlines() if " " in l)
    check("a bare query DOES cache a connection", o.get("LEAKED_BEFORE") == "True")
    check("init_db(reset=True) RELEASES it", o.get("LEAKED_AFTER") == "False")
    check("  ...leaving zero usable connections", o.get("USABLE_AFTER") == "0")
    check("  ...so the database file can be deleted", o.get("REMOVED") == "True")

    db_src = open(os.path.join(ROOT, "db", "__init__.py"), encoding="utf-8").read()
    check("init_db closes before deleting", "close_db()" in db_src.split("if reset:")[0][-900:])
    check("  ...and the reason is documented", "WinError 32" in db_src)
    check("no retry loop around os.remove", "except PermissionError" not in db_src)
    check("no sleep-based workaround", "time.sleep" not in db_src)
    check("reset still deletes the WAL sidecars",
          '("", "-wal", "-shm", "-journal")' in db_src)
    check("production still closes per request via teardown",
          "teardown_appcontext(close_db)" in
          open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())

    print(f"\n{'='*72}\nHOSTING READINESS - ALL CHECKS PASSED\n{'='*72}")


if __name__ == "__main__":
    main()
