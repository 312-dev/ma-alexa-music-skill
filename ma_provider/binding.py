"""Whether the skill is actually bound, and putting it back when it is not.

The most expensive failure this project has had is not an error. Amazon
reports the skill enabled, the search resolves against the catalog and is
answered 200, and `Playback.Initiate` simply never arrives. Every diagnostic
is green and no music plays. Measured 2026-08-02: eight of twenty searches
reached playback, and the binding decayed on a clock rather than breaking on
an event.

So the binding is measured from this service's own traffic instead: for each
search, did a proof-of-playback directive follow it before the next search
did. That question can be answered from the capture filenames alone, and it is
the only honest measure of the binding there is.

Two halves, and they exist for different failures. The keep-alive re-cycles on
a clock, ahead of the decay, and refuses to run while a session is live. The
reactive detector catches whatever that clock is too slow for, and is
deliberately allowed to cycle mid-session, because by the time it fires the
session is already broken.

Lived in the Flask wizard's background thread until that was retired. Nothing
here was tied to it: the loop is now one of Music Assistant's scheduled tasks,
and the logic is unchanged because it was paid for in outages.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from . import setup_captures as captures
from . import setup_ops
from . import setup_state as store
from . import smapi_rest

logger = logging.getLogger("ampere.binding")

AUTO_SYNC_MIN_HOURS = 6


def _auto_sync_due(state: dict, now: float) -> bool:
    hours = int(state.get("auto_sync_hours") or 0)
    if hours <= 0 or not state.get("catalogs"):
        return False
    hours = max(AUTO_SYNC_MIN_HOURS, hours)
    return now - (state.get("last_auto_sync") or 0) >= hours * 3600


# How stale the enablement may get before the keep-alive re-cycles it. Found
# empirically 2026-08-02: the provider-slot binding decays within hours of a
# re-provision cycle (worked 16 minutes after one, dead by 7 hours), leaving
# searches resolving and playback silently never starting.
#
# Configurable because four is an extrapolation from two observations, and the
# real survival time is a property of Amazon's provisioning rather than of this
# code, so it may well differ per account. The miss detector below measures it:
# lower this until the detector stops firing. Cost is only more enablement
# cycles, which draw on a different rate pool from catalog uploads and are
# nowhere near it. Zero disables the keep-alive and leaves only the detector.
BINDING_KEEPALIVE_HOURS = float(os.environ.get("BINDING_KEEPALIVE_HOURS", "4"))


def _binding_stale(state: dict, now: float) -> bool:
    if BINDING_KEEPALIVE_HOURS <= 0:
        return False
    if not (state.get("skill_id") and state.get("enabled")):
        return False
    return now - (state.get("enabled_at") or 0) >= BINDING_KEEPALIVE_HOURS * 3600


def _recent_traffic(max_age: float = 1200.0) -> bool:
    """Has the music endpoint seen a request in the last twenty minutes.

    The keep-alive must never yank the enablement out from under an active
    playback session; the newest capture is the cheapest honest signal that one
    exists. Twenty minutes, not ten: a session only produces a directive per
    track boundary, and a long ambient track or a short pause must not read as
    idle. Staleness tolerance is hours, so the wider window costs nothing."""
    newest = captures.newest_time()
    return newest is not None and time.time() - newest < max_age


# The directives that prove a binding. A search arriving means Alexa routed to
# this skill; an Initiate arriving means the provider slot actually resolved to
# it. The gap between those two is where the decay lives.
SEARCH_DIRECTIVE = "Alexa.Media.Search.GetPlayableContent"
INITIATE_DIRECTIVE = "Alexa.Media.Playback.Initiate"

# GetNextItem only arrives at a track boundary of a session this skill is
# already serving, so it is proof of a live binding just as strong as an
# Initiate. It was excluded at first for being the bulk of the traffic, which
# confused "says nothing about which search reached playback" with "says
# nothing about the binding". It answers the second question dispositively.
PLAYING_DIRECTIVE = "Alexa.Audio.PlayQueue.GetNextItem"
PROOF_DIRECTIVES = (INITIATE_DIRECTIVE, PLAYING_DIRECTIVE)

# How long a search may go unanswered before it counts as a miss. Real pairs
# land about two seconds apart; a minute is far past any plausible slow path
# and keeps a search that is merely in flight from being called a failure.
BINDING_GRACE_SECONDS = 60.0

# How many searches in a row must miss before a cycle is worth attempting.
#
# One is not enough, measured 2026-08-02. A voice transfer between speakers
# emits a TRACK-level search to re-describe the playing item and never emits an
# Initiate, because Amazon re-forms the speaker cluster in its own audio layer
# and simply re-pulls the existing stream URL with a Range request. A single
# superseded or cancelled utterance looks the same. Both are normal, and the
# detector cycled the skill on both, which is worse than doing nothing: the
# cycle lands mid-session and is itself the thing that breaks playback.
#
# The real outage produced three misses in a row with no playback of any kind
# between them, so two is enough to separate the shapes.
REACTIVE_MISS_THRESHOLD = 2


def _binding_events(limit: int = 400) -> list[tuple[float, str]]:
    """Recent searches and proof-of-playback directives, oldest first.

    Read from the capture filenames alone. The name carries a sortable UTC
    stamp and the directive, so lexical order is chronological order and the
    whole history is available for one scandir and no stat calls at all. This
    is the same technique app.prune_captures uses, for the same reason.
    """
    wanted = (SEARCH_DIRECTIVE, *PROOF_DIRECTIVES)
    try:
        names = sorted(
            e.name for e in os.scandir(captures.log_dir())
            if e.name.endswith(".json")
            and any(directive in e.name for directive in wanted)
        )
    except OSError:
        return []
    rows = []
    for name in names[-limit:]:
        when = captures.capture_time(name)
        if when is None:
            continue  # a name we did not write proves nothing about binding
        kind = next(d for d in wanted if d in name)
        rows.append((when, kind))
    return rows


def _binding_health(limit: int = 20, now: float | None = None) -> dict:
    """Did recent searches actually reach playback.

    This is the only honest measure of the binding there is. Amazon's own
    enablement status reports True throughout the failure, the search resolves
    against the catalog and is answered 200, and Initiate simply never comes.
    So the question is asked of our own traffic instead: for each search, did
    an Initiate follow it before the next search did.
    """
    stamp = time.time() if now is None else now
    events = _binding_events()
    pairs = []
    for index, (when, kind) in enumerate(events):
        if kind != SEARCH_DIRECTIVE:
            continue
        following = events[index + 1] if index + 1 < len(events) else None
        if following is None and stamp - when < BINDING_GRACE_SECONDS:
            continue  # still in flight, not yet an answer either way
        pairs.append({"at": when,
                      "reached": bool(following
                                      and following[1] in PROOF_DIRECTIVES)})
    recent = pairs[-limit:]
    played = [w for w, kind in events if kind in PROOF_DIRECTIVES]
    newest = recent[-1] if recent else None

    # Trailing run only. An older miss that has since been followed by a search
    # that reached playback is history, not an outage.
    consecutive = 0
    for pair in reversed(recent):
        if pair["reached"]:
            break
        consecutive += 1

    return {
        "searches": len(recent),
        "reached": sum(1 for p in recent if p["reached"]),
        "miss": bool(newest and not newest["reached"]),
        "miss_at": newest["at"] if newest and not newest["reached"] else 0.0,
        "consecutive_misses": consecutive,
        "last_initiate": played[-1] if played else 0.0,
    }


def _reactive_check(state: dict, now: float, log) -> None:
    """Cycle once when a search fails to reach playback, then wait for proof.

    The breaker exists because a cycle is not guaranteed to be the fix. If the
    cause is something else, an unbounded reactor would cycle on every failed
    request and churn the enablement against Amazon's rate limits for nothing.
    So one attempt per outage, and re-arming requires evidence from a device,
    never Amazon's own acknowledgement of the cycle: a 200 there proves
    nothing, which is the trap this entire failure mode lives in.
    """
    health = _binding_health(now=now)
    armed = bool(state.get("reactive_armed", True))
    cycled_at = float(state.get("reactive_cycled_at") or 0.0)

    if not armed and health["last_initiate"] > cycled_at:
        store.update(reactive_armed=True, reactive_misses=0)
        log.info("binding detector: playback recovered, re-armed")
        return

    if not health["miss"]:
        return

    # Counted once per search, not once per check. The miss is identified by
    # the instant it happened, so a tick that re-reads the same unanswered
    # search adds nothing. Without this the count was really a count of
    # elapsed minutes: ten reported misses from one real one, measured
    # 2026-08-02 over a window with no searches in it at all.
    if health["miss_at"] <= float(state.get("reactive_last_miss") or 0.0):
        return

    if not armed:
        misses = int(state.get("reactive_misses") or 0) + 1
        store.update(reactive_misses=misses, reactive_last_miss=health["miss_at"])
        log.warning("binding detector: still degraded, %d search(es) have "
                    "missed since the cycle at %s; a re-cycle is not the fix "
                    "for this one", misses, iso_utc(cycled_at))
        return

    if health["consecutive_misses"] < REACTIVE_MISS_THRESHOLD:
        store.update(reactive_last_miss=health["miss_at"])
        log.info("binding detector: a search missed playback, waiting for a "
                 "second before calling it an outage (a voice transfer looks "
                 "exactly like this and is not one)")
        return

    if not (state.get("skill_id") or os.environ.get("SKILL_ID", "")):
        return
    if not smapi_rest.connected():
        return

    log.warning("binding detector: %d searches in a row never reached "
                "playback, cycling once", health["consecutive_misses"])
    outcome = setup_ops.cycle_enablement(
        state.get("skill_id") or os.environ.get("SKILL_ID", ""))
    store.update(reactive_armed=False, reactive_cycled_at=time.time(),
                 reactive_misses=0, reactive_last_miss=health["miss_at"])
    log.info("binding detector: %s", outcome.detail)


def iso_utc(when: float) -> str:
    if not when:
        return "never"
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")



def tick(now: float | None = None, log: logging.Logger | None = None) -> str:
    """One pass of the keep-alive and the detector. Returns what it did.

    Blocking, and safe to call on a schedule. Order matters: the keep-alive is
    tried first because it is the cheap preventive one and refuses to run
    during a session, and the reactive check is last because it is the only
    path here that may cycle mid-session.

    Every branch is a no-op when there is nothing to do, so the ordinary
    result is "nothing", and a caller can log the exceptions rather than the
    ticks.
    """
    log = log or logger
    stamp = time.time() if now is None else now
    state = store.load()

    if not smapi_rest.connected():
        return "not connected to Amazon"

    if _binding_stale(state, stamp) and not _recent_traffic():
        log.info("binding keep-alive: last cycle is stale, re-cycling")
        outcome = setup_ops.cycle_enablement(setup_ops.skill_id(state))
        log.info("binding keep-alive: %s", outcome.detail)
        return f"keep-alive: {outcome.detail}"

    # The reactive half. The keep-alive above prevents the decay it knows
    # about, on a clock derived from one observation; this catches whatever
    # that clock is too slow for.
    before = store.load().get("reactive_cycled_at") or 0.0
    _reactive_check(state, stamp, log)
    after = store.load().get("reactive_cycled_at") or 0.0
    return "detector cycled the skill" if after > before else "nothing"


# The public names. The leading underscores are historical: these were private
# to a Flask module, and every one of them is now part of this module's point.
binding_stale = _binding_stale
binding_health = _binding_health
recent_traffic = _recent_traffic
reactive_check = _reactive_check
auto_sync_due = _auto_sync_due
