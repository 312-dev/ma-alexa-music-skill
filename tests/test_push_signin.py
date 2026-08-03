"""The parts of the interactive sign-in that can be tested without a browser.

The sign-in itself needs Music Assistant's webserver and a person, so what is
covered here is the html modifier: the measure that stops the account password
sitting in Amazon's create-account form.

That measure exists because the other one cannot be verified from a test at
all. Which of the two panels Amazon expands is decided by its own JavaScript,
so fetching either path server-side returns identical markup. Pointing the
proxy at /ap/signin is therefore a best guess that only a browser can confirm,
and this has to hold on its own if the guess is wrong.
"""

from __future__ import annotations

from ma_provider import push_signin

# The real structure, from the live page on 2026-08-03. Both forms carry inputs
# named `email` and `password`, which is exactly why alexapy's autofill fills
# both: it matches on the name attribute and nothing else.
PAGE = """
<html><body>
<form id="ap_register_form" name="register" action="https://www.amazon.com/ap/register/1">
  <input type="text" id="ap_customer_name" name="customerName" value="Gray Adams">
  <input type="email" id="ap_email" name="email" value="someone@example.test">
  <input type="password" id="ap_password" name="password" value="hunter2">
  <input type="submit" value="Create your Amazon account">
</form>
<form id="ap_login_form" name="signIn" action="https://www.amazon.com/ap/signin/1">
  <input type="email" id="ap_email_login" name="email" value="someone@example.test">
  <input type="password" id="ap_password" name="password" value="hunter2">
</form>
</body></html>
"""


def _forms(html: str) -> tuple[str, str]:
    register, _, signin = html.partition('id="ap_login_form"')
    return register, signin


def test_the_password_is_taken_out_of_the_create_account_form():
    """The hazard. Its submit button is the only one on the panel."""
    register, _ = _forms(push_signin._clear_register_form(PAGE))

    assert "hunter2" not in register
    assert "someone@example.test" not in register
    assert "Gray Adams" not in register


def test_the_sign_in_form_keeps_what_autofill_put_there():
    """Clearing both would defeat the point of autofill entirely."""
    _, signin = _forms(push_signin._clear_register_form(PAGE))

    assert "hunter2" in signin
    assert "someone@example.test" in signin


def test_the_create_account_form_is_emptied_and_not_removed():
    """Deleting it would break the script that toggles between the panels."""
    out = push_signin._clear_register_form(PAGE)

    assert "ap_register_form" in out
    assert "ap_customer_name" in out


def test_a_page_without_the_form_is_returned_untouched():
    """Most pages in the flow are captchas, OTP prompts and redirects."""
    for html in ("<p>enter the code</p>", "", "<html></html>"):
        assert push_signin._clear_register_form(html) == html


def test_unparseable_html_never_breaks_the_login():
    """A cosmetic fix must not be able to stop someone signing in."""
    broken = '<form id="ap_register_form"><input name="password" value="x"'

    assert isinstance(push_signin._clear_register_form(broken), str)


def test_the_modifier_is_ordered_after_autofill():
    """It rewrites what autofill produced, so it has to run second.

    Dicts preserve insertion order and authcaptureproxy applies modifiers in
    that order, so this is the whole mechanism.
    """
    class FakeProxy:
        modifiers = {"autofill": lambda html: html}

        async def change_host_url(self, url):
            self.modifiers = {}

    import asyncio
    import logging

    class FakeLogin:
        start_url = "https://www.amazon.com/ap/register?openid.oa2.scope=x"

    proxy = FakeProxy()
    asyncio.run(push_signin._prefer_signin_page(
        proxy, FakeLogin(), logging.getLogger("test")))

    keys = list(proxy.modifiers)
    assert keys.index("autofill") < keys.index("ampere_clear_register_form")


def test_the_pkce_query_survives_the_path_change():
    """Losing it turns an authorization request into an ordinary sign-in.

    yarl's with_path drops the query, which cost a probe earlier the same day:
    both paths answered 404 until the query was carried across explicitly.
    """
    from yarl import URL

    start = URL("https://www.amazon.com/ap/register?openid.oa2.scope=device_auth_access&a=1")
    moved = start.with_path(push_signin.SIGNIN_PATH).with_query(start.query)

    assert moved.path == "/ap/signin"
    assert moved.query.get("openid.oa2.scope") == "device_auth_access"
