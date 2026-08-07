"""Music Assistant: an Alexa Music Skill for any Subsonic-compatible music server.

Speaks Amazon's Music/Radio/Podcast Skill API on one side and the Subsonic API
on the other. Nothing here is specific to one server; Navidrome is simply what
it has been tested against. Self-hosted servers are usually not reachable from
the internet, so every asset Amazon fetches (audio, cover art) is proxied
through this service behind a signed, expiring token.

Design note: Alexa echoes {id, queueId, contentId} back on every queue request,
so playback position is carried by Alexa rather than stored here. Queues are
derived from the contentId, which keeps the hot path within Amazon's 100ms p50
budget for Initiate.

## No web framework in this module

This was one Flask app until phase 5 of `ma_provider/PLAN.md`. It is now the
service's whole behaviour with no framework attached, and two adapters serve
it: `app.py` is the standalone Flask deployment, and `ma_provider/webserver.py`
runs the same functions on aiohttp inside Music Assistant's own process.

That split is the point rather than a side effect. Music Assistant is aiohttp,
so a bridge that could only be reached through Flask could never move into it,
and the measurement that made the move worth doing was that of 1,948 lines
here only 64 touched Flask at all, 21 of them inside two envelope helpers.

Every function below therefore takes plain values and returns plain values:
dicts, tuples, byte iterators, file paths. Nothing reads an ambient request
and nothing constructs a response object. An adapter is expected to be dull.

## Everything here is blocking, on purpose

Subsonic calls are `urllib`, and building a station fans out across a
`ThreadPoolExecutor`. Under aiohttp that would stall Music Assistant's event
loop and with it every stream MA is serving, so the async adapter hands each
call to a worker thread rather than awaiting it. Rewriting this module async
would have meant touching all 1,948 lines to buy nothing: the work is genuinely
IO-bound fan-out, which threads already do well.
"""

from __future__ import annotations

import base64
import contextlib
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
from collections.abc import Mapping
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape

from . import access
from . import logring
from . import mastream_cache
from . import mdns
from . import oauth
from . import queue_api
from . import queuestate
from . import setup_state
from . import signature
from . import smapi_rest
from . import handoff
from . import ma_resolve
from . import subsonic
# Re-exported rather than merely used: `core` is what the adapters import, and
# making them reach into two modules for one route's worth of vocabulary would
# be a worse seam than the one line it saves.
from .answers import (  # noqa: F401
    CHUNK, Json, LocalFile, Page, Redirect, Upstream, fetch_upstream, iter_chunks,
)
from . import stream_ref

# Not created at import. Importing a module should not put directories on
# somebody else's disk: inside Music Assistant this ran during MA's startup and
# left `captures/` and `queuestate/` in MA's storage root next to library.db
# and settings.json. `configure()` moves them under a directory of Music Assistant's
# own, and `capture()` creates what it needs when it needs it.
LOG_DIR = pathlib.Path(os.environ.get("CAPTURE_DIR", "/data/captures"))
ICON_DIR = pathlib.Path(os.environ.get("ICON_DIR", "/app/icons"))

# Signs every stream and art URL Amazon holds, every OAuth token the linked
# account carries, and every published queue id.
#
# The random fallback is a development convenience and nothing more. Three
# modules each generated their own, so with no SIGNING_KEY set they did not
# even agree with each other, and all three changed on every restart. In a
# container with an env var that never happened. Inside Music Assistant there
# is no env var, so it would have happened on every restart: Alexa would get a
# 403 partway through a queue it was already playing, the linked account would
# come apart, and a republished queue would be handed a new id.
#
# `set_signing_key` is how the one persisted key reaches all three.
SIGNING_KEY = os.environ.get("SIGNING_KEY", "").encode() or os.urandom(32)


def set_signing_key(key: bytes) -> None:
    """Use one key everywhere that signs something."""
    global SIGNING_KEY
    SIGNING_KEY = key
    oauth.SIGNING_KEY = key
    queue_api.SIGNING_KEY = key


# Guards the admin plane: /captures replays inbound Amazon requests and /diag
# names the music server. Empty means the admin plane is closed, which is the
# right default for a value that is a password.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# The public HTTPS origin Amazon reaches this service on. No default: every
# stream and art URL Amazon fetches is built from it, so a wrong value does not
# fail here, it fails later as audio that will not play, with nothing in any log
# to say why. So it is still required, but it is checked when something is
# about to serve rather than when this module is imported.
#
# Importing used to `raise SystemExit`. That was right while this was a
# process of its own and is actively dangerous now that it is not: inside Music
# Assistant the import happens during MA's startup, so an unset variable took
# down MA itself. Measured on 2026-08-03, when it left MA running with its
# stream server never started, which is a music system that has stopped playing
# because of an Alexa setting.
#
# The environment is the standalone deployment's way of supplying this. Inside
# Music Assistant it comes from the provider's config instead, through
# `configure()` below, which is why this can be empty at import and correct by
# the time anything asks.
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")

# The running Music Assistant instance and its event loop, supplied by
# configure() inside MA and left None in the standalone deployment. When both
# are present and the operator selected catalog providers, the resolver reads
# tracks from MA's library rather than the Subsonic server. The bridge handlers
# run in a worker thread, so the loop is how a resolve reaches an async MA call.
MASS: Any = None
LOOP: Any = None

PUBLIC_BASE_HELP = (
    "PUBLIC_BASE is not set. It must be the https:// origin Amazon can "
    "reach this service on, for example https://music.example.com"
)


def configure(public_base: str = "", storage_path: str = "",
              subsonic_url: str = "", subsonic_user: str = "",
              subsonic_password: str = "", signing_key: str = "",
              admin_token: str = "", oauth_client_id: str = "",
              oauth_client_secret: str = "", oauth_link_secret: str = "",
              after_content: str = "",
              catalog_providers: list[str] | None = None,
              handoff_phrase: str = "",
              mass: Any = None, loop: Any = None) -> None:
    """Supply settings that do not come from the environment.

    Music Assistant holds its own configuration and does not hand providers an
    environment, so every value the standalone deployment reads from an env var
    has to be injectable. Called before anything starts serving.

    `storage_path` moves Music Assistant's state under a directory of its own. Left
    alone, the defaults are absolute paths chosen for a container this service
    owned, and inside Music Assistant they resolve to MA's storage root: on
    2026-08-03 that put `captures/` and `queuestate/` next to MA's library.db
    and settings.json. Nothing broke, which is the problem with it.

    Every argument is skipped when empty rather than written as an empty
    string. Configuring twice with a partial second call is a normal thing for
    a provider reload to do, and it must not blank out what the first call set.
    """
    global PUBLIC_BASE, FALLBACK_ART, LOG_DIR, ADMIN_TOKEN, MASS, LOOP
    if mass is not None:
        MASS = mass
    if loop is not None:
        LOOP = loop
    # The handoff phrase drives both the spoken command and the catalog entity
    # Alexa resolves it against; inside MA there is no env var to carry it, so it
    # is set here from config or the two disagree and the handoff never resolves.
    if handoff_phrase:
        handoff.configure(handoff_phrase)
    if public_base:
        PUBLIC_BASE = public_base.rstrip("/")
        FALLBACK_ART = _fallback_art()

    subsonic.configure(subsonic_url, subsonic_user, subsonic_password)

    if signing_key:
        set_signing_key(signing_key.encode())

    if admin_token:
        ADMIN_TOKEN = admin_token

    # The client id and secret are Music Assistant's own: Amazon learns them from the
    # skill manifest this service writes, so nobody ever types them and they
    # can be generated. The link passphrase is the opposite, and is the one
    # linking value a person reads off the screen and types into the Alexa app.
    if oauth_client_id:
        oauth.CLIENT_ID = oauth_client_id
    if oauth_client_secret:
        oauth.CLIENT_SECRET = oauth_client_secret
    if oauth_link_secret:
        oauth.LINK_SECRET = oauth_link_secret

    if storage_path:
        root = pathlib.Path(storage_path)
        LOG_DIR = root / "captures"
        queuestate.STATE_DIR = root / "queuestate"
        queue_api.STATE_DIR = root / "queuestate" / "external"
        mastream_cache.CACHE_DIR = root / "mastream"
        smapi_rest.set_state_dir(root)
        setup_state.STATE_DIR = root

    # Music Assistant holds the continuation mode as a provider config value,
    # but the code that consults it per queue reads the setup-state file, so the
    # chosen mode is copied across here. Done after the storage_path block above,
    # because that is what points setup_state at MA's storage; writing before it
    # would land the value in a stale location the reader never opens. Normalized
    # through after_content_setting so an impossible value cannot reach the file.
    if after_content:
        setup_state.update(after_content=after_content_setting(after_content))

    # The catalog crawl runs in a worker thread with no provider config, so the
    # chosen source providers are copied into the same setup-state file it reads
    # for the after-content mode. Written whenever a list is supplied (an empty
    # list is a real choice: catalog the Subsonic server the classic way), and
    # skipped only when the argument was omitted, so a partial reconfigure does
    # not blank a saved selection.
    if catalog_providers is not None:
        setup_state.update(
            catalog_providers=[str(p) for p in catalog_providers if p])


def require_public_base() -> None:
    """Refuse to serve without somewhere for Amazon to fetch assets from."""
    if not PUBLIC_BASE:
        raise RuntimeError(PUBLIC_BASE_HELP)


def missing_settings() -> list[str]:
    """What is still unset that serving the endpoint would need.

    Checked here rather than by marking the config entries required, because
    Music Assistant refuses to load a provider whose required entries are
    empty, and `depends_on` does not relax that. Marking them required made an
    unconfigured Alexa endpoint remove every Echo player from MA, which is a
    working feature that does not depend on any of these.

    So MA is told they are optional and the thing that serves decides. The
    provider stays up either way and the log names exactly what is missing.
    """
    missing = []
    if not PUBLIC_BASE:
        missing.append("public base URL")
    if not subsonic.BASE:
        missing.append("music server URL")
    if not subsonic.USER:
        missing.append("music server username")
    return missing

# How long a stream URL stays valid. Amazon defaults to ~60s when validUntil is
# omitted, which is far too short; we set it explicitly and generously.
STREAM_TTL = int(os.environ.get("STREAM_TTL", str(12 * 3600)))

# Where Music Assistant answers, for tracks that have no Subsonic id and are
# streamed back out of MA instead. Held here rather than sent with the queue on
# purpose: the publish endpoint would otherwise be a way to hand the bridge an
# arbitrary URL to fetch, and a proxy that will fetch anything it is told to is
# a much larger thing than a music bridge. Default assumes MA is on the same
# host, which is how it is deployed here (both containers are host-network).
#
# Port 8097, not 8095. Music Assistant runs two webservers: the API and
# frontend on 8095, and a separate streams server on 8097, which is the one
# `mass.streams.register_dynamic_route` registers against. A route registered
# there is not reachable on 8095 at all, and asking 8095 for it returns a bare
# 404 with an empty body, which reads exactly like a route that was never
# registered. Measured 2026-08-03, after believing the latter for a while.
MA_STREAM_BASE = os.environ.get("MA_STREAM_BASE", "http://127.0.0.1:8097").rstrip("/")

logger = logging.getLogger("ma-music-skill")


def install_logging() -> None:
    """Take over the root logger. Only a process that owns one may call this.

    `basicConfig` sets the format and level for every logger in the
    interpreter, and `logring.attach()` adds a handler to the root. That is
    correct for the standalone deployment, whose whole job is this service, and
    plainly wrong for a provider loaded into Music Assistant, where it would
    reformat MA's logs and tee them into Music Assistant's ring buffer.

    It ran at import until 2026-08-03 and did no visible harm only because MA
    configures logging before it loads providers, which makes `basicConfig` a
    no-op. That is luck rather than design, and it would have stopped being
    true the first time load order changed.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logring.attach()  # after basicConfig: a prior root handler would no-op it

_BOOTED = False


def boot_once() -> None:
    """One-time capture hygiene, deferred off import.

    Doing this at import would run it in every process that imports the core
    for a test collection or a CLI, and would slow the first health check
    behind a directory rewrite. Once per process, on the first request, is
    enough, so every adapter calls this on the way in and it returns
    immediately after the first time.
    """
    global _BOOTED
    if _BOOTED:
        return
    _BOOTED = True
    try:
        scrubbed, dropped = scrub_captures(), prune_captures()
        if scrubbed or dropped:
            logger.info("captures: redacted %d, pruned %d", scrubbed, dropped)
    except Exception:
        logger.warning("capture hygiene failed", exc_info=True)


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
# a seed artist with 200 tracks next to neighbors with 10 gives a station that
# is mostly the seed artist again.
RADIO_ARTISTS = int(os.environ.get("RADIO_ARTISTS", "12"))
RADIO_TRACKS_PER_ARTIST = int(os.environ.get("RADIO_TRACKS_PER_ARTIST", "12"))

# Values saved on the stations page, taking effect without a restart. The
# environment values above are the defaults when nothing has been saved. This
# used to be the other way around, and the settings page had to carry a warning
# that its own form did nothing until the operator edited the environment and
# restarted, which is not a settings page.
_SETTINGS_CACHE: dict = {"path": None, "mtime": None, "value": {}}


def _saved_settings() -> dict:
    """The stations settings file, cached on its mtime.

    Read on the hot path (continuation decisions per queue request), so it must
    cost a stat rather than a parse when nothing has changed.

    The path is asked of the module that owns the file rather than rebuilt
    from a directory and a filename. Rebuilding it worked for as long as both
    computations read the same environment variable, and stopped the moment
    Music Assistant supplied a storage path instead: the wizard would write
    settings that this, reading a different path, would never see.
    """
    path = setup_state.path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _SETTINGS_CACHE["path"] == str(path) and _SETTINGS_CACHE["mtime"] == mtime:
        return _SETTINGS_CACHE["value"]
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = {}
    _SETTINGS_CACHE.update(path=str(path), mtime=mtime,
                           value=value if isinstance(value, dict) else {})
    return _SETTINGS_CACHE["value"]


def effective_after_content() -> str:
    saved = str(_saved_settings().get("after_content") or "").strip().lower()
    return saved if saved in AFTER_CONTENT_MODES else AFTER_CONTENT


def _effective_int(key: str, fallback: int) -> int:
    try:
        value = int(_saved_settings().get(key))
    except (TypeError, ValueError):
        return fallback
    return max(1, min(100, value))


def effective_radio_artists() -> int:
    return _effective_int("radio_artists", RADIO_ARTISTS)


def effective_radio_tracks_per_artist() -> int:
    return _effective_int("radio_tracks_per_artist", RADIO_TRACKS_PER_ARTIST)

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
    # Stamp the album name from the album we asked for. An untagged file comes
    # back with no album field of its own, and an Item without metadata.album
    # is not shown as blank in the Alexa app: Alexa fills the slot with the
    # provider name instead, so the card reads "Music Assistant" where the album should
    # be. Seen live on two copies of Saint Valentine, one tagged and one not.
    return [
        track if track.get("album") else {**track, "album": album.get("name", "")}
        for album, tracks in zip(albums, results)
        for track in tracks
    ]


def similar_artist_ids(seed_id: str) -> list[str]:
    """The seed artist, then the library artists most like them."""
    if seed_id in _RADIO_CACHE:
        _RADIO_CACHE.move_to_end(seed_id)
        return _RADIO_CACHE[seed_id]

    ids = [seed_id]
    try:
        for artist in subsonic.similar_artists(seed_id, effective_radio_artists()):
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
    """A capped, stable slice of one artist's catalog.

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
        for track in artist_sample(artist_id, tracks,
                                   effective_radio_tracks_per_artist())
    ]


def _ma_mode() -> bool:
    """Whether the id-less whole-collection intents resolve out of MA.

    A marked content id (``ma-...``) is self-describing and routes to MA on its
    own. But `star` and `rnd:all` carry no id to mark, so their source is
    decided here: MA is running and the operator picked catalog providers, which
    is exactly the state in which their library lives in MA rather than on the
    Subsonic server.
    """
    if MASS is None or LOOP is None:
        return False
    return bool(setup_state.load().get("catalog_providers"))


def resolve_tracks(content_id: str) -> list[dict]:
    """contentId -> ordered list of song records, from Subsonic or Music Assistant.

    Navidrome returns full song records from album/playlist/genre listings, so
    they are cached whole. An earlier version kept only the ids and re-fetched
    each song with getSong on every item, which cost one round trip per track
    and pushed Initiate past 3 seconds against Amazon's 400ms p99 budget.

    A content id whose native id carries the MA marker (see
    `stream_ref.MA_ID_PREFIX`) is resolved out of Music Assistant's library
    instead; the records are the same shape but name their audio with an
    `ma_ref` that `build_item` streams through the MA route.
    """
    if content_id in _QUEUE_CACHE:
        _QUEUE_CACHE.move_to_end(content_id)
        return _QUEUE_CACHE[content_id]

    kind, _, ident = content_id.partition(":")
    # A marked id always names a Music Assistant library item, whatever the
    # deployment; the marker is authoritative and needs no other state. MASS is
    # only ever unset in the standalone deployment, which never emits a marked
    # id, so the guard is belt-and-braces.
    ma = ma_resolve.is_ma_native(ident) and MASS is not None and LOOP is not None
    cacheable = True
    try:
        if kind == "tr":
            songs = (ma_resolve.resolve("tr", ident, MASS, LOOP) if ma
                     else [subsonic.song(ident)])
        elif kind == "al":
            songs = (ma_resolve.resolve("al", ident, MASS, LOOP) if ma
                     else subsonic.album_tracks(ident))
        elif kind == "ar":
            songs = (ma_resolve.resolve("ar", ident, MASS, LOOP) if ma
                     else artist_tracks(ident))
        elif kind == "rad":
            if ma:
                # A marked seed resolves to the seed artist's own tracks. Provider-
                # agnostic similar-tracks radio is parked on a branch; this keeps
                # after-content 'radio' mode playing something coherent for an MA
                # track without the lyrics-heavy similar lookup.
                songs = ma_resolve.resolve("ar", ident, MASS, LOOP)
            else:
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
            songs = (ma_resolve.resolve("pl", ident, MASS, LOOP) if ma
                     else subsonic.playlist_tracks(ident))
        elif kind == "gen":
            # Deterministic, unlike random_songs(genre=...). The queue is
            # re-derived from the contentId on every GetNextItem, so a random
            # listing would reshuffle mid-playback.
            songs = (ma_resolve.resolve("gen", ident, MASS, LOOP) if ma
                     else subsonic.songs_by_genre(ident))
        elif kind == "star":
            songs = (ma_resolve.favorites(MASS, LOOP) if _ma_mode()
                     else subsonic.starred_songs())
        elif kind == "ext":
            # A queue Music Assistant composed and handed over, which has no
            # Subsonic identity of its own. An unknown or expired token is an
            # empty answer that must not be cached, or the queue would stay
            # empty for the life of the process after one late lookup.
            songs = queue_api.resolve(ident)
            cacheable = bool(songs)
        else:
            # rnd:all and any unrecognised id: a shuffle of the whole library.
            songs = (ma_resolve.library_sample(MASS, LOOP) if _ma_mode()
                     else subsonic.random_songs(50))
    except Exception:
        logger.exception("resolve_tracks failed for %s", content_id)
        songs = []

    songs = [s for s in songs if s and s.get("id")]
    if cacheable:
        _QUEUE_CACHE[content_id] = songs
        while len(_QUEUE_CACHE) > _QUEUE_CACHE_MAX:
            _QUEUE_CACHE.popitem(last=False)
    return songs


def forget_content(content_id: str) -> None:
    """Drop one contentId's cached track list so its next resolve re-reads source.

    resolve_tracks caches the list the first time a contentId resolves and never
    re-derives an entry it already holds, which is what keeps GetNextItem inside
    Amazon's budget. The cost is that a contentId whose underlying track list is
    mutated in place -- an `ext:` queue grown by queue_api.append_to_queue --
    goes on being served the list cached at its first resolve. Any writer that
    changes what a contentId resolves to has to call this, or the change is
    masked for the life of the process. No-op for a contentId never cached.
    """
    _QUEUE_CACHE.pop(content_id, None)


def match_playlist(query: str) -> dict | None:
    """Find a playlist by spoken name.

    Subsonic's search3 does not cover playlists, so this matches against the
    playlist list directly. Speech tends to append the word "playlist", and
    casing is never reliable, so both are normalized away before comparing.
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


# The whole spoken phrase, once it has been reduced to bare words, that means
# "the whole library" or "the songs I have favorited". These never resolve
# against the uploaded catalog (there is no artist called "everything"), so
# without this a request to shuffle the library falls through to a free-text
# search and matches whichever track happens to be titled closest to the words.
# The native content ids they map to are the same ones handle_initiate and
# resolve_tracks already special-case: `rnd:all` is random_songs, `star` is the
# starred list, and both shuffle by default.
_LIBRARY_PHRASES = frozenset({
    "my library", "my music", "all my music", "all my songs",
    "my whole library", "my entire library", "everything", "shuffle",
})
_FAVORITES_PHRASES = frozenset({
    "my favorites", "favorites", "my favourites", "favourites",
    "starred", "starred songs", "my starred songs",
    "liked songs", "my liked songs", "songs i like", "songs i love",
})


def _library_intent(spoken: str) -> str | None:
    """Map a whole-library or favorites phrase to its native content id.

    Returns `rnd:all` for the library, `star` for favorites, or None when the
    words are a real title to search for. Normalized the way match_playlist
    normalizes: casing is never reliable off speech, and punctuation ("songs I
    like." with a trailing period) arrives attached, so both are stripped
    before comparing against the fixed phrase sets.

    A leading "play"/"shuffle" is the control verb Alexa did not strip, not part
    of the name, so it is removed before the lookup - except "shuffle" entirely
    on its own, which is itself the request to shuffle the library.
    """
    norm = re.sub(r"[^\w\s]", "", spoken or "").strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return None
    if norm == "shuffle":
        return "rnd:all"
    norm = re.sub(r"^(?:play|shuffle)\s+", "", norm)
    if norm in _FAVORITES_PHRASES:
        return "star"
    if norm in _LIBRARY_PHRASES:
        return "rnd:all"
    return None


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
#
# Built by a function rather than written once, because the origin it embeds
# may arrive after import when Music Assistant supplies it from provider
# config. `configure()` rebuilds this; baking the empty value in at import
# would put relative URLs in front of Amazon, which is the failure this
# fallback exists to prevent.
def _fallback_art() -> list[dict]:
    return [
        {"url": f"{PUBLIC_BASE}/icons/ma-512.png",
         "size": "X_LARGE", "widthPixels": 512, "heightPixels": 512},
        {"url": f"{PUBLIC_BASE}/icons/ma-108.png",
         "size": "SMALL", "widthPixels": 108, "heightPixels": 108},
    ]


FALLBACK_ART = _fallback_art()


def art_block(cover_id: str | None, art_url: str = "") -> dict:
    """Where Alexa should fetch this track's cover from.

    `art_url` short-circuits the proxy. A Music Assistant track from Spotify or
    Tidal already has its artwork on a public CDN, so Amazon fetches it from
    there directly: one less hop, and none of that image ever crosses this
    service. Navidrome's art is on the tailnet and has to be proxied, which is
    what cover_id is for.
    """
    if art_url:
        return {
            "sources": [
                {"url": art_url, "size": "X_LARGE",
                 "widthPixels": 600, "heightPixels": 600}
            ]
        }
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
    not offer, and a station whose skip button grays out on track twelve is
    not a station.
    """
    # Two kinds of track can be in one queue. A Music Assistant track has no
    # Subsonic id and is fetched back out of MA; everything else comes from the
    # music server as it always has.
    ma_ref = song.get("ma_ref") or ""
    # A station, as opposed to a track: endless, so no length, nothing to seek
    # into, and nothing to buffer. Read off the reference rather than carried
    # beside it, because the media type is already part of a Music Assistant
    # uri and reading it there means a published queue cannot misdescribe
    # itself.
    live = bool(ma_ref) and stream_ref.is_live(ma_ref)
    if ma_ref:
        uri, expires = signed_url("mastream", ma_ref)
    else:
        uri, expires = signed_url("stream", song["id"])
    # A Music Assistant track's cover is not on a public host either, so when it
    # carries an imageproxy id rather than a ready art url, the same bridge that
    # serves its audio serves the image (core.serve_maart). Subsonic covers keep
    # their own path through art_block.
    art_url = song.get("art_url", "")
    if not art_url and song.get("art_id"):
        art_url, _ = signed_url("maart", song["art_id"])
    item = {
        "id": f"{index}",
        "playbackInfo": {"type": "DEFAULT"},
        "metadata": {
            "type": "TRACK",
            "name": name_prop(song.get("title", "Unknown")),
            "art": art_block(song.get("coverArt"), art_url),
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
            # A Music Assistant track can be scrubbed only when it is being
            # buffered here, because MA's own audio is realtime and answers no
            # range. With the buffer on, the ranged GET that follows a scrub
            # lands on a complete file and is answered exactly, so the control
            # is offered; with it off it is greyed out, because a control that
            # is offered and then misbehaves is worse than one that is not.
            # Never on a station: there is no position in an endless stream.
            {"type": "ADJUST", "name": "SEEK_POSITION",
             "enabled": bool(song.get("duration")) and not live
             and (not ma_ref or mastream_cache.ENABLED)},
        ],
        "rules": {"feedbackEnabled": False},
        "stream": {
            "id": f"s-{song['id']}",
            "uri": uri,
            "offsetInMilliseconds": 0,
            "validUntil": iso(expires),
        },
    }
    # A station has no length, and claiming one would put a progress bar on
    # something that never reaches its end.
    if song.get("duration") and not live:
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
    whatever AFTER_CONTENT says.

    An `ext:` queue stops, whatever the setting says, because it is Music
    Assistant's queue and MA decides what follows it. Continuing one here means
    Alexa plays tracks MA never queued, which it cannot recognise as its own:
    MA follows Alexa by matching the reported title against its queue items, so
    the moment playback leaves that list MA's idea of the current track freezes
    on the last one it knew. Measured 2026-08-02 with AFTER_CONTENT=radio, a
    one track MA queue, and a display stuck on that track while the speakers
    played station fill. MA has its own endless-play feature for anyone who
    wants one.

    Everything else obeys the setting, which is off unless it has been turned
    on.
    """
    kind = content_id.partition(":")[0]
    if kind == "rad":
        return "radio"
    if kind == queue_api.CONTENT_PREFIX:
        return "stop"
    return effective_after_content()


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


def start_offset(content_id: str) -> int:
    """Milliseconds into its first track this content should begin at.

    Only an `ext:` queue can carry one, because only Music Assistant publishes
    a queue with a position attached. Everything else starts where it starts.
    """
    kind, _, ident = (content_id or "").partition(":")
    if kind != queue_api.CONTENT_PREFIX:
        return 0
    return queue_api.start_offset_ms(ident)


def item_at(content_id: str, index: int, queue_id: str = "") -> dict | None:
    song = song_at(content_id, index, queue_id)
    if song is None:
        return None
    songs = queue_order(content_id, queue_id)

    # Alexa asks about an item before it plays it, so this is the read-ahead:
    # by the time it comes back for the audio the buffer is usually already
    # complete, and a scrub or a room change lands on a real file. Started from
    # this item rather than from the top, because the tracks worth having are
    # the ones about to play.
    mastream_cache.prefetch(
        [s["ma_ref"] for s in songs[index:] if s.get("ma_ref")]
    )
    return build_item(song, index, len(songs),
                      continuation_mode(content_id) != "stop")


# --------------------------------------------------------------------------
# envelope helpers
# --------------------------------------------------------------------------


def envelope(namespace: str, name: str, payload: dict, version: str = "1.0") -> dict:
    """A directive response, as the dict that goes on the wire.

    Plain dict rather than a response object: this is the one place every
    handler funnels through, so keeping a framework out of it is what keeps a
    framework out of all of them. It is also what lets `dispatch` record the
    answer in a capture without serializing and re-parsing it, which is what
    the Flask version had to do.
    """
    return {
        "header": {
            "messageId": str(uuid.uuid4()),
            "namespace": namespace,
            "name": name,
            "payloadVersion": version,
        },
        "payload": payload,
    }


def error(namespace: str, err_type: str, message: str, version: str = "1.0") -> dict:
    logger.warning("error %s/%s: %s", namespace, err_type, message)
    return {
        "header": {
            "messageId": str(uuid.uuid4()),
            "namespace": namespace,
            "name": "ErrorResponse",
            "payloadVersion": version,
        },
        "payload": {"type": err_type, "message": message},
    }


# Keys whose values are credentials rather than telemetry. Every music
# directive carries the linked account's OAuth token and an Alexa API token,
# and a capture is written to disk in the clear and rendered into the Logs
# screen, so the value must never be stored. The key itself stays, replaced
# with a marker: "was this request signed" is read off the presence of the
# signature headers, and a redaction that removed them would break that.
# SignatureCertChainUrl is deliberately absent, being a public URL that is
# genuinely useful when verification is being debugged.
SECRET_KEYS = frozenset({
    "accesstoken", "apiaccesstoken", "authorization", "refreshtoken",
    "bearertoken", "signature", "signature-256",
})
REDACTED = "<redacted>"

# How many captures to keep. Nothing bounded this directory before: one file
# per directive, forever. It is read by the status panel on a ten second poll
# and scanned by the scheduler every minute, so unbounded growth was a slow
# leak in disk and in both of those hot paths.
CAPTURE_KEEP = int(os.environ.get("CAPTURE_KEEP", "400"))

_CAPTURES_WRITTEN = 0
_PRUNE_EVERY = 20


def redact(value):
    """Strip credential values out of a capture, at any depth."""
    if isinstance(value, dict):
        return {
            key: REDACTED if key.lower() in SECRET_KEYS else redact(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def prune_captures(keep: int | None = None) -> int:
    """Delete the oldest captures past the cap. Returns how many went.

    Filenames lead with a UTC timestamp, so lexical order is chronological
    order and no stat call is needed to sort them.
    """
    limit = CAPTURE_KEEP if keep is None else keep
    if limit <= 0:
        return 0
    try:
        names = sorted(p.name for p in LOG_DIR.glob("*.json"))
    except OSError:
        return 0
    dropped = 0
    for name in names[:max(0, len(names) - limit)]:
        try:
            (LOG_DIR / name).unlink()
            dropped += 1
        except OSError:
            pass
    return dropped


def scrub_captures() -> int:
    """Redact credentials out of captures written before redaction existed.

    Runs once at boot. Without it the tokens already on disk would sit there
    until the prune cap eventually walked past them, which on a quiet install
    could be months.
    """
    changed = 0
    try:
        paths = list(LOG_DIR.glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            raw = path.read_text()
            if not any(key in raw.lower() for key in SECRET_KEYS):
                continue
            body = json.loads(raw)
        except (OSError, ValueError):
            continue
        cleaned = redact(body)
        if cleaned == body:
            continue
        try:
            path.write_text(json.dumps(cleaned, indent=2))
            changed += 1
        except OSError:
            pass
    return changed


def capture(payload: object, kind: str) -> None:
    """Record a directive and the answer we gave it. Never fatal.

    This runs after the response has already been built. A failure here, and
    a full disk is the realistic one, used to escape into the request handler
    and turn a correct answer into a 500, so losing telemetry took playback
    down with it. Telemetry is never worth a directive.
    """
    global _CAPTURES_WRITTEN
    try:
        # Created here rather than at import. The `exist_ok` path is a stat, so
        # it costs nothing on every call after the first, and it means nothing
        # makes a directory until there is genuinely something to put in it.
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        (LOG_DIR / f"{stamp}-{kind}.json").write_text(
            json.dumps(redact(payload), indent=2))
        _CAPTURES_WRITTEN += 1
        if _CAPTURES_WRITTEN % _PRUNE_EVERY == 0:
            prune_captures()
    except Exception:
        logger.warning("capture failed for %s", kind, exc_info=True)


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
    # A Music Assistant native id has no Subsonic record. Its name lives in MA's
    # library, so look it up there instead. Done synchronously rather than warmed
    # in the background like the Subsonic path, because Alexa often resolves a
    # playlist or artist by entity alone with no spoken value to fall back on, so
    # the name has to be in the very first response or Alexa announces "ma-59".
    # A library read is a local query, not the 300-700ms Subsonic round trip the
    # background warming exists to keep off this path.
    if ma_resolve.is_ma_native(native):
        key = f"{prefix}:{native}"
        if key in _DESCRIBE_CACHE:
            _DESCRIBE_CACHE.move_to_end(key)
            return _DESCRIBE_CACHE[key]
        result = (None, None)
        if MASS is not None and LOOP is not None:
            result = ma_resolve.describe(
                prefix, ma_resolve.library_id(native), MASS, LOOP)
        if result != (None, None):
            _DESCRIBE_CACHE[key] = result
            while len(_DESCRIBE_CACHE) > _DESCRIBE_CACHE_MAX:
                _DESCRIBE_CACHE.popitem(last=False)
        return result

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
            # song() answers None for a track it cannot find rather than
            # raising, so a miss here is ordinary and must not be handled as a
            # traceback per lookup.
            song = subsonic.song(native)
            if not song:
                return None, None
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


def station_content(artist_id: str, spoken: str | None) -> dict:
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


def handle_get_playable_content(payload: dict) -> dict:
    attrs = (payload.get("selectionCriteria") or {}).get("attributes") or []
    station, attrs = station_request(attrs)

    # Checked ahead of the catalog, because a real playlist that happens to
    # share the handoff phrase would otherwise win and Music Assistant would
    # silently play something else. Resolved to the concrete token of the
    # newest published queue rather than to a moving alias: Alexa echoes this
    # contentId back for the life of the queue, so an alias would swap the
    # music underneath a session that was already playing.
    spoken = " ".join(
        str(a.get("value")) for a in attrs if a.get("value")
    ).strip()
    by_entity = any(
        queue_api.is_handoff_entity(str(a.get("entityId") or "")) for a in attrs
    )
    if by_entity or queue_api.is_handoff_phrase(spoken):
        content_id = queue_api.handoff_content_id()
        if not content_id:
            return error("Alexa.Media", "CONTENT_NOT_FOUND",
                         "nothing has been handed over yet")
        # Recorded before answering, because this is the only evidence that
        # Amazon acted on the command rather than merely accepting it, and
        # `play_media` is waiting on exactly that.
        queue_api.note_handoff_claimed()
        logger.info("handoff phrase resolved to %s", content_id)
        return envelope(
            "Alexa.Media.Search",
            "GetPlayableContent.Response",
            {
                "content": {
                    "id": content_id,
                    "metadata": {
                        "type": "PLAYLIST",
                        "name": name_prop(queue_api.handoff_name()),
                        "art": art_block(None),
                    },
                }
            },
        )

    # When Alexa resolves speech against our uploaded catalog it returns the
    # catalog's own entity id (e.g. "artist.<navidrome id>") rather than free
    # text. That is a direct hit: map it straight to a contentId, no search.
    for attr in attrs:
        entity_id = attr.get("entityId") or ""
        kind, _, native = entity_id.partition(".")

        # A resolved *station* entity is its own signal, and the only one that
        # survives resolution. "play <artist> radio" never reaches station_request
        # with the word intact once Alexa matches it against the catalog: the word
        # is what Alexa used to pick the entity, so it is spent by the time the
        # attribute arrives, and MEDIA_TYPE STATION has never shown up from this
        # skill. Step 2 of the stations plan (emit a "<name> Radio" entity per
        # artist in catalog_sync, keyed by the artist id) exists precisely so the
        # phrase resolves to one of these instead of to the bare artist. When it
        # does, the entity *type* carries the intent that the free text no longer
        # can, so map it straight to rad:<artist id> -- what station_content
        # builds -- with no dependence on a word that is already gone.
        if kind in ("station", "radio") and native:
            # The catalog display name is "<name> Radio"; strip the trailing word
            # so station_content does not render "<name> Radio Radio".
            spoken = _STATION_WORD.sub("", attr.get("value") or "").strip() or None
            return station_content(native, spoken)

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

    # Alexa tells us what kind of thing the user asked for. Honor it: asking
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
    # labeled the request as something else.
    if station:
        preference = ["artist", "album", "song"]

    # "shuffle my music", "my favorites", "everything": a whole-collection
    # phrase that reached this far did not resolve against the catalog (there is
    # no artist named "everything"), so route it to the native library or
    # favorites content id instead of searching for the closest-titled track. A
    # real artist, album or playlist already won above in the catalog-entity
    # loop, and stations seed from a named artist rather than the library, so a
    # "... station" request is deliberately excluded.
    if not station and (native := _library_intent(query)):
        return envelope(
            "Alexa.Media.Search",
            "GetPlayableContent.Response",
            {
                "content": {
                    "id": native,
                    "metadata": {
                        "type": "PLAYLIST",
                        "name": name_prop(
                            "My Favorites" if native == "star" else "My Library"
                        ),
                        "art": art_block(None),
                    },
                }
            },
        )

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


def generic_response(payload_version: str = "3.0") -> dict:
    """The empty Alexa.Response event that acknowledges Set* directives.

    Amazon documents the payload as empty and gives no state echo. The only
    literal example they publish carries payloadVersion 3.0 even though the
    PlayQueue directives themselves are 1.0, and mirroring 1.0 instead left the
    Alexa app never registering the new shuffle/loop state.
    """
    return envelope("Alexa", "Response", {}, version=payload_version)


# Asking for an artist and getting the first track of their earliest album,
# every single time, is not what anyone means. Collections shuffle by default;
# albums keep their order, because their order is the point. Playlists sit in
# between (some are ordered mixtapes, some are just buckets), so theirs is a
# saved setting, off unless turned on.
#
# This has to be a default rather than a reading of the request: Alexa sends
# `shuffle: false` on every fresh Initiate, so "the user turned it off" and
# "nothing was specified" are indistinguishable here. The queue state we return
# reports shuffle as on, so the app shows it on and SetShuffle can still turn it
# off for album order.
SHUFFLE_BY_DEFAULT = {"ar", "gen", "star", "rnd", "rad"}


def shuffle_by_default(content_id: str) -> bool:
    prefix = content_id.partition(":")[0]
    if prefix == "pl":
        return bool(_saved_settings().get("shuffle_playlists"))
    return prefix in SHUFFLE_BY_DEFAULT


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


def handle_initiate(payload: dict) -> dict:
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

    # A seek arrives as a fresh Initiate, because Music Assistant implements
    # seek by re-issuing play_media on the current item. Without this the queue
    # is republished and Alexa starts it from zero, so dragging the scrubber
    # restarted the song while MA's own clock showed the position dragged to.
    # Only the first item carries it: everything after it is a normal
    # transition and starts at its own beginning.
    if (offset := start_offset(content_id)) and first.get("stream"):
        first["stream"]["offsetInMilliseconds"] = offset
        logger.info("starting %s at %dms", content_id, offset)

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
    """Normalize the two reference shapes Amazon uses.

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


def handle_get_next_item(payload: dict) -> dict:
    content_id, index, queue_id = _locate(payload)
    state = queuestate.get(queue_id)

    nxt_index = index + 1
    if state.get("loop"):
        # Only loop needs to know where the end is, and item_at resolves the
        # queue again anyway. Asking for it unconditionally meant every single
        # GetNextItem resolved the queue twice, which on a cold cache is two
        # discography walks against Amazon's 400ms p99 budget for a track
        # transition. Alexa gives up quietly when that budget is missed and the
        # queue simply stops.
        total = len(queue_order(content_id, queue_id))
        if total and nxt_index >= total:
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


def handle_get_previous_item(payload: dict) -> dict:
    content_id, index, queue_id = _locate(payload)
    state = queuestate.get(queue_id)

    prev_index = index - 1
    if prev_index < 0 and state.get("loop"):
        prev_index = len(queue_order(content_id, queue_id)) - 1

    prev = item_at(content_id, prev_index, queue_id)
    if prev is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "start of queue")
    return envelope("Alexa.Audio.PlayQueue", "GetPreviousItem.Response", {"item": prev})


def handle_get_item(payload: dict) -> dict:
    content_id, index, queue_id = _locate(payload, "targetItemReference")
    item = item_at(content_id, index, queue_id)
    if item is None:
        return error("Alexa.Media", "ITEM_NOT_FOUND", "item unavailable")
    # Response namespace is Alexa.Audio.PlayQueue even though the directive
    # arrives on Alexa.Media.PlayQueue. This matches Amazon's own examples.
    return envelope("Alexa.Audio.PlayQueue", "GetItem.Response", {"item": item})


def handle_set_shuffle(payload: dict) -> dict:
    _content, _index, queue_id = _locate(payload)
    queuestate.update(queue_id, shuffle=bool(payload.get("enable")))
    logger.info("shuffle=%s on queue %s", payload.get("enable"), queue_id[:8])
    return generic_response()


def handle_set_loop(payload: dict) -> dict:
    _content, _index, queue_id = _locate(payload)
    queuestate.update(queue_id, loop=bool(payload.get("enable")))
    logger.info("loop=%s on queue %s", payload.get("enable"), queue_id[:8])
    return generic_response()


def handle_set_repeat(payload: dict) -> dict:
    _content, _index, queue_id = _locate(payload)
    status = (payload.get("mode") or {}).get("status", "OFF")
    queuestate.update(queue_id, repeat=status)
    logger.info("repeat=%s on queue %s", status, queue_id[:8])
    return generic_response()


def handle_get_view(payload: dict) -> dict:
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


def handle_jump_to_item(payload: dict) -> dict:
    content_id, _index, queue_id = _locate(payload)
    try:
        target = int(payload.get("targetItemId", ""))
    except (TypeError, ValueError):
        return error("Alexa.Media", "INVALID_ITEM", "unrecognized target item")

    item = item_at(content_id, target, queue_id)
    if item is None:
        return error("Alexa.Media", "INVALID_ITEM", "The requested item is no longer valid.")
    return envelope("Alexa.Audio.PlayQueue", "JumpToItem.Response", {"item": item})


def handle_receive_feedback(payload: dict) -> dict:
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


def handle_get_displayable_content(payload: dict) -> dict:
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





# --------------------------------------------------------------------------
# the directive endpoint
# --------------------------------------------------------------------------


def dispatch(body: object, headers: Mapping[str, str],
             raw_body: bytes = b"") -> Json:
    """Handle one Alexa directive, from parsed body to answer.

    This is `POST /music`, minus the framework. `headers` and `raw_body` are
    passed in rather than read off an ambient request object, which is also
    what lets signature verification work under both adapters: it already
    accepted them explicitly and only fell back to Flask's globals when they
    were absent.
    """
    boot_once()

    # Off by default (VERIFY_REQUESTS=warn), because switching it on breaks a
    # working deployment silently: a rejected directive is indistinguishable
    # from a skill that was never called, and a music skill cannot be simulated
    # to check first. Watch a day of real traffic pass in the log, then set on.
    allow, why = signature.check_request(headers=headers, body=raw_body)
    if not allow:
        logger.warning("rejected unverified request: %s", why)
        return Json(403, error("Alexa", "INVALID_DIRECTIVE",
                               f"unverified request: {why}", "3.0"))

    if not isinstance(body, dict):
        return Json(400, error("Alexa", "INVALID_DIRECTIVE",
                               "expected JSON body", "3.0"))

    header = body.get("header") or {}
    namespace, name = header.get("namespace", "?"), header.get("name", "?")
    payload = body.get("payload") or {}

    record = {"headers": dict(headers), "body": body}
    kind = f"{namespace}.{name}"

    handler = ROUTES.get((namespace, name))
    if handler is None:
        logger.warning("unhandled directive %s.%s", namespace, name)
        capture(record, kind)
        return Json(200, error("Alexa", "INVALID_DIRECTIVE",
                               f"unhandled {namespace}.{name}", "3.0"))

    started = time.monotonic()
    try:
        answer = handler(payload)
    except Exception:
        logger.exception("handler %s.%s failed", namespace, name)
        record["response"] = {"error": "handler failed"}
        capture(record, kind)
        return Json(200, error("Alexa", "INTERNAL_ERROR", "handler failed", "3.0"))

    # Store what we answered next to what arrived: "did the request route all
    # the way through" is unanswerable from the request alone. Handlers now
    # return the dict itself, so this no longer has to serialize the response
    # and parse it back to find out what it said.
    record["response"] = answer
    capture(record, kind)
    logger.info("%s.%s served in %dms", namespace, name,
                (time.monotonic() - started) * 1000)
    return Json(200, answer)


# --------------------------------------------------------------------------
# asset proxies (Navidrome is tailnet-only; Amazon fetches these)
# --------------------------------------------------------------------------


def serve_stream(song_id: str, expires: int, sig: str,
                 range_header: str | None = None) -> Json | Upstream:
    if not verify("stream", song_id, expires, sig):
        return Json(403, {"error": "bad or expired signature"})
    return fetch_upstream(subsonic.stream_url(song_id), range_header, "audio/mpeg")


def serve_mastream(ref: str, expires: int, sig: str,
                   range_header: str | None = None) -> Json | LocalFile | Upstream:
    """Audio for a track that lives in Music Assistant rather than Subsonic.

    The ref is the item's MA uri, base64url encoded. It is turned into a URL
    only here, against a base this service holds in its own config, so a
    published queue can name an item but can never name a host.

    Served from the buffer when there is one, which is what makes these tracks
    seekable and lets them survive being moved between rooms: a `LocalFile`
    answer is sent by the adapter with range support, so a scrub gets a real
    `206` and `Content-Range` exactly as the Navidrome proxy already does.

    Falling back to streaming from Music Assistant is deliberate rather than
    defensive. A full disk, a slow fetch or a disabled cache should cost
    seeking, which is what phase 2 shipped without, and never cost playback.
    """
    if not verify("mastream", ref, expires, sig):
        return Json(403, {"error": "bad or expired signature"})
    if not stream_ref.is_ref(ref):
        return Json(400, {"error": "not a Music Assistant reference"})

    # A plain fetch of a track that is not buffered yet is streamed straight
    # through, and the buffer is filled behind it. Waiting would be correct and
    # is what a range request does, but a first fetch does not need a range: it
    # needs audio now. Measured, the head of a queue is normally already
    # buffered because publishing runs ahead of the utterance, and this covers
    # the case where it is not without putting five seconds of silence in front
    # of the first track.
    #
    # A ranged request does wait. That is the scrub and the room-to-room move,
    # where an answer from byte zero is not a slow answer but a wrong one.
    buffered = mastream_cache.path_for(ref)
    if range_header or buffered.exists():
        if complete := mastream_cache.ensure(ref):
            return LocalFile(complete, "audio/mpeg")
        if range_header:
            logger.warning(
                "Alexa sent %s for a Music Assistant track that could not be "
                "buffered; serving from the beginning instead", range_header,
            )
    else:
        mastream_cache.prefetch([ref])

    return fetch_upstream(f"{MA_STREAM_BASE}{stream_ref.stream_path(ref)}",
                          range_header, "audio/mpeg")


def serve_art(cover_id: str, expires: int, sig: str,
              range_header: str | None = None) -> Json | Upstream:
    if not verify("art", cover_id, expires, sig):
        return Json(403, {"error": "bad or expired signature"})
    return fetch_upstream(subsonic.cover_art_url(cover_id), range_header, "image/jpeg")


def serve_maart(image_id: str, expires: int, sig: str,
                range_header: str | None = None) -> Json | Upstream:
    """Cover art for a Music Assistant track, proxied like its audio.

    The id is Music Assistant's own opaque imageproxy id (see
    ma_resolve.song_from_track), so this proxies MA's streamserver the same way
    serve_art proxies Navidrome: the image lives on the tailnet and Amazon
    fetches it here, through the one public endpoint it already talks to.
    """
    if not verify("maart", image_id, expires, sig):
        return Json(403, {"error": "bad or expired signature"})
    return fetch_upstream(
        f"{MA_STREAM_BASE}/imageproxy/{image_id}?size=600&fmt=jpeg",
        range_header, "image/jpeg")


# --------------------------------------------------------------------------
# ops
# --------------------------------------------------------------------------


def admin_authorized(remote_addr: str | None, headers: Mapping[str, str]) -> bool:
    """Token, and only from a network setup is allowed on.

    /captures replays inbound Amazon requests and /diag names the music server,
    so they belong to the admin plane even though they live on the port Amazon
    calls. Amazon never asks for either, so nothing is lost by refusing them
    from off the LAN. Same rule and same variables as the wizard.
    """
    expected = ADMIN_TOKEN
    if not expected or headers.get("X-Admin-Token") != expected:
        return False
    forwarded = headers.get("X-Forwarded-For")
    if access.forwarded_untrusted(remote_addr, forwarded):
        # A proxy on the same host makes a public request look like loopback.
        return False
    return access.address_allowed(access.client_ip(remote_addr, forwarded))


def healthz() -> Json:
    return Json(200, {"ok": True})


def captures_listing() -> Json:
    return Json(200, [
        {"file": p.name, "content": json.loads(p.read_text())}
        for p in sorted(LOG_DIR.glob("*.json"))
    ])


def diagnostics(query: str = "the") -> Json:
    try:
        res = subsonic.search(query, songs=3)
    except Exception as exc:
        return Json(502, {"subsonic": "error", "detail": str(exc)})
    return Json(200, {
        "subsonic": "ok",
        "songs": [
            {"id": s["id"], "title": s.get("title"), "artist": s.get("artist")}
            for s in res.get("song", [])
        ],
    })


# --------------------------------------------------------------------------
# OAuth 2.0 account linking (required before Alexa will use a music skill)
# --------------------------------------------------------------------------

_AUTHORIZE_FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Link Music Assistant</title>
<style>body{{font-family:system-ui;max-width:22rem;margin:3rem auto;padding:0 1rem}}
input,button{{width:100%;padding:.7rem;font-size:1rem;margin-top:.6rem;box-sizing:border-box}}
</style>
<h2>Link Music Assistant</h2>
<p>Enter the linking passphrase to connect Alexa to your library.</p>
<form method=post>
  <input type=hidden name=redirect_uri value="{redirect_uri}">
  <input type=hidden name=state value="{state}">
  <input type=hidden name=client_id value="{client_id}">
  <input type=password name=passphrase placeholder="Linking passphrase" autofocus required>
  <button type=submit>Link account</button>
</form>"""

HTML = "text/html; charset=utf-8"


def oauth_authorize_page(args: Mapping[str, str]) -> Json | Page:
    client_id = args.get("client_id", "")
    redirect_uri = args.get("redirect_uri", "")
    state = args.get("state", "")

    if client_id != oauth.CLIENT_ID:
        return Json(400, {"error": "unauthorized_client"})
    if not oauth.redirect_allowed(redirect_uri):
        return Json(400, {"error": "invalid_redirect_uri", "got": redirect_uri})

    return Page(200, _AUTHORIZE_FORM.format(
        redirect_uri=escape(redirect_uri),
        state=escape(state),
        client_id=escape(client_id),
    ), HTML)


def oauth_authorize_submit(form: Mapping[str, str]) -> Json | Page | Redirect:
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    passphrase = form.get("passphrase", "")

    if not oauth.redirect_allowed(redirect_uri):
        return Json(400, {"error": "invalid_redirect_uri"})
    if not oauth.LINK_SECRET or not hmac.compare_digest(passphrase, oauth.LINK_SECRET):
        logger.warning("account link rejected: bad passphrase")
        return Page(403, "Incorrect passphrase.", "text/plain")

    code = oauth.mint("code", oauth.CODE_TTL, sub="owner")
    sep = "&" if "?" in redirect_uri else "?"
    logger.info("account linked, redirecting to %s", redirect_uri)
    return Redirect(f"{redirect_uri}{sep}code={code}&state={urllib.parse.quote(state)}")


def oauth_token_exchange(form: Mapping[str, str],
                         auth_header: str = "") -> Json:
    client_id = form.get("client_id") or ""
    client_secret = form.get("client_secret") or ""

    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            client_id, _, client_secret = decoded.partition(":")
        except Exception:
            pass

    if client_id != oauth.CLIENT_ID or not hmac.compare_digest(
        client_secret, oauth.CLIENT_SECRET
    ):
        return Json(401, {"error": "invalid_client"})

    grant = form.get("grant_type")
    if grant == "authorization_code":
        if not oauth.read(form.get("code", ""), "code"):
            return Json(400, {"error": "invalid_grant"})
    elif grant == "refresh_token":
        if not oauth.read(form.get("refresh_token", ""), "refresh"):
            return Json(400, {"error": "invalid_grant"})
    else:
        return Json(400, {"error": "unsupported_grant_type"})

    logger.info("issued tokens via %s", grant)
    return Json(200, oauth.issue_tokens())


# --------------------------------------------------------------------------
# static, human-facing
# --------------------------------------------------------------------------


def icon(name: str) -> Page:
    """Always send the bytes.

    Amazon's manifest validator (`AlexaSkillManagementAPI/1.0`) re-fetches the
    icons on every update and sends a conditional request. A framework's
    default conditional handling answers 304 with an empty body, which Amazon
    reported as RESOURCE_NOT_FOUND against largeIconUri and failed the whole
    update. Turning conditional handling off is not enough either: an ETag is
    still emitted, and a validator that echoes it back still gets a 304. The
    only way to guarantee bytes on every request is to send no validators at
    all, which is why this returns a `Page` of bytes rather than a `LocalFile`.
    These are tens of kilobytes and fetched rarely, so nothing is lost.
    """
    path = (ICON_DIR / name).resolve()
    if not path.is_file() or ICON_DIR.resolve() not in path.parents:
        return Page(404, "not found", "text/plain")
    return Page(200, path.read_bytes(), "image/png",
                {"Cache-Control": "no-store"})


def landing() -> Page:
    """Human-facing landing page.

    The Alexa app opens the skill's endpoint root in a webview. Serving only
    POST here returns 405 with no body, which the webview renders as a blank
    black screen.
    """
    return Page(200,
                "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Music Assistant</title>"
                "<style>body{font-family:system-ui;max-width:26rem;margin:3rem auto;padding:0 1rem;"
                "text-align:center}img{width:96px;height:96px}</style>"
                "<img src='/icons/ma-512.png' alt=''>"
                "<h2>Music Assistant</h2>"
                "<p>Streams your self-hosted library to Alexa devices.</p>"
                "<p><a href='/privacy'>Privacy</a> &middot; <a href='/terms'>Terms</a></p>",
                HTML)


def privacy() -> Page:
    return Page(200,
                "<h1>Privacy</h1><p>Private, single-user development skill. Streams from a "
                "self-hosted server. No user data is collected, stored, or shared.</p>",
                HTML)


def terms() -> Page:
    return Page(200,
                "<h1>Terms of Use</h1><p>Private, single-user development skill, as-is, "
                "no warranty.</p>",
                HTML)


# --------------------------------------------------------------------------
# process startup
# --------------------------------------------------------------------------


def advertise(port: int) -> None:
    """Announce the service on the local network, if MDNS is configured.

    Takes the port rather than reading it, because the two adapters do not
    listen on the same one: standalone owns its port outright, while inside
    Music Assistant this is a second listener alongside MA's own.
    """
    mdns.advertise(port)


def warm_up() -> None:
    """Start filling the artist cache, if that is wanted here.

    Called by whatever is about to serve, not at import. Importing used to fire
    it, which meant Music Assistant's startup made Subsonic requests before
    anything had told Music Assistant where Subsonic is, and logged a traceback for each
    one. A cache warm is an optimisation; it has no business running in a
    process that has not decided to serve anything yet.
    """
    if os.environ.get("PREWARM", "1") != "1":
        return
    if not subsonic.configured():
        logger.debug("no music server configured yet, not warming the cache")
        return
    _WARM_POOL.submit(prewarm_artists)
