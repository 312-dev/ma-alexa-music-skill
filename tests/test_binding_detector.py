"""The reactive binding detector and its circuit breaker.

The failure being detected is invisible from Amazon's side: enablement status
reports True, the search resolves against the catalog and is answered 200, and
Alexa.Media.Playback.Initiate simply never arrives. The only honest evidence
either way is our own traffic, so that is what these read.
"""

from __future__ import annotations

import logging
import os
import time

import pytest

from setup_ui import state as store, views
import smapi_rest

SEARCH = views.SEARCH_DIRECTIVE
INITIATE = views.INITIATE_DIRECTIVE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("SKILL_ID", "")
    (tmp_path / "captures").mkdir()
    return tmp_path


def write_capture(root, offset_seconds: float, directive: str) -> None:
    """A capture at a chosen age. Only the name and mtime are ever read."""
    when = time.time() - offset_seconds
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(when))
    path = root / "captures" / f"{stamp}{int(offset_seconds):06d}-{directive}.json"
    path.write_text("{}")
    os.utime(path, (when, when))


class FakeLog:
    def __init__(self):
        self.lines = []

    def _record(self, message, *args):
        self.lines.append(message % args if args else message)

    info = warning = _record

    def exception(self, message, *args):
        self._record(message, *args)


# --- reading the traffic ----------------------------------------------------


def test_a_search_answered_by_initiate_counts_as_reaching_playback(isolated):
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 298, INITIATE)
    health = views._binding_health()
    assert health["searches"] == 1
    assert health["reached"] == 1
    assert health["miss"] is False


def test_a_search_with_no_initiate_is_a_miss(isolated):
    write_capture(isolated, 300, SEARCH)
    health = views._binding_health()
    assert health["searches"] == 1
    assert health["reached"] == 0
    assert health["miss"] is True
    assert health["miss_at"] > 0


def test_a_search_still_in_flight_is_not_yet_a_miss(isolated):
    """Two seconds old with nothing after it is a pending request, not a fault."""
    write_capture(isolated, 2, SEARCH)
    health = views._binding_health()
    assert health["searches"] == 0
    assert health["miss"] is False


def test_the_real_outage_shape_is_read_correctly(isolated):
    """The live capture directory on the day this was found.

    Three searches missed in a row, then a cycle, then a search that paired
    with an Initiate two seconds later.
    """
    write_capture(isolated, 6000, SEARCH)
    write_capture(isolated, 5000, SEARCH)
    write_capture(isolated, 4000, SEARCH)
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 298, INITIATE)
    health = views._binding_health()
    assert health["searches"] == 4
    assert health["reached"] == 1
    assert health["miss"] is False, "the newest search did reach playback"


def test_health_ignores_directives_that_prove_nothing(isolated):
    """GetNextItem is the bulk of the traffic and says nothing about binding."""
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 200, "Alexa.Audio.PlayQueue.GetNextItem")
    health = views._binding_health()
    assert health["miss"] is True


# --- the breaker ------------------------------------------------------------


@pytest.fixture
def armed_skill(monkeypatch):
    store.update(skill_id="amzn1.ask.skill.test", enabled=True,
                 reactive_armed=True, reactive_misses=0, reactive_cycled_at=0.0)
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    cycles = []
    monkeypatch.setattr(
        views, "_cycle_enablement",
        lambda skill_id: (cycles.append(skill_id),
                          {"ok": True, "detail": "Re-provisioned."})[1])
    return cycles


def test_a_miss_triggers_exactly_one_cycle(isolated, armed_skill):
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert armed_skill == ["amzn1.ask.skill.test"]
    after = store.load()
    assert after["reactive_armed"] is False
    assert after["reactive_cycled_at"] > 0


def test_further_misses_do_not_cycle_again(isolated, armed_skill):
    """A cause the cycle cannot fix must not be retried on every request.

    Without this the reactor would churn the enablement against Amazon's rate
    limits for as long as the real fault lasted.
    """
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert len(armed_skill) == 1

    write_capture(isolated, 100, SEARCH)
    views._reactive_check(store.load(), time.time(), log)
    views._reactive_check(store.load(), time.time(), log)

    assert len(armed_skill) == 1, "one attempt per outage"
    assert store.load()["reactive_misses"] == 2


def test_it_re_arms_only_on_observed_playback(isolated, armed_skill):
    """Amazon answering 200 to the cycle proves nothing.

    That is the exact trap this whole failure mode lives in: enablement status
    reports enabled throughout. Only an Initiate landing after the attempt is
    evidence the provider slot is really bound.
    """
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert store.load()["reactive_armed"] is False

    # The cycle reported success, but nothing has played yet.
    views._reactive_check(store.load(), time.time(), log)
    assert store.load()["reactive_armed"] is False

    # A device starts playback: that, and only that, re-arms the detector.
    write_capture(isolated, 0, INITIATE)
    views._reactive_check(store.load(), time.time(), log)
    reloaded = store.load()
    assert reloaded["reactive_armed"] is True
    assert reloaded["reactive_misses"] == 0


def test_an_initiate_from_before_the_cycle_does_not_re_arm(isolated, armed_skill):
    """Stale evidence is not evidence. The Initiate has to postdate the attempt."""
    write_capture(isolated, 4000, INITIATE)
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    views._reactive_check(store.load(), time.time(), log)
    assert store.load()["reactive_armed"] is False


def test_healthy_traffic_never_cycles(isolated, armed_skill):
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 298, INITIATE)
    views._reactive_check(store.load(), time.time(), FakeLog())
    assert armed_skill == []


def test_it_does_nothing_without_a_skill(isolated, monkeypatch):
    store.update(skill_id="", enabled=False)
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    cycles = []
    monkeypatch.setattr(views, "_cycle_enablement",
                        lambda sid: cycles.append(sid))
    write_capture(isolated, 300, SEARCH)
    views._reactive_check(store.load(), time.time(), FakeLog())
    assert cycles == []


def test_it_does_nothing_while_disconnected_from_amazon(isolated, monkeypatch):
    store.update(skill_id="amzn1.ask.skill.test", enabled=True)
    monkeypatch.setattr(smapi_rest, "connected", lambda: False)
    cycles = []
    monkeypatch.setattr(views, "_cycle_enablement",
                        lambda sid: cycles.append(sid))
    write_capture(isolated, 300, SEARCH)
    views._reactive_check(store.load(), time.time(), FakeLog())
    assert cycles == []


# --- what the operator is shown ---------------------------------------------


def test_scheduler_context_reports_a_degraded_binding(isolated):
    store.update(skill_id="s", enabled=True, enabled_at=time.time(),
                 reactive_armed=False, reactive_misses=3,
                 reactive_cycled_at=time.time() - 600)
    panel = views.scheduler_context(store.load())
    assert panel["armed"] is False
    assert panel["misses"] == 3
    assert "minutes ago" in panel["cycled_ago"]
    assert panel["keepalive_due"].startswith("in ")


def test_scheduler_context_when_nothing_is_scheduled(isolated):
    store.update(skill_id="", enabled=False, auto_sync_hours=0)
    panel = views.scheduler_context(store.load())
    assert panel["sync_hours"] == 0
    assert panel["sync_due"] == ""
    assert panel["keepalive_due"] == ""
    assert panel["sync_last"] == "never"


def test_soon_reads_as_the_mirror_of_ago():
    assert views.soon(-5) == "due now"
    assert views.soon(30) == "in 30 seconds"
    assert views.soon(600) == "in 10 minutes"
    assert views.soon(7200) == "in 2 hours"
