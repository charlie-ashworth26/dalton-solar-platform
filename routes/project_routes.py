from flask import Blueprint, jsonify

from db import query

bp = Blueprint("project_routes", __name__, url_prefix="/api/projects")


@bp.route("", methods=["GET"])
def list_projects():
    rows = query("SELECT * FROM projects ORDER BY id")
    return jsonify([dict(r) for r in rows])
