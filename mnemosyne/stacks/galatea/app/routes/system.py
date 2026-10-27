"""UI page, action log, liveness and metrics."""

from flask import Blueprint, Response, jsonify, render_template
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import db

bp = Blueprint("system", __name__)


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/actions")
def actions():
    rows = db.get_db().execute(
        "SELECT ts, action, result, detail FROM actions ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/healthz")
def healthz():
    # Liveness only: "the process serves requests". Deliberately independent of
    # PrusaLink and the plug — a switched-off printer must not get Galatea
    # restarted (under k3s a failing liveness probe kills the pod).
    return "ok"


@bp.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
