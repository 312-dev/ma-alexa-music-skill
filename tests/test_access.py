"""Which addresses reach the admin plane, and how hard it is to lie about one.

This service is on the public internet for Amazon's sake. Its ops routes are
not: /captures replays inbound Amazon traffic and /diag names the music server,
so a leaked token from off the LAN must not hand either over. These tests are
the boundary between those two facts.

Written against `access` and `core.admin_authorized` directly rather than
through a client. There used to be a Flask wizard whose before-request hook
enforced the same rule and these drove that; the rule now has one
implementation, called by the aiohttp handler with the peer address and the
headers, so that is what they call too.

The lockout counter that lived alongside this went with the wizard. It guarded
a login form, and Music Assistant is already authenticated.
"""

from __future__ import annotations

import pytest

from ma_provider import access
from ma_provider import core

PUBLIC = "203.0.113.7"
LAN = "192.168.1.50"
TAILNET = "100.64.0.1"
PROXY = "172.18.0.2"

TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(core, "ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("SETUP_ALLOW_NETWORKS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    yield


def reaches(ip, token=TOKEN, **headers):
    """Whether the admin plane would answer this request."""
    if token is not None:
        headers["X-Admin-Token"] = token
    return core.admin_authorized(ip, headers)


# --- which addresses are allowed --------------------------------------------


@pytest.mark.parametrize("ip", ["127.0.0.1", LAN, "10.1.2.3", "172.16.9.9", TAILNET])
def test_private_addresses_reach_the_admin_plane(ip):
    """Tailnet included: 100.64.0.0/10 is a normal way to reach this."""
    assert reaches(ip) is True


@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "1.1.1.1"])
def test_public_addresses_are_refused(ip):
    assert reaches(ip) is False


def test_refusal_does_not_depend_on_knowing_the_token():
    """A valid token from the internet is still refused. Network first."""
    assert reaches(PUBLIC, token=TOKEN) is False


def test_a_wrong_token_is_refused_from_the_lan_too():
    """Both halves are required, not either."""
    assert reaches(LAN, token="not-the-token") is False
    assert reaches(LAN, token=None) is False


def test_any_opens_it_up(monkeypatch):
    monkeypatch.setenv("SETUP_ALLOW_NETWORKS", "any")
    assert reaches(PUBLIC) is True


def test_an_explicit_cidr_list_is_honoured(monkeypatch):
    monkeypatch.setenv("SETUP_ALLOW_NETWORKS", "203.0.113.0/24")
    assert reaches(PUBLIC) is True
    # The default private ranges are replaced, not added to.
    assert reaches(LAN) is False


def test_an_unparseable_peer_is_refused():
    assert reaches("") is False
    assert reaches(None) is False


# --- X-Forwarded-For --------------------------------------------------------
#
# The case that made the source check useless in practice: Caddy, Traefik and
# cloudflared all dial this service on loopback by default, so a request from
# the internet arrives looking like 127.0.0.1 and passes the private rule.


def test_forwarded_for_is_ignored_without_trusted_proxies():
    """The spoofing case. Anyone can set this header."""
    assert reaches(PUBLIC, **{"X-Forwarded-For": LAN}) is False


@pytest.mark.parametrize("peer", ["127.0.0.1", "172.17.0.1", LAN])
def test_a_forwarded_request_from_an_untrusted_peer_is_refused(peer):
    assert reaches(peer, **{"X-Forwarded-For": PUBLIC}) is False


def test_even_a_forwarded_lan_client_is_refused_without_trusted_proxies():
    """We cannot tell this from a spoof, so it fails closed."""
    assert reaches("127.0.0.1", **{"X-Forwarded-For": LAN}) is False


def test_naming_the_proxy_turns_the_header_back_on(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1/32")
    assert reaches("127.0.0.1", **{"X-Forwarded-For": LAN}) is True
    assert reaches("127.0.0.1", **{"X-Forwarded-For": PUBLIC}) is False


def test_forwarded_for_is_read_when_the_peer_is_a_trusted_proxy(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert reaches(PROXY, **{"X-Forwarded-For": LAN}) is True


def test_a_trusted_proxy_still_refuses_a_public_client(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert reaches(PROXY, **{"X-Forwarded-For": PUBLIC}) is False


def test_a_spoofed_prefix_cannot_hide_the_real_client(monkeypatch):
    """Walking right to left is what makes a prepended hop useless."""
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert reaches(PROXY, **{"X-Forwarded-For": f"{LAN}, {PUBLIC}"}) is False


def test_a_chain_of_trusted_proxies_is_walked_through(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert reaches(PROXY, **{"X-Forwarded-For": f"{LAN}, 172.18.0.9"}) is True


def test_direct_access_is_unaffected():
    """No proxy header, so nothing changes for LAN or tailnet users."""
    assert reaches(LAN) is True
    assert reaches(TAILNET) is True


def test_client_ip_helper_directly():
    assert access.client_ip(PROXY, LAN) == PROXY          # no trusted proxies
    assert access.client_ip(PUBLIC, None) == PUBLIC
