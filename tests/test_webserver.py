"""The aiohttp adapter: Ampere's front door when it runs inside MA.

These drive the real `AmpereWebServer` over a real socket rather than calling
its handlers, because almost everything that can go wrong in an adapter is in
the seam it is supposed to hide. A handler returning the right tuple while
aiohttp renders it as a 500 is exactly the failure this file exists to catch,
and it is invisible to a test that calls the handler directly.

Deliberately paired with the Flask suite rather than replacing it. The two
adapters must agree, because Amazon cannot tell them apart, and
`test_both_adapters_answer_the_same_way` is the check that they do.
"""

from __future__ import annotations

import io
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ma_provider import core, webserver
from conftest import directive


@pytest.fixture
async def aio(fake_subsonic, monkeypatch):
    """A live server on an ephemeral port, torn down with the test."""
    import logging

    server = webserver.AmpereWebServer(logging.getLogger("test-ampere"))
    application = web.Application()
    application.add_routes(server._routes())

    # The pool the handlers hop through is normally created in start(), which
    # also binds a port and starts mDNS. Neither belongs in a unit test, so the
    # pool is supplied directly and the rest is left alone.
    from concurrent.futures import ThreadPoolExecutor
    server._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-web")

    client = TestClient(TestServer(application))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        server._pool.shutdown(wait=False)


# --- the shapes an adapter has to render ------------------------------------


async def test_a_directive_round_trips(aio):
    resp = await aio.post("/music", json=directive(
        "Alexa.Media.Search", "GetPlayableContent", {"filters": {}}, "1.0"))
    assert resp.status == 200
    body = await resp.json()
    assert body["header"]["namespace"] == "Alexa.Media.Search"


async def test_the_root_answers_a_directive_and_a_browser(aio):
    """Both, on one path. A 405 here is a black screen in the Alexa app."""
    posted = await aio.post("/", json=directive(
        "Alexa.Media.Search", "GetPlayableContent", {"filters": {}}, "1.0"))
    assert posted.status == 200

    got = await aio.get("/")
    assert got.status == 200
    assert got.headers["Content-Type"].startswith("text/html")
    assert "Ampere" in await got.text()


async def test_a_page_carries_its_charset(aio):
    """`Page` states one content type string; aiohttp wants it in two parts."""
    resp = await aio.get("/privacy")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"


async def test_healthz_is_public_and_says_nothing_else(aio):
    resp = await aio.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


async def test_an_icon_is_sent_with_no_validators(aio):
    """Amazon's manifest validator fails the whole update on a 304.

    So the icon route must never emit an ETag or a Last-Modified, which is why
    core returns the bytes rather than a path for this one route. An adapter
    that "helpfully" served it as a file would reintroduce the bug.
    """
    resp = await aio.get("/icons/ampere-512.png")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "ETag" not in resp.headers
    assert "Last-Modified" not in resp.headers


async def test_a_path_outside_the_icon_directory_is_refused(aio):
    resp = await aio.get("/icons/../core.py")
    assert resp.status in (400, 404)


# --- signed asset routes ----------------------------------------------------


async def test_a_bad_signature_is_refused(aio):
    import time
    resp = await aio.get(f"/stream/t1/{int(time.time()) + 60}/{'0' * 32}")
    assert resp.status == 403


async def test_a_non_numeric_expiry_is_refused_rather_than_raising(aio):
    """Flask's route converter parsed this; aiohttp hands over the raw string.

    Without the explicit int() the signature check would raise on the way in
    and the adapter would answer 500 to something that is simply not signed.
    """
    resp = await aio.get("/stream/t1/not-a-number/" + "0" * 32)
    assert resp.status == 403


async def test_a_signed_stream_is_piped_through(aio, monkeypatch):
    seen = []

    def fake_fetch(url, range_header=None, default_content_type=""):
        seen.append((url, range_header))
        return core.Upstream(200, {"Content-Type": "audio/mpeg"},
                             io.BytesIO(b"the audio"))

    monkeypatch.setattr(core, "fetch_upstream", fake_fetch)
    url, expires = core.signed_url("stream", "t1")
    resp = await aio.get(url[url.index("/stream/"):])

    assert resp.status == 200
    assert await resp.read() == b"the audio"
    assert seen and seen[0][0].endswith("t1.mp3")


async def test_a_range_header_reaches_the_upstream(aio, monkeypatch):
    """The room-to-room move and the scrub both arrive as a Range."""
    seen = []

    def fake_fetch(url, range_header=None, default_content_type=""):
        seen.append(range_header)
        return core.Upstream(206, {"Content-Type": "audio/mpeg",
                                   "Content-Range": "bytes 10-99/100"},
                             io.BytesIO(b"tail"))

    monkeypatch.setattr(core, "fetch_upstream", fake_fetch)
    url, _expires = core.signed_url("stream", "t1")
    resp = await aio.get(url[url.index("/stream/"):],
                         headers={"Range": "bytes=10-"})

    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 10-99/100"
    assert seen == ["bytes=10-"]


async def test_a_buffered_track_is_served_as_a_real_file(aio, monkeypatch, tmp_path):
    """A LocalFile answer must become a 206-capable response.

    This is the whole reason `core` hands back a path instead of bytes: seeking
    a Music Assistant track depends on the adapter's own range handling.
    """
    from ma_provider import mastream_cache, stream_ref

    ref = stream_ref.encode_ref("deezer://track/3135556")
    audio = tmp_path / "buffered.mp3"
    audio.write_bytes(b"0123456789" * 10)

    monkeypatch.setattr(mastream_cache, "ENABLED", True)
    monkeypatch.setattr(mastream_cache, "path_for", lambda _r: audio)
    monkeypatch.setattr(mastream_cache, "ensure", lambda _r: audio)

    _url, expires = core.signed_url("mastream", ref)
    sig = core.sign("mastream", ref, expires)
    resp = await aio.get(f"/mastream/{ref}/{expires}/{sig}",
                         headers={"Range": "bytes=5-14"})

    assert resp.status == 206, "a scrub must get a partial response"
    assert await resp.read() == b"5678901234"


# --- admin plane ------------------------------------------------------------


async def test_captures_needs_a_token(aio):
    assert (await aio.get("/captures")).status == 401


async def test_diag_needs_a_token(aio):
    assert (await aio.get("/diag")).status == 401


async def test_captures_accepts_the_token_from_a_local_peer(aio):
    resp = await aio.get("/captures", headers={"X-Admin-Token": "test-admin-token"})
    assert resp.status == 200


# --- OAuth ------------------------------------------------------------------


async def test_authorize_refuses_a_foreign_redirect(aio):
    resp = await aio.get("/oauth/authorize", params={
        "client_id": "ma-alexa", "response_type": "code",
        "redirect_uri": "https://evil.example.com/steal",
    })
    assert resp.status == 400


async def test_the_link_flow_redirects_with_a_code(aio):
    resp = await aio.post("/oauth/authorize", data={
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
        "state": "abc", "passphrase": "test-link-secret",
    }, allow_redirects=False)
    assert resp.status == 302
    location = resp.headers["Location"]
    assert location.startswith("https://pitangui.amazon.com/api/skill/link/V1?")
    assert "state=abc" in location


async def test_a_wrong_passphrase_is_refused(aio):
    resp = await aio.post("/oauth/authorize", data={
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
        "state": "abc", "passphrase": "wrong",
    }, allow_redirects=False)
    assert resp.status == 403


# --- the handoff endpoint ---------------------------------------------------


async def test_publishing_a_queue_needs_the_token(aio):
    resp = await aio.post("/queue", json={"tracks": ["t1"]})
    assert resp.status == 401


async def test_a_published_queue_comes_back_by_token(aio, tmp_path, monkeypatch):
    from ma_provider import queue_api

    monkeypatch.setattr(queue_api, "STATE_DIR", tmp_path / "external")
    queue_api.STATE_DIR.mkdir(parents=True, exist_ok=True)

    headers = {"X-Admin-Token": "test-admin-token"}
    published = await aio.post("/queue", json={"tracks": ["t1", "t2"]},
                               headers=headers)
    assert published.status == 200
    content_id = (await published.json())["content_id"]

    token = content_id.split(":", 1)[1]
    shown = await aio.get(f"/queue/{token}", headers=headers)
    assert shown.status == 200
    assert (await shown.json())["count"] == 2


# --- the two adapters must not drift ----------------------------------------


async def test_both_adapters_answer_the_same_way(aio, client):
    """One core, two front doors, and Amazon cannot tell them apart.

    Compared on the routes whose answers are deterministic. Anything carrying a
    messageId or a signed URL differs by construction, so this pins status,
    content type and body for the ones that do not.
    """
    for path in ("/healthz", "/privacy", "/terms", "/"):
        flask_resp = client.get(path)
        aio_resp = await aio.get(path)

        assert aio_resp.status == flask_resp.status_code, path
        content_type = aio_resp.headers["Content-Type"].split(";")[0]
        assert content_type == flask_resp.headers["Content-Type"].split(";")[0], path

        if content_type == "application/json":
            # Compared as values, not as bytes. The two libraries disagree
            # about spacing after a colon and about a trailing newline, and
            # Amazon parses these rather than diffing them.
            assert await aio_resp.json() == json.loads(flask_resp.data), path
        else:
            assert await aio_resp.read() == flask_resp.data, path


async def test_both_adapters_refuse_the_admin_plane_alike(aio, client):
    for path in ("/captures", "/diag"):
        assert (await aio.get(path)).status == client.get(path).status_code == 401


async def test_both_adapters_shape_a_directive_alike(aio, client):
    body = directive("Alexa.Media.PlayQueue", "SetShuffle",
                     {"queueId": "q1", "shuffle": True}, "1.0")

    flask_body = json.loads(client.post("/music", json=body).data)
    aio_body = await (await aio.post("/music", json=body)).json()

    # messageId is a fresh uuid per response, so it is the one field that must
    # differ rather than match.
    assert flask_body["header"]["messageId"] != aio_body["header"]["messageId"]
    for part in ("namespace", "name", "payloadVersion"):
        assert flask_body["header"][part] == aio_body["header"][part]
    assert flask_body["payload"] == aio_body["payload"]
