"""Galatea — control plane for Pygmalion (Prusa MK4S).

Human auth happens in Caddy (basic auth). Caddy then injects X-Galatea-Token
upstream. The API refuses requests without it, so containers sharing a Docker
network with Galatea cannot switch the printer by bypassing Caddy.
"""

import hmac

from flask import Flask, jsonify, request

from app import config, db
from app.routes import climate, filament, plug, printer, system

# Reachable without the token: the page shell, static assets, probes, metrics.
# None of them change anything.
_OPEN_PREFIXES = ("/static/", "/healthz", "/metrics")


def create_app() -> Flask:
    app = Flask(__name__)
    db.init_db()
    app.teardown_appcontext(db.close_db)

    @app.before_request
    def require_token():
        path = request.path
        if path == "/" or path.startswith(_OPEN_PREFIXES):
            return None
        token = request.headers.get("X-Galatea-Token", "")
        if not hmac.compare_digest(token, config.API_TOKEN):
            return jsonify(ok=False, message="Unauthorized."), 401
        return None

    for module in (system, printer, plug, climate, filament):
        app.register_blueprint(module.bp)
    return app


app = create_app()
