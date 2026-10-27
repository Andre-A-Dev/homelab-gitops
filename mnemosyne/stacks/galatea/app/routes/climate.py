"""Enclosure climate — read-only."""

from flask import Blueprint, jsonify

from app.clients import shelly
from app.metrics import UPSTREAM_ERRORS

bp = Blueprint("climate", __name__, url_prefix="/api/climate")


@bp.get("")
def climate():
    try:
        return jsonify(reachable=True, **shelly.get_climate())
    except shelly.UpstreamError:
        UPSTREAM_ERRORS.labels("climate").inc()
        return jsonify(reachable=False), 503
