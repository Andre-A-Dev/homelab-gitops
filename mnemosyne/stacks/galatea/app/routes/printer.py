"""Printer status and job control.

Every command re-reads the printer state fresh and checks that the command
makes sense *now*, instead of trusting what the UI showed a few seconds ago.
"""

from flask import Blueprint, jsonify, request

from app import db
from app.clients import prusalink
from app.metrics import ACTIONS, UPSTREAM_ERRORS

bp = Blueprint("printer", __name__, url_prefix="/api/printer")


def _reply(ok: bool, message: str, status: int = 200):
    return jsonify(ok=ok, message=message), status


def _command(action: str, allowed_states: set[str], call):
    snap = prusalink.get_status(fresh=True)
    if not snap.reachable:
        UPSTREAM_ERRORS.labels("prusalink").inc()
        db.log_action(action, "unreachable")
        ACTIONS.labels(action, "unreachable").inc()
        return _reply(False, "Printer does not answer. Nothing was sent.", 503)

    if snap.state == "ATTENTION" and "ATTENTION" not in allowed_states:
        # The printer waits for a decision on its display; the API cannot answer it.
        db.log_action(action, "refused", "state=ATTENTION")
        ACTIONS.labels(action, "refused").inc()
        return _reply(False, "Printer shows a dialog. Answer it on the printer display first.", 409)

    if snap.state not in allowed_states or snap.job_id is None:
        db.log_action(action, "refused", f"state={snap.state}")
        ACTIONS.labels(action, "refused").inc()
        return _reply(False, f"Not possible while the printer is {str(snap.state).lower()}.", 409)

    try:
        code = call(snap.job_id)
    except prusalink.UpstreamError as exc:
        UPSTREAM_ERRORS.labels("prusalink").inc()
        db.log_action(action, "error", str(exc))
        ACTIONS.labels(action, "error").inc()
        return _reply(False, "Printer did not accept the command. Check the printer display.", 502)

    if code == 204:
        db.log_action(action, "done", f"job={snap.job_id}")
        ACTIONS.labels(action, "done").inc()
        return _reply(True, f"{action.capitalize()} sent.")

    # 404/409: the printer changed state between our check and the command.
    db.log_action(action, "refused", f"upstream={code}")
    ACTIONS.labels(action, "refused").inc()
    return _reply(False, "Printer state changed in the meantime. Nothing happened.", 409)


@bp.get("/status")
def status():
    return jsonify(prusalink.get_status().to_dict())


@bp.post("/pause")
def pause():
    return _command("pause", {"PRINTING"}, prusalink.pause)


@bp.post("/resume")
def resume():
    return _command("resume", {"PAUSED"}, prusalink.resume)


@bp.post("/stop")
def stop():
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return _reply(False, "Stopping cannot be undone. Send confirm=true.", 400)
    # ATTENTION is allowed here: aborting is often exactly the answer to a
    # dialog (e.g. filament runout). Whether the firmware accepts DELETE in that
    # state is undocumented — a 409 from the printer is handled like any other.
    return _command("stop", {"PRINTING", "PAUSED", "ATTENTION"}, prusalink.stop)
