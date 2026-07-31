"""Queue controls: shuffle, loop, repeat, jump, view and feedback.

The service is otherwise stateless, so these tests pin the one place that
isn't: shuffle order must stay identical across requests, or playback wanders
as each GetNextItem re-derives a different order.
"""

from __future__ import annotations

import conftest
from conftest import directive


def post(client, body):
    resp = client.post("/music", json=body)
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def start(client, content_id="ar:a1", modes=None):
    """Initiate a queue and return (queueId, firstItem)."""
    payload = {"contentId": content_id}
    if modes:
        payload["playbackModes"] = modes
    out = post(client, directive("Alexa.Media.Playback", "Initiate", payload))
    pm = out["payload"]["playbackMethod"]
    return pm["id"], pm


def wrapped_ref(queue_id, index, content_id="ar:a1"):
    """The {namespace,name,value} form used by Set*/GetView."""
    return {"namespace": "Alexa.Audio.PlayQueue", "name": "item",
            "value": {"id": str(index), "queueId": queue_id, "contentId": content_id}}


def bare_ref(queue_id, index, content_id="ar:a1"):
    """The flat form used by GetNextItem/GetPreviousItem/JumpToItem."""
    return {"id": str(index), "queueId": queue_id, "contentId": content_id}


# --- controls are actually advertised ---------------------------------------


def test_initiate_advertises_enabled_controls(client):
    """An empty controls list silently disables shuffle and loop."""
    _qid, pm = start(client, "al:al1")
    by_name = {c["name"]: c for c in pm["controls"]}
    assert by_name["SHUFFLE"] == {"type": "TOGGLE", "name": "SHUFFLE",
                                  "enabled": True, "selected": False}
    assert by_name["LOOP"]["enabled"] is True
    assert by_name["REPEAT"] == {"name": "REPEAT", "type": "CYCLE",
                                 "enabled": True, "value": {"status": "OFF"}}


def test_feedback_rule_enabled(client):
    """Omitting rules.feedback disables thumbs the same way."""
    _qid, pm = start(client)
    assert pm["rules"]["feedback"] == {"type": "PREFERENCE", "enabled": True}


def test_initiate_honours_requested_playback_modes(client):
    _qid, pm = start(client, modes={"shuffle": True, "loop": True,
                                    "repeat": {"status": "ON"}})
    by_name = {c["name"]: c for c in pm["controls"]}
    assert by_name["SHUFFLE"]["selected"] is True
    assert by_name["LOOP"]["selected"] is True
    assert by_name["REPEAT"]["value"]["status"] == "ON"


# --- shuffle ----------------------------------------------------------------


def test_set_shuffle_returns_empty_generic_response(client):
    qid, _pm = start(client)
    out = post(client, directive(
        "Alexa.Media.PlayQueue", "SetShuffle",
        {"currentItemReference": wrapped_ref(qid, 0), "enable": True},
    ))
    assert out["header"]["namespace"] == "Alexa"
    assert out["header"]["name"] == "Response"
    assert out["payload"] == {}


def test_shuffle_state_persists_and_shows_in_controls(client):
    qid, _pm = start(client)
    post(client, directive("Alexa.Media.PlayQueue", "SetShuffle",
                           {"currentItemReference": wrapped_ref(qid, 0), "enable": True}))
    view = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                  {"currentItemReference": wrapped_ref(qid, 0)}))
    shuffle = [c for c in view["payload"]["queueControls"] if c["name"] == "SHUFFLE"][0]
    assert shuffle["selected"] is True


def test_shuffle_order_is_stable_across_requests(client):
    """The critical property: the queue is re-derived every request."""
    qid, _pm = start(client)
    post(client, directive("Alexa.Media.PlayQueue", "SetShuffle",
                           {"currentItemReference": wrapped_ref(qid, 0), "enable": True}))

    def track_at(i):
        out = post(client, directive("Alexa.Media.PlayQueue", "GetItem",
                                     {"targetItemReference": wrapped_ref(qid, i)}))
        return out["payload"]["item"]["metadata"]["name"]["display"]

    first_pass = [track_at(i) for i in range(3)]
    second_pass = [track_at(i) for i in range(3)]
    assert first_pass == second_pass


def test_shuffle_changes_order_for_different_queues(client):
    """Order is seeded by queueId, so two queues shuffle differently."""
    orders = []
    for _ in range(2):
        qid, _pm = start(client)
        post(client, directive("Alexa.Media.PlayQueue", "SetShuffle",
                               {"currentItemReference": wrapped_ref(qid, 0), "enable": True}))
        out = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                     {"currentItemReference": wrapped_ref(qid, 0)}))
        orders.append([i["metadata"]["name"]["display"] for i in out["payload"]["items"]])
    assert all(len(o) == 3 for o in orders)


def test_queues_do_not_share_state(client):
    q1, _ = start(client, "al:al1")
    q2, _ = start(client, "al:al1")
    post(client, directive("Alexa.Media.PlayQueue", "SetShuffle",
                           {"currentItemReference": wrapped_ref(q1, 0, "al:al1"), "enable": True}))
    view2 = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                   {"currentItemReference": wrapped_ref(q2, 0, "al:al1")}))
    shuffle2 = [c for c in view2["payload"]["queueControls"] if c["name"] == "SHUFFLE"][0]
    assert shuffle2["selected"] is False


# --- loop -------------------------------------------------------------------


def test_loop_wraps_end_to_start(client):
    """With loop on, the last track is followed by the first, never finished."""
    qid, _pm = start(client)
    post(client, directive("Alexa.Media.PlayQueue", "SetLoop",
                           {"currentItemReference": wrapped_ref(qid, 0), "enable": True}))
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                 {"currentItemReference": bare_ref(qid, 2)}))
    assert out["payload"]["isQueueFinished"] is False
    assert out["payload"]["item"]["id"] == "0"


def test_loop_off_reports_queue_finished(client):
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                 {"currentItemReference": bare_ref(qid, 2)}))
    assert out["payload"]["isQueueFinished"] is True


def test_loop_wraps_start_to_end_on_previous(client):
    qid, _pm = start(client)
    post(client, directive("Alexa.Media.PlayQueue", "SetLoop",
                           {"currentItemReference": wrapped_ref(qid, 0), "enable": True}))
    out = post(client, directive("Alexa.Audio.PlayQueue", "GetPreviousItem",
                                 {"currentItemReference": bare_ref(qid, 0)}))
    assert out["payload"]["item"]["id"] == "2"


# --- repeat -----------------------------------------------------------------


def test_set_repeat_records_mode(client):
    qid, _pm = start(client)
    post(client, directive("Alexa.Media.PlayQueue", "SetRepeat",
                           {"currentItemReference": wrapped_ref(qid, 0),
                            "mode": {"status": "ON"}}))
    view = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                  {"currentItemReference": wrapped_ref(qid, 0)}))
    repeat = [c for c in view["payload"]["queueControls"] if c["name"] == "REPEAT"][0]
    assert repeat["value"]["status"] == "ON"


# --- GetView ----------------------------------------------------------------


def test_get_view_namespace_flips(client):
    """Request is Alexa.Media.PlayQueue; response is Alexa.Audio.PlayQueue."""
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                 {"currentItemReference": wrapped_ref(qid, 0)}))
    assert out["header"]["namespace"] == "Alexa.Audio.PlayQueue"
    assert out["header"]["name"] == "GetView.Response"


def test_get_view_returns_controls_and_items(client):
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                 {"currentItemReference": wrapped_ref(qid, 0)}))
    assert out["payload"]["queueControls"]
    assert out["payload"]["items"]
    assert out["payload"]["items"][0]["stream"]["uri"]


def test_get_view_caps_at_ten_items(client, app, monkeypatch):
    """Amazon discards anything past 10."""
    many = [{"id": f"t{i}", "title": f"Track {i}", "artist": "A", "duration": 10}
            for i in range(40)]
    monkeypatch.setattr(app, "resolve_tracks", lambda cid: many)
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                 {"currentItemReference": wrapped_ref(qid, 0)}))
    assert len(out["payload"]["items"]) == 10


# --- JumpToItem -------------------------------------------------------------


def test_jump_to_item(client):
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Audio.PlayQueue", "JumpToItem",
                                 {"currentItemReference": bare_ref(qid, 0),
                                  "targetItemId": "2"}))
    assert out["header"]["name"] == "JumpToItem.Response"
    assert out["payload"]["item"]["id"] == "2"


def test_jump_out_of_range_is_invalid_item(client):
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Audio.PlayQueue", "JumpToItem",
                                 {"currentItemReference": bare_ref(qid, 0),
                                  "targetItemId": "99"}))
    assert out["payload"]["type"] == "INVALID_ITEM"


def test_jump_to_garbage_target_is_invalid_item(client):
    qid, _pm = start(client)
    out = post(client, directive("Alexa.Audio.PlayQueue", "JumpToItem",
                                 {"currentItemReference": bare_ref(qid, 0),
                                  "targetItemId": "not-a-number"}))
    assert out["payload"]["type"] == "INVALID_ITEM"


# --- feedback ---------------------------------------------------------------


def feedback(qid, index, value, content_id="ar:a1"):
    return directive(
        "Alexa.UserPreference", "ReceiveFeedback",
        {"activeContext": {"content": [wrapped_ref(qid, index, content_id)]},
         "feedback": {"type": "PREFERENCE", "value": value}},
        version="2.0",
    )


def test_thumbs_up_stars_the_track(client):
    qid, _pm = start(client, "al:al1")
    out = post(client, feedback(qid, 0, "POSITIVE", "al:al1"))
    assert conftest.STAR_CALLS == [("star", "t1")]
    assert out["payload"] == {}


def test_thumbs_down_unstars_and_skips(client):
    qid, _pm = start(client, "al:al1")
    out = post(client, feedback(qid, 0, "NEGATIVE", "al:al1"))
    assert conftest.STAR_CALLS == [("unstar", "t1")]
    assert out["header"]["namespace"] == "Alexa.UserPreference.Audio"
    assert out["header"]["payloadVersion"] == "2.0"
    assert out["payload"]["skip"] is True


def test_feedback_write_failure_does_not_break_playback(client, app, monkeypatch):
    def boom(_sid):
        raise RuntimeError("navidrome down")
    monkeypatch.setattr(app.subsonic, "star", boom)
    qid, _pm = start(client)
    out = post(client, feedback(qid, 0, "POSITIVE"))
    assert out["payload"] == {}


# --- reference shapes -------------------------------------------------------


def test_both_reference_shapes_parse_identically(client):
    """Set* wraps the reference; GetNextItem sends it bare."""
    qid, _pm = start(client)
    wrapped = post(client, directive("Alexa.Media.PlayQueue", "GetItem",
                                     {"targetItemReference": wrapped_ref(qid, 1)}))
    flat = post(client, directive("Alexa.Audio.PlayQueue", "GetNextItem",
                                  {"currentItemReference": bare_ref(qid, 0)}))
    assert wrapped["payload"]["item"]["id"] == flat["payload"]["item"]["id"] == "1"


# --- regressions ------------------------------------------------------------


def test_generic_response_uses_payload_version_3(client):
    """Mirroring the directive's 1.0 left the app never updating shuffle state."""
    qid, _pm = start(client)
    out = post(client, directive(
        "Alexa.Media.PlayQueue", "SetShuffle",
        {"currentItemReference": wrapped_ref(qid, 0), "enable": True},
    ))
    assert out["header"]["payloadVersion"] == "3.0"
    assert out["header"]["namespace"] == "Alexa"
    assert out["header"]["name"] == "Response"


def test_shuffle_can_be_turned_back_off(client):
    qid, _pm = start(client)
    for enable in (True, False):
        post(client, directive("Alexa.Media.PlayQueue", "SetShuffle",
                               {"currentItemReference": wrapped_ref(qid, 0), "enable": enable}))
    view = post(client, directive("Alexa.Media.PlayQueue", "GetView",
                                  {"currentItemReference": wrapped_ref(qid, 0)}))
    shuffle = [c for c in view["payload"]["queueControls"] if c["name"] == "SHUFFLE"][0]
    assert shuffle["selected"] is False


def test_names_preserve_natural_case(client):
    """Alexa ignores `display`, so speech.text is what renders on screen."""
    _qid, pm = start(client, "al:al1")
    meta = pm["firstItem"]["metadata"]
    assert meta["name"]["speech"]["text"] == "Light Year"
    assert meta["name"]["display"] == "Light Year"
    assert meta["authors"][0]["name"]["speech"]["text"] == "Gregory Alan Isakov"


def test_items_offer_seek_position(client):
    """Without ADJUST/SEEK_POSITION the Alexa app renders a dead progress bar.

    Undeclared controls are disabled controls, which is why Spotify tracks
    could be scrubbed and ours could not.
    """
    _qid, pm = start(client)
    controls = {c["name"]: c for c in pm["firstItem"]["controls"]}
    assert controls["SEEK_POSITION"]["type"] == "ADJUST"
    assert controls["SEEK_POSITION"]["enabled"] is True


def test_seek_is_disabled_when_duration_is_unknown(client, app, monkeypatch):
    """Scrubbing a track of unknown length is meaningless."""
    song = dict(app.subsonic.song("t1"))
    song.pop("duration", None)
    monkeypatch.setattr(app.subsonic, "album_tracks", lambda aid: [song])
    app._QUEUE_CACHE.clear()

    _qid, pm = start(client)
    controls = {c["name"]: c for c in pm["firstItem"]["controls"]}
    assert controls["SEEK_POSITION"]["enabled"] is False
    assert "durationInMilliseconds" not in pm["firstItem"]


def test_artist_shuffles_by_default(client, app):
    """Same artist twice must not always open with the same track.

    Alexa sends shuffle:false on every fresh Initiate, so this has to be a
    default rather than a reading of the request.
    """
    firsts = set()
    for _ in range(8):
        app._QUEUE_CACHE.clear()
        out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                     {"contentId": "ar:a1"}))
        pm = out["payload"]["playbackMethod"]
        firsts.add(pm["firstItem"]["metadata"]["name"]["display"])
        shuffle = [c for c in pm["controls"] if c["name"] == "SHUFFLE"][0]
        assert shuffle["selected"] is True
    assert len(firsts) > 1, f"first track never varied: {firsts}"


def test_albums_and_playlists_keep_their_order(client, app):
    """An album's order is the point of an album."""
    for content_id, expected in (("al:al1", "Light Year"), ("pl:p1", "That Moon Song")):
        app._QUEUE_CACHE.clear()
        out = post(client, directive("Alexa.Media.Playback", "Initiate",
                                     {"contentId": content_id}))
        pm = out["payload"]["playbackMethod"]
        assert pm["firstItem"]["metadata"]["name"]["display"] == expected, content_id
        shuffle = [c for c in pm["controls"] if c["name"] == "SHUFFLE"][0]
        assert shuffle["selected"] is False, content_id


def test_explicit_shuffle_still_honoured_on_an_album(client, app):
    app._QUEUE_CACHE.clear()
    out = post(client, directive(
        "Alexa.Media.Playback", "Initiate",
        {"contentId": "al:al1", "playbackModes": {"shuffle": True}},
    ))
    pm = out["payload"]["playbackMethod"]
    shuffle = [c for c in pm["controls"] if c["name"] == "SHUFFLE"][0]
    assert shuffle["selected"] is True
