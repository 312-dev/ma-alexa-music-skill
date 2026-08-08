"""mDNS advertisement, and the cases where it must refuse to advertise.

The failure this guards against is subtle: a name that resolves to an address
nothing on the LAN can route to. That is worse than having no name, because it
resolves instantly and then times out, so it reads as an outage rather than as
a networking choice.

The responder is Music Assistant's own shared `AsyncZeroconf`, reached at
`mass.discovery.aiozc`. These tests stand in a fake for it: nothing here touches
the network, and MA itself is not importable in this suite, so the two helpers
that reach into MA (`primary_address`, `_addresses`) are monkeypatched at their
module seam rather than exercised.
"""

from __future__ import annotations

import socket

import pytest

from ma_provider import mdns


class FakeAiozc:
    """A stand-in for `mass.discovery.aiozc` that records register/unregister."""

    def __init__(self) -> None:
        self.registered: list = []
        self.unregistered: list = []
        self.fail: Exception | None = None

    async def async_register_service(self, info) -> None:
        if self.fail is not None:
            raise self.fail
        self.registered.append(info)

    async def async_unregister_service(self, info) -> None:
        self.unregistered.append(info)


class FakeMass:
    """Just enough of `mass` for mdns: a discovery controller holding an aiozc."""

    def __init__(self, aiozc: FakeAiozc) -> None:
        self.discovery = type("Discovery", (), {"aiozc": aiozc})()


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.delenv("MDNS", raising=False)
    monkeypatch.delenv("MDNS_NAME", raising=False)
    yield
    await mdns.stop()


def _at(address: str):
    """Monkeypatch factory: force the probed primary address to `address`."""

    async def _addr() -> str | None:
        return address

    return _addr


async def _pack(address: str) -> list[bytes]:
    """Stand in for the MA-backed address packer, without importing MA."""
    return [socket.inet_aton(address)]


# --- the switch -------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_enable_it(monkeypatch, value):
    monkeypatch.setenv("MDNS", value)
    assert mdns.enabled()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_everything_else_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv("MDNS", value)
    assert not mdns.enabled()


def test_it_is_off_when_unset():
    assert not mdns.enabled()


async def test_advertise_is_a_no_op_when_off():
    mass = FakeMass(FakeAiozc())
    assert await mdns.advertise(mass, 5056) is False


# --- the name ---------------------------------------------------------------


def test_default_name():
    assert mdns.service_name() == "ma-alexa"


@pytest.mark.parametrize("raw,expected", [
    ("Music Assistant", "musicassistant"),
    ("my label", "mylabel"),
    ("a.b.c", "abc"),                # dots would break the .local suffix
    ("-amp-", "amp"),
    ("!!!", "ma-alexa"),             # nothing usable left, fall back
    ("", "ma-alexa"),
])
def test_names_are_reduced_to_a_usable_label(monkeypatch, raw, expected):
    monkeypatch.setenv("MDNS_NAME", raw)
    assert mdns.service_name() == expected


# --- which addresses are worth advertising ----------------------------------


@pytest.mark.parametrize("address", ["192.168.1.50", "10.0.0.4", "100.64.0.1"])
def test_lan_addresses_are_advertised(address):
    assert mdns.reachable(address)


@pytest.mark.parametrize("address", [
    "172.17.0.2",   # docker0, the default bridge
    "172.18.0.5",   # a user-defined bridge
    "127.0.0.1",
    "not-an-address",
])
def test_unreachable_addresses_are_refused(address):
    assert not mdns.reachable(address)


async def test_it_refuses_to_advertise_a_bridge_address(monkeypatch, caplog):
    """The whole reason this is opt-in."""
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "primary_address", _at("172.17.0.2"))
    mass = FakeMass(FakeAiozc())
    assert await mdns.advertise(mass, 5056) is False
    assert "host networking" in caplog.text
    assert mass.discovery.aiozc.registered == []


async def test_it_refuses_when_no_address_can_be_found(monkeypatch):
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "primary_address", _at(None))
    mass = FakeMass(FakeAiozc())
    assert await mdns.advertise(mass, 5056) is False
    assert mass.discovery.aiozc.registered == []


# --- degrading -------------------------------------------------------------


async def test_a_failed_registration_does_not_take_the_bridge_down(monkeypatch):
    """Port 5353 is often already held by Avahi or Bonjour on the host."""
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "primary_address", _at("192.168.1.50"))
    monkeypatch.setattr(mdns, "_addresses", _pack)
    aiozc = FakeAiozc()
    aiozc.fail = OSError("address already in use")
    assert await mdns.advertise(FakeMass(aiozc), 5056) is False


async def test_a_good_registration_publishes_on_the_shared_responder(monkeypatch):
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setenv("MDNS_NAME", "jukebox")
    monkeypatch.setattr(mdns, "primary_address", _at("192.168.1.50"))
    monkeypatch.setattr(mdns, "_addresses", _pack)

    aiozc = FakeAiozc()
    mass = FakeMass(aiozc)

    assert await mdns.advertise(mass, 5056) is True
    assert len(aiozc.registered) == 1
    info = aiozc.registered[0]
    assert info.server == "jukebox.local."
    assert info.port == 5056
    assert info.properties[b"path"] == b"/setup"
    assert info.parsed_addresses() == ["192.168.1.50"]

    await mdns.stop()
    assert aiozc.unregistered == [info]
