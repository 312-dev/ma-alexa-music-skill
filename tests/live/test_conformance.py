"""The live conformance cases: one per meaningful cell of `matrix.CELLS`.

Marked `live` and excluded from a default `pytest` run, because every one of
these plays audio in a real apartment. `pytest -m live tests/live` is the only
way to start them.

The house style of the assertions, which is the whole point of the suite:

*Nothing asserts on the absence of an error.* `player_queues/play_media` skips
an unresolvable item and returns success; `handle_player_command` swallows
anything aimed at an unavailable player and returns success. A green test built
on "it did not raise" is green against a provider that never reached the
speaker. Every case reads state back and asserts on what it finds - queue
contents, playback state, elapsed time, reported volume.

*Nothing looks too early.* Music Assistant answers optimistically: it writes the state it
expects and publishes it before Alexa has been asked. `harness.observe()`
refuses to read before the relevant floor, so a pass means the value survived
Music Assistant's own re-poll rather than merely that Music Assistant was hopeful.

*Some of these cases are expected to fail.* Seek and rewind assert what Music
Assistant is supposed to report about a position after it has been moved, and it
reports something else. Stop asserts that a stopped speaker stays stopped, and
on Subsonic it does not. Group pause asserts that a paused group stays paused,
and roughly one time in three it does not. The fault in each is in the provider,
not in the assertion, and softening the assertion would delete the finding
rather than fix it. The failure messages carry the measurement that supports the
claim - read those first, not the traceback.
"""

from __future__ import annotations

import time

import pytest

from tests.live import matrix, safety
from tests.live.harness import (
    MAX_VOLUME,
    NEXT_PREV_DEBOUNCE,
    PLAY_CONFIRM_SECONDS,
    PREVIOUS_RESTART_ELAPSED,
    RESYNC_SECONDS,
    VOLUME_BUDGET,
    VOLUME_QUEUE_DELAY,
    VOLUME_TOLERANCE,
    RECORDER,
    MAError,
    names_match,
    observe,
    record,
)

pytestmark = pytest.mark.live

# Budgets. A floor is the earliest an answer can be honest; a budget is the
# latest it may arrive before the run says so. Exceeding a budget is reported as
# a finding, not silently tolerated, and the assertion failing is the report.
TRANSPORT_BUDGET = 15.0
PLAY_BUDGET = 25.0
NEXT_BUDGET = 25.0
SEEK_BUDGET = 25.0
GROUP_VOLUME_BUDGET = VOLUME_BUDGET + 3.0
# How long a resumed player has to move two seconds of position before it is
# called stalled, and how long a played one has to reach a position worth
# seeking away from. Budgets, not sleeps: both are usually met in a third of
# the time, and both were previously paid in full on every run.
RESUME_ADVANCE_BUDGET = 8.0
SEEK_SETTLE_BUDGET = 6.0


@pytest.fixture(autouse=True)
def leave_it_quiet(ma_alexa):
    """Nothing is left playing between cases, and nothing is left paused.

    The paused half matters as much as the playing half: `player_queues/pause`
    arms a 30 second watchdog that stops the queue on its own, so a case that
    finished paused would hand the next one a player that transitions to idle
    underneath it, at a time that depends on how long the next case took to set
    up. That reads as flakiness and is not.

    This also closes the suite's blind spot, and does it here rather than in a
    fixture of its own so that the check runs *after* `quiesce` and therefore
    sees the stops it issues - which is where the give-ups were actually
    happening. Music Assistant answers every control optimistically, so a command that
    Amazon dropped, that was resent twice and still lost, is identical from
    Music Assistant's side to one that worked first time. Everything the cells
    assert on comes from Music Assistant. One full run had 25 transport
    resends and 5 outright give-ups on the speaker group and reported six
    green stop cells; it was found by reading the provider's logs.

    Resends are recorded and do not fail: Amazon dropping a command and taking
    it on the second ask is documented behaviour that the retry exists for.
    Giving up entirely is a different claim - it means the speaker was left
    doing something other than what was asked - and that fails.
    """
    before = {t.player_id: ma_alexa.s.convergence(t.player_id)
              for t in ma_alexa.all_targets()}
    yield
    ma_alexa.quiesce()

    lost, resent = [], 0
    for target in ma_alexa.all_targets():
        was_resends, was_gave_up = before.get(target.player_id, (0, 0))
        now_resends, now_gave_up = ma_alexa.s.convergence(target.player_id)
        resent += now_resends - was_resends
        if now_gave_up > was_gave_up:
            lost.append(f"{target.name} ({now_gave_up - was_gave_up})")
    if resent or lost:
        RECORDER.note_convergence(resends=resent, lost=lost)
    assert not lost, (
        "a command never stuck: " + ", ".join(lost) + ". The cell may have "
        "passed anyway because Music Assistant reports the state it was asked for "
        "without waiting for Alexa to agree.")


def cells(feature: str):
    found = matrix.for_feature(feature)
    return pytest.mark.parametrize("cell", found, ids=[c.id for c in found])


def _gate(cell: matrix.Cell) -> None:
    """Skipped cells carry their reason into the pytest report, not just here."""
    if cell.status == matrix.SKIP:
        pytest.skip(f"{cell.id}: {cell.reason}")


def _tracks_for(source: str, wanted: int) -> int:
    # There is one SomaFM station in play; a radio queue is one live item.
    return 1 if source == "radio" else wanted


# --- play --------------------------------------------------------------------


@cells("play")
def test_play(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    media, obs = ma_alexa.arrange_playing(target, cell.source, tracks=1)

    assert obs.ok, f"{cell.id}: {obs.detail} queued={obs.extra.get('queued')}"

    items = ma_alexa.s.queue_items(target.queue_id)
    assert len(items) == 1, f"expected one queued item, got {[i['name'] for i in items]}"
    # The item that is there is the one that was asked for. `play_media`
    # succeeding is compatible with it having queued something else entirely,
    # or nothing, so identity is checked rather than count alone.
    assert names_match([str(i.get("name", "")) for i in items], [m["name"] for m in media]), \
        f"queued {[i.get('name') for i in items]}, asked for {[m['name'] for m in media]}"


# --- pause / resume / stop ---------------------------------------------------


@cells("pause")
def test_pause(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    # Pause a stream that is actually running. A handover is still in flight for
    # several seconds after `play_media` reports playing - Alexa buffers, and MA
    # shows a `paused` nobody asked for - and a pause issued into that window is
    # testing the handover rather than the pause.
    assert ma_alexa.settle(target), "precondition: playback never settled"

    _, issued, ack = ma_alexa.s.call_timed("player_queues/pause", queue_id=target.queue_id)
    event_ms = ma_alexa.s.wait_for_event(issued, target.player_id, {"player_updated"}, 4.0)
    ok, effect_ms = ma_alexa.wait_state(
        target, "paused", floor=RESYNC_SECONDS, budget=TRANSPORT_BUDGET, issued=issued,
    )
    record("pause", target, cell.source, ok=ok, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=RESYNC_SECONDS, budget=TRANSPORT_BUDGET,
           detail=f"state={ma_alexa.s.player(target.player_id).get('state')}")
    assert ok, "still not paused after the re-poll window"


@cells("resume")
def test_resume(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    assert ma_alexa.settle(target), "precondition: playback never settled"

    ma_alexa.s.call("player_queues/pause", queue_id=target.queue_id)
    paused, _ = ma_alexa.wait_state(target, "paused", floor=RESYNC_SECONDS,
                                 budget=TRANSPORT_BUDGET, issued=time.monotonic())
    assert paused, "precondition: could not get it paused to resume from"
    before = ma_alexa.s.elapsed(target.queue_id)

    _, issued, ack = ma_alexa.s.call_timed("player_queues/resume", queue_id=target.queue_id)
    event_ms = ma_alexa.s.wait_for_event(issued, target.player_id, {"player_updated"}, 4.0)
    ok, effect_ms = ma_alexa.wait_state(
        target, "playing", floor=RESYNC_SECONDS, budget=PLAY_BUDGET, issued=issued,
    )
    # Playing is not enough on its own - a stalled player also reports playing -
    # so the position has to be moving. Measured across two reads *after* the
    # resume rather than against the position before the pause, because MA sets
    # `resume_pos = 0` for a live radio item on purpose (there is nowhere to
    # resume to in a stream), and comparing to the pre-pause position would call
    # that correct behaviour a failure.
    #
    # Asking for a real advance, not any advance at all. `elapsed()`
    # extrapolates forward from the last publish, so a poll landing inside the
    # window replaces an extrapolated value with a measured one and the reading
    # can step *down* a fraction. Measured twice on a streaming group: 5.0 then
    # 4.6 four seconds later, recorded as "the position is not moving" on a
    # player that had plainly resumed from 0.7.
    #
    # Watched rather than slept through. The eight seconds is the budget a
    # stalled player has to spend before it is called stalled, not a duration a
    # healthy one should pay: a speaker genuinely playing clears two seconds of
    # advance in about two, and the suite ran this six times.
    first = ma_alexa.s.elapsed(target.queue_id)
    second = first

    def advanced() -> bool:
        nonlocal second
        second = ma_alexa.s.elapsed(target.queue_id)
        return second - first >= 2.0

    advancing, _ = observe(ma_alexa.s, advanced, floor=0.0, budget=RESUME_ADVANCE_BUDGET,
                           issued=time.monotonic())
    record("resume", target, cell.source, ok=ok and advancing, ack_ms=ack,
           event_ms=event_ms, effect_ms=effect_ms, floor=RESYNC_SECONDS,
           budget=PLAY_BUDGET, paused_at=round(before, 1),
           detail=f"paused at {before:.1f}; after resume {first:.1f} -> {second:.1f}")
    assert ok, "did not return to playing"
    assert advancing, (f"reports playing but the position is not moving: "
                       f"{first:.1f} then {second:.1f} after "
                       f"{RESUME_ADVANCE_BUDGET:.0f}s of watching")


@cells("stop")
def test_stop(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    assert ma_alexa.settle(target), "precondition: playback never settled"

    _, issued, ack = ma_alexa.s.call_timed("player_queues/stop", queue_id=target.queue_id)
    event_ms = ma_alexa.s.wait_for_event(issued, target.player_id, {"player_updated"}, 4.0)
    ok, effect_ms = ma_alexa.wait_state(
        target, "idle", floor=RESYNC_SECONDS, budget=TRANSPORT_BUDGET, issued=issued,
    )
    # Reaching idle is not the whole of "stopped". A stop that is undone a few
    # seconds later by a poll, a resend or a replayed push event leaves a
    # speaker playing in an empty room, and a test that read the state once at
    # the moment it first went quiet would call that a pass.
    time.sleep(RESYNC_SECONDS + 4.0)
    settled_state = ma_alexa.s.player(target.player_id).get("state")
    stayed = settled_state == "idle"
    record("stop", target, cell.source, ok=ok and stayed, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=RESYNC_SECONDS, budget=TRANSPORT_BUDGET,
           detail=f"reached idle={ok}; state {RESYNC_SECONDS + 4.0:.1f}s later "
                  f"={settled_state}")
    assert ok, "still not idle after the re-poll window"
    assert stayed, f"stopped, then went back to {settled_state} on its own"


# --- next / previous ---------------------------------------------------------


@cells("next")
def test_next(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    media, played = ma_alexa.arrange_playing(target, cell.source, tracks=3)
    assert played.ok, f"precondition: {played.detail}"
    assert ma_alexa.settle(target), "precondition: the first track never settled"

    before = ma_alexa.s.queue(target.queue_id)
    index_before = before.get("current_index")
    uri_before = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("uri")

    _, issued, ack = ma_alexa.s.call_timed("player_queues/next", queue_id=target.queue_id)
    event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_updated"}, 4.0)

    # The queue index moves at once; the audio moves a second later, because
    # next debounces through `mass.call_later(1, ...)` so that two presses skip
    # one track and not two. The two are measured separately on purpose.
    moved, index_ms = observe(
        ma_alexa.s,
        lambda: ma_alexa.s.queue(target.queue_id).get("current_index") != index_before,
        floor=0.0, budget=6.0, issued=issued,
    )

    def on_new_track() -> bool:
        media_now = ma_alexa.s.player(target.player_id).get("current_media") or {}
        return bool(media_now.get("uri")) and media_now.get("uri") != uri_before

    ok, effect_ms = observe(
        ma_alexa.s, on_new_track,
        floor=NEXT_PREV_DEBOUNCE + PLAY_CONFIRM_SECONDS, budget=NEXT_BUDGET, issued=issued,
    )
    after = ma_alexa.s.queue(target.queue_id)
    title_after = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("title")
    # The index is checked at the end, not only that it moved at some point:
    # a transition can be *reverted* when Music Assistant's poll lands mid-change and MA
    # resolves the index from whatever Alexa is still reporting. "It moved and
    # then came back" is a failure, and recording only `moved` would file it as
    # a pass.
    stepped = after.get("current_index") == (index_before or 0) + 1
    record("next", target, cell.source, ok=ok and moved and stepped, ack_ms=ack,
           event_ms=event_ms, effect_ms=effect_ms,
           floor=NEXT_PREV_DEBOUNCE + PLAY_CONFIRM_SECONDS,
           budget=NEXT_BUDGET, index_ms=index_ms, player_title=title_after,
           moved_then_reverted=bool(moved and not stepped),
           detail=f"index {index_before} -> {after.get('current_index')}, "
                  f"speaker on {title_after!r}"
                  + (" (moved, then reverted)" if moved and not stepped else ""))
    assert moved, f"current_index never left {index_before}"
    assert stepped, (f"expected one step forward, got {index_before} -> "
                     f"{after.get('current_index')}; the index moved and then "
                     f"came back, and the speaker is on {title_after!r}")
    assert ok, "the player is still reporting the previous track"


@cells("previous")
def test_previous(ma_alexa, cell):
    """`previous` steps back only while the current track is under 5s in.

    Past that it restarts the current track, which is what a listener expects
    from the button and is why this asserts against the elapsed time read at the
    moment the command went out rather than against a fixed index delta.
    """
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=3)
    assert played.ok, f"precondition: {played.detail}"
    assert ma_alexa.settle(target), "precondition: the first track never settled"

    # Named before the command, because "settled" has to mean settled on the
    # *new* item: without a name to wait for, the queue is still validly settled
    # on the item it has not left yet and the wait returns instantly.
    expected = (ma_alexa.s.queue(target.queue_id).get("next_item") or {}).get("name")
    assert expected, "precondition: the queue has no next item to step onto"

    ma_alexa.s.call("player_queues/next", queue_id=target.queue_id)
    # One transition at a time. Issuing `previous` while the `next` is still in
    # flight is what makes this case look intermittently broken: MA's index
    # snaps to whichever item Alexa was still reporting.
    assert ma_alexa.settle(target, expect_item=expected), \
        f"precondition: {expected!r} never settled after next"
    before = ma_alexa.s.queue(target.queue_id)
    index_before = before.get("current_index")
    assert index_before, "precondition: next did not move off the first item"
    # The *raw* stored value, because that is the one MA branches on. The
    # corrected value extrapolates forward from the last publish and is
    # therefore larger, and predicting a step back from a corrected 4.9 when MA
    # sees a raw 5.1 is a test that fails for arithmetic reasons.
    elapsed_at_issue = ma_alexa.s.raw_elapsed(target.queue_id)

    _, issued, ack = ma_alexa.s.call_timed("player_queues/previous", queue_id=target.queue_id)
    event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_updated"}, 4.0)

    restart_expected = elapsed_at_issue >= PREVIOUS_RESTART_ELAPSED
    if restart_expected:
        # A restart is not visible in the index, so it is asserted against the
        # position: had `previous` done nothing at all, the track would simply
        # have carried on to `elapsed_at_issue + drift`. Being meaningfully
        # behind that is the only evidence a restart happened.
        want_index = index_before

        def landed() -> bool:
            queue = ma_alexa.s.queue(target.queue_id)
            if queue.get("current_index") != index_before:
                return False
            untouched = elapsed_at_issue + (time.monotonic() - issued)
            return ma_alexa.s.elapsed(target.queue_id) < untouched - 3.0
    else:
        want_index = index_before - 1

        def landed() -> bool:
            return ma_alexa.s.queue(target.queue_id).get("current_index") == want_index

    ok, effect_ms = observe(
        ma_alexa.s, landed,
        floor=NEXT_PREV_DEBOUNCE + PLAY_CONFIRM_SECONDS, budget=NEXT_BUDGET, issued=issued,
    )
    after = ma_alexa.s.queue(target.queue_id)
    title_after = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("title")
    record("previous", target, cell.source, ok=ok, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=NEXT_PREV_DEBOUNCE + PLAY_CONFIRM_SECONDS,
           budget=NEXT_BUDGET, restart_expected=restart_expected,
           elapsed_at_issue=round(elapsed_at_issue, 1), player_title=title_after,
           detail=f"elapsed {elapsed_at_issue:.1f} at issue, index "
                  f"{index_before} -> {after.get('current_index')}, speaker on "
                  f"{title_after!r}, {'restart' if restart_expected else 'step back'} expected")
    assert ok, (f"elapsed was {elapsed_at_issue:.1f} at issue so a "
                f"{'restart' if restart_expected else 'step back to ' + str(want_index)} "
                f"was due; index is {after.get('current_index')}, elapsed "
                f"{ma_alexa.s.elapsed(target.queue_id):.1f}, and the speaker is on "
                f"{title_after!r}")


# --- seek / rewind -----------------------------------------------------------


SEEK_TO = 100
REWIND_BY = -20

# How far the speaker may land from where it was sent. Generous, because the
# measurement spans a play_media round trip through Amazon and a poll interval.
SEEK_LANDING_TOLERANCE = 12.0
# The difference between Music Assistant's reported position and Music Assistant's own
# is recorded by `seek` and `rewind` but no longer asserted on. The two are not
# taken from one snapshot: each layer stores its value when it last updated, so
# the difference carries however long has passed between them as well as the
# seek offset that genuinely separates them. Whether the seek landed where it
# was asked to is the property worth holding, and both cells assert that
# directly.


def _seek_error_case(ma_alexa, cell, command: str, args: dict) -> None:
    """A live stream has no duration, so MA must refuse to move its position."""
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"

    issued = time.monotonic()
    raised: MAError | None = None
    try:
        ma_alexa.s.call(command, queue_id=target.queue_id, **args)
    except MAError as exc:
        raised = exc
    ack = (time.monotonic() - issued) * 1000.0

    ok = raised is not None and raised.code == cell.error_code
    record(cell.feature, target, cell.source, ok=ok, ack_ms=ack, floor=0.0, budget=5.0,
           error_code=raised.code if raised else None,
           detail=(f"raised {raised.code}" if raised else "returned success"))
    assert raised is not None, (
        f"{command} on a queue with no duration returned success. A silent no-op "
        f"is indistinguishable from a seek that happened, which is the failure "
        f"mode this cell exists to catch."
    )
    assert raised.code == cell.error_code, \
        f"expected error {cell.error_code}, got {raised.code}: {raised.details}"


@cells("seek")
def test_seek(ma_alexa, cell):
    """Two questions, deliberately asserted apart.

    Did the speaker move, and does Music Assistant report where it moved to.
    `queue.elapsed_time` is what every UI shows and is the media time this
    asserts on; `player.elapsed_time` is Music Assistant's own layer underneath it.

    The two are *supposed* to differ, by exactly the offset the track was
    republished at. MA adds `streamdetails.seek_position` back on for a player
    that is not in flow mode, so Music Assistant reports its position relative to where
    the stream starts and MA restores the media time. An earlier version of
    this asserted the two layers agreed, which was true while they were both
    absolute -- and the seek landed twice. Agreement was never the property
    worth having; the media time being the one asked for is.
    """
    _gate(cell)
    if cell.status == matrix.EXPECT_ERROR:
        _seek_error_case(ma_alexa, cell, "player_queues/seek", {"position": SEEK_TO})
        return

    target = ma_alexa.target(cell.target)
    media, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    duration = media[0]["duration"]
    assert duration > SEEK_TO + 30, f"{media[0]['name']} is too short to seek in"
    # Wait until the track is genuinely underway before moving it, rather than
    # sleeping a fixed six seconds. Seeking a player still reporting 0 tests the
    # publish and not the seek; one second of real position is enough to know
    # it started, and a player that never gets there fails the seek anyway.
    observe(ma_alexa.s, lambda: ma_alexa.s.elapsed(target.queue_id) >= 1.0,
            floor=0.0, budget=SEEK_SETTLE_BUDGET, issued=time.monotonic())

    _, issued, ack = ma_alexa.s.call_timed(
        "player_queues/seek", queue_id=target.queue_id, position=SEEK_TO)
    event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_time_updated"}, 6.0)

    def landed() -> bool:
        # The media time, which is the number a person sees and the one the
        # seek was expressed in.
        where = ma_alexa.s.elapsed(target.queue_id)
        if not where:
            return False
        return abs(where - (SEEK_TO + (time.monotonic() - issued))) <= SEEK_LANDING_TOLERANCE

    ok, effect_ms = observe(ma_alexa.s, landed, floor=PLAY_CONFIRM_SECONDS,
                            budget=SEEK_BUDGET, issued=issued)

    drift = time.monotonic() - issued
    raw_player, _corrected_player = ma_alexa.s.player_elapsed(target.player_id)
    raw_queue = ma_alexa.s.raw_elapsed(target.queue_id)
    where = ma_alexa.s.elapsed(target.queue_id)
    # Both raw, so this is two readings of one quantity rather than two clocks.
    # Expected to be the seek offset, because that is precisely what MA adds
    # back: zero here means Music Assistant has stopped taking it off and the seek is
    # landing twice again, and twice the offset means it is coming off twice.
    offset = raw_queue - raw_player
    # Recorded, not asserted. The two are stored at whatever instant each layer
    # last updated and there is no read that takes them together, so the
    # difference carries however long has passed between them: measured at
    # +103.2 against a 100s seek purely because the queue had advanced 3.2s
    # since Music Assistant's snapshot. It is evidence about which layer holds what, not
    # a property this can hold to a tolerance.
    #
    # Nothing is lost by dropping it. `ok` above catches the double count
    # outright and more directly: an offset applied twice puts the queue at
    # 200s for a 100s seek, which is nowhere near where it was asked to be.
    record("seek", target, cell.source, ok=ok, ack_ms=ack,
           event_ms=event_ms, effect_ms=effect_ms, floor=PLAY_CONFIRM_SECONDS,
           budget=SEEK_BUDGET, player_elapsed=round(raw_player, 1),
           queue_elapsed=round(raw_queue, 1), asked=SEEK_TO,
           offset=round(offset, 1),
           detail=f"asked {SEEK_TO}; MA reports {where:.1f} after {drift:.1f}s; "
                  f"MA/Music Assistant raw {raw_queue:.1f}/{raw_player:.1f} "
                  f"(offset {offset:+.1f}, expected {SEEK_TO})")

    assert ok, (
        f"the queue did not move to {SEEK_TO}s: MA reads {where:.1f} "
        f"{drift:.1f}s later, with Music Assistant holding {raw_player:.1f}. Landing at "
        f"roughly twice {SEEK_TO} means the offset is being counted twice: MA "
        f"adds `streamdetails.seek_position` to a non-flow player's position, "
        f"because its own stream server starts the audio *at* the seek point, "
        f"and Music Assistant must therefore report its position relative to the offset "
        f"it published the track with rather than as absolute media time."
    )


@cells("rewind")
def test_rewind(ma_alexa, cell):
    """There is no rewind command. Rewind is `skip` with negative seconds."""
    _gate(cell)
    if cell.status == matrix.EXPECT_ERROR:
        _seek_error_case(ma_alexa, cell, "player_queues/skip", {"seconds": REWIND_BY})
        return

    target = ma_alexa.target(cell.target)
    media, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    # Far enough in that a 20 second rewind lands somewhere. Waited for rather
    # than slept through: `skip` reads the raw stored position, which only
    # moves when the player publishes, and Music Assistant publishes on a poll that
    # slows to a minute while the push stream is healthy. A fixed 28 second
    # sleep left MA holding 2.2s of a track that had been playing for half a
    # minute, and the cell failed on a precondition that was really MA's
    # position being stale.
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if ma_alexa.s.raw_elapsed(target.queue_id) > abs(REWIND_BY) + 5.0:
            break
        time.sleep(1.0)
    # The raw stored value, not the extrapolated one, because that is what MA
    # subtracts from:
    #
    #     async def skip(self, queue_id, seconds=10):
    #         await self.seek(queue_id, int(self._queues[queue_id].elapsed_time + seconds))
    #
    # Predicting from `elapsed()` made this wrong by however long it had been
    # since the player last published, which for Music Assistant is a ten second poll:
    # measured a prediction of 11.0s against a rewind that actually landed at
    # 20.7s, and the cell then failed for a reason that had nothing to do with
    # the speaker. The same trap `raw_elapsed` was written for.
    before = ma_alexa.s.raw_elapsed(target.queue_id)
    assert before > abs(REWIND_BY), f"only {before:.1f}s in; nothing to rewind"
    expected = int(before) + REWIND_BY

    _, issued, ack = ma_alexa.s.call_timed(
        "player_queues/skip", queue_id=target.queue_id, seconds=REWIND_BY)
    event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_time_updated"}, 6.0)

    def moved_back() -> bool:
        # The media time, for the same reason as `seek`: it is the number a
        # person reads, and the number the rewind was expressed in.
        where = ma_alexa.s.elapsed(target.queue_id)
        return abs(where - (expected + (time.monotonic() - issued))) <= SEEK_LANDING_TOLERANCE

    ok, effect_ms = observe(ma_alexa.s, moved_back, floor=PLAY_CONFIRM_SECONDS,
                            budget=SEEK_BUDGET, issued=issued)
    drift = time.monotonic() - issued
    raw_player, _corrected_player = ma_alexa.s.player_elapsed(target.player_id)
    raw_queue = ma_alexa.s.raw_elapsed(target.queue_id)
    where = ma_alexa.s.elapsed(target.queue_id)
    # As in `seek`: the two layers differ by the offset the track was
    # republished at, which for a rewind is wherever `skip` landed.
    # Recorded, not asserted; see the note in `seek`.
    offset = raw_queue - raw_player

    record("rewind", target, cell.source, ok=ok, ack_ms=ack,
           event_ms=event_ms, effect_ms=effect_ms, floor=PLAY_CONFIRM_SECONDS,
           budget=SEEK_BUDGET, player_elapsed=round(raw_player, 1),
           queue_elapsed=round(raw_queue, 1), asked=round(expected, 1),
           offset=round(offset, 1),
           detail=f"{before:.1f} - {abs(REWIND_BY)} = {expected:.1f}; MA reports "
                  f"{where:.1f} after {drift:.1f}s; MA/Music Assistant raw "
                  f"{raw_queue:.1f}/{raw_player:.1f} (offset {offset:+.1f}, "
                  f"expected {expected:.1f})")
    assert ok, (f"rewind should have landed near {expected:.1f}s; MA reads "
                f"{where:.1f}, with Music Assistant holding {raw_player:.1f}")


# --- shuffle / repeat --------------------------------------------------------


@cells("shuffle")
def test_shuffle(ma_alexa, cell):
    """Enable, then disable, and check the order comes back.

    Asserting that enabling shuffle *changed* the order would be a flaky test:
    with three items left to shuffle, one run in six reshuffles them into the
    order they were already in. Turning shuffle off re-sorts the remainder by
    `sort_index`, which is deterministic, so that is the half worth asserting.
    """
    _gate(cell)
    target = ma_alexa.target(cell.target)
    tracks = _tracks_for(cell.source, 4)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=tracks)
    assert played.ok, f"precondition: {played.detail}"

    original = [i["queue_item_id"] for i in ma_alexa.s.queue_items(target.queue_id)]

    _, issued, ack = ma_alexa.s.call_timed(
        "player_queues/shuffle", queue_id=target.queue_id, shuffle_enabled=True)
    event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_updated"}, 4.0)
    on, effect_ms = observe(
        ma_alexa.s, lambda: ma_alexa.s.queue(target.queue_id).get("shuffle_enabled") is True,
        floor=0.0, budget=6.0, issued=issued)
    shuffled = [i["queue_item_id"] for i in ma_alexa.s.queue_items(target.queue_id)]

    ma_alexa.s.call("player_queues/shuffle", queue_id=target.queue_id, shuffle_enabled=False)
    off, _ = observe(
        ma_alexa.s, lambda: ma_alexa.s.queue(target.queue_id).get("shuffle_enabled") is False,
        floor=0.0, budget=6.0, issued=time.monotonic())
    restored = [i["queue_item_id"] for i in ma_alexa.s.queue_items(target.queue_id)]

    kept_items = sorted(shuffled) == sorted(original)
    ok = on and off and kept_items and restored == original
    record("shuffle", target, cell.source, ok=ok, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=0.0, budget=6.0, items=len(original),
           reordered=shuffled != original,
           detail=f"{len(original)} items; on={on} off={off} "
                  f"reordered={shuffled != original} restored={restored == original}")
    assert on, "shuffle_enabled did not become true"
    assert kept_items, "shuffling changed which items are in the queue, not just their order"
    assert off, "shuffle_enabled did not go back to false"
    assert restored == original, "turning shuffle off did not restore the sort_index order"


@cells("repeat")
def test_repeat(ma_alexa, cell):
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=_tracks_for(cell.source, 2))
    assert played.ok, f"precondition: {played.detail}"

    seen: dict[str, str | None] = {}
    ack = event_ms = effect_ms = None
    for mode in ("all", "one", "off"):
        _, issued, ack = ma_alexa.s.call_timed(
            "player_queues/repeat", queue_id=target.queue_id, repeat_mode=mode)
        event_ms = ma_alexa.s.wait_for_event(issued, target.queue_id, {"queue_updated"}, 4.0)
        landed, effect_ms = observe(
            ma_alexa.s,
            lambda m=mode: ma_alexa.s.queue(target.queue_id).get("repeat_mode") == m,
            floor=0.0, budget=6.0, issued=issued)
        seen[mode] = ma_alexa.s.queue(target.queue_id).get("repeat_mode")
        del landed

    ok = all(seen[m] == m for m in ("all", "one", "off"))
    record("repeat", target, cell.source, ok=ok, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=0.0, budget=6.0,
           detail=", ".join(f"{k}->{v}" for k, v in seen.items()))
    assert ok, f"repeat modes did not stick: {seen}"


# --- enqueue -----------------------------------------------------------------


@cells("enqueue")
def test_enqueue(ma_alexa, cell):
    """`option=add` must append without disturbing what is playing.

    The failure this is really looking for is the one Music Assistant's no-op
    `enqueue_next_media` exists to prevent: MA re-issuing `play_media` per track
    would restart the queue at every boundary, and from the outside that looks
    like enqueue "working" while the music jumps back to the start.
    """
    _gate(cell)
    target = ma_alexa.target(cell.target)
    tracks = _tracks_for(cell.source, 2)
    media, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    assert ma_alexa.settle(target), "precondition: playback never settled"

    # Radio has exactly one station in this environment, so the enqueued item is
    # the same station again. The question - does `add` append rather than
    # replace - is unchanged by that.
    extra = ma_alexa.media(cell.source, tracks)[-1]
    before = ma_alexa.s.queue(target.queue_id)
    index_before = before.get("current_index")
    uri_before = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("uri")

    _, issued, ack = ma_alexa.s.call_timed(
        "player_queues/play_media", queue_id=target.queue_id, media=[extra["uri"]],
        option="add", radio_mode=False)
    event_ms = ma_alexa.s.wait_for_event(
        issued, target.queue_id, {"queue_items_updated", "queue_updated"}, 6.0)

    ok, effect_ms = observe(
        ma_alexa.s, lambda: ma_alexa.s.queue(target.queue_id).get("items") == 2,
        floor=0.0, budget=10.0, issued=issued)

    # "Undisturbed" is observed over a window, not sampled once. Alexa reports a
    # brief `paused` while it buffers, so a single read taken at an unlucky
    # instant would report a disturbance that a listener never heard. What must
    # not happen is the index or the current track moving, and those are read at
    # the end, once playback has come back.
    def still_playing_the_same_thing() -> bool:
        queue = ma_alexa.s.queue(target.queue_id)
        uri_now = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("uri")
        return (queue.get("state") == "playing"
                and queue.get("current_index") == index_before
                and uri_now == uri_before)

    undisturbed, _ = observe(ma_alexa.s, still_playing_the_same_thing,
                             floor=RESYNC_SECONDS, budget=20.0, issued=time.monotonic())
    after = ma_alexa.s.queue(target.queue_id)
    items = ma_alexa.s.queue_items(target.queue_id)
    uri_after = (ma_alexa.s.player(target.player_id).get("current_media") or {}).get("uri")

    record("enqueue", target, cell.source, ok=ok and undisturbed, ack_ms=ack,
           event_ms=event_ms, effect_ms=effect_ms, floor=0.0, budget=10.0,
           detail=f"items {before.get('items')} -> {after.get('items')}, "
                  f"index {index_before} -> {after.get('current_index')}, "
                  f"state={after.get('state')}")
    assert ok, f"queue did not grow to two items: {[i['name'] for i in items]}"
    assert undisturbed, (
        f"enqueue disturbed playback: index {index_before} -> "
        f"{after.get('current_index')}, state {after.get('state')}, "
        f"playing {uri_after} (was {uri_before})")


# --- volume ------------------------------------------------------------------


@cells("volume")
def test_volume(ma_alexa, cell):
    """Volume is quantised, so this asserts a tolerance and not equality.

    An Echo Studio asked for 18 reports 17. Music Assistant's own confirm loop uses
    +/-2 for the same reason, and a suite demanding equality would be reporting
    the speaker's rounding as a defect.

    A group is read on a different field, because Music Assistant means
    something different by a group's volume. `_handle_cmd_volume_set` sends a
    `PlayerType.GROUP` to `set_group_volume`, which writes only to the members;
    and `Player.group_volume` is the maximum over those members, taken with
    `exclude_self=True`. So no MA code path writes a group's own
    `volume_level`, and none reads it. Reading it here asserted on a field the
    command by design cannot move, and failed on every run for that reason
    rather than because a speaker did not change volume.
    """
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"

    # The field MA itself shows for this player, which is what the command
    # moves in both cases: its own level for a speaker, the members' maximum
    # for a group.
    field = "group_volume" if cell.target == "group" else "volume_level"

    wanted = 20
    assert wanted <= MAX_VOLUME
    _, issued, ack = ma_alexa.s.call_timed(
        "players/cmd/volume_set", player_id=target.player_id, volume_level=wanted)
    event_ms = ma_alexa.s.wait_for_event(issued, target.player_id, {"player_updated"}, 4.0)

    def landed() -> bool:
        level = ma_alexa.s.player(target.player_id).get(field)
        return level is not None and abs(int(level) - wanted) <= VOLUME_TOLERANCE

    ok, effect_ms = observe(ma_alexa.s, landed, floor=VOLUME_QUEUE_DELAY,
                            budget=VOLUME_BUDGET, issued=issued)
    reported = ma_alexa.s.player(target.player_id).get(field)
    record("volume", target, cell.source, ok=ok, ack_ms=ack, event_ms=event_ms,
           effect_ms=effect_ms, floor=VOLUME_QUEUE_DELAY, budget=VOLUME_BUDGET,
           asked=wanted, reported=reported,
           detail=f"asked {wanted}, {field} reports {reported}")
    assert ok, f"asked for {wanted}, {field} reports {reported} after the confirm loop"


@cells("group_volume")
def test_group_volume(ma_alexa, cell):
    """Group volume interpolates. It is not a broadcast.

    `set_group_volume` snapshots every powered child, takes the loudest as the
    base, and moves each child toward 100 or toward 0 in proportion - so setting
    the group to 20 sets no member to 20 unless they were all equal to begin
    with. The reported group volume is the *max* of the children. A test that
    asserted every member equals the requested level would be asserting the
    opposite of the design.
    """
    _gate(cell)
    group = ma_alexa.target(cell.target)
    members = ma_alexa.members(group)
    assert len(members) >= 2, "a group volume case needs more than one member"

    # A deliberately uneven starting point, so "interpolated" and "broadcast"
    # produce different answers and the assertion can tell them apart. Every
    # value is under the cap.
    start = {}
    for offset, member in enumerate(members):
        level = 8 + offset * 3
        ma_alexa.set_volume(member, level, wait=False)
        start[member.player_id] = level

    # Wait for the uneven starting point to actually exist, rather than for a
    # duration chosen to be longer than it usually takes. The interpolation
    # under test reads these levels, so a run that proceeds early is not slow,
    # it is measuring a different starting point than the one it recorded.
    def seeded() -> bool:
        return all(
            (level := ma_alexa.s.player(m.player_id).get("volume_level")) is not None
            and abs(int(level) - start[m.player_id]) <= VOLUME_TOLERANCE
            for m in members
        )

    observe(ma_alexa.s, seeded, floor=VOLUME_QUEUE_DELAY,
            budget=VOLUME_QUEUE_DELAY + 6.0, issued=time.monotonic())

    wanted = 20
    assert wanted <= MAX_VOLUME
    _, issued, ack = ma_alexa.s.call_timed(
        "players/cmd/group_volume", player_id=group.player_id, volume_level=wanted)
    event_ms = ma_alexa.s.wait_for_event(issued, group.player_id, {"player_updated"}, 4.0)

    def landed() -> bool:
        level = ma_alexa.s.player(group.player_id).get("group_volume")
        return level is not None and abs(int(level) - wanted) <= VOLUME_TOLERANCE

    ok, effect_ms = observe(ma_alexa.s, landed, floor=VOLUME_QUEUE_DELAY,
                            budget=GROUP_VOLUME_BUDGET, issued=issued)

    reported = ma_alexa.s.player(group.player_id).get("group_volume")
    finals = {m.player_id: ma_alexa.s.player(m.player_id).get("volume_level") for m in members}
    interpolated = not all(
        v is not None and abs(int(v) - wanted) <= VOLUME_TOLERANCE for v in finals.values())
    ordering_kept = [finals[m.player_id] for m in members] == sorted(
        [finals[m.player_id] for m in members])
    within_cap = all(v is None or int(v) <= MAX_VOLUME for v in finals.values())

    record("group_volume", group, cell.source, ok=ok and interpolated and within_cap,
           ack_ms=ack, event_ms=event_ms, effect_ms=effect_ms, floor=VOLUME_QUEUE_DELAY,
           budget=GROUP_VOLUME_BUDGET, asked=wanted, reported=reported,
           members_before=start, members_after=finals, ordering_kept=ordering_kept,
           detail=f"asked {wanted}, group reports {reported}; members "
                  f"{list(start.values())} -> {list(finals.values())}")
    assert within_cap, f"a member went above the {MAX_VOLUME} cap: {finals}"
    assert ok, (f"group volume asked for {wanted}, reports {reported} after "
                f"{GROUP_VOLUME_BUDGET}s; members {finals}")
    assert interpolated, (
        f"every member landed on {wanted}: this was a broadcast, not the "
        f"documented interpolation from the snapshot {start}")


# --- mute: declared nowhere, and therefore silent ----------------------------


@cells("mute")
def test_mute_is_a_documented_no_op(ma_alexa, cell):
    """Music Assistant does not declare `VOLUME_MUTE`, so mute does nothing, quietly.

    `mute_control` resolves to `"none"`, `cmd_volume_mute` matches no branch and
    falls off the end of the function. Both obvious assertions are wrong here:
    that mute works, and that mute raises. The contract is that it returns
    without error and changes nothing, and that is what is checked - so that if
    it ever starts half-working, this notices.
    """
    _gate(cell)
    target = ma_alexa.target(cell.target)
    _, played = ma_alexa.arrange_playing(target, cell.source, tracks=1)
    assert played.ok, f"precondition: {played.detail}"

    before = ma_alexa.s.player(target.player_id)
    assert before.get("mute_control") == "none", \
        f"mute_control is {before.get('mute_control')!r}; this cell's premise has changed"

    issued = time.monotonic()
    raised: MAError | None = None
    try:
        ma_alexa.s.call("players/cmd/volume_mute", player_id=target.player_id, muted=True)
    except MAError as exc:
        raised = exc
    ack = (time.monotonic() - issued) * 1000.0

    time.sleep(RESYNC_SECONDS + 1.5)
    after = ma_alexa.s.player(target.player_id)
    unchanged = (after.get("volume_muted") == before.get("volume_muted")
                 and after.get("volume_level") == before.get("volume_level"))

    record("mute", target, cell.source, ok=raised is None and unchanged, ack_ms=ack,
           floor=RESYNC_SECONDS, budget=6.0,
           error_code=raised.code if raised else None,
           detail=f"unsupported; muted {before.get('volume_muted')} -> "
                  f"{after.get('volume_muted')}, volume {before.get('volume_level')} -> "
                  f"{after.get('volume_level')}")
    assert raised is None, f"mute raised {raised}; it is documented as silent"
    assert unchanged, (
        f"mute is not implemented but something changed: muted "
        f"{before.get('volume_muted')} -> {after.get('volume_muted')}, volume "
        f"{before.get('volume_level')} -> {after.get('volume_level')}")


# --- behaviours that are not cells of the grid -------------------------------
#
# Documented properties of the API that a suite asserting only on the grid would
# stop noticing. Each is run once; none of them vary by source or by target.


def test_an_unrecognised_repeat_mode_is_coerced_not_refused(ma_alexa):
    """Every MA enum defines `_missing_`. Nothing validates by rejecting.

    Worth pinning because it is the trap that makes a negative test lie: a case
    written as "assert this bad value is refused" passes for the wrong reason on
    an API that never refuses anything.
    """
    target = ma_alexa.target("single")
    ma_alexa.arrange_playing(target, "streaming", tracks=1)

    ma_alexa.s.call("player_queues/repeat", queue_id=target.queue_id,
                  repeat_mode="backwards-and-sideways")
    time.sleep(1.0)
    stored = ma_alexa.s.queue(target.queue_id).get("repeat_mode")

    record("enum-coercion", target, "streaming", ok=stored == "unknown",
           detail=f"repeat_mode=backwards-and-sideways stored as {stored!r}")
    assert stored == "unknown", \
        f"expected coercion to 'unknown', got {stored!r} - MA may have started validating"


def test_resume_on_an_empty_queue_does_not_silently_do_nothing(ma_alexa):
    """`clear` does not make a queue un-resumable, and that is worth knowing.

    `COMMANDS.md` records `QueueEmpty` (8) here. On this instance MA 2.9.9 falls
    through to `_try_resume_from_playlog` when the queue has no current item, so
    an emptied queue can be *repopulated* by a resume rather than refusing it.

    Asserted as "one of those two things happened", because the outcome that
    must never be allowed to pass unnoticed is the third one: returning success
    and doing nothing at all. Anything treating `clear` as a way to leave a
    speaker in a known state is relying on a refusal that does not happen.
    """
    target = ma_alexa.target("single")
    ma_alexa.quiesce(target)
    time.sleep(1.0)
    assert ma_alexa.s.queue(target.queue_id).get("items") == 0, "precondition: not empty"

    raised: MAError | None = None
    try:
        ma_alexa.s.call("player_queues/resume", queue_id=target.queue_id)
    except MAError as exc:
        raised = exc

    time.sleep(PLAY_CONFIRM_SECONDS)
    after = ma_alexa.s.queue(target.queue_id)
    refused = raised is not None and raised.code == matrix.QUEUE_EMPTY
    repopulated = bool(after.get("items"))

    record("resume-empty", target, "n/a", ok=refused or repopulated,
           error_code=raised.code if raised else None,
           detail=("refused with QueueEmpty" if refused else
                   f"returned success and the queue now holds {after.get('items')} "
                   f"item(s), state={after.get('state')}"))
    assert refused or repopulated, (
        f"resume on an empty queue returned success and left it empty: a silent "
        f"no-op. raised={raised}, items={after.get('items')}, "
        f"state={after.get('state')}")


def test_grouping_commands_are_refused(ma_alexa):
    """Alexa owns its groups. MA must not think it can assemble one.

    Refusal is the contract; the code is not. `COMMANDS.md` predicted
    `UnsupportedFeature` (9) from the undeclared `SET_MEMBERS`, and this
    instance answers `PlayerCommandFailed` (11) - "Player Kitchen Echo does not
    support group commands" - because MA checks `can_group_with` before it
    checks the feature set. Both are refusals. Pinning the exact code here would
    turn a documentation slip into a red suite while a command that was actually
    *accepted* is the only outcome that matters.
    """
    target = ma_alexa.target("single")
    other = next(m for m in ma_alexa.members(ma_alexa.target("group"))
                 if m.player_id != target.player_id)

    raised: MAError | None = None
    try:
        ma_alexa.s.call("players/cmd/group", player_id=safety.check(target.player_id),
                      target_player=safety.check(other.player_id))
    except MAError as exc:
        raised = exc

    refusals = {matrix.UNSUPPORTED_FEATURE, matrix.PLAYER_COMMAND_FAILED}
    ok = raised is not None and raised.code in refusals
    record("grouping-refused", target, "n/a", ok=ok,
           error_code=raised.code if raised else None,
           detail=(f"refused with {raised.code}: {raised.details}" if raised
                   else "returned success"))
    assert raised is not None, (
        "players/cmd/group returned success. Music Assistant declares no SET_MEMBERS and "
        "an accepted grouping command would mean MA believes it can restructure "
        "Amazon's own speaker groups.")
    assert raised.code in refusals, \
        f"expected a refusal ({sorted(refusals)}), got {raised.code}: {raised.details}"


def test_a_seek_into_the_last_seconds_is_clamped_to_the_start(ma_alexa):
    """`_seek_offset_ms` clamps an offset within 3s of the end back to zero.

    By design: an offset at the very end starts a track that finishes instantly,
    which is what a listener experiences as the player going quiet and then
    pausing with no position. Asserted so the clamp is not removed by accident.
    """
    target = ma_alexa.target("single")
    media, played = ma_alexa.arrange_playing(target, "streaming", tracks=1)
    assert played.ok, f"precondition: {played.detail}"
    duration = int(media[0]["duration"])
    assert duration > 30

    issued = time.monotonic()
    ma_alexa.s.call("player_queues/seek", queue_id=target.queue_id, position=duration - 1)

    def restarted() -> bool:
        elapsed = ma_alexa.s.player(target.player_id).get("elapsed_time")
        return elapsed is not None and float(elapsed) < 30.0

    ok, effect_ms = observe(ma_alexa.s, restarted, floor=PLAY_CONFIRM_SECONDS,
                            budget=SEEK_BUDGET, issued=issued)
    player_elapsed = ma_alexa.s.player(target.player_id).get("elapsed_time")
    record("seek-end-clamp", target, "streaming", ok=ok, effect_ms=effect_ms,
           floor=PLAY_CONFIRM_SECONDS, budget=SEEK_BUDGET,
           detail=f"sought to {duration - 1}/{duration}; Alexa reports {player_elapsed}")
    assert ok, (f"a seek to {duration - 1}s of a {duration}s track should be "
                f"clamped to the start; Alexa reports {player_elapsed}")
