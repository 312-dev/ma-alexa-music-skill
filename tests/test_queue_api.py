"""Tests for the out-of-band queue publish endpoint."""

from __future__ import annotations

import json
import time

import pytest

from ma_provider import core as app_module
from ma_provider import handoff
from ma_provider import queue_api

AUTH = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Each test gets its own store.

    The module reads QUEUE_STATE_DIR once at import, so the directory is
    patched rather than the environment.
    """
    monkeypatch.setattr(queue_api, "STATE_DIR", tmp_path / "external")
    queue_api.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def queue_client(client):
    """The handoff routes, on the adapter that actually serves them.

    This used to mount a bare Flask app carrying only the handoff blueprint,
    to keep the suite about the wire contract rather than everything else the
    app happened to serve. The blueprint is gone and /queue is now two routes
    on the aiohttp adapter, which the shared client already speaks, so the
    separation is no longer worth its own app.
    """
    return client


def publish(queue_client, tracks, name="", start_offset_ms=None):
    body = {"tracks": tracks, "name": name}
    if start_offset_ms is not None:
        body["start_offset_ms"] = start_offset_ms
    return queue_client.post("/queue", json=body, headers=AUTH)


# --- publishing -------------------------------------------------------------


def test_publish_round_trip(queue_client):
    resp = publish(queue_client, ["t1", "t2", "t3"], "evening")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["content_id"].startswith("ext:")
    assert body["count"] == 3

    token = body["content_id"].split(":", 1)[1]
    songs = queue_api.resolve(token)
    assert [s["id"] for s in songs] == ["t1", "t2", "t3"]


def test_publish_preserves_order(queue_client):
    resp = publish(queue_client, ["t3", "t1", "t2"])
    token = resp.get_json()["content_id"].split(":", 1)[1]
    assert [s["id"] for s in queue_api.resolve(token)] == ["t3", "t1", "t2"]


def test_token_is_stable_for_the_same_list(queue_client):
    first = publish(queue_client, ["t1", "t2"]).get_json()["content_id"]
    second = publish(queue_client, ["t1", "t2"]).get_json()["content_id"]
    assert first == second


def test_token_differs_for_a_different_list(queue_client):
    a = publish(queue_client, ["t1", "t2"]).get_json()["content_id"]
    b = publish(queue_client, ["t2", "t1"]).get_json()["content_id"]
    c = publish(queue_client, ["t1", "t2", "t3"]).get_json()["content_id"]
    assert len({a, b, c}) == 3


def test_token_is_not_a_plain_digest_of_the_ids(queue_client):
    """Knowing the track ids must not be enough to derive the token."""
    import hashlib

    token = publish(queue_client, ["t1", "t2"]).get_json()["content_id"].split(":")[1]
    plain = hashlib.sha256(b"t1\nt2").hexdigest()
    assert token not in plain


def test_missing_song_does_not_sink_the_queue(queue_client):
    resp = publish(queue_client, ["t1", "nope", "t2"])
    body = resp.get_json()
    assert body["count"] == 2
    assert body["requested"] == 3


def test_publish_rejects_a_bad_body(queue_client):
    assert queue_client.post("/queue", json={}, headers=AUTH).status_code == 400
    assert queue_client.post(
        "/queue", json={"tracks": []}, headers=AUTH
    ).status_code == 400
    assert queue_client.post(
        "/queue", json={"tracks": "t1"}, headers=AUTH
    ).status_code == 400
    assert queue_client.post(
        "/queue", json={"tracks": [{"id": "t1"}]}, headers=AUTH
    ).status_code == 400


# --- auth -------------------------------------------------------------------


def test_publish_requires_auth(queue_client):
    assert queue_client.post("/queue", json={"tracks": ["t1"]}).status_code == 401
    assert queue_client.post(
        "/queue", json={"tracks": ["t1"]}, headers={"X-Admin-Token": "wrong"}
    ).status_code == 401


def test_show_requires_auth(queue_client):
    token = publish(queue_client, ["t1"]).get_json()["content_id"].split(":", 1)[1]
    assert queue_client.get(f"/queue/{token}").status_code == 401
    assert queue_client.get(
        f"/queue/{token}", headers={"X-Admin-Token": "wrong"}
    ).status_code == 401
    assert queue_client.get(f"/queue/{token}", headers=AUTH).status_code == 200


# --- reading back -----------------------------------------------------------


def test_show_returns_the_queue(queue_client):
    token = publish(queue_client, ["t1", "t2"], "dinner").get_json()[
        "content_id"
    ].split(":", 1)[1]
    body = queue_client.get(f"/queue/{token}", headers=AUTH).get_json()
    assert body["name"] == "dinner"
    assert [t["id"] for t in body["tracks"]] == ["t1", "t2"]
    assert body["tracks"][0]["title"] == "Light Year"


def test_show_unknown_token_is_404(queue_client):
    assert queue_client.get("/queue/deadbeef", headers=AUTH).status_code == 404


def test_unknown_token_resolves_to_nothing():
    assert queue_api.resolve("deadbeef") == []
    assert queue_api.resolve("") == []
    assert queue_api.resolve("../../etc/passwd") == []


# --- shape parity with resolve_tracks ---------------------------------------


def test_resolve_matches_resolve_tracks_shape(queue_client):
    """The ext: branch must hand app.py exactly what every other branch does."""
    native = app_module.resolve_tracks("pl:p1")
    token = publish(
        queue_client, [s["id"] for s in native]
    ).get_json()["content_id"].split(":", 1)[1]
    assert queue_api.resolve(token) == native


def test_resolved_songs_survive_build_item(queue_client):
    token = publish(queue_client, ["t1"]).get_json()["content_id"].split(":", 1)[1]
    item = app_module.build_item(queue_api.resolve(token)[0], 0, 1)
    assert item["metadata"]["name"]["display"] == "Light Year"
    assert item["stream"]["uri"].startswith("https://example.test/stream/t1/")
    assert item["durationInMilliseconds"] == 240_000


# --- lifetime ---------------------------------------------------------------


def test_expired_queue_resolves_to_nothing(queue_client, monkeypatch):
    token = publish(queue_client, ["t1"]).get_json()["content_id"].split(":", 1)[1]
    assert queue_api.resolve(token)

    monkeypatch.setattr(queue_api, "TTL", 1)
    path = queue_api.STATE_DIR / f"{token}.json"
    record = json.loads(path.read_text())
    record["published"] = time.time() - 10
    path.write_text(json.dumps(record))

    assert queue_api.resolve(token) == []
    assert queue_client.get(f"/queue/{token}", headers=AUTH).status_code == 404


def test_expired_queue_is_deleted_on_the_next_sweep(queue_client, monkeypatch):
    token = publish(queue_client, ["t1"]).get_json()["content_id"].split(":", 1)[1]
    path = queue_api.STATE_DIR / f"{token}.json"
    record = json.loads(path.read_text())
    record["published"] = time.time() - 10
    path.write_text(json.dumps(record))
    monkeypatch.setattr(queue_api, "TTL", 1)

    publish(queue_client, ["t2"])
    assert not path.exists()


def test_store_is_bounded(queue_client, monkeypatch):
    monkeypatch.setattr(queue_api, "MAX_QUEUES", 3)
    tokens = []
    for i, ids in enumerate([["t1"], ["t2"], ["t3"], ["t4"], ["t5"]]):
        tokens.append(publish(queue_client, ids).get_json()["content_id"].split(":")[1])
        # Publish times land in the same millisecond otherwise, and eviction
        # order would be undefined.
        time.sleep(0.01)

    live = list(queue_api.STATE_DIR.glob("*.json"))
    assert len(live) == 3
    assert queue_api.resolve(tokens[0]) == []
    assert queue_api.resolve(tokens[1]) == []
    assert queue_api.resolve(tokens[4])


def test_unreadable_record_is_swept(queue_client):
    (queue_api.STATE_DIR / "garbage.json").write_text("not json")
    publish(queue_client, ["t1"])
    assert not (queue_api.STATE_DIR / "garbage.json").exists()


# --- the handoff phrase -----------------------------------------------------


def test_handoff_points_at_the_newest_queue(queue_client):
    publish(queue_client, ["t1"])
    time.sleep(0.01)
    second = publish(queue_client, ["t2", "t3"]).get_json()["content_id"]
    assert queue_api.handoff_content_id() == second


def test_handoff_is_none_when_nothing_is_published():
    assert queue_api.handoff_content_id() is None


def test_handoff_resolves_a_concrete_token_not_an_alias(queue_client):
    """Alexa echoes the contentId back for hours; it must not be a moving target."""
    content_id = publish(queue_client, ["t1"]).get_json()["content_id"]
    assert queue_api.handoff_content_id() == content_id
    assert content_id != f"ext:{queue_api.CURRENT}"


def test_current_alias_resolves_to_the_newest_queue(queue_client):
    publish(queue_client, ["t1"])
    time.sleep(0.01)
    publish(queue_client, ["t2", "t3"])
    assert [s["id"] for s in queue_api.resolve("current")] == ["t2", "t3"]


def test_current_alias_readable_over_http(queue_client):
    publish(queue_client, ["t1"], "on deck")
    body = queue_client.get("/queue/current", headers=AUTH).get_json()
    assert body["name"] == "on deck"


def test_handoff_phrase_matching():
    assert queue_api.is_handoff_phrase("music assistant")
    assert queue_api.is_handoff_phrase("Music Assistant")
    assert queue_api.is_handoff_phrase("  music   assistant  ")
    # Speech routinely appends the noun, and app.py strips the same words.
    assert queue_api.is_handoff_phrase("music assistant playlist")
    assert queue_api.is_handoff_phrase("Music Assistant, Playlist")
    assert not queue_api.is_handoff_phrase("music")
    assert not queue_api.is_handoff_phrase("assistant music")
    assert not queue_api.is_handoff_phrase("")
    assert not queue_api.is_handoff_phrase("gregory alan isakov")


def test_handoff_phrase_is_configurable(monkeypatch):
    monkeypatch.setattr(handoff, "HANDOFF_PHRASES", ("my queue", "the hand off"))
    assert queue_api.is_handoff_phrase("my queue")
    assert queue_api.is_handoff_phrase("The Hand Off")
    assert not queue_api.is_handoff_phrase("music assistant")


def test_handoff_name_falls_back_when_unlabeled(queue_client):
    publish(queue_client, ["t1"])
    assert queue_api.handoff_name() == "Music Assistant"
    time.sleep(0.01)
    publish(queue_client, ["t2"], "friday night")
    assert queue_api.handoff_name() == "friday night"


# --- the seek offset --------------------------------------------------------
#
# Music Assistant has no seek command for a player that does not stream through
# it: PlayerQueues.seek re-issues play_media on the current item with an
# offset. So the offset has to travel with the queue that publish stores, or
# Alexa starts the republished queue at zero and the track restarts.


def test_a_published_queue_remembers_where_to_start(queue_client):
    token = publish(queue_client, ["t1", "t2"], "evening",
                    90500).get_json()["content_id"].split(":", 1)[1]
    assert queue_api.start_offset_ms(token) == 90500


def test_a_queue_published_without_an_offset_starts_at_the_beginning(queue_client):
    token = publish(queue_client, ["t1", "t2"]).get_json()["content_id"].split(":", 1)[1]
    assert queue_api.start_offset_ms(token) == 0


def test_replaying_the_same_queue_clears_a_stale_seek(queue_client):
    """The token is derived from the track ids, so a re-publish overwrites.

    That is what stops a seek from haunting every later play of the same queue.
    """
    tracks = ["t1", "t2"]
    token = publish(queue_client, tracks, "", 90500).get_json()[
        "content_id"].split(":", 1)[1]
    assert queue_api.start_offset_ms(token) == 90500

    again = publish(queue_client, tracks).get_json()["content_id"].split(":", 1)[1]
    assert again == token, "same ids, same record"
    assert queue_api.start_offset_ms(token) == 0


def test_an_unknown_token_has_no_offset(queue_client):
    assert queue_api.start_offset_ms("nope") == 0


def test_an_expired_queue_reports_no_offset(queue_client, monkeypatch):
    """An offset onto tracks that are gone is worse than no offset."""
    token = publish(queue_client, ["t1"], "", 90500).get_json()[
        "content_id"].split(":", 1)[1]
    monkeypatch.setattr(queue_api, "_expired", lambda record: True)
    assert queue_api.start_offset_ms(token) == 0


def test_a_non_integer_offset_is_refused(queue_client):
    resp = publish(queue_client, ["t1"], "", "halfway")
    assert resp.status_code == 400


def test_a_negative_offset_is_clamped(queue_client):
    token = publish(queue_client, ["t1"], "", -5).get_json()[
        "content_id"].split(":", 1)[1]
    assert queue_api.start_offset_ms(token) == 0


# --- tracks that live in Music Assistant rather than Subsonic ---------------
#
# A queue MA composes can contain a Spotify or Tidal track, which has no
# Subsonic id at all. Before phase 2 those were dropped from the published list
# and silently did not play. They now travel as objects in the same list, and
# the point of these tests is that mixing the two kinds changes nothing about
# the kind that already worked.


def ma_track(uri="spotify://track/abc", **extra):
    from ma_provider import stream_ref

    track = {
        "source": "ma",
        "ref": stream_ref.encode_ref(uri),
        "title": "Some Song",
        "artist": "Some Artist",
        "album": "Some Album",
        "duration": 214,
    }
    track.update(extra)
    return track


def test_a_music_assistant_track_publishes_without_a_subsonic_lookup(queue_client):
    """It arrives complete. There is nothing to look up and nowhere to look."""
    from ma_provider import stream_ref

    resp = publish(queue_client, [ma_track()])
    assert resp.status_code == 200
    token = resp.get_json()["content_id"].split(":", 1)[1]

    (song,) = queue_api.resolve(token)
    assert song["ma_ref"] == stream_ref.encode_ref("spotify://track/abc")
    assert song["title"] == "Some Song"
    assert song["artist"] == "Some Artist"
    assert song["duration"] == 214


def test_the_two_kinds_mix_in_one_queue_and_keep_their_order(queue_client):
    """MA does not group its queue by source, so neither can the publish."""
    resp = publish(queue_client, ["t1", ma_track("tidal://track/9"), "t2"])
    token = resp.get_json()["content_id"].split(":", 1)[1]

    songs = queue_api.resolve(token)
    assert [s["id"] for s in songs] == [
        "t1", f"{queue_api.MA_ID_PREFIX}{ma_track('tidal://track/9')['ref']}", "t2",
    ]
    assert [bool(s.get("ma_ref")) for s in songs] == [False, True, False]


def test_a_music_assistant_track_is_told_apart_by_its_id(queue_client):
    """resolve() drops anything without an id, so one has to be minted."""
    resp = publish(queue_client, [ma_track()])
    token = resp.get_json()["content_id"].split(":", 1)[1]
    (song,) = queue_api.resolve(token)
    assert song["id"].startswith(queue_api.MA_ID_PREFIX)


def test_the_token_still_distinguishes_two_different_queues(queue_client):
    """The token hashes what was published, whatever kind each entry was."""
    a = publish(queue_client, [ma_track("spotify://track/a")]).get_json()["content_id"]
    b = publish(queue_client, [ma_track("spotify://track/b")]).get_json()["content_id"]
    c = publish(queue_client, ["t1", ma_track("spotify://track/a")]).get_json()["content_id"]
    assert len({a, b, c}) == 3


def test_republishing_the_same_mixed_queue_is_the_same_content_id(queue_client):
    """Alexa is already holding it; orphaning it would end playback."""
    first = publish(queue_client, ["t1", ma_track()]).get_json()["content_id"]
    second = publish(queue_client, ["t1", ma_track()]).get_json()["content_id"]
    assert first == second


def test_a_reference_that_is_not_a_music_assistant_uri_is_refused(queue_client):
    """The bridge proxies this reference, so it must name an item, not a host."""
    from ma_provider import stream_ref

    resp = publish(queue_client, [
        ma_track(), ma_track(ref=stream_ref.encode_ref("http://169.254.169.254/")),
    ])
    assert resp.status_code == 200
    token = resp.get_json()["content_id"].split(":", 1)[1]
    assert len(queue_api.resolve(token)) == 1


def test_a_track_object_must_say_it_came_from_music_assistant(queue_client):
    """Anything else is a caller that has misunderstood the format."""
    track = ma_track()
    track.pop("source")
    assert publish(queue_client, [track]).status_code == 400


def test_a_music_assistant_track_needs_a_title(queue_client):
    """Nothing downstream can look one up, so a blank one is not publishable."""
    resp = publish(queue_client, [ma_track(title="  ")])
    token = resp.get_json()["content_id"].split(":", 1)[1]
    assert queue_api.resolve(token) == []


def test_remotely_reachable_art_is_kept_and_anything_else_is_not(queue_client):
    """Amazon fetches art itself, so only a public URL is worth carrying."""
    resp = publish(queue_client, [
        ma_track("spotify://track/1", art_url="https://i.scdn.co/image/abc"),
        ma_track("spotify://track/2", art_url="/imageproxy?path=local"),
    ])
    token = resp.get_json()["content_id"].split(":", 1)[1]
    kept, dropped = queue_api.resolve(token)
    assert kept["art_url"] == "https://i.scdn.co/image/abc"
    assert "art_url" not in dropped


def test_a_missing_duration_is_absent_rather_than_zero(queue_client):
    """build_item reads duration to decide whether a length can be shown."""
    resp = publish(queue_client, [ma_track(duration=0), ma_track("x://y/2", duration="nonsense")])
    token = resp.get_json()["content_id"].split(":", 1)[1]
    assert all("duration" not in s for s in queue_api.resolve(token))


# --- the lookups that were the whole delay before playback ------------------


def test_a_song_is_fetched_once_and_then_remembered(monkeypatch):
    """Publishing cost one Subsonic round trip per track, every time.

    Measured 2026-08-03: 8.33s of a 10s play, against a call to Amazon that
    took 0.20s. The remote service was never the slow part; re-reading
    metadata we had just read was.
    """
    calls = []
    monkeypatch.setattr(queue_api.subsonic, "song",
                        lambda sid: calls.append(sid) or {"id": sid, "title": sid})
    queue_api.forget_songs()

    assert queue_api._fetch("t1")["id"] == "t1"
    assert queue_api._fetch("t1")["id"] == "t1"
    assert calls == ["t1"], "the second read must not reach Subsonic"


def test_republishing_the_same_queue_costs_no_lookups(monkeypatch):
    """The case this exists for. Resuming, seeking, moving a queue between
    rooms and skipping all republish a list fetched moments earlier."""
    calls = []
    monkeypatch.setattr(queue_api.subsonic, "song",
                        lambda sid: calls.append(sid) or {"id": sid, "title": sid})
    queue_api.forget_songs()

    queue_api.publish(["t1", "t2", "t3"])
    first = len(calls)
    queue_api.publish(["t1", "t2", "t3"])

    assert first == 3
    assert len(calls) == 3, "the republish fetched nothing"


def test_a_stale_entry_is_refetched(monkeypatch):
    """Half an hour, not forever. A retag or a replaced file should be picked
    up the same evening rather than at the next restart."""
    calls = []
    monkeypatch.setattr(queue_api.subsonic, "song",
                        lambda sid: calls.append(sid) or {"id": sid})
    queue_api.forget_songs()

    queue_api._fetch("t1")
    stamp, value = queue_api._SONG_CACHE["t1"]
    queue_api._SONG_CACHE["t1"] = (stamp - queue_api.SONG_CACHE_TTL - 1, value)
    queue_api._fetch("t1")

    assert calls == ["t1", "t1"]


def test_a_missing_song_is_remembered_as_missing(monkeypatch):
    """A queue holding a deleted track would otherwise pay the same failing
    lookup on every republish."""
    calls = []

    def gone(sid):
        calls.append(sid)
        raise RuntimeError("no such song")

    monkeypatch.setattr(queue_api.subsonic, "song", gone)
    queue_api.forget_songs()

    assert queue_api._fetch("t9") is None
    assert queue_api._fetch("t9") is None
    assert calls == ["t9"]


def test_forgetting_songs_makes_them_be_read_again(monkeypatch):
    """For a library that has just been rescanned."""
    calls = []
    monkeypatch.setattr(queue_api.subsonic, "song",
                        lambda sid: calls.append(sid) or {"id": sid})
    queue_api.forget_songs()

    queue_api._fetch("t1")
    queue_api.forget_songs()
    queue_api._fetch("t1")

    assert calls == ["t1", "t1"]
