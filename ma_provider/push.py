"""Keeping one HTTP/2 push connection alive, and turning it into events.

`HTTP2EchoClient` opens the stream and hands over directives. It does not
supervise itself: on a close or an error it calls back and stops. A consumer
that assumes otherwise gets a provider which works for an hour and then stops
updating with nothing in the log, which is worse than polling because nothing
looks wrong. So the reconnect, the backoff and the token renewal live here.

Deliberately not a fork of the alexapy client. Everything Amazon-facing stays
upstream's, so a version bump brings its fixes along; this file only decides
*when* to build one and what to do with what comes out.

## Push is an accelerator, never the source of truth

This is the load-bearing decision in the whole design and everything else falls
out of it.

The pinned alexapy (1.29.17) reads the stream with `httpx.Timeout(None)`. A
connection can therefore stop delivering without closing, and there is no
signal: heartbeats never reach `msg_callback`, only parseable lines do, so a
stalled stream is externally indistinguishable from a quiet house. 1.30.0 added
`read_timeout` to close exactly that gap, and `alexapy_compat` detects which is
installed.

Because the pinned version cannot detect a stall, polling is never switched
off, only slowed, and how far it slows depends on whether alexapy can notice a
dead stream. If push silently dies the worst case is state that lags by the
slow interval, which is what today already feels like. That is a bad minute,
not a broken house.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable

from . import alexapy_compat, push_events

# Reconnect backoff. Starts fast because most closes are transient, and caps
# well short of an hour so a stream that died while nobody was listening is
# back before anyone next presses play.
BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
BACKOFF_FACTOR = 2.0

# A connection that stayed up this long is treated as healthy, and the backoff
# resets. Without this a stream that flaps every few minutes would climb to the
# cap and stay there, having been "successful" every time.
STABLE_AFTER = 120.0

# Handed to alexapy when it supports it: close a stream that has delivered
# nothing at all for this long, so this class can rebuild it.
STALE_AFTER = 300.0


class PushStream:
    """One supervised push connection for one Amazon account.

    The stream is per account, not per device, which is what makes this worth
    doing: ten Echoes cost one connection rather than ten pollers. Groups
    arrive on it as first-class devices.
    """

    def __init__(
        self,
        *,
        auth: Any,
        on_event: Callable[[push_events.PushEvent], None],
        logger: Any,
        on_health: Callable[[bool], None] | None = None,
    ) -> None:
        self.auth = auth
        self.on_event = on_event
        self.logger = logger
        self.on_health = on_health

        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._stop = asyncio.Event()
        self._closed = asyncio.Event()
        self._connected = False
        self._opened_at = 0.0
        self._backoff = BACKOFF_START
        self.events_seen = 0
        self.last_event_at = 0.0
        self.last_error = ""

    # -- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Begin supervising. Returns immediately; connecting happens behind it.

        Never blocks provider startup on Amazon being reachable. A provider
        that failed to load because a supplementary stream could not connect
        would trade every feature for one.
        """
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Take the stream down and stop reconnecting.

        Order matters: the flag is set before the task is cancelled so a
        reconnect already in flight sees it and gives up rather than racing the
        cancellation and leaving a stream nobody owns.
        """
        self._stop.set()
        self._closed.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._teardown_client()
        self._set_connected(False)

    # -- the supervisor ------------------------------------------------------

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive
                self.last_error = f"{type(err).__name__}: {err}"
                self.logger.debug("push connect failed: %s", self.last_error)

            if self._stop.is_set():
                return

            # A connection that lasted is not evidence of a problem, so the
            # next attempt starts fresh rather than inheriting a backoff earned
            # by an unrelated outage hours ago.
            if self._opened_at and time.monotonic() - self._opened_at > STABLE_AFTER:
                self._backoff = BACKOFF_START

            delay = self._backoff
            self._backoff = min(self._backoff * BACKOFF_FACTOR, BACKOFF_CAP)
            self.logger.info(
                "live updates disconnected; reconnecting in %.0fs", delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _connect_once(self) -> None:
        """Build a client, run it, and wait for it to end.

        The token is renewed first and every time. `HTTP2EchoClient` reads
        `access_token` once, in its constructor, and puts it in a header that
        lives as long as the connection, so a stale token cannot be fixed
        afterwards: it produces a stream that opens and delivers nothing.
        """
        if not await self.auth.ensure_fresh():
            self.last_error = self.auth.state.detail
            raise RuntimeError("no usable token for live updates")

        from alexapy.alexahttp2 import HTTP2EchoClient

        alexapy_compat.ensure_session(self.auth.login)
        self._closed.clear()
        self._client = HTTP2EchoClient(
            self.auth.login,
            msg_callback=self._on_message,
            open_callback=self._on_open,
            close_callback=self._on_close,
            error_callback=self._on_error,
            **alexapy_compat.push_client_kwargs(STALE_AFTER),
        )
        await self._client.async_run()

        # async_run starts the reader and returns. The connection's life is
        # bounded by its callbacks, so this waits on the close signal rather
        # than on the coroutine, and on the token, so a bearer about to expire
        # forces a rebuild while the stream is still healthy instead of after
        # it has quietly stopped being renewable.
        renew_in = self.auth.seconds_until_refresh()
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=renew_in)
        except asyncio.TimeoutError:
            self.logger.debug("cycling the push stream to renew its token")
        finally:
            await self._teardown_client()
            self._set_connected(False)

    async def _teardown_client(self) -> None:
        """Stop a client completely, not just its socket.

        `async_run` starts two tasks: the reader, and a ping loop that sleeps
        299 seconds and repeats forever. Closing the httpx client leaves that
        ping loop running, so every reconnect used to leak one, and each leaked
        loop went on pinging Amazon with the same bearer token. A handful of
        reconnects and the account has several unattached clients pinging on
        its behalf, which is its own reason for Amazon to hang up -- a
        reconnect loop that causes the disconnects it is reconnecting from.

        `on_close` is alexapy's own teardown and cancels both tasks. It also
        calls back into `_on_close` here, which only sets an event that is
        already set, so calling it during teardown is harmless.
        """
        client, self._client = self._client, None
        if client is None:
            return
        # Best effort throughout. Failing to tidy up is not a reason to stop
        # reconnecting, but it must happen before the socket goes.
        with contextlib.suppress(Exception):
            client.on_close()
        for task in list(getattr(client, "_tasks", ()) or ()):
            with contextlib.suppress(Exception):
                task.cancel()
        with contextlib.suppress(Exception):
            await client.client.aclose()

    # -- callbacks from alexapy ----------------------------------------------

    async def _on_open(self) -> None:
        self._opened_at = time.monotonic()
        self._set_connected(True)
        self.last_error = ""
        self.logger.info("live updates connected")

    async def _on_close(self) -> None:
        self._closed.set()
        self._set_connected(False)

    async def _on_error(self, error: str) -> None:
        """Record why the stream ended.

        The first occurrence of each distinct reason is logged at info, not
        debug. A stream that reconnects every few seconds and says only
        "reconnecting" is unactionable: the reason lives in the exception
        alexapy hands here, and putting it at debug meant the one line that
        explained a flapping connection was the one line nobody could see.

        Repeats stay at debug, because a persistent fault would otherwise
        write a line every backoff interval for as long as it lasts.
        """
        text = str(error)
        if text and text != self.last_error:
            self.logger.info("live updates dropped: %s", text)
        else:
            self.logger.debug("push stream error: %s", text)
        self.last_error = text
        self._closed.set()

    async def _on_message(self, message: Any) -> None:
        """One raw directive.

        Every failure here is swallowed on purpose. This runs inside alexapy's
        reader; an exception escaping would kill the stream over one malformed
        payload, and the poll would then be quietly carrying the whole load
        with push still reporting itself connected.
        """
        try:
            events = push_events.decode(message)
        except Exception as err:  # noqa: BLE001
            self.logger.debug("undecodable push directive: %s", type(err).__name__)
            return

        for event in events:
            self.events_seen += 1
            self.last_event_at = time.time()
            try:
                self.on_event(event)
            except Exception as err:  # noqa: BLE001
                self.logger.debug(
                    "handler for %s failed: %s", event.command, err)

    # -- health --------------------------------------------------------------

    def _set_connected(self, value: bool) -> None:
        if value == self._connected:
            return
        self._connected = value
        if self.on_health is not None:
            try:
                self.on_health(value)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("health callback failed: %s", err)

    def status(self) -> dict[str, Any]:
        """What the settings page shows about live updates."""
        return {
            "connected": self._connected,
            "events": self.events_seen,
            "last_event_at": self.last_event_at,
            "error": self.last_error,
            "detects_stalls": alexapy_compat.supports_read_timeout(),
        }
