import os

# MUST run before any module reads os.environ. A VS Code restart previously
# produced a shell with no Perch variables; app.py did not load .env and
# PERCH_API_MODE silently defaulted to mock, which made a live reconciliation
# call return 501 "Not implemented by this client."
from config_bootstrap import init_configuration, perch_config_report
init_configuration(verbose=False)   # banner is printed once, at server start

from flask import Flask, jsonify, send_from_directory, render_template

from db import close_db, init_db, DB_PATH
from routes import auth_routes, enrollment_routes, document_routes, agreement_routes
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
