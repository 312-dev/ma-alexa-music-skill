"""The standalone Flask deployment of Ampere.

An adapter, and nothing else. Every decision this service makes lives in
`core`, which imports no web framework at all; this file's whole job is to
turn a Flask request into the plain values `core` accepts and to turn the
plain values it returns back into a Flask response.

It is deliberately dull, and the dullness is the measure of whether the split
worked. When this file starts wanting to know something about music, the thing
it wants belongs in `core`.

The reason for the split is `ma_provider/webserver.py`, which is the same set
of routes on aiohttp, running inside Music Assistant's own process. Two
adapters over one core is what lets Ampere be both a standalone service for
anyone with a Subsonic server and a native part of Music Assistant, without
either one being a fork of the other.
"""

from __future__ import annotations

import os

from flask import Blueprint, Flask, Response, jsonify, redirect, request, send_file

from ma_provider import core
from ma_provider import queue_api
import setup_ui

# The wizard's catalog sync runs on a thread of its own, started here because
# it belongs to the process that is serving the wizard rather than to the core.
setup_ui.views.start_auto_sync()

app = Flask(__name__)

# Queue handoff for Music Assistant, which composes queues this service has no
# other way to name. The blueprint is built here rather than in `queue_api`
# because that module is imported by `core`, and a Flask import there would put
# a web framework underneath everything Ampere does. Inside Music Assistant
# there is no HTTP hop at all: the provider calls `queue_api.publish` directly.
queue_bp = Blueprint("queue_api", __name__)


@queue_bp.post("/queue")
def publish_queue():
    return respond(queue_api.publish_request(
        request.get_json(silent=True), request.headers))


@queue_bp.get("/queue/<token>")
def show_queue(token: str):
    return respond(queue_api.show_request(token, request.headers))


app.register_blueprint(queue_bp)

# Setup and status at /setup. It refuses to serve at all when ADMIN_TOKEN is
# unset rather than serving open, which is the right default for something that
# can create skills and read the library.
app.register_blueprint(setup_ui.bp)


def respond(result):
    """Turn one of `core`'s five answer shapes into a Flask response.

    The match is exhaustive on purpose. A new answer shape in the core should
    fail loudly here rather than fall through to something that happens to
    render, because the adapters are the two places a shape can be forgotten.
    """
    match result:
        case core.Json(status, payload):
            return jsonify(payload), status
        case core.Redirect(location):
            return redirect(location, code=302)
        case core.LocalFile(path, content_type):
            # conditional=True is what answers a scrub with a real 206 and a
            # Content-Range rather than the whole file from byte zero.
            return send_file(path, mimetype=content_type, conditional=True)
        case core.Page(status, body, content_type, headers):
            return Response(body, status=status, mimetype=content_type,
                            headers=dict(headers))
        case core.Upstream(status, headers, stream):
            return Response(core.iter_chunks(stream), status=status,
                            headers=dict(headers), direct_passthrough=True)
    raise TypeError(f"core returned an answer shape this adapter cannot serve: {result!r}")


@app.post("/music")
@app.post("/")
def music():
    return respond(core.dispatch(
        request.get_json(silent=True),
        request.headers,
        request.get_data(cache=True),
    ))


@app.get("/stream/<song_id>/<int:expires>/<sig>")
def stream(song_id: str, expires: int, sig: str):
    return respond(core.serve_stream(song_id, expires, sig,
                                     request.headers.get("Range")))


@app.get("/mastream/<ref>/<int:expires>/<sig>")
def mastream(ref: str, expires: int, sig: str):
    return respond(core.serve_mastream(ref, expires, sig,
                                       request.headers.get("Range")))


@app.get("/art/<cover_id>/<int:expires>/<sig>")
def art(cover_id: str, expires: int, sig: str):
    return respond(core.serve_art(cover_id, expires, sig,
                                  request.headers.get("Range")))


def _authorized() -> bool:
    return core.admin_authorized(request.remote_addr, request.headers)


@app.get("/healthz")
def healthz():
    return respond(core.healthz())


@app.get("/captures")
def captures():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    return respond(core.captures_listing())


@app.get("/diag")
def diag():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    return respond(core.diagnostics(request.args.get("q", "the")))


@app.get("/oauth/authorize")
def oauth_authorize_form():
    return respond(core.oauth_authorize_page(request.args))


@app.post("/oauth/authorize")
def oauth_authorize_submit():
    return respond(core.oauth_authorize_submit(request.form))


@app.post("/oauth/token")
def oauth_token():
    return respond(core.oauth_token_exchange(
        request.form, request.headers.get("Authorization", "")))


@app.get("/icons/<path:name>")
def icons(name: str):
    return respond(core.icon(name))


@app.get("/")
def landing():
    return respond(core.landing())


@app.get("/privacy")
def privacy():
    return respond(core.privacy())


@app.get("/terms")
def terms():
    return respond(core.terms())


PORT = int(os.environ.get("PORT", "5056"))

# Optional, and a no-op unless MDNS is set. Under gunicorn this runs in the
# worker rather than the master, which is fine at one worker and is why the
# module tolerates a registration that fails because something already holds
# 5353.
core.advertise(PORT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
