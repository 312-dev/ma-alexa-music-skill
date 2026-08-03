"""The library upload, as a Music Assistant background task.

Uploading a real library is minutes of Subsonic crawling followed by five
rate-limited pushes to Amazon. The standalone deployment runs that on a daemon
thread it started itself, publishes progress into a module-level dict, and has
a page poll the dict every two seconds. That works, and it is also a small
reimplementation of something Music Assistant already does properly.

MA's task controller gives progress, captured logs, cancellation, retry, and
an editable schedule, all rendered in a UI the operator is already looking at.
So inside MA the same operation is handed to `mass.tasks` and this module is
only the adapter between the two.

Two things need care, and both come from the work being synchronous.

The crawl blocks, so it runs on Ampere's own thread pool rather than the
event loop. That means progress callbacks arrive on a worker thread, and MA's
progress functions touch loop state, so every report is marshalled back with
`call_soon_threadsafe` instead of being called where it happens.

Cancelling an `asyncio` task does not stop a thread that is already running.
So cancellation is cooperative: MA cancelling the handler sets an event, and
the upload checks it between units of work. Without that, a cancelled upload
would keep crawling in the background, still writing to the state file, while
the UI showed it as stopped.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

from . import binding
from . import setup_ops
from . import setup_state as store

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# Deterministic, so pressing the button twice does not queue two crawls of the
# same library. MA returns the existing task when one with this id is active.
UPLOAD_TASK_ID = "ampere_catalog_upload"
SYNC_TASK_ID = "ampere_catalog_sync"
BINDING_TASK_ID = "ampere_binding_keepalive"

# The floor under the schedule interval. Amazon rate-limits music catalog
# uploads per catalog per day; four runs a day stays under that ceiling, so
# with this floor the limit is unreachable by construction. Most scheduled runs
# upload nothing anyway, because unchanged catalogs are skipped.
MIN_SYNC_HOURS = 6


class AmpereTasks:
    """Ampere's long-running work, as MA sees it."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger,
                 executor: Any = None) -> None:
        self.mass = mass
        self.logger = logger
        self.executor = executor

    # -- running one upload -------------------------------------------------

    def start_upload(self, *, cycle_after: bool = False) -> Any:
        """Queue an upload and return MA's handle for it.

        Returns immediately. Whatever asked for this gets a task in MA's list
        rather than a request that hangs for several minutes.
        """
        return self.mass.tasks.run_background_task(
            name="Ampere: upload library to Amazon",
            handler=lambda: self._upload(cycle_after=cycle_after),
            task_id=UPLOAD_TASK_ID,
            allow_cancel=True,
            # Worth retrying: the common failure is one of the five catalog
            # pushes hitting a rate limit, and a re-run skips everything that
            # already succeeded rather than starting over.
            allow_retry=True,
        )

    async def _upload(self, *, cycle_after: bool) -> None:
        stop = threading.Event()
        loop = asyncio.get_running_loop()

        def report(phase: str, fraction: float | None) -> None:
            # Called from the worker thread. MA's progress functions are not
            # safe to call from one, so this only schedules the call.
            percent = None if fraction is None else round(fraction * 100)
            loop.call_soon_threadsafe(self._report, phase, percent)

        try:
            outcome = await loop.run_in_executor(
                self.executor,
                lambda: setup_ops.run_upload(progress=report,
                                             should_stop=stop.is_set,
                                             cycle_after=cycle_after),
            )
        except asyncio.CancelledError:
            # The thread is still running the crawl. Ask it to stop and let it
            # notice, rather than leaving it writing to the state file behind
            # a task the operator has been told is cancelled.
            stop.set()
            raise

        for row in outcome.rows:
            state = "ok" if row.get("ok") else "failed"
            self.logger.info("%s: %s %s", row.get("kind", ""), state,
                             row.get("detail", ""))
        if not outcome.ok:
            # Raised, not logged and swallowed. A task that fails silently
            # shows as succeeded in MA's list, which is worse than no task at
            # all: it is a green tick over a library Alexa cannot resolve.
            raise RuntimeError(outcome.detail or "the catalog upload failed")

    def _report(self, phase: str, percent: int | None) -> None:
        from music_assistant.controllers.tasks import update_current_task_progress

        update_current_task_progress(percent, phase)

    # -- keeping it up to date ----------------------------------------------

    def register_sync(self) -> None:
        """Register or update the recurring library sync.

        Registered even when the operator has it switched off, because MA's
        scheduled tasks carry their own enabled flag and an editable schedule.
        Removing the task instead would take away the switch along with the
        schedule, and leave nothing in the UI to say the feature exists.
        """
        from music_assistant_models.background_task import TaskSchedule
        from music_assistant_models.enums import TaskScheduleType

        hours = int(store.load().get("auto_sync_hours") or 0)
        schedule = TaskSchedule(
            type=TaskScheduleType.HOURLY,
            every=max(MIN_SYNC_HOURS, hours) if hours else MIN_SYNC_HOURS,
            enabled=hours > 0,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=SYNC_TASK_ID,
            name="Ampere: sync library to Amazon",
            handler=lambda: self._upload(cycle_after=True),
            schedule=schedule,
            allow_cancel=True,
            allow_retry=False,
        )

    def register_binding_keepalive(self) -> None:
        """The keep-alive and the reactive detector, on MA's scheduler.

        Hourly, and not configurable in the UI. The interval is not a
        preference: it has to be well under BINDING_KEEPALIVE_HOURS or the
        keep-alive it drives cannot fire before the decay it exists to
        prevent, and a user who set it to daily would silently get a skill
        that stops answering every afternoon.

        Cheap to run. The ordinary tick reads capture filenames, makes no
        network call, and returns "nothing".
        """
        from music_assistant_models.background_task import TaskSchedule
        from music_assistant_models.enums import TaskScheduleType

        self.mass.tasks.register_scheduled_task(
            task_id=BINDING_TASK_ID,
            name="Ampere: keep the Alexa skill bound",
            handler=self._binding_tick,
            schedule=TaskSchedule(type=TaskScheduleType.HOURLY, every=1,
                                  enabled=True),
            allow_cancel=True,
            allow_retry=False,
        )

    async def _binding_tick(self) -> None:
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(self.executor, binding.tick)
        # Logged only when it acted. An hourly task that says "nothing" every
        # hour is an hourly task nobody reads.
        if outcome != "nothing":
            self.logger.info("binding keep-alive: %s", outcome)

    def unregister_sync(self) -> None:
        """Take the scheduled task away when the provider goes.

        Its handler is a bound method of this instance, so leaving it
        registered would keep a dead provider's upload running on a timer.
        """
        for task_id in (SYNC_TASK_ID, BINDING_TASK_ID):
            try:
                self.mass.tasks.remove_task(task_id)
            except Exception:
                # Already gone, or currently running and therefore not
                # removable. Neither is worth failing an unload over.
                self.logger.debug("%s was not removed", task_id, exc_info=True)
