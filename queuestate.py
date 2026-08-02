"""Per-queue shuffle / loop / repeat state.

Everything else in this service is stateless: a queue is re-derived from its
contentId on every request. Shuffle and loop can't be, because Amazon sends
SetShuffle/SetLoop as fire-and-forget directives whose only acknowledgement is
an empty generic response, and expects the very next GetNextItem to reflect the
change.

State is written to disk rather than held in memory because gunicorn runs
multiple workers, and a dict would give each worker its own divergent view.
Writes are atomic (tmp + rename) so a concurrent read never sees a partial file.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import tempfile
import time

STATE_DIR = pathlib.Path(os.environ.get("QUEUE_STATE_DIR", "/data/queuestate"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT: dict[str, object] = {"shuffle": False, "loop": False, "repeat": "OFF"}

# A queue's state outlives the queue: nothing tells this service that Alexa has
# finished with one, so without an expiry every queue ever started leaves a file
# behind permanently. A week is far longer than any real session, including one
# left paused overnight, and losing a state file only means the queue reverts to
# unshuffled defaults rather than breaking.
STATE_TTL = float(os.environ.get("QUEUE_STATE_TTL", str(7 * 86400)))
_PRUNE_INTERVAL = 600.0
_last_prune = 0.0


def prune(now: float | None = None) -> int:
    """Drop state files no live queue can still be using. Returns how many.

    Self-throttled: this is called from the write path, which runs on every
    shuffle and loop directive, and a directory walk per directive would be
    wasteful for something whose deadline is a week away.
    """
    global _last_prune
    stamp = time.time() if now is None else now
    if stamp - _last_prune < _PRUNE_INTERVAL:
        return 0
    _last_prune = stamp
    dropped = 0
    try:
        entries = list(os.scandir(STATE_DIR))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            if stamp - entry.stat().st_mtime < STATE_TTL:
                continue
            os.unlink(entry.path)
            dropped += 1
        except OSError:
            pass
    return dropped


def _path(queue_id: str) -> pathlib.Path:
    # queueIds are ours (uuid4), but never trust an id straight into a path.
    safe = "".join(c for c in queue_id if c.isalnum() or c in "-_")[:80]
    return STATE_DIR / f"{safe or 'unknown'}.json"


def get(queue_id: str) -> dict:
    try:
        return {**DEFAULT, **json.loads(_path(queue_id).read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT)


def update(queue_id: str, **changes) -> dict:
    state = {**get(queue_id), **changes}
    target = _path(queue_id)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, target)
    except Exception:
        with contextlib_suppress():
            os.unlink(tmp)
        raise
    with contextlib_suppress():
        prune()  # housekeeping must never fail a directive
    return state


class contextlib_suppress:
    """Tiny stand-in so a cleanup failure never masks the original error."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def order(ids: list[str], queue_id: str, shuffled: bool) -> list[str]:
    """Return the play order for a queue.

    The shuffle is seeded with the queueId so every worker, and every later
    request, derives the identical order. A plain random.shuffle would reorder
    the queue on each GetNextItem and playback would wander.
    """
    if not shuffled or len(ids) < 2:
        return ids
    out = list(ids)
    random.Random(queue_id).shuffle(out)
    return out
