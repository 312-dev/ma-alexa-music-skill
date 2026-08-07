"""Runtime patch making Music Assistant's OpenSubsonic playlist resolution fast.

Upstream ``OpenSonicProvider.get_playlist_tracks`` fetches the full album
(``getAlbum`` + ``getAlbumInfo2``) and lyrics (``getLyricsBySongId``) for every
entry inside the enumeration loop. Because playlist tracks are always resolved
from the provider, enqueuing a large playlist fans a single ``getPlaylist`` call
out into thousands of serial Subsonic requests -- about two minutes for a
718-track playlist on this deployment.

None of that is needed to enqueue a track: ``parse_track`` builds the album
reference from the playlist entry itself, and lyrics are fetched on demand at
playback via ``get_track``. This module swaps in a version that does the single
``getPlaylist`` call and parses entries locally.

MA ships as the official (read-only) image here while Music Assistant is bind-mounted, so
this is an in-process stopgap for the upstream fix (music-assistant/server:
"Resolve OpenSubsonic playlist tracks without per-track album and lyrics
fetches"). Once the MA image on this host carries that fix, ``already_fixed()``
returns True and ``apply()`` declines, so the patch retires itself.

Note: the replacement is a plain coroutine (no ``@use_cache``); a cold resolve is
now ~1s, and dropping the 3h cache means playlist edits are reflected on the next
play. That is an acceptable trade for a stopgap.
"""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from logging import Logger

# the original method, kept so unload() can put it back
_ORIGINAL: Any = None


async def _fast_get_playlist_tracks(
    self: Any, prov_playlist_id: str, page: int = 0
) -> list[Any]:
    """Resolve playlist tracks with one getPlaylist call and no per-track fetches."""
    from libopensonic.errors import DataNotFoundError, ParameterError
    from music_assistant_models.errors import MediaNotFoundError

    from music_assistant.providers.opensubsonic.parsers import parse_track

    result: list[Any] = []
    if page > 0:
        # paging not supported; the whole list is returned at once
        return result
    started = time.monotonic()
    try:
        sonic_playlist = await self.conn.get_playlist(prov_playlist_id)
    except (ParameterError, DataNotFoundError) as err:
        raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
    if not sonic_playlist.entry:
        return result
    for index, sonic_song in enumerate(sonic_playlist.entry, 1):
        self._set_loudness(sonic_song)
        track = parse_track(self.logger, self.instance_id, sonic_song)
        track.position = index
        result.append(track)
    self.logger.info(
        "subsonic playlist patch: resolved playlist %s in %.2fs (%d tracks)",
        prov_playlist_id,
        time.monotonic() - started,
        len(result),
    )
    return result


def _unwrapped(func: Any) -> Any:
    """Follow ``functools.wraps`` so we read the real function body, not a decorator."""
    return getattr(func, "__wrapped__", func)


def _provider_cls() -> Any:
    from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

    return OpenSonicProvider


def already_fixed() -> bool:
    """True if the installed MA no longer fetches lyrics per track in get_playlist_tracks."""
    try:
        src = inspect.getsource(_unwrapped(_provider_cls().get_playlist_tracks))
    except Exception:
        # no source -> assume not fixed; our equivalent patch is safe to apply anyway
        return False
    return "get_track_lyrics" not in src


def is_applied() -> bool:
    """True if our fast replacement is currently installed."""
    try:
        return _provider_cls().get_playlist_tracks is _fast_get_playlist_tracks
    except Exception:
        return False


def apply(logger: Logger) -> bool:
    """Install the fast get_playlist_tracks on OpenSonicProvider.

    :param logger: Logger for a single line recording what happened.
    :returns: True if the patch was installed, False if it was skipped.
    """
    global _ORIGINAL
    try:
        cls = _provider_cls()
    except Exception:
        logger.debug("subsonic playlist patch: opensubsonic not present, nothing to do")
        return False
    if cls.get_playlist_tracks is _fast_get_playlist_tracks:
        return False  # already installed by us
    if already_fixed():
        logger.info("subsonic playlist patch: MA already resolves playlists lazily, skipping")
        return False
    _ORIGINAL = cls.get_playlist_tracks
    cls.get_playlist_tracks = _fast_get_playlist_tracks
    logger.info("subsonic playlist patch: applied (single getPlaylist per enqueue)")
    return True


def restore(logger: Logger) -> None:
    """Put the original get_playlist_tracks back if we replaced it."""
    global _ORIGINAL
    if _ORIGINAL is None:
        return
    try:
        _provider_cls().get_playlist_tracks = _ORIGINAL
        logger.info("subsonic playlist patch: restored original get_playlist_tracks")
    except Exception:
        logger.debug("subsonic playlist patch: could not restore, provider gone")
    finally:
        _ORIGINAL = None
