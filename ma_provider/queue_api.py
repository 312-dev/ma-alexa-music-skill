"""Out-of-band queues: a stable contentId for a track list that has no name.

Every other contentId this service issues names something Subsonic already
knows about: an album, a playlist, an artist, a genre. The id alone is enough
to re-derive the track list on any later request, which is what keeps Initiate
inside Amazon's budget and lets the service hold no per-user playback state.

Music Assistant breaks that assumption. A queue MA composes is the output of a
smart playlist, or three albums shuffled together, or whatever the user
dragged into order thirty seconds ago. The individual tracks still live on the
Subsonic server, and they have to: the bridge streams from there and nowhere
else. The *list* is what has no name. There is nothing for a contentId to
point at. Alexa still needs one: it echoes the contentId it was handed at Initiate
back on every later queue request, for the life of the queue, and an Item
carries no field to hand back a different one. Content cannot be swapped
mid-queue, and a queue with no id cannot be played at all.

So the list is published here, ahead of playback, and is given an opaque id
that behaves like every other one: stable, re-derivable, and good for the life
of the queue. `ext:<token>` is that id.

The token is an HMAC of the track list under SIGNING_KEY, truncated. Hashing
the list means republishing an unchanged queue returns the id Alexa is already
holding instead of orphaning it. HMAC rather than a plain digest means someone
who knows a few song ids still cannot enumerate other people's queues.

Records are written to disk, not held in memory, for the same two reasons
queuestate is: gunicorn runs several workers and a dict would give each its own
view, and a bridge restart in the middle of a song must not end playback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pathlib
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from . import handoff
from . import mastream_cache
from . import subsonic
from .answers import Json
from . import stream_ref

logger = logging.getLogger("ma-music-skill.queue-api")

# Same key app.py signs stream URLs with. If SIGNING_KEY is unset both modules
# fall back to their own random key, which is fine here: nothing outside this
# module ever has to reproduce one of these tokens.
SIGNING_KEY = os.environ.get("SIGNING_KEY", "").encode() or os.urandom(32)

# A subdirectory of the queuestate root rather than the root itself, so these
# never share a namespace with the per-queueId shuffle/loop files.
STATE_DIR = pathlib.Path(
    os.environ.get("QUEUE_STATE_DIR", "/data/queuestate")
) / "external"

# Seven days. A published queue is dead weight the moment Alexa stops asking
# for it, but there is no event that tells us when that happened, so the only
# safe bound is one long enough that no real listening session outlives it.
TTL = int(os.environ.get("EXTERNAL_QUEUE_TTL", str(7 * 24 * 3600)))
MAX_QUEUES = int(os.environ.get("EXTERNAL_QUEUE_MAX", "64"))

CONTENT_PREFIX = "ext"

# The alias resolve() accepts for "whatever was published most recently". It is
# deliberately never handed to Alexa; see handoff_content_id below.
CURRENT = "current"

TOKEN_BYTES = 12

# Song lookups run wide because a published queue can be a few hundred tracks
# and each one is a round trip. This is off the Alexa request path entirely
# (publishing happens before anyone says anything), but a user waiting on a
# speaker to start is still waiting.
_FETCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="extq-fetch")


# --------------------------------------------------------------------------
# the handoff phrase
# --------------------------------------------------------------------------
#
# Publishing a queue solves the id problem and none of the utterance problem.
# Alexa resolves what was *said* against the uploaded catalog and hands us the
# result; an arbitrary MA queue has no name anyone can say. Three ways out were
# considered:
#
#   (a) An on-deck slot: publish, then let the next Initiate from that account
#       claim whatever is pending. Requires no phrase at all, and is wrong the
#       first time two things start at once or the user says something else in
#       between. The failure is silent and plays the wrong music.
#   (b) One fixed phrase that always means "the queue I just published". The
#       user says it once per handoff and never sees it again, because MA
#       speaks it, not them.
#   (c) Refuse anything without a catalog identity, so MA can only ever ask for
#       an album or a playlist that already exists. Correct, and gives up the
#       entire point of the provider.
#
# (b) is chosen. It is the only one that is both deterministic and general.
# The cost is a word that competes with the library: Alexa resolves content
# before it routes to a provider, so a phrase matching a real artist or track
# will be eaten by it (this is the same failure that cost "jukebox" and "gray
# tunes" their alias). Hence a list, so a user whose library really does
# contain a "Music Assistant" can move to a phrase that it does not.
#
# The phrase resolves at search time to the concrete `ext:<token>` of the
# newest published queue, never to the `ext:current` alias. Alexa would echo an
# alias back for hours and quietly follow it onto a later queue; a token pins
# the answer at the moment the user asked.
#
# The phrase must also be *in the catalog*, which is not a nicety. Measured on
# 2026-08-02: Amazon does not forward an utterance the catalog cannot resolve.
# "ask ampere to play Gregory Alan Isakov" arrived, played and streamed; "ask
# ampere to play music assistant" and a deliberate nonsense phrase produced no
# inbound request at all, not a search with free text in it. Entity resolution
# is Amazon's gate, ahead of the skill, so an unresolvable phrase is not passed
# through as text; it is simply never asked about. Hence HANDOFF_ENTITY_ID
# below and the entity `catalog_sync.py` emits for it. The free-text branch is
# kept because it costs nothing and covers the case where Amazon does forward
# the words.

# The naming itself lives in `handoff`, so `catalog_sync` can emit the entity
# without importing this module and its state directories. Only the predicates
# are re-exported: aliasing the constants too would leave a name here that
# reads like the setting and silently is not the one the predicates consult.
is_handoff_phrase = handoff.is_handoff_phrase
is_handoff_entity = handoff.is_handoff_entity


def handoff_content_id() -> str | None:
    """The contentId the handoff phrase should resolve to right now.

    None when nothing has been published yet, which is a CONTENT_NOT_FOUND
    rather than something to guess at.
    """
    record = _newest()
    return f"{CONTENT_PREFIX}:{record['token']}" if record else None


def handoff_name() -> str:
    """What the Alexa app should call the handoff queue."""
    record = _newest()
    label = (record or {}).get("name") or ""
    return label or "Music Assistant"


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def _safe(token: str) -> str:
    # Tokens are ours (hex), but never trust an id straight into a path.
    return "".join(c for c in (token or "") if c.isalnum() or c in "-_")[:80]


def _path(token: str) -> pathlib.Path:
    return STATE_DIR / f"{_safe(token)}.json"


def token_for(track_ids: list[str]) -> str:
    """Stable, unguessable token for an ordered track list."""
    msg = ("extq\n" + "\n".join(track_ids)).encode()
    return hmac.new(SIGNING_KEY, msg, hashlib.sha256).hexdigest()[: TOKEN_BYTES * 2]


def _write(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _path(record["token"])
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: pathlib.Path) -> dict | None:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("token") else None


def _expired(record: dict, now: float | None = None) -> bool:
    return (now or time.time()) - float(record.get("published", 0)) > TTL


def _records() -> list[dict]:
    """Every live record, newest first. Expired files are deleted on the way."""
    now = time.time()
    out = []
    for path in STATE_DIR.glob("*.json"):
        record = _read(path)
        if record is None or _expired(record, now):
            # An unreadable file is as useless as an expired one and will never
            # become readable, so both go.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        out.append(record)
    out.sort(key=lambda r: float(r.get("published", 0)), reverse=True)
    return out


def _newest() -> dict | None:
    """The most recently published live record.

    Ordered by mtime and read one at a time rather than through _records,
    because this runs on the Alexa search path when the handoff phrase is
    resolved, and reading the whole store to answer it would put a few dozen
    file reads in front of a speaker that is waiting to start.
    """
    try:
        paths = sorted(
            STATE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    now = time.time()
    for path in paths:
        record = _read(path)
        if record is not None and not _expired(record, now):
            return record
    return None


def _evict() -> None:
    """Keep the store bounded. Oldest publish time goes first."""
    records = _records()
    for record in records[MAX_QUEUES:]:
        try:
            _path(record["token"]).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# publish / resolve
# --------------------------------------------------------------------------


def _fetch(song_id: str) -> dict | None:
    try:
        return subsonic.song(song_id)
    except Exception:
        # One track that has left the library must not cost the whole queue.
        logger.warning("published queue references unknown song %s", song_id)
        return None


# The `id` prefix an MA-sourced track gets. It has to be an id at all because
# the token hashes ids and resolve() drops anything without one, and it has to
# be distinguishable from a Subsonic id because the two are streamed from
# entirely different places.
MA_ID_PREFIX = "ma:"


def _ma_song(track: dict) -> dict | None:
    """A Music Assistant track, shaped like the Subsonic records around it.

    Everything Alexa renders is already in the publish body, because the bridge
    has no way to ask Music Assistant what a track is called. The one thing not
    resolved here is the audio: `ma_ref` is the item's MA uri, and it is turned
    into a URL only when Alexa actually asks for the bytes.
    """
    ref = str(track.get("ref") or "")
    if not stream_ref.is_ref(ref):
        # Refused rather than stored. A ref that does not decode to an MA uri
        # is either a bug or an attempt to point the bridge's proxy at
        # something else, and both should fail where they happen.
        logger.warning("refusing a published track whose ref is not an MA uri")
        return None

    title = str(track.get("title") or "").strip()
    if not title:
        return None

    song = {
        "id": f"{MA_ID_PREFIX}{ref}",
        "ma_ref": ref,
        "title": title,
        "artist": str(track.get("artist") or ""),
        "album": str(track.get("album") or ""),
    }
    try:
        duration = int(track.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        song["duration"] = duration
    # Only ever an absolute URL Amazon can reach on its own. The provider only
    # sends one when Music Assistant says the image is remotely accessible.
    art = str(track.get("art_url") or "")
    if art.startswith(("http://", "https://")):
        song["art_url"] = art
    return song


def publish(tracks: list, name: str = "", start_offset_ms: int = 0) -> dict:
    """Store an ordered track list and return its record.

    A track is either a Subsonic song id as a string, or a dict describing a
    track that lives in Music Assistant and has no Subsonic identity. Both end
    up as the same kind of record, so nothing downstream has to care which it
    was beyond choosing where to fetch the audio from.

    Whole song records are stored, not ids, for the reason resolve_tracks
    caches whole records: re-fetching each song per Item put Initiate seconds
    over budget. Publishing is the one moment there is time to do the lookups.

    `start_offset_ms` is how far into the **first** track playback should
    begin. It exists because Music Assistant implements seek by re-issuing
    play_media on the current item, so the only way a seek can survive the trip
    to Alexa is to travel with the queue that trip publishes.
    """
    # Kept in order and in one list, because the token is derived from it and
    # the queue must hash the same whichever kinds it mixes.
    keys: list[str] = []
    subsonic_ids: list[str] = []
    ma_songs: dict[int, dict] = {}
    for track in tracks:
        if isinstance(track, dict):
            song = _ma_song(track)
            if song is None:
                continue
            ma_songs[len(keys)] = song
            keys.append(song["id"])
        elif str(track or "").strip():
            subsonic_ids.append(str(track))
            keys.append(str(track))

    token = token_for(keys)

    # Only the Subsonic ids need a round trip; an MA track arrived complete.
    fetched = dict(zip(subsonic_ids, _FETCH_POOL.map(_fetch, subsonic_ids)))
    songs = []
    for position, key in enumerate(keys):
        song = ma_songs.get(position) or fetched.get(key)
        if song and song.get("id"):
            songs.append(song)
    record = {
        "token": token,
        "name": (name or "").strip(),
        "tracks": songs,
        "requested": len(keys),
        "published": time.time(),
        # The token is derived from the track ids, so re-publishing the same
        # queue lands on the same record and this overwrites. That is what
        # makes the offset self-clearing: a plain play of the same queue
        # publishes 0 and the stale seek goes with it.
        "start_offset_ms": max(0, int(start_offset_ms or 0)),
    }
    _write(record)
    _evict()

    # The first tracks start buffering now, before Alexa has been told
    # anything. Publishing runs a second or two ahead of the utterance and
    # several ahead of the first audio fetch, and a track takes about five
    # seconds to buffer, so the head of a queue is usually ready before
    # anything asks for it. This is the one point in the flow where nothing
    # has asked yet; from the first Item onwards the read-ahead in item_at
    # takes over.
    mastream_cache.prefetch([s["ma_ref"] for s in songs if s.get("ma_ref")])
    return record


def resolve(token: str) -> list[dict]:
    """Song records for a published token, or [] if unknown or expired.

    Shape matches what resolve_tracks returns for every other prefix: a list of
    Subsonic song dicts as subsonic.song hands them back. app.py's `ext:`
    branch can drop this straight in.
    """
    if token == CURRENT:
        record = _newest()
    else:
        record = _read(_path(token))
        if record is not None and _expired(record):
            record = None
    if record is None:
        return []
    return [s for s in record.get("tracks", []) if s and s.get("id")]


def start_offset_ms(token: str) -> int:
    """How far into the first track a published queue should start.

    Zero for anything this module did not publish, which is every content id
    that did not come from Music Assistant. Read the same way `resolve` reads,
    so an expired queue reports no offset rather than an offset onto tracks
    that are no longer there.
    """
    if token == CURRENT:
        record = _newest()
    else:
        record = _read(_path(token))
        if record is not None and _expired(record):
            record = None
    if record is None:
        return 0
    return max(0, int(record.get("start_offset_ms") or 0))


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def authorized(headers: Mapping[str, str]) -> bool:
    """The handoff endpoint's own admission check.

    Deliberately narrower than the admin plane's: this one is called by Music
    Assistant, which may be on the same host or another one, so it is a token
    check without the source-address rule. Publishing a queue reveals nothing
    and can only ever cost the caller their own playback.
    """
    expected = os.environ.get("ADMIN_TOKEN")
    return bool(expected) and headers.get("X-Admin-Token") == expected


def publish_request(body: object, headers: Mapping[str, str]) -> Json:
    """`POST /queue`, minus the framework."""
    if not authorized(headers):
        return Json(401, {"error": "unauthorized"})

    if not isinstance(body, dict):
        body = {}
    tracks = body.get("tracks")
    bad = "tracks must be a non-empty list of song ids or Music Assistant tracks"
    if not isinstance(tracks, list) or not tracks:
        return Json(400, {"error": bad})
    if not all(isinstance(t, (str, dict)) for t in tracks):
        return Json(400, {"error": bad})
    if any(isinstance(t, dict) and t.get("source") != "ma" for t in tracks):
        return Json(400, {"error": "a track object must set source to 'ma'"})

    try:
        offset = int(body.get("start_offset_ms") or 0)
    except (TypeError, ValueError):
        return Json(400, {"error": "start_offset_ms must be an integer"})

    record = publish(tracks, str(body.get("name") or ""), offset)
    logger.info(
        "published external queue %s (%d of %d tracks) %r%s",
        record["token"], len(record["tracks"]), record["requested"], record["name"],
        f" starting at {record['start_offset_ms']}ms" if record["start_offset_ms"] else "",
    )
    return Json(200, {
        "content_id": f"{CONTENT_PREFIX}:{record['token']}",
        "count": len(record["tracks"]),
        "requested": record["requested"],
    })


def show_request(token: str, headers: Mapping[str, str]) -> Json:
    """`GET /queue/<token>`, minus the framework."""
    if not authorized(headers):
        return Json(401, {"error": "unauthorized"})

    record = _newest() if token == CURRENT else _read(_path(token))
    if record is None or _expired(record):
        return Json(404, {"error": "unknown queue"})
    return Json(200, {
        "content_id": f"{CONTENT_PREFIX}:{record['token']}",
        "name": record.get("name", ""),
        "count": len(record.get("tracks", [])),
        "requested": record.get("requested", 0),
        "published": record.get("published", 0),
        "tracks": [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "artist": s.get("artist"),
                "album": s.get("album"),
            }
            for s in record.get("tracks", [])
        ],
    })
