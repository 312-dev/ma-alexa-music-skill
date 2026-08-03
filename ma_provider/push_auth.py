"""The bearer token Amazon's push stream requires, and where it comes from.

Ampere's REST calls authenticate with a cookie jar. The push stream does not:
`HTTP2EchoClient` sends `Bearer <login.access_token>`, and a cookie session has
no such token. Measured 2026-08-03 against the live account, a stream opened
with a cookie-only login connects with `Bearer None` and Amazon closes it
immediately, delivering nothing. That is the whole reason this module exists.

There is exactly one way to obtain the token and it is not obvious. alexapy's
`AlexaLogin` defaults to `oauth_login=True`, which points the login at a PKCE
`/ap/register` URL whose `/ap/maplanding` redirect carries
`openid.oa2.access_token`. Walking those pages is what mints it. Ampere never
reaches them today because it restores a cookie and `login()` returns early on
a session that already works.

`get_tokens()` cannot substitute. It registers a virtual device against
`/auth/register`, and that request needs an `auth_data` block holding either an
existing bearer token or an authorization code from the PKCE walk. With
cookies alone both are absent and Amazon refuses: confirmed live, it returns
False. So the walk comes first and registration second, never the other way
round.

**Nothing here logs a token, a password or an OTP.** The whole file handles
credentials and the only safe assumption is that anything printed will end up
in a support thread.

Framework-free on purpose. Music Assistant cannot be imported in this repo's
test environment, so anything that has to be tested lives outside the provider
module, which is the same reason `settings.py` was split out.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from . import alexapy_compat

# Refresh this long before the token actually expires. Amazon issues these with
# an hour of life, and a token that expires mid-stream does not fail politely:
# the connection was authorised at construction, so it keeps running and simply
# stops carrying events.
REFRESH_MARGIN = 600.0

# How long a registration file is trusted without any expiry recorded. Older
# alexapy versions did not always populate expires_in, and treating "unknown"
# as "forever" would keep a dead token indefinitely.
ASSUMED_LIFETIME = 3600.0


@dataclass
class AuthState:
    """What the wizard needs to say about push authentication.

    `detail` is written for a person reading a settings page, not for a log
    grep, because that is the only place it is ever shown.
    """

    ok: bool = False
    detail: str = "Not connected."
    needs_login: bool = False
    expires_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class PushAuth:
    """Owns the device registration, its token, and keeping it alive.

    Deliberately a separate `AlexaLogin` from the one the provider uses for
    REST calls. The registration is a distinct identity, with its own uuid and
    its own tokens, and entangling it with the working session would mean a
    failed token refresh could take device discovery down with it. The stream
    is an accelerator; it must never be able to break the thing it accelerates.
    """

    def __init__(
        self,
        *,
        store_path: str,
        url: str,
        email: str,
        logger,
    ) -> None:
        self.store_path = store_path
        self.url = url or "amazon.com"
        self.email = email
        self.logger = logger
        self.login: Any = None
        self.state = AuthState()

    # -- persistence ---------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.store_path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _write(self, record: dict[str, Any]) -> None:
        """Store the registration, readable only by this user.

        Written to a temporary file and renamed, because a process that dies
        mid-write would otherwise leave a truncated file that parses as "no
        registration" and silently sends the operator back through an
        interactive login.
        """
        directory = os.path.dirname(self.store_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{self.store_path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.store_path)

    def forget(self) -> None:
        """Drop the registration. Used when disconnecting the account."""
        self.login = None
        self.state = AuthState(detail="Disconnected.", needs_login=True)
        try:
            os.remove(self.store_path)
        except OSError:
            pass

    # -- lifecycle -----------------------------------------------------------

    async def restore(self) -> bool:
        """Bring back a registration saved by a previous run.

        The supported route: `AlexaLogin(oauth=..., uuid=...)` takes back
        exactly what `oauth_snapshot` produced, so nothing is assigned by hand
        and nothing has to be kept in sync with alexapy's internals.

        Returns False when there is nothing saved, which is not an error. It is
        the state every installation starts in and the one the wizard offers a
        button for.
        """
        saved = self._read()
        oauth = saved.get("oauth")
        if not isinstance(oauth, dict) or not oauth.get("access_token"):
            self.state = AuthState(
                detail="Amazon has not been connected for live updates yet.",
                needs_login=True,
            )
            return False

        try:
            from alexapy import AlexaLogin
        except ImportError:  # pragma: no cover - hard dependency
            return False

        self.login = AlexaLogin(
            url=self.url,
            email=self.email,
            # The registration authenticates with the tokens below. A password
            # is not needed to restore one and is deliberately not read here:
            # this path must keep working when the stored password is stale.
            password="",
            outputpath=lambda path: path,
            oauth=dict(oauth),
            uuid=str(saved.get("uuid") or "") or None,
        )
        alexapy_compat.ensure_session(self.login)
        return await self.ensure_fresh()

    async def adopt(self, login: Any) -> bool:
        """Take the token off a login that has just walked the OAuth pages.

        This is what the interactive sign-in hands over. By the time it is
        called, `login.access_token` has already been captured from the
        maplanding redirect; what remains is to register a virtual device with
        it, which is what actually authorises the push stream.

        `register_capabilities()` is not optional and not cosmetic: alexapy's
        own docstring says it is required for HTTP/2 push. A registration
        without it yields a token that authenticates and a stream that stays
        empty, which is the hardest possible thing to debug because every
        individual step reports success.
        """
        if not getattr(login, "access_token", None):
            self.state = AuthState(
                detail="Signed in, but Amazon did not return a token for live "
                       "updates. Live updates stay off; everything else works.",
                needs_login=True,
            )
            return False

        try:
            registered = await login.get_tokens()
        except Exception as err:
            self.logger.debug("device registration failed: %s", type(err).__name__)
            registered = False
        if not registered:
            self.state = AuthState(
                detail="Amazon refused to register this instance for live "
                       "updates. Everything else still works.",
                needs_login=True,
            )
            return False

        try:
            await login.register_capabilities()
        except Exception as err:
            # Not fatal on its own: the token exists and the stream may still
            # be accepted. Worth recording, because if the stream then delivers
            # nothing this is the first thing to suspect.
            self.logger.info(
                "capability registration did not complete (%s); live updates "
                "may not arrive", type(err).__name__)

        self.login = login
        self._persist()
        self.state = AuthState(ok=True, detail="Connected.",
                               expires_at=self._expires_at())
        return True

    async def ensure_fresh(self) -> bool:
        """Renew the token if it is close to expiring.

        Called before each connect and on a timer while connected. An expired
        bearer does not close the stream, because the stream was authorised
        when it opened; it just stops being usable for the *next* one, so the
        cost of finding out late is a reconnect that fails for a reason that
        looks like an outage.
        """
        if self.login is None:
            return False

        remaining = self._expires_at() - time.time()
        if remaining > REFRESH_MARGIN:
            self.state = AuthState(ok=True, detail="Connected.",
                                   expires_at=self._expires_at())
            return True

        if not getattr(self.login, "refresh_token", None):
            self.state = AuthState(
                detail="The Amazon connection for live updates has expired and "
                       "there is no refresh token. Sign in again to restore it.",
                needs_login=True,
            )
            return False

        try:
            refreshed = await self.login.refresh_access_token()
        except Exception as err:
            self.logger.info("token refresh failed: %s", type(err).__name__)
            refreshed = False

        if not refreshed:
            self.state = AuthState(
                detail="Amazon would not renew the connection for live "
                       "updates. Sign in again to restore it.",
                needs_login=True,
            )
            return False

        self._persist()
        self.state = AuthState(ok=True, detail="Connected.",
                               expires_at=self._expires_at())
        return True

    @property
    def token(self) -> str:
        return str(getattr(self.login, "access_token", "") or "")

    def seconds_until_refresh(self) -> float:
        """How long until `ensure_fresh` would actually do something.

        Never negative and never zero, so a caller sleeping on it cannot spin.
        """
        return max(60.0, self._expires_at() - time.time() - REFRESH_MARGIN)

    # -- internals -----------------------------------------------------------

    def _expires_at(self) -> float:
        raw = getattr(self.login, "expires_in", None)
        if isinstance(raw, (int, float)) and raw > 0:
            # alexapy stores an absolute timestamp here despite the name.
            # Values small enough to be a duration are treated as one rather
            # than as a date in 1970, which would make every token look
            # expired.
            return float(raw) if raw > 1_000_000_000 else time.time() + float(raw)
        return time.time() + ASSUMED_LIFETIME

    def _persist(self) -> None:
        if self.login is None:
            return
        try:
            self._write({
                "oauth": alexapy_compat.oauth_snapshot(self.login),
                "uuid": getattr(self.login, "uuid", ""),
                "email": self.email,
                "url": self.url,
                "saved_at": time.time(),
            })
        except OSError as err:
            # A registration that cannot be written still works for this
            # process. The cost is another interactive sign-in after a restart,
            # which is worth a warning and not worth failing the connection.
            self.logger.warning(
                "could not save the Amazon registration for live updates "
                "(%s); it will have to be set up again after a restart", err)


async def close_quietly(login: Any) -> None:
    """Release a login's session without letting teardown raise.

    Used on the failure paths of an interactive sign-in, where the login is
    being discarded and an exception from `close()` would replace a useful
    error message with an irrelevant one.
    """
    if login is None:
        return
    try:
        await login.close()
    except Exception:  # noqa: BLE001 - teardown must not raise
        pass
    await asyncio.sleep(0)
