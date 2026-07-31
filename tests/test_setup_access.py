"""Where the setup UI will answer from, and how hard it is to guess into.

The bridge has to be on the public internet for Amazon's sake. The setup UI
must not be, because it can create skills against the operator's Amazon account
and read their library. These tests are the boundary between those two facts.
"""

from __future__ import annotations

import pytest
from flask import Flask

import app as app_module
from setup_ui import access
from setup_ui import views


PUBLIC = "203.0.113.7"
LAN = "192.168.1.50"
TAILNET = "100.85.183.28"
PROXY = "172.18.0.2"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.delenv("SETUP_ALLOW_NETWORKS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    access.reset()
    yield
    access.reset()


@pytest.fixture
def client():
    application = Flask(__name__)
    application.register_blueprint(views.bp)
    application.config.update(TESTING=True)
    return application.test_client()


def get(client, path="/setup", ip=LAN, **headers):
    return client.get(path, environ_overrides={"REMOTE_ADDR": ip}, headers=headers)


# --- which addresses are allowed --------------------------------------------


@pytest.mark.parametrize("ip", ["127.0.0.1", LAN, "10.1.2.3", "172.16.9.9", TAILNET])
def test_private_addresses_reach_setup(client, ip):
    """Tailnet included: 100.64.0.0/10 is a normal way to reach this."""
    assert get(client, ip=ip).status_code != 403


@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "1.1.1.1"])
def test_public_addresses_are_refused(client, ip):
    resp = get(client, ip=ip)
    assert resp.status_code == 403
    assert b"Not from this address" in resp.data


def test_refusal_does_not_depend_on_knowing_the_token(client):
    """A valid token from the internet is still refused. Network first."""
    resp = get(client, ip=PUBLIC, **{"X-Admin-Token": "test-admin-token"})
    assert resp.status_code == 403


def test_any_opens_it_up(client, monkeypatch):
    monkeypatch.setenv("SETUP_ALLOW_NETWORKS", "any")
    assert get(client, ip=PUBLIC).status_code != 403


def test_an_explicit_cidr_list_is_honoured(client, monkeypatch):
    monkeypatch.setenv("SETUP_ALLOW_NETWORKS", "203.0.113.0/24")
    assert get(client, ip=PUBLIC).status_code != 403
    # The default private ranges are replaced, not added to.
    assert get(client, ip=LAN).status_code == 403


def test_an_unparseable_peer_is_refused(client):
    assert get(client, ip="").status_code == 403


# --- X-Forwarded-For --------------------------------------------------------


def test_forwarded_for_is_ignored_without_trusted_proxies(client):
    """The spoofing case. Anyone can set this header."""
    resp = get(client, ip=PUBLIC, **{"X-Forwarded-For": LAN})
    assert resp.status_code == 403


def test_forwarded_for_is_read_when_the_peer_is_a_trusted_proxy(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert get(client, ip=PROXY, **{"X-Forwarded-For": LAN}).status_code != 403


def test_a_trusted_proxy_still_refuses_a_public_client(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert get(client, ip=PROXY, **{"X-Forwarded-For": PUBLIC}).status_code == 403


def test_a_spoofed_prefix_cannot_hide_the_real_client(client, monkeypatch):
    """Walking right to left is what makes a prepended hop useless."""
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    resp = get(client, ip=PROXY, **{"X-Forwarded-For": f"{LAN}, {PUBLIC}"})
    assert resp.status_code == 403


def test_a_chain_of_trusted_proxies_is_walked_through(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    resp = get(client, ip=PROXY, **{"X-Forwarded-For": f"{LAN}, 172.18.0.9"})
    assert resp.status_code != 403


def test_client_ip_helper_directly():
    assert access.client_ip(PROXY, LAN) == PROXY          # no trusted proxies
    assert access.client_ip(PUBLIC, None) == PUBLIC


# --- the verify route is deliberately public --------------------------------


def test_verify_is_reachable_from_the_internet(client):
    """It exists to be opened on a phone with WiFi off. That is the point."""
    resp = client.get("/setup/verify/nosuchtoken",
                      environ_overrides={"REMOTE_ADDR": PUBLIC})
    assert resp.status_code != 403


# --- lockout ----------------------------------------------------------------


def bad_login(client, ip=LAN):
    return client.post("/setup/login", data={"token": "wrong", "target": "/setup"},
                       environ_overrides={"REMOTE_ADDR": ip})


def test_repeated_failures_lock_the_address_out(client):
    for _ in range(access.LOCKOUT_THRESHOLD):
        assert bad_login(client).status_code == 401
    assert bad_login(client).status_code == 429
    assert get(client).status_code == 429


def test_lockout_is_per_address(client):
    for _ in range(access.LOCKOUT_THRESHOLD):
        bad_login(client, ip=LAN)
    assert bad_login(client, ip="192.168.1.77").status_code == 401


def test_a_good_login_clears_the_count(client):
    for _ in range(access.LOCKOUT_THRESHOLD - 1):
        bad_login(client)
    ok = client.post("/setup/login",
                     data={"token": "test-admin-token", "target": "/setup"},
                     environ_overrides={"REMOTE_ADDR": LAN})
    assert ok.status_code in (302, 303)
    assert access.locked_out(LAN) == 0
    assert bad_login(client).status_code == 401


def test_lockout_message_does_not_leak_the_token(client):
    for _ in range(access.LOCKOUT_THRESHOLD):
        bad_login(client)
    assert b"test-admin-token" not in bad_login(client).data


# --- the data plane is unaffected -------------------------------------------


def test_amazon_still_reaches_the_music_endpoint_from_the_internet():
    """The whole point of separating the planes: /music must stay public."""
    c = app_module.app.test_client()
    resp = c.post("/music", json={
        "header": {"namespace": "Alexa.Media.Search",
                   "name": "GetPlayableContent",
                   "messageId": "m", "payloadVersion": "1.0"},
        "payload": {"selectionCriteria": {"attributes": [
            {"type": "ARTIST", "value": "Gregory Alan Isakov",
             "entityId": "artist.a1"}]}},
    }, environ_overrides={"REMOTE_ADDR": PUBLIC})
    assert resp.status_code == 200


# --- /diag and /captures share the boundary ---------------------------------


@pytest.mark.parametrize("path", ["/diag", "/captures"])
def test_ops_routes_refuse_a_valid_token_from_the_internet(path):
    """A leaked token off-LAN should not hand over captured Amazon traffic."""
    c = app_module.app.test_client()
    resp = c.get(path, headers={"X-Admin-Token": "test-admin-token"},
                 environ_overrides={"REMOTE_ADDR": PUBLIC})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/diag", "/captures"])
def test_ops_routes_still_work_on_the_lan(path):
    c = app_module.app.test_client()
    resp = c.get(path, headers={"X-Admin-Token": "test-admin-token"},
                 environ_overrides={"REMOTE_ADDR": LAN})
    assert resp.status_code == 200
