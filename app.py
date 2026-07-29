"""Music Assistant Alexa Music Skill bridge.

Speaks Amazon's Music/Radio/Podcast Skill API on one side and Navidrome's
Subsonic API on the other. Navidrome is tailnet-only, so every asset Amazon
fetches (audio, cover art) is proxied through this service behind a signed,
expiring token.

Design note: Alexa echoes {id, queueId, contentId} back on every queue request,
so playback position is carried by Alexa rather than stored here. Queues are
derived from the contentId, which keeps the hot path within Amazon's 100ms p50
budget for Initiate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pathlib
import time
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

import subsonic

LOG_DIR = pathlib.Path(os.environ.get("CAPTURE_DIR", "/data/captures"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
ICON_DIR = pathlib.Path(os.environ.get("ICON_DIR", "/data/icons"))

SIGNING_KEY = os.environ.get("SIGNING_KEY", "").encode() or os.urandom(32)
PUBLIC_BASE = os.environ.get(
    "PUBLIC_BASE", "https://alexa-music.graysons.network"
).rstrip("/")

# How long a stream URL stays valid. Amazon defaults to ~60s when validUntil is
# omitted, which is far too short; we set it explicitly and generously.
STREAM_TTL = int(os.environ.get("STREAM_TTL", str(12 * 3600)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ma-music-skill")

app = Flask(__name__)

# contentId -> [song_id, ...]. Best-effort only; a miss re-derives from Subsonic.
_QUEUE_CACHE: OrderedDict[str, list[str]] = OrderedDict()
_QUEUE_CACHE_MAX = 256


# --------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------


def sign(kind: str, ident: str, expires: int) -> str:
    msg = f"{kind}:{ident}:{expires}".encode()
    return hmac.new(SIGNING_KEY, msg, hashlib.sha256).hexdigest()[:32]


def signed_url(kind: str, ident: str, ttl: int = STREAM_TTL) -> tuple[str, int]:
    expires = int(time.time()) + ttl
    sig = sign(kind, ident, expires)
    return f"{PUBLIC_BASE}/{kind}/{ident}/{expires}/{sig}", expires


def verify(kind: str, ident: str, expires: int, sig: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(sign(kind, ident, expires), sig)


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# content resolution
# --------------------------------------------------------------------------


def resolve_tracks(content_id: str) -> list[str]:
    """contentId -> ordered list of Navidrome song ids."""
    if content_id in _QUEUE_CACHE:
        _QUEUE_CACHE.move_to_end(content_id)
        return _QUEUE_CACHE[content_id]

    kind, _, ident = content_id.partition(":")
    try:
        if kind == "tr":
            ids = [ident]
        elif kind == "al":
            ids = [s["id"] for s in subsonic.album_tracks(ident)]
        elif kind == "ar":
            # getTopSongs takes an artist NAME, so resolve the id first and
            # fall back to walking the discography if there are no top songs.
            info = subsonic.call("getArtist.view", id=ident).get("artist", {})
            ids = []
            if name := info.get("name"):
                ids = [s["id"] for s in subsonic.artist_top_songs(name)]
            if not ids:
                ids = [
                    s["id"]
                    for alb in info.get("album", [])
                    for s in subsonic.album_tracks(alb["id"])
                ]
        elif kind == "gen":
            ids = [s["id"] for s in subsonic.random_songs(50, genre=ident)]
        else:
            ids = [s["id"] for s in subsonic.random_songs(50)]
    except Exception:
        logger.exception("resolve_tracks failed for %s", content_id)
        ids = []

    _QUEUE_CACHE[content_id] = ids
    while len(_QUEUE_CACHE) > _QUEUE_CACHE_MAX:
        _QUEUE_CACHE.popitem(last=False)
    return ids


def name_prop(text: str) -> dict:
    return {
        "speech": {"type": "PLAIN_TEXT", "text": (text or "").lower()},
        "display": text or "",
    }


def art_block(cover_id: str | None) -> dict:
    if not cover_id:
        return {"sources": []}
    url, _ = signed_url("art", cover_id)
    return {
        "sources": [
            {"url": url, "size": "X_LARGE", "widthPixels": 600, "heightPixels": 600}
        ]
    }


def build_item(song: dict, index: int, total: int) -> dict:
    """Build an Alexa Item from a Subsonic song."""
    uri, expires = signed_url("stream", song["id"])
    item = {
        "id": f"{index}",
        "playbackInfo": {"type": "DEFAULT"},
        "metadata": {
            "type": "TRACK",
            "name": name_prop(song.get("title", "Unknown")),
            "art": art_block(song.get("coverArt")),
            "authors": [{"name": name_prop(song.get("artist", ""))}],
        },
        "controls": [
            {"type": "COMMAND", "name": "NEXT", "enabled": index < total - 1},
            {"type": "COMMAND", "name": "PREVIOUS", "enabled": index > 0},
        ],
        "rules": {"feedbackEnabled": False},
        "stream": {
            "id": f"s-{song['id']}",
            "uri": uri,
            "offsetInMilliseconds": 0,
            "validUntil": iso(expires),
        },
    }
    if song.get("duration"):
        item["durationInMilliseconds"] = int(song["duration"]) * 1000
    if song.get("album"):
        item["metadata"]["album"] = {"name": name_prop(song["album"])}
    return item


def item_at(content_id: str, index: int) -> dict | None:
    ids = resolve_tracks(content_id)
    if index < 0 or index >= len(ids):
        return None
    try:
        song = subsonic.song(ids[index])
    except Exception:
        logger.exception("getSong failed for %s", ids[index])
        return None
    return build_item(song, index, len(ids))


# --------------------------------------------------------------------------
# envelope helpers
# --------------------------------------------------------------------------


def envelope(namespace: str, name: str, payload: dict) -> Response:
    return jsonify(
        {
            "header": {
                "messageId": str(uuid.uuid4()),
                "namespace": namespace,
                "name": name,
                "payloadVersion": "1.0",
            },
            "payload": payload,
        }
    )


def error(namespace: str, err_type: str, message: str, version: str = "1.0") -> Response:
    logger.warning("error %s/%s: %s", namespace, err_type, message)
    return jsonify(
        {
            "header": {
                "messageId": str(uuid.uuid4()),
                "namespace": namespace,
                "name": "ErrorResponse",
                "payloadVersion": version,
            },
            "payload": {"type": err_type, "message": message},
        }
    )


def capture(payload: object, kind: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    (LOG_DIR / f"{stamp}-{kind}.json").write_text(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------
# directive handlers
# --------------------------------------------------------------------------


def handle_get_playable_content(payload: dict) -> Response:
    attrs = (payload.get("selectionCriteria") or {}).get("attributes") or []
    usable = [a for a in attrs if a.get("value") and a.get("type") != "MEDIA_TYPE"]
    query = " ".join(a["value"] for a in usable).strip()

    # Alexa tells us what kind of thing the user asked for. Honour it: asking
    # for an artist should queue that artist, not one track whose title happens
    # to match better.
    wanted = next((a.get("type") for a in usable if a.get("type")), None)
    media_type = next(
        (a.get("value") for a in attrs if a.get("type") == "MEDIA_TYPE"), None
    )
    preference = {
        "ARTIST": ["artist", "album", "song"],
        "ALBUM": ["album", "artist", "song"],
        "TRACK": ["song", "album", "artist"],
        "GENRE": ["genre"],
    }.get(wanted or media_type or "", ["song", "album", "artist"])

    if not query:
        # "play <provider name>" with no criteria: play something.
        content_id, display = "rnd:all", "Your Library"
    elif preference == ["genre"]:
        content_id, display = f"gen:{query}", query
    else:
        try:
            res = subsonic.search(query)
        except Exception:
            logger.exception("search failed")
            return error("Alexa", "INTERNAL_ERROR", "search failed", "3.0")

        picked = None
        for kind in preference:
            hits = res.get(kind) or []
            if hits:
                picked = (kind, hits[0])
                break

        if picked is None:
            return error("Alexa.Media", "CONTENT_NOT_FOUND", f"no match for {query}")

        kind, hit = picked
        prefix = {"song": "tr", "album": "al", "artist": "ar"}[kind]
        content_id = f"{prefix}:{hit['id']}"
        display = hit.get("title") or hit.get("name") or query

    return envelope(
        "Alexa.Media.Search",
        "GetPlayableContent.Response",
        {
            "content": {
                "id": content_id,
                "metadata": {"type": "TRACK", "name": name_prop(display), "art": {"sources": []}},
            }
        },
    )


def handle_initiate(payload: dict) -> Response:
    content_id = payload.get("contentId") or "rnd:all"
    first = item_at(content_id, 0)
    if first is None:
        return error("Alexa.Media", "CONTENT_NOT_FOUND", f"nothing playable for {content_id}")

    return envelope(
        "Alexa.Media.Playback",
        "Initiate.Response",
        {
            "playbackMethod": {
                "type": "ALEXA_AUDIO_PLAYER_QUEUE",
                "id": str(uuid.uuid4()),
                "controls": [],
                "rules": {"feedback": {"type": "PREFERENCE", "enabled": False}},
                "firstItem": first,
            }
        },
    )


def _current_index_and_content(payload: dict) -> tuple[str, int]:
    ref = payload.get("currentItemReference") or {}
    content = ref.get("contentId")
    if not content:
        content = (ref.get("content") or {}).get("id", "")
    try:
        index = int(ref.get("id", "0"))
    except (TypeError, ValueError):
        index = 0
    return content, index


def handle_get_next_item(payload: dict) -> Response:
    content_id, index = _current_index_and_content(payload)
    nxt = item_at(content_id, index + 1)
    if nxt is None:
        return envelope(
            "Alexa.Audio.PlayQueue",
            "GetNextItem.Response",
            {"isQueueFinished": True, "item": None},
        )
    return envelope(
        "Alexa.Audio.PlayQueue",
        "GetNextItem.Response",
        {"isQueueFinished": False, "item": nxt},
    )


def handle_get_previous_item(payload: dict) -> Response:
    content_id, index = _current_index_and_content(payload)
    prev = item_at(content_id, index - 1)
    if prev is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "start of queue")
    return envelope("Alexa.Audio.PlayQueue", "GetPreviousItem.Response", {"item": prev})


def handle_get_item(payload: dict) -> Response:
    ref = (payload.get("targetItemReference") or {}).get("value") or {}
    content_id = ref.get("contentId", "")
    try:
        index = int(ref.get("id", "0"))
    except (TypeError, ValueError):
        index = 0
    item = item_at(content_id, index)
    if item is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "item unavailable")
    # Response namespace is Alexa.Audio.PlayQueue even though the directive
    # arrives on Alexa.Media.PlayQueue. This matches Amazon's own examples.
    return envelope("Alexa.Audio.PlayQueue", "GetItem.Response", {"item": item})


ROUTES = {
    ("Alexa.Media.Search", "GetPlayableContent"): handle_get_playable_content,
    ("Alexa.Media.Playback", "Initiate"): handle_initiate,
    ("Alexa.Audio.PlayQueue", "GetNextItem"): handle_get_next_item,
    ("Alexa.Audio.PlayQueue", "GetPreviousItem"): handle_get_previous_item,
    ("Alexa.Media.PlayQueue", "GetItem"): handle_get_item,
}


@app.post("/music")
@app.post("/")
def music():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("Alexa", "INVALID_DIRECTIVE", "expected JSON body", "3.0"), 400

    header = body.get("header") or {}
    namespace, name = header.get("namespace", "?"), header.get("name", "?")
    payload = body.get("payload") or {}

    capture({"headers": dict(request.headers), "body": body}, f"{namespace}.{name}")

    handler = ROUTES.get((namespace, name))
    if handler is None:
        logger.warning("unhandled directive %s.%s", namespace, name)
        return error("Alexa", "INVALID_DIRECTIVE", f"unhandled {namespace}.{name}", "3.0")

    started = time.monotonic()
    try:
        resp = handler(payload)
    except Exception:
        logger.exception("handler %s.%s failed", namespace, name)
        return error("Alexa", "INTERNAL_ERROR", "handler failed", "3.0")
    logger.info("%s.%s served in %dms", namespace, name, (time.monotonic() - started) * 1000)
    return resp


# --------------------------------------------------------------------------
# asset proxies (Navidrome is tailnet-only; Amazon fetches these)
# --------------------------------------------------------------------------


def _proxy(upstream: str, content_type_default: str):
    req = urllib.request.Request(upstream)
    rng = request.headers.get("Range")
    if rng:
        req.add_header("Range", rng)
    upstream_resp = urllib.request.urlopen(req, timeout=20)
    status = upstream_resp.status
    headers = {}
    for key in ("Content-Type", "Content-Length", "Accept-Ranges", "Content-Range"):
        if value := upstream_resp.headers.get(key):
            headers[key] = value
    headers.setdefault("Content-Type", content_type_default)
    return Response(upstream_resp, status=status, headers=headers, direct_passthrough=True)


@app.get("/stream/<song_id>/<int:expires>/<sig>")
def stream(song_id: str, expires: int, sig: str):
    if not verify("stream", song_id, expires, sig):
        return jsonify({"error": "bad or expired signature"}), 403
    return _proxy(subsonic.stream_url(song_id), "audio/mpeg")


@app.get("/art/<cover_id>/<int:expires>/<sig>")
def art(cover_id: str, expires: int, sig: str):
    if not verify("art", cover_id, expires, sig):
        return jsonify({"error": "bad or expired signature"}), 403
    return _proxy(subsonic.cover_art_url(cover_id), "image/jpeg")


# --------------------------------------------------------------------------
# ops
# --------------------------------------------------------------------------


def _authorized() -> bool:
    expected = os.environ.get("ADMIN_TOKEN")
    return bool(expected) and request.headers.get("X-Admin-Token") == expected


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/captures")
def captures():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(
        [
            {"file": p.name, "content": json.loads(p.read_text())}
            for p in sorted(LOG_DIR.glob("*.json"))
        ]
    )


@app.get("/diag")
def diag():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        res = subsonic.search(request.args.get("q", "the"), songs=3)
        return jsonify(
            {
                "subsonic": "ok",
                "songs": [
                    {"id": s["id"], "title": s.get("title"), "artist": s.get("artist")}
                    for s in res.get("song", [])
                ],
            }
        )
    except Exception as exc:
        return jsonify({"subsonic": "error", "detail": str(exc)}), 502


@app.get("/icons/<path:name>")
def icons(name: str):
    return send_from_directory(ICON_DIR, name, mimetype="image/png")


@app.get("/privacy")
def privacy():
    return (
        "<h1>Privacy</h1><p>Private, single-user development skill. Streams from a "
        "self-hosted server. No user data is collected, stored, or shared.</p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/terms")
def terms():
    return (
        "<h1>Terms of Use</h1><p>Private, single-user development skill, as-is, "
        "no warranty.</p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5056")))
