"""Directive handling.

The GetPlayableContent entity-id case is a regression test built from a payload
Amazon actually sent to this skill on 2026-07-30. An earlier version read only
the free-text `value` field, missed `entityId`, and silently fell through to
playing the whole library at random.
"""

from __future__ import annotations

import pytest

from conftest import directive


def post(client, body):
    resp = client.post("/music", json=body)
    assert resp.status_code == 200, resp.data
    return resp.get_json()


# --- GetPlayableContent -----------------------------------------------------


def test_entity_id_from_catalog_resolves_directly(client):
    """Verbatim payload Amazon sent for 'play Gregory Alan Isakov'."""
    body = directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {
            "requestContext": {"user": {"id": "u"}},
            "filters": {"explicitLanguageAllowed": True},
            "selectionCriteria": {
                "attributes": [
                    {
                        "authoritySources": [{"context": None, "name": "", "relevance": None}],
                        "entityId": "artist.1HZIG9UghtPXx7V8D1Ncap",
                        "type": "ARTIST",
                    }
                ]
            },
        },
    )
    out = post(client, body)
    assert out["header"]["name"] == "GetPlayableContent.Response"
    assert out["payload"]["content"]["id"] == "ar:1HZIG9UghtPXx7V8D1Ncap"


def test_entity_id_prefixes_map_to_content_ids(client):
    for entity, expected in [
        ("artist.X", "ar:X"),
        ("album.Y", "al:Y"),
        ("track.Z", "tr:Z"),
        ("playlist.P", "pl:P"),
        ("genre.Jazz", "gen:Jazz"),
    ]:
        out = post(client, directive(
            "Alexa.Media.Search", "GetPlayableContent",
            {"selectionCriteria": {"attributes": [{"entityId": entity, "type": "ARTIST"}]}},
        ))
        assert out["payload"]["content"]["id"] == expected, entity


def test_entity_type_matches_the_entity_kind(client):
    """MediaMetadata.type is a closed enum and must describe what we return.

    Answering TRACK for an artist is a schema violation: Alexa stopped calling
    Initiate after a catalog rebuild and fell back to the default provider.
    """
    for entity, expected in [
        ("artist.X", "ARTIST"),
        ("album.Y", "ALBUM"),
        ("track.Z", "TRACK"),
        ("playlist.P", "PLAYLIST"),
        ("genre.Jazz", "GENRE"),
    ]:
        out = post(client, directive(
            "Alexa.Media.Search", "GetPlayableContent",
            {"selectionCriteria": {"attributes": [{"entityId": entity, "type": "ARTIST"}]}},
        ))
        assert out["payload"]["content"]["metadata"]["type"] == expected, entity


def test_catalog_hit_is_named_not_labelled_with_its_kind(client, app):
    """Amazon sends entityId with no `value`, so the name comes from the cache.

    In production `prewarm_artists` fills this at boot; the old code answered
    with the literal entity kind ("artist") instead.
    """
    app.prewarm_artists()
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"entityId": "artist.a1", "type": "ARTIST"}]}},
    ))
    name = out["payload"]["content"]["metadata"]["name"]
    assert name["display"] == "Gregory Alan Isakov"
    assert name["speech"]["text"] == "Gregory Alan Isakov"


def test_naming_never_blocks_the_response(client, app, monkeypatch):
    """A cold name lookup must not sit on Amazon's clock.

    A synchronous Navidrome call here pushed GetPlayableContent from 0ms to
    300-700ms, and Alexa went silent rather than calling Initiate.
    """
    def slow(*a, **k):
        raise AssertionError("lookup must not run on the request path")
    monkeypatch.setattr(app, "_lookup_content", slow)

    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"entityId": "artist.cold", "type": "ARTIST"}]}},
    ))
    content = out["payload"]["content"]
    assert content["id"] == "ar:cold"
    assert content["metadata"]["type"] == "ARTIST"
    assert content["metadata"]["art"]["sources"]


def test_spoken_value_is_preferred_over_a_cold_cache(client):
    """When Alexa does send text, use it rather than waiting on a lookup."""
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [
            {"entityId": "artist.zz", "type": "ARTIST", "value": "Breaking Benjamin"}
        ]}},
    ))
    assert out["payload"]["content"]["metadata"]["name"]["display"] == "Breaking Benjamin"


def test_every_response_carries_at_least_one_https_art_source(client):
    """`art` is required and each ArtSource requires an HTTPS url."""
    bodies = [
        {"selectionCriteria": {"attributes": [{"entityId": "genre.Jazz", "type": "GENRE"}]}},
        {"selectionCriteria": {"attributes": [{"entityId": "artist.a1", "type": "ARTIST"}]}},
        {"selectionCriteria": {"attributes": []}},
        {"selectionCriteria": {"attributes": [{"type": "ARTIST", "value": "Gregory Alan Isakov"}]}},
    ]
    for body in bodies:
        out = post(client, directive("Alexa.Media.Search", "GetPlayableContent", body))
        sources = out["payload"]["content"]["metadata"]["art"]["sources"]
        assert sources, body
        for source in sources:
            assert source["url"].startswith("https://"), source


def test_metadata_lookup_failure_still_returns_playable_content(client, app, monkeypatch):
    """A naming lookup must never cost us the playback."""
    def boom(*a, **k):
        raise RuntimeError("navidrome down")
    monkeypatch.setattr(app.subsonic, "call", boom)
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"entityId": "artist.a1", "type": "ARTIST"}]}},
    ))
    assert out["payload"]["content"]["id"] == "ar:a1"
    assert out["payload"]["content"]["metadata"]["type"] == "ARTIST"
    assert out["payload"]["content"]["metadata"]["art"]["sources"]


def test_unknown_entity_prefix_falls_back_to_search(client):
    """An id we did not mint must not be mistaken for a contentId."""
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [
            {"entityId": "somethingelse.123", "type": "ARTIST", "value": "Gregory Alan Isakov"}
        ]}},
    ))
    assert out["payload"]["content"]["id"].startswith("ar:")


def test_empty_criteria_plays_library(client):
    """'play <provider name>' arrives with no attributes."""
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": []}},
    ))
    assert out["payload"]["content"]["id"] == "rnd:all"


def test_artist_request_prefers_artist_over_track(client):
    """Asking for an artist must queue the artist, not a better-matching title."""
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"type": "ARTIST", "value": "Gregory Alan Isakov"}]}},
    ))
    assert out["payload"]["content"]["id"] == "ar:a1"


def test_playlist_matched_by_spoken_name(client):
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"type": "PLAYLIST", "value": "bedtime playlist"}]}},
    ))
    assert out["payload"]["content"]["id"] == "pl:p1"


def test_genre_request(client):
    out = post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"type": "GENRE", "value": "Jazz"}]}},
    ))
    assert out["payload"]["content"]["id"] == "gen:Jazz"


# --- Initiate ---------------------------------------------------------------


def test_initiate_returns_playable_first_item(client):
    # An album, because artists shuffle by default and have no fixed first track.
    out = post(client, directive(
        "Alexa.Media.Playback", "Initiate",
        {"contentId": "al:al1", "requestContext": {"user": {"id": "u"}}},
    ))
    pm = out["payload"]["playbackMethod"]
    assert pm["type"] == "ALEXA_AUDIO_PLAYER_QUEUE"
    first = pm["firstItem"]
    assert first["metadata"]["name"]["display"] == "Light Year"
    assert first["stream"]["uri"].startswith("https://example.test/stream/t1/")
    assert first["durationInMilliseconds"] == 240_000


def test_initiate_sets_valid_until_explicitly(client):
    """Omitting validUntil gives Amazon's ~60 second default. Never omit it."""
    out = post(client, directive(
        "Alexa.Media.Playback", "Initiate", {"contentId": "ar:a1"},
    ))
    stream = out["payload"]["playbackMethod"]["firstItem"]["stream"]
    assert stream["validUntil"].endswith("Z")
    assert len(stream["validUntil"]) == 20


def test_initiate_on_empty_content_errors(client, app, monkeypatch):
    monkeypatch.setattr(app.subsonic, "playlist_tracks", lambda pid: [])
    out = post(client, directive("Alexa.Media.Playback", "Initiate", {"contentId": "pl:empty"}))
    assert out["header"]["name"] == "ErrorResponse"
    assert out["payload"]["type"] == "CONTENT_NOT_FOUND"


# --- queue navigation -------------------------------------------------------


def test_get_next_item_advances(client):
    out = post(client, directive(
        "Alexa.Audio.PlayQueue", "GetNextItem",
        {"currentItemReference": {"id": "0", "queueId": "q", "contentId": "ar:a1"},
         "isUserInitiated": False},
    ))
    assert out["payload"]["isQueueFinished"] is False
    assert out["payload"]["item"]["id"] == "1"
    assert out["payload"]["item"]["metadata"]["name"]["display"] == "That Moon Song"


def test_get_next_item_at_end_reports_finished(client):
    out = post(client, directive(
        "Alexa.Audio.PlayQueue", "GetNextItem",
        {"currentItemReference": {"id": "2", "queueId": "q", "contentId": "ar:a1"}},
    ))
    assert out["payload"]["isQueueFinished"] is True
    assert out["payload"]["item"] is None


def test_get_next_item_accepts_nested_content_form(client):
    """Premium-audio and podcast payloads nest the id under `content`."""
    out = post(client, directive(
        "Alexa.Audio.PlayQueue", "GetNextItem",
        {"currentItemReference": {
            "id": "0", "queueId": "q",
            "content": {"id": "ar:a1", "metadataType": "TRACK"}}},
    ))
    assert out["payload"]["isQueueFinished"] is False
    assert out["payload"]["item"]["id"] == "1"


def test_get_previous_item(client):
    out = post(client, directive(
        "Alexa.Audio.PlayQueue", "GetPreviousItem",
        {"currentItemReference": {"id": "1", "queueId": "q", "contentId": "ar:a1"}},
    ))
    assert out["payload"]["item"]["id"] == "0"


def test_get_previous_at_start_errors(client):
    out = post(client, directive(
        "Alexa.Audio.PlayQueue", "GetPreviousItem",
        {"currentItemReference": {"id": "0", "queueId": "q", "contentId": "ar:a1"}},
    ))
    assert out["payload"]["type"] == "ITEM_NOT_FOUND"


def test_playlist_order_is_preserved(client):
    """A playlist must play in its own order, not the library's."""
    out = post(client, directive("Alexa.Media.Playback", "Initiate", {"contentId": "pl:p1"}))
    assert out["payload"]["playbackMethod"]["firstItem"]["metadata"]["name"]["display"] == "That Moon Song"


def test_genre_queue_is_deterministic(client):
    """Re-deriving the queue must return the same order, or playback reshuffles.

    getSongsByGenre is used rather than getRandomSongs(genre=) precisely so the
    queue can be rebuilt identically on every GetNextItem. Genres shuffle by
    default, so this pins consistency within one queueId rather than a title.
    """
    out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                 {"contentId": "gen:Jazz"}))
    pm = out["payload"]["playbackMethod"]
    qid, first = pm["id"], pm["firstItem"]["metadata"]["name"]["display"]

    ref = {"id": "0", "queueId": qid, "contentId": "gen:Jazz"}
    seconds = set()
    for _ in range(3):
        nxt = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                     {"currentItemReference": ref}))
        seconds.add(nxt["payload"]["item"]["metadata"]["name"]["display"])
    assert len(seconds) == 1, seconds
    assert seconds.pop() != first


# --- GetItem ----------------------------------------------------------------


def test_get_item_uses_audio_playqueue_namespace(client):
    """Directive arrives on Alexa.Media.PlayQueue, response uses Alexa.Audio.PlayQueue."""
    out = post(client, directive(
        "Alexa.Media.PlayQueue", "GetItem",
        {"targetItemReference": {
            "namespace": "Alexa.Audio.PlayQueue", "name": "item",
            "value": {"id": "1", "queueId": "q", "contentId": "ar:a1"}}},
    ))
    assert out["header"]["namespace"] == "Alexa.Audio.PlayQueue"
    assert out["header"]["name"] == "GetItem.Response"
    assert out["payload"]["item"]["id"] == "1"


# --- routing / robustness ---------------------------------------------------


def test_unknown_directive_returns_invalid_directive(client):
    out = post(client, directive("Alexa.Nonsense", "Whatever", {}))
    assert out["payload"]["type"] == "INVALID_DIRECTIVE"
    assert out["header"]["payloadVersion"] == "3.0"


def test_non_json_body_rejected(client):
    resp = client.post("/music", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_handler_exception_becomes_internal_error(client, app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("navidrome down")
    monkeypatch.setattr(app.subsonic, "call", boom)
    monkeypatch.setattr(app.subsonic, "artist_top_songs", boom)
    app._QUEUE_CACHE.clear()
    out = post(client, directive("Alexa.Media.Playback", "Initiate", {"contentId": "ar:a1"}))
    assert out["header"]["name"] == "ErrorResponse"


def test_unknown_fields_are_tolerated(client):
    """Amazon warns that new fields may appear; parsing must not break."""
    body = directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [{"entityId": "artist.X", "type": "ARTIST"}]},
         "policies": "[POLICIES]", "somethingNew": {"nested": True}},
    )
    out = post(client, body)
    assert out["payload"]["content"]["id"] == "ar:X"


def test_artist_discography_order_is_stable_despite_parallel_fetch(client, app):
    """Albums are fetched concurrently; the base order must be deterministic.

    Tested below the shuffle, because artist queues now shuffle per queueId.
    If the concurrent album fetch reordered, every queue would be built on a
    different foundation and re-derivation would wander mid-playback.
    """
    orders = []
    for _ in range(4):
        app._QUEUE_CACHE.clear()
        orders.append(tuple(s["id"] for s in app.resolve_tracks("ar:a1")))
    assert len(set(orders)) == 1, orders


def test_top_songs_not_used_for_artists(client, app, monkeypatch):
    """getTopSongs hits Last.fm, costs ~0.9s and returns nothing here."""
    called = []
    monkeypatch.setattr(app.subsonic, "artist_top_songs",
                        lambda name: called.append(name) or [])
    app._QUEUE_CACHE.clear()
    post(client, directive("Alexa.Media.Playback", "Initiate", {"contentId": "ar:a1"}))
    assert called == []


# --- Music Assistant handoff ------------------------------------------------
#
# The wiring between queue_api and the directive router: an ext: contentId has
# to resolve like any other, and the handoff phrase has to beat the catalog to
# the answer, because a real playlist of the same name would otherwise win and
# Music Assistant would play something else without saying so.

import queue_api  # noqa: E402


@pytest.fixture
def published(tmp_path, monkeypatch):
    """Publish queues into a store of this test's own.

    STATE_DIR is read once at import, so the attribute is patched rather than
    the environment.
    """
    monkeypatch.setattr(queue_api, "STATE_DIR", tmp_path / "external")
    queue_api.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def publish(tracks, name=""):
        return "ext:" + queue_api.publish(list(tracks), name)["token"]

    return publish


def _ask_for(client, spoken, entity_id=None):
    attr = {"type": "ARTIST", "value": spoken}
    if entity_id:
        attr["entityId"] = entity_id
    return post(client, directive(
        "Alexa.Media.Search", "GetPlayableContent",
        {"selectionCriteria": {"attributes": [attr]}},
    ))


def test_ext_content_resolves_like_any_other_queue(app, published):
    content_id = published(["t3", "t1"])
    assert [s["id"] for s in app.resolve_tracks(content_id)] == ["t3", "t1"]


def test_unknown_ext_token_is_not_cached(app, published):
    """An empty answer must not stick, or one late lookup kills the queue."""
    assert app.resolve_tracks("ext:nosuchtoken") == []
    assert "ext:nosuchtoken" not in app._QUEUE_CACHE


def test_handoff_phrase_resolves_to_the_newest_queue(client, app, published):
    published(["t1"])
    expected = published(["t2", "t3"], "evening")
    body = _ask_for(client, queue_api.HANDOFF_PHRASES[0])
    assert body["payload"]["content"]["id"] == expected


def test_handoff_phrase_beats_a_catalog_entity_of_the_same_name(client, published):
    """The entity branch wins on order alone if this is not checked first."""
    expected = published(["t2"])
    body = _ask_for(client, queue_api.HANDOFF_PHRASES[0], entity_id="playlist.p1")
    assert body["payload"]["content"]["id"] == expected


def test_handoff_with_nothing_published_is_content_not_found(client, published):
    body = _ask_for(client, queue_api.HANDOFF_PHRASES[0])
    assert body["payload"]["type"] == "CONTENT_NOT_FOUND"


def test_an_ordinary_request_still_reaches_the_catalog(client, published):
    published(["t1"])
    body = _ask_for(client, "Gregory Alan Isakov", entity_id="artist.a1")
    assert body["payload"]["content"]["id"] == "ar:a1"
