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


def test_an_idle_group_does_not_hold_its_members():
    """The bug: every Echo in Whole Apartment vanished from the picker.

    They stayed listed under Settings > Players, which is the tell: MA was not
    hiding them for being unavailable, it was hiding them for being owned.
    """
    provider = _provider_module()
    assert _group(provider).is_active_session is False


def test_a_playing_group_does_hold_its_members():
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    assert _group(provider, PlaybackState.PLAYING).is_active_session is True


def test_a_paused_group_releases_its_members():
    """Against MA's guidance, and on purpose.

    MA says a group should hold its members while paused. That assumes a group
    the user can put down; MA offers this player no stop control, only pause.
    A group that held its members while paused held them until something else
    started, so every Echo in it was permanently missing from the picker.
    """
    provider = _provider_module()
    from music_assistant_models.enums import PlaybackState

    assert _group(provider, PlaybackState.PAUSED).is_active_session is False


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

    player._apply_state(_info(mediaProgress=5000))

    assert player._attr_current_media.duration == 240


def test_a_poll_that_does_report_a_duration_is_believed():
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240))

    player._apply_state(_info(mediaProgress=5000, mediaLength=300000))

    assert player._attr_current_media.duration == 300


def test_a_zero_duration_is_not_a_duration():
    """Alexa sends 0 around a transition; it means unknown, not instantaneous."""
    provider = _provider_module()
    player = _polled(provider, _media(provider, duration=240))

    player._apply_state(_info(mediaProgress=5000, mediaLength=0))

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

    player._apply_state(_info(mediaProgress=5000))

    media = player._attr_current_media
    assert media.artist == "Someone"
    assert media.album == "A Record"
    assert media.image_url == "https://art.test/1.jpg"
