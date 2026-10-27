"""Printer power (Shelly plug) with the power-off interlock."""

import threading

from flask import Blueprint, jsonify, request

from app import config, db
from app.clients import prusalink, shelly
from app.interlock import evaluate_power_off
from app.metrics import ACTIONS, INTERLOCK_BLOCKS, UPSTREAM_ERRORS

bp = Blueprint("plug", __name__, url_prefix="/api/plug")

# Serializes check-then-switch, so two concurrent requests cannot both pass the
# interlock on the same snapshot. Requires a single worker process (see Dockerfile).
_switch_lock = threading.Lock()


def _decide(snap, force: bool):
    return evaluate_power_off(
        reachable=snap.reachable,
        state=snap.state,
        nozzle_c=snap.nozzle_c,
        nozzle_max_c=config.INTERLOCK_NOZZLE_MAX_C,
        force=force,
    )


@bp.get("")
def state():
    try:
        plug = shelly.get_plug()
    except shelly.UpstreamError:
        UPSTREAM_ERRORS.labels("plug").inc()
        return jsonify(reachable=False), 503

    # Preview only, on cached data — tells the UI whether "Power off" will be
    # refused. The actual switch re-checks with fresh data.
    preview = _decide(prusalink.get_status(), force=False) if plug["on"] else None
    return jsonify(reachable=True, **plug, power_off=preview.to_dict() if preview else None)


@bp.post("/on")
def power_on():
    try:
        shelly.set_plug(True)
    except shelly.UpstreamError as exc:
        UPSTREAM_ERRORS.labels("plug").inc()
        db.log_action("power_on", "error", str(exc))
        ACTIONS.labels("power_on", "error").inc()
        return jsonify(ok=False, message="Plug does not answer. Nothing changed."), 502
    db.log_action("power_on", "done")
    ACTIONS.labels("power_on", "done").inc()
    return jsonify(ok=True, message="Power on.")


@bp.post("/off")
def power_off():
    force = (request.get_json(silent=True) or {}).get("force") is True

    with _switch_lock:
        try:
            if not shelly.get_plug()["on"]:
                return jsonify(ok=True, message="Power is already off.")
        except shelly.UpstreamError as exc:
            UPSTREAM_ERRORS.labels("plug").inc()
            db.log_action("power_off", "error", str(exc))
            ACTIONS.labels("power_off", "error").inc()
            return jsonify(ok=False, message="Plug does not answer. Nothing changed."), 502

        decision = _decide(prusalink.get_status(fresh=True), force)
        if not decision.allowed:
            INTERLOCK_BLOCKS.labels(decision.reason).inc()
            db.log_action("power_off", "blocked", decision.reason)
            ACTIONS.labels("power_off", "blocked").inc()
            return jsonify(ok=False, blocked=True, **decision.to_dict()), 409

        try:
            shelly.set_plug(False)
        except shelly.UpstreamError as exc:
            UPSTREAM_ERRORS.labels("plug").inc()
            db.log_action("power_off", "error", str(exc))
            ACTIONS.labels("power_off", "error").inc()
            return jsonify(ok=False, message="Plug does not answer. Nothing changed."), 502

    result = "forced" if force else "done"
    db.log_action("power_off", result, decision.reason)
    ACTIONS.labels("power_off", result).inc()
    prusalink.invalidate_cache()
    return jsonify(ok=True, message="Power off.")
