"""Buffering Music Assistant's audio so Alexa gets a seekable file.

Phase 3 of ma_provider/PLAN.md, and the thing that makes a non-Subsonic track a
first-class one rather than a degraded one.

## What was wrong with streaming it through

Phase 2 proxies MA's audio straight to Alexa. MA serves a realtime stream:
always `200`, no `Content-Length`, no `Accept-Ranges`. Measured, that plays
perfectly well, and Alexa never sends a range on a first fetch. But two things
do send one, and both are ordinary:

  - scrubbing the progress bar
  - moving playback between rooms, which re-pulls the *same* URL with
    `Range: bytes=N-` and no directive of any kind reaching the skill

A range against a source that cannot answer it gets byte zero with a `200`, so
the track restarts. Phase 2 dealt with that by not offering the seek control at
all and accepting that a transfer restarts the song.

## Why a whole-file buffer rather than a clever windowed one

Because of one measurement. Music Assistant produces a complete 3:46 track in
about 4.5 seconds, first byte in 0.1:

    bytes=9058264  total=4.726s  firstbyte=0.206s
    bytes=8503423  total=4.376s  firstbyte=0.101s

At that speed there is no reason to serve a partial object. Buffer the whole
track, then hand Alexa an ordinary file with a real length that answers ranges
exactly, which is what Navidrome already does and why Subsonic tracks have
always behaved. The complicated designs, serving ranges out of a growing file
and guessing byte offsets from a constant bitrate, all exist to avoid a wait
that turns out to be a few seconds and is usually zero anyway, because the
queue is published before Alexa asks for anything and prefetching starts there.

## Notes on the implementation

Single process by design: the Dockerfile runs one gunicorn worker with many
threads precisely so in-process caches work, so a module-level dict and a lock
are enough and no cross-process coordination is needed.

A partial download is written to `.part` and renamed into place only when it is
complete, so a file that exists is a file that is whole. That matters more than
usual here: half a track served with a confident `Content-Length` is worse than
no track at all, because nothing downstream can tell.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ma_provider import stream_ref

logger = logging.getLogger("ma-music-skill.mastream-cache")

# Where Music Assistant answers. Held here, and never taken from a published
# queue, so that the publish endpoint cannot be used to point this at an
# arbitrary host.
#
# Port 8097, not 8095. MA runs two webservers: the API and frontend on 8095,
# and a separate streams server on 8097, which is the one
# `mass.streams.register_dynamic_route` registers against. A route registered
# there is not reachable on 8095 at all, and 8095 answers with a bare 404 that
# reads exactly like a route that was never registered.
BASE = os.environ.get("MA_STREAM_BASE", "http://127.0.0.1:8097").rstrip("/")

CACHE_DIR = pathlib.Path(
    os.environ.get("MA_CACHE_DIR", "/data/mastream")
)

# About a hundred tracks at the ~9 MB a lossy MA track weighs. Small next to
# the disk and large next to any real queue.
MAX_BYTES = int(os.environ.get("MA_CACHE_MAX_BYTES", str(1024 * 1024 * 1024)))

# A day. A cached track is dead weight the moment its queue is over, and there
# is no event that says when that was, so the bound is a duration rather than a
# lifecycle.
TTL = int(os.environ.get("MA_CACHE_TTL", str(24 * 3600)))

# How long a request will wait for a track that is still being fetched. Well
# clear of the ~5s a fetch takes, and short enough that a genuinely stuck
# fetch degrades to the unbuffered path instead of holding a speaker silent.
WAIT_TIMEOUT = int(os.environ.get("MA_CACHE_WAIT", "45"))

ENABLED = os.environ.get("MA_STREAM_CACHE", "1") != "0"

# Read-ahead. Alexa asks for items before it plays them, so this rarely has to
# do anything; it exists for the first track of a queue, which is the one case
# where nothing has asked yet.
PREFETCH_AHEAD = int(os.environ.get("MA_CACHE_PREFETCH", "2"))

_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mastream-fetch")

# ref -> lock. One fetch per track, however many requests arrive for it: four
# Echoes in a group starting together is the normal case, not the exception.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_INFLIGHT: set[str] = set()


def _lock_for(ref: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(ref)
        if lock is None:
            lock = _LOCKS[ref] = threading.Lock()
        return lock


def _safe(ref: str) -> str:
    """A filename from a ref. Refs are base64url, but never trust one."""
    return "".join(c for c in (ref or "") if c.isalnum() or c in "-_")[:200]


def path_for(ref: str) -> pathlib.Path:
    return CACHE_DIR / f"{_safe(ref)}.mp3"


def _fetch(ref: str) -> pathlib.Path | None:
    """Pull a whole track from Music Assistant. Returns the finished file.

    Serialized per ref by the caller's lock, and re-checks on the way in
    because the request that was waiting for the lock is usually waiting for
    exactly the download that just finished.
    """
    target = path_for(ref)
    if target.exists():
        return target

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    url = f"{BASE}{stream_ref.stream_path(ref)}"
    started = time.monotonic()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=30) as upstream:
            with open(part, "wb") as handle:
                while chunk := upstream.read(256 * 1024):
                    handle.write(chunk)
                    size += len(chunk)
        if size == 0:
            raise OSError("Music Assistant returned no audio")
        # Renamed only once whole, so a file that exists is a file that is
        # complete. Anything else would be served with a confident
        # Content-Length that is a lie.
        os.replace(part, target)
    except (urllib.error.URLError, OSError, ValueError) as err:
        logger.warning("could not buffer %s: %s", stream_ref.decode_ref(ref) or ref, err)
        try:
            part.unlink()
        except OSError:
            pass
        return None

    logger.info(
        "buffered %s: %.1f MB in %.1fs",
        stream_ref.decode_ref(ref) or ref, size / 1e6, time.monotonic() - started,
    )
    evict()
    return target


def ensure(ref: str) -> pathlib.Path | None:
    """The complete file for a ref, fetching it if it is not here yet.

    None means "serve it some other way", never "there is no such track": a
    caller that gets None should fall back to streaming from MA rather than
    failing the request, so a full disk costs seeking and not playback.
    """
    if not ENABLED or not stream_ref.is_ref(ref):
        return None

    target = path_for(ref)
    if target.exists():
        # Touch, so eviction sees "recently played" rather than "downloaded a
        # long time ago". A track being played on repeat should not be the
        # first thing thrown away.
        try:
            os.utime(target, None)
        except OSError:
            pass
        return target

    lock = _lock_for(ref)
    if not lock.acquire(timeout=WAIT_TIMEOUT):
        logger.warning("gave up waiting for another request to buffer %s", ref)
        return None
    try:
        return _fetch(ref)
    finally:
        lock.release()


def prefetch(refs: list[str]) -> None:
    """Start buffering, without waiting. Safe to call with anything."""
    if not ENABLED:
        return
    for ref in refs[:PREFETCH_AHEAD] if PREFETCH_AHEAD else []:
        if not stream_ref.is_ref(ref) or path_for(ref).exists():
            continue
        with _LOCKS_GUARD:
            if ref in _INFLIGHT:
                continue
            _INFLIGHT.add(ref)
        _POOL.submit(_prefetch_one, ref)


def _prefetch_one(ref: str) -> None:
    try:
        ensure(ref)
    finally:
        with _LOCKS_GUARD:
            _INFLIGHT.discard(ref)


def evict() -> None:
    """Keep the cache inside its size and age bounds, oldest touched first."""
    try:
        entries = [
            (path.stat().st_mtime, path.stat().st_size, path)
            for path in CACHE_DIR.glob("*.mp3")
        ]
    except OSError:
        return

    now = time.time()
    live = []
    for mtime, size, path in entries:
        if now - mtime > TTL:
            _remove(path)
        else:
            live.append((mtime, size, path))

    total = sum(size for _m, size, _p in live)
    if total <= MAX_BYTES:
        return
    for _mtime, size, path in sorted(live):
        if total <= MAX_BYTES:
            break
        if _remove(path):
            total -= size


def _remove(path: pathlib.Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def stats() -> dict:
    """What is cached, for the status panel and for answering "is this on"."""
    try:
        sizes = [p.stat().st_size for p in CACHE_DIR.glob("*.mp3")]
    except OSError:
        sizes = []
    return {
        "enabled": ENABLED,
        "tracks": len(sizes),
        "bytes": sum(sizes),
        "max_bytes": MAX_BYTES,
    }
