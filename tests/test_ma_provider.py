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

import pytest

from ma_provider import bridge, utterance

REPO = pathlib.Path(__file__).resolve().parent.parent


# --- utterance --------------------------------------------------------------


def test_command_for_a_single_device():
    """No target: the Echo the command is delivered to plays it."""
    assert (
        utterance.custom_command("ampere", "music assistant")
        == "ask ampere to play music assistant"
    )


def test_command_for_a_group():
    """A group has no dialog interface, so the group is named in the sentence."""
    assert (
        utterance.custom_command("ampere", "music assistant", "whole apartment")
        == "ask ampere to play music assistant on whole apartment"
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
        "ask ampere to play music assistant"
    )
    assert utterance.custom_command("ampere", "music assistant", None) == (
        "ask ampere to play music assistant"
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
    assert body == {"tracks": ["t1", "t2"], "name": "Evening"}
    assert client.headers["X-Admin-Token"] == "tok"


def test_publish_posts_and_returns_the_content_id():
    session = FakeSession(FakeResponse(payload={"content_id": "ext:abc", "count": 2}))
    client = bridge.BridgeClient("https://ampere.example", "tok", session)

    content_id = asyncio.run(client.publish_queue(["t1", "t2"], "Evening"))

    assert content_id == "ext:abc"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://ampere.example/queue")
    assert kwargs["json"] == {"tracks": ["t1", "t2"], "name": "Evening"}
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


def test_seek_is_not_in_supported_features():
    """alexapy has no seek at all, and SeekController is a Video API.

    Declaring a feature Alexa cannot perform makes MA draw a scrubber that
    silently does nothing, which is worse than not offering one.
    """
    provider = _provider_module()
    from music_assistant_models.enums import PlayerFeature

    assert PlayerFeature.SEEK not in provider.PLAYER_FEATURES
    assert PlayerFeature.VOLUME_SET in provider.PLAYER_FEATURES
    assert PlayerFeature.NEXT_PREVIOUS in provider.PLAYER_FEATURES


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
