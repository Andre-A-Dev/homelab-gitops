"""Filament spools — plain CRUD, one spool marked as loaded.

Consumption is booked manually for now (weigh the spool, update remaining_g).
Automatic booking from PrusaLink job data comes once it is verified which
field the firmware actually reports for used filament.
"""

from flask import Blueprint, jsonify, request

from app import db

bp = Blueprint("filament", __name__, url_prefix="/api/filament")

# field -> (type, required on create)
FIELDS = {
    "material": (str, True),
    "color": (str, True),
    "manufacturer": (str, False),
    "diameter_mm": (float, False),
    "total_weight_g": (float, True),
    "remaining_g": (float, False),
    "spool_weight_g": (float, False),
    "cost_eur": (float, False),
    "purchased_at": (str, False),
    "notes": (str, False),
}


def _clean(body: dict, creating: bool) -> tuple[dict, str | None]:
    out = {}
    for name, (typ, required) in FIELDS.items():
        if name not in body or body[name] in (None, ""):
            if creating and required:
                return {}, f"{name} is required."
            continue
        try:
            value = typ(body[name])
        except (TypeError, ValueError):
            return {}, f"{name} must be a {typ.__name__}."
        if typ is float and value < 0:
            return {}, f"{name} cannot be negative."
        if typ is str:
            value = value.strip()
        out[name] = value
    if creating:
        out.setdefault("remaining_g", out["total_weight_g"])
    return out, None


def _row(row) -> dict:
    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    return d


@bp.get("")
def list_spools():
    rows = db.get_db().execute(
        "SELECT * FROM spools ORDER BY is_active DESC, material, color"
    ).fetchall()
    return jsonify([_row(r) for r in rows])


@bp.post("")
def create_spool():
    data, error = _clean(request.get_json(silent=True) or {}, creating=True)
    if error:
        return jsonify(ok=False, message=error), 400
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    conn = db.get_db()
    cur = conn.execute(f"INSERT INTO spools ({cols}) VALUES ({marks})", tuple(data.values()))
    conn.commit()
    row = conn.execute("SELECT * FROM spools WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_row(row)), 201


@bp.patch("/<int:spool_id>")
def update_spool(spool_id: int):
    body = request.get_json(silent=True) or {}
    data, error = _clean(body, creating=False)
    if error:
        return jsonify(ok=False, message=error), 400

    conn = db.get_db()
    if conn.execute("SELECT 1 FROM spools WHERE id = ?", (spool_id,)).fetchone() is None:
        return jsonify(ok=False, message="Spool not found."), 404

    with conn:  # one transaction: only one spool can be loaded at a time
        if body.get("is_active") is True:
            conn.execute("UPDATE spools SET is_active = 0 WHERE is_active = 1")
            conn.execute("UPDATE spools SET is_active = 1 WHERE id = ?", (spool_id,))
        elif body.get("is_active") is False:
            conn.execute("UPDATE spools SET is_active = 0 WHERE id = ?", (spool_id,))
        if data:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE spools SET {sets} WHERE id = ?", (*data.values(), spool_id))

    row = conn.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    return jsonify(_row(row))


@bp.delete("/<int:spool_id>")
def delete_spool(spool_id: int):
    conn = db.get_db()
    cur = conn.execute("DELETE FROM spools WHERE id = ?", (spool_id,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, message="Spool not found."), 404
    return jsonify(ok=True)
