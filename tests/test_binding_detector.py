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
from datetime import datetime, timezone

import pytest

from ma_provider import setup_state as store
from setup_ui import views
from ma_provider import smapi_rest

SEARCH = views.SEARCH_DIRECTIVE
INITIATE = views.INITIATE_DIRECTIVE
PLAYING = views.PLAYING_DIRECTIVE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # The directories, not the environment variables that used to name them.
    # Music Assistant supplies no environment and moves both under its own
    # storage path, so these are now module state that `core.configure` sets
    # and setting the variables here would isolate nothing.
    from ma_provider import core, setup_state

    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "captures")
    monkeypatch.setattr(setup_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("SKILL_ID", "")
    (tmp_path / "captures").mkdir()
    return tmp_path


def write_capture(root, offset_seconds: float, directive: str,
                  mtime: float | None = None) -> None:
    """A capture at a chosen age, named exactly the way app.capture names one.

    The name is the whole signal: the detector never stats these. `mtime` is
    here only so a test can set it to something wrong and prove that.
    """
    when = time.time() - offset_seconds
    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = root / "captures" / f"{stamp}-{directive}.json"
    path.write_text("{}")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


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


def test_a_track_boundary_proves_the_binding_as_well_as_an_initiate(isolated):
    """GetNextItem only arrives for a session this skill is already serving.

    It was excluded at first for being the bulk of the traffic. That confused
    two questions: it does not say which search reached playback, but it says
    the binding is alive, which is the only thing the detector acts on.
    """
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 200, PLAYING)
    health = views._binding_health()
    assert health["miss"] is False
    assert health["reached"] == 1
    assert health["last_initiate"] > 0, "playback observed, whatever named it"


def test_health_ignores_directives_that_prove_nothing(isolated):
    """A directive that is neither a search nor evidence of audio is noise."""
    write_capture(isolated, 300, SEARCH)
    write_capture(isolated, 200, "Alexa.Media.Playback.Pause")
    health = views._binding_health()
    assert health["miss"] is True


def test_a_boot_scrub_does_not_scramble_the_history(isolated):
    """The regression this was actually found by.

    scrub_captures rewrites captures in place to strip credentials, and a
    rewrite resets mtime. On the live box that put 111 of 176 binding events
    into a single mtime minute, where ties broke on the directive name: every
    tied search appeared to be followed by another search, so it read as a
    miss. Here every file is given the same wrong mtime, which is the worst
    case of exactly that, and the reading must not move.
    """
    scrub = time.time()
    for offset, directive in ((6000, SEARCH), (5998, INITIATE),
                              (4000, SEARCH), (3998, INITIATE),
                              (2000, SEARCH)):
        write_capture(isolated, offset, directive, mtime=scrub)

    health = views._binding_health()
    assert health["searches"] == 3
    assert health["reached"] == 2, "the two paired searches still pair"
    assert health["miss"] is True, "the unanswered newest search is still a miss"


def test_the_status_and_logs_panels_survive_a_scrub_too(isolated):
    """Same defect, same fix: what the operator is shown must stay ordered.

    "Did anything arrive recently" is the question the Status panel exists to
    answer, so reporting a rewritten capture as having just arrived is the one
    wrong answer it must not give.
    """
    scrub = time.time()
    write_capture(isolated, 9000, SEARCH, mtime=scrub)
    write_capture(isolated, 30, INITIATE, mtime=scrub)

    newest = views.last_requests(limit=4)
    assert newest[0]["directive"].endswith("Initiate"), "newest first"
    assert "seconds ago" in newest[0]["ago"]
    assert "hours ago" in newest[1]["ago"], "the old one is still old"

    names = [name for name, _ in views.recent_captures(4)]
    assert names == sorted(names, reverse=True)


def test_recent_captures_keeps_a_foreign_filename(isolated):
    """A stray file should still be listed, not silently dropped."""
    (isolated / "captures" / "stray.json").write_text("{}")
    assert [n for n, _ in views.recent_captures(4)] == ["stray.json"]


def test_capture_time_reads_the_name_not_the_clock(isolated):
    """Ordering comes from the filename, so it survives anything touching mtime."""
    assert views._capture_time("20260802T153412987654-x.json") == pytest.approx(
        datetime(2026, 8, 2, 15, 34, 12, 987654, tzinfo=timezone.utc).timestamp())
    assert views._capture_time("not-a-capture.json") is None
    assert views._capture_time("20261302T999999000000-x.json") is None


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


def test_a_run_of_misses_triggers_exactly_one_cycle(isolated, armed_skill):
    write_capture(isolated, 400, SEARCH)
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert armed_skill == ["amzn1.ask.skill.test"]
    after = store.load()
    assert after["reactive_armed"] is False
    assert after["reactive_cycled_at"] > 0


def test_a_single_miss_waits_for_a_second_opinion(isolated, armed_skill):
    """The regression that ruined an evening of measurements.

    A voice transfer between speakers emits a TRACK-level search to re-describe
    the playing item and never emits an Initiate, because Amazon re-forms the
    speaker cluster in its own audio layer and re-pulls the existing stream URL
    with a Range request. Measured 2026-08-02: the detector read that as a dead
    binding and cycled the skill one second after a successful Initiate, while
    an Echo was mid-stream. The cycle was itself the outage.
    """
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert armed_skill == [], "one unanswered search is not an outage"
    assert store.load()["reactive_armed"] is True


def test_a_miss_that_playback_followed_is_not_part_of_a_run(isolated, armed_skill):
    """The run has to be trailing. Old misses that recovered are history."""
    write_capture(isolated, 500, SEARCH)
    write_capture(isolated, 400, SEARCH)
    write_capture(isolated, 398, INITIATE)
    write_capture(isolated, 300, SEARCH)
    views._reactive_check(store.load(), time.time(), FakeLog())
    assert armed_skill == [], "only the newest miss is outstanding"


def test_further_misses_do_not_cycle_again(isolated, armed_skill):
    """A cause the cycle cannot fix must not be retried on every request.

    Without this the reactor would churn the enablement against Amazon's rate
    limits for as long as the real fault lasted.
    """
    write_capture(isolated, 400, SEARCH)
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    views._reactive_check(store.load(), time.time(), log)
    assert len(armed_skill) == 1

    write_capture(isolated, 100, SEARCH)
    views._reactive_check(store.load(), time.time(), log)
    views._reactive_check(store.load(), time.time(), log)

    assert len(armed_skill) == 1, "one attempt per outage"
    assert store.load()["reactive_misses"] == 1, "one search, not one per tick"


def test_a_miss_is_counted_once_however_often_it_is_read(isolated, armed_skill):
    """The count is of searches, not of elapsed minutes.

    Measured 2026-08-02: over ten minutes in which no search arrived at all,
    the detector reported the miss count climbing from one to ten, because
    every tick re-read the same unanswered search and added to the total.
    """
    write_capture(isolated, 400, SEARCH)
    write_capture(isolated, 300, SEARCH)
    log = FakeLog()
    for _ in range(6):
        views._reactive_check(store.load(), time.time(), log)
    assert store.load()["reactive_misses"] == 0, "nothing new has missed"
    assert len(armed_skill) == 1


def test_it_re_arms_only_on_observed_playback(isolated, armed_skill):
    """Amazon answering 200 to the cycle proves nothing.

    That is the exact trap this whole failure mode lives in: enablement status
    reports enabled throughout. Only an Initiate landing after the attempt is
    evidence the provider slot is really bound.
    """
    write_capture(isolated, 400, SEARCH)
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
    write_capture(isolated, 400, SEARCH)
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
    write_capture(isolated, 400, SEARCH)
    write_capture(isolated, 300, SEARCH)
    views._reactive_check(store.load(), time.time(), FakeLog())
    assert cycles == []


def test_it_does_nothing_while_disconnected_from_amazon(isolated, monkeypatch):
    store.update(skill_id="amzn1.ask.skill.test", enabled=True)
    monkeypatch.setattr(smapi_rest, "connected", lambda: False)
    cycles = []
    monkeypatch.setattr(views, "_cycle_enablement",
                        lambda sid: cycles.append(sid))
    write_capture(isolated, 400, SEARCH)
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


def test_a_zero_interval_turns_the_keepalive_off(isolated, monkeypatch):
    """The escape hatch for anyone whose binding does not decay."""
    fresh = {"skill_id": "s", "enabled": True, "enabled_at": 0.0}
    assert views._binding_stale(fresh, time.time()) is True
    monkeypatch.setattr(views, "BINDING_KEEPALIVE_HOURS", 0)
    assert views._binding_stale(fresh, time.time()) is False
    store.update(**fresh)
    assert views.scheduler_context(store.load())["keepalive_due"] == ""


def test_soon_reads_as_the_mirror_of_ago():
    assert views.soon(-5) == "due now"
    assert views.soon(30) == "in 30 seconds"
    assert views.soon(600) == "in 10 minutes"
    assert views.soon(7200) == "in 2 hours"
