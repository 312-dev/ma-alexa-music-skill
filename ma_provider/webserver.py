"""Ampere's public endpoint, served from inside Music Assistant.

The same routes the standalone Flask deployment serves, on aiohttp, in MA's own
process. `core` decides everything; this turns an aiohttp request into the
plain values it takes and its answers back into aiohttp responses. It is the
sibling of `app.py` and is meant to be just as dull.

## Why a listener of its own, and not one of MA's

MA already has two webservers and a `register_dynamic_route` on each, which is
how `stream_route.py` serves audio and how MA's own sonos and dlna providers
work. Registering there would have been less code. It is also unsafe for this
particular endpoint, for a reason that is about MA rather than about Ampere.

Neither of MA's webservers authenticates anything at the HTTP layer. The
request handler is a catch-all that dispatches straight to whatever matched:

    self._webapp.router.add_route("*", "/{tail:.*}", self._handle_catch_all)

MA's auth lives on its WebSocket API, not in front of that. Which is exactly
why the deployment binds MA's vhost to the tailnet and says so in the
Caddyfile: the web UI has no authentication of its own.

Ampere's endpoint has the opposite requirement. Amazon calls it from the public
internet and there is no way to ask them to come from somewhere else. Putting
it on a port MA already serves would publish everything else on that port with
it, including `/command/{queue_id}/next.mp3`, which skips the current track for
anyone who can guess a queue id, and `/announcement/{player_id}`.

So Ampere listens on its own port with its own routing table, and the only
things reachable there are the ones written below. A reverse proxy points the
public hostname at this port and at nothing else.

## Why every handler hops to a thread

`core` is synchronous: Subsonic calls are urllib, and building a station fans
out over a thread pool. Awaiting any of that on MA's event loop would stall
every stream MA is serving for the length of a Navidrome round trip.

The pool is Ampere's own rather than `asyncio.to_thread`'s default, for the
same reason each task on this box has its own cgroup: a burst of Alexa
directives, or one Navidrome that has stopped answering, then exhausts a pool
nobody else is waiting on. Sharing the default executor would let Ampere's bad
day become MA's.
"""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from . import core, queue_api

if TYPE_CHECKING:
    import logging

# Eight, matching the thread count the standalone deployment runs gunicorn
# with. The work is entirely IO bound, so this is a concurrency limit rather
# than a claim about cores.
POOL_SIZE = 8

DEFAULT_PORT = 5056


class AmpereWebServer:
    """The Alexa-facing HTTP endpoint, on a port of its own inside MA."""

    def __init__(self, logger: logging.Logger, port: int = DEFAULT_PORT,
                 host: str | None = None) -> None:
        self.logger = logger
        self.port = port
        # None binds every interface. The proxy in front is what decides who
        # can actually reach this, the same as for the standalone deployment.
        self.host = host
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._pool: ThreadPoolExecutor | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> bool:
        """Raise the listener. Returns whether it is actually serving.

        A busy port does not take the provider down with it. The obvious thing
        is to let the OSError propagate, but this port is one another process
        can legitimately hold: during a migration off the standalone
        deployment, and for as long as a rollback leaves it running, both want
        the same number.

        Failing the whole provider there would also remove every Echo from
        Music Assistant, which is a working feature that has nothing to do with
        whether a socket is free. So the bind failure is loud and specific, the
        provider keeps its players, and the log says exactly what to do.
        """
        try:
            core.require_public_base()
        except RuntimeError as err:
            self.logger.error(
                "Ampere will not serve the Alexa endpoint: %s. Set the public "
                "base URL in this provider's settings. Echo players keep "
                "working.", err,
            )
            return False

        self._pool = ThreadPoolExecutor(
            max_workers=POOL_SIZE, thread_name_prefix="ampere-web"
        )
        app = web.Application()
        app.add_routes(self._routes())

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        try:
            await self._site.start()
        except OSError as err:
            self.logger.error(
                "Ampere could not bind port %s (%s). Something else is already "
                "listening, most likely a standalone Ampere deployment. Stop it, "
                "or set a different endpoint port in this provider's settings. "
                "Echo players keep working; the Alexa endpoint is not being "
                "served from Music Assistant.",
                self.port, err,
            )
            self._site = None
            await self._runner.cleanup()
            self._runner = None
            return False

        self.logger.info("Ampere endpoint listening on port %s", self.port)

        # Optional, and a no-op unless MDNS is set.
        await self._in_thread(core.advertise, self.port)
        return True

    @property
    def serving(self) -> bool:
        return self._site is not None

    async def stop(self) -> None:
        """Take the listener down with the provider that raised it.

        Order matters: the site stops accepting first, then the runner drains,
        and only then is the pool allowed to go. Shutting the pool first would
        strand any request already inside `core`.
        """
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        self.logger.info("Ampere endpoint stopped")

    async def _in_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    # -- routing ------------------------------------------------------------

    def _routes(self) -> list[web.RouteDef]:
        return [
            # The Alexa app opens the endpoint root in a webview, so the same
            # path has to answer both a directive and a human.
            web.post("/", self.music),
            web.post("/music", self.music),
            web.get("/", self.landing),
            web.get("/privacy", self.privacy),
            web.get("/terms", self.terms),

            web.get("/stream/{ident}/{expires}/{sig}", self.stream),
            web.get("/mastream/{ident}/{expires}/{sig}", self.mastream),
            web.get("/art/{ident}/{expires}/{sig}", self.art),

            web.get("/healthz", self.healthz),
            web.get("/captures", self.captures),
            web.get("/diag", self.diag),

            web.get("/oauth/authorize", self.oauth_authorize_form),
            web.post("/oauth/authorize", self.oauth_authorize_submit),
            web.post("/oauth/token", self.oauth_token),

            web.get("/icons/{name:.*}", self.icons),

            # The handoff endpoint. Kept even though the provider now publishes
            # in process, because it is the documented contract and something
            # other than this provider may be speaking it.
            web.post("/queue", self.publish_queue),
            web.get("/queue/{token}", self.show_queue),
        ]

    # -- turning core's answers into aiohttp responses -----------------------

    async def respond(self, request: web.Request, result: Any) -> web.StreamResponse:
        """The one place an answer shape becomes a response.

        Exhaustive on purpose, and it raises rather than falling through. A new
        shape in `core` should fail loudly in both adapters instead of quietly
        rendering as something that happens to be valid.
        """
        match result:
            case core.Json(status, payload):
                return web.json_response(payload, status=status)

            case core.Redirect(location):
                return web.Response(status=302, headers={"Location": location})

            case core.LocalFile(path, content_type):
                # FileResponse answers a Range with a real 206 and a
                # Content-Range, which is what a scrub and a room-to-room move
                # both depend on.
                return web.FileResponse(path, headers={"Content-Type": content_type})

            case core.Page(status, body, content_type, headers):
                ctype, _, charset = content_type.partition("; charset=")
                payload = body.encode(charset or "utf-8") if isinstance(body, str) else body
                return web.Response(body=payload, status=status,
                                    content_type=ctype,
                                    charset=charset or None,
                                    headers=dict(headers))

            case core.Upstream():
                return await self._pipe(request, result)

        raise TypeError(
            f"core returned an answer shape this adapter cannot serve: {result!r}"
        )

    async def _pipe(self, request: web.Request,
                    upstream: core.Upstream) -> web.StreamResponse:
        """Move an upstream body through without holding it in memory.

        Each read is a thread hop because the upstream handle is urllib's and
        blocks. Reading it inline would stall the loop for as long as Navidrome
        takes to produce the next 64 KiB, which on a cold transcode is not
        a small number.
        """
        response = web.StreamResponse(status=upstream.status,
                                      headers=dict(upstream.headers))
        await response.prepare(request)
        sent = 0
        try:
            while chunk := await self._in_thread(upstream.stream.read, core.CHUNK):
                await response.write(chunk)
                sent += len(chunk)
        except (ConnectionResetError, ConnectionError):
            # Alexa closing the socket is how a skip or a stop reaches us.
            self.logger.debug("client went away after %d bytes", sent)
        finally:
            with contextlib.suppress(Exception):
                await self._in_thread(upstream.stream.close)
        return response

    # -- handlers -----------------------------------------------------------

    async def music(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        body = None
        with contextlib.suppress(Exception):
            body = await request.json()
        return await self.respond(request, await self._in_thread(
            core.dispatch, body, dict(request.headers), raw))

    async def stream(self, request: web.Request) -> web.StreamResponse:
        return await self._signed(request, core.serve_stream)

    async def mastream(self, request: web.Request) -> web.StreamResponse:
        return await self._signed(request, core.serve_mastream)

    async def art(self, request: web.Request) -> web.StreamResponse:
        return await self._signed(request, core.serve_art)

    async def _signed(self, request: web.Request,
                      handler: Callable[..., Any]) -> web.StreamResponse:
        """The three proxies differ only in which core function they call.

        Flask parsed `expires` into an int through its route converter, so a
        non-numeric one never reached the handler and simply did not match.
        aiohttp hands over the raw string, so the same request has to be
        refused here rather than raising inside the signature check.
        """
        try:
            expires = int(request.match_info["expires"])
        except (KeyError, ValueError):
            return web.json_response({"error": "bad or expired signature"}, status=403)
        return await self.respond(request, await self._in_thread(
            handler,
            request.match_info["ident"],
            expires,
            request.match_info["sig"],
            request.headers.get("Range"),
        ))

    async def healthz(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, core.healthz())

    async def captures(self, request: web.Request) -> web.StreamResponse:
        if not self._admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await self.respond(request,
                                  await self._in_thread(core.captures_listing))

    async def diag(self, request: web.Request) -> web.StreamResponse:
        if not self._admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await self.respond(request, await self._in_thread(
            core.diagnostics, request.query.get("q", "the")))

    def _admin(self, request: web.Request) -> bool:
        return core.admin_authorized(request.remote, dict(request.headers))

    async def oauth_authorize_form(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request,
                                  core.oauth_authorize_page(dict(request.query)))

    async def oauth_authorize_submit(self, request: web.Request) -> web.StreamResponse:
        form = dict(await request.post())
        return await self.respond(request, core.oauth_authorize_submit(form))

    async def oauth_token(self, request: web.Request) -> web.StreamResponse:
        form = dict(await request.post())
        return await self.respond(request, core.oauth_token_exchange(
            form, request.headers.get("Authorization", "")))

    async def icons(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, await self._in_thread(
            core.icon, request.match_info["name"]))

    async def landing(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, core.landing())

    async def privacy(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, core.privacy())

    async def terms(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, core.terms())

    async def publish_queue(self, request: web.Request) -> web.StreamResponse:
        body = None
        with contextlib.suppress(Exception):
            body = await request.json()
        return await self.respond(request, await self._in_thread(
            queue_api.publish_request, body, dict(request.headers)))

    async def show_queue(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request, await self._in_thread(
            queue_api.show_request,
            request.match_info["token"],
            dict(request.headers),
        ))
