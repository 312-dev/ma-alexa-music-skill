"""mDNS advertisement, and the cases where it must refuse to advertise.

The failure this guards against is subtle: a name that resolves to an address
nothing on the LAN can route to. That is worse than having no name, because it
resolves instantly and then times out, so it reads as an outage rather than as
a networking choice.
"""

from __future__ import annotations

import pytest

import mdns


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("MDNS", raising=False)
    monkeypatch.delenv("MDNS_NAME", raising=False)
    yield
    mdns.stop()


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


def test_advertise_is_a_no_op_when_off():
    assert mdns.advertise(5056) is False


# --- the name ---------------------------------------------------------------


def test_default_name():
    assert mdns.service_name() == "ampere"


@pytest.mark.parametrize("raw,expected", [
    ("Ampere", "ampere"),
    ("my ampere", "myampere"),
    ("amp.ere", "ampere"),          # dots would break the .local suffix
    ("-amp-", "amp"),
    ("!!!", "ampere"),              # nothing usable left, fall back
    ("", "ampere"),
])
def test_names_are_reduced_to_a_usable_label(monkeypatch, raw, expected):
    monkeypatch.setenv("MDNS_NAME", raw)
    assert mdns.service_name() == expected


# --- which addresses are worth advertising ----------------------------------


@pytest.mark.parametrize("address", ["192.168.1.50", "10.0.0.4", "100.85.183.28"])
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


def test_it_refuses_to_advertise_a_bridge_address(monkeypatch, caplog):
    """The whole reason this is opt-in."""
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "HAVE_ZEROCONF", True)
    monkeypatch.setattr(mdns, "primary_address", lambda: "172.17.0.2")
    assert mdns.advertise(5056) is False
    assert "host networking" in caplog.text


def test_it_refuses_when_no_address_can_be_found(monkeypatch):
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "HAVE_ZEROCONF", True)
    monkeypatch.setattr(mdns, "primary_address", lambda: None)
    assert mdns.advertise(5056) is False


# --- degrading -------------------------------------------------------------


def test_a_missing_zeroconf_package_is_a_warning_not_a_crash(monkeypatch, caplog):
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "HAVE_ZEROCONF", False)
    assert mdns.advertise(5056) is False
    assert "zeroconf" in caplog.text


def test_a_failed_registration_does_not_take_the_bridge_down(monkeypatch):
    """Port 5353 is often already held by Avahi or Bonjour on the host."""
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setattr(mdns, "HAVE_ZEROCONF", True)
    monkeypatch.setattr(mdns, "primary_address", lambda: "192.168.1.50")

    def boom(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(mdns, "Zeroconf", boom)
    assert mdns.advertise(5056) is False


def test_a_good_registration_publishes(monkeypatch):
    monkeypatch.setenv("MDNS", "1")
    monkeypatch.setenv("MDNS_NAME", "jukebox")
    monkeypatch.setattr(mdns, "HAVE_ZEROCONF", True)
    monkeypatch.setattr(mdns, "primary_address", lambda: "192.168.1.50")

    registered = {}

    class FakeZeroconf:
        def register_service(self, info):
            registered["info"] = info

        def close(self):
            registered["closed"] = True

    monkeypatch.setattr(mdns, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(
        mdns, "ServiceInfo",
        lambda *args, **kwargs: {"args": args, "kwargs": kwargs},
    )

    assert mdns.advertise(5056) is True
    assert registered["info"]["kwargs"]["server"] == "jukebox.local."
    assert registered["info"]["kwargs"]["port"] == 5056
    assert registered["info"]["kwargs"]["properties"]["path"] == "/setup"

    mdns.stop()
    assert registered.get("closed") is True
