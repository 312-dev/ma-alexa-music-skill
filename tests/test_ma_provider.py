"""Tests for the Music Assistant provider.

Music Assistant is not a dependency of the bridge and is not installed here, so
everything MA-shaped is skipped rather than failed. What is left is the part
that was actually hard: the utterance, and the shape of the call to the bridge.

ma_provider/__init__.py resolves `setup` lazily for exactly this reason, so
importing a submodule does not drag in music_assistant.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

from ma_provider import bridge, utterance

REPO = pathlib.Path(__file__).resolve().parent.parent


# --- utterance --------------------------------------------------------------


def test_command_for_a_single_device():
    """No target: the Echo the command is delivered to plays it."""
    assert (
        utterance.custom_command("ampere", "music assistant")
        == "ask ampere to play the music assistant playlist"
    )


def test_command_for_a_group():
    """A group has no dialog interface, so the group is named in the sentence."""
    assert (
        utterance.custom_command("ampere", "music assistant", "whole apartment")
        == "ask ampere to play the music assistant playlist on whole apartment"
    )


def test_command_uses_ask_not_on():
    """`play X on ampere` resolves to a speaker when typed. Never emit it."""
    text = utterance.custom_command("ampere", "music assistant", "kitchen")
    assert text.startswith("ask ampere to play ")
    assert " from ampere" not in text
    assert not text.startswith("play ")


def test_target_names_are_escaped():
    assert utterance.sanitize("Echo Dot (Kitchen)") == "Echo Dot Kitchen"
    assert utterance.sanitize("Liz & Gray's Room") == "Liz and Gray's Room"
    assert utterance.sanitize("  Living   Room  ") == "Living Room"
    assert utterance.sanitize("Bedroom\nplay something else") == (
        "Bedroom play something else"
    )
    assert utterance.sanitize("Office / Upstairs") == "Office Upstairs"
    assert utterance.sanitize("Nook \U0001f50a") == "Nook"


def test_accented_names_survive():
    """Stripping accents renames someone's speaker, and the target then misses."""
    assert utterance.sanitize("Küche") == "Küche"


def test_empty_target_is_dropped_not_left_dangling():
    assert utterance.custom_command("ampere", "music assistant", "***") == (
        "ask ampere to play the music assistant playlist"
    )
    assert utterance.custom_command("ampere", "music assistant", None) == (
        "ask ampere to play the music assistant playlist"
    )


def test_unusable_alias_or_label_raises():
    with pytest.raises(ValueError):
        utterance.custom_command("", "music assistant")
    with pytest.raises(ValueError):
        utterance.custom_command("ampere", "!!!")


# --- bridge client ----------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for mass.http_session."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


def test_publish_request_shape():
    client = bridge.BridgeClient("https://ampere.example/", "tok", session=None)
    url, body = client.publish_request(["t1", "t2"], "Evening")
    assert url == "https://ampere.example/queue"
    assert body == {"tracks": ["t1", "t2"], "name": "Evening",
                    "start_offset_ms": 0}
    assert client.headers["X-Admin-Token"] == "tok"


def test_a_seek_travels_with_the_queue_it_republishes():
    """The only channel there is. MA re-issues play_media for a seek, so the
    position has to ride along with that publish or it is lost."""
    client = bridge.BridgeClient("https://ampere.example/", "tok", session=None)
    _url, body = client.publish_request(["t1"], "Evening", 90500)
    assert body["start_offset_ms"] == 90500


def test_a_negative_offset_is_clamped_not_forwarded():
    """Alexa is handed an unsigned position; garbage in must not reach it."""
    client = bridge.BridgeClient("https://ampere.example/", "tok", session=None)
    _url, body = client.publish_request(["t1"], "", -5)
    assert body["start_offset_ms"] == 0


def test_publish_posts_and_returns_the_content_id():
    session = FakeSession(FakeResponse(payload={"content_id": "ext:abc", "count": 2}))
    client = bridge.BridgeClient("https://ampere.example", "tok", session)

    content_id = asyncio.run(client.publish_queue(["t1", "t2"], "Evening"))

    assert content_id == "ext:abc"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://ampere.example/queue")
    assert kwargs["json"] == {"tracks": ["t1", "t2"], "name": "Evening",
                              "start_offset_ms": 0}
    assert kwargs["headers"]["X-Admin-Token"] == "tok"


def test_publish_track_ids_are_stringified():
    session = FakeSession(FakeResponse(payload={"content_id": "ext:abc", "count": 1}))
    client = bridge.BridgeClient("https://ampere.example", "tok", session)
    asyncio.run(client.publish_queue([123]))
    assert session.calls[0][2]["json"]["tracks"] == ["123"]


def test_publish_raises_on_http_error():
    session = FakeSession(FakeResponse(status=401, text="unauthorized"))
    client = bridge.BridgeClient("https://ampere.example", "bad", session)
    with pytest.raises(bridge.BridgeError):
        asyncio.run(client.publish_queue(["t1"]))


def test_publish_raises_without_a_content_id():
    session = FakeSession(FakeResponse(payload={"count": 0}))
    client = bridge.BridgeClient("https://ampere.example", "tok", session)
    with pytest.raises(bridge.BridgeError):
        asyncio.run(client.publish_queue(["t1"]))


def test_publish_refuses_an_empty_queue():
    client = bridge.BridgeClient("https://ampere.example", "tok", session=None)
    with pytest.raises(bridge.BridgeError):
        asyncio.run(client.publish_queue([]))


# --- manifest ---------------------------------------------------------------


def test_manifest_is_loadable_and_declares_a_player_provider():
    manifest = json.loads((REPO / "ma_provider" / "manifest.json").read_text())
    assert manifest["domain"]
    assert manifest["type"] == "player"
    assert "alexapy" in " ".join(manifest["requirements"])


def test_importing_a_submodule_does_not_require_music_assistant():
    """The suite has to pass on a machine with no MA installed."""
    import ma_provider

    assert ma_provider.DOMAIN
    with pytest.raises(AttributeError):
        ma_provider.definitely_not_an_attribute


# --- the provider itself, only where MA is present --------------------------


def _provider_module():
    pytest.importorskip("music_assistant_models")
    pytest.importorskip("music_assistant")
    pytest.importorskip("alexapy")
    from ma_provider import provider

    return provider


def _bare_player(provider):
    """An AmperePlayer with no __init__ run: these tests touch one method.

    Only the attributes the methods under test actually read are set, since
    MA's Player.__init__ writes a default config through the whole config
    controller and standing that up would make these tests of MA.
    """
    player = provider.AmperePlayer.__new__(provider.AmperePlayer)
    player.logger = SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None,
        debug=lambda *a, **k: None)
    player.is_group = False
    player._player_id = "p1"
    player._attr_current_media = None
    player._titles_to_items = {}
    return player


def _queue_with_seek(seconds):
    """The chain play_media walks: queue -> current_item -> streamdetails."""
    item = SimpleNamespace(streamdetails=SimpleNamespace(seek_position=seconds))
    queue = SimpleNamespace(current_item=item)
    return SimpleNamespace(get=lambda _qid: queue,
                           get_item=lambda _qid, _item_id: item)


def test_seek_is_supported_by_republishing_with_an_offset():
    """Not through alexapy, which has no seek, but through the Item schema.

    MA implements seek as play_media on the current item with an offset, and
    Alexa's Item carries stream.offsetInMilliseconds, so the position rides
    along with the queue that seek republishes.
    """
    provider = _provider_module()
    from music_assistant_models.enums import PlayerFeature

    assert PlayerFeature.SEEK in provider.PLAYER_FEATURES
    assert PlayerFeature.VOLUME_SET in provider.PLAYER_FEATURES
    assert PlayerFeature.NEXT_PREVIOUS in provider.PLAYER_FEATURES


def test_the_seek_position_is_read_off_the_queue_in_milliseconds():
    """Nothing in PlayerMedia carries it; the queue item's streamdetails do.

    MA keeps it as float seconds and Alexa wants integer milliseconds.
    """
    provider = _provider_module()

    player = _bare_player(provider)
    media = SimpleNamespace(source_id="q1", queue_item_id="i1")
    player.mass = SimpleNamespace(player_queues=_queue_with_seek(90.5))

    assert player._seek_offset_ms(media) == 90500


def test_a_plain_play_publishes_no_offset():
    """An ordinary play must not inherit the last seek."""
    provider = _provider_module()

    player = _bare_player(provider)
    player.mass = SimpleNamespace(player_queues=_queue_with_seek(0.0))

    assert player._seek_offset_ms(
        SimpleNamespace(source_id="q1", queue_item_id="i1")) == 0


def test_a_queue_that_has_gone_is_not_an_offset_and_not_a_crash():
    """Every hop to the offset is optional; a missing one means start at zero."""
    provider = _provider_module()

    player = _bare_player(provider)
    player.mass = SimpleNamespace(player_queues=SimpleNamespace(
        get=lambda _qid: None, get_item=lambda _qid, _item_id: None))

    assert player._seek_offset_ms(
        SimpleNamespace(source_id="q1", queue_item_id="i1")) == 0
    assert player._seek_offset_ms(
        SimpleNamespace(source_id=None, queue_item_id=None)) == 0


def test_player_does_not_require_flow_mode():
    """The whole point: discrete tracks with real metadata, not one opaque stream."""
    provider = _provider_module()

    # requires_flow_mode is a property on the Player, and the override ignores
    # self, so it can be read off the class without an Amazon session.
    assert provider.AmperePlayer.requires_flow_mode.fget(None) is False


def test_group_detection():
    """Whole Home Audio groups arrive in the same device list as the speakers."""
    provider = _provider_module()

    assert provider._is_group({"deviceFamily": "WHA"})
    assert not provider._is_group({"deviceFamily": "ECHO"})
    assert not provider._is_group({})


# --- rediscovery ------------------------------------------------------------


class _FakePlayers:
    """Just enough of MA's PlayerController to see which branch was taken."""

    def __init__(self, existing=None):
        self._existing = existing
        self.registered = []

    def get_player(self, player_id, raise_unavailable=False):
        return self._existing

    async def register_or_update(self, player):
        self.registered.append(player)


class _FakeMass:
    def __init__(self, players):
        self.players = players


def _device(provider, serial="S1"):
    device = provider.AlexaDevice()
    device.device_serial_number = serial
    device._cluster_members = []
    return device


def test_a_rediscovered_player_is_refreshed_not_replaced():
    """Handing a fresh Player to register_or_update un-registers it.

    That method swaps the object into the controller's dict and returns
    without re-running registration, so the replacement never gets
    `set_initialized()`. `all_players` filters on exactly that, which is what
    the UI and the whole API read: every player silently disappeared while
    still sitting in the dict.
    """
    provider = _provider_module()

    refreshed = {}

    class _Existing(provider.AmperePlayer):
        def __init__(self):
            self.is_group = False
            self.updated = 0

        def refresh(self, device, name, *, speaker=None, member_ids=None):
            refreshed.update(device=device, name=name, speaker=speaker,
                             member_ids=member_ids)

        def update_state(self, *a, **k):
            self.updated += 1

    existing = _Existing()
    players = _FakePlayers(existing)
    stand_in = type("P", (), {"mass": _FakeMass(players)})()

    device = _device(provider)
    asyncio.run(provider.AmpereAlexaProvider._publish(
        stand_in, "p1", device, "Kitchen Echo"))

    assert players.registered == [], "must not re-register an existing player"
    assert refreshed["name"] == "Kitchen Echo"
    assert existing.updated == 1


def test_a_player_seen_for_the_first_time_is_registered():
    """Only the branch is under test, so the Player itself is a stub.

    MA's Player.__init__ writes a default player config through the whole
    config controller. Standing that up would make this a test of MA rather
    than of which branch `_publish` takes.
    """
    provider = _provider_module()

    built = []

    class _Stub:
        def __init__(self, prov, player_id, device, name, **kwargs):
            self.player_id = player_id
            self.name = name
            self.kwargs = kwargs
            built.append(self)

    players = _FakePlayers(None)
    stand_in = type("P", (), {"mass": _FakeMass(players)})()
    device = _device(provider)

    real = provider.AmperePlayer
    provider.AmperePlayer = _Stub
    try:
        asyncio.run(provider.AmpereAlexaProvider._publish(
            stand_in, "p1", device, "Kitchen Echo", is_group=True,
            member_ids=["m1"]))
    finally:
        provider.AmperePlayer = real

    assert len(players.registered) == 1
    assert players.registered[0] is built[0]
    assert built[0].player_id == "p1"
    assert built[0].name == "Kitchen Echo"
    assert built[0].kwargs["is_group"] is True
    assert built[0].kwargs["member_ids"] == ["m1"]


def test_refresh_takes_the_new_name_and_members_and_leaves_the_rest():
    """A rediscovery can rename a group or change its members, nothing else."""
    provider = _provider_module()

    player = provider.AmperePlayer.__new__(provider.AmperePlayer)
    player.is_group = True
    player.group_name = "Old Name"
    player._titles_to_items = {"a": "b"}

    device = _device(provider, "S2")
    speaker = _device(provider, "S3")
    player.refresh(device, "Whole Apartment", speaker=speaker,
                   member_ids=["m1", "m2"])

    assert player.group_name == "Whole Apartment"
    assert player._attr_name == "Whole Apartment"
    assert player.device is device
    assert player.speaker is speaker
    assert player._attr_group_members == ["m1", "m2"]
    # The queue's title index belongs to playback, not to discovery.
    assert player._titles_to_items == {"a": "b"}


def test_only_the_first_handoff_phrase_is_spoken():
    """The setting is a list of phrases the bridge accepts, not one phrase.

    Uttering the raw setting says the commas out loud and resolves to nothing,
    which is a silent failure: Amazon answers the speaker, not the bridge, so
    no log anywhere records it.
    """
    provider = _provider_module()

    assert provider._first_phrase("ampere queue, music assistant", "x") == "ampere queue"
    assert provider._first_phrase("  music assistant  ", "x") == "music assistant"
    assert provider._first_phrase("", "fallback") == "fallback"
    assert provider._first_phrase(None, "fallback") == "fallback"
    assert provider._first_phrase(",, ,", "fallback") == "fallback"


def test_the_utterance_names_the_kind_of_thing_it_wants():
    """Without the noun, Alexa does not resolve the label at all.

    Measured against an ingested catalog entity: "play handoff" came back
    "I'm not quite sure how to help you with that" and never reached the
    skill; "play the handoff playlist" resolved to playlist.ma-handoff and
    played. Naming the kind is what picks the catalog to resolve against.
    """
    text = utterance.custom_command("ampere", "handoff")
    assert text == "ask ampere to play the handoff playlist"
    assert " playlist" in text

    grouped = utterance.custom_command("ampere", "handoff", "whole apartment")
    # The target has to stay at the end, or `on ...` is read as part of the name.
    assert grouped.endswith("on whole apartment")
    assert "playlist on whole apartment" in grouped


def test_a_group_is_started_by_one_of_its_own_members():
    """A member is a perfectly good place to send the command.

    The only constraint is that the thing spoken to is a real Echo: a Whole
    Home Audio group is a cluster with no dialog interface of its own. An
    earlier version required a speaker from outside the group, on a measurement
    taken while the binding detector was re-provisioning the skill underneath
    live sessions and breaking attempts indiscriminately. A member is preferred
    so Alexa's confirmation lands in a room the music is about to play in.
    """
    provider = _provider_module()

    speakers = {"MEM_A": object(), "MEM_B": object(), "OUTSIDER": object()}
    members = ["MEM_A", "MEM_B"]

    assert provider._group_speaker(speakers, members) == "MEM_A"


def test_the_group_speaker_choice_is_stable():
    """Discovery runs repeatedly; a different speaker each time is a race."""
    provider = _provider_module()

    speakers = {"Z": object(), "A": object(), "M": object(), "OUT": object()}
    members = ["Z", "A", "M"]
    assert provider._group_speaker(speakers, members) == "A"
    assert provider._group_speaker(dict(reversed(list(speakers.items()))), members) == "A"


def test_a_group_containing_every_speaker_is_startable():
    """The case the outside-only rule wrongly declared broken.

    A house whose group holds every Echo it owns is the common case, not an
    edge one, and it starts fine.
    """
    provider = _provider_module()

    speakers = {"MEM_A": object(), "MEM_B": object()}
    assert provider._group_speaker(speakers, ["MEM_A", "MEM_B"]) == "MEM_A"


def test_a_member_that_cannot_host_the_skill_is_not_spoken_to():
    """clusterMembers can name a device that cannot run a music skill."""
    provider = _provider_module()

    speakers = {"CAPABLE": object(), "OUT": object()}
    assert provider._group_speaker(speakers, ["NO_SKILL", "CAPABLE"]) == "CAPABLE"


def test_a_group_of_only_incapable_members_still_gets_a_speaker():
    """Never index an empty list; any real Echo is better than a crash."""
    provider = _provider_module()

    speakers = {"OUT_B": object(), "OUT_A": object()}
    assert provider._group_speaker(speakers, ["NO_SKILL"]) == "OUT_A"


# --- polling ----------------------------------------------------------------


class _FailingStateApi:
    """A state endpoint that raises, the way an asleep Echo's does."""

    def __init__(self, raises=True, payload=None):
        self.raises = raises
        self.payload = payload
        self.calls = 0

    async def get_state(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("device did not answer")
        return self.payload


def _pollable(provider, state_api):
    player = _bare_player(provider)
    player._attr_available = True
    player._attr_name = "Bathroom Echo"
    player.update_state = lambda *a, **k: None
    type(player).state_api = property(lambda self: state_api)
    return player


def test_one_failed_poll_does_not_hide_a_speaker():
    """The bug that lost two Echoes from the player list.

    An Echo that is asleep or briefly unreachable still plays when something is
    sent to it, and playing to one wakes it. Hiding it on a single miss takes a
    working speaker off the list, and the old code did that while logging the
    reason at debug, which nothing runs at.
    """
    provider = _provider_module()
    player = _pollable(provider, _FailingStateApi())
    try:
        asyncio.run(player.poll())
        assert player._attr_available is True
        assert player._poll_failures == 1
    finally:
        del type(player).state_api


def test_a_sustained_outage_does_hide_it():
    provider = _provider_module()
    player = _pollable(provider, _FailingStateApi())
    try:
        for _ in range(provider.POLL_FAILURES_BEFORE_UNAVAILABLE):
            asyncio.run(player.poll())
        assert player._attr_available is False
    finally:
        del type(player).state_api


def test_answering_again_clears_the_failure_run():
    provider = _provider_module()
    api = _FailingStateApi()
    player = _pollable(provider, api)
    try:
        asyncio.run(player.poll())
        asyncio.run(player.poll())
        api.raises = False
        api.payload = {"playerInfo": {"state": "PLAYING"}}
        asyncio.run(player.poll())
        assert player._poll_failures == 0
        assert player._attr_available is True
    finally:
        del type(player).state_api


def test_an_empty_payload_is_not_a_claim_that_nothing_is_playing():
    """A speaker group answers with no playerInfo while its members play.

    Reading that as IDLE is what stopped the position advancing and left MA's
    optimistic guess as the only clock.
    """
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    player = _bare_player(provider)
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 42

    player._apply_state({})

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 42


def test_a_real_idle_report_is_still_believed():
    """Only an empty payload is ignored, not a payload that says IDLE."""
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    player = _bare_player(provider)
    player._attr_playback_state = PlaybackState.PLAYING

    player._apply_state({"state": "IDLE"})

    assert player._attr_playback_state == PlaybackState.IDLE


# --- group capture ----------------------------------------------------------
#
# MA decides whether a group owns its members from the group's `powered` and
# `is_active_session`. A member that is owned is hidden from the player picker,
# so getting this wrong takes working speakers off the list.


def _group(provider, state=None):
    from music_assistant_models.enums import PlaybackState

    player = _bare_player(provider)
    player.is_group = True
    player._attr_playback_state = state or PlaybackState.IDLE
    return player


def test_a_group_never_holds_its_members_whatever_it_is_doing():
    """Capture protects an MA-formed sync session. There is not one here.

    An Alexa Whole Home Audio group is Amazon's: Amazon assembles it, Amazon
    dissolves it, and a command sent to a member is a request Amazon knows how
    to service. Two earlier versions held members while playing-or-paused and
    then while playing, and each one deleted every Echo in the house from the
    player picker for as long as the group was in that state, while leaving
    them visible under Settings > Players.
    """
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    for state in (PlaybackState.IDLE, PlaybackState.PLAYING,
                  PlaybackState.PAUSED):
        assert _group(provider, state).is_active_session is False, state


def test_a_plain_echo_never_holds_anything():
    """MA requires False from a non-group, whatever it happens to be doing."""
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    player = _bare_player(provider)
    player.is_group = False
    player._attr_playback_state = PlaybackState.PLAYING
    assert player.is_active_session is False


def _constructed(provider, is_group):
    """Run only this class's __init__ body.

    MA's Player.__init__ writes a default player config through the whole
    config controller, and standing that up would make this a test of MA.
    """
    player = provider.AmperePlayer.__new__(provider.AmperePlayer)
    base = provider.Player.__init__
    provider.Player.__init__ = lambda self, prov, pid: None
    try:
        provider.AmperePlayer.__init__(
            player, None, "p1", _device(provider), "Whole Apartment",
            is_group=is_group)
    finally:
        provider.Player.__init__ = base
    return player


def test_a_group_does_not_claim_to_be_powered():
    """`powered is True` short-circuits MA's check and captures forever.

    None is what makes it fall through to is_active_session, which is the
    question actually being asked.
    """
    provider = _provider_module()

    assert _constructed(provider, is_group=True)._attr_powered is None
    assert _constructed(provider, is_group=False)._attr_powered is True


def test_the_seeked_item_is_addressed_by_id_not_by_the_queue_position():
    """play_index assigns current_item after loading, so a seek can race it.

    Observed 2026-08-02: one seek in a run of them published no offset and
    restarted the song, because the queue's idea of "current" was still the
    previous item, whose streamdetails carry no seek.
    """
    provider = _provider_module()

    stale = SimpleNamespace(streamdetails=SimpleNamespace(seek_position=0.0))
    seeked = SimpleNamespace(streamdetails=SimpleNamespace(seek_position=74.0))

    player = _bare_player(provider)
    player.mass = SimpleNamespace(player_queues=SimpleNamespace(
        get_item=lambda _qid, item_id: seeked if item_id == "wanted" else None,
        get=lambda _qid: SimpleNamespace(current_item=stale)))

    media = SimpleNamespace(source_id="q1", queue_item_id="wanted")
    assert player._seek_offset_ms(media) == 74000


def test_an_unknown_item_id_falls_back_to_the_queue_position():
    """Better the queue's guess than no offset at all."""
    provider = _provider_module()

    current = SimpleNamespace(streamdetails=SimpleNamespace(seek_position=30.0))
    player = _bare_player(provider)
    player.mass = SimpleNamespace(player_queues=SimpleNamespace(
        get_item=lambda _qid, _item_id: None,
        get=lambda _qid: SimpleNamespace(current_item=current)))

    media = SimpleNamespace(source_id="q1", queue_item_id="gone")
    assert player._seek_offset_ms(media) == 30000


# --- what a poll is allowed to erase ----------------------------------------


def _polled(provider, previous=None):
    player = _bare_player(provider)
    player._attr_current_media = previous
    return player


def _media(provider, **kwargs):
    from music_assistant_models.player import PlayerMedia

    return PlayerMedia(uri="ampere://p1/Song", title="Song", **kwargs)


def _info(**progress):
    return {"state": "PLAYING", "infoText": {"title": "Song"},
            "progress": progress}


def test_a_poll_without_a_duration_keeps_the_one_we_had():
    """The duration visibly reset to zero on every resync.

    current_media is rebuilt from scratch each poll, so a field Alexa omitted
    was erased rather than left alone, taking the progress bar with it.
    """
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240))

    player._apply_state(_info(mediaProgress=5))

    assert player._attr_current_media.duration == 240


def test_a_poll_that_does_report_a_duration_is_believed():
    """mediaLength is seconds. This test asserted 300 against a payload of
    300000, which only passed while the code divided by 1000, so the two wrong
    halves agreed with each other and the pair looked correct."""
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240))

    player._apply_state(_info(mediaProgress=5, mediaLength=300))

    assert player._attr_current_media.duration == 300


def test_a_zero_duration_is_not_a_duration():
    """Alexa sends 0 around a transition; it means unknown, not instantaneous."""
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240))

    player._apply_state(_info(mediaProgress=5, mediaLength=0))

    assert player._attr_current_media.duration == 240


def test_a_new_track_does_not_inherit_the_old_metadata():
    """Carrying values forward is only safe while the title is unchanged."""
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240, artist="Someone"))

    player._apply_state({"state": "PLAYING",
                         "infoText": {"title": "A Different Song"},
                         "progress": {}})

    assert player._attr_current_media.title == "A Different Song"
    assert player._attr_current_media.duration is None
    assert player._attr_current_media.artist is None


def test_artist_and_art_survive_a_thin_poll_too():
    """Alexa omits more than just the duration, especially on a group."""
    provider = _provider_module()
    player = _polled(provider, _media(
        provider, artist="Someone", album="A Record",
        image_url="https://art.test/1.jpg"))

    player._apply_state(_info(mediaProgress=5))

    media = player._attr_current_media
    assert media.artist == "Someone"
    assert media.album == "A Record"
    assert media.image_url == "https://art.test/1.jpg"


def test_an_idle_speaker_does_not_log_on_every_poll():
    """An Echo with nothing playing answers with no title forever.

    Logging that per poll turns a signal into 36 lines a minute, which is how
    a log stops being read at all.
    """
    provider = _provider_module()
    lines = []

    player = _polled(provider, None)
    player.logger = SimpleNamespace(info=lambda m, *a: lines.append(m % a),
                                    warning=lambda *a, **k: None,
                                    debug=lambda *a, **k: None)
    for _ in range(5):
        player._apply_state({"state": "IDLE", "infoText": {}, "progress": {}})

    assert lines == []


def test_losing_the_title_mid_track_is_worth_one_line():
    provider = _provider_module()
    lines = []

    player = _polled(provider, _media(provider, duration=240))
    player.logger = SimpleNamespace(info=lambda m, *a: lines.append(m % a),
                                    warning=lambda *a, **k: None,
                                    debug=lambda *a, **k: None)

    player._apply_state({"state": "PLAYING", "infoText": {}, "progress": {}})

    assert len(lines) == 1
    assert "stopped reporting a track title" in lines[0]
    assert player._attr_current_media.title == "Song", "media left as it was"


# --- letting MA's queue follow Alexa ----------------------------------------


def _with_queue(provider, player, *titles):
    # name is the composite MA builds, "Artist - Title"; media_item.name is the
    # track title on its own. Tests pass whichever shape they mean.
    items = [SimpleNamespace(name=t, media_item=None, queue_item_id=f"id-{i}")
             for i, t in enumerate(titles)]
    player.mass = SimpleNamespace(
        player_queues=SimpleNamespace(items=lambda _qid: items))
    return player


def test_the_playing_title_is_matched_against_the_live_queue():
    """The map built at play_media is memory only.

    It is empty after a restart and after a queue transferred in from another
    player, and those are exactly the moments MA's queue index has no other way
    to catch up: the group was audibly on one track while MA showed the track
    the queue had arrived holding.
    """
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None),
                         "And I Love You So", "Triste", "My Way")

    assert player._queue_item_for("My Way") == "id-2"


def test_the_prebuilt_map_wins_when_it_has_the_answer():
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None), "My Way")
    player._titles_to_items = {"my way": "from-the-map"}

    assert player._queue_item_for("My Way") == "from-the-map"


def test_matching_ignores_case_the_way_alexa_reports_it():
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None), "THAT'S LIFE")

    assert player._queue_item_for("That's Life") == "id-0"


def test_a_title_that_is_not_in_the_queue_matches_nothing():
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None), "My Way")

    assert player._queue_item_for("Something Else") is None


def test_no_queue_at_all_is_not_a_crash():
    """Polling starts before anything has ever played on this player."""
    provider = _provider_module()
    player = _polled(provider, None)

    def explode(_qid):
        raise KeyError("no queue")

    player.mass = SimpleNamespace(
        player_queues=SimpleNamespace(items=explode))

    assert player._queue_item_for("My Way") is None


def test_the_polled_media_names_the_queue_it_belongs_to():
    """MA will not believe a queue_item_id without a matching source_id.

    PlayerQueues._parse_player_current_item_id requires both, and its fallbacks
    parse a Sonos uri or an MA stream url, neither of which an ampere:// uri
    can look like. Without source_id the queue index never advances and MA goes
    on showing whatever track the queue was holding when it arrived.
    """
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None), "My Way")

    player._apply_state({"state": "PLAYING",
                         "infoText": {"title": "My Way"},
                         "progress": {"mediaProgress": 1}})

    media = player._attr_current_media
    assert media.source_id == "p1", "must equal the player id, which keys the queue"
    assert media.queue_item_id == "id-0"


def test_alexa_reports_a_title_where_ma_holds_artist_and_title():
    """The reason queue following never worked, on any track.

    MA names a queue item "Artist - Title"; Alexa reports the title alone.
    Comparing them directly never matched once, and with no match MA cannot
    parse a current item id, so _update_queue_from_player returns early and
    both the queue index and the scrubber freeze. Measured 2026-08-02 against
    a live queue of ['Perry Como - And I Love You So', 'Ramones - Blitzkrieg
    Bop', ...] while Alexa reported 'Mony, Mony'.
    """
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None),
                         "Perry Como - And I Love You So",
                         "The White Stripes - Seven Nation Army")

    assert player._queue_item_for("Seven Nation Army") == "id-1"
    assert player._queue_item_for("And I Love You So") == "id-0"


def test_the_media_items_own_name_is_preferred():
    """The composite is a display string; the media item carries the truth."""
    provider = _provider_module()
    item = SimpleNamespace(name="Some Artist - Wrong Display",
                           media_item=SimpleNamespace(name="Real Title"),
                           queue_item_id="id-x")
    player = _polled(provider, None)
    player.mass = SimpleNamespace(
        player_queues=SimpleNamespace(items=lambda _qid: [item]))

    assert player._queue_item_for("Real Title") == "id-x"


def test_the_whole_composite_still_matches():
    """Some sources may report the composite; it must not stop working."""
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None),
                         "Robyn - Dancing On My Own")

    assert player._queue_item_for("Robyn - Dancing On My Own") == "id-0"


def test_a_hyphen_in_the_artist_does_not_eat_the_title():
    """rpartition, not partition: split on the last separator."""
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None),
                         "Emerson, Lake - Palmer - Lucky Man")

    assert player._queue_item_for("Lucky Man") == "id-0"


def test_a_title_containing_a_hyphen_survives():
    provider = _provider_module()
    player = _with_queue(provider, _polled(provider, None),
                         "Robyn - Dancing On My Own - Radio Edit")

    assert player._queue_item_for("Radio Edit") == "id-0"


# --- tracks that are not on the Subsonic server -----------------------------
#
# Phase 2 of PLAN.md. A queue Music Assistant composes can hold a Spotify or
# Tidal track, which has no Subsonic id at all and used to be dropped from the
# published list without playing. It now travels as an object the bridge
# fetches back out of MA.


def _bare_provider(provider, *, allow_ma=True):
    """A provider with no __init__ run, for the queue-mapping methods only.

    Standing up a real one logs into Amazon. These methods read config, a
    logger and the stream route, and nothing else.
    """
    instance = provider.AmpereAlexaProvider.__new__(provider.AmpereAlexaProvider)
    instance.logger = SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None,
        debug=lambda *a, **k: None)
    instance.config = SimpleNamespace(
        get_value=lambda key, default=None: (
            allow_ma if key == provider.CONF_MA_SOURCE else default
        )
    )
    return instance


def _mapping(domain, item_id, available=True):
    return SimpleNamespace(
        provider_domain=domain, item_id=item_id, available=available)


def _item(name, uri="", mappings=(), artists=(), album="", duration=0, image=None):
    media_item = SimpleNamespace(
        name=name, uri=uri, provider_mappings=list(mappings),
        artists=[SimpleNamespace(name=a) for a in artists],
        album=SimpleNamespace(name=album) if album else None,
        duration=duration, image=image,
    )
    return SimpleNamespace(
        name=name, uri=uri, media_item=media_item, queue_item_id=f"q-{name}",
        duration=duration, image=image,
    )


def test_a_subsonic_track_is_published_as_a_bare_id():
    """Unchanged from before phase 2, and that is the point."""
    provider = _provider_module()
    instance = _bare_provider(provider)

    tracks, _titles = instance.publish_tracks(
        [_item("Light Year", mappings=[_mapping("opensubsonic", "t1")])]
    )
    assert tracks == ["t1"]


def test_a_track_with_no_subsonic_id_is_published_as_a_music_assistant_track():
    """It used to be dropped and silently not play."""
    provider = _provider_module()
    from ma_provider import stream_ref

    instance = _bare_provider(provider)
    tracks, _titles = instance.publish_tracks([
        _item("Dancing On My Own", uri="spotify://track/abc",
              artists=("Robyn",), album="Body Talk", duration=293),
    ])

    (track,) = tracks
    assert track["source"] == "ma"
    assert stream_ref.decode_ref(track["ref"]) == "spotify://track/abc"
    assert track["title"] == "Dancing On My Own"
    assert track["artist"] == "Robyn"
    assert track["album"] == "Body Talk"
    assert track["duration"] == 293


def test_subsonic_wins_when_a_track_is_on_both():
    """Deliberate, not incidental.

    Navidrome serves a finite file with Accept-Ranges, so those tracks seek and
    survive being moved between rooms. Music Assistant's audio is realtime and
    does neither. Routing a track through MA that Subsonic already has would
    trade working features for nothing.
    """
    provider = _provider_module()
    instance = _bare_provider(provider)

    tracks, _titles = instance.publish_tracks([
        _item("Light Year", uri="spotify://track/abc",
              mappings=[_mapping("opensubsonic", "t1")]),
    ])
    assert tracks == ["t1"]


def test_an_unavailable_subsonic_mapping_falls_through_to_music_assistant():
    """A mapping that exists but is marked unavailable cannot be streamed."""
    provider = _provider_module()
    instance = _bare_provider(provider)

    tracks, _titles = instance.publish_tracks([
        _item("Light Year", uri="spotify://track/abc",
              mappings=[_mapping("opensubsonic", "t1", available=False)]),
    ])
    assert isinstance(tracks[0], dict)


def test_the_two_kinds_keep_their_order_in_one_queue():
    """MA does not group its queue by source, so the publish must not either."""
    provider = _provider_module()
    instance = _bare_provider(provider)

    tracks, _titles = instance.publish_tracks([
        _item("A", mappings=[_mapping("opensubsonic", "t1")]),
        _item("B", uri="spotify://track/b"),
        _item("C", mappings=[_mapping("opensubsonic", "t2")]),
    ])
    assert [type(t) for t in tracks] == [str, dict, str]
    assert tracks[0] == "t1" and tracks[2] == "t2"


def test_turning_the_setting_off_goes_back_to_dropping_them():
    """The escape hatch, for a source that turns out to misbehave."""
    provider = _provider_module()
    instance = _bare_provider(provider, allow_ma=False)

    tracks, _titles = instance.publish_tracks([
        _item("A", mappings=[_mapping("opensubsonic", "t1")]),
        _item("B", uri="spotify://track/b"),
    ])
    assert tracks == ["t1"]


def test_an_item_with_no_uri_cannot_be_published_either_way():
    """There is nothing to name it by, so it is still dropped."""
    provider = _provider_module()
    instance = _bare_provider(provider)

    tracks, _titles = instance.publish_tracks([_item("Mystery")])
    assert tracks == []


def test_only_art_amazon_can_reach_is_carried():
    """Amazon fetches art itself, and MA's image proxy is on the tailnet.

    A Spotify or Tidal cover is on a public CDN and is handed over as-is; a
    local file's artwork is left out rather than pointed at a host Amazon
    cannot resolve.
    """
    provider = _provider_module()
    instance = _bare_provider(provider)

    public = SimpleNamespace(remotely_accessible=True,
                             path="https://i.scdn.co/image/abc")
    private = SimpleNamespace(remotely_accessible=False, path="/local/cover.jpg")

    tracks, _titles = instance.publish_tracks([
        _item("A", uri="spotify://track/a", image=public),
        _item("B", uri="filesystem_local://track/b", image=private),
    ])
    assert tracks[0]["art_url"] == "https://i.scdn.co/image/abc"
    assert "art_url" not in tracks[1]


def test_the_title_index_still_covers_both_kinds():
    """MA follows Alexa by name, and that has to work for every track played."""
    provider = _provider_module()
    instance = _bare_provider(provider)

    _tracks, titles = instance.publish_tracks([
        _item("A", mappings=[_mapping("opensubsonic", "t1")]),
        _item("B", uri="spotify://track/b"),
    ])
    assert titles == {"a": "q-A", "b": "q-B"}


# --- what Alexa reports about position --------------------------------------


def _progress_player(provider, payload):
    player = _bare_player(provider)
    player.mass = SimpleNamespace(player_queues=SimpleNamespace(
        get=lambda _q: None, get_item=lambda _q, _i: None))
    player._attr_current_media = None
    player._attr_playback_state = None
    player._attr_name = "Kitchen Echo"
    player.update_state = lambda *a, **k: None
    player._apply_state(payload)
    return player


def test_position_and_length_are_read_as_seconds():
    """Both are seconds, whatever the millisecond-shaped names suggest.

    Measured 2026-08-03 against a live Echo: mediaLength stayed 226 for a
    3:46 track while mediaProgress climbed 11 -> 21 -> 32 -> 42 across four
    ten-second polls. Dividing by 1000 made the position advance at a
    thousandth of real time, which is what "the scrubber never moves" was.
    """
    provider = _provider_module()
    player = _progress_player(provider, {
        "playerInfo": {"state": "PLAYING"},
        "infoText": {"title": "Harder, Better, Faster, Stronger"},
        "progress": {"mediaLength": 226, "mediaProgress": 42,
                     "allowScrubbing": False},
    })

    assert player._attr_elapsed_time == 42
    assert player._attr_current_media.duration == 226


def test_a_length_of_zero_is_no_length_rather_than_a_zero_length():
    provider = _provider_module()
    player = _progress_player(provider, {
        "playerInfo": {"state": "PLAYING"},
        "infoText": {"title": "Something"},
        "progress": {"mediaLength": 0, "mediaProgress": 0},
    })
    assert player._attr_current_media.duration is None


# --- publishing without the round trip --------------------------------------
#
# Inside Music Assistant the bridge is this process, so `LocalBridge` calls
# `queue_api.publish` directly. It has to be interchangeable with `BridgeClient`
# at the one call site in the provider, so these mirror the HTTP tests above.


@pytest.fixture
def local_queue_dir(tmp_path, monkeypatch):
    from ma_provider import queue_api

    monkeypatch.setattr(queue_api, "STATE_DIR", tmp_path / "external")
    queue_api.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return queue_api


def test_a_local_publish_returns_a_content_id(local_queue_dir, fake_subsonic):
    content_id = asyncio.run(bridge.LocalBridge().publish_queue(["t1", "t2"], "Evening"))

    assert content_id.startswith(f"{local_queue_dir.CONTENT_PREFIX}:")
    stored = local_queue_dir.resolve(content_id.split(":", 1)[1])
    assert [s["id"] for s in stored] == ["t1", "t2"]


def test_a_local_publish_is_the_same_content_id_as_republishing(local_queue_dir,
                                                                fake_subsonic):
    """The token hashes the track list, so an unchanged queue keeps its id.

    That property is what stops a republish orphaning the queue Alexa is
    already holding, and it must survive the move off HTTP.
    """
    once = asyncio.run(bridge.LocalBridge().publish_queue(["t1", "t2"]))
    again = asyncio.run(bridge.LocalBridge().publish_queue(["t1", "t2"]))
    assert once == again


def test_a_local_publish_stringifies_track_ids(local_queue_dir, monkeypatch):
    """Same coercion the HTTP body did, now that there is no body.

    Asserted on what `publish` was handed rather than on the outcome, because a
    caller holding ids as ints would otherwise fail deep inside the Subsonic
    lookup with a message about an unknown song.
    """
    seen = {}

    def fake_publish(tracks, name="", start_offset_ms=0):
        seen.update(tracks=tracks, name=name, start_offset_ms=start_offset_ms)
        return {"token": "abc", "tracks": [{"id": "123"}], "requested": 1}

    monkeypatch.setattr(local_queue_dir, "publish", fake_publish)
    asyncio.run(bridge.LocalBridge().publish_queue([123], "Evening", -5))

    assert seen["tracks"] == ["123"], "ids must reach publish as strings"
    assert seen["name"] == "Evening"
    assert seen["start_offset_ms"] == 0, "a negative offset is clamped, not passed"


def test_a_local_publish_refuses_an_empty_queue():
    with pytest.raises(bridge.BridgeError):
        asyncio.run(bridge.LocalBridge().publish_queue([]))


def test_a_local_publish_refuses_a_queue_with_nothing_playable(local_queue_dir,
                                                               fake_subsonic):
    """Every id unknown to the library is a failure, not an empty success.

    The HTTP path raised here because the bridge answered without a
    content_id. Locally there is no response to inspect, so the check is on
    what was actually stored; without it the provider would speak an utterance
    for a queue with no tracks and the Echo would say it found nothing.
    """
    with pytest.raises(bridge.BridgeError):
        asyncio.run(bridge.LocalBridge().publish_queue(["nope-1", "nope-2"]))


def test_both_bridges_present_the_same_call(local_queue_dir, fake_subsonic):
    """The provider has one call site and must not have to know which it holds."""
    for impl in (bridge.LocalBridge(), bridge.BridgeClient("http://x", "t", None)):
        publish = impl.publish_queue
        assert asyncio.iscoroutinefunction(publish)
        names = publish.__code__.co_varnames[:publish.__code__.co_argcount]
        assert names == ("self", "tracks", "name", "start_offset_ms")
