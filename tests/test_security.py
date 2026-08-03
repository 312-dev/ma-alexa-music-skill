"""Signed asset URLs, admin routes and the OAuth provider.

Navidrome is tailnet-only and its credentials never leave the box, so every
asset Amazon fetches is proxied behind an expiring HMAC. These tests pin that
the signature actually gates access.
"""

from __future__ import annotations

import io
import time


# --- signed stream / art URLs ----------------------------------------------


def test_signature_roundtrip(app):
    url, expires = app.signed_url("stream", "t1")
    sig = url.rsplit("/", 1)[1]
    assert app.verify("stream", "t1", expires, sig) is True


def test_tampered_signature_rejected(app):
    _url, expires = app.signed_url("stream", "t1")
    assert app.verify("stream", "t1", expires, "0" * 32) is False


def test_signature_is_bound_to_the_track(app):
    """A signature for one track must not unlock another."""
    url, expires = app.signed_url("stream", "t1")
    sig = url.rsplit("/", 1)[1]
    assert app.verify("stream", "t2", expires, sig) is False


def test_signature_is_bound_to_the_kind(app):
    """A stream signature must not work on the art route."""
    url, expires = app.signed_url("stream", "t1")
    sig = url.rsplit("/", 1)[1]
    assert app.verify("art", "t1", expires, sig) is False


def test_expired_signature_rejected(app):
    past = int(time.time()) - 10
    sig = app.sign("stream", "t1", past)
    assert app.verify("stream", "t1", past, sig) is False


def test_expiry_cannot_be_extended_without_resigning(app):
    url, expires = app.signed_url("stream", "t1")
    sig = url.rsplit("/", 1)[1]
    assert app.verify("stream", "t1", expires + 86400, sig) is False


def test_stream_route_rejects_bad_signature(client):
    resp = client.get(f"/stream/t1/{int(time.time()) + 60}/{'0' * 32}")
    assert resp.status_code == 403


def test_art_route_rejects_bad_signature(client):
    resp = client.get(f"/art/cov1/{int(time.time()) + 60}/{'0' * 32}")
    assert resp.status_code == 403


# --- admin routes -----------------------------------------------------------


def test_captures_requires_token(client):
    assert client.get("/captures").status_code == 401


def test_captures_accepts_correct_token(client):
    resp = client.get("/captures", headers={"X-Admin-Token": "test-admin-token"})
    assert resp.status_code == 200


def test_diag_requires_token(client):
    assert client.get("/diag").status_code == 401


def test_healthz_is_public_but_leaks_nothing(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_landing_page_serves_html(client):
    """A 405 here renders as a black screen in the Alexa app."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"]


# --- OAuth ------------------------------------------------------------------


def test_authorize_rejects_foreign_redirect(client):
    resp = client.get("/oauth/authorize", query_string={
        "client_id": "ma-alexa", "response_type": "code",
        "redirect_uri": "https://evil.example.com/steal",
    })
    assert resp.status_code == 400


def test_authorize_rejects_unknown_client(client):
    resp = client.get("/oauth/authorize", query_string={
        "client_id": "not-us", "response_type": "code",
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
    })
    assert resp.status_code == 400


def test_authorize_renders_for_amazon_redirect(client):
    resp = client.get("/oauth/authorize", query_string={
        "client_id": "ma-alexa", "response_type": "code",
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
        "state": "abc",
    })
    assert resp.status_code == 200


def test_wrong_passphrase_rejected(client):
    resp = client.post("/oauth/authorize", data={
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
        "state": "abc", "passphrase": "wrong",
    })
    assert resp.status_code == 403


def test_full_authorization_code_flow(client):
    resp = client.post("/oauth/authorize", data={
        "redirect_uri": "https://pitangui.amazon.com/api/skill/link/V1",
        "state": "abc", "passphrase": "test-link-secret",
    })
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("https://pitangui.amazon.com/api/skill/link/V1?")
    assert "state=abc" in location

    code = location.split("code=")[1].split("&")[0]
    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": "ma-alexa", "client_secret": "test-client-secret",
    })
    assert token.status_code == 200
    body = token.get_json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"] and body["refresh_token"]


def test_token_rejects_bad_client_secret(client):
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "x",
        "client_id": "ma-alexa", "client_secret": "nope",
    })
    assert resp.status_code == 401


def test_token_rejects_forged_code(client):
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "forged.signature",
        "client_id": "ma-alexa", "client_secret": "test-client-secret",
    })
    assert resp.status_code == 400


def test_refresh_token_grant(client):
    from ma_provider import oauth
    refresh = oauth.mint("refresh", 3600, sub="owner")
    resp = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": "ma-alexa", "client_secret": "test-client-secret",
    })
    assert resp.status_code == 200


def test_access_token_not_accepted_as_refresh_token(client):
    """Token kinds must not be interchangeable."""
    from ma_provider import oauth
    access = oauth.mint("access", 3600, sub="owner")
    resp = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": access,
        "client_id": "ma-alexa", "client_secret": "test-client-secret",
    })
    assert resp.status_code == 400


# --- the mechanism room-to-room transfer depends on -------------------------
#
# Measured 2026-08-02. Moving audio between Echoes produces no directive at
# all: Amazon re-forms the speaker cluster in its own audio layer and has the
# new master re-request the URL the previous one was already playing, with a
# Range header, mid-track. The skill is never asked to do anything. So these
# three properties of a stream URL are not conveniences, they are the whole
# multi-room feature, and nothing else in the suite would notice them break.


def test_a_stream_url_is_not_bound_to_whichever_device_asked_first(
        client, fake_subsonic, monkeypatch):
    """The second Echo presents the identical URL and must be served."""
    from ma_provider import core as app_module

    seen = []

    def fake_fetch(upstream, range_header=None, default_content_type=""):
        seen.append(upstream)
        return app_module.Upstream(200, {"Content-Type": "audio/mpeg"},
                                   io.BytesIO(b"audio"))

    monkeypatch.setattr(app_module, "fetch_upstream", fake_fetch)
    url, _expires = app_module.signed_url("stream", "t1")
    path = url[url.index("/stream/"):]

    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 200, "a transfer replays the same URL"
    assert len(seen) == 2


def test_a_range_request_is_forwarded_and_its_answer_preserved(
        client, fake_subsonic, monkeypatch):
    """The joining speaker asks for the middle of the track, not the start.

    A 200 with the whole file here would restart the song in every room, which
    is what a transfer must not do.
    """
    from ma_provider import core as app_module

    class FakeUpstream:
        status = 206
        headers = {"Content-Type": "audio/mpeg", "Accept-Ranges": "bytes",
                   "Content-Range": "bytes 500-999/1000", "Content-Length": "500"}

        def read(self, *_):
            return b""

        def __iter__(self):
            return iter([b"tail"])

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["range"] = req.get_header("Range")
        return FakeUpstream()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    url, _expires = app_module.signed_url("stream", "t1")
    path = url[url.index("/stream/"):]

    resp = client.get(path, headers={"Range": "bytes=500-"})
    assert sent["range"] == "bytes=500-", "the Range must reach Navidrome"
    assert resp.status_code == 206, "a 200 here restarts the track in every room"
    assert resp.headers["Content-Range"] == "bytes 500-999/1000"
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_a_url_outlives_any_plausible_track(app):
    """A transfer re-requests a URL minted when the track began.

    Expiry has to clear the length of a track by a wide margin or a move near
    the end of a long one would 403 into silence.
    """
    _url, expires = app.signed_url("stream", "t1")
    assert expires - time.time() > 3600, "an hour is the floor, not the target"
