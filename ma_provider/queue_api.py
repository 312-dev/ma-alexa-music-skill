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
import threading
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
# speaker to start is still waiting: measured 2026-08-03, publishing was 8.33s
# of a 10s play. Wide on purpose. These are small metadata reads against one Subsonic server
# on a LAN or a tailnet, and they are the only thing standing between pressing
# play and hearing music, so the queue is fetched in as few round trips as the
# server will take rather than in careful batches of eight.
#
# Created lazily and torn down with the provider (see shutdown_pool), so a
# reload does not leak the old threads and stack a second pool on top.
_FETCH_POOL: ThreadPoolExecutor | None = None
_FETCH_POOL_LOCK = threading.Lock()


def _fetch_pool() -> ThreadPoolExecutor:
    global _FETCH_POOL
    with _FETCH_POOL_LOCK:
        if _FETCH_POOL is None:
            _FETCH_POOL = ThreadPoolExecutor(
                max_workers=32, thread_name_prefix="extq-fetch"
            )
        return _FETCH_POOL


def shutdown_pool() -> None:
    """Release the fetch pool so a later use lazily builds a fresh one."""
    global _FETCH_POOL
    with _FETCH_POOL_LOCK:
        pool, _FETCH_POOL = _FETCH_POOL, None
    if pool is not None:
        pool.shutdown(wait=False)


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
# "ask music assistant to play Gregory Alan Isakov" arrived, played and streamed; "ask
# music assistant to play music assistant" and a deliberate nonsense phrase produced no
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


# When Alexa last acted on a handoff, as a monotonic stamp.
#
# Amazon accepts a `run_custom` and then sometimes does nothing with it: the
# call returns 200 in under a second and no search ever arrives. Measured
# 2026-08-03, and the same shape as a volume change needing to be sent twice.
# A caller cannot tell the difference from its own side, so this records the
# only unambiguous evidence there is, which is Alexa turning up to ask.
_LAST_HANDOFF = 0.0


def handoff_claimed_at() -> float:
    """When a handoff was last resolved for Alexa. 0.0 if never."""
    return _LAST_HANDOFF


def note_handoff_claimed() -> None:
    global _LAST_HANDOFF
    _LAST_HANDOFF = time.monotonic()


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


# Song records already fetched, by id. Publishing a queue used to cost one
# Subsonic round trip per track every single time, which on a real playlist is
# the whole delay between pressing play and hearing anything: measured
# 2026-08-03, publishing took 8.33s of a 10s play, against a `run_custom` to
# Amazon that took 0.20s. The remote service was never the slow part.
#
# Cached because the same tracks are published over and over. Resuming,
# seeking, moving a queue between rooms and skipping all republish a list whose
# songs were fetched moments earlier, and none of that metadata has changed.
_SONG_CACHE: dict[str, tuple[float, dict | None]] = {}

# Long enough to cover a listening session, short enough that a retag or a
# replaced file is picked up the same evening rather than next restart.
SONG_CACHE_TTL = float(os.environ.get("SONG_CACHE_TTL", "1800"))

# A ceiling so a very large library cannot turn this into a memory leak. Songs
# are small; this is thousands of tracks, not a limit anyone will notice.
SONG_CACHE_MAX = 5000


def _fetch(song_id: str) -> dict | None:
    now = time.time()
    hit = _SONG_CACHE.get(song_id)
    if hit is not None and now - hit[0] < SONG_CACHE_TTL:
        return hit[1]
    try:
        song = subsonic.song(song_id)
    except Exception:
        # One track that has left the library must not cost the whole queue.
        logger.warning("published queue references unknown song %s", song_id)
        # Cached as a miss too, briefly. A queue holding a deleted track would
        # otherwise pay the same failing lookup on every republish.
        song = None
    if len(_SONG_CACHE) >= SONG_CACHE_MAX:
        _SONG_CACHE.clear()
    _SONG_CACHE[song_id] = (now, song)
    return song


def forget_songs() -> None:
    """Drop the cached song records.

    For a library that has just been rescanned, where holding half an hour of
    stale titles would be worse than paying the lookups again.
    """
    _SONG_CACHE.clear()


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


def _prepared_subsonic(track: dict) -> dict | None:
    """A Subsonic record the provider resolved from MA's own metadata.

    The counterpart to `_ma_song` for the Subsonic side: the provider had the
    title, artist and duration in the queue item Music Assistant handed it, so
    it built the record there rather than making the bridge fetch it back with
    a getSong. Shaped exactly like a fetched Subsonic record -- a plain `id`
    and no `ref` -- so `build_item` streams and renders it identically.

    Validated the same way a fetched record is: an id and a title are the
    minimum, and a record without them is dropped rather than published as a
    track that cannot be named or streamed.
    """
    sid = str(track.get("id") or "").strip()
    title = str(track.get("title") or "").strip()
    if not sid or not title:
        return None
    song: dict = {
        "id": sid,
        "title": title,
        "artist": str(track.get("artist") or ""),
        "album": str(track.get("album") or ""),
        "coverArt": str(track.get("coverArt") or sid),
    }
    try:
        seconds = int(track.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        song["duration"] = seconds
    art = str(track.get("art_url") or "")
    if art.startswith(("http://", "https://")):
        song["art_url"] = art
    return song


def _songs_from_tracks(tracks: list) -> tuple[list[str], list[dict]]:
    """Turn a publish/append input list into (keys, songs).

    `keys` is the ordered list of ids the token hashes and `requested` counts;
    `songs` is the ordered list of complete song records, minus anything that
    failed validation. The two are separate because `publish` derives the token
    and the requested count from `keys` while `append_to_queue` does neither, so
    folding one into the other would make one of them lie.

    This is the loop `publish` used to inline, lifted out so an append reaches
    songs through the identical path -- same three input shapes, same fetch
    pool, same "drop anything without an id" rule -- rather than a second copy
    that could drift from the one Alexa is actually served from.
    """
    # Kept in order and in one list, because the token is derived from it and
    # the queue must hash the same whichever kinds it mixes.
    keys: list[str] = []
    subsonic_ids: list[str] = []
    prepared: dict[int, dict] = {}
    for track in tracks:
        if isinstance(track, dict):
            # Three shapes reach here. A dict with `source == "ma"` (or a `ref`)
            # is a track streamed from Music Assistant. A dict marked
            # `source == "subsonic"` is a Subsonic track the provider already
            # resolved from MA's own metadata, so it needs no getSong. A bare
            # string is a Subsonic id that does, and is fetched below.
            if track.get("source") == "ma" or track.get("ref"):
                song = _ma_song(track)
            else:
                song = _prepared_subsonic(track)
            if song is None:
                continue
            prepared[len(keys)] = song
            keys.append(song["id"])
        elif str(track or "").strip():
            subsonic_ids.append(str(track))
            keys.append(str(track))

    # Only the bare Subsonic ids need a round trip; an MA track and a
    # pre-resolved Subsonic track both arrived complete.
    fetched = dict(zip(subsonic_ids, _fetch_pool().map(_fetch, subsonic_ids)))
    songs = []
    for position, key in enumerate(keys):
        song = prepared.get(position) or fetched.get(key)
        if song and song.get("id"):
            songs.append(song)
    return keys, songs


def _forget_resolved(token: str) -> None:
    """Drop core's cached resolution of this token, if core is importable.

    `resolve_tracks` caches an `ext:` token's track list the first time it is
    resolved (cacheable = bool(songs)) and never re-derives an entry it already
    holds. So a record mutated in place -- an append, below -- is invisible:
    every later GetNextItem serves the list cached at the first resolve. Any
    writer that changes what a token resolves to has to invalidate that cache
    or the change is masked for the life of the process.

    Imported lazily because `core` imports this module at load time; a
    top-level `from . import core` here would be a circular import. By the time
    an append runs, both modules are fully loaded, so the late import is free.

    NOTE: the cache is per-process. Under a multi-worker server the worker that
    services an append is not necessarily the one Amazon's GetNextItem lands
    on, and this only clears the caller's copy -- the disk record is shared, the
    caches are not. A growing queue therefore needs either a single-worker
    deployment or a cross-worker invalidation signal. See PLAN phase 8.
    """
    try:
        from . import core
    except Exception:  # pragma: no cover - core is always importable in practice
        return
    core.forget_content(f"{CONTENT_PREFIX}:{token}")


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
    keys, songs = _songs_from_tracks(tracks)
    token = token_for(keys)
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


def append_to_queue(token: str, tracks: list) -> dict | None:
    """Append tracks to a queue already published under `token`.

    The SPIKE mechanism behind "radio continues after an MA queue ends": grow a
    published queue in place so Alexa's later GetNextItem calls pull the new
    tracks off the same contentId, with no re-Initiate (a re-Initiate would gap
    or restart playback). Nothing calls this yet -- it is the tested building
    block a queue-growth listener would use once the live probe confirms Alexa
    keeps pulling from a grown token. See PLAN phase 8 and
    tools/growing_token_probe.py.

    `tracks` takes the same shapes `publish` does -- a bare Subsonic id, a
    `source == "ma"` dict, or a `source == "subsonic"` pre-resolved dict -- and
    is prepared through the identical path (`_songs_from_tracks`), so an
    appended track is indistinguishable from one published from the start.
    Returns the updated record, or None if the token is unknown or expired.

    The token is deliberately NOT re-derived. `token_for` hashes the track
    list, so a fresh publish of the grown list would mint a different id -- but
    Alexa is already holding `ext:<token>` and echoes it back on every later
    request, and an Item carries no field to hand it a new one. So the record
    keeps its original token even though it no longer hashes its contents. That
    broken invariant is the whole point: a stable id whose list may grow. (One
    consequence: a later plain publish of the same longer list lands on a
    different record, so the self-clearing-seek behaviour does not apply to a
    grown queue -- fine, since a grown queue is not something replayed by
    saying the same thing.)

    Two writes matter and both happen here: the record on disk, so a fresh
    resolve sees the new tracks; and core's resolve cache, invalidated via
    `_forget_resolved` or the first resolve's cached list masks the append for
    the life of the process.
    """
    if token == CURRENT:
        record = _newest()
    else:
        record = _read(_path(token))
        if record is not None and _expired(record):
            record = None
    if record is None:
        return None

    _keys, new_songs = _songs_from_tracks(tracks)
    if not new_songs:
        # Appending nothing valid is a no-op, not a failure: return the record
        # unchanged rather than an error.
        return record

    record["tracks"] = list(record.get("tracks", [])) + new_songs
    record["requested"] = int(record.get("requested", 0)) + len(_keys)
    _write(record)

    # Buffer the appended MA tracks the way publish buffers a fresh queue's
    # head, so the bytes are ready by the time Alexa asks for them.
    mastream_cache.prefetch([s["ma_ref"] for s in new_songs if s.get("ma_ref")])

    # The crux: without this the append is written to disk and never seen, the
    # list core cached at the first GetNextItem being served for the rest of
    # the process. Invalidate it so the next resolve re-reads the grown record.
    _forget_resolved(record["token"])
    return record


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
    if any(isinstance(t, dict) and t.get("source") not in ("ma", "subsonic")
           for t in tracks):
        return Json(400, {"error": "a track object must set source to 'ma' or 'subsonic'"})

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


def append_request(token: str, body: object, headers: Mapping[str, str]) -> Json:
    """`POST /queue/<token>/append`, minus the framework.

    Exists so an append runs in the same process that serves GetNextItem. The
    cache `append_to_queue` invalidates is per-process (see `_forget_resolved`),
    so an append issued from a separate process -- a probe, a CLI -- writes the
    disk record but never clears the resolve cache the running server holds, and
    the grown queue stays masked. Routing the append through this endpoint, which
    the server itself serves, is what makes the invalidation land where it
    matters. The same reason a growing queue needs a single-worker deployment.
    """
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
    if any(isinstance(t, dict) and t.get("source") not in ("ma", "subsonic")
           for t in tracks):
        return Json(400, {"error": "a track object must set source to 'ma' or 'subsonic'"})

    record = append_to_queue(token, tracks)
    if record is None:
        return Json(404, {"error": "unknown or expired queue"})
    logger.info(
        "appended to external queue %s (now %d of %d tracks)",
        record["token"], len(record["tracks"]), record["requested"],
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
