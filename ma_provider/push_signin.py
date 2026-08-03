"""The one button that turns live updates on.

Everything else about push is framework-free and tested. This file is not: it
needs Music Assistant's webserver and its authentication helper, so it is kept
as thin as it can be and holds no logic worth testing in isolation.

The flow is deliberately the one Music Assistant's own alexa provider uses,
step for step: an ACTION entry in the provider settings opens a popup,
`AuthenticationHelper` waits for it to come back, and alexapy's `AlexaProxy`
serves Amazon's real login pages through a route on MA's webserver so a captcha
or a two-factor prompt can be answered by hand.

Two reasons to copy it rather than invent something. It is a flow the operator
has already met if they have ever set up the upstream alexa provider, and
proxying Amazon's login is genuinely hard to get right, so the fewer novel
parts the better.

Where this diverges from upstream is the point of the whole exercise. MA's
provider finishes by saving the cookie and discards `login.access_token`.
That token is the only thing that can authorise a push stream, and it exists
precisely because the proxy walked the PKCE pages to get it, so here it is
handed to `PushAuth` to be registered and kept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import alexapy_compat, push_auth

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# Routes on MA's webserver, live only for the duration of one sign-in. The
# signin path is a wildcard because Amazon posts to several URLs under it and
# the proxy has to see all of them.
PROXY_PATH = "/ampere/auth/proxy/"
POST_PATH = "/ampere/auth/proxy/ap/signin/*"


async def sign_in(
    mass: MusicAssistant,
    auth: push_auth.PushAuth,
    *,
    session_id: str,
    url: str,
    email: str,
    password: str,
    otp_secret: str,
    cookie_path: str,
    logger: Any,
) -> push_auth.AuthState:
    """Run the interactive Amazon sign-in and keep what it produces.

    Returns the resulting state rather than raising, because this is rendered
    straight into a settings page and an operator needs to be told what to do
    next, not handed a traceback.
    """
    try:
        from aiohttp import web
        from alexapy import AlexaLogin, AlexaProxy
        from music_assistant.helpers.auth import AuthenticationHelper
    except ImportError as err:  # pragma: no cover - hard dependencies
        return push_auth.AuthState(
            detail=f"Live updates are unavailable: {err}", needs_login=True)

    login = AlexaLogin(
        url=url or "amazon.com",
        email=email,
        password=password,
        otp_secret=otp_secret,
        outputpath=lambda path: path,
        # The default, restated because it is load-bearing. It is what points
        # the login at the PKCE pages that mint the token; without it this
        # whole flow would end with a cookie and nothing else.
        oauth_login=True,
    )

    base = mass.webserver.base_url.rstrip("/")
    proxy_url = f"{base}{PROXY_PATH}"
    proxy = AlexaProxy(login, proxy_url)
    finished = False

    async def handler(request: web.Request) -> Any:
        nonlocal finished
        response = await proxy.all_handler(request)
        if "Successfully logged in" in getattr(response, "text", ""):
            finished = True
            return web.Response(
                text=(
                    "<html><body><h2>Signed in.</h2>"
                    "<p>You can close this window. Live updates will connect "
                    "on their own.</p></body></html>"
                ),
                content_type="text/html",
            )
        return response

    mass.webserver.register_dynamic_route(PROXY_PATH, handler, "GET")
    mass.webserver.register_dynamic_route(POST_PATH, handler, "POST")
    try:
        async with AuthenticationHelper(mass, session_id) as helper:
            await helper.authenticate(proxy_url)

        if not await login.test_loggedin():
            await push_auth.close_quietly(login)
            return push_auth.AuthState(
                detail="Amazon did not complete the sign-in. Nothing changed.",
                needs_login=True,
            )

        # Save the session too. It is the same file the REST side already
        # reads, so a sign-in done for push also refreshes the credentials
        # everything else runs on, and an expiring cookie stops being a second
        # thing the operator has to think about.
        await alexapy_compat.save_cookies(login, cookie_path)

        state_ok = await auth.adopt(login)
        if not state_ok:
            # The login itself worked; only the push registration did not. The
            # cookie above is still worth keeping, so the login is not closed.
            logger.info("signed in, but live updates could not be registered")
        return auth.state
    except KeyError:
        # No callback parameter: the operator closed the popup.
        await push_auth.close_quietly(login)
        return push_auth.AuthState(
            detail="Sign-in was cancelled.", needs_login=True)
    except Exception as err:  # noqa: BLE001 - rendered, not raised
        await push_auth.close_quietly(login)
        logger.debug("sign-in failed: %s", type(err).__name__)
        return push_auth.AuthState(
            detail=f"Sign-in failed: {err}", needs_login=True)
    finally:
        if not finished:
            logger.debug("sign-in ended without a success page")
        for path, method in ((PROXY_PATH, "GET"), (POST_PATH, "POST")):
            try:
                mass.webserver.unregister_dynamic_route(path, method)
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
