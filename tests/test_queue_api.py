"""Tests for the out-of-band queue publish endpoint."""

from __future__ import annotations

import json
import time

import pytest

import app as app_module
import handoff
import queue_api

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
def queue_client():
    """A client for a bare app carrying only the blueprint.

    app.py does not register it yet, and this suite must not depend on when it
    does.
    """
    from flask import Flask

    application = Flask(__name__)
    application.register_blueprint(queue_api.bp)
    application.config.update(TESTING=True)
    return application.test_client()


def publish(queue_client, tracks, name=""):
    return queue_client.post(
        "/queue", json={"tracks": tracks, "name": name}, headers=AUTH
    )


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
