"""Capture redaction, capture retention, and queue-state expiry.

Three leaks that all looked like housekeeping and were not. Captures held the
linked account's OAuth token in the clear and were rendered into the admin UI;
nothing bounded the capture directory that two hot paths scan; and every queue
ever started left a state file behind permanently.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time

import pytest

from ma_provider import core as app_module
from ma_provider import core as core_module
from ma_provider import queuestate
from conftest import directive


@pytest.fixture(autouse=True)
def clean_captures(tmp_path, monkeypatch):
    """A capture directory per test, so counting files means something."""
    target = tmp_path / "captures"
    target.mkdir()
    monkeypatch.setattr(app_module, "LOG_DIR", target)
    monkeypatch.setattr(app_module, "_CAPTURES_WRITTEN", 0)
    return target


def _written(directory):
    return sorted(p.name for p in directory.glob("*.json"))


# --- redaction --------------------------------------------------------------


def test_capture_strips_the_access_token(clean_captures):
    """The token must not reach disk. It is a live bearer credential."""
    app_module.capture(
        {"headers": {"Authorization": "Bearer abc123"},
         "body": {"payload": {"accessToken": "Atza|super-secret"}}},
        "Alexa.Media.Search.GetPlayableContent",
    )
    raw = (clean_captures / _written(clean_captures)[0]).read_text()
    assert "super-secret" not in raw
    assert "abc123" not in raw
    assert app_module.REDACTED in raw


def test_capture_keeps_the_signature_keys_it_redacts():
    """The status panel reads "was this signed" off the presence of the key.

    A redaction that dropped the keys would silently turn every capture into
    "probably not Amazon".
    """
    cleaned = app_module.redact(
        {"headers": {"Signature-256": "abc", "Signaturecertchainurl": "https://s3/x"}})
    assert "Signature-256" in cleaned["headers"]
    assert cleaned["headers"]["Signature-256"] == app_module.REDACTED
    # A public URL, and the one thing worth having when verification is broken.
    assert cleaned["headers"]["Signaturecertchainurl"] == "https://s3/x"


def test_redaction_reaches_any_depth():
    nested = {"a": [{"b": {"apiAccessToken": "secret", "keep": "visible"}}]}
    cleaned = app_module.redact(nested)
    assert cleaned["a"][0]["b"]["apiAccessToken"] == app_module.REDACTED
    assert cleaned["a"][0]["b"]["keep"] == "visible"


def test_scrub_rewrites_captures_written_before_redaction(clean_captures):
    """Tokens already on disk do not get to wait for the prune cap."""
    stale = clean_captures / "20260101T000000000000-Alexa.Media.Playback.Initiate.json"
    stale.write_text(json.dumps({"body": {"payload": {"accessToken": "old-secret"}}}))
    assert app_module.scrub_captures() == 1
    assert "old-secret" not in stale.read_text()
    # Idempotent: a second pass finds nothing left to do.
    assert app_module.scrub_captures() == 0


def test_a_live_directive_does_not_persist_its_token(client):
    """End to end, through the real route rather than the helper."""
    body = directive("Alexa.Media.Search", "GetPlayableContent",
                     {"accessToken": "Atza|live-token", "filters": {}}, "1.0")
    client.post("/music", json=body)
    on_disk = "\n".join(p.read_text() for p in app_module.LOG_DIR.glob("*.json"))
    assert on_disk, "the directive should have been captured at all"
    assert "live-token" not in on_disk


# --- retention --------------------------------------------------------------


def test_prune_keeps_the_newest_and_drops_the_rest(clean_captures):
    for n in range(10):
        (clean_captures / f"2026080{n}T000000000000-Test.Ping.json").write_text("{}")
    assert app_module.prune_captures(keep=4) == 6
    names = _written(clean_captures)
    assert len(names) == 4
    # Filenames lead with a UTC stamp, so the survivors are the newest.
    assert names[0].startswith("20260806")


def test_prune_is_a_no_op_under_the_cap(clean_captures):
    (clean_captures / "20260801T000000000000-Test.Ping.json").write_text("{}")
    assert app_module.prune_captures(keep=400) == 0
    assert len(_written(clean_captures)) == 1


def test_captures_prune_themselves_as_they_are_written(clean_captures, monkeypatch):
    monkeypatch.setattr(app_module, "CAPTURE_KEEP", 5)
    monkeypatch.setattr(app_module, "_PRUNE_EVERY", 4)
    for _ in range(40):
        app_module.capture({"body": {}}, "Test.Ping")
        time.sleep(0.001)  # the stamp has microsecond resolution; keep names unique
    assert len(_written(clean_captures)) <= 5


# --- a capture failure is never fatal ---------------------------------------


def test_a_failing_capture_does_not_break_the_directive(client, tmp_path,
                                                        monkeypatch):
    """The realistic failure is a full disk, and it used to return a 500.

    capture() runs after the response has already been built, so an exception
    there threw away a correct answer: losing telemetry took playback down
    with it. Simulated by pointing the capture directory at a regular file,
    which makes every write under it fail the way a full volume would.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setattr(app_module, "LOG_DIR", blocked)

    body = directive("Alexa.Media.Search", "GetPlayableContent",
                     {"filters": {}}, "1.0")
    response = client.post("/music", json=body)
    assert response.status_code == 200


# --- queue state ------------------------------------------------------------


def test_queue_state_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(queuestate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(queuestate, "STATE_TTL", 60.0)
    monkeypatch.setattr(queuestate, "_last_prune", 0.0)

    old = tmp_path / "old-queue.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    fresh = tmp_path / "fresh-queue.json"
    fresh.write_text("{}")

    assert queuestate.prune() == 1
    assert not old.exists()
    assert fresh.exists(), "a live queue's state must survive"


def test_queue_state_prune_is_throttled(tmp_path, monkeypatch):
    """It runs on the write path, which fires on every shuffle directive."""
    monkeypatch.setattr(queuestate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(queuestate, "STATE_TTL", 60.0)
    monkeypatch.setattr(queuestate, "_last_prune", 0.0)

    assert queuestate.prune() == 0  # first call takes the slot
    stale = tmp_path / "stale.json"
    stale.write_text("{}")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    assert queuestate.prune() == 0, "the interval should suppress this one"
    assert stale.exists()


def test_writing_queue_state_still_works_when_prune_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(queuestate, "STATE_DIR", tmp_path)

    def explode(*_a, **_kw):
        raise OSError("cannot scan")

    monkeypatch.setattr(queuestate, "prune", explode)
    assert queuestate.update("q1", shuffle=True)["shuffle"] is True


def test_the_image_copies_the_package_whole_rather_than_module_by_module():
    """A named-module COPY list silently omits whatever was added last.

    Caught the hard way: `handoff.py` was added, the build succeeded, the image
    passed a push, and it only failed when it ran. The old form of this test
    compared the list against the directory and had to be kept in step by hand.

    Copying the package as a directory removes the failure mode instead of
    detecting it, so what is worth pinning now is that nobody goes back to
    enumerating files. Every module lives in `ma_provider`, so one COPY of that
    directory is both necessary and sufficient.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text()

    assert re.search(r"^COPY\s+ma_provider/\s+ma_provider/\s*$", dockerfile, re.M), (
        "the image must COPY the ma_provider package as a directory"
    )
    named = {
        m for m in re.findall(r"ma_provider/[\w.-]+\.py", dockerfile)
    }
    assert not named, (
        "modules are named individually again, which is how one gets missed: "
        + ", ".join(sorted(named))
    )

    # Nothing but the Flask adapter should be left loose at the root, since a
    # loose module is one the provider inside Music Assistant cannot import.
    loose = {
        path.name
        for path in root.glob("*.py")
        if path.name not in {"conftest.py", "setup.py", "app.py"}
    }
    assert not loose, (
        "these belong in ma_provider, or the provider cannot import them: "
        + ", ".join(sorted(loose))
    )


def test_the_core_does_not_depend_on_a_web_framework():
    """The property the whole split exists to create.

    `core` and everything it imports must load with Flask unavailable, because
    the same code has to run under aiohttp inside Music Assistant. Nothing
    stops someone adding `from flask import jsonify` to a module the core
    reaches; the import would work locally, the tests would pass, and the
    failure would surface only when Music Assistant tried to load the provider.

    Checked by import under a blocked meta-path rather than by grepping for the
    word, so a transitive dependency several modules deep is caught too. That
    is not hypothetical: `queue_api` was exactly such a dependency, and reading
    the import lines of `core` alone would never have found it.

    Run in a subprocess because pytest has already imported Flask into this
    one, and an import that is already satisfied never consults the finder.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    probe = textwrap.dedent(
        """
        import sys, importlib.abc

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "flask":
                    raise ImportError("flask reached the core via " + name)
                return None

        sys.meta_path.insert(0, Blocker())
        from ma_provider import core  # noqa: F401
        assert not [m for m in sys.modules if m.split(".")[0] == "flask"]
        """
    )
    env = {
        **os.environ,
        "PUBLIC_BASE": "https://example.test",
        "PREWARM": "0",
        "SUBSONIC_USER": "tester",
        "SUBSONIC_PASSWORD": "not-a-real-password",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0, (
        "core no longer imports without Flask:\n" + done.stderr
    )


def test_importing_the_core_never_kills_the_host_process():
    """It did, on 2026-08-03, and it took Music Assistant down with it.

    `core` used to `raise SystemExit` at import when PUBLIC_BASE was unset.
    That was defensible while Ampere was a process of its own: refusing to
    start beats serving stream URLs Amazon cannot fetch. Inside Music Assistant
    the same import happens during MA's startup, so an unset Ampere setting
    left MA running with its stream server never started. A music system
    stopped playing because of an Alexa variable.

    The requirement did not go away, it moved: `require_public_base` is called
    by whatever is about to serve. What this pins is that importing alone,
    with nothing configured, is inert.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    probe = textwrap.dedent(
        """
        from ma_provider import core
        assert core.PUBLIC_BASE == "", "expected no origin to be configured"
        try:
            core.require_public_base()
        except RuntimeError:
            pass
        else:
            raise AssertionError("serving without a public base must be refused")
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE"}
    env.update(PREWARM="0", SUBSONIC_USER="tester",
               SUBSONIC_PASSWORD="not-a-real-password")
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0, (
        "importing core without PUBLIC_BASE must not fail:\n" + done.stderr
    )


def test_importing_the_package_has_no_side_effects():
    """The hazard class behind the 2026-08-03 outage, pinned as a rule.

    Ampere was written as an application, and an application may do things at
    import that a library may not: exit on bad config, take the root logger,
    make directories, start network calls. Every one of those became a hazard
    the moment the same code was loaded into Music Assistant's process.

    Three had already shipped. `raise SystemExit` aborted MA's startup and left
    it without a stream server. `logging.basicConfig` plus `logring.attach()`
    would have reformatted MA's logs and teed them into Ampere's ring buffer,
    and did no harm only because MA configures logging first, which is luck.
    `mkdir` put Ampere's directories in MA's storage root beside library.db.

    Rather than wait to discover the fourth, this walks the package for
    top-level statements that call out to the world. Assignments and function
    definitions are fine; doing something is not.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {
        # A logging handler on a logger this package owns by name, which is
        # local to it and does not touch the root logger.
        ("logring.py", "setFormatter"),
    }
    offenders = []
    for path in sorted((root / "ma_provider").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            called = node.value.func
            name = getattr(called, "attr", getattr(called, "id", "?"))
            if (path.name, name) in allowed:
                continue
            offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)[:70]}")

    assert not offenders, (
        "importing this package must not do anything, because the import "
        "happens inside Music Assistant's process:\n  " + "\n  ".join(offenders)
    )


def test_one_signing_key_reaches_everything_that_signs():
    """Three modules sign things and they must agree.

    Each read SIGNING_KEY independently and fell back to `os.urandom(32)`
    separately, so with nothing set they did not even agree with each other.
    That never showed in a container with the variable set. Inside Music
    Assistant there is no environment, so the fallback would have fired on
    every restart: a 403 partway through a queue Alexa was already playing, a
    linked account coming apart, and a republished queue getting a new id that
    orphans the one Alexa holds.
    """
    from ma_provider import oauth, queue_api

    original = app_module.SIGNING_KEY
    try:
        app_module.set_signing_key(b"a-key-every-module-must-share")
        assert app_module.SIGNING_KEY == b"a-key-every-module-must-share"
        assert oauth.SIGNING_KEY == app_module.SIGNING_KEY
        assert queue_api.SIGNING_KEY == app_module.SIGNING_KEY
    finally:
        app_module.set_signing_key(original)


def test_a_signature_survives_the_key_being_reapplied():
    """What the persisted key actually buys: URLs outlive a restart.

    Signing, re-applying the same key as a fresh process would, and verifying
    is the closest a unit test gets to "Alexa came back for track nine an hour
    later".
    """
    original = app_module.SIGNING_KEY
    try:
        app_module.set_signing_key(b"stable-across-restarts")
        _url, expires = app_module.signed_url("stream", "t1")
        signature = app_module.sign("stream", "t1", expires)

        app_module.set_signing_key(b"stable-across-restarts")
        assert app_module.verify("stream", "t1", expires, signature) is True

        app_module.set_signing_key(b"a-different-key-entirely")
        assert app_module.verify("stream", "t1", expires, signature) is False
    finally:
        app_module.set_signing_key(original)


def test_every_injected_secret_reaches_the_module_that_uses_it():
    """Configuring must actually move the value, not just accept it.

    Each of these lives in a different module and was read from a different
    environment variable. Music Assistant supplies none of them, so if any one
    fails to arrive the failure is silent and remote: the admin plane answers
    nobody, or account linking rejects the right passphrase, and the log says
    only that a request was refused.
    """
    from ma_provider import oauth

    before = (app_module.ADMIN_TOKEN, oauth.CLIENT_ID, oauth.CLIENT_SECRET,
              oauth.LINK_SECRET, app_module.SIGNING_KEY)
    try:
        app_module.configure(
            admin_token="token-from-config",
            oauth_client_id="ampere-abcd1234",
            oauth_client_secret="secret-from-config",
            oauth_link_secret="passphrase-from-config",
        )
        assert app_module.ADMIN_TOKEN == "token-from-config"
        assert oauth.CLIENT_ID == "ampere-abcd1234"
        assert oauth.CLIENT_SECRET == "secret-from-config"
        assert oauth.LINK_SECRET == "passphrase-from-config"
    finally:
        (app_module.ADMIN_TOKEN, oauth.CLIENT_ID, oauth.CLIENT_SECRET,
         oauth.LINK_SECRET) = before[:4]
        app_module.set_signing_key(before[4])


def test_configuring_again_does_not_blank_what_was_already_set():
    """A partial second call is a normal thing for a reload to make.

    `configure` takes ten keyword arguments and callers pass the ones they
    have. If an omitted argument overwrote the stored value with an empty
    string, the second call would silently close the admin plane and break
    account linking, and the only symptom would be 403s.
    """
    from ma_provider import oauth

    before = (app_module.ADMIN_TOKEN, oauth.LINK_SECRET)
    try:
        app_module.configure(admin_token="set-the-first-time",
                             oauth_link_secret="also-the-first-time")
        app_module.configure(public_base="https://elsewhere.test")

        assert app_module.ADMIN_TOKEN == "set-the-first-time"
        assert oauth.LINK_SECRET == "also-the-first-time"
    finally:
        app_module.ADMIN_TOKEN, oauth.LINK_SECRET = before
        app_module.configure(public_base="https://example.test")


def test_the_admin_plane_is_closed_when_no_token_was_supplied():
    """Empty means closed, not open.

    /captures replays inbound Amazon requests and /diag names the music
    server. A token read from an environment Music Assistant does not provide
    is an empty token, and an empty token that compared equal to an absent
    header would open both to anyone on the network.
    """
    before = app_module.ADMIN_TOKEN
    try:
        app_module.ADMIN_TOKEN = ""
        assert app_module.admin_authorized("127.0.0.1", {}) is False
        assert app_module.admin_authorized("127.0.0.1",
                                           {"X-Admin-Token": ""}) is False
    finally:
        app_module.ADMIN_TOKEN = before


def test_one_directory_answer_reaches_everything_that_reads_it(tmp_path):
    """Configuring the storage path must move every reader, not most of them.

    Four modules keep state under it and each used to derive its own location
    from its own environment variable. That agreed for as long as they all
    read the same variable and stopped agreeing the moment Music Assistant
    supplied a path instead. The failure is quiet in the worst way: the wizard
    writes a setting to one file and the thing that acts on it reads another,
    so the setting simply appears not to work.
    """
    from ma_provider import mastream_cache, queue_api, queuestate
    from ma_provider import setup_captures, setup_state, smapi_rest

    before = (core_module.LOG_DIR, queuestate.STATE_DIR, queue_api.STATE_DIR,
              mastream_cache.CACHE_DIR, setup_state.STATE_DIR)
    try:
        app_module.configure(storage_path=str(tmp_path))

        for path in (core_module.LOG_DIR, queuestate.STATE_DIR,
                     queue_api.STATE_DIR, mastream_cache.CACHE_DIR,
                     setup_state.STATE_DIR, smapi_rest.state_dir()):
            assert tmp_path in path.parents or path == tmp_path, path

        # Read through the accessors the running code actually calls, not the
        # globals: an accessor that rebuilds the path itself is exactly the
        # bug this test exists to catch.
        assert setup_captures.log_dir() == core_module.LOG_DIR
        assert setup_state.path().parent == tmp_path
    finally:
        (core_module.LOG_DIR, queuestate.STATE_DIR, queue_api.STATE_DIR,
         mastream_cache.CACHE_DIR, setup_state.STATE_DIR) = before
