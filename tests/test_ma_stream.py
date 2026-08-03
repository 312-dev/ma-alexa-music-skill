"""Serving a track that lives in Music Assistant rather than on Subsonic.

Phase 2 of ma_provider/PLAN.md. The queue Alexa is handed can now mix two kinds
of track, and these are the tests for what the bridge does with the new kind:
which URL it builds, what it refuses to build one for, and which controls it
declines to offer because that source cannot honour them.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

import app as app_module
import queue_api
from ma_provider import stream_ref

SPOTIFY = "spotify://track/4uLU6hMCjMI75M1A2tKUQC"
REF = stream_ref.encode_ref(SPOTIFY)


def ma_song(**extra):
    """A published record as queue_api stores one for an MA track."""
    song = {
        "id": f"{queue_api.MA_ID_PREFIX}{REF}",
        "ma_ref": REF,
        "title": "Dancing On My Own",
        "artist": "Robyn",
        "album": "Body Talk",
        "duration": 293,
    }
    song.update(extra)
    return song


# --- which URL Alexa is handed ---------------------------------------------


def test_a_music_assistant_track_is_streamed_from_the_music_assistant_route(app):
    item = app.build_item(ma_song(), 0, 1)
    assert f"/mastream/{REF}/" in item["stream"]["uri"]


def test_a_subsonic_track_is_untouched_by_any_of_this(app):
    """The kind that already worked must keep working exactly as before."""
    item = app.build_item(
        {"id": "t1", "title": "Light Year", "artist": "x", "duration": 240}, 0, 1
    )
    assert "/stream/t1/" in item["stream"]["uri"]
    assert "/mastream/" not in item["stream"]["uri"]


def test_the_stream_url_is_signed_like_every_other_one(app):
    item = app.build_item(ma_song(), 0, 1)
    _, ref, expires, sig = item["stream"]["uri"].rsplit("/", 3)
    assert app.verify("mastream", ref, int(expires), sig) is True


def test_a_stream_signature_does_not_work_on_the_music_assistant_route(app):
    """Same rule as art: a signature is bound to the kind it was issued for."""
    _url, expires = app.signed_url("stream", REF)
    sig = app.sign("stream", REF, expires)
    assert app.verify("mastream", REF, expires, sig) is False


# --- the controls that cannot work on this source ---------------------------


def _seek_enabled(app, song, index=0, total=3):
    controls = {c["name"]: c for c in app.build_item(song, index, total)["controls"]}
    return controls["SEEK_POSITION"]["enabled"]


def test_seeking_is_offered_on_a_buffered_music_assistant_track(app):
    """Phase 3. The buffer makes the ranged GET behind a scrub answerable."""
    assert _seek_enabled(app, ma_song()) is True


def test_seeking_is_not_offered_when_the_buffer_is_off(app, monkeypatch):
    """MA's own audio is realtime: no length, no ranges, so no scrubbing.

    A control that is declared and then misbehaves is worse than one that is
    greyed out, and a scrub that cannot be answered restarts the track.
    """
    import mastream_cache

    monkeypatch.setattr(mastream_cache, "ENABLED", False)
    assert _seek_enabled(app, ma_song()) is False


def test_seeking_is_still_offered_on_a_subsonic_track(app):
    """Navidrome answers ranges, so nothing about phase 2 may cost it that."""
    controls = {
        c["name"]: c
        for c in app.build_item({"id": "t1", "title": "x", "duration": 240}, 0, 3)[
            "controls"
        ]
    }
    assert controls["SEEK_POSITION"]["enabled"] is True


def test_next_and_previous_still_work_across_a_mixed_queue(app):
    """Skipping is Alexa's own, and does not depend on where the audio is from."""
    controls = {c["name"]: c for c in app.build_item(ma_song(), 1, 3)["controls"]}
    assert controls["NEXT"]["enabled"] is True
    assert controls["PREVIOUS"]["enabled"] is True


def test_a_duration_is_still_reported(app):
    """It is known from MA even though the stream itself has no length."""
    assert app.build_item(ma_song(), 0, 1)["durationInMilliseconds"] == 293_000


# --- artwork ----------------------------------------------------------------


def test_public_art_is_handed_to_amazon_directly(app):
    """Spotify art is already on a CDN Amazon can reach. No reason to proxy it."""
    art = app.build_item(ma_song(art_url="https://i.scdn.co/image/abc"), 0, 1)
    assert art["metadata"]["art"]["sources"][0]["url"] == "https://i.scdn.co/image/abc"


def test_a_track_with_no_reachable_art_falls_back_to_the_skill_icon(app):
    sources = app.build_item(ma_song(), 0, 1)["metadata"]["art"]["sources"]
    assert all("/icons/" in s["url"] for s in sources)


# --- the route itself -------------------------------------------------------


def test_the_route_rejects_a_bad_signature(client):
    resp = client.get(f"/mastream/{REF}/{int(time.time()) + 60}/{'0' * 32}")
    assert resp.status_code == 403


def test_the_route_rejects_an_expired_signature(client, app):
    past = int(time.time()) - 60
    sig = app.sign("mastream", REF, past)
    assert client.get(f"/mastream/{REF}/{past}/{sig}").status_code == 403


def test_the_route_refuses_a_reference_that_is_not_a_music_assistant_uri(client, app):
    """Signed, and still refused.

    A signature only proves the bridge issued the URL. What stops this route
    being a way to reach an arbitrary host is that the reference names an item
    and the address of Music Assistant comes from config, so both checks have
    to hold independently.
    """
    evil = stream_ref.encode_ref("http://169.254.169.254/latest/meta-data/")
    url, expires = app.signed_url("mastream", evil)
    sig = app.sign("mastream", evil, expires)
    assert client.get(f"/mastream/{evil}/{expires}/{sig}").status_code == 400


def test_the_route_fetches_music_assistant_and_nowhere_else(client, app, monkeypatch):
    """The reference names an item; the host is the bridge's own config."""
    import mastream_cache

    monkeypatch.setattr(mastream_cache, "ENABLED", False)
    _url, expires = app.signed_url("mastream", REF)
    sig = app.sign("mastream", REF, expires)

    with mock.patch.object(app_module, "_proxy") as proxy:
        proxy.return_value = ("", 200)
        client.get(f"/mastream/{REF}/{expires}/{sig}")

    (upstream, _content_type), _ = proxy.call_args
    assert upstream == f"{app_module.MA_STREAM_BASE}/ampere_stream/{REF}.mp3"


def test_a_published_music_assistant_queue_survives_the_whole_path(app, tmp_path,
                                                                   monkeypatch):
    """Publish, resolve, build an Item: the trip a real track actually makes."""
    monkeypatch.setattr(queue_api, "STATE_DIR", tmp_path / "external")
    queue_api.STATE_DIR.mkdir(parents=True, exist_ok=True)

    record = queue_api.publish([
        {"source": "ma", "ref": REF, "title": "Dancing On My Own",
         "artist": "Robyn", "duration": 293},
    ])
    (song,) = queue_api.resolve(record["token"])
    item = app.build_item(song, 0, 1)

    assert item["metadata"]["name"]["speech"]["text"] == "Dancing On My Own"
    assert f"/mastream/{REF}/" in item["stream"]["uri"]


# --- what the buffer buys: real range support -------------------------------


@pytest.fixture
def buffered(monkeypatch, tmp_path):
    """A cache holding one complete track, without touching the network."""
    import mastream_cache

    monkeypatch.setattr(mastream_cache, "CACHE_DIR", tmp_path / "mastream")
    monkeypatch.setattr(mastream_cache, "ENABLED", True)
    mastream_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = mastream_cache.path_for(REF)
    path.write_bytes(b"ID3" + bytes(range(256)) * 40)
    return path


def _signed(app):
    _url, expires = app.signed_url("mastream", REF)
    return expires, app.sign("mastream", REF, expires)


def test_a_buffered_track_is_served_with_a_length_and_accepts_ranges(
    client, app, buffered
):
    """Exactly what Music Assistant's own stream cannot do."""
    expires, sig = _signed(app)
    resp = client.get(f"/mastream/{REF}/{expires}/{sig}")

    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert int(resp.headers["Content-Length"]) == buffered.stat().st_size


def test_a_range_is_answered_with_a_206_and_the_right_bytes(client, app, buffered):
    """The case that breaks unbuffered playback.

    Moving audio between rooms sends no directive at all: Amazon re-pulls the
    same URL with a Range header. Answering that with a 200 from byte zero is
    what restarted the track in the new room.
    """
    expires, sig = _signed(app)
    whole = buffered.read_bytes()
    resp = client.get(
        f"/mastream/{REF}/{expires}/{sig}", headers={"Range": "bytes=1000-1099"}
    )

    assert resp.status_code == 206
    assert resp.data == whole[1000:1100]
    assert resp.headers["Content-Range"] == f"bytes 1000-1099/{len(whole)}"


def test_an_open_ended_range_runs_to_the_end(client, app, buffered):
    """`Range: bytes=N-` is the shape Alexa actually sends."""
    expires, sig = _signed(app)
    whole = buffered.read_bytes()
    resp = client.get(
        f"/mastream/{REF}/{expires}/{sig}", headers={"Range": "bytes=5000-"}
    )

    assert resp.status_code == 206
    assert resp.data == whole[5000:]
