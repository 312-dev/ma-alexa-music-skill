"""The upload as a Music Assistant background task.

The work itself is covered by the wizard and setup suites. What is tested here
is the adapter, because both of its hazards come from the seam rather than
from either side of it: the work is synchronous and MA is not, so progress
crosses a thread boundary and cancellation does not cross it at all.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from ma_provider import setup_ops, setup_state as store
from ma_provider import tasks as ma_alexa_tasks


class FakeTaskController:
    def __init__(self) -> None:
        self.background: list[dict] = []
        self.scheduled: list[dict] = []
        self.removed: list[str] = []

    def run_background_task(self, **kwargs):
        self.background.append(kwargs)
        return kwargs

    def register_scheduled_task(self, **kwargs):
        self.scheduled.append(kwargs)
        return kwargs

    def remove_task(self, task_id):
        self.removed.append(task_id)


class FakeMass:
    def __init__(self) -> None:
        self.tasks = FakeTaskController()


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    import logging

    monkeypatch.setattr(store, "STATE_DIR", tmp_path)
    return ma_alexa_tasks.MaAlexaTasks(FakeMass(), logging.getLogger("test"))


# --- queueing ---------------------------------------------------------------


def test_an_upload_is_queued_under_a_fixed_id(adapter):
    """Pressing the button twice must not crawl the library twice.

    MA returns the existing task when one with the same id is already active,
    which only works if the id is the same every time. A generated id would
    make the second press queue a second full crawl against a rate-limited
    API.
    """
    adapter.start_upload()
    adapter.start_upload()
    ids = [call["task_id"] for call in adapter.mass.tasks.background]
    assert ids == [ma_alexa_tasks.UPLOAD_TASK_ID] * 2


def test_a_failed_upload_can_be_retried_but_a_schedule_cannot(adapter):
    """A re-run skips whatever already succeeded, so retrying is cheap for a
    person who is watching. A schedule that retried on its own would spend
    rate-limited upload slots with nobody reading the outcome."""
    adapter.start_upload()
    adapter.register_sync()
    assert adapter.mass.tasks.background[0]["allow_retry"] is True
    assert adapter.mass.tasks.scheduled[0]["allow_retry"] is False


# --- the schedule -----------------------------------------------------------


def test_the_schedule_is_registered_even_when_it_is_switched_off(adapter):
    """MA renders a scheduled task with its own enabled switch.

    Registering only when it is on would mean the way to turn it on is a
    control that only appears once it is already on.
    """
    adapter.register_sync()
    schedule = adapter.mass.tasks.scheduled[0]["schedule"]
    assert schedule.enabled is False


def test_an_interval_below_the_floor_is_raised_to_it(adapter):
    """Amazon rate-limits catalog uploads per catalog per day. The floor makes
    that limit unreachable by construction rather than by hoping the operator
    picks a sane number."""
    store.update(auto_sync_hours=1)
    adapter.register_sync()
    schedule = adapter.mass.tasks.scheduled[0]["schedule"]
    assert schedule.enabled is True
    assert schedule.every == ma_alexa_tasks.MIN_SYNC_HOURS


def test_a_sensible_interval_is_kept(adapter):
    store.update(auto_sync_hours=24)
    adapter.register_sync()
    assert adapter.mass.tasks.scheduled[0]["schedule"].every == 24


def test_unloading_takes_every_schedule_away(adapter):
    """Their handlers are bound to this provider instance, so a schedule left
    behind would keep firing into a provider that is gone. Both of them: the
    binding keep-alive is the one that would go on cycling a skill nobody is
    serving."""
    adapter.register_sync()
    adapter.register_binding_keepalive()
    adapter.unregister_sync()
    assert set(adapter.mass.tasks.removed) == {ma_alexa_tasks.SYNC_TASK_ID,
                                               ma_alexa_tasks.BINDING_TASK_ID}


def test_a_schedule_that_will_not_go_does_not_fail_the_unload(adapter):
    """A task that is currently running cannot be removed. That is not a
    reason to leave the provider half unloaded."""
    def refuse(_task_id):
        raise RuntimeError("task is running")

    adapter.mass.tasks.remove_task = refuse
    adapter.unregister_sync()  # must not raise


# --- the two seam hazards ---------------------------------------------------


async def test_progress_from_the_worker_thread_is_marshalled_to_the_loop(
    adapter, monkeypatch
):
    """MA's progress functions touch loop state.

    The crawl reports from a worker thread, so calling them where the report
    happens would be a data race that shows up as corrupted progress or a
    hang, not as an exception.
    """
    seen: list[tuple] = []
    monkeypatch.setattr(adapter, "_report",
                        lambda phase, percent: seen.append(
                            (phase, percent, threading.current_thread().name)))

    loop_thread = threading.current_thread().name

    def fake_run_upload(*, progress, should_stop, cycle_after, **_kw):
        progress("reading the library", 0.5)
        return setup_ops.Outcome(True, "done")

    monkeypatch.setattr(setup_ops, "run_upload", fake_run_upload)
    await adapter._upload(cycle_after=False)

    assert seen == [("reading the library", 50, loop_thread)]


async def test_cancelling_the_task_asks_the_crawl_to_stop(adapter, monkeypatch):
    """Cancelling an asyncio task does not stop a thread that is already
    running. Without the cooperative flag a cancelled upload keeps crawling
    and keeps writing to the state file, while the UI shows it as stopped."""
    started = threading.Event()
    observed: dict = {}

    def fake_run_upload(*, progress, should_stop, cycle_after, **_kw):
        started.set()
        for _ in range(200):
            if should_stop():
                observed["stopped"] = True
                return setup_ops.Outcome(False, "Stopped")
            time.sleep(0.005)
        observed["stopped"] = False
        return setup_ops.Outcome(True, "ran to completion")

    monkeypatch.setattr(setup_ops, "run_upload", fake_run_upload)

    task = asyncio.ensure_future(adapter._upload(cycle_after=False))
    await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The thread is still going; give it a moment to notice the flag.
    for _ in range(200):
        if observed.get("stopped"):
            break
        await asyncio.sleep(0.01)
    assert observed.get("stopped") is True


async def test_a_failed_upload_fails_the_task(adapter, monkeypatch):
    """A task that swallows its failure shows a green tick in MA's list over a
    library Alexa cannot resolve, which is worse than having no task."""
    monkeypatch.setattr(setup_ops, "run_upload",
                        lambda **kw: setup_ops.Outcome(False, "rate limited"))
    with pytest.raises(RuntimeError, match="rate limited"):
        await adapter._upload(cycle_after=False)


async def test_a_successful_upload_does_not_raise(adapter, monkeypatch):
    monkeypatch.setattr(setup_ops, "run_upload",
                        lambda **kw: setup_ops.Outcome(
                            True, "5 of 5", [{"kind": "artists", "ok": True,
                                              "detail": "1 entity"}]))
    await adapter._upload(cycle_after=False)
