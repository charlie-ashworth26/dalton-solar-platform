import os

# MUST run before any module reads os.environ. A VS Code restart previously
# produced a shell with no Perch variables; app.py did not load .env and
# PERCH_API_MODE silently defaulted to mock, which made a live reconciliation
# call return 501 "Not implemented by this client."
from config_bootstrap import init_configuration, perch_config_report
init_configuration(verbose=False)   # banner is printed once, at server start

import logging

from flask import Flask, jsonify, send_from_directory, render_template, request

from db import close_db, init_db, DB_PATH
from routes import auth_routes, enrollment_routes, document_routes, agreement_routes, admin_routes
from routes import signing_routes, qa_routes, developer_routes, submission_routes
from routes import report_routes, project_routes, perch_routes


def create_app():
    app = Flask(__name__)
    app.teardown_appcontext(close_db)

    if not os.path.exists(DB_PATH):
        init_db()
    else:
        # Existing installations must receive additive migrations too. This is
        # intentionally non-destructive; the migration runner records each file
        # and applies it once.
        from db.migrate import run_migrations
        run_migrations(DB_PATH, verbose=False)

    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(enrollment_routes.bp)
    app.register_blueprint(document_routes.bp)
    app.register_blueprint(agreement_routes.bp)
    app.register_blueprint(signing_routes.bp)
    app.register_blueprint(qa_routes.bp)
    app.register_blueprint(developer_routes.bp)
    app.register_blueprint(submission_routes.bp)
    app.register_blueprint(report_routes.bp)
    app.register_blueprint(project_routes.bp)
    app.register_blueprint(perch_routes.bp)
    app.register_blueprint(admin_routes.bp)

    _configure_logging(app)
    _configure_proxy(app)

    @app.route("/api/environment", methods=["GET"])
    def environment_info():
        """Unauthenticated on purpose: the banner must render on the LOGIN page,
        before anyone has a token. Exposes only the banner text and a mode
        label - never secrets, hosts, or configuration values."""
        return jsonify({
            "banner": environment_banner(),
            "environment": (os.environ.get("DALTON_ENV") or "local").strip().lower(),
        })

    @app.route("/", methods=["GET"])
    def index():
        return render_template("index.html")

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"ok": True})

    @app.route("/sign/<token>", methods=["GET"])
    def signing_page(token):
        return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"), "signing_session.html")

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    return app


def environment_banner():
    """Config-driven warning banner.

    Controlled by DALTON_ENV_BANNER. Absent/empty -> no banner, so a future
    production environment shows nothing unless someone explicitly sets it.
    DALTON_ENV=staging supplies the default text as a convenience.
    """
    text = (os.environ.get("DALTON_ENV_BANNER") or "").strip()
    if not text and (os.environ.get("DALTON_ENV") or "").strip().lower() == "staging":
        text = "TEST ENVIRONMENT — DO NOT ENTER REAL CUSTOMER INFORMATION"
    return text or None


def _configure_logging(app):
    """Plain stdout/stderr logging. Render captures both, so nothing more is
    needed for this milestone. Deliberately minimal."""
    level = (os.environ.get("DALTON_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    app.logger.setLevel(getattr(logging, level, logging.INFO))

    @app.errorhandler(Exception)
    def _unhandled(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        # Log the full traceback for us; return an opaque message to the client
        # so internals are never exposed.
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        return jsonify({"error": "An unexpected server error occurred."}), 500


def _configure_proxy(app):
    """Behind Render's load balancer, request.remote_addr is the PROXY.

    ProxyFix is applied ONLY when DALTON_TRUSTED_PROXY_COUNT says how many
    proxies we actually control - the same explicit opt-in the contract
    acceptance IP logic already uses. Never blanket-trusts X-Forwarded-For.
    """
    try:
        hops = int(os.environ.get("DALTON_TRUSTED_PROXY_COUNT", "0"))
    except (TypeError, ValueError):
        hops = 0
    if hops > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops,
                                x_host=hops, x_prefix=0)
        app.logger.info("ProxyFix enabled for %d trusted proxy hop(s)", hops)


app = create_app()

if __name__ == "__main__":
    from config_bootstrap import format_startup_banner
    print(format_startup_banner(perch_config_report(),
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
                                if os.path.exists(os.path.join(
                                    os.path.dirname(os.path.abspath(__file__)), ".env")) else None,
                                []))
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
