"""Minimal Subsonic/OpenSubsonic client for Navidrome.

Navidrome lives on the tailnet and is not internet-routable, so everything the
skill exposes to Amazon is proxied back through this service. That is a feature
rather than a workaround: it lets us control URL lifetime and transcoding
rather than handing Amazon a credentialed, permanent Navidrome link.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import urllib.parse
import urllib.request
import json
from typing import Any

BASE = os.environ.get("SUBSONIC_URL", "http://100.93.15.8:4533").rstrip("/")
USER = os.environ.get("SUBSONIC_USER", "")
PASSWORD = os.environ.get("SUBSONIC_PASSWORD", "")
CLIENT = "ma-alexa-skill"
API_VERSION = "1.16.1"

TIMEOUT = float(os.environ.get("SUBSONIC_TIMEOUT", "6"))


def _auth_params() -> dict[str, str]:
    """Subsonic token auth: t = md5(password + salt), fresh salt per call."""
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{PASSWORD}{salt}".encode()).hexdigest()
    return {
        "u": USER,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT,
        "f": "json",
    }


def build_url(view: str, **params: Any) -> str:
    """Build a fully-authenticated Subsonic URL."""
    query = _auth_params()
    query.update({k: str(v) for k, v in params.items() if v is not None})
    return f"{BASE}/rest/{view}?{urllib.parse.urlencode(query)}"


def call(view: str, **params: Any) -> dict:
    """Call a JSON Subsonic endpoint and return the inner subsonic-response."""
    with urllib.request.urlopen(build_url(view, **params), timeout=TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    inner = body.get("subsonic-response", {})
    if inner.get("status") != "ok":
        err = inner.get("error", {})
        raise RuntimeError(f"subsonic error {err.get('code')}: {err.get('message')}")
    return inner


def search(query: str, *, songs: int = 20, albums: int = 5, artists: int = 5) -> dict:
    return call(
        "search3.view",
        query=query,
        songCount=songs,
        albumCount=albums,
        artistCount=artists,
    ).get("searchResult3", {})


def album_tracks(album_id: str) -> list[dict]:
    return call("getAlbum.view", id=album_id).get("album", {}).get("song", [])


def artist_albums(artist_id: str) -> list[dict]:
    return call("getArtist.view", id=artist_id).get("artist", {}).get("album", [])


def song(song_id: str) -> dict:
    return call("getSong.view", id=song_id).get("song", {})


def random_songs(size: int = 50, genre: str | None = None) -> list[dict]:
    return (
        call("getRandomSongs.view", size=size, genre=genre)
        .get("randomSongs", {})
        .get("song", [])
    )


def artist_top_songs(artist_name: str, count: int = 50) -> list[dict]:
    return (
        call("getTopSongs.view", artist=artist_name, count=count)
        .get("topSongs", {})
        .get("song", [])
    )


def stream_url(song_id: str, *, fmt: str = "mp3", bitrate: int = 256) -> str:
    """Transcoded stream URL.

    Alexa's AudioPlayer does not accept FLAC, so we always transcode. Navidrome
    streams chunked with no Content-Length when transcoding, which Alexa
    tolerates.
    """
    return build_url("stream.view", id=song_id, format=fmt, maxBitRate=bitrate)


def cover_art_url(cover_id: str, size: int = 600) -> str:
    return build_url("getCoverArt.view", id=cover_id, size=size)
