"""Ampere: an Alexa Music Skill for any Subsonic-compatible music server.

Speaks Amazon's Music/Radio/Podcast Skill API on one side and the Subsonic API
on the other. Nothing here is specific to one server; Navidrome is simply what
it has been tested against. Self-hosted servers are usually not reachable from
the internet, so every asset Amazon fetches (audio, cover art) is proxied
through this service behind a signed, expiring token.

Design note: Alexa echoes {id, queueId, contentId} back on every queue request,
so playback position is carried by Alexa rather than stored here. Queues are
derived from the contentId, which keeps the hot path within Amazon's 100ms p50
budget for Initiate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import random
import re
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape

from flask import Flask, Response, jsonify, request, send_from_directory

import oauth
import queuestate
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

# contentId -> [song, ...]. Whole records, not ids: re-fetching each song was
# what pushed Initiate past 3s. Best-effort; a miss re-derives from Subsonic.
_QUEUE_CACHE: OrderedDict[str, list[dict]] = OrderedDict()
_QUEUE_CACHE_MAX = 256

# What plays once the requested content runs out, if anything.
#
# Off by default. A queue that ends is what both Alexa and the listener already
# expect, and continuation must never pre-empt or replace what was actually
# asked for: it begins strictly past the last track of the requested content.
#
# The four live values continue with, respectively, more of the seed artist,
# more of the seed genre, the library at random, and a station built from
# artists similar to the seed.
AFTER_CONTENT_MODES = ("stop", "artist", "genre", "library", "radio")


def after_content_setting(raw: str) -> str:
    """Read AFTER_CONTENT, refusing to guess at a value we do not know."""
    value = (raw or "").strip().lower() or "stop"
    if value not in AFTER_CONTENT_MODES:
        logger.warning("unknown AFTER_CONTENT %r, using stop", raw)
        return "stop"
    return value


AFTER_CONTENT = after_content_setting(os.environ.get("AFTER_CONTENT", "stop"))

# How wide a station casts, and how much of each artist it will take. Uncapped,
# a seed artist with 200 tracks next to neighbours with 10 gives a station that
# is mostly the seed artist again.
RADIO_ARTISTS = int(os.environ.get("RADIO_ARTISTS", "12"))
RADIO_TRACKS_PER_ARTIST = int(os.environ.get("RADIO_TRACKS_PER_ARTIST", "12"))

# seed artist id -> [artist id, ...]. Kept apart from _QUEUE_CACHE because
# getArtistInfo2 is the expensive half of building a station at ~725ms, and its
# answer does not go stale when the track cache turns over.
_RADIO_CACHE: OrderedDict[str, list[str]] = OrderedDict()
_RADIO_CACHE_MAX = 256


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


def artist_tracks(artist_id: str) -> list[dict]:
    """Every track by an artist, in discography order.

    getTopSongs is deliberately not used: it costs ~0.9s and comes back empty
    against this library. That is a fact about getTopSongs and not about
    Last.fm-backed endpoints in general, which is worth being precise about,
    because getArtistInfo2 is just as Last.fm-backed and returns ~20 usable
    similar artists in 725ms. Stations are built on exactly that.

    Albums are fetched concurrently but re-sorted into the discography's own
    order, because the queue must be identical every time it is re-derived.
    """
    info = subsonic.call("getArtist.view", id=artist_id).get("artist", {})
    albums = info.get("album", [])
    if not albums:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(albums))) as pool:
        results = list(pool.map(lambda alb: subsonic.album_tracks(alb["id"]), albums))
    return [track for album in results for track in album]


def similar_artist_ids(seed_id: str) -> list[str]:
    """The seed artist, then the library artists most like them."""
    if seed_id in _RADIO_CACHE:
        _RADIO_CACHE.move_to_end(seed_id)
        return _RADIO_CACHE[seed_id]

    ids = [seed_id]
    try:
        for artist in subsonic.similar_artists(seed_id, RADIO_ARTISTS):
            artist_id = str(artist.get("id"))
            if artist_id not in ids:
                ids.append(artist_id)
    except Exception:
        logger.exception("similar artist lookup failed for %s", seed_id)

    # Only a real answer is worth keeping, as with the describe cache: caching
    # a failed lookup would pin the station to the seed artist alone for as
    # long as the entry survived.
    if len(ids) > 1:
        _RADIO_CACHE[seed_id] = ids
        while len(_RADIO_CACHE) > _RADIO_CACHE_MAX:
            _RADIO_CACHE.popitem(last=False)
    return ids


def artist_sample(artist_id: str, tracks: list[dict], limit: int) -> list[dict]:
    """A capped, stable slice of one artist's catalogue.

    Seeded on the artist id rather than the queueId, so the pool is identical
    for every queue built from the same contentId. That is what lets the pool
    be cached against the contentId at all.
    """
    if len(tracks) <= limit:
        return list(tracks)
    picked = random.Random(artist_id).sample(range(len(tracks)), limit)
    return [tracks[i] for i in sorted(picked)]


def radio_pool(seed_id: str, artist_ids: list[str] | None = None) -> list[dict]:
    """Track pool for a station: the seed artist and artists like them.

    Expanding a dozen artists into tracks is a dozen discography walks, so this
    resolves the whole pool in one go and lets resolve_tracks cache it against
    the contentId. Doing it per track would put a round trip on every single
    GetNextItem.

    Callers that need to know whether the station is real can resolve the artist
    list themselves and pass it in, rather than paying for the lookup twice.
    """
    artist_ids = similar_artist_ids(seed_id) if artist_ids is None else artist_ids
    with ThreadPoolExecutor(max_workers=min(4, len(artist_ids))) as pool:
        walks = list(pool.map(artist_tracks, artist_ids))
    return [
        track
        for artist_id, tracks in zip(artist_ids, walks)
        for track in artist_sample(artist_id, tracks, RADIO_TRACKS_PER_ARTIST)
    ]


def resolve_tracks(content_id: str) -> list[dict]:
    """contentId -> ordered list of Navidrome song objects.

    Navidrome returns full song records from album/playlist/genre listings, so
    they are cached whole. An earlier version kept only the ids and re-fetched
    each song with getSong on every item, which cost one round trip per track
    and pushed Initiate past 3 seconds against Amazon's 400ms p99 budget.
    """
    if content_id in _QUEUE_CACHE:
        _QUEUE_CACHE.move_to_end(content_id)
        return _QUEUE_CACHE[content_id]

    kind, _, ident = content_id.partition(":")
    cacheable = True
    try:
        if kind == "tr":
            songs = [subsonic.song(ident)]
        elif kind == "al":
            songs = subsonic.album_tracks(ident)
        elif kind == "ar":
            songs = artist_tracks(ident)
        elif kind == "rad":
            # A station that fell back to the seed artist alone is not a
            # station, and caching one pins the failure permanently, because
            # this cache never re-derives an entry it already holds. Navidrome
            # returned no similar artists for Foreigner once, and every request
            # after that served a Foreigner-only "station" until the process
            # was restarted. similar_artist_ids already refuses to cache the
            # degraded lookup; the pool built from it has to refuse too.
            artist_ids = similar_artist_ids(ident)
            songs = radio_pool(ident, artist_ids)
            cacheable = len(artist_ids) > 1
        elif kind == "pl":
            songs = subsonic.playlist_tracks(ident)
        elif kind == "gen":
            # Deterministic, unlike random_songs(genre=...). The queue is
            # re-derived from the contentId on every GetNextItem, so a random
            # listing would reshuffle mid-playback.
            songs = subsonic.songs_by_genre(ident)
        elif kind == "star":
            songs = subsonic.starred_songs()
        else:
            songs = subsonic.random_songs(50)
    except Exception:
        logger.exception("resolve_tracks failed for %s", content_id)
        songs = []

    songs = [s for s in songs if s and s.get("id")]
    if cacheable:
        _QUEUE_CACHE[content_id] = songs
        while len(_QUEUE_CACHE) > _QUEUE_CACHE_MAX:
            _QUEUE_CACHE.popitem(last=False)
    return songs


def match_playlist(query: str) -> dict | None:
    """Find a playlist by spoken name.

    Subsonic's search3 does not cover playlists, so this matches against the
    playlist list directly. Speech tends to append the word "playlist", and
    casing is never reliable, so both are normalised away before comparing.
    """
    wanted = re.sub(r"\bplaylists?\b", "", query, flags=re.I).strip().lower()
    if not wanted:
        return None
    try:
        candidates = subsonic.playlists()
    except Exception:
        logger.exception("playlist lookup failed")
        return None

    exact = [p for p in candidates if (p.get("name") or "").lower() == wanted]
    if exact:
        return exact[0]
    partial = [p for p in candidates if wanted in (p.get("name") or "").lower()]
    return partial[0] if partial else None


def name_prop(text: str) -> dict:
    """Name for both speech and display.

    Amazon's examples lowercase `speech.text` (they use it for TTS) and put
    natural case in `display`. But their own docs note "Currently the Alexa
    service ignores this [display] field" - so `speech.text` is what actually
    renders on screen, and lowercasing it made every artist and album name
    appear lowercase in the player. Natural case reads correctly on screen and
    speaks the same.
    """
    value = text or ""
    return {
        "speech": {"type": "PLAIN_TEXT", "text": value},
        "display": value,
    }


# Art is a required member of MediaMetadata and every ArtSource requires an
# HTTPS url, so an empty source list is not a valid answer for content that
# happens to have no cover (a genre, or the whole library). Fall back to the
# skill icon rather than returning nothing.
FALLBACK_ART = [
    {"url": f"{PUBLIC_BASE}/icons/ampere-512.png",
     "size": "X_LARGE", "widthPixels": 512, "heightPixels": 512},
    {"url": f"{PUBLIC_BASE}/icons/ampere-108.png",
     "size": "SMALL", "widthPixels": 108, "heightPixels": 108},
]


def art_block(cover_id: str | None) -> dict:
    if not cover_id:
        return {"sources": list(FALLBACK_ART)}
    url, _ = signed_url("art", cover_id)
    return {
        "sources": [
            {"url": url, "size": "X_LARGE", "widthPixels": 600, "heightPixels": 600}
        ]
    }


def build_item(song: dict, index: int, total: int, endless: bool = False) -> dict:
    """Build an Alexa Item from a Subsonic song.

    `endless` says the queue continues past its own last track, so NEXT stays
    enabled there. An undeclared or disabled control is a control Alexa will
    not offer, and a station whose skip button greys out on track twelve is
    not a station.
    """
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
            {"type": "COMMAND", "name": "NEXT", "enabled": endless or index < total - 1},
            {"type": "COMMAND", "name": "PREVIOUS", "enabled": index > 0},
            # ADJUST/SEEK_POSITION is what makes the progress bar draggable in
            # the Alexa app. Same rule as SHUFFLE and LOOP: a control that is
            # not declared is a control Alexa disables, which is why scrubbing
            # worked for Spotify and not for us. Only offer it when the track
            # length is known, because seeking an unknown duration is
            # meaningless. Navidrome serves the transcode with Accept-Ranges,
            # so the ranged GET that follows is satisfied by the proxy.
            {"type": "ADJUST", "name": "SEEK_POSITION",
             "enabled": bool(song.get("duration"))},
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


def queue_order(content_id: str, queue_id: str) -> list[dict]:
    """Songs in the order this queue should play them."""
    state = queuestate.get(queue_id)
    return queuestate.order(resolve_tracks(content_id), queue_id, bool(state["shuffle"]))


def continuation_mode(content_id: str) -> str:
    """What follows this content once it runs out.

    A station is endless by definition, so `rad:` continues with more of itself
    whatever AFTER_CONTENT says. Everything else obeys the setting, which is
    off unless it has been turned on.
    """
    return "radio" if content_id.partition(":")[0] == "rad" else AFTER_CONTENT


def seed_song(content_id: str) -> dict | None:
    """The track a continuation is built from.

    The last thing actually requested: a track request names one outright, and
    a collection is seeded on the last track of its base order. Base order and
    not play order, because the continuation has to be identical for every
    queue derived from the same contentId or it could not be cached against it.
    """
    songs = resolve_tracks(content_id)
    return songs[-1] if songs else None


def continuation_content(content_id: str, mode: str) -> str:
    """The contentId whose tracks extend this queue past its end.

    Expressed as a contentId rather than a track list so the pool resolves and
    caches through exactly the same path as everything else.
    """
    if mode == "library":
        return "rnd:all"

    kind, _, ident = content_id.partition(":")
    if mode == "genre":
        genre = (seed_song(content_id) or {}).get("genre")
        return f"gen:{genre}" if genre else "rnd:all"

    # An artist or station request names its own seed, so no lookup is needed.
    artist_id = ident if kind in ("ar", "rad") else (
        (seed_song(content_id) or {}).get("artistId")
    )
    if not artist_id:
        # Navidrome leaves artistId and genre off some imported files. Falling
        # back to the library keeps the music going rather than ending on a
        # technicality.
        return "rnd:all"
    return f"ar:{artist_id}" if mode == "artist" else f"rad:{artist_id}"


def song_at(content_id: str, index: int, queue_id: str) -> dict | None:
    """The song at an index, extending past the end of the queue if configured.

    Continuation cannot be a switch to a different queue. Alexa echoes back the
    contentId it was handed at Initiate on every later request (confirmed
    against captures: ids 6, 7 and 8 of one queue all carried the contentId it
    started with) and an Item has no field to hand it a different one. So this
    is a virtual extension instead: indexes past the base queue map into the
    continuation pool, deterministically, which keeps every index reproducible
    from (contentId, queueId, index) alone with nothing stored.
    """
    if index < 0:
        return None
    songs = queue_order(content_id, queue_id)
    if not songs:
        # An empty queue is a failure to answer the request, not an invitation
        # to play something else instead.
        return None
    if index < len(songs):
        return songs[index]

    mode = continuation_mode(content_id)
    if mode == "stop":
        return None
    pool = resolve_tracks(continuation_content(content_id, mode))
    if not pool:
        return None

    # Every further pass over the pool is reshuffled on its lap number, so a
    # long listen does not replay one running order, and any index is still
    # derivable on its own.
    lap, position = divmod(index - len(songs), len(pool))
    return queuestate.order(pool, f"{queue_id}:{lap}", True)[position]


def item_at(content_id: str, index: int, queue_id: str = "") -> dict | None:
    song = song_at(content_id, index, queue_id)
    if song is None:
        return None
    total = len(queue_order(content_id, queue_id))
    return build_item(song, index, total, continuation_mode(content_id) != "stop")


# --------------------------------------------------------------------------
# envelope helpers
# --------------------------------------------------------------------------


def envelope(namespace: str, name: str, payload: dict, version: str = "1.0") -> Response:
    return jsonify(
        {
            "header": {
                "messageId": str(uuid.uuid4()),
                "namespace": namespace,
                "name": name,
                "payloadVersion": version,
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


# MediaMetadata.type is a closed enum: ALBUM, ARTIST, GENRE, PLAYLIST, TRACK
# for music, plus STATION and PROGRAM for radio and podcasts. An earlier
# version answered TRACK for every kind, so an artist request came back
# described as a track.
CONTENT_TYPES = {
    "ar": "ARTIST",
    "al": "ALBUM",
    "tr": "TRACK",
    "pl": "PLAYLIST",
    "gen": "GENRE",
    "rad": "STATION",
}


_DESCRIBE_CACHE: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()
_DESCRIBE_CACHE_MAX = 8192
_WARM_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="describe-warm")


def prewarm_artists() -> None:
    """Load every artist name up front.

    Artists are the overwhelmingly common catalog hit and `getArtists` returns
    the whole index in one call, so the names can be resident before Alexa ever
    asks. Without this the first request for any given artist answers with no
    name at all, because the lookup deliberately refuses to block.
    """
    try:
        index = subsonic.call("getArtists.view").get("artists", {}).get("index", [])
        for group in index:
            for artist in group.get("artist", []):
                _DESCRIBE_CACHE[f"ar:{artist['id']}"] = (
                    artist.get("name"), artist.get("coverArt")
                )
        logger.info("prewarmed %d artist names", len(_DESCRIBE_CACHE))
    except Exception:
        logger.exception("artist prewarm failed")


def describe_content(prefix: str, native: str) -> tuple[str | None, str | None]:
    """Real name and cover art for a contentId.

    When Alexa resolves speech against our catalog it sends `entityId` and no
    `value`, so there is no spoken text to echo back. Looking the entity up is
    the only way to name it; the previous fallback used the entity kind, which
    made every catalog hit answer with the literal string "artist".

    Never blocks. This sits on the GetPlayableContent path, which answered in
    no measurable time before the lookup existed and started taking 300-700ms
    after. A miss returns nothing and warms the cache in the background, so the
    label improves on a later request but the response time never moves. A name
    is worth having; it is not worth spending Amazon's patience on, and a bare
    literal ("artist") was good enough to play music for days.
    """
    key = f"{prefix}:{native}"
    if key in _DESCRIBE_CACHE:
        _DESCRIBE_CACHE.move_to_end(key)
        return _DESCRIBE_CACHE[key]
    _WARM_POOL.submit(_warm, key, prefix, native)
    return None, None


def _warm(key: str, prefix: str, native: str) -> None:
    result = _lookup_content(prefix, native)
    # Only a real answer is worth keeping; a failed lookup should be retried.
    if result == (None, None):
        return
    _DESCRIBE_CACHE[key] = result
    while len(_DESCRIBE_CACHE) > _DESCRIBE_CACHE_MAX:
        _DESCRIBE_CACHE.popitem(last=False)


def _lookup_content(prefix: str, native: str) -> tuple[str | None, str | None]:
    try:
        if prefix == "ar":
            info = subsonic.call("getArtist.view", id=native).get("artist", {})
            albums = info.get("album") or []
            cover = info.get("coverArt") or (albums[0].get("coverArt") if albums else None)
            return info.get("name"), cover
        if prefix == "al":
            info = subsonic.call("getAlbum.view", id=native).get("album", {})
            return info.get("name"), info.get("coverArt")
        if prefix == "tr":
            song = subsonic.song(native)
            return song.get("title"), song.get("coverArt")
        if prefix == "pl":
            for playlist in subsonic.playlists():
                if playlist.get("id") == native:
                    return playlist.get("name"), playlist.get("coverArt")
        if prefix == "gen":
            return native, None
    except Exception:
        logger.exception("metadata lookup failed for %s:%s", prefix, native)
    return None, None


# Trailing only, so "Radiohead" and "Radio Moscow" are still themselves. Same
# treatment match_playlist gives the trailing word "playlist".
_STATION_WORD = re.compile(r"\s+(radio|station)$", re.I)


def station_request(attrs: list[dict]) -> tuple[bool, list[dict]]:
    """Did the user ask for a station rather than for the content itself?

    Two signals, both in fields Amazon already sends. MEDIA_TYPE carrying
    STATION is the shape their own metadata enum implies, but it has never
    appeared in a capture from this skill, so the spoken form is the one that
    has to work: "play <artist> radio" arrives with the word still attached to
    the free text. When neither fires, `rad:` is never reached by voice, only
    by a queue running out.

    Returns the verdict and the attributes with the trailing word removed, so
    the search that follows looks for the artist and not for their name plus a
    word they never said.
    """
    wanted = any(
        a.get("type") == "MEDIA_TYPE" and str(a.get("value") or "").upper()
        in ("STATION", "RADIO")
        for a in attrs
    )
    stripped = []
    for attr in attrs:
        value = attr.get("value")
        if value and attr.get("type") != "MEDIA_TYPE" and _STATION_WORD.search(value):
            wanted = True
            attr = {**attr, "value": _STATION_WORD.sub("", value).strip()}
        stripped.append(attr)
    return wanted, stripped


def station_content(artist_id: str, spoken: str | None) -> Response:
    """GetPlayableContent answer for an explicitly requested station."""
    looked_up, cover = describe_content("ar", artist_id)
    name = spoken or looked_up or artist_id
    logger.info("station requested: rad:%s (%s)", artist_id, name)
    # Building the pool costs a getArtistInfo2 plus a discography walk per
    # artist, and Initiate follows about a second later. Warm it off the
    # request path so that second is spent usefully rather than on Amazon's
    # clock.
    _WARM_POOL.submit(resolve_tracks, f"rad:{artist_id}")
    return envelope(
        "Alexa.Media.Search",
        "GetPlayableContent.Response",
        {
            "content": {
                "id": f"rad:{artist_id}",
                "metadata": {
                    "type": "STATION",
                    "name": name_prop(f"{name} Radio"),
                    "art": art_block(cover),
                },
            }
        },
    )


def handle_get_playable_content(payload: dict) -> Response:
    attrs = (payload.get("selectionCriteria") or {}).get("attributes") or []
    station, attrs = station_request(attrs)

    # When Alexa resolves speech against our uploaded catalog it returns the
    # catalog's own entity id (e.g. "artist.<navidrome id>") rather than free
    # text. That is a direct hit: map it straight to a contentId, no search.
    for attr in attrs:
        entity_id = attr.get("entityId") or ""
        kind, _, native = entity_id.partition(".")
        prefix = {
            "artist": "ar",
            "album": "al",
            "track": "tr",
            "playlist": "pl",
            "genre": "gen",
        }.get(kind)
        if prefix and native:
            if station and prefix == "ar":
                return station_content(native, attr.get("value"))
            looked_up, cover = describe_content(prefix, native)
            display = attr.get("value") or looked_up or native
            logger.info(
                "catalog entity resolved: %s -> %s:%s (%s)",
                entity_id, prefix, native, display,
            )
            return envelope(
                "Alexa.Media.Search",
                "GetPlayableContent.Response",
                {
                    "content": {
                        "id": f"{prefix}:{native}",
                        "metadata": {
                            "type": CONTENT_TYPES[prefix],
                            "name": name_prop(display),
                            "art": art_block(cover),
                        },
                    }
                },
            )

    # No catalog match: fall back to free-text search over whatever was said.
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
        "PLAYLIST": ["playlist"],
    }.get(wanted or media_type or "", ["playlist", "song", "album", "artist"])

    # A station is always built from an artist, so resolve one even if Alexa
    # labelled the request as something else.
    if station:
        preference = ["artist", "album", "song"]

    cover = None
    if not query:
        # "play <provider name>" with no criteria: play something.
        content_id, display, ctype = "rnd:all", "Your Library", "PLAYLIST"
    elif preference == ["genre"]:
        content_id, display, ctype = f"gen:{query}", query, "GENRE"
    elif (match := match_playlist(query)) and preference[0] == "playlist":
        content_id, ctype = f"pl:{match['id']}", "PLAYLIST"
        display, cover = match.get("name", query), match.get("coverArt")
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
        if station and kind == "artist":
            return station_content(hit["id"], hit.get("name"))
        prefix = {"song": "tr", "album": "al", "artist": "ar"}[kind]
        content_id = f"{prefix}:{hit['id']}"
        display = hit.get("title") or hit.get("name") or query
        ctype, cover = CONTENT_TYPES[prefix], hit.get("coverArt")

    return envelope(
        "Alexa.Media.Search",
        "GetPlayableContent.Response",
        {
            "content": {
                "id": content_id,
                "metadata": {
                    "type": ctype,
                    "name": name_prop(display),
                    "art": art_block(cover),
                },
            }
        },
    )


def queue_controls(state: dict) -> list[dict]:
    """Controls for the queue.

    Amazon: "if the enablement status of a control isn't specified, Alexa
    assumes the control is disabled." An empty list therefore silently kills
    shuffle and loop. `selected` reflects current state so the app and the
    voice model agree.

    REPEAT is emitted as type CYCLE. That combination appears only in Amazon's
    Initiate examples and not in the BaseControl/QueueControl tables, but it is
    the only shape they have ever published for it.
    """
    return [
        {"type": "TOGGLE", "name": "SHUFFLE", "enabled": True,
         "selected": bool(state.get("shuffle"))},
        {"type": "TOGGLE", "name": "LOOP", "enabled": True,
         "selected": bool(state.get("loop"))},
        {"name": "REPEAT", "type": "CYCLE", "enabled": True,
         "value": {"status": state.get("repeat", "OFF")}},
    ]


def generic_response(payload_version: str = "3.0") -> Response:
    """The empty Alexa.Response event that acknowledges Set* directives.

    Amazon documents the payload as empty and gives no state echo. The only
    literal example they publish carries payloadVersion 3.0 even though the
    PlayQueue directives themselves are 1.0, and mirroring 1.0 instead left the
    Alexa app never registering the new shuffle/loop state.
    """
    return envelope("Alexa", "Response", {}, version=payload_version)


# Asking for an artist and getting the first track of their earliest album,
# every single time, is not what anyone means. Collections shuffle by default;
# albums and playlists keep their order, because their order is the point.
#
# This has to be a default rather than a reading of the request: Alexa sends
# `shuffle: false` on every fresh Initiate, so "the user turned it off" and
# "nothing was specified" are indistinguishable here. The queue state we return
# reports shuffle as on, so the app shows it on and SetShuffle can still turn it
# off for album order.
SHUFFLE_BY_DEFAULT = {"ar", "gen", "star", "rnd", "rad"}


def shuffle_by_default(content_id: str) -> bool:
    return content_id.partition(":")[0] in SHUFFLE_BY_DEFAULT


def warm_continuation(content_id: str) -> None:
    """Resolve whatever will extend this queue, before it is needed.

    Runs off the request path, so a slow seed lookup costs nothing. Failures
    are logged and dropped: the continuation will simply be built on demand,
    which is what happened before this existed.
    """
    mode = continuation_mode(content_id)
    if mode == "stop":
        return
    try:
        resolve_tracks(continuation_content(content_id, mode))
    except Exception:
        logger.exception("continuation warm failed for %s", content_id)


def handle_initiate(payload: dict) -> Response:
    content_id = payload.get("contentId") or "rnd:all"
    queue_id = str(uuid.uuid4())

    modes = payload.get("playbackModes") or {}
    state = queuestate.update(
        queue_id,
        shuffle=bool(modes.get("shuffle")) or shuffle_by_default(content_id),
        loop=bool(modes.get("loop")),
        repeat=(modes.get("repeat") or {}).get("status", "OFF"),
    )

    first = item_at(content_id, 0, queue_id)
    if first is None:
        return error("Alexa.Media", "CONTENT_NOT_FOUND", f"nothing playable for {content_id}")

    # Build the continuation pool now rather than at the track transition.
    # A station is a dozen discography walks and takes ~3s cold, and without
    # this it lands on the first GetNextItem past the end of the queue, which
    # is the one moment with no slack at all. A directly requested rad: queue
    # already warms during GetPlayableContent; this gives a queue that will
    # continue into one the same treatment.
    _WARM_POOL.submit(warm_continuation, content_id)

    return envelope(
        "Alexa.Media.Playback",
        "Initiate.Response",
        {
            "playbackMethod": {
                "type": "ALEXA_AUDIO_PLAYER_QUEUE",
                "id": queue_id,
                "controls": queue_controls(state),
                # Omitting rules.feedback disables thumbs the same way an empty
                # controls list disables shuffle.
                "rules": {"feedback": {"type": "PREFERENCE", "enabled": True}},
                "firstItem": first,
            }
        },
    )


def _ref_values(payload: dict, key: str = "currentItemReference") -> dict:
    """Normalise the two reference shapes Amazon uses.

    GetNextItem / GetPreviousItem / JumpToItem send a bare
    {id, queueId, contentId}. SetShuffle / SetLoop / SetRepeat / GetView /
    GetItem wrap the same thing as {namespace, name, value}.
    """
    ref = payload.get(key) or {}
    if isinstance(ref.get("value"), dict):
        ref = ref["value"]
    return ref


def _locate(payload: dict, key: str = "currentItemReference") -> tuple[str, int, str]:
    ref = _ref_values(payload, key)
    content = ref.get("contentId") or (ref.get("content") or {}).get("id", "")
    queue_id = ref.get("queueId", "")
    try:
        index = int(ref.get("id", "0"))
    except (TypeError, ValueError):
        index = 0
    return content, index, queue_id


def handle_get_next_item(payload: dict) -> Response:
    content_id, index, queue_id = _locate(payload)
    total = len(queue_order(content_id, queue_id))
    state = queuestate.get(queue_id)

    nxt_index = index + 1
    if nxt_index >= total and state.get("loop") and total:
        # Loop on: the last track is followed by the first, and the queue never
        # reports finished.
        nxt_index = 0

    nxt = item_at(content_id, nxt_index, queue_id)
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
    content_id, index, queue_id = _locate(payload)
    state = queuestate.get(queue_id)

    prev_index = index - 1
    if prev_index < 0 and state.get("loop"):
        prev_index = len(queue_order(content_id, queue_id)) - 1

    prev = item_at(content_id, prev_index, queue_id)
    if prev is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "start of queue")
    return envelope("Alexa.Audio.PlayQueue", "GetPreviousItem.Response", {"item": prev})


def handle_get_item(payload: dict) -> Response:
    content_id, index, queue_id = _locate(payload, "targetItemReference")
    item = item_at(content_id, index, queue_id)
    if item is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "item unavailable")
    # Response namespace is Alexa.Audio.PlayQueue even though the directive
    # arrives on Alexa.Media.PlayQueue. This matches Amazon's own examples.
    return envelope("Alexa.Audio.PlayQueue", "GetItem.Response", {"item": item})


def handle_set_shuffle(payload: dict) -> Response:
    _content, _index, queue_id = _locate(payload)
    queuestate.update(queue_id, shuffle=bool(payload.get("enable")))
    logger.info("shuffle=%s on queue %s", payload.get("enable"), queue_id[:8])
    return generic_response()


def handle_set_loop(payload: dict) -> Response:
    _content, _index, queue_id = _locate(payload)
    queuestate.update(queue_id, loop=bool(payload.get("enable")))
    logger.info("loop=%s on queue %s", payload.get("enable"), queue_id[:8])
    return generic_response()


def handle_set_repeat(payload: dict) -> Response:
    _content, _index, queue_id = _locate(payload)
    status = (payload.get("mode") or {}).get("status", "OFF")
    queuestate.update(queue_id, repeat=status)
    logger.info("repeat=%s on queue %s", status, queue_id[:8])
    return generic_response()


def handle_get_view(payload: dict) -> Response:
    """Current queue for the app's queue view.

    Amazon discards anything past 10 items, so send a window around the
    current track rather than the whole queue.

    The window walks until an index has nothing behind it rather than stopping
    at the length of the base queue, so a continuing queue shows what is coming
    next instead of an empty view.
    """
    content_id, index, queue_id = _locate(payload)
    state = queuestate.get(queue_id)

    start = max(0, index)
    items = []
    for i in range(start, start + 10):
        item = item_at(content_id, i, queue_id)
        if item is None:
            break
        items.append(item)

    if not items:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "queue unavailable")

    # Response namespace flips to Alexa.Audio.PlayQueue, as with GetItem.
    return envelope(
        "Alexa.Audio.PlayQueue",
        "GetView.Response",
        {"queueControls": queue_controls(state), "items": items},
    )


def handle_jump_to_item(payload: dict) -> Response:
    content_id, _index, queue_id = _locate(payload)
    try:
        target = int(payload.get("targetItemId", ""))
    except (TypeError, ValueError):
        return error("Alexa.Media", "INVALID_ITEM", "unrecognised target item")

    item = item_at(content_id, target, queue_id)
    if item is None:
        return error("Alexa.Media", "INVALID_ITEM", "The requested item is no longer valid.")
    return envelope("Alexa.Audio.PlayQueue", "JumpToItem.Response", {"item": item})


def handle_receive_feedback(payload: dict) -> Response:
    """Thumbs up/down, written through to Navidrome stars."""
    refs = (payload.get("activeContext") or {}).get("content") or []
    ref = (refs[0] if refs else {}).get("value") or {}
    content_id = ref.get("contentId", "")
    try:
        index = int(ref.get("id", "0"))
    except (TypeError, ValueError):
        index = 0
    queue_id = ref.get("queueId", "")

    value = (payload.get("feedback") or {}).get("value")
    # song_at rather than an index into the base queue: thumbs on a track the
    # continuation is playing must star that track, not fall off the end.
    song = song_at(content_id, index, queue_id)
    if song:
        song_id = song["id"]
        try:
            if value == "POSITIVE":
                subsonic.star(song_id)
            elif value == "NEGATIVE":
                subsonic.unstar(song_id)
        except Exception:
            logger.exception("feedback write failed for %s", song_id)

    if value == "NEGATIVE":
        # Skip the disliked track; Alexa then asks for the next item.
        return envelope(
            "Alexa.UserPreference.Audio",
            "ReceiveFeedback.Response",
            {"skip": True},
            version="2.0",
        )
    return generic_response("2.0")


ART_LADDER = [
    ("X_SMALL", 48), ("SMALL", 60), ("MEDIUM", 110), ("LARGE", 256), ("X_LARGE", 600),
]


def art_sources(cover_id: str | None) -> dict:
    """Full size ladder.

    GetDisplayableContent requests carry no device or viewport information, so
    there is no way to pick a size per screen. Always return every rung.
    """
    if not cover_id:
        return {"sources": []}
    url, _ = signed_url("art", cover_id)
    return {
        "sources": [
            {"url": url, "size": size, "widthPixels": px, "heightPixels": px}
            for size, px in ART_LADDER
        ]
    }


def display_item(content_id: str, kind: str, display: str, cover: str | None,
                 author: str | None = None) -> dict:
    """One tile in a browse shelf."""
    meta = {
        "type": kind,
        "name": name_prop(display),
        "art": art_sources(cover),
    }
    # MediaMetadata makes `authors` REQUIRED on ALBUM (and PROGRAM_SERIES), and
    # optional on TRACK. Emit it unconditionally for ALBUM so a missing artist
    # name can never produce a schema-invalid item.
    if kind == "ALBUM":
        meta["authors"] = [{"name": name_prop(author or "Unknown Artist")}]
    elif author and kind == "TRACK":
        meta["authors"] = [{"name": name_prop(author)}]
    return {
        "id": content_id,
        "actions": {
            "playable": True,
            # Artists, albums and playlists can be drilled into; a track cannot.
            "browsable": kind in ("ARTIST", "ALBUM", "PLAYLIST", "GENRE"),
        },
        "metadata": meta,
    }


def handle_get_displayable_content(payload: dict) -> Response:
    """Browse/show experience for Echo Show, Fire TV and the Alexa app.

    Unlike GetPlayableContent this returns grouped shelves rather than a single
    result, takes an ARRAY of resolved criteria, and answers on payloadVersion
    2.0. Errors still go out on Alexa.Media at 1.0.
    """
    criteria = payload.get("alexaResolvedSelectionCriteria") or []
    limit = int(payload.get("maxResultLimit") or 25)

    # Pull any free text / entity ids Alexa resolved. SEARCH_METHOD is
    # undocumented and appears for contextual recommendations, so treat its
    # presence as "just show something".
    terms, entity_ids, contextual = [], [], False
    for crit in criteria:
        for attr in crit.get("attributes") or []:
            atype = attr.get("type")
            if atype == "SEARCH_METHOD":
                contextual = True
            elif atype != "MEDIA_TYPE":
                if attr.get("entityId"):
                    entity_ids.append(attr["entityId"])
                if attr.get("value"):
                    terms.append(attr["value"])

    groups: list[dict] = []
    try:
        if entity_ids or terms:
            query = " ".join(terms).strip()
            res = subsonic.search(query, songs=limit, albums=limit, artists=limit) if query else {}

            artists = [
                display_item(f"ar:{a['id']}", "ARTIST", a.get("name", ""), a.get("coverArt"))
                for a in (res.get("artist") or [])[:limit]
            ]
            albums = [
                display_item(f"al:{a['id']}", "ALBUM", a.get("name", ""), a.get("coverArt"),
                             a.get("artist"))
                for a in (res.get("album") or [])[:limit]
            ]
            tracks = [
                display_item(f"tr:{s['id']}", "TRACK", s.get("title", ""), s.get("coverArt"),
                             s.get("artist"))
                for s in (res.get("song") or [])[:limit]
            ]
            for label, items in (("Artists", artists), ("Albums", albums), ("Songs", tracks)):
                if items:
                    groups.append({"metadata": {"name": name_prop(label)},
                                   "contentList": items})
        else:
            # No criteria, or a contextual/recommendation request: offer the
            # library's own shelves.
            playlists = [
                display_item(f"pl:{p['id']}", "PLAYLIST", p.get("name", ""), p.get("coverArt"))
                for p in subsonic.playlists()[:limit]
            ]
            if playlists:
                groups.append({"metadata": {"name": name_prop("Your Playlists")},
                               "contentList": playlists})
            genres = [
                display_item(f"gen:{g['value']}", "GENRE", g.get("value", ""), None)
                for g in subsonic.genres()[:limit]
            ]
            if genres:
                groups.append({"metadata": {"name": name_prop("Genres")},
                               "contentList": genres})
    except Exception:
        logger.exception("displayable content lookup failed")
        return error("Alexa.Media", "CONTENT_NOT_FOUND", "could not build browse results")

    if not groups:
        logger.info("no displayable content (contextual=%s)", contextual)
        return error("Alexa.Media", "CONTENT_NOT_FOUND", "nothing to display")

    return envelope(
        "Alexa.Media.Search",
        "GetDisplayableContent.Response",
        {"contentGroups": groups},
        version="2.0",
    )


ROUTES = {
    ("Alexa.Media.Search", "GetPlayableContent"): handle_get_playable_content,
    ("Alexa.Media.Search", "GetDisplayableContent"): handle_get_displayable_content,
    ("Alexa.Media.Playback", "Initiate"): handle_initiate,
    ("Alexa.Audio.PlayQueue", "GetNextItem"): handle_get_next_item,
    ("Alexa.Audio.PlayQueue", "GetPreviousItem"): handle_get_previous_item,
    ("Alexa.Audio.PlayQueue", "JumpToItem"): handle_jump_to_item,
    ("Alexa.Media.PlayQueue", "GetItem"): handle_get_item,
    ("Alexa.Media.PlayQueue", "GetView"): handle_get_view,
    ("Alexa.Media.PlayQueue", "SetShuffle"): handle_set_shuffle,
    ("Alexa.Media.PlayQueue", "SetLoop"): handle_set_loop,
    ("Alexa.Media.PlayQueue", "SetRepeat"): handle_set_repeat,
    ("Alexa.UserPreference", "ReceiveFeedback"): handle_receive_feedback,
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


# --------------------------------------------------------------------------
# OAuth 2.0 account linking (required before Alexa will use a music skill)
# --------------------------------------------------------------------------

_AUTHORIZE_FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Link Ampere</title>
<style>body{{font-family:system-ui;max-width:22rem;margin:3rem auto;padding:0 1rem}}
input,button{{width:100%;padding:.7rem;font-size:1rem;margin-top:.6rem;box-sizing:border-box}}
</style>
<h2>Link Ampere</h2>
<p>Enter the linking passphrase to connect Alexa to your library.</p>
<form method=post>
  <input type=hidden name=redirect_uri value="{redirect_uri}">
  <input type=hidden name=state value="{state}">
  <input type=hidden name=client_id value="{client_id}">
  <input type=password name=passphrase placeholder="Linking passphrase" autofocus required>
  <button type=submit>Link account</button>
</form>"""


@app.get("/oauth/authorize")
def oauth_authorize_form():
    client_id = request.args.get("client_id", "")
    redirect_uri = request.args.get("redirect_uri", "")
    state = request.args.get("state", "")

    if client_id != oauth.CLIENT_ID:
        return jsonify({"error": "unauthorized_client"}), 400
    if not oauth.redirect_allowed(redirect_uri):
        return jsonify({"error": "invalid_redirect_uri", "got": redirect_uri}), 400

    html = _AUTHORIZE_FORM.format(
        redirect_uri=escape(redirect_uri),
        state=escape(state),
        client_id=escape(client_id),
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/oauth/authorize")
def oauth_authorize_submit():
    redirect_uri = request.form.get("redirect_uri", "")
    state = request.form.get("state", "")
    passphrase = request.form.get("passphrase", "")

    if not oauth.redirect_allowed(redirect_uri):
        return jsonify({"error": "invalid_redirect_uri"}), 400
    if not oauth.LINK_SECRET or not hmac.compare_digest(passphrase, oauth.LINK_SECRET):
        logger.warning("account link rejected: bad passphrase")
        return "Incorrect passphrase.", 403, {"Content-Type": "text/plain"}

    code = oauth.mint("code", oauth.CODE_TTL, sub="grayson")
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}code={code}&state={urllib.parse.quote(state)}"
    logger.info("account linked, redirecting to %s", redirect_uri)
    return Response(status=302, headers={"Location": target})


@app.post("/oauth/token")
def oauth_token():
    form = request.form
    client_id = form.get("client_id") or ""
    client_secret = form.get("client_secret") or ""

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            client_id, _, client_secret = decoded.partition(":")
        except Exception:
            pass

    if client_id != oauth.CLIENT_ID or not hmac.compare_digest(
        client_secret, oauth.CLIENT_SECRET
    ):
        return jsonify({"error": "invalid_client"}), 401

    grant = form.get("grant_type")
    if grant == "authorization_code":
        if not oauth.read(form.get("code", ""), "code"):
            return jsonify({"error": "invalid_grant"}), 400
    elif grant == "refresh_token":
        if not oauth.read(form.get("refresh_token", ""), "refresh"):
            return jsonify({"error": "invalid_grant"}), 400
    else:
        return jsonify({"error": "unsupported_grant_type"}), 400

    logger.info("issued tokens via %s", grant)
    return jsonify(oauth.issue_tokens())


@app.get("/icons/<path:name>")
def icons(name: str):
    """Always send the bytes.

    Amazon's manifest validator (`AlexaSkillManagementAPI/1.0`) re-fetches the
    icons on every update and sends a conditional request. Flask's default
    conditional handling answered 304 with an empty body, which Amazon reported
    as RESOURCE_NOT_FOUND against largeIconUri and failed the whole update.

    `send_from_directory(conditional=False)` is not enough: it still emits an
    ETag, and a validator that echoes it back still gets a 304. The only way to
    guarantee bytes on every request is to send no validators at all. These are
    tens of kilobytes and fetched rarely, so nothing is lost.
    """
    path = (ICON_DIR / name).resolve()
    if not path.is_file() or ICON_DIR.resolve() not in path.parents:
        return Response("not found", status=404, mimetype="text/plain")
    return Response(
        path.read_bytes(),
        mimetype="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
def landing():
    """Human-facing landing page.

    The Alexa app opens the skill's endpoint root in a webview. Serving only
    POST here returns 405 with no body, which the webview renders as a blank
    black screen.
    """
    return (
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Ampere</title>"
        "<style>body{font-family:system-ui;max-width:26rem;margin:3rem auto;padding:0 1rem;"
        "text-align:center}img{width:96px;height:96px}</style>"
        "<img src='/icons/ampere-512.png' alt=''>"
        "<h2>Ampere</h2>"
        "<p>Streams your self-hosted library to Alexa devices.</p>"
        "<p><a href='/privacy'>Privacy</a> &middot; <a href='/terms'>Terms</a></p>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


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


# Off in tests, where Navidrome is mocked and there is nothing to warm from.
if os.environ.get("PREWARM", "1") == "1":
    _WARM_POOL.submit(prewarm_artists)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5056")))
