"""The /setup blueprint.

Deliberately imports nothing from app: the parent registers this blueprint on
that module, so a module-level import back would be circular. The two places
that genuinely need the bridge's own station logic import it inside the view
function instead. Everything else comes from the environment, which is where
app reads it from too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone

from flask import (Blueprint, jsonify, make_response, redirect,
                   render_template, request)
from itsdangerous import BadSignature, URLSafeTimedSerializer

import catalog_sync
import logring
import smapi_rest
import subsonic

from . import (access, captures, qr, smapi, state as store,
               steps as wizard_steps, validate)

bp = Blueprint(
    "setup", __name__,
    url_prefix="/setup",
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

logger = logging.getLogger("ma-music-skill.setup")

TOKENS = validate.Tokens()

_COOKIE = "ampere_setup"
_SESSION_MAX_AGE = 12 * 3600

# A browser cannot set X-Admin-Token on a normal navigation, so the header auth
# app uses everywhere else is swapped for a signed cookie carrying a digest of
# the same token. Rotating ADMIN_TOKEN therefore invalidates every session.
_FALLBACK_KEY = os.urandom(32)


def _signing_key() -> bytes:
    return os.environ.get("SIGNING_KEY", "").encode() or _FALLBACK_KEY


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_key(), salt="ampere-setup-session")


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN") or ""


def authed() -> bool:
    expected = _admin_token()
    if not expected:
        return False
    supplied = request.headers.get("X-Admin-Token")
    if supplied and hmac.compare_digest(supplied, expected):
        return True
    cookie = request.cookies.get(_COOKIE)
    if not cookie:
        return False
    try:
        value = _serializer().loads(cookie, max_age=_SESSION_MAX_AGE)
    except BadSignature:
        return False
    return hmac.compare_digest(str(value), _fingerprint(expected))


_OPEN = {"setup.static", "setup.login", "setup.verify",
         "setup.oauth_callback"}


@bp.context_processor
def _template_defaults() -> dict:
    """Values every template in this blueprint can assume are present.

    container_hint is the hostname, which inside Docker is the container id, so
    the command shown on the sign-in page is one the reader can paste rather
    than a placeholder they have to go and resolve first.
    """
    try:
        rows = wizard_steps.progress(store.load())
        done = sum(1 for r in rows if r["done"])
        total = len(rows)
    except Exception:
        done, total = 0, len(wizard_steps.STEPS)
    return {"container_hint": socket.gethostname() or "ampere",
            "setup_complete": done == total,
            "steps_done": done,
            "steps_total": total}


def secure_cookie() -> bool:
    """Whether the session cookie may carry the Secure flag.

    Derived from this connection, not from PUBLIC_BASE. The admin plane is
    deliberately reached over a LAN or a tailnet rather than the public origin,
    and that is usually plain http. A Secure cookie on a plain http response is
    discarded by the browser without a word, so signing in appeared to do
    nothing at all: the redirect fired, the cookie evaporated on the way, and
    the next request arrived unauthenticated and rendered the form again.

    X-Forwarded-Proto is honored only from a trusted proxy, for the same
    reason X-Forwarded-For is: otherwise the client decides its own answer.
    """
    if request.is_secure:
        return True
    if access.peer_is_trusted(request.remote_addr):
        proto = request.headers.get("X-Forwarded-Proto", "")
        return proto.split(",")[0].strip().lower() == "https"
    return False


def request_ip() -> str:
    return access.client_ip(
        request.remote_addr, request.headers.get("X-Forwarded-For")
    )


@bp.before_request
def _guard():
    if request.endpoint == "setup.static":
        return None
    if not _admin_token():
        # Serving this open would hand anyone who finds the URL the ability to
        # run ask against the operator's Amazon account. Refusing is the only
        # safe default, and saying why beats a blank 404.
        return render_template("disabled.html"), 503

    ip = request_ip()

    # The verify route is the one thing here that is deliberately reachable
    # from anywhere: its entire purpose is to be opened on a phone that is off
    # the WiFi, to prove the endpoint answers from the public internet. It
    # carries a random short-lived token and reveals nothing.
    forwarded = request.headers.get("X-Forwarded-For")
    if request.endpoint not in ("setup.verify", "setup.oauth_callback") and (
        access.forwarded_untrusted(request.remote_addr, forwarded)
        or not access.address_allowed(ip)
    ):
        # Amazon never needs the admin plane, so there is no cost to refusing
        # it everywhere Amazon might be calling from.
        return render_template(
            "blocked.html", ip=ip,
            networks=os.environ.get("SETUP_ALLOW_NETWORKS") or "private",
            proxied=bool(forwarded),
            trusted=os.environ.get("TRUSTED_PROXIES") or "",
        ), 403

    if request.endpoint in _OPEN:
        return None

    if remaining := access.locked_out(ip):
        return render_template("lockout.html", seconds=remaining), 429
    if authed():
        return None
    return render_template("login.html", target=request.path, failed=False), 401


# --- helpers ----------------------------------------------------------------


def fragment(template: str, back: str = "/setup/wizard", **context):
    """Render a partial, wrapped in the layout when HTMX is not driving.

    Every page has to work without JavaScript, and a bare partial returned to a
    plain form post is a wall of unstyled text with no way back.
    """
    if request.headers.get("HX-Request"):
        return render_template(template, **context)
    return render_template("fragment.html", fragment=template, back=back, **context)


def public_base() -> str:
    return os.environ.get("PUBLIC_BASE", "").rstrip("/")


# Capture naming and ordering live in one module, because the wizard's step
# model needs them too and cannot import this one without a cycle.
log_dir = captures.log_dir
recent_captures = captures.recent
_capture_time = captures.capture_time


def ago(when: float) -> str:
    delta = max(0, int(time.time() - when))
    if delta < 60:
        return f"{delta} seconds ago"
    if delta < 3600:
        return f"{delta // 60} minutes ago"
    if delta < 86400:
        return f"{delta // 3600} hours ago"
    return f"{delta // 86400} days ago"


def soon(delta: float) -> str:
    """The mirror of ago, for something that has not happened yet."""
    seconds = int(delta)
    if seconds <= 0:
        return "due now"
    if seconds < 60:
        return f"in {seconds} seconds"
    if seconds < 3600:
        return f"in {seconds // 60} minutes"
    if seconds < 86400:
        return f"in {seconds // 3600} hours"
    return f"in {seconds // 86400} days"


def last_requests(limit: int = 4) -> list[dict]:
    """The newest inbound captures, parsed enough to describe them."""
    out = []
    for name, stamp in recent_captures(limit):
        directive = name.split("-", 1)[-1][:-5] or name
        signed = None
        try:
            body = json.loads((log_dir() / name).read_text())
            header = (body.get("body") or {}).get("header") or {}
            if header.get("namespace"):
                directive = f"{header['namespace']}.{header.get('name', '?')}"
            headers = body.get("headers") or {}
            signed = any(k.lower().startswith("signature") for k in headers)
        except (OSError, ValueError, AttributeError):
            pass
        if "?" in directive:
            # The endpoint stamps "?" for a JSON body with no Alexa envelope
            # (a health probe, a curl smoke test), so the file is literally
            # named "?.?". Say what it was instead of echoing the filename.
            directive = "unrecognized request (not an Alexa directive)"
        out.append({"directive": directive, "when": stamp, "ago": ago(stamp),
                    "signed": signed})
    return out


def _logs_context() -> dict:
    records = list(logring.RING.records)[-200:]
    records.reverse()
    rows = [dict(r, ago=ago(r["at"])) for r in records]
    captures = []
    try:
        for name, when in recent_captures(8):
            try:
                body = json.loads((log_dir() / name).read_text())
            except (OSError, ValueError):
                body = {}
            headers = body.get("headers") or {}
            captures.append({
                "name": name,
                "ago": ago(when),
                "signed": any(k.lower().startswith("signature")
                              for k in headers),
                "pretty": json.dumps(
                    {"request": body.get("body"),
                     "response": body.get("response",
                                          "not recorded (older capture)")},
                    indent=2)[:4000],
            })
    except OSError:
        pass
    return {"records": rows, "captures": captures}


@bp.get("/logs")
def logs_page():
    return render_template("logs.html", **_logs_context())


@bp.get("/logs/tail")
def logs_tail():
    return fragment("_logs.html", **_logs_context())


def subsonic_probe() -> dict:
    """The same cheap search /diag uses."""
    try:
        result = subsonic.search("the", songs=3)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    songs = result.get("song") or []
    noun = "result" if len(songs) == 1 else "results"
    return {"ok": True,
            "detail": f"{len(songs)} {noun} for a sample search",
            "sample": [s.get("title") for s in songs[:3]]}


# SMAPI reads mean shelling out to `ask` once per catalog, which is seconds
# each. The volatile panel polls every ten seconds and must never pay that, so
# it reads this cache and only an explicit refresh recomputes it.
_SMAPI_CACHE: dict = {"at": 0.0, "value": None}
_SMAPI_TTL = 300


def catalog_ids(current: dict) -> dict[str, str]:
    stored = current.get("catalogs") or {}
    kinds = ("artists", "albums", "tracks", "playlists", "genres")
    return {
        kind: os.environ.get(f"CATALOG_{kind.upper()}", "") or stored.get(kind, "")
        for kind in kinds
    }


def smapi_snapshot(force: bool = False) -> dict | None:
    if not force and _SMAPI_CACHE["value"] is not None:
        if time.time() - _SMAPI_CACHE["at"] < _SMAPI_TTL:
            return _SMAPI_CACHE["value"]
    if not force and _SMAPI_CACHE["value"] is None:
        return None

    current = store.load()
    skill_id = os.environ.get("SKILL_ID", "") or current.get("skill_id", "")
    snapshot = {
        "connected": smapi_rest.connected(),
        "skill_id": skill_id,
        "enablement": None,
        "catalogs": [],
        "worst": smapi.classify_ingestion(None),
    }
    if not (snapshot["connected"] and skill_id):
        _SMAPI_CACHE.update(at=time.time(), value=snapshot)
        return snapshot

    try:
        enabled = smapi_rest.enablement_status(skill_id)
    except Exception:
        enabled = False
    snapshot["enablement"] = {
        "ok": enabled,
        "detail": (
            "Enabled for development."
            if enabled else
            "The skill is not enabled, so Alexa answers from your default music "
            "provider and says so out loud, while this bridge looks healthy "
            "because it is never asked anything."
        ),
    }

    rank = {"failed": 0, "none": 1, "waiting": 2, "ok": 3}
    worst = None
    uploads = current.get("uploads") or {}
    for kind, catalog_id in catalog_ids(current).items():
        upload_id = uploads.get(kind)
        if not (catalog_id and upload_id):
            continue
        try:
            state, detail = smapi_rest.ingestion_verdict(
                smapi_rest.upload_status(catalog_id, upload_id))
        except Exception as exc:
            state, detail = "failed", str(exc)[:200]
        verdict = {"state": "ok" if state == "ready" else state, "detail": detail}
        snapshot["catalogs"].append({"kind": kind, "id": catalog_id, **verdict})
        if worst is None or rank[verdict["state"]] < rank[worst["state"]]:
            worst = verdict
    snapshot["worst"] = worst or smapi.classify_ingestion(None)
    _SMAPI_CACHE.update(at=time.time(), value=snapshot)
    return snapshot


def status_context(force: bool = False) -> dict:
    current = store.load()
    return {
        "public_base": public_base(),
        "after_content": os.environ.get("AFTER_CONTENT", "stop"),
        "signing_key_set": bool(os.environ.get("SIGNING_KEY")),
        "admin_token_set": bool(_admin_token()),
        "subsonic": subsonic_probe(),
        "subsonic_url": os.environ.get("SUBSONIC_URL", "") or current.get("subsonic_url", ""),
        "requests": last_requests(),
        "smapi": smapi_snapshot(force),
        "checked_ago": ago(_SMAPI_CACHE["at"]) if _SMAPI_CACHE["at"] else "never",
        "alias": current.get("alias", ""),
        "binding": _binding_now(current),
        "health": _binding_health(),
        "scheduler": scheduler_context(current),
        "state": current,
    }


def scheduler_context(state: dict) -> dict:
    """What the background thread is going to do, and when.

    The scheduler was previously invisible: it logged to a ring buffer that a
    restart empties, so "when did the keep-alive last run" could only be
    answered by catching it in the act. For a process whose whole job is to
    act while nobody is watching, that is the wrong default.
    """
    now = time.time()
    hours = int(state.get("auto_sync_hours") or 0)
    sync_due = 0.0
    if hours:
        hours = max(AUTO_SYNC_MIN_HOURS, hours)
        sync_due = (state.get("last_auto_sync") or 0) + hours * 3600
    keepalive_due = 0.0
    if (BINDING_KEEPALIVE_HOURS > 0
            and state.get("enabled") and state.get("skill_id")):
        keepalive_due = ((state.get("enabled_at") or 0)
                         + BINDING_KEEPALIVE_HOURS * 3600)
    # Whole hours are the normal case and "4 hours" reads better than "4.0".
    keepalive_hours = (int(BINDING_KEEPALIVE_HOURS)
                       if BINDING_KEEPALIVE_HOURS == int(BINDING_KEEPALIVE_HOURS)
                       else BINDING_KEEPALIVE_HOURS)
    return {
        "sync_hours": hours,
        "keepalive_hours": keepalive_hours,
        "sync_last": (ago(state["last_auto_sync"])
                      if state.get("last_auto_sync") else "never"),
        "sync_due": soon(sync_due - now) if sync_due else "",
        "keepalive_due": soon(keepalive_due - now) if keepalive_due else "",
        "armed": bool(state.get("reactive_armed", True)),
        "cycled_ago": (ago(state["reactive_cycled_at"])
                       if state.get("reactive_cycled_at") else ""),
        "misses": int(state.get("reactive_misses") or 0),
    }


# --- login ------------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", target=request.args.get("next", "/setup"),
                               failed=False)
    ip = request_ip()
    if remaining := access.locked_out(ip):
        return render_template("lockout.html", seconds=remaining), 429

    supplied = (request.form.get("token") or "").strip()
    if not hmac.compare_digest(supplied, _admin_token()):
        # Counted per address. A single shared token is otherwise guessable at
        # whatever rate the reverse proxy will pass through.
        access.record_failure(ip)
        return render_template("login.html", target=request.form.get("target", "/setup"),
                               failed=True), 401
    access.record_success(ip)
    target = request.form.get("target") or "/setup"
    if not target.startswith("/setup"):
        target = "/setup"
    response = make_response(redirect(target))
    response.set_cookie(
        _COOKIE,
        _serializer().dumps(_fingerprint(_admin_token())),
        max_age=_SESSION_MAX_AGE, httponly=True, samesite="Lax",
        secure=secure_cookie(),
    )
    return response


@bp.get("/logout")
def logout():
    response = make_response(redirect("/setup/login"))
    response.delete_cookie(_COOKIE)
    return response


# --- status -----------------------------------------------------------------


@bp.get("")
@bp.get("/")
def landing():
    """Setup first, status afterwards.

    Before the skill exists the status page has nothing to report and the
    wizard is the only thing worth showing. After it exists the wizard is done
    and status is what gets visited repeatedly.
    """
    if not wizard_steps.complete(store.load()):
        return redirect("/setup/wizard")
    return render_template("status.html", **status_context())


@bp.get("/status")
def status():
    """The status page on its own, reachable even mid-setup."""
    return render_template("status.html", **status_context())


@bp.get("/status/panel")
def status_partial():
    return render_template("_status.html", **status_context())


@bp.post("/status/refresh")
def status_refresh():
    return fragment("_status.html", back="/setup", **status_context(force=True))


# --- endpoint validation ----------------------------------------------------


def run_checks(base: str) -> list[dict]:
    rows = [validate.check_scheme(base)]
    later = ("Resolves to a public address", "TLS handshake and certificate",
             "GET /healthz over the public URL", "POST /music with a real directive")
    if not rows[0]["ok"]:
        rows += [validate.check(name, None, "Skipped: fix PUBLIC_BASE first.")
                 for name in later]
        return rows
    rows.append(validate.check_address(base))
    rows.append(validate.check_tls(base))
    rows.append(validate.check_healthz(base))
    rows.append(validate.check_music_post(base))
    return rows


def endpoint_context() -> dict:
    base = public_base()
    rows = run_checks(base)
    passed = all(row["ok"] for row in rows)
    cert_type = next((r["note"] for r in rows if r["name"].startswith("TLS")), "")
    store.update(endpoint_ok=passed, cert_type=cert_type)
    token = request.args.get("token") or ""
    return {
        "public_base": base,
        "rows": rows,
        "passed": passed,
        "cert_type": cert_type,
        "proof": proof_context(token),
        "ttl_minutes": TOKENS.ttl // 60,
    }


def proof_context(token: str) -> dict:
    if not token:
        return {"token": "", "status": "none", "url": "", "svg": "", "code": ""}
    url = f"{public_base()}/setup/verify/{token}"
    matrix = qr.encode(url)
    return {
        "token": token,
        "status": TOKENS.status(token),
        "url": url,
        "svg": qr.svg(matrix) if matrix else "",
        # Read out loud or typed by hand when the QR will not scan.
        "code": token,
    }


@bp.get("/endpoint")
def endpoint():
    return render_template("endpoint.html", **endpoint_context())


@bp.get("/endpoint/checks")
def endpoint_checks():
    return fragment("_checks.html", back="/setup/endpoint", **endpoint_context())


@bp.post("/endpoint/proof")
def endpoint_proof_new():
    token = TOKENS.mint()
    if not request.headers.get("HX-Request"):
        return redirect(f"/setup/endpoint?token={token}")
    return render_template("_proof.html", proof=proof_context(token),
                           ttl_minutes=TOKENS.ttl // 60)


@bp.get("/endpoint/proof")
def endpoint_proof():
    return render_template("_proof.html",
                           proof=proof_context(request.args.get("token", "")),
                           ttl_minutes=TOKENS.ttl // 60)


@bp.get("/verify/<token>")
def verify(token: str):
    """Hit by a phone on cellular, so it carries no cookie and cannot require one.

    This is the only check that proves the route Amazon actually takes. Every
    other test here runs from inside the network the bridge is on, where a
    tailnet address or a split-horizon DNS answer looks perfectly healthy.
    """
    outcome = TOKENS.mark_seen(token, request.headers.get("User-Agent", ""))
    codes = {"seen": 200, "expired": 410, "unknown": 404}
    return render_template("verify.html", outcome=outcome), codes[outcome]


# --- alias ------------------------------------------------------------------


def _configuration_gate():
    """Configuration pages open once setup is complete.

    Before that the wizard owns these values: a knob changed here would either
    be overwritten by a later step or would configure a skill that does not
    exist yet. The wizard's own endpoints stay open; this gates only the
    standalone pages.
    """
    if not wizard_steps.complete(store.load()):
        return redirect("/setup/wizard")
    return None


@bp.route("/alias", methods=["GET", "POST"])
def alias():
    if (gate := _configuration_gate()) is not None:
        return gate
    candidate = (request.values.get("candidate") or "").strip()
    result = validate.assess_alias(candidate, subsonic) if candidate else None
    template = "_alias.html" if request.headers.get("HX-Request") else "alias.html"
    return render_template(template, candidate=candidate, result=result,
                           saved=store.load().get("alias", ""))


@bp.post("/alias/save")
def alias_save():
    candidate = (request.form.get("candidate") or "").strip()
    store.update(alias=candidate)
    result = validate.assess_alias(candidate, subsonic) if candidate else None
    return fragment("_alias.html", back="/setup/alias", candidate=candidate,
                    result=result, saved=candidate)


# --- stations ---------------------------------------------------------------


def station_context() -> dict:
    current = store.load()
    import app as bridge  # lazy: app registers this blueprint, so not at import

    return {
        "modes": bridge.AFTER_CONTENT_MODES,
        "live": {
            "after_content": bridge.effective_after_content(),
            "radio_artists": bridge.effective_radio_artists(),
            "radio_tracks_per_artist": bridge.effective_radio_tracks_per_artist(),
            "shuffle_playlists": bridge.shuffle_by_default("pl:probe"),
        },
        "saved": {
            "after_content": current.get("after_content") or bridge.AFTER_CONTENT,
            "radio_artists": current.get("radio_artists") or bridge.RADIO_ARTISTS,
            "radio_tracks_per_artist": (current.get("radio_tracks_per_artist")
                                        or bridge.RADIO_TRACKS_PER_ARTIST),
            "shuffle_playlists": bool(current.get("shuffle_playlists")),
        },
    }


@bp.get("/stations")
def stations():
    if (gate := _configuration_gate()) is not None:
        return gate
    return render_template("stations.html", stored=None, **station_context())


@bp.post("/stations")
def stations_save():
    if (gate := _configuration_gate()) is not None:
        return gate
    import app as bridge

    mode = (request.form.get("after_content") or "").strip().lower()
    if mode not in bridge.AFTER_CONTENT_MODES:
        mode = bridge.AFTER_CONTENT

    def positive(field: str, fallback: int) -> int:
        try:
            return max(1, min(100, int(request.form.get(field, ""))))
        except (TypeError, ValueError):
            return fallback

    store.update(
        after_content=mode,
        radio_artists=positive("radio_artists", bridge.RADIO_ARTISTS),
        radio_tracks_per_artist=positive("radio_tracks_per_artist",
                                         bridge.RADIO_TRACKS_PER_ARTIST),
        shuffle_playlists=bool(request.form.get("shuffle_playlists")),
    )
    # Pools already cached were built under the old numbers and would pin a
    # station to its old shape until the cache turned over on its own.
    bridge._RADIO_CACHE.clear()
    bridge._QUEUE_CACHE.clear()
    return render_template("stations.html", stored=True, **station_context())


@bp.get("/stations/preview")
def stations_preview():
    """The artist pool a seed actually produces.

    A station that had quietly degraded to its seed artist alone was found by
    ear, over hours. It is one glance here.
    """
    if (gate := _configuration_gate()) is not None:
        return gate
    seed = (request.args.get("seed") or "").strip()
    if not seed:
        return fragment("_pool.html", back="/setup/stations", seed="", error="", pool=None)

    import app as bridge

    try:
        hits = subsonic.search(seed, songs=0, albums=0, artists=5)
        artists = hits.get("artist") or []
        if not artists:
            return fragment("_pool.html", back="/setup/stations", seed=seed, pool=None,
                            error=f"No artist matching {seed} in the library.")
        chosen = artists[0]
        artist_ids = bridge.similar_artist_ids(chosen["id"])
        tracks = bridge.radio_pool(chosen["id"], artist_ids)
    except Exception as exc:
        return fragment("_pool.html", back="/setup/stations", seed=seed, pool=None,
                        error=f"Could not build the pool: {exc}")

    counts: dict[str, int] = {}
    for track in tracks:
        counts[track.get("artist") or "Unknown"] = counts.get(track.get("artist") or "Unknown", 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return fragment(
        "_pool.html", back="/setup/stations", seed=seed, error="",
        pool={
            "artist": chosen.get("name", seed),
            "artist_count": len(artist_ids),
            "track_count": len(tracks),
            "rows": ordered,
            "degraded": len(artist_ids) <= 1,
        },
    )


# --- wizard -----------------------------------------------------------------


CATALOG_KINDS = {
    "artists": "AMAZON.MusicGroup",
    "albums": "AMAZON.MusicAlbum",
    "tracks": "AMAZON.MusicRecording",
    "playlists": "AMAZON.MusicPlaylist",
    "genres": "AMAZON.Genre",
}


_VENDORS = {"at": 0.0, "value": None}


def vendor_prefill() -> dict:
    """The vendor the account already knows, so the form arrives filled in.

    Cached for five minutes: it is one SMAPI call per render otherwise, and
    vendors change roughly never. An account with several vendors gets them
    all, for a dropdown rather than an env-var errand.
    """
    if not smapi_rest.connected():
        return {"detected": "", "options": [], "error": ""}
    now = time.time()
    if _VENDORS["value"] is not None and now - _VENDORS["at"] < 300:
        return _VENDORS["value"]
    try:
        options = [(v.get("id", ""), v.get("name", ""))
                   for v in smapi_rest.vendors()]
        forced = (os.environ.get("VENDOR_ID") or "").strip()
        detected = forced or (options[0][0] if len(options) == 1 else "")
        value = {"detected": detected, "options": options, "error": ""}
    except Exception as exc:
        value = {"detected": "", "options": [], "error": str(exc)}
    _VENDORS.update(at=now, value=value)
    return value


def wizard_context(**extra) -> dict:
    current = store.load()
    context = {
        "state": current,
        "vendor": vendor_prefill(),
        "public_base": public_base(),
        "amazon": {
            "connected": smapi_rest.connected(),
            "redirect_uri": smapi_rest.redirect_uri(),
            "scopes": smapi_rest.SCOPES,
            # Kept so anyone who already has the CLI working can carry on.
            "cli_on_path": smapi.ask_on_path(),
            "cli_configured": smapi.ask_configured(),
        },
        "catalog_ids": catalog_ids(current),
        "kinds": CATALOG_KINDS,
        "subsonic_url": os.environ.get("SUBSONIC_URL", "") or current.get("subsonic_url", ""),
        "subsonic_user": os.environ.get("SUBSONIC_USER", "") or current.get("subsonic_user", ""),
        "message": None,
        "ingestion": None,
    }
    context.update(extra)
    return context


def step_completed(template: str, **context):
    """Answer a step form whose work succeeded.

    A fragment swap updates the step body but not the rail or the Continue
    button, so a step that just completed still looks locked. HX-Refresh tells
    the browser to reload the page instead, which re-derives everything.
    Failures keep the fragment swap so the error lands inline next to the form.
    """
    if request.headers.get("HX-Request"):
        response = make_response("", 204)
        response.headers["HX-Refresh"] = "true"
        return response
    return fragment(template, **context)


def _manifest_verdict(skill_id: str, tries: int = 8, delay: float = 1.5) -> str:
    """Wait out the async manifest validation that follows skill creation.

    Creation returning a skillId is not acceptance: validation runs after,
    and its failure is otherwise silent until catalog association 404s. An
    empty return means validated, or still pending after a patient wait;
    a non-empty return is Amazon's own error text.
    """
    for attempt in range(tries):
        try:
            status = smapi_rest.skill_status(skill_id)
        except Exception:
            return ""
        last = (status.get("manifest") or {}).get("lastUpdateRequest") or {}
        state = last.get("status", "")
        if state == "SUCCEEDED":
            return ""
        if state == "FAILED":
            return ("; ".join(e.get("message", "")
                              for e in last.get("errors") or [])
                    or "manifest validation failed")
        if attempt + 1 < tries:
            time.sleep(delay)
    return ""


def _existing_music_skills(exclude: str = "") -> list[dict]:
    """Music skills already on the vendor, which would compete for the alias.

    Alexa routes an invocation across every enabled music skill, so a leftover
    from an earlier install fights the new skill for the same words. Surfaced
    before creation, not discovered by ear afterwards.
    """
    if not smapi_rest.connected():
        return []
    found = []
    try:
        for summary in smapi_rest.list_skills():
            if summary.get("skillId") == exclude:
                continue
            if "music" not in (summary.get("apis") or []):
                continue
            name = next(iter((summary.get("nameByLocale") or {}).values()), "")
            found.append({"id": summary.get("skillId", ""), "name": name,
                          "stage": summary.get("stage", "")})
    except Exception:
        return []
    return found


def step_context(step_key: str) -> dict:
    state = store.load()
    rows = wizard_steps.progress(state)
    step = wizard_steps.BY_KEY[step_key]
    index = [r["key"] for r in rows].index(step_key)
    row = rows[index]
    context = {
        "rows": rows,
        "step": row,
        "step_template": step.template,
        "previous": rows[index - 1]["key"] if index > 0 else None,
        "next": rows[index + 1]["key"] if index + 1 < len(rows) else None,
        **wizard_context(),
    }
    if step_key == "skill":
        if state.get("skill_id") and not row["done"]:
            # Recorded here, missing on Amazon: deleted outside the wizard.
            context["skill_missing"] = True
        elif not state.get("skill_id"):
            context["existing_skills"] = _existing_music_skills()
    if step_key == "upload":
        context["upload_job"] = dict(_UPLOAD)
        context["auto_min_hours"] = AUTO_SYNC_MIN_HOURS
        context["auto_last_ago"] = (ago(state["last_auto_sync"])
                                    if state.get("last_auto_sync") else "")
    if step_key == "enable":
        context["binding"] = _binding_now(state)
    return context


_BINDING = {"at": 0.0, "value": None}


def _binding_now(state: dict, force: bool = False) -> dict:
    """What Amazon says about the enablement right now.

    The stored enabled flag records that the wizard once cycled it; only the
    live answer knows whether a later catalog upload silently unbound the
    skill. When the lookup itself fails, say unknown rather than guessing.
    Cached briefly: the status panel polls every ten seconds and must not
    turn that into a SMAPI call apiece."""
    now = time.time()
    if not force and _BINDING["value"] and now - _BINDING["at"] < 30:
        out = dict(_BINDING["value"])
    else:
        skill_id = state.get("skill_id") or os.environ.get("SKILL_ID", "")
        out = {"known": False, "bound": False, "cycled_ago": ""}
        if skill_id and smapi_rest.connected():
            try:
                out["bound"] = smapi_rest.enablement_status(skill_id)
                out["known"] = True
            except Exception:
                pass
        _BINDING.update(at=now, value=dict(out))
    if state.get("enabled_at"):
        out["cycled_ago"] = ago(state["enabled_at"])
    return out


def _cycle_enablement(skill_id: str) -> dict:
    """Re-provision the skill, then delete and set the enablement.

    The manifest re-put is the load-bearing part, found the hard way: a
    freshly created skill can sit half provisioned on Amazon's side, where
    searches route and resolve but Playback.Initiate never comes, and no
    amount of enablement cycling alone fixes it. Re-putting the manifest
    forces the music-provider provisioning to run again; cycling on top of
    that fresh registration is what actually restored playback. The delete
    before set stays required as well: a catalog upload unbinds the provider
    slot without reporting it."""
    if not skill_id:
        return {"ok": False, "detail": "No skill id yet."}
    nudged = ""
    try:
        smapi_rest.update_manifest(
            skill_id, smapi_rest.get_manifest(skill_id))
        problem = _manifest_verdict(skill_id, tries=10, delay=1.5)
        if problem:
            return {"ok": False,
                    "detail": f"Re-provisioning failed: {problem}"}
        nudged = "Re-provisioned and enabled"
    except Exception:
        # The plain cycle is still worth doing when the nudge cannot run.
        nudged = "Enabled (re-provisioning was skipped)"
    try:
        try:
            smapi_rest.delete_enablement(skill_id)
        except smapi_rest.SmapiError:
            pass  # Not enabled is the normal state here, not a failure.
        smapi_rest.set_enablement(skill_id)
    except Exception as exc:
        return {"ok": False, "detail": _rest_error(exc)}
    store.update(enabled=True, enabled_at=time.time())
    return {"ok": True, "detail": f"{nudged} for development."}


@bp.post("/skill/enable")
def skill_enable():
    current = store.load()
    result = _cycle_enablement(
        current.get("skill_id") or os.environ.get("SKILL_ID", ""))
    fresh = store.load()
    return fragment("_binding.html", result=result, state=fresh,
                    binding=_binding_now(fresh, force=True),
                    health=_binding_health())


@bp.post("/skill/disable")
def skill_disable():
    current = store.load()
    skill_id = current.get("skill_id") or os.environ.get("SKILL_ID", "")
    if not skill_id:
        result = {"ok": False, "detail": "No skill id yet."}
    else:
        try:
            smapi_rest.delete_enablement(skill_id)
            store.update(enabled=False)
            result = {"ok": True,
                      "detail": ("Disabled. Alexa will not route to this "
                                 "skill until it is enabled again.")}
        except Exception as exc:
            result = {"ok": False, "detail": _rest_error(exc)}
    fresh = store.load()
    return fragment("_binding.html", result=result, state=fresh,
                    binding=_binding_now(fresh, force=True),
                    health=_binding_health())


@bp.post("/skill/cycle")
def skill_cycle():
    """Cycle on demand, for a caller that is not a browser.

    The point of this route is pre-emption. The highest-stakes moment for this
    skill is a scheduled one: an alarm-driven routine asking for music into a
    room where nobody is going to retry. Neither the keep-alive clock nor the
    reactive detector can guarantee a fresh binding at one named instant, and
    both are cheaper to skip than to make precise. So the caller that already
    knows when that instant is gets to say so. Home Assistant calls this over
    the tailnet a couple of minutes before the wake alarm, with X-Admin-Token.

    Answers JSON rather than a fragment because nothing here renders it.
    """
    current = store.load()
    skill_id = current.get("skill_id") or os.environ.get("SKILL_ID", "")
    result = _cycle_enablement(skill_id)
    fresh = store.load()
    body = {
        "ok": result["ok"],
        "detail": result["detail"],
        "enabled_at": fresh.get("enabled_at") or 0,
    }
    return jsonify(body), (200 if result["ok"] else 503)


@bp.get("/skill/health")
def skill_health():
    """The binding measured from real traffic, as JSON.

    Same numbers the status page shows. Exposed separately so a monitor can
    alert on a degraded binding without scraping HTML.
    """
    current = store.load()
    health = _binding_health()
    return jsonify({
        **health,
        "armed": bool(current.get("reactive_armed", True)),
        "misses_since_cycle": int(current.get("reactive_misses") or 0),
        "degraded": (not current.get("reactive_armed", True)
                     and int(current.get("reactive_misses") or 0) > 0),
    })


@bp.get("/wizard")
def wizard():
    """Resume where setup actually is, or show the finished page."""
    state = store.load()
    if wizard_steps.complete(state):
        return render_template("done.html", **wizard_context())
    return redirect(
        f"/setup/wizard/{wizard_steps.STEPS[wizard_steps.current_index(state)].key}")


@bp.post("/wizard/subsonic")
def wizard_subsonic():
    url = (request.form.get("url") or "").strip()
    user = (request.form.get("user") or "").strip()
    password = request.form.get("password") or ""
    result = validate.subsonic_ping(url, user, password)
    if result["ok"]:
        # The password is not written here. It reaches the bridge through the
        # environment like every other secret, and keeping a copy in /data
        # would create a second place for it to leak from.
        store.update(subsonic_url=url, subsonic_user=user)
        return step_completed("wizard/_subsonic.html", result=result,
                              **wizard_context())
    return fragment("wizard/_subsonic.html", back="/setup/wizard/server",
                    result=result, **wizard_context())


@bp.post("/wizard/alias")
def wizard_alias():
    """Save the alias, checking it against the library in the same motion.

    The standalone alias page keeps the exploratory checker for later tuning.
    Inside the wizard a separate check button was one more thing to decide
    about, and it linked to a page the configuration gate refuses until setup
    is done. Saving is the moment the check matters, so saving runs it.
    """
    candidate = (request.form.get("alias") or "").strip()
    result = validate.assess_alias(candidate, subsonic)
    if result["verdict"] == "empty":
        return fragment("wizard/_alias.html", result=result, **wizard_context())
    store.update(alias=candidate)
    if result["verdict"] == "clear":
        return step_completed("wizard/_alias.html", result=result,
                              **wizard_context())
    # Saved, but the operator should see what it collides with before moving
    # on. The fragment carries its own way forward, because a fragment swap
    # cannot re-enable the Continue button outside the step body.
    return fragment("wizard/_alias.html", result=result, **wizard_context())


@bp.get("/wizard/<step_key>")
def wizard_step(step_key: str):
    if step_key not in wizard_steps.BY_KEY:
        return redirect("/setup/wizard")
    state = store.load()
    rows = wizard_steps.progress(state)
    row = next(r for r in rows if r["key"] == step_key)
    # Reachable means every earlier step is done. Going back is always allowed,
    # since reviewing a finished step is not the same as redoing it.
    if not (row["reachable"] or row["done"]):
        return redirect("/setup/wizard")
    return render_template("wizard.html", **step_context(step_key))


# The verifier and state for an in-flight consent round trip. In memory on
# purpose: it is valid for one redirect and writing it down would leave the
# thing that binds the authorization code to this process sitting in /data.
_PENDING: dict = {}


@bp.post("/wizard/amazon/begin")
def wizard_amazon_begin():
    client_id = (request.form.get("client_id") or "").strip()
    client_secret = (request.form.get("client_secret") or "").strip()
    if not (client_id and client_secret):
        return fragment("wizard/_amazon.html", result={
            "ok": False,
            "detail": "Both the client ID and the client secret are needed.",
        }, **wizard_context())
    if not smapi_rest.redirect_uri().startswith("https://"):
        return fragment("wizard/_amazon.html", result={
            "ok": False,
            "detail": "PUBLIC_BASE must be an https origin before connecting, "
                      "because Amazon will only redirect back to https.",
        }, **wizard_context())

    url, state, verifier = smapi_rest.begin(client_id)
    _PENDING.clear()
    # The origin this wizard is being driven from. Amazon must redirect to the
    # public https hostname, but the admin plane does not serve there, so the
    # callback page needs to send the operator back to the address they were
    # actually using.
    _PENDING.update({"state": state, "verifier": verifier, "at": time.time(),
                     "client_id": client_id, "client_secret": client_secret,
                     "origin": request.host_url.rstrip("/")})
    return redirect(url)


@bp.get("/oauth/callback")
def oauth_callback():
    """Where Amazon sends the operator back.

    Reachable from any address, because the operator's browser is on the public
    internet and cannot be judged by the LAN rule the rest of setup uses. What
    protects it instead is the state value and the PKCE verifier, both of which
    only this process holds and both of which are good for exactly one attempt.
    """
    if error := request.args.get("error"):
        return render_template("oauth_done.html", ok=False,
                               back=_PENDING.get("origin"), detail=(
            f"{error}: {request.args.get('error_description', '')}"))

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    pending = dict(_PENDING)
    _PENDING.clear()
    back = pending.get("origin")

    if not pending or not state or not hmac.compare_digest(state, pending.get("state", "")):
        return render_template("oauth_done.html", ok=False, back=back, detail=(
            "That response did not match a consent request from this bridge. "
            "Start again from the wizard."))
    if time.time() - pending.get("at", 0) > 900:
        return render_template("oauth_done.html", ok=False, back=back, detail=(
            "The consent request expired. Start again from the wizard."))

    try:
        smapi_rest.complete(code, pending["client_id"], pending["client_secret"],
                            pending["verifier"])
    except smapi_rest.SmapiError as exc:
        return render_template("oauth_done.html", ok=False, back=back,
                               detail=f"{exc} {exc.body}".strip())
    return render_template("oauth_done.html", ok=True, back=back, detail="")


@bp.post("/wizard/amazon/disconnect")
def wizard_amazon_disconnect():
    smapi_rest.forget_credentials()
    return fragment("wizard/_amazon.html", result={
        "ok": True, "detail": "Disconnected. The refresh token was deleted.",
    }, **wizard_context())


def _rest_error(exc: Exception) -> str:
    if isinstance(exc, smapi_rest.SmapiError):
        return f"{exc} {exc.body}".strip()
    return str(exc)[:400]


@bp.post("/wizard/skill")
def wizard_skill():
    current = store.load()
    if not current.get("endpoint_ok"):
        return fragment("wizard/_skill.html", blocked=True, result=None,
                        **wizard_context())
    if not smapi_rest.connected():
        return fragment("wizard/_skill.html", blocked=False, result={
            "ok": False, "detail": "Connect to Amazon first."},
            **wizard_context())

    if current.get("skill_id"):
        # Guard against a resubmitted form or an old tab: the button must not
        # be able to mint duplicates on the developer account.
        return step_completed("wizard/_skill.html", blocked=False, result={
            "ok": True,
            "detail": f"The skill already exists: {current['skill_id']}. "
                      "Nothing was created."}, **wizard_context())

    alias_word = (request.form.get("alias") or current.get("alias") or "Ampere").strip()
    vendor = (request.form.get("vendor_id") or "").strip()
    manifest = smapi.manifest(
        name=alias_word.title(),
        public_base=public_base(),
        cert_type=current.get("cert_type") or "Trusted",
        aliases=[alias_word.lower()],
    )
    try:
        skill_id = smapi_rest.create_skill(manifest, vendor)
    except Exception as exc:
        return fragment("wizard/_skill.html", blocked=False, result={
            "ok": False, "detail": _rest_error(exc)}, **wizard_context())

    if problem := _manifest_verdict(skill_id):
        # A skill that failed validation exists in name only: it lists, it
        # 404s for catalog association, and nothing ever calls its endpoint.
        # Better deleted now, with Amazon's own words shown, than discovered
        # two steps later.
        try:
            smapi_rest.delete_skill(skill_id)
        except Exception:
            pass
        return fragment("wizard/_skill.html", blocked=False, result={
            "ok": False, "detail": f"Amazon rejected the manifest: {problem}"},
            existing_skills=_existing_music_skills(), **wizard_context())

    store.update(skill_id=skill_id, alias=alias_word, vendor_id=vendor)
    wizard_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    # A recreated skill starts with no catalog associations even though the
    # catalogs themselves survived on the vendor. Re-binding here keeps the
    # catalogs step honest about already being done.
    for catalog_id in (current.get("catalogs") or {}).values():
        try:
            smapi_rest.associate_catalog(skill_id, catalog_id)
        except Exception:
            pass
    return step_completed("wizard/_skill.html", blocked=False, result={
        "ok": True, "detail": f"Created {skill_id}"}, **wizard_context())


@bp.post("/wizard/skill/forget")
def wizard_skill_forget():
    """Discard the record of a skill Amazon no longer has.

    The record, not the skill, is what gets removed: there is nothing left on
    Amazon's side to delete. Enablement is cleared with it, because it
    described the vanished skill.
    """
    store.update(skill_id="", enabled=False)
    wizard_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    return step_completed("wizard/_skill.html", blocked=False, result={
        "ok": True, "detail": "Stale record discarded."},
        existing_skills=_existing_music_skills(), **wizard_context())


@bp.post("/wizard/skill/remove")
def wizard_skill_remove():
    """Delete a leftover music skill that would compete for the alias."""
    skill_id = (request.form.get("skill_id") or "").strip()
    if not skill_id or request.form.get("confirm") != "yes":
        return fragment("wizard/_skill.html", blocked=False, result={
            "ok": False,
            "detail": "Deletion needs the confirmation box ticked."},
            existing_skills=_existing_music_skills(), **wizard_context())
    try:
        smapi_rest.delete_skill(skill_id)
    except Exception as exc:
        return fragment("wizard/_skill.html", blocked=False, result={
            "ok": False, "detail": _rest_error(exc)},
            existing_skills=_existing_music_skills(), **wizard_context())
    return fragment("wizard/_skill.html", blocked=False, result={
        "ok": True, "detail": f"Deleted {skill_id}."},
        existing_skills=_existing_music_skills(), **wizard_context())


@bp.post("/wizard/catalogs")
def wizard_catalogs():
    current = store.load()
    skill_id = current.get("skill_id") or os.environ.get("SKILL_ID", "")
    if not skill_id:
        return fragment("wizard/_catalogs.html", results=None,
                        **wizard_context(message="Create the skill first."))

    catalogs = dict(current.get("catalogs") or {})
    # Orphans from an earlier run: a catalog created moments before its
    # association failed was never recorded anywhere. Reusing by title means
    # a re-run heals instead of minting duplicates on the vendor.
    try:
        existing = {c.get("title", ""): c.get("id", "")
                    for c in smapi_rest.list_catalogs()}
    except Exception:
        existing = {}
    results = []
    for kind, catalog_type in CATALOG_KINDS.items():
        title = f"Ampere {kind}"
        try:
            catalog_id = catalogs.get(kind) or existing.get(title, "")
            if not catalog_id:
                catalog_id = smapi_rest.create_catalog(title, catalog_type)
            # Recorded before association, so a failure there cannot orphan it.
            catalogs[kind] = catalog_id
            store.update(catalogs=catalogs)
            # Association has to happen before any upload or the content has
            # nowhere to resolve against. The PUT is idempotent, so running it
            # again for an already-associated catalog is a no-op, not an error.
            smapi_rest.associate_catalog(skill_id, catalog_id)
        except Exception as exc:
            results.append({"kind": kind, "ok": False, "detail": _rest_error(exc)})
            continue
        results.append({"kind": kind, "ok": True, "detail": catalog_id})
    store.update(catalogs=catalogs)
    if results and all(row["ok"] for row in results):
        return step_completed("wizard/_catalogs.html", results=results,
                              **wizard_context())
    return fragment("wizard/_catalogs.html", results=results, **wizard_context())


@bp.post("/wizard/upload")
def wizard_upload():
    """Start the build-and-upload job and answer immediately.

    Reading a whole library and pushing five catalogs takes minutes on a real
    collection. Inside one request that was a button that appeared to do
    nothing; as a job, the page can ask after its phase every two seconds.
    """
    current = store.load()
    if not any(catalog_ids(current).values()):
        return fragment("wizard/_upload.html", upload_job=dict(_UPLOAD),
                        **wizard_context(message="Create the catalogs first."))
    with _UPLOAD_LOCK:
        if not _UPLOAD["running"]:
            _UPLOAD.update(running=True, phase="starting", percent=None,
                           results=None, message="", done_at=0.0, cancel=False,
                           auto=False)
            threading.Thread(target=_run_upload, daemon=True).start()
    return fragment("wizard/_upload_progress.html", job=dict(_UPLOAD),
                    back="/setup/wizard/upload")


class _UploadStopped(Exception):
    """The page that started the upload was left before it finished."""


def _run_upload(auto: bool = False) -> None:
    # The crawl owns 0-85% of the bar (it dominates wall time); the five
    # catalog uploads share the last 15%.
    def _tell(text, fraction=None):
        if _UPLOAD["cancel"]:
            raise _UploadStopped()
        _UPLOAD["phase"] = text
        _UPLOAD["percent"] = None if fraction is None else round(fraction * 85)

    uploaded_any = False
    try:
        current = store.load()
        ids = catalog_ids(current)
        _tell("reading the library", 0.0)
        collected = catalog_sync.collect(progress=_tell)
        saved = store.load().get("catalog_hashes") or {}
        results, uploads = [], dict(current.get("uploads") or {})
        for index, (kind, entities) in enumerate(collected.items()):
            if _UPLOAD["cancel"]:
                raise _UploadStopped()
            catalog_id = ids.get(kind)
            if not catalog_id or not entities:
                continue
            final, hashes = catalog_sync.apply_timestamps(kind, entities, saved)
            if kind in uploads and saved.get(kind) == hashes:
                # Nothing changed since the last accepted upload. Skipping is
                # what makes a schedule safe: only real deltas spend one of
                # the rate-limited upload slots.
                results.append({"kind": kind, "ok": True,
                                "detail": "unchanged, skipped"})
                continue
            _UPLOAD["phase"] = f"uploading {kind} ({len(final)} entities)"
            _UPLOAD["percent"] = 85 + round(15 * index / max(1, len(collected)))
            payload = json.dumps({
                "type": catalog_sync.TYPES[kind], "version": 2.0,
                "locales": catalog_sync.LOCALES, "entities": final,
            }).encode()
            try:
                upload_id = smapi_rest.upload_catalog(catalog_id, payload)
            except Exception as exc:
                results.append({"kind": kind, "ok": False,
                                "detail": _rest_error(exc)})
                continue
            saved[kind] = hashes
            uploads[kind] = upload_id
            uploaded_any = True
            results.append({"kind": kind, "ok": True,
                            "detail": f"{len(final)} entities, upload {upload_id}"})
        store.update(catalog_hashes=saved, uploads=uploads)
        if auto and uploaded_any:
            # An upload unbinds the provider slot without reporting it. A
            # human is told to cycle afterwards; a schedule has to do it
            # itself or it would silently break voice playback while every
            # diagnostic stays green.
            _UPLOAD["phase"] = "cycling enablement"
            outcome = _cycle_enablement(
                current.get("skill_id") or os.environ.get("SKILL_ID", ""))
            results.append({"kind": "enablement", "ok": outcome["ok"],
                            "detail": outcome["detail"]})
        _UPLOAD["results"] = results
    except _UploadStopped:
        _UPLOAD["message"] = ("Stopped: the page was left before the upload "
                              "finished. Nothing was recorded; run it again "
                              "when you are ready.")
    except Exception as exc:
        # Logged as well as shown, because the dict is the only other record
        # and it is read by one page. A run started from a browser tab that is
        # then closed, or from curl, failed here in complete silence: the whole
        # library crawl ran, hit the container's memory ceiling, and left
        # nothing behind but a log that stopped mid-crawl.
        logger.exception("catalog upload failed")
        _UPLOAD["message"] = f"Could not read the library: {exc}"
    finally:
        _UPLOAD["running"] = False
        _UPLOAD["done_at"] = time.time()


_UPLOAD = {"running": False, "phase": "", "percent": None, "results": None,
           "message": "", "done_at": 0.0, "cancel": False, "auto": False}
_UPLOAD_LOCK = threading.Lock()

# The floor under the schedule interval. Amazon rate-limits music catalog
# uploads per catalog per day; four runs a day stays under that ceiling, so
# with this floor the limit is unreachable by construction. The diff check
# in _run_upload means most scheduled runs upload nothing anyway.
AUTO_SYNC_MIN_HOURS = 6


def _auto_sync_due(state: dict, now: float) -> bool:
    hours = int(state.get("auto_sync_hours") or 0)
    if hours <= 0 or not state.get("catalogs"):
        return False
    hours = max(AUTO_SYNC_MIN_HOURS, hours)
    return now - (state.get("last_auto_sync") or 0) >= hours * 3600


# How stale the enablement may get before the keep-alive re-cycles it. Found
# empirically 2026-08-02: the provider-slot binding decays within hours of a
# re-provision cycle (worked 16 minutes after one, dead by 7 hours), leaving
# searches resolving and playback silently never starting.
#
# Configurable because four is an extrapolation from two observations, and the
# real survival time is a property of Amazon's provisioning rather than of this
# code, so it may well differ per account. The miss detector below measures it:
# lower this until the detector stops firing. Cost is only more enablement
# cycles, which draw on a different rate pool from catalog uploads and are
# nowhere near it. Zero disables the keep-alive and leaves only the detector.
BINDING_KEEPALIVE_HOURS = float(os.environ.get("BINDING_KEEPALIVE_HOURS", "4"))


def _binding_stale(state: dict, now: float) -> bool:
    if BINDING_KEEPALIVE_HOURS <= 0:
        return False
    if not (state.get("skill_id") and state.get("enabled")):
        return False
    return now - (state.get("enabled_at") or 0) >= BINDING_KEEPALIVE_HOURS * 3600


def _recent_traffic(max_age: float = 1200.0) -> bool:
    """Has the music endpoint seen a request in the last twenty minutes.

    The keep-alive must never yank the enablement out from under an active
    playback session; the newest capture is the cheapest honest signal that one
    exists. Twenty minutes, not ten: a session only produces a directive per
    track boundary, and a long ambient track or a short pause must not read as
    idle. Staleness tolerance is hours, so the wider window costs nothing."""
    newest = captures.newest_time()
    return newest is not None and time.time() - newest < max_age


# The directives that prove a binding. A search arriving means Alexa routed to
# this skill; an Initiate arriving means the provider slot actually resolved to
# it. The gap between those two is where the decay lives.
SEARCH_DIRECTIVE = "Alexa.Media.Search.GetPlayableContent"
INITIATE_DIRECTIVE = "Alexa.Media.Playback.Initiate"

# GetNextItem only arrives at a track boundary of a session this skill is
# already serving, so it is proof of a live binding just as strong as an
# Initiate. It was excluded at first for being the bulk of the traffic, which
# confused "says nothing about which search reached playback" with "says
# nothing about the binding". It answers the second question dispositively.
PLAYING_DIRECTIVE = "Alexa.Audio.PlayQueue.GetNextItem"
PROOF_DIRECTIVES = (INITIATE_DIRECTIVE, PLAYING_DIRECTIVE)

# How long a search may go unanswered before it counts as a miss. Real pairs
# land about two seconds apart; a minute is far past any plausible slow path
# and keeps a search that is merely in flight from being called a failure.
BINDING_GRACE_SECONDS = 60.0

# How many searches in a row must miss before a cycle is worth attempting.
#
# One is not enough, measured 2026-08-02. A voice transfer between speakers
# emits a TRACK-level search to re-describe the playing item and never emits an
# Initiate, because Amazon re-forms the speaker cluster in its own audio layer
# and simply re-pulls the existing stream URL with a Range request. A single
# superseded or cancelled utterance looks the same. Both are normal, and the
# detector cycled the skill on both, which is worse than doing nothing: the
# cycle lands mid-session and is itself the thing that breaks playback.
#
# The real outage produced three misses in a row with no playback of any kind
# between them, so two is enough to separate the shapes.
REACTIVE_MISS_THRESHOLD = 2


def _binding_events(limit: int = 400) -> list[tuple[float, str]]:
    """Recent searches and proof-of-playback directives, oldest first.

    Read from the capture filenames alone. The name carries a sortable UTC
    stamp and the directive, so lexical order is chronological order and the
    whole history is available for one scandir and no stat calls at all. This
    is the same technique app.prune_captures uses, for the same reason.
    """
    wanted = (SEARCH_DIRECTIVE, *PROOF_DIRECTIVES)
    try:
        names = sorted(
            e.name for e in os.scandir(captures.log_dir())
            if e.name.endswith(".json")
            and any(directive in e.name for directive in wanted)
        )
    except OSError:
        return []
    rows = []
    for name in names[-limit:]:
        when = captures.capture_time(name)
        if when is None:
            continue  # a name we did not write proves nothing about binding
        kind = next(d for d in wanted if d in name)
        rows.append((when, kind))
    return rows


def _binding_health(limit: int = 20, now: float | None = None) -> dict:
    """Did recent searches actually reach playback.

    This is the only honest measure of the binding there is. Amazon's own
    enablement status reports True throughout the failure, the search resolves
    against the catalog and is answered 200, and Initiate simply never comes.
    So the question is asked of our own traffic instead: for each search, did
    an Initiate follow it before the next search did.
    """
    stamp = time.time() if now is None else now
    events = _binding_events()
    pairs = []
    for index, (when, kind) in enumerate(events):
        if kind != SEARCH_DIRECTIVE:
            continue
        following = events[index + 1] if index + 1 < len(events) else None
        if following is None and stamp - when < BINDING_GRACE_SECONDS:
            continue  # still in flight, not yet an answer either way
        pairs.append({"at": when,
                      "reached": bool(following
                                      and following[1] in PROOF_DIRECTIVES)})
    recent = pairs[-limit:]
    played = [w for w, kind in events if kind in PROOF_DIRECTIVES]
    newest = recent[-1] if recent else None

    # Trailing run only. An older miss that has since been followed by a search
    # that reached playback is history, not an outage.
    consecutive = 0
    for pair in reversed(recent):
        if pair["reached"]:
            break
        consecutive += 1

    return {
        "searches": len(recent),
        "reached": sum(1 for p in recent if p["reached"]),
        "miss": bool(newest and not newest["reached"]),
        "miss_at": newest["at"] if newest and not newest["reached"] else 0.0,
        "consecutive_misses": consecutive,
        "last_initiate": played[-1] if played else 0.0,
    }


def _reactive_check(state: dict, now: float, log) -> None:
    """Cycle once when a search fails to reach playback, then wait for proof.

    The breaker exists because a cycle is not guaranteed to be the fix. If the
    cause is something else, an unbounded reactor would cycle on every failed
    request and churn the enablement against Amazon's rate limits for nothing.
    So one attempt per outage, and re-arming requires evidence from a device,
    never Amazon's own acknowledgement of the cycle: a 200 there proves
    nothing, which is the trap this entire failure mode lives in.
    """
    health = _binding_health(now=now)
    armed = bool(state.get("reactive_armed", True))
    cycled_at = float(state.get("reactive_cycled_at") or 0.0)

    if not armed and health["last_initiate"] > cycled_at:
        store.update(reactive_armed=True, reactive_misses=0)
        log.info("binding detector: playback recovered, re-armed")
        return

    if not health["miss"]:
        return

    # Counted once per search, not once per check. The miss is identified by
    # the instant it happened, so a tick that re-reads the same unanswered
    # search adds nothing. Without this the count was really a count of
    # elapsed minutes: ten reported misses from one real one, measured
    # 2026-08-02 over a window with no searches in it at all.
    if health["miss_at"] <= float(state.get("reactive_last_miss") or 0.0):
        return

    if not armed:
        misses = int(state.get("reactive_misses") or 0) + 1
        store.update(reactive_misses=misses, reactive_last_miss=health["miss_at"])
        log.warning("binding detector: still degraded, %d search(es) have "
                    "missed since the cycle at %s; a re-cycle is not the fix "
                    "for this one", misses, iso_utc(cycled_at))
        return

    if health["consecutive_misses"] < REACTIVE_MISS_THRESHOLD:
        store.update(reactive_last_miss=health["miss_at"])
        log.info("binding detector: a search missed playback, waiting for a "
                 "second before calling it an outage (a voice transfer looks "
                 "exactly like this and is not one)")
        return

    if not (state.get("skill_id") or os.environ.get("SKILL_ID", "")):
        return
    if not smapi_rest.connected():
        return

    log.warning("binding detector: %d searches in a row never reached "
                "playback, cycling once", health["consecutive_misses"])
    outcome = _cycle_enablement(
        state.get("skill_id") or os.environ.get("SKILL_ID", ""))
    store.update(reactive_armed=False, reactive_cycled_at=time.time(),
                 reactive_misses=0, reactive_last_miss=health["miss_at"])
    log.info("binding detector: %s", outcome["detail"])


def iso_utc(when: float) -> str:
    if not when:
        return "never"
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _auto_sync_loop() -> None:
    log = logger
    while True:
        time.sleep(60)
        try:
            state = store.load()
            now = time.time()
            if _auto_sync_due(state, now) and smapi_rest.connected():
                with _UPLOAD_LOCK:
                    if _UPLOAD["running"]:
                        continue  # a manual run owns the job; try next minute
                    _UPLOAD.update(running=True, phase="starting (scheduled)",
                                   percent=None, results=None, message="",
                                   done_at=0.0, cancel=False, auto=True)
                store.update(last_auto_sync=now)
                _run_upload(auto=True)
                continue
            if (_binding_stale(state, now) and not _recent_traffic()
                    and smapi_rest.connected()):
                log.info("binding keep-alive: last cycle is stale, re-cycling")
                outcome = _cycle_enablement(
                    state.get("skill_id") or os.environ.get("SKILL_ID", ""))
                log.info("binding keep-alive: %s", outcome["detail"])
                continue

            # The reactive half. The keep-alive above prevents the decay it
            # knows about, on a clock derived from one observation; this
            # catches whatever that clock is too slow for. It deliberately
            # runs on a miss rather than on quiet, so it is the one path here
            # that may cycle during a session: the session is already broken.
            _reactive_check(state, now, log)
        except Exception:
            log.exception("auto sync failed")


_AUTO_SYNC_STARTED = threading.Event()


def start_auto_sync() -> None:
    if _AUTO_SYNC_STARTED.is_set():
        return
    _AUTO_SYNC_STARTED.set()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()


@bp.post("/wizard/autosync")
def wizard_autosync():
    enabled = bool(request.form.get("enabled"))
    try:
        hours = int(request.form.get("hours") or 0)
    except ValueError:
        hours = 0
    hours = max(AUTO_SYNC_MIN_HOURS, hours) if enabled else 0
    store.update(auto_sync_hours=hours)
    return fragment("wizard/_upload.html", upload_job=dict(_UPLOAD),
                    **wizard_context())


@bp.post("/wizard/upload/stop")
def wizard_upload_stop():
    """The page's parting beacon. Leaving the upload page abandons the job:
    the running thread sees the flag at its next progress tick and stops
    without recording anything. Scheduled runs are not the page's to stop."""
    with _UPLOAD_LOCK:
        if _UPLOAD["running"] and not _UPLOAD.get("auto"):
            _UPLOAD["cancel"] = True
    return "", 204


@bp.get("/wizard/upload/progress")
def wizard_upload_progress():
    job = dict(_UPLOAD)
    if not job["running"] and request.headers.get("HX-Request"):
        # Finished since the last poll: reload the page so the rail, the
        # Continue button and the ingestion panel all re-derive at once. The
        # guard has to come off before the reload, or the leave-warning
        # dialog would fire on the moment of success.
        return (
            '<script>if (window.ampGuard) {'
            ' removeEventListener("beforeunload", window.ampGuard);'
            ' removeEventListener("pagehide", window.ampStop);'
            ' document.body.removeEventListener("htmx:afterSwap", window.ampSync);'
            ' window.ampGuard = null; window.ampStop = null; window.ampSync = null; }'
            ' location.reload();</script>', 200)
    return fragment("wizard/_upload_progress.html", job=job,
                    back="/setup/wizard/upload")


@bp.get("/wizard/ingestion")
def wizard_ingestion():
    current = store.load()
    uploads = current.get("uploads") or {}
    rows = []
    for kind, catalog_id in catalog_ids(current).items():
        upload_id = uploads.get(kind)
        if not (catalog_id and upload_id):
            continue
        er = slu = top = ""
        try:
            status = smapi_rest.upload_status(catalog_id, upload_id)
            state, detail = smapi_rest.ingestion_verdict(status)
            steps = {}
            for step in (status.get("ingestionSteps") or []):
                name = step.get("name") or step.get("stepName") or ""
                steps[name] = step.get("status", "")
            er, slu = steps.get("ER_INGESTION", ""), steps.get("SLU_MODELING", "")
            top = status.get("status", "")
        except Exception as exc:
            state, detail = "failed", _rest_error(exc)
        rows.append({"kind": kind, "id": catalog_id, "state": state,
                     "detail": detail, "er": er, "slu": slu, "top": top})
    all_ready = bool(rows) and all(r["state"] == "ready" for r in rows)
    if all_ready:
        # The panel just observed success; the step gate must agree NOW, not
        # after its cache expires, or Continue stays locked until a manual
        # page reload.
        wizard_steps.mark_ingestion_ok(current)
    if all_ready and request.headers.get("HX-Request"):
        # Swap the rail and the Continue button along with the panel, so the
        # unlock lands without a page reload.
        ctx = step_context("upload")
        return (render_template("wizard/_ingestion.html", rows=rows,
                                **wizard_context())
                + render_template("wizard/_rail.html", oob=True, **ctx)
                + render_template("wizard/_stepnav.html", oob=True, **ctx))
    return fragment("wizard/_ingestion.html", rows=rows, **wizard_context())


@bp.post("/wizard/enable")
def wizard_enable():
    current = store.load()
    result = _cycle_enablement(
        current.get("skill_id") or os.environ.get("SKILL_ID", ""))
    _BINDING.update(at=0.0, value=None)
    if not result["ok"]:
        return fragment("wizard/_enable.html", result=result,
                        **wizard_context())
    return step_completed("wizard/_enable.html", result=result,
                          **wizard_context())


@bp.get("/wizard/teardown")
def wizard_teardown():
    return fragment("wizard/_teardown.html", results=None, **wizard_context())


@bp.post("/wizard/teardown")
def wizard_teardown_run():
    """Delete the skill and its catalogs, for a genuine clean slate.

    Guarded by typing the skill id back, because this is not recoverable: the
    catalogs go with it and the replacement has to be re-uploaded and
    re-ingested from nothing.
    """
    current = store.load()
    skill_id = current.get("skill_id") or os.environ.get("SKILL_ID", "")
    if (request.form.get("confirm") or "").strip() != skill_id or not skill_id:
        return fragment("wizard/_teardown.html", results=[{
            "what": "nothing", "ok": False,
            "detail": "Type the skill id exactly to confirm.",
        }], **wizard_context())

    results = []
    for kind, catalog_id in catalog_ids(current).items():
        if not catalog_id:
            continue
        try:
            smapi_rest.delete_catalog(catalog_id)
            results.append({"what": f"catalog {kind}", "ok": True, "detail": catalog_id})
        except Exception as exc:
            results.append({"what": f"catalog {kind}", "ok": False,
                            "detail": _rest_error(exc)})
    try:
        smapi_rest.delete_skill(skill_id)
        results.append({"what": "skill", "ok": True, "detail": skill_id})
    except Exception as exc:
        results.append({"what": "skill", "ok": False, "detail": _rest_error(exc)})

    store.update(skill_id="", catalogs={}, uploads={}, catalog_hashes={},
                 enabled=False)
    return step_completed("wizard/_teardown.html", results=results,
                          **wizard_context())
