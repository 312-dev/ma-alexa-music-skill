"""The guard on the alexapy seam.

alexapy is pinned, and bumping the pin is how Amazon-login fixes arrive. This
provider reaches past its public API in a handful of places, all of them in
`alexapy_compat`, and the failure mode of a rename is the worst kind: nothing
raises at import, the provider loads, and a login quietly stops working on
someone's speakers.

These tests exist so that a bump fails here instead. They are deliberately
about *names existing*, not behaviour: behaviour is alexapy's business and
testing it here would be reimplementing it a second time.

Skipped rather than failed when alexapy is absent, because the pure-Python
parts of this repo are testable without it and a missing optional dependency
is not a defect in this module.
"""

from __future__ import annotations

import inspect

import pytest

from ma_provider import alexapy_compat

alexapy = pytest.importorskip("alexapy", reason="alexapy not installed")


@pytest.mark.parametrize("name", alexapy_compat.REQUIRED_LOGIN_ATTRS)
def test_the_private_names_we_depend_on_still_exist(name):
    """A rename here is silent at runtime, so it has to be loud here."""
    assert hasattr(alexapy.AlexaLogin, name) or name in _slots_and_annotations(), (
        f"alexapy.AlexaLogin has no {name!r}. Music Assistant reaches for it in "
        f"alexapy_compat; check that module against the new version."
    )


@pytest.mark.parametrize("name", alexapy_compat.REQUIRED_LOGIN_API)
def test_the_public_api_we_call_still_exists(name):
    assert hasattr(alexapy.AlexaLogin, name) or name in _slots_and_annotations()


def test_alexalogin_still_accepts_a_saved_registration():
    """`oauth=` and `uuid=` are how a registration survives a restart.

    Without them the only way to restore a token would be to assign the
    attributes by hand, which is exactly the kind of private coupling this
    module exists to avoid. If they ever go away, the persistence design has to
    change rather than quietly degrade into re-registering on every reload.
    """
    parameters = inspect.signature(alexapy.AlexaLogin.__init__).parameters

    assert "oauth" in parameters
    assert "uuid" in parameters
    assert "otp_secret" in parameters
    # The PKCE login is what produces the bearer token push needs. A version
    # that defaulted this off would leave push silently unauthenticated.
    assert parameters["oauth_login"].default is True


def test_the_push_client_still_takes_the_callbacks_we_supply():
    """HTTP2EchoClient is used as-is and supervised from outside.

    Its constructor is the whole integration surface, so a changed signature
    is a changed contract.
    """
    from alexapy.alexahttp2 import HTTP2EchoClient

    parameters = inspect.signature(HTTP2EchoClient.__init__).parameters

    for name in ("login", "msg_callback", "open_callback",
                 "close_callback", "error_callback"):
        assert name in parameters, f"HTTP2EchoClient lost {name!r}"


def test_the_push_client_still_does_not_reconnect_itself():
    """The single most important fact about this dependency.

    `HTTP2EchoClient` calls close_callback and stops. Music Assistant supervises it from
    outside on that basis. If a future version grew its own reconnect, running
    both would produce duplicate streams rather than a fixed one.

    Checked by looking for a reconnect *entry point* rather than the word
    anywhere in the source. The first version of this test matched the
    substring and failed on 1.30.0, where "reconnect" appears only in a comment
    explaining that the *consumer* reconnects, which is this design working as
    intended rather than breaking.
    """
    from alexapy.alexahttp2 import HTTP2EchoClient

    for name in ("reconnect", "_reconnect", "connect_forever", "run_forever"):
        assert not hasattr(HTTP2EchoClient, name), (
            f"alexapy's HTTP2EchoClient grew {name!r}. Music Assistant supervises it "
            f"externally; running both would double-connect."
        )


def test_stream_staleness_support_is_detected_rather_than_assumed():
    """A silent stream is the failure that looks like a quiet house.

    The pinned 1.29.17 cannot detect one: `process_messages` waits forever with
    `httpx.Timeout(None)`. 1.30.0 added `read_timeout`, defaulting to 300s.
    Which one is installed decides whether `push.py` runs its own watchdog, so
    the detection has to match reality on whichever is present.
    """
    from alexapy.alexahttp2 import HTTP2EchoClient

    detected = alexapy_compat.supports_read_timeout()
    actual = "read_timeout" in inspect.signature(
        HTTP2EchoClient.__init__
    ).parameters

    assert detected is actual
    assert alexapy_compat.push_client_kwargs(120.0) == (
        {"read_timeout": 120.0} if actual else {}
    )


def test_the_pinned_version_is_the_one_being_tested():
    """Otherwise this whole file guards the wrong thing.

    The manifest pin is what Music Assistant installs in production. A venv on
    a different version makes every assertion here a statement about a library
    that is not the one running, which is worse than no guard at all because it
    reads like coverage.

    A warning rather than a failure: a developer may legitimately be trying the
    next version, and the point is that they should know they are.
    """
    import pathlib
    import re

    requirements = (
        pathlib.Path(__file__).resolve().parent.parent
        / "ma_provider" / "requirements.txt"
    ).read_text()
    match = re.search(r"alexapy==([\d.]+)", requirements)
    assert match, "the alexapy pin has moved out of ma_provider/requirements.txt"

    if alexapy.__version__ != match.group(1):
        pytest.skip(
            f"installed alexapy {alexapy.__version__} is not the pinned "
            f"{match.group(1)}; these guards describe a version that is not "
            f"the one production runs"
        )


def _slots_and_annotations() -> set[str]:
    """Attributes set in __init__ are not class attributes.

    hasattr on the class misses anything assigned to self, which is most of
    them, so the source of __init__ is consulted as a fallback.
    """
    source = inspect.getsource(alexapy.AlexaLogin.__init__)
    return {
        line.split("=")[0].strip().removeprefix("self.").split(":")[0].strip()
        for line in source.splitlines()
        if "self." in line and "=" in line
    }
