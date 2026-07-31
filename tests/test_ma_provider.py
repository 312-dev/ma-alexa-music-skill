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
