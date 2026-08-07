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

import contextlib
from typing import TYPE_CHECKING, Any

from . import alexapy_compat, push_auth

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# Routes on MA's webserver, live only for the duration of one sign-in.
#
# PROXY_PATH is what the proxy rewrites Amazon's URLs to point at, so it is the
# base and keeps its trailing slash. PROXY_WILDCARD is what is actually
# registered: one entry, any method, matching every page underneath.
PROXY_PATH = "/ma_alexa/auth/proxy/"
PROXY_WILDCARD = "/ma_alexa/auth/proxy/*"


# Amazon serves one page holding two forms, and which panel is expanded is
# decided by its own JavaScript from the path. alexapy starts the login at
# `/ap/register`, so an existing customer is shown "Create account" first.
#
# That is not merely untidy. alexapy's autofill matches inputs by `name`, and
# both forms have inputs named `email` and `password`, so the account password
# is filled into the *create account* form as well. The primary button on that
# panel then reads CREATE YOUR AMAZON ACCOUNT, and pressing it is a plausible
# thing for someone to do when it is the only button on screen.
REGISTER_FORM = "ap_register_form"
SIGNIN_PATH = "/ap/signin"


async def _prefer_signin_page(proxy: Any, login: Any, logger: Any) -> None:
    """Start the login on the sign-in panel, and defuse the other one.

    Two independent measures, because the first cannot be verified from here.
    Which panel Amazon expands is chosen client side, so a server-side fetch of
    either path returns the same markup and proves nothing; only a browser can
    settle it. The second measure therefore has to stand on its own.

    1. Point the proxy at `/ap/signin` rather than `/ap/register`, carrying the
       PKCE query across unchanged. The query is what makes this an
       authorization request at all: yarl's `with_path` drops it, and losing it
       turns the whole flow into an ordinary sign-in that mints no token.

    2. Empty the create-account form on every page that has one, so the
       password is not sitting in it whichever panel opens. Nothing is removed:
       deleting the form outright would be tidier and would also break the
       script that toggles between the two.
    """
    from yarl import URL

    try:
        start = URL(str(login.start_url))
        await proxy.change_host_url(
            start.with_path(SIGNIN_PATH).with_query(start.query)
        )
    except Exception as err:  # noqa: BLE001 - a nicer landing page is optional
        logger.debug("could not redirect the login page: %s", type(err).__name__)

    # change_host_url resets stored data, so alexapy's autofill is put back
    # rather than assumed to have survived, and ours is added after it: dicts
    # preserve insertion order, so this runs on the filled html and not before.
    existing = dict(getattr(proxy, "modifiers", {}) or {})
    existing.setdefault("autofill", _noop)
    existing["ma_alexa_clear_register_form"] = _clear_register_form
    proxy.modifiers = existing


def _noop(html: str) -> str:
    return html


def _clear_register_form(html: str) -> str:
    """Take the prefilled credentials back out of the create-account form."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 arrives with alexapy
        return html

    if REGISTER_FORM not in html:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id=REGISTER_FORM)
        if form is None:
            return html
        for tag in form.find_all("input"):
            if tag.get("name") in ("email", "password", "customerName"):
                tag["value"] = ""
        return str(soup)
    except Exception:  # noqa: BLE001 - never break the login over cosmetics
        return html


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

    def _build(secret: str) -> Any:
        return AlexaLogin(
            url=url or "amazon.com",
            email=email,
            password=password,
            otp_secret=secret,
            outputpath=lambda path: path,
            # The default, restated because it is load-bearing. It is what
            # points the login at the PKCE pages that mint the token; without
            # it this whole flow would end with a cookie and nothing else.
            oauth_login=True,
        )

    # A bad two-factor seed raises out of the constructor, before any of the
    # error handling below is in scope, and pyotp's message ("Non-base32 digit
    # found") names the symptom and nothing an operator can act on.
    #
    # An unusable seed is also not a reason to refuse to sign in. The proxy
    # puts Amazon's real pages in front of a person, so a six digit code can
    # simply be typed; the seed only exists to save them that. So the failure
    # is downgraded to signing in without it.
    try:
        login = _build(otp_secret)
    except Exception as err:  # noqa: BLE001 - reported, not raised
        logger.info(
            "the saved two-factor secret is not a valid TOTP seed (%s); "
            "signing in without it, so Amazon will ask for a code",
            type(err).__name__)
        try:
            login = _build("")
        except Exception as build_err:  # noqa: BLE001
            return push_auth.AuthState(
                detail=f"Could not start the sign-in: {build_err}",
                needs_login=True,
            )

    base = mass.webserver.base_url.rstrip("/")
    proxy_url = f"{base}{PROXY_PATH}"
    proxy = AlexaProxy(login, proxy_url)
    await _prefer_signin_page(proxy, login, logger)
    finished = False

    try:
        helper_context = AuthenticationHelper(mass, session_id)
    except Exception as err:  # noqa: BLE001
        await push_auth.close_quietly(login)
        return push_auth.AuthState(
            detail=f"Could not start the sign-in: {err}", needs_login=True)

    try:
        async with helper_context as helper:

            async def handler(request: web.Request) -> Any:
                nonlocal finished
                response = await proxy.all_handler(request)
                if "Successfully logged in" in getattr(response, "text", ""):
                    finished = True
                    # Send the *browser* to the callback rather than fetching
                    # it from here.
                    #
                    # Both routes release `helper.authenticate()`, so the
                    # difference is only in what the operator is left looking
                    # at, and it is the whole difference. Music Assistant's
                    # callback answers with `<body onload="window.close()">`,
                    # so a browser that arrives there closes its own popup and
                    # the settings page simply updates. Fetching it server side
                    # consumes that page here, and the popup is left showing
                    # whatever we invent instead, which is a dead end the
                    # operator has to notice and dismiss.
                    #
                    # Music Assistant's own alexa provider fetches it server
                    # side and hands back a page of its own, which is where
                    # this started. Following it that far was a mistake: the
                    # auto-closing popup is the native pattern and it is
                    # already built.
                    #
                    # By this point `proxy.all_handler` has already captured
                    # the token onto `login`, so nothing is lost by redirecting
                    # away.
                    return web.HTTPFound(helper.callback_url)
                return response

            # One wildcard route, any method. Amazon's login is not two URLs:
            # a two-factor challenge lands on /ap/cvf/verify, a captcha
            # somewhere else again, and each unregistered path is a 404 in the
            # middle of a sign-in that was otherwise working. Music Assistant's
            # own alexa provider registers only the base path and
            # /ap/signin/*, and has the same gap.
            #
            # The catch-all matches a route whose path ends in "/*" by prefix,
            # and a method of "*" against anything, so this one entry covers
            # every page the flow can visit.
            mass.webserver.register_dynamic_route(PROXY_WILDCARD, handler, "*")
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
        with contextlib.suppress(Exception):
            mass.webserver.unregister_dynamic_route(PROXY_WILDCARD, "*")
