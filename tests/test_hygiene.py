"""Capture redaction, capture retention, and queue-state expiry.

Three leaks that all looked like housekeeping and were not. Captures held the
linked account's OAuth token in the clear and were rendered into the admin UI;
nothing bounded the capture directory that two hot paths scan; and every queue
ever started left a state file behind permanently.
"""

from __future__ import annotations

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
