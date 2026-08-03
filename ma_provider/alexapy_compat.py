"""Everywhere Ampere reaches past alexapy's public API, in one file.

Written against **alexapy 1.29.17**, the version pinned in manifest.json.

alexapy is a real dependency and does the hard part: it tracks Amazon's login
pages, which change without notice, and it owns device registration, token
refresh and the HTTP/2 transport. None of that is reimplemented here and none
of it should be. Bumping the pin is how those fixes arrive, and the point of
this module is to make a bump cheap to verify: one file to reread, and
`tests/test_alexapy_compat.py` fails loudly if any of these names have moved.

There are three private touches and each has a reason that is not laziness:

`_cookiefile`
    alexapy decides where to save cookies from the `outputpath` callable, and
    `save_cookiefile()` reassigns `_cookiefile` from it every time it runs. So
    the attribute cannot be set once and trusted; a caller that wants a
    specific path has to set it immediately before, or supply an `outputpath`
    that produces the path it wants. Music Assistant's own alexa provider does
    exactly this in its `save_cookie()`.

`_session` / `_create_session()`
    The session owns the cookie jar, and there is no public accessor that
    creates one on demand. `login.session` is a read-only property that returns
    None until something else has built it.

`_get_cookies_from_session()`
    alexapy's own `load_cookie()` cannot read the jar format aiohttp writes,
    so a restored session has to be converted back into the plain dict that
    `login(cookies=...)` expects. This is the specific incompatibility that
    makes a saved session reusable at all.

Music Assistant's built-in alexa provider touches `_session`, `_cookiefile`,
`_outputpath` and `_debug` in the same way. That is worth knowing before
treating any of this as unusually fragile: alexapy removing them would break
Music Assistant itself.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, cast

import aiohttp

if TYPE_CHECKING:
    from alexapy import AlexaLogin

# The names this module depends on existing. Kept as data so a test can assert
# them against the installed alexapy rather than discovering a rename at
# runtime, on someone's speakers, as a login that silently stops working.
REQUIRED_LOGIN_ATTRS = (
    "_cookiefile",
    "_session",
    "_create_session",
    "_get_cookies_from_session",
)

# Public API this provider relies on. Listed for the same reason: a public
# method going away is more likely to be noticed in a changelog than a
# parameter quietly changing meaning, but neither should be found out here.
REQUIRED_LOGIN_API = (
    "login",
    "test_loggedin",
    "get_tokens",
    "register_capabilities",
    "refresh_access_token",
    "close",
    "access_token",
    "refresh_token",
    "expires_in",
    "mac_dms",
    "uuid",
    "session",
    "url",
)


def cookie_path(storage_path: str, username: str) -> str:
    """Where the Alexa session for this account lives.

    Deliberately the same directory and filename Music Assistant's own alexa
    provider uses. Two providers configured for one account then share a
    session rather than racing each other into Amazon's rate limit, and this
    becoming a mode on the upstream provider later costs nobody a re-login.
    """
    return os.path.join(storage_path, ".alexa", f"alexa_media.{username}.pickle")


def point_at_cookiefile(login: AlexaLogin, path: str) -> None:
    """Make this login read and write its cookies at `path`.

    Both halves are needed. `_cookiefile` is what the next read consults, and
    `outputpath` is what `save_cookiefile()` rebuilds it from, so setting only
    the first works until the moment alexapy saves and then silently reverts to
    a relative path under the working directory.
    """
    login._cookiefile = [path]
    login._outputpath = lambda _candidate, _fixed=path: _fixed


async def load_cookies(login: AlexaLogin) -> dict[str, str] | None:
    """Restore a saved Alexa session into this login's own session jar.

    aiohttp writes its cookie jar as a pickle that alexapy's `load_cookie()`
    cannot parse, so it is loaded with aiohttp's own loader and the cookies
    handed back for `login(cookies=...)`. Loading it into the session jar
    rather than only returning it preserves the cookie domains, which the auth
    flow needs.

    A corrupt jar returns None, which means "log in from scratch". Raising
    would turn a stale file into a provider that will not load.
    """
    path = login._cookiefile[0] if login._cookiefile else None
    if not path or not await asyncio.to_thread(os.path.exists, path):
        return None
    if login._session is None:
        login._create_session()
    jar = login._session.cookie_jar
    if not isinstance(jar, aiohttp.CookieJar):
        return None
    try:
        await asyncio.to_thread(jar.load, path)
    except (OSError, EOFError, TypeError, ValueError, AttributeError):
        return None
    cookies = login._get_cookies_from_session()
    return cast("dict[str, str]", cookies) if cookies else None


async def save_cookies(login: AlexaLogin, path: str) -> bool:
    """Write this login's session cookies to `path`.

    Used after an interactive login, so the session survives a restart and the
    proxy flow is a one-off rather than something the operator meets again on
    every reload.
    """
    if login._session is None:
        return False
    jar = login._session.cookie_jar
    if not isinstance(jar, aiohttp.CookieJar):
        return False
    await asyncio.to_thread(os.makedirs, os.path.dirname(path), exist_ok=True)
    try:
        await asyncio.to_thread(jar.save, path)
    except (OSError, EOFError, TypeError, AttributeError):
        return False
    return True


def ensure_session(login: AlexaLogin) -> None:
    """Guarantee a session exists.

    `HTTP2EchoClient.__init__` asserts on it, and a login restored from saved
    tokens has never built one, so the assert fires before the stream is ever
    attempted.
    """
    if login._session is None:
        login._create_session()


def supports_read_timeout() -> bool:
    """Whether the installed alexapy detects a silently stalled stream itself.

    1.29.17, the pinned version, does not: a stream can stop delivering without
    closing, and nothing notices. 1.30.0 added `read_timeout` (default 300s),
    which closes the connection so the consumer reconnects.

    Detected rather than assumed, because the pin is chosen to match whatever
    Music Assistant ships for its own alexa provider, not by us, and it will
    move without this repo being touched. On a version that has it we hand the
    job to alexapy; on one that does not, `push.py` runs its own watchdog.
    Neither ever runs both, which would close a healthy stream twice.
    """
    try:
        from alexapy.alexahttp2 import HTTP2EchoClient
    except ImportError:  # pragma: no cover - alexapy is a hard dependency
        return False
    import inspect

    return "read_timeout" in inspect.signature(HTTP2EchoClient.__init__).parameters


def push_client_kwargs(stale_after: float) -> dict[str, Any]:
    """Constructor extras for HTTP2EchoClient, empty on versions without them.

    Passing an unsupported keyword is a TypeError at connect time, which would
    present as push never working rather than as a version mismatch, so the
    feature check happens here rather than at the call site.
    """
    return {"read_timeout": stale_after} if supports_read_timeout() else {}


ALL_VOLUMES_URI = "/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes"


async def device_volumes(login: AlexaLogin) -> dict[str, tuple[int, bool]]:
    """Every device's current volume, keyed by serial, in one request.

    Amazon reports volume inside `playerInfo`, which is empty on a device that
    is not playing. So a speaker that has been idle since startup has no known
    volume at all, and `poll` cannot invent one: `_apply_state` returns early
    on an empty payload precisely because a group answers that way while its
    members are audibly playing.

    The consequence was not obvious until it was measured. Music Assistant
    computes a group's volume from its members and scales each member's
    individual volume to match a new group level, so a member whose volume is
    None cannot be scaled: measured 2026-08-03, a group change asked three
    speakers for a fallback 33 and only the one player with a known volume got
    a correctly interpolated value.

    alexapy has no getter for this -- `set_volume` with no `get_volume` -- and
    neither Home Assistant's integration nor Music Assistant's own alexa
    provider reads it. The endpoint exists all the same, returns every device
    including speaker groups, and costs one request for the whole account
    rather than one per speaker.

    Returns an empty dict on any failure. Volume seeding is an improvement on
    knowing nothing, never a reason for discovery to fail.
    """
    session = getattr(login, "_session", None)
    if session is None:
        return {}
    url = f"https://alexa.{login.url}{ALL_VOLUMES_URI}"
    try:
        async with session.get(
            url, headers=getattr(login, "_headers", {}), ssl=getattr(login, "_ssl", None)
        ) as response:
            if response.status != 200:
                return {}
            payload = await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - a missing seed is not a failure
        return {}

    found: dict[str, tuple[int, bool]] = {}
    for entry in (payload or {}).get("volumes") or ():
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        serial = entry.get("dsn")
        volume = entry.get("speakerVolume")
        if not serial or not isinstance(volume, (int, float)):
            continue
        found[str(serial)] = (int(volume), bool(entry.get("speakerMuted")))
    return found


def oauth_snapshot(login: AlexaLogin) -> dict[str, Any]:
    """The registration, in the exact shape AlexaLogin's constructor takes back.

    `AlexaLogin(oauth=..., uuid=...)` is the supported way to restore a device
    registration, which is why this returns a dict rather than a bespoke type:
    it goes back in the way it came out, with no translation layer to drift.
    """
    return {
        "access_token": login.access_token,
        "refresh_token": login.refresh_token,
        "mac_dms": login.mac_dms,
        "expires_in": login.expires_in,
    }
