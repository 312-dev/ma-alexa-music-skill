"""The token lifecycle for the push stream.

Driven by fakes rather than alexapy, because what is being tested is the order
things happen in and what is written down, not Amazon's behaviour. The one
place a real alexapy detail leaks in is `expires_in`, which alexapy stores as
an absolute timestamp despite its name, and getting that wrong makes every
token look either permanently valid or permanently expired.
"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from ma_provider import push_auth


class FakeLogin:
    """Just enough AlexaLogin for the token lifecycle."""

    def __init__(self, *, access_token="tok", refresh_token="ref",
                 register=True, refresh=True, expires_in=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.mac_dms = "mac"
        self.expires_in = (
            expires_in if expires_in is not None else time.time() + 3600
        )
        self.uuid = "UUID0001"
        self._register = register
        self._refresh = refresh
        self.calls: list[str] = []
        self.closed = False

    async def get_tokens(self):
        self.calls.append("get_tokens")
        return self._register

    async def register_capabilities(self):
        self.calls.append("register_capabilities")
        return True

    async def refresh_access_token(self):
        self.calls.append("refresh_access_token")
        if self._refresh:
            self.access_token = "tok2"
            self.expires_in = time.time() + 3600
        return self._refresh

    async def close(self):
        self.closed = True


@pytest.fixture
def auth(tmp_path):
    return push_auth.PushAuth(
        store_path=str(tmp_path / "push" / "registration.json"),
        url="amazon.com",
        email="someone@example.test",
        logger=logging.getLogger("test"),
    )


# -- adopting a fresh sign-in ------------------------------------------------


def test_registration_happens_after_the_token_and_not_before(auth):
    """Order is the whole thing.

    /auth/register needs a bearer token in its auth_data. Called first, with a
    cookie-only session, Amazon refuses: measured live, get_tokens returned
    False. So a login without a token must never reach registration.
    """
    login = FakeLogin(access_token="")

    assert _run(auth.adopt(login)) is False
    assert login.calls == []
    assert auth.state.needs_login is True


def test_capabilities_are_registered_because_push_requires_them(auth):
    """alexapy's own docstring: required for HTTP2/Push.

    Skipping it yields a token that authenticates and a stream that stays
    silent, which every individual step reports as success.
    """
    login = FakeLogin()

    assert _run(auth.adopt(login)) is True
    assert login.calls == ["get_tokens", "register_capabilities"]
    assert auth.state.ok is True


def test_a_refused_registration_is_reported_in_words_a_person_can_act_on(auth):
    login = FakeLogin(register=False)

    assert _run(auth.adopt(login)) is False
    assert auth.state.ok is False
    assert auth.state.needs_login is True
    assert "everything else" in auth.state.detail.lower()


def test_capability_failure_does_not_lose_a_working_token(auth):
    """Not fatal on its own; the token may still be accepted."""
    login = FakeLogin()

    async def boom():
        raise RuntimeError("nope")

    login.register_capabilities = boom

    assert _run(auth.adopt(login)) is True
    assert auth.state.ok is True


def test_a_refused_capability_registration_is_warned_about(auth, caplog):
    """It reports failure by returning False, not by raising.

    So wrapping the call in `try` catches nothing, and the result was being
    discarded entirely. alexapy's own docstring says capabilities are required
    for HTTP/2 push: skipping them yields a token that authenticates, a stream
    that opens, and no events at all, with every individual step reporting
    success. That is the single hardest failure in this whole feature to
    diagnose, so it has to be said out loud.
    """
    caplog.set_level(logging.WARNING)
    login = FakeLogin()

    async def refused():
        return False

    login.register_capabilities = refused

    _run(auth.adopt(login))

    assert "capabilities" in caplog.text.lower()


# -- persistence -------------------------------------------------------------


def test_the_registration_is_written_where_a_restart_can_find_it(auth):
    _run(auth.adopt(FakeLogin()))

    saved = json.loads(open(auth.store_path).read())

    assert saved["oauth"]["access_token"] == "tok"
    assert saved["oauth"]["refresh_token"] == "ref"
    assert saved["uuid"] == "UUID0001"


def test_the_registration_file_is_not_world_readable(auth):
    """It holds a bearer token for the whole Amazon account."""
    _run(auth.adopt(FakeLogin()))

    assert os.stat(auth.store_path).st_mode & 0o077 == 0


def test_a_half_written_file_cannot_be_left_behind(auth, monkeypatch):
    """Written and renamed, so a crash cannot produce a parseable-but-empty file.

    A truncated file reads as "never registered" and sends the operator back
    through an interactive login for no reason.
    """
    _run(auth.adopt(FakeLogin()))
    original = open(auth.store_path).read()

    real_replace = os.replace

    def die(src, dst):
        raise OSError("crash between write and rename")

    monkeypatch.setattr(os, "replace", die)
    auth.login.access_token = "newer"
    auth._persist()
    monkeypatch.setattr(os, "replace", real_replace)

    assert open(auth.store_path).read() == original


def test_forgetting_removes_the_file_and_asks_for_a_new_sign_in(auth):
    _run(auth.adopt(FakeLogin()))

    auth.forget()

    assert not os.path.exists(auth.store_path)
    assert auth.login is None
    assert auth.state.needs_login is True


def test_nothing_saved_is_a_state_not_an_error(auth):
    """Every installation starts here."""
    assert _run(auth.restore()) is False
    assert auth.state.needs_login is True
    assert auth.state.ok is False


def test_an_unreadable_store_does_not_raise(auth):
    os.makedirs(os.path.dirname(auth.store_path), exist_ok=True)
    with open(auth.store_path, "w") as handle:
        handle.write("{not json")

    assert _run(auth.restore()) is False


# -- expiry ------------------------------------------------------------------


def test_expires_in_is_read_as_the_absolute_time_alexapy_stores(auth):
    """Despite the name. Reading it as a duration dates every token to 1970."""
    auth.login = FakeLogin(expires_in=time.time() + 3600)

    assert auth._expires_at() > time.time() + 3000


def test_a_value_small_enough_to_be_a_duration_is_treated_as_one(auth):
    """Defensive: older versions did not always store an absolute value."""
    auth.login = FakeLogin(expires_in=3600)

    assert auth._expires_at() > time.time() + 3000


def test_a_token_near_expiry_is_refreshed_before_it_is_needed(auth):
    """An expired bearer does not close the stream; it breaks the next one."""
    login = FakeLogin(expires_in=time.time() + 60)
    auth.login = login

    assert _run(auth.ensure_fresh()) is True
    assert "refresh_access_token" in login.calls
    assert login.access_token == "tok2"


def test_a_healthy_token_is_left_alone(auth):
    login = FakeLogin(expires_in=time.time() + 7200)
    auth.login = login

    assert _run(auth.ensure_fresh()) is True
    assert login.calls == []


def test_a_refreshed_token_is_written_back(auth):
    _run(auth.adopt(FakeLogin(expires_in=time.time() + 60)))

    _run(auth.ensure_fresh())

    assert json.loads(open(auth.store_path).read())["oauth"]["access_token"] == "tok2"


def test_a_refusal_to_refresh_asks_for_a_sign_in_rather_than_retrying_forever(auth):
    auth.login = FakeLogin(expires_in=time.time() + 60, refresh=False)

    assert _run(auth.ensure_fresh()) is False
    assert auth.state.needs_login is True


def test_no_refresh_token_is_reported_rather_than_crashed_on(auth):
    auth.login = FakeLogin(expires_in=time.time() + 60, refresh_token=None)

    assert _run(auth.ensure_fresh()) is False
    assert auth.state.needs_login is True


def test_the_refresh_timer_can_never_spin(auth):
    """A caller sleeping on this must not busy-loop on an expired token."""
    auth.login = FakeLogin(expires_in=time.time() - 10_000)

    assert auth.seconds_until_refresh() >= 60.0


# -- secrets -----------------------------------------------------------------


def test_no_token_is_ever_logged(auth, caplog):
    """Anything printed here ends up in a support thread."""
    caplog.set_level(logging.DEBUG)
    login = FakeLogin(access_token="SUPERSECRETTOKEN", register=False)

    _run(auth.adopt(login))
    _run(auth.ensure_fresh())

    assert "SUPERSECRETTOKEN" not in caplog.text
    assert "SUPERSECRETTOKEN" not in auth.state.detail


def _run(coro):
    import asyncio

    return asyncio.run(coro)
