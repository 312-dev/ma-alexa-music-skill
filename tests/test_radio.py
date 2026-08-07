"""Stations and queue continuation.

Two properties matter more than anything else here.

The first is latency. getArtistInfo2 answers in ~725ms and getSimilarSongs2 in
~10s, so the station pool has to be built once and cached, never per track, and
getSimilarSongs2 must never be reached at all.

The second is determinism. Alexa re-derives the queue from the contentId on
every GetNextItem, so an index that resolves to a different track on a later
request makes playback wander. That has to hold past the end of the base queue
too, where the tracks are coming from a continuation pool.
"""

from __future__ import annotations

import conftest
from conftest import directive


def post(client, body):
    resp = client.post("/music", json=body)
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def start(client, content_id):
    out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                 {"contentId": content_id}))
    pm = out["payload"]["playbackMethod"]
    return pm["id"], pm


def bare_ref(queue_id, index, content_id):
    return {"id": str(index), "queueId": queue_id, "contentId": content_id}


def wrapped_ref(queue_id, index, content_id):
    return {"namespace": "Alexa.Audio.PlayQueue", "name": "item",
            "value": bare_ref(queue_id, index, content_id)}


def track_of(item):
    """The Navidrome song id behind an Alexa item."""
    return item["stream"]["id"].removeprefix("s-")


def item_at(client, queue_id, index, content_id):
    out = post(client, directive("Alexa.Media.PlayQueue", "GetItem",
                                 {"targetItemReference":
                                  wrapped_ref(queue_id, index, content_id)}))
    return out["payload"].get("item")


def views(name):
    return [call for call in conftest.CALLS if call[0] == name]


# --- building the pool ------------------------------------------------------


def test_station_pool_is_the_seed_artist_and_artists_like_them(app):
    pool = {song["id"] for song in app.resolve_tracks("rad:a1")}
    assert pool == {"t1", "t2", "t3", "t4", "t5", "t6"}


def test_similar_artists_missing_from_the_library_are_dropped(app):
    """A similar artist with nothing local is a name with no tracks behind it.

    Servers signal that with a negative id, which must never be walked as if it
    were a real artist.
    """
    app.resolve_tracks("rad:a1")
    walked = {call[1] for call in views("getArtist.view")}
    assert walked == {"a1", "a2", "a3"}


def test_similar_artist_lookup_happens_once_per_station(client):
    """getArtistInfo2 costs ~725ms. Once per station, never once per track."""
    qid, _pm = start(client, "rad:a1")
    for index in range(5):
        post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                               {"currentItemReference": bare_ref(qid, index, "rad:a1")}))
    assert len(views("getArtistInfo2.view")) == 1


def test_station_pool_is_cached_against_the_content_id(app):
    app.resolve_tracks("rad:a1")
    walks = len(views("getArtist.view"))
    app.resolve_tracks("rad:a1")
    assert len(views("getArtist.view")) == walks


def test_similar_songs_endpoint_is_never_called(client):
    """getSimilarSongs2 returns local songs but takes ~10s, every time.

    It timed out silently under SUBSONIC_TIMEOUT for long enough to look
    unsupported. No Alexa request can wait for it.
    """
    qid, _pm = start(client, "rad:a1")
    for index in range(4):
        post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                               {"currentItemReference": bare_ref(qid, index, "rad:a1")}))
    assert views("getSimilarSongs2.view") == []


def test_a_failed_similar_lookup_is_not_cached(app, monkeypatch):
    """Otherwise one bad response pins the station to the seed artist."""
    def boom(*a, **k):
        raise RuntimeError("navidrome down")
    monkeypatch.setattr(app.subsonic, "similar_artists", boom)
    assert app.similar_artist_ids("a1") == ["a1"]
    assert "a1" not in app._RADIO_CACHE


def test_each_artist_is_capped_and_the_cap_is_stable(app, monkeypatch):
    """Uncapped, a prolific seed artist is most of their own station."""
    monkeypatch.setattr(app, "RADIO_TRACKS_PER_ARTIST", 1)
    first = [song["id"] for song in app.radio_pool("a1")]
    app._QUEUE_CACHE.clear()
    second = [song["id"] for song in app.radio_pool("a1")]
    assert len(first) == 3
    assert first == second


# --- a station never ends ---------------------------------------------------


def test_station_never_reports_the_queue_finished(client):
    """The pool is six tracks; the station is not."""
    qid, _pm = start(client, "rad:a1")
    for index in range(30):
        out = post(client, directive(
            "Alexa.Audio.PlayQueue", "GetNextItem",
            {"currentItemReference": bare_ref(qid, index, "rad:a1")},
        ))
        assert out["payload"]["isQueueFinished"] is False, index
        assert out["payload"]["item"] is not None, index


def test_any_index_past_the_pool_is_reproducible(client):
    """The property the whole service rests on, applied past the pool end."""
    qid, _pm = start(client, "rad:a1")
    passes = [[track_of(item_at(client, qid, i, "rad:a1")) for i in range(20)]
              for _ in range(3)]
    assert passes[0] == passes[1] == passes[2]


def test_each_lap_of_the_pool_is_reshuffled(app):
    """A station that replays one running order is a playlist."""
    laps = []
    for lap in range(4):
        start_index = lap * 6
        laps.append(tuple(
            app.song_at("rad:a1", start_index + i, "queue-seed")["id"]
            for i in range(6)
        ))
    assert len(set(laps)) > 1, laps
    assert all(set(lap) == set(laps[0]) for lap in laps)


def test_station_shuffles_by_default(client, app):
    firsts = set()
    for _ in range(8):
        app._QUEUE_CACHE.clear()
        _qid, pm = start(client, "rad:a1")
        firsts.add(track_of(pm["firstItem"]))
        shuffle = [c for c in pm["controls"] if c["name"] == "SHUFFLE"][0]
        assert shuffle["selected"] is True
    assert len(firsts) > 1, firsts


def test_station_next_control_never_greys_out(client):
    qid, _pm = start(client, "rad:a1")
    item = item_at(client, qid, 5, "rad:a1")
    controls = {c["name"]: c for c in item["controls"]}
    assert controls["NEXT"]["enabled"] is True


def test_queue_view_looks_past_the_end_of_the_pool(client):
    """Stopping the window at the base length left a continuing queue blank."""
    qid, _pm = start(client, "rad:a1")
    out = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                 {"currentItemReference":
                                  wrapped_ref(qid, 4, "rad:a1")}))
    assert len(out["payload"]["items"]) == 10


def test_station_with_an_empty_pool_still_errors(client, app, monkeypatch):
    """Nothing to play is an error, not an endless silence."""
    monkeypatch.setattr(app, "radio_pool", lambda seed: [])
    app._QUEUE_CACHE.clear()
    out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                 {"contentId": "rad:a1"}))
    assert out["payload"]["type"] == "CONTENT_NOT_FOUND"


# --- the AFTER_CONTENT setting ----------------------------------------------


def test_default_is_stop(app):
    assert app.AFTER_CONTENT == "stop"


def test_setting_is_normalized_and_unknown_values_are_refused(app):
    for raw, expected in [
        ("radio", "radio"), (" ARTIST ", "artist"), ("genre", "genre"),
        ("library", "library"), ("stop", "stop"), ("", "stop"),
        ("nonsense", "stop"), ("shuffle-everything", "stop"),
    ]:
        assert app.after_content_setting(raw) == expected, raw


def test_stop_leaves_the_queue_ending_where_it_did(client):
    qid, _pm = start(client, "ar:a1")
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                 {"currentItemReference": bare_ref(qid, 2, "ar:a1")}))
    assert out["payload"]["isQueueFinished"] is True


def test_continuation_seeds_from_what_was_requested(app):
    """An artist or station names its own seed; a collection uses its last track."""
    for content_id, mode, expected in [
        ("ar:a1", "radio", "rad:a1"),
        ("ar:a1", "artist", "ar:a1"),
        ("rad:a1", "radio", "rad:a1"),
        ("tr:t9", "radio", "rad:a2"),
        ("tr:t9", "artist", "ar:a2"),
        ("al:al1", "radio", "rad:a1"),
        ("al:al1", "genre", "gen:Folk"),
        ("al:al1", "library", "rnd:all"),
        ("pl:p1", "artist", "ar:a1"),
    ]:
        assert app.continuation_content(content_id, mode) == expected, (content_id, mode)


def test_an_artist_seed_costs_no_lookup(app):
    """`ar:` and `rad:` carry the seed in the contentId already."""
    assert app.continuation_content("ar:zz", "radio") == "rad:zz"
    assert views("getArtist.view") == []


def test_a_track_with_no_artist_falls_back_to_the_library(app, monkeypatch):
    """Navidrome omits artistId on some imported files."""
    bare = {"id": "t0", "title": "Untagged", "duration": 10}
    monkeypatch.setattr(app.subsonic, "album_tracks", lambda aid: [bare])
    app._QUEUE_CACHE.clear()
    assert app.continuation_content("al:al1", "radio") == "rnd:all"
    assert app.continuation_content("al:al1", "genre") == "rnd:all"


def test_continuation_plays_on_instead_of_finishing(client, app, monkeypatch):
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    qid, _pm = start(client, "ar:a1")
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                 {"currentItemReference": bare_ref(qid, 2, "ar:a1")}))
    assert out["payload"]["isQueueFinished"] is False
    assert out["payload"]["item"]["id"] == "3"


def test_continuation_extends_and_never_replaces(client, app, monkeypatch):
    """It must begin strictly past the last track of what was asked for."""
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    qid, _pm = start(client, "al:al1")
    played = [track_of(item_at(client, qid, i, "al:al1")) for i in range(2)]
    assert played == ["t1", "t2"]
    assert track_of(item_at(client, qid, 2, "al:al1")) in {
        "t1", "t2", "t3", "t4", "t5", "t6"
    }


def test_continuation_by_genre_uses_the_genre_of_the_last_track(client, app, monkeypatch):
    monkeypatch.setattr(app, "AFTER_CONTENT", "genre")
    qid, _pm = start(client, "al:al1")
    # The genre listing fixture is t3 then t1, so anything past the album can
    # only have come from it.
    assert track_of(item_at(client, qid, 2, "al:al1")) in {"t1", "t3"}


def test_continuation_indexes_are_reproducible(client, app, monkeypatch):
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    qid, _pm = start(client, "al:al1")
    passes = [[track_of(item_at(client, qid, i, "al:al1")) for i in range(12)]
              for _ in range(3)]
    assert passes[0] == passes[1] == passes[2]


def test_continuation_never_starts_an_empty_queue(client, app, monkeypatch):
    """A request we cannot answer is an error, not a cue to play anything."""
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    monkeypatch.setattr(app.subsonic, "playlist_tracks", lambda pid: [])
    out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                 {"contentId": "pl:empty"}))
    assert out["payload"]["type"] == "CONTENT_NOT_FOUND"


def test_next_stays_enabled_on_the_last_track_when_the_queue_continues(client, app,
                                                                       monkeypatch):
    monkeypatch.setattr(app, "AFTER_CONTENT", "library")
    qid, _pm = start(client, "al:al1")
    last = item_at(client, qid, 1, "al:al1")
    controls = {c["name"]: c for c in last["controls"]}
    assert controls["NEXT"]["enabled"] is True


def test_loop_still_wins_over_continuation(client, app, monkeypatch):
    """Loop was asked for out loud; continuation is only a fallback."""
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    qid, _pm = start(client, "al:al1")
    post(client, directive("Alexa.Media.PlayQueue", "SetLoop",
                           {"currentItemReference": wrapped_ref(qid, 0, "al:al1"),
                            "enable": True}))
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                 {"currentItemReference": bare_ref(qid, 1, "al:al1")}))
    assert out["payload"]["item"]["id"] == "0"


def test_feedback_reaches_a_continuation_track(client, app, monkeypatch):
    """Thumbs past the end of the base queue must star what is playing."""
    monkeypatch.setattr(app, "AFTER_CONTENT", "radio")
    qid, _pm = start(client, "ar:a1")
    post(client, directive(
        "Alexa.UserPreference", "ReceiveFeedback",
        {"activeContext": {"content": [wrapped_ref(qid, 4, "ar:a1")]},
         "feedback": {"type": "PREFERENCE", "value": "POSITIVE"}},
        version="2.0",
    ))
    assert len(conftest.STAR_CALLS) == 1
    assert conftest.STAR_CALLS[0][1] in {"t1", "t2", "t3", "t4", "t5", "t6"}


# --- asking for a station out loud ------------------------------------------


def playable(client, attributes):
    out = post(client, directive("Alexa.Media.Search", "GetPlayableContent",
                                 {"selectionCriteria": {"attributes": attributes}}))
    return out["payload"]["content"]


def test_spoken_radio_request_returns_a_station(client):
    content = playable(client, [{"type": "ARTIST", "value": "Gregory Alan Isakov radio"}])
    assert content["id"] == "rad:a1"
    assert content["metadata"]["type"] == "STATION"


def test_spoken_station_request_returns_a_station(client):
    content = playable(client, [{"type": "ARTIST", "value": "Gregory Alan Isakov station"}])
    assert content["id"] == "rad:a1"


def test_media_type_station_returns_a_station(client):
    content = playable(client, [
        {"type": "MEDIA_TYPE", "value": "STATION"},
        {"type": "ARTIST", "value": "Gregory Alan Isakov"},
    ])
    assert content["id"] == "rad:a1"


def test_a_resolved_entity_can_be_asked_for_as_a_station(client):
    content = playable(client, [
        {"type": "MEDIA_TYPE", "value": "STATION"},
        {"entityId": "artist.a1", "type": "ARTIST"},
    ])
    assert content["id"] == "rad:a1"


def test_a_resolved_station_entity_is_a_station(client):
    """"Play <artist> radio" the way it actually arrives once the catalog carries
    a station per artist: Alexa consumes the trailing word to match the "<name>
    Radio" catalog entity, so nothing is left to strip and the entity id is the
    only signal. Its type -- station, not artist -- must route to rad:, not ar:.
    """
    content = playable(client, [
        {"entityId": "station.a1", "type": "ARTIST", "value": "Gregory Alan Isakov Radio"},
    ])
    assert content["id"] == "rad:a1"
    assert content["metadata"]["type"] == "STATION"


def test_a_resolved_station_entity_is_not_named_radio_radio(client, app):
    """The catalog display name already ends in "Radio"; station_content appends
    it too, so the trailing word has to be stripped or the station reads back as
    "<name> Radio Radio"."""
    app.prewarm_artists()
    content = playable(client, [
        {"entityId": "station.a1", "type": "ARTIST", "value": "Gregory Alan Isakov Radio"},
    ])
    assert content["metadata"]["name"]["display"] == "Gregory Alan Isakov Radio"


def test_a_plain_artist_request_is_never_a_station(client):
    """Continuation and stations must not pre-empt what was asked for."""
    assert playable(client, [{"type": "ARTIST", "value": "Gregory Alan Isakov"}])["id"] == "ar:a1"
    assert playable(client, [{"entityId": "artist.a1", "type": "ARTIST"}])["id"] == "ar:a1"


def test_an_artist_whose_name_contains_radio_is_not_a_station(client):
    """Only a trailing word counts, so Radiohead stays an artist."""
    for name in ("Radiohead", "Radio Moscow"):
        assert playable(client, [{"type": "ARTIST", "value": name}])["id"] == "ar:a1", name


def test_a_station_request_that_matches_no_artist_is_not_forced(client, app, monkeypatch):
    monkeypatch.setattr(app.subsonic, "search",
                        lambda q, songs=20, albums=5, artists=5: {"song": [conftest.SONGS["t1"]]})
    content = playable(client, [{"type": "ARTIST", "value": "something radio"}])
    assert content["id"] == "tr:t1"


def test_the_pool_is_warmed_off_the_request_path(client):
    """GetPlayableContent answered in no measurable time before any of this.

    Building the pool is a getArtistInfo2 plus a discography walk per artist,
    and Initiate follows about a second later, so the build belongs in that
    second and not on Amazon's clock.
    """
    playable(client, [{"type": "ARTIST", "value": "Gregory Alan Isakov radio"}])
    assert views("getArtistInfo2.view") == []
    assert views("getArtist.view") == []
    warmed = [args for fn, args, _kw in conftest.WARM_SUBMITS if args == ("rad:a1",)]
    assert warmed, conftest.WARM_SUBMITS


def test_station_response_is_named_and_carries_art(client, app):
    app.prewarm_artists()
    content = playable(client, [{"type": "ARTIST", "value": "Gregory Alan Isakov radio"}])
    assert content["metadata"]["name"]["display"] == "Gregory Alan Isakov Radio"
    assert content["metadata"]["art"]["sources"]


def test_a_degraded_station_is_not_cached(client, app, monkeypatch):
    """One empty similar-artist lookup must not pin the station forever.

    _QUEUE_CACHE never re-derives an entry it already holds, so caching a
    seed-only pool served a Foreigner-only "station" until the process was
    restarted.
    """
    monkeypatch.setattr(app.subsonic, "similar_artists", lambda aid, count=20: [])
    first = app.resolve_tracks("rad:a1")
    assert {t["artist"] for t in first} == {"Gregory Alan Isakov"}
    assert "rad:a1" not in app._QUEUE_CACHE

    # The next request gets a real station rather than the degraded one.
    monkeypatch.setattr(
        app.subsonic, "similar_artists",
        lambda aid, count=20: [{"id": "a2", "name": "Blind Pilot"},
                               {"id": "a3", "name": "Iron and Wine"}],
    )
    second = app.resolve_tracks("rad:a1")
    assert len({t["artist"] for t in second}) > 1
    assert "rad:a1" in app._QUEUE_CACHE


def test_a_real_station_is_still_cached(client, app):
    """The fix must not cost the cache on the path that works."""
    app.resolve_tracks("rad:a1")
    assert "rad:a1" in app._QUEUE_CACHE


def test_initiate_warms_the_continuation_pool(client, app):
    """The station must be built before the track transition, not during it.

    Cold, a station is a dozen discography walks and measured ~3s on the live
    service. On the first GetNextItem past the end of a queue that is exactly
    the moment there is no slack.
    """
    app._QUEUE_CACHE.clear()
    post(client, directive("Alexa.Media.Playback", "Initiate", {"contentId": "tr:t1"}))
    warmed = [args[0] for fn, args, _ in conftest.WARM_SUBMITS
              if fn is app.warm_continuation]
    assert warmed == ["tr:t1"]


def test_warming_is_skipped_when_nothing_follows(client, app, monkeypatch):
    """AFTER_CONTENT=stop must not pay for a station nobody will hear."""
    monkeypatch.setattr(app, "AFTER_CONTENT", "stop")
    app._QUEUE_CACHE.clear()
    app.warm_continuation("tr:t1")
    assert "rad:a1" not in app._QUEUE_CACHE


def test_warm_failure_does_not_break_playback(client, app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("navidrome down")
    monkeypatch.setattr(app, "continuation_content", boom)
    app.warm_continuation("tr:t1")


# --- album metadata ---------------------------------------------------------
#
# An Item with no metadata.album is not rendered as blank by Alexa: it fills
# the slot with the provider name, so the now-playing card reads "Music Assistant"
# where the album should be. Seen live on an untagged copy of a track whose
# other copy was tagged, which is what makes it look intermittent.


def test_untagged_track_inherits_its_album_name(app, monkeypatch):
    """A file with no album tag still names the album it was fetched from."""
    monkeypatch.setattr(
        app.subsonic, "album_tracks",
        lambda aid: [{"id": "x1", "title": "Untagged", "artist": "Someone"}],
    )
    tracks = app.artist_tracks("a1")
    assert tracks, "expected the discography walk to return something"
    assert all(t["album"] for t in tracks)


def test_a_real_album_tag_is_never_overwritten(app, monkeypatch):
    monkeypatch.setattr(
        app.subsonic, "album_tracks",
        lambda aid: [{"id": "x1", "title": "Tagged", "album": "Real Album"}],
    )
    assert {t["album"] for t in app.artist_tracks("a1")} == {"Real Album"}


def test_built_item_carries_the_album_through(app):
    item = app.build_item(
        {"id": "t1", "title": "Light Year", "artist": "GAI",
         "album": "Appaloosa Bones", "duration": 240},
        0, 3,
    )
    assert item["metadata"]["album"]["name"]["display"] == "Appaloosa Bones"
