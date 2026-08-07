"""Resolving a catalog content id to playable tracks out of Music Assistant.

The Subsonic resolver in `core.resolve_tracks` turns a content id such as
``al:1234`` into Navidrome song records the bridge streams straight through.
This is its Music Assistant twin: given the same kind of content id, but one
whose native id is a Music Assistant *library* id (marked with
``stream_ref.MA_ID_PREFIX``), it returns records shaped exactly like the
Subsonic ones except that the audio is named by an MA reference (`ma_ref`)
rather than a Subsonic song id. `core.build_item` already knows both shapes:
an `ma_ref` streams through the `/ma_alexa_stream/<ref>.mp3` route, so a queue
resolved here plays from whatever provider MA has the track on (Plex, Tidal,
Spotify, a local folder), not only the Subsonic server.

Everything here runs in the bridge's worker thread, the same as the Subsonic
resolver, so each Music Assistant controller coroutine is bounced onto MA's
event loop with `run_coroutine_threadsafe`. `resolve_tracks` caches per content
id, so the bounce happens once per queue rather than once per track.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import TYPE_CHECKING, Any

from .stream_ref import MA_ID_PREFIX, encode_ref, is_live

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

logger = logging.getLogger(__name__)

# A whole-library or favorites pool is bounded the way the Subsonic path bounds
# `random_songs(50)`: a queue is re-derived from its content id on every
# GetNextItem, so the pool must be a fixed, deterministically ordered slice
# rather than an unbounded or randomly ordered read. The shuffle a listener
# hears is applied on top, per queue, by `queuestate`.
LIBRARY_SAMPLE = 200
FAVORITES_LIMIT = 500

# How a station is built, tuned for Amazon's sub-second Initiate budget.
# similar_tracks is track-seeded, and MA's OpenSubsonic implementation fetches
# lyrics for every track it returns, so the cost is roughly one Navidrome round
# trip per (seed x per-seed) track. A first attempt with three seeds and a
# limit of 144 took 88 seconds and Alexa gave up. One seed and a small fetch
# keeps a station to a couple of seconds; it need not be large, because a
# station is endless and Amazon pulls more through the queue's own continuation.
RADIO_SEED_TRACKS = 1
RADIO_SIMILAR_PER_SEED = 15
RADIO_POOL_CAP = 40


def is_ma_native(ident: str) -> bool:
    """Whether a content id's native part names a Music Assistant library item."""
    return ident.startswith(MA_ID_PREFIX)


def library_id(ident: str) -> str:
    """The bare library id behind a marked native id (``ma-1234`` -> ``1234``)."""
    return ident[len(MA_ID_PREFIX):] if ident.startswith(MA_ID_PREFIX) else ident


def streamable_uri(track: Any, mass: Any = None) -> str:
    """
    A provider uri for a track that the stream route can actually resolve.

    A library item's own uri is ``library://track/<id>``, and "library" is not a
    streaming provider: the route decodes a ref to ``get_provider(<id>)`` and a
    library uri has nowhere to resolve. So a real provider mapping is chosen
    instead -- highest quality first, only ones marked available, and (when a
    live MA is supplied) only providers that are actually loaded -- exactly as MA
    picks a version to play. Falls back to the item's own uri when it already
    names a provider (a playlist track comes straight from its provider), and to
    empty when nothing is streamable, which makes the caller drop the track.
    """
    mappings = getattr(track, "provider_mappings", None) or ()
    for m in sorted(mappings, key=lambda x: getattr(x, "quality", 0) or 0,
                    reverse=True):
        if not getattr(m, "available", False):
            continue
        instance = getattr(m, "provider_instance", "") or getattr(m, "provider_domain", "")
        item_id = getattr(m, "item_id", "")
        if not instance or not item_id:
            continue
        if mass is not None and mass.get_provider(instance) is None:
            continue
        return f"{instance}://track/{item_id}"

    uri = getattr(track, "uri", "") or ""
    if uri and "://" in uri and not uri.startswith("library://"):
        return uri
    return ""


def song_from_track(track: Any, mass: Any = None) -> dict | None:
    """
    Map a Music Assistant track to the song record `core.build_item` expects.

    Returns None for a track with no streamable provider uri, which cannot be
    referenced and so cannot be played; the caller drops it.
    """
    uri = streamable_uri(track, mass)
    if not uri:
        return None

    ref = encode_ref(uri)
    artists = getattr(track, "artists", None) or ()
    album = getattr(track, "album", None)
    duration = getattr(track, "duration", 0) or 0

    song: dict[str, Any] = {
        # Only ever used for the Item's stream element id and to filter empty
        # records; the ma_ref is what actually names the audio. The uri is a
        # stable unique string, which is all this has to be.
        "id": uri,
        "ma_ref": ref,
        "title": getattr(track, "name", "") or uri,
        "artist": ", ".join(
            a.name for a in artists if getattr(a, "name", "")
        ),
        "album": getattr(album, "name", "") or "",
        # A station has no length; a track carries its own. is_live reads this
        # off the reference so an endless MA stream is never given a progress bar.
        "duration": 0 if is_live(ref) else int(duration or 0),
    }

    # The first credited artist, marked, so a continuation (radio or artist
    # after-content) can seed from an MA track. Without it the seed carries no
    # artistId and `core.continuation_content` falls back to a whole-library
    # shuffle. The marker routes `rad:`/`ar:` back through MA, and the id is the
    # same library artist db id `catalog_sync.artist_ref` keys the station and
    # artist entities on, so a station reached this way resolves to itself.
    first = artists[0] if artists else None
    if first is not None and getattr(first, "item_id", None) is not None:
        song["artistId"] = f"{MA_ID_PREFIX}{first.item_id}"

    # Album art. A streaming service's own CDN url is public and passes straight
    # through. Anything else -- a Subsonic cover on the tailnet, a local file --
    # Amazon cannot reach, so its stable imageproxy id is carried instead and the
    # bridge proxies the bytes (core.serve_maart). Either way Alexa gets a cover
    # rather than the fallback icon. compute_image_id is thread-safe and also
    # registers the id so the proxy can resolve it back.
    image = getattr(track, "image", None)
    if image is not None and getattr(image, "path", ""):
        if getattr(image, "remotely_accessible", False):
            song["art_url"] = image.path
        elif mass is not None and getattr(image, "provider", ""):
            with contextlib.suppress(Exception):
                song["art_id"] = mass.metadata.compute_image_id(
                    image.provider, image.path)

    return song


def resolve(kind: str, ident: str, mass: MusicAssistant,
            loop: asyncio.AbstractEventLoop) -> list[dict]:
    """
    Resolve a marked catalog content id to MA song records.

    :param kind: The short content-id kind (``ar``/``al``/``tr``/``pl``/``gen``).
    :param ident: The native id, still carrying the ``ma-`` marker.
    :param mass: The running Music Assistant instance.
    :param loop: MA's event loop; controller calls are bounced onto it.
    """
    db_id = library_id(ident)

    def gather(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        tracks = _fetch(kind, db_id, mass, gather)
    except Exception:
        logger.exception("MA resolve failed for %s:%s", kind, ident)
        return []

    songs = [song_from_track(t, mass) for t in tracks if t]
    return [s for s in songs if s]


_DESCRIBE_CONTROLLERS = {
    "ar": "artists", "al": "albums", "tr": "tracks",
    "pl": "playlists", "gen": "genres",
}


def describe(prefix: str, db_id: str, mass: MusicAssistant,
             loop: asyncio.AbstractEventLoop) -> tuple[str | None, str | None]:
    """
    The real display name for a marked catalog entity, from MA's library.

    Without this a content id resolves to its bare library id ("ma-59"), which is
    what Alexa then announces and shows as the source. Art is not returned here:
    an MA item has no public cover url Amazon can fetch, so the caller keeps its
    own fallback until the bridge can proxy the image.
    """
    attr = _DESCRIBE_CONTROLLERS.get(prefix)
    if attr is None:
        return None, None
    controller = getattr(mass.music, attr, None)
    if controller is None:
        return None, None
    try:
        item = asyncio.run_coroutine_threadsafe(
            controller.get_library_item(db_id), loop).result()
    except Exception:
        logger.exception("MA describe failed for %s:%s", prefix, db_id)
        return None, None
    return (getattr(item, "name", None) or None), None


def favorites(mass: MusicAssistant,
              loop: asyncio.AbstractEventLoop) -> list[dict]:
    """The library's favorited tracks, for the `star` intent in MA mode."""
    return _library(mass, loop, favorite=True, limit=FAVORITES_LIMIT)


def library_sample(mass: MusicAssistant,
                   loop: asyncio.AbstractEventLoop) -> list[dict]:
    """A bounded, stable slice of the whole library, for `rnd:all` in MA mode."""
    return _library(mass, loop, favorite=None, limit=LIBRARY_SAMPLE)


def subsonic_artist_id(ident: str, mass: MusicAssistant,
                       loop: asyncio.AbstractEventLoop) -> str:
    """
    The OpenSubsonic provider id for a marked MA library artist, or "".

    The fast station path builds from Subsonic's similar-artists (getArtistInfo2)
    plus plain library listings, none of which fetch lyrics, so a station stays
    inside Amazon's Initiate budget. MA's similar_tracks has the same last.fm
    data but pays a per-track lyrics fetch that pushed a station past a minute.
    Only an opensubsonic mapping qualifies, because that is the server Music Assistant's
    own Subsonic client is pointed at, so the id it returns is one that client
    can resolve. A non-Subsonic artist returns "" and the caller uses the
    provider-agnostic MA path instead.
    """
    db_id = library_id(ident)

    def gather(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        artist = gather(mass.music.artists.get_library_item(db_id))
    except Exception:
        logger.exception("MA artist lookup failed for %s", ident)
        return ""
    for mapping in getattr(artist, "provider_mappings", None) or ():
        if (getattr(mapping, "provider_domain", "") == "opensubsonic"
                and getattr(mapping, "item_id", "")):
            return mapping.item_id
    return ""


def radio(ident: str, mass: MusicAssistant, loop: asyncio.AbstractEventLoop,
          *, seed_tracks: int = RADIO_SEED_TRACKS,
          per_seed: int = RADIO_SIMILAR_PER_SEED,
          cap: int = RADIO_POOL_CAP) -> tuple[list[dict], bool]:
    """
    A provider-agnostic station for a marked artist id.

    Returns ``(songs, degraded)``. `degraded` is True when nothing beyond the
    seed artist itself could be found, which the caller uses to decline caching:
    a station that fell back to the seed artist alone is not a station, and
    caching one would pin the failure for the life of the process, the same way
    the Subsonic path refuses to cache a station with a single artist.

    :param ident: The marked artist id (``ma-<db_id>``).
    :param mass: The running Music Assistant instance.
    :param loop: MA's event loop; controller calls are bounced onto it.
    :param seed_tracks: How many of the artist's tracks to seed similar from.
    :param per_seed: How many similar tracks to fetch per seed. Small on purpose:
        each returned track costs a lyrics fetch inside MA, so this bounds the
        Initiate latency.
    :param cap: Cap on the returned pool.
    """
    db_id = library_id(ident)

    def gather(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        artist_tracks = gather(mass.music.artists.tracks(db_id, "library"))
    except Exception:
        logger.exception("MA radio seed lookup failed for %s", ident)
        return [], True
    if not artist_tracks:
        return [], True

    pool, seen = [], set()
    for seed in _sample(artist_tracks, seed_tracks, db_id):
        seed_id = getattr(seed, "item_id", None)
        if seed_id is None:
            continue
        try:
            # Seeded from the library track; similar_tracks walks its provider
            # mappings, and allow_lookup lets a provider with no native similar
            # (a local folder) still get a station from MA's cross-provider
            # metadata lookup. OpenSubsonic answers natively, so the lookup is
            # not reached here; the cost is the per-track lyrics fetch, which
            # per_seed bounds.
            similar = gather(mass.music.tracks.similar_tracks(
                seed_id, "library", limit=per_seed, allow_lookup=True))
        except Exception:
            logger.exception("MA similar_tracks failed for seed %s", seed_id)
            continue
        for track in similar:
            key = getattr(track, "uri", None) or getattr(track, "item_id", None)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            pool.append(track)

    songs = [s for s in (song_from_track(t, mass) for t in pool if t) if s][:cap]
    if songs:
        return songs, False

    # Floor: the seed artist's own tracks, so the request still plays something
    # coherent. Reported degraded so the caller does not cache a non-station.
    floor = [s for s in (song_from_track(t, mass) for t in artist_tracks if t)
             if s][:cap]
    return floor, True


# --- private ----------------------------------------------------------------


def _sample(items: list, n: int, seed_key) -> list:
    """A deterministic sample, so the same station id resolves to the same
    seeds on every GetNextItem and the pool stays cacheable."""
    if len(items) <= n:
        return list(items)
    picked = random.Random(str(seed_key)).sample(range(len(items)), n)
    return [items[i] for i in sorted(picked)]


def _fetch(kind: str, db_id: str, mass: MusicAssistant, gather) -> list:
    if kind == "tr":
        return [gather(mass.music.tracks.get_library_item(db_id))]
    if kind == "al":
        return gather(mass.music.albums.tracks(db_id, "library"))
    if kind == "ar":
        return gather(mass.music.artists.tracks(db_id, "library"))
    if kind == "pl":
        return gather(_playlist_tracks(mass, db_id))
    if kind == "gen":
        return gather(mass.music.tracks.library_items(
            genre=[int(db_id)], summary=False, limit=LIBRARY_SAMPLE))
    return []


async def _playlist_tracks(mass: MusicAssistant, db_id: str) -> list:
    # playlists.tracks is an async generator (playlist tracks are not stored in
    # the db, they are streamed from the provider), so it is drained here into
    # the flat list every other kind already returns.
    out = []
    async for track in mass.music.playlists.tracks(db_id, "library"):
        out.append(track)
    return out


def _library(mass: MusicAssistant, loop: asyncio.AbstractEventLoop,
             *, favorite: bool | None, limit: int) -> list[dict]:
    def gather(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        # A stable order (the default sort_name), not random: the queue is
        # re-derived per GetNextItem, so a random read would reshuffle
        # mid-playback. The listener's shuffle is applied over this by queuestate.
        tracks = gather(mass.music.tracks.library_items(
            favorite=favorite, summary=False, limit=limit, order_by="sort_name"))
    except Exception:
        logger.exception("MA library read failed (favorite=%s)", favorite)
        return []

    songs = [song_from_track(t, mass) for t in tracks if t]
    return [s for s in songs if s]
