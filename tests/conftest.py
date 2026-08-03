"""Test fixtures.

Environment is set before importing app, because app creates its capture and
icon directories at import time and would otherwise try to write to /data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="ma-alexa-tests-")
os.environ.setdefault("CAPTURE_DIR", os.path.join(_TMP, "captures"))
# The repository's real icons, not an empty temp directory. Amazon refetches
# these on every manifest update and fails the whole update on a 304, so the
# route that serves them has behaviour worth testing, and it cannot be tested
# against a directory with nothing in it.
os.environ.setdefault(
    "ICON_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")
)
os.environ.setdefault("SIGNING_KEY", "test-signing-key-not-a-real-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("PUBLIC_BASE", "https://example.test")
os.environ.setdefault("QUEUE_STATE_DIR", os.path.join(_TMP, "queuestate"))
os.environ.setdefault("MA_CACHE_DIR", os.path.join(_TMP, "mastream"))
# Off by default, so publishing a queue in a test does not try to reach a Music
# Assistant that is not there. The cache's own suite turns it back on.
os.environ.setdefault("MA_CACHE_PREFETCH", "0")
# Never actually fetched: every Subsonic call is replaced by the fake library
# below. It is set because `subsonic.configured()` gates real behaviour now,
# including whether the endpoint agrees to serve at all, and a suite whose
# music server looks unconfigured would exercise the wrong branch throughout.
os.environ.setdefault("SUBSONIC_URL", "http://navidrome.test")
os.environ.setdefault("SUBSONIC_USER", "tester")
os.environ.setdefault("SUBSONIC_PASSWORD", "not-a-real-password")
os.environ.setdefault("OAUTH_CLIENT_ID", "ma-alexa")
os.environ.setdefault("OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("OAUTH_LINK_SECRET", "test-link-secret")
os.environ.setdefault("PREWARM", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The behaviour under test lives in `core`, which imports no web framework.
# There used to be a Flask adapter over it and a fixture handing out its test
# client; the aiohttp adapter that replaced it is exercised by
# tests/test_webserver.py against a real socket, so nothing here needs a
# framework at all.
from ma_provider import core as app_module  # noqa: E402


# --- fake library -----------------------------------------------------------

SONGS = {
    "t1": {"id": "t1", "title": "Light Year", "artist": "Gregory Alan Isakov",
           "artistId": "a1", "album": "Appaloosa Bones", "duration": 240,
           "coverArt": "cov1", "genre": "Folk"},
    "t2": {"id": "t2", "title": "That Moon Song", "artist": "Gregory Alan Isakov",
           "artistId": "a1", "album": "This Empty Northern Hemisphere",
           "duration": 200, "coverArt": "cov2", "genre": "Folk"},
    "t3": {"id": "t3", "title": "Big Black Car", "artist": "Gregory Alan Isakov",
           "artistId": "a1", "album": "The Weatherman", "duration": 300,
           "genre": "Folk"},
    "t4": {"id": "t4", "title": "Three Rounds and a Sound", "artist": "Blind Pilot",
           "artistId": "a2", "album": "3 Rounds", "duration": 210, "genre": "Folk"},
    "t5": {"id": "t5", "title": "Poor Boy", "artist": "Blind Pilot",
           "artistId": "a2", "album": "3 Rounds", "duration": 190, "genre": "Folk"},
    "t6": {"id": "t6", "title": "Naked As We Came", "artist": "Iron and Wine",
           "artistId": "a3", "album": "Our Endless Numbered Days",
           "duration": 160, "genre": "Folk"},
    "t9": {"id": "t9", "title": "Unrelated", "artist": "Someone Else",
           "artistId": "a2", "album": "Other", "duration": 100},
}

# The station fixtures. a1 is the seed; a2 and a3 are in the library; the
# fourth entry carries the negative id a Subsonic server uses for an artist it
# knows about but does not hold.
ARTIST_NAMES = {"a1": "Gregory Alan Isakov", "a2": "Blind Pilot", "a3": "Iron and Wine"}
ARTIST_ALBUMS = {"a1": ["al1", "al2"], "a2": ["al3"], "a3": ["al4"]}
ALBUM_NAMES = {"al1": "Appaloosa Bones", "al2": "The Weatherman",
               "al3": "3 Rounds", "al4": "Our Endless Numbered Days"}
SIMILAR_ARTISTS = {
    "a1": [
        {"id": "a2", "name": "Blind Pilot"},
        {"id": "a3", "name": "Iron and Wine"},
        {"id": "-1", "name": "Fleet Foxes"},
    ],
}

# Every subsonic.call made during a test, as (view, id), so a test can assert
# an expensive lookup happened once rather than once per track.
CALLS: list[tuple[str, str | None]] = []

STAR_CALLS: list[tuple[str, str]] = []

# Everything handed to the background warm pool during a test.
WARM_SUBMITS: list[tuple] = []


class RecordingPool:
    """Stand-in for the background warm pool.

    Real threads outlive the monkeypatched Navidrome and go to the network
    after teardown, which no test may do. Running the work synchronously
    instead would put it back on the request path, which is the one thing the
    pool exists to keep it off. So record it and never run it.
    """

    def submit(self, fn, *args, **kwargs):
        WARM_SUBMITS.append((fn, args, kwargs))
        return None

PLAYLISTS = [
    {"id": "p1", "name": "bedtime"},
    {"id": "p2", "name": "hype mode"},
    {"id": "p3", "name": "golden hour"},
]


@pytest.fixture(autouse=True)
def fake_subsonic(monkeypatch):
    """Replace every Navidrome call. No test touches the network."""
    sub = app_module.subsonic

    monkeypatch.setattr(sub, "song", lambda sid: SONGS[sid])
    # The artist path walks the discography: getArtist -> album_tracks per album.
    ALBUM_TRACKS = {
        "al1": [SONGS["t1"], SONGS["t2"]],
        "al2": [SONGS["t3"]],
        "al3": [SONGS["t4"], SONGS["t5"]],
        "al4": [SONGS["t6"]],
    }
    monkeypatch.setattr(sub, "album_tracks", lambda aid: ALBUM_TRACKS.get(aid, [SONGS["t1"], SONGS["t2"]]))
    monkeypatch.setattr(sub, "artist_top_songs", lambda name: [])
    CALLS.clear()

    def fake_call(view, **kw):
        CALLS.append((view, kw.get("id")))
        if view == "getArtistInfo2.view":
            # subsonic.similar_artists does the library filtering, so the fake
            # answers at the wire shape and lets the real code run.
            return {"artistInfo2": {
                "similarArtist": list(SIMILAR_ARTISTS.get(kw.get("id"), [])),
            }}
        if view == "getArtist.view":
            artist_id = kw.get("id", "a1")
            return {"artist": {
                "name": ARTIST_NAMES.get(artist_id, "Gregory Alan Isakov"),
                "album": [{"id": a, "name": ALBUM_NAMES.get(a, "Unknown")}
                          for a in ARTIST_ALBUMS.get(artist_id, ["al1", "al2"])],
            }}
        if view == "getArtists.view":
            return {"artists": {"index": [
                {"artist": [
                    {"id": "a1", "name": "Gregory Alan Isakov", "coverArt": "cov1"},
                    {"id": "a2", "name": "Someone Else"},
                ]},
            ]}}
        if view == "getAlbum.view":
            return {"album": {"name": "Appaloosa Bones", "coverArt": "cov1"}}
        return {"artist": {
            "name": "Gregory Alan Isakov",
            "album": [{"id": "al1"}, {"id": "al2"}],
        }}

    monkeypatch.setattr(sub, "call", fake_call)
    monkeypatch.setattr(sub, "playlists", lambda: list(PLAYLISTS))
    monkeypatch.setattr(sub, "playlist_tracks", lambda pid: [SONGS["t2"], SONGS["t1"]])
    monkeypatch.setattr(sub, "songs_by_genre", lambda g, count=100: [SONGS["t3"], SONGS["t1"]])
    monkeypatch.setattr(sub, "starred_songs", lambda: [SONGS["t1"]])
    monkeypatch.setattr(sub, "genres", lambda: [{"value": "Jazz"}, {"value": "Rock"}])
    monkeypatch.setattr(sub, "random_songs", lambda size=50, genre=None: [SONGS["t9"]])
    monkeypatch.setattr(
        sub, "search",
        lambda q, songs=20, albums=5, artists=5: {
            "song": [SONGS["t1"]],
            "album": [{"id": "al1", "name": "Appaloosa Bones", "artist": "Gregory Alan Isakov"}],
            "artist": [{"id": "a1", "name": "Gregory Alan Isakov"}],
        },
    )
    STAR_CALLS.clear()
    monkeypatch.setattr(sub, "star", lambda sid: STAR_CALLS.append(("star", sid)))
    monkeypatch.setattr(sub, "unstar", lambda sid: STAR_CALLS.append(("unstar", sid)))
    monkeypatch.setattr(sub, "stream_url", lambda sid, fmt="mp3", bitrate=256: f"http://nav.test/{sid}.mp3")
    monkeypatch.setattr(sub, "cover_art_url", lambda cid, size=600: f"http://nav.test/art/{cid}")

    WARM_SUBMITS.clear()
    monkeypatch.setattr(app_module, "_WARM_POOL", RecordingPool())

    app_module._QUEUE_CACHE.clear()
    app_module._DESCRIBE_CACHE.clear()
    app_module._RADIO_CACHE.clear()
    yield


@pytest.fixture
def app():
    return app_module


def directive(namespace: str, name: str, payload: dict, version: str = "1.0") -> dict:
    return {
        "header": {
            "namespace": namespace,
            "name": name,
            "messageId": "test-message-id",
            "payloadVersion": version,
        },
        "payload": payload,
    }


# --- a synchronous client over the real adapter ------------------------------


class _Response:
    """Enough of a Flask response for the tests that were written against one.

    Only the four things they actually read. Deliberately not a general
    compatibility layer: the point is that the assertions did not have to
    change when the framework underneath them did, not that the old framework
    is still emulated somewhere.
    """

    def __init__(self, status: int, body: bytes, headers) -> None:
        self.status_code = status
        self.data = body
        self.headers = headers

    def get_json(self):
        return json.loads(self.data)

    @property
    def text(self) -> str:
        return self.data.decode()


class _Client:
    """The aiohttp adapter, driven from synchronous test code.

    These tests used to post at a Flask app. That app is gone and its aiohttp
    replacement is what serves Alexa in production, so pointing them here is
    not a compatibility shim: it moves a large body of directive tests onto the
    adapter that is actually running.

    One loop and one server for the whole test, because a server per request
    would rebind a port per assertion.
    """

    def __init__(self, loop, client) -> None:
        self._loop = loop
        self._client = client

    def _call(self, method, path, **kwargs):
        if (payload := kwargs.pop("json", None)) is not None:
            kwargs["json"] = payload
        if "content_type" in kwargs:
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Content-Type"] = kwargs.pop("content_type")
        # Flask spelled the query string one way and aiohttp spells it
        # another. Translated here rather than in thirty call sites, because
        # the assertions in those tests are about the endpoint's answers and
        # never were about the client.
        if "query_string" in kwargs:
            kwargs["params"] = kwargs.pop("query_string")
        # Flask's test client does not follow redirects and aiohttp's does.
        # Defaulted back, because several tests here assert on the 302 itself:
        # the account-linking redirect carries the authorization code, and
        # following it would send that code to Amazon and assert on Amazon's
        # answer instead of ours.
        kwargs.setdefault("allow_redirects",
                          bool(kwargs.pop("follow_redirects", False)))

        async def run():
            response = await getattr(self._client, method)(path, **kwargs)
            return _Response(response.status, await response.read(),
                             response.headers)

        return self._loop.run_until_complete(run())

    def get(self, path, **kwargs):
        return self._call("get", path, **kwargs)

    def post(self, path, **kwargs):
        return self._call("post", path, **kwargs)


@pytest.fixture
def client(fake_subsonic):
    import asyncio
    import logging
    from concurrent.futures import ThreadPoolExecutor

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from ma_provider import webserver

    server = webserver.AmpereWebServer(logging.getLogger("test-ampere"))
    application = web.Application()
    application.add_routes(server._routes())
    # The pool the handlers hop through is normally created by start(), which
    # also binds a port and starts mDNS. Neither belongs in a unit test.
    server._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-web")

    loop = asyncio.new_event_loop()
    inner = TestClient(TestServer(application), loop=loop)
    loop.run_until_complete(inner.start_server())
    try:
        yield _Client(loop, inner)
    finally:
        loop.run_until_complete(inner.close())
        server._pool.shutdown(wait=False)
        loop.close()
