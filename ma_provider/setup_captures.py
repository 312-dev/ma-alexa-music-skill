"""How captures are named, and how to read that name back.

`app.capture` writes one file per inbound directive, named for the instant it
arrived and the directive it carried:

    20260802T153412987654-Alexa.Media.Playback.Initiate.json

That prefix is the index. Everything here reads it rather than calling stat,
for one reason worth stating plainly: `app.scrub_captures` rewrites captures in
place to strip credentials out of old ones, and a rewrite resets mtime. When it
first ran on a live deployment it flattened 203 files into a single instant,
which silently reordered every panel and measurement built on their mtimes. A
filename is written once and never touched again.

Lives in its own module because both the wizard's step model and the status
views need it, and `steps` cannot import `views` without a cycle.
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime, timezone

# The stamp app.capture writes: %Y%m%dT%H%M%S%f, so eight digits, a T, then
# twelve more (six of clock, six of microseconds).
STAMP = re.compile(r"^(\d{8}T\d{12})-")


def log_dir() -> pathlib.Path:
    """Where captures are, asked of the module that writes them.

    Read from the environment directly until it had two readers in two
    processes. Inside Music Assistant the directory moves under Ampere's own
    storage path, and a second copy of the default would have this module
    listing an empty directory forever while `core` wrote captures elsewhere.
    Nothing about that looks like a fault: the wizard would simply report that
    Amazon has never called.
    """
    from . import core

    return core.LOG_DIR


def capture_time(name: str) -> float | None:
    """When a capture arrived, from its filename. None if it is not one of ours."""
    match = STAMP.match(name)
    if not match:
        return None
    try:
        return (datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f")
                .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def recent(limit: int) -> list[tuple[str, float]]:
    """The newest captures, newest first, as (filename, when it arrived).

    Ordered by name, so the sort costs no syscalls and the result is truncated
    before anything is read. The panels built on this refresh every ten
    seconds, so that ordering is the whole cost of the query.
    """
    try:
        names = sorted((e.name for e in os.scandir(log_dir())
                        if e.name.endswith(".json")), reverse=True)
    except OSError:
        return []
    rows = []
    for name in names[:limit]:
        when = capture_time(name)
        if when is None:
            try:  # not a name this service wrote; list it rather than hide it
                when = (log_dir() / name).stat().st_mtime
            except OSError:
                continue
        rows.append((name, when))
    return rows


def newest_time() -> float | None:
    """When the most recent capture arrived, or None if there are none.

    Falls back to mtime for a name this service did not write. Callers use this
    to decide whether a playback session is live, where reporting activity that
    is not there only delays housekeeping, while missing activity that is there
    could interrupt a song.
    """
    rows = recent(1)
    return rows[0][1] if rows else None
