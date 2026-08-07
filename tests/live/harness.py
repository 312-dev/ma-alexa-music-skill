"""Drive a real Music Assistant, watch what actually happens, and time it.

Three things make a live suite against Music Assistant different from an ordinary
integration test, and this module exists to encode all three in one place so no
individual test has to remember them.

**Success is not evidence.** `player_queues/play_media` logs and skips a media
item it cannot resolve and then returns success, and `handle_player_command`
swallows any command aimed at an unavailable player and also returns success.
So `call()` here never doubles as an assertion. Every action is followed by
`observe()`, which reads state back and decides whether the thing asked for
actually happened.

**State is optimistic.** Music Assistant writes the state it expects and calls
`update_state()` before Alexa has done anything, then re-polls 1.5s later. A
read taken immediately after a command confirms Music Assistant's optimism, not the
speaker. `observe()` therefore refuses to look before a floor, one per action
class, taken from the provider's own constants. A suite that asserts sooner is
measuring its own impatience and will pass on a provider that never reached the
speaker at all.

**The devices are real.** Every target is obtained through `tests.live.safety`,
which fails closed on the Amazon serial. There is no other way to get a player
id into a command from here: `authorise()` is the only constructor of a
`Target`, and it refuses anything the user has not cleared - including a group
that contains one device they have not.

Connection model: one websocket for the whole session, held open by a private
event loop on a background thread, so that the event stream is running *before*
any command is issued (MA subscribes you to everything at auth time and there
is no way to ask for events retroactively). Tests stay synchronous.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import aiohttp

from tests.live import safety

HERE = Path(__file__).resolve().parent
# environment.json describes one specific live deployment (its players, groups
# and ids) and is per-user, so it is gitignored rather than committed. See
# environment.example.json for the shape. Absent, the live suite still imports;
# its tests skip for want of a live target rather than failing to collect.
_ENV_FILE = HERE / "environment.json"
ENVIRONMENT: dict[str, Any] = (
    json.loads(_ENV_FILE.read_text()) if _ENV_FILE.exists() else {}
)

# --- what Music Assistant's own constants say a test is allowed to expect -------------
#
# Copied from ma_provider/provider.py rather than imported, deliberately: the
# suite is a check on the deployed provider, and importing the constants from
# the code under test would make a suite that agrees with whatever the code
# currently says. If these drift from provider.py the drift is a finding.

RESYNC_SECONDS = 1.5          # Music Assistant re-polls Alexa this long after a control
PLAY_CONFIRM_SECONDS = 4.0    # play_media is only confirmed when Alexa fetches
VOLUME_QUEUE_DELAY = 1.5      # alexapy batches volume writes; the hard floor
VOLUME_CONFIRM_SECONDS = 2.0
VOLUME_ATTEMPTS = 3
VOLUME_RETRY_SPREAD = 2.0
VOLUME_TOLERANCE = 2          # an Echo Studio asked for 18 reports 17
END_OF_TRACK_MS = 3000        # a seek this close to the end is clamped to 0
NEXT_PREV_DEBOUNCE = 1.0      # queue index moves now, audio moves ~1s later
PREVIOUS_RESTART_ELAPSED = 5.0  # previous restarts rather than steps back
PAUSE_WATCHDOG_SECONDS = 30.0   # a queue left paused this long is stopped

# Worst case for a volume write that has to be retried: the batching delay plus
# every confirm window plus every retry jitter.
VOLUME_BUDGET = (
    VOLUME_QUEUE_DELAY
    + VOLUME_ATTEMPTS * VOLUME_CONFIRM_SECONDS
    + (VOLUME_ATTEMPTS - 1) * VOLUME_RETRY_SPREAD
)  # ~11.5s

# Never exceeded on any speaker, at any point, for any reason.
MAX_VOLUME = 25
BASELINE_VOLUME = 15

def _default_ws_url() -> str:
    # From environment.json when present, else empty. A live run supplies it
    # (via the file or MA_WS_URL); the default offline run only imports this
    # module and never reads the value, so an absent environment.json must not
    # break collection.
    url = (ENVIRONMENT.get("extra") or {}).get("ma_tailnet_url", "")
    return url.replace("http://", "ws://") + "/ws" if url else ""


DEFAULT_WS_URL = os.environ.get("MA_WS_URL", _default_ws_url())
BOX = os.environ.get("MA_ALEXA_BOX", "my-box")


class MAError(Exception):
    """An `ErrorResultMessage` off the websocket, with its code intact.

    The HTTP transport loses the error class (every handler exception becomes a
    bare 500), which is why this suite is on the websocket: several cells assert
    on *which* error MA raised, not merely that it raised.
    """

    def __init__(self, code: int, details: str, command: str) -> None:
        super().__init__(f"{command}: error {code}: {details}")
        self.code = code
        self.details = details
        self.command = command


class Timeout(Exception):
    """An observation that never became true inside its budget."""


# --- targets -----------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A player this run is permitted to drive.

    Only `authorise()` builds one. Holding a `Target` is the proof that
    `safety.check` has already refused to hand back anything else.
    """

    kind: str            # "single" | "group"
    player_id: str
    name: str
    members: tuple[str, ...] = ()

    @property
    def queue_id(self) -> str:
        # PlayerQueues._get_queue does `queue_id = player.player_id`.
        return self.player_id


def authorise(player: dict[str, Any]) -> Target:
    """Turn a live `Player` snapshot into a target, or refuse to.

    A single speaker is cleared by `safety.check` on its own serial. A group has
    no serial of its own on the allow list and must not get one: it is cleared
    only if *every* live member is, because telling "Whole Apartment" to play
    reaches all four rooms. Both paths raise `safety.Unsafe` rather than
    returning None, so a caller that forgets to look at the result still cannot
    drive an excluded device.
    """
    player_id = player["player_id"]
    members = tuple(player.get("group_members") or ())

    if player.get("type") == "group":
        if not members:
            raise safety.Unsafe(f"refusing to drive {player_id}: group reports no members")
        # Each member individually, so the refusal names the offending device...
        for member in members:
            safety.check(member)
        # ...and then the group rule, which is the one that would have caught a
        # member that appeared between the two reads.
        ok, blockers = safety.group_is_safe(list(members))
        if not ok:
            raise safety.Unsafe(f"refusing to drive group {player_id}: contains {blockers}")
        return Target("group", player_id, player.get("display_name") or player_id, members)

    safety.check(player_id)
    return Target("single", player_id, player.get("display_name") or player_id)


# --- measurement -------------------------------------------------------------


@dataclass
class Observation:
    """One action and everything measurable about how it landed.

    Three clocks, because they answer three different questions:

    - `ack_ms`   how long MA took to acknowledge the command. Round trip only.
    - `event_ms` when MA published a changed snapshot. For an Music Assistant control
      this is the *optimistic* write, before Alexa has been asked anything, so
      it is a measure of the server and not of the speaker.
    - `effect_ms` when the outcome was first observed to be true, never read
      before `floor_s`. This is the only one that says the speaker did it.
    """

    action: str
    source: str
    target: str
    player: str
    ok: bool
    detail: str = ""
    ack_ms: float | None = None
    event_ms: float | None = None
    effect_ms: float | None = None
    floor_s: float = 0.0
    budget_s: float = 0.0
    over_budget: bool = False
    error_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Recorder:
    """Every observation the run made, and the latency summary over them."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self.notes: list[str] = []
        # What the run spent making Amazon obey, across every cell. Totals
        # rather than per-cell rows because the interesting number is how much
        # of this a whole run contains: a single resend is ordinary, and
        # hundreds of them are a finding about Amazon that no individual cell
        # is in a position to make.
        self.resends = 0
        self.lost: list[str] = []

    def add(self, obs: Observation) -> Observation:
        self.observations.append(obs)
        return obs

    def note(self, text: str) -> None:
        self.notes.append(text)

    def note_convergence(self, *, resends: int, lost: list[str]) -> None:
        self.resends += resends
        self.lost.extend(lost)

    def latency_table(self) -> dict[str, dict[str, Any]]:
        """p50 and max per action type, for each of the three clocks."""
        table: dict[str, dict[str, Any]] = {}
        for obs in self.observations:
            row = table.setdefault(
                obs.action,
                {"n": 0, "ack_ms": [], "event_ms": [], "effect_ms": [],
                 "budget_s": obs.budget_s, "over_budget": 0},
            )
            row["n"] += 1
            for key in ("ack_ms", "event_ms", "effect_ms"):
                value = getattr(obs, key)
                if value is not None:
                    row[key].append(value)
            if obs.over_budget:
                row["over_budget"] += 1
        summary: dict[str, dict[str, Any]] = {}
        for action, row in sorted(table.items()):
            entry: dict[str, Any] = {"n": row["n"], "budget_s": row["budget_s"],
                                     "over_budget": row["over_budget"]}
            for key in ("ack_ms", "event_ms", "effect_ms"):
                samples = row[key]
                entry[f"{key}_p50"] = round(statistics.median(samples), 1) if samples else None
                entry[f"{key}_max"] = round(max(samples), 1) if samples else None
            summary[action] = entry
        return summary


RECORDER = Recorder()


# --- the client --------------------------------------------------------------


def _read_token() -> str:
    """The staged MA token, without it passing through a log or a file here.

    Read over ssh into memory. `tools/ma_token.sh` goes to some trouble never to
    print it - a command line is readable by anything that can see /proc, and by
    whatever is reading the session transcript - so it is not written into
    `results/`, not put in an argv, and not included in any report.
    """
    if token := os.environ.get("MA_TOKEN"):
        return token.strip()
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", BOX, "cat /opt/ma_alexa/.ma-token"],
        capture_output=True, text=True, check=False,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError(
            "no Music Assistant token: set MA_TOKEN, or run tools/ma_token.sh "
            f"so that {BOX}:/opt/ma_alexa/.ma-token exists"
        )
    return token


class LiveSession:
    """One websocket, one background event loop, a synchronous face.

    The loop lives on its own thread for a reason that is easy to get wrong:
    MA wires the socket to `mass.subscribe()` the moment `auth` succeeds and has
    no subscribe command, so a harness that connects per test can never see the
    event that its own command caused - the connection would be younger than the
    command. One long-lived connection, reading continuously from before the
    first action, is the only shape that can time anything.
    """

    def __init__(self, ws_url: str = DEFAULT_WS_URL) -> None:
        self.ws_url = ws_url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ma-live-session")
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._events: deque[tuple[float, str, str | None, Any]] = deque(maxlen=20000)
        self._events_lock = threading.Lock()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self.server_version = ""
        self.schema_version: int | None = None

    # -- lifecycle ------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._submit(self._connect(), timeout=45)

    def close(self) -> None:
        try:
            self._submit(self._disconnect(), timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _submit(self, coro, timeout: float = 90):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.ws_url, heartbeat=30)
        info = await asyncio.wait_for(self._ws.receive_json(), 20)
        self.server_version = str(info.get("server_version"))
        self.schema_version = info.get("schema_version")
        self._loop.create_task(self._reader())
        await self._send("auth", {"token": _read_token()})

    async def _disconnect(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                if (message_id := frame.get("message_id")) is not None:
                    # Responses may arrive interleaved with events and with each
                    # other, so they are matched on message_id and never on
                    # arrival order.
                    future = self._pending.pop(str(message_id), None)
                    if future is not None and not future.done():
                        future.set_result(frame)
                    continue
                if (event := frame.get("event")) is not None:
                    with self._events_lock:
                        self._events.append(
                            (time.monotonic(), event, frame.get("object_id"), frame.get("data"))
                        )
        except Exception:  # pragma: no cover - socket teardown
            pass

    # -- commands -------------------------------------------------------------

    async def _send(self, command: str, args: dict[str, Any]) -> Any:
        assert self._ws is not None
        with self._id_lock:
            message_id = str(self._next_id)
            self._next_id += 1
        future: asyncio.Future = self._loop.create_future()
        self._pending[message_id] = future
        await self._ws.send_json({"message_id": message_id, "command": command, "args": args})
        frame = await asyncio.wait_for(future, timeout=90)
        if "error_code" in frame:
            raise MAError(frame["error_code"], str(frame.get("details")), command)
        return frame.get("result")

    def call(self, command: str, **args: Any) -> Any:
        """Issue a command. Returns MA's result; raises `MAError` on an error.

        Not an assertion. A successful return means MA accepted the frame, which
        for `play_media` and for any command to an unavailable player is
        compatible with nothing at all having happened.
        """
        return self._submit(self._send(command, args))

    def call_timed(self, command: str, **args: Any) -> tuple[Any, float, float]:
        """`call`, plus the monotonic issue time and the acknowledgement latency."""
        issued = time.monotonic()
        result = self._submit(self._send(command, args))
        return result, issued, (time.monotonic() - issued) * 1000.0

    # -- events ---------------------------------------------------------------

    def first_event_after(
        self, since: float, object_id: str, types: Iterable[str],
    ) -> float | None:
        """Milliseconds from `since` to the first matching event, if any."""
        wanted = set(types)
        with self._events_lock:
            snapshot = list(self._events)
        for stamp, event, obj, _data in snapshot:
            if stamp >= since and event in wanted and obj == object_id:
                return (stamp - since) * 1000.0
        return None

    def wait_for_event(
        self, since: float, object_id: str, types: Iterable[str], timeout: float,
    ) -> float | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.first_event_after(since, object_id, types)
            if found is not None:
                return found
            time.sleep(0.05)
        return None

    def events_between(
        self, start: float, end: float, object_id: str | None = None,
    ) -> list[tuple[float, str, str | None, Any]]:
        with self._events_lock:
            snapshot = list(self._events)
        return [e for e in snapshot
                if start <= e[0] <= end and (object_id is None or e[2] == object_id)]

    # -- reads ----------------------------------------------------------------

    def players(self) -> list[dict[str, Any]]:
        return self.call("players/all", return_unavailable=True, return_disabled=False)

    def player(self, player_id: str) -> dict[str, Any]:
        state = self.call("players/get", player_id=player_id, raise_unavailable=False)
        if state is None:
            raise Timeout(f"players/get returned null for {player_id}")
        return state

    def convergence(self, player_id: str) -> tuple[int, int]:
        """What it has cost this player, in total, to make commands stick.

        `(resends, gave_up)`, both monotonic since the provider loaded. The
        numbers come from Music Assistant's own confirm loops via `extra_data`, which
        the players payload already carries.

        This exists because the suite could not otherwise see a whole class of
        failure. Music Assistant answers every control optimistically - it writes the
        state that was asked for, and Music Assistant shows it at once - so a
        command Amazon dropped, resent twice and still lost is indistinguishable
        from one that worked, in every field an assertion here reads. One run
        had 25 transport resends and 5 outright give-ups on the speaker group
        and reported six green stop cells. That was found by reading the
        provider's logs, which is not a mechanism anyone can rely on twice.
        """
        state = self.player(player_id)
        # `extra_data` is MA's own outgoing alias for `extra_attributes`. Both
        # are read so this keeps working whichever name a future MA drops.
        attrs = state.get("extra_attributes") or state.get("extra_data") or {}
        return int(attrs.get("ma_alexa_resends", 0)), int(attrs.get("ma_alexa_gave_up", 0))

    def queue(self, queue_id: str) -> dict[str, Any]:
        state = self.call("player_queues/get", queue_id=queue_id)
        if state is None:
            raise Timeout(f"player_queues/get returned null for {queue_id}")
        return state

    def queue_items(self, queue_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.call("player_queues/items", queue_id=queue_id, limit=limit, offset=0)

    def elapsed(self, queue_id: str) -> float:
        """`PlayerQueue.corrected_elapsed_time`, computed here.

        `elapsed_time` on the wire is a snapshot taken at
        `elapsed_time_last_updated`, and it only advances while the queue is
        playing. Comparing two raw reads of it measures nothing.
        """
        queue = self.queue(queue_id)
        elapsed = float(queue.get("elapsed_time") or 0.0)
        if queue.get("state") != "playing":
            return elapsed
        last = float(queue.get("elapsed_time_last_updated") or 0.0)
        speed = float(queue.get("playback_speed") or 1.0)
        if last <= 0:
            return elapsed
        return elapsed + max(0.0, time.time() - last) * speed

    def raw_elapsed(self, queue_id: str) -> float:
        """`queue.elapsed_time` as stored, with no extrapolation.

        The number Music Assistant itself branches on. `previous` compares this
        against 5 seconds to decide between stepping back and restarting the
        current track, so a test predicting which it will do has to read the
        same value rather than the corrected one - they differ by however long
        it has been since the player last published, and that gap is exactly
        wide enough to make the prediction wrong occasionally.
        """
        return float(self.queue(queue_id).get("elapsed_time") or 0.0)

    def player_elapsed(self, player_id: str) -> tuple[float, float]:
        """What the *speaker* says its position is: (raw snapshot, corrected).

        `player.elapsed_time` is Music Assistant's record of what it last read off Alexa,
        so it is the only number in the system that came from the device. The
        queue's `elapsed_time` is derived from it, and the two are returned
        separately by the seek cases because when they disagree, which one is
        wrong is the entire finding.
        """
        state = self.player(player_id)
        raw = float(state.get("elapsed_time") or 0.0)
        if state.get("state") != "playing":
            return raw, raw
        last = float(state.get("elapsed_time_last_updated") or 0.0)
        if last <= 0:
            return raw, raw
        return raw, raw + max(0.0, time.time() - last)

    def resolve(self, uri: str) -> dict[str, Any]:
        """A media item that MA says is playable, or an exception.

        Called before anything is queued, because `play_media` skips an item it
        cannot resolve *and returns success*: without this, a cell whose media
        had gone stale would queue nothing and report a pass.
        """
        item = self.call("music/item_by_uri", uri=uri)
        if not item:
            raise Timeout(f"{uri} did not resolve")
        if item.get("is_playable") is False:
            raise Timeout(f"{uri} resolved but is_playable is false")
        # `is_playable` is not sufficient, measured 2026-08-03: a Deezer track
        # came back with `is_playable: true` and its only provider mapping
        # marked `available: false`. `play_media` accepted it, skipped it, and
        # returned success, so a four-track queue quietly held three - which is
        # precisely the failure this whole method exists to prevent, arriving
        # through the check meant to prevent it.
        mappings = item.get("provider_mappings") or []
        if mappings and not any(m.get("available") for m in mappings):
            raise Timeout(
                f"{uri} resolved and claims is_playable, but no provider mapping "
                f"is available: {[(m.get('provider_instance'), m.get('available')) for m in mappings]}"
            )
        return item


# --- observing an outcome ----------------------------------------------------


def observe(
    session: LiveSession,
    predicate: Callable[[], bool],
    *,
    floor: float,
    budget: float,
    issued: float,
    poll: float = 0.4,
) -> tuple[bool, float | None]:
    """Wait out the floor, then look until the outcome holds or the budget ends.

    The floor is not a convenience sleep. Music Assistant answers controls optimistically:
    it writes the state it expects and publishes it before Alexa has been asked
    anything, and only re-polls `RESYNC_SECONDS` later. A read taken before the
    floor cannot distinguish a speaker that obeyed from a provider that merely
    said so, so this refuses to take one.
    """
    remaining = floor - (time.monotonic() - issued)
    if remaining > 0:
        time.sleep(remaining)
    deadline = issued + budget
    while True:
        if predicate():
            return True, (time.monotonic() - issued) * 1000.0
        if time.monotonic() >= deadline:
            return False, None
        time.sleep(poll)


def record(
    action: str,
    target: Target,
    source: str,
    *,
    ok: bool,
    detail: str = "",
    ack_ms: float | None = None,
    event_ms: float | None = None,
    effect_ms: float | None = None,
    floor: float = 0.0,
    budget: float = 0.0,
    error_code: int | None = None,
    **extra: Any,
) -> Observation:
    """File one row of the report. Called on failures too, not only passes."""
    return RECORDER.add(Observation(
        action=action, source=source, target=target.kind, player=target.name,
        ok=ok, detail=detail, ack_ms=ack_ms, event_ms=event_ms, effect_ms=effect_ms,
        floor_s=floor, budget_s=budget,
        over_budget=bool(effect_ms is not None and budget and effect_ms / 1000.0 > budget),
        error_code=error_code, extra=extra,
    ))


def _names_in_order(queued: list[str], asked: list[str]) -> bool:
    """Whether the queue holds the asked-for items in the asked-for order.

    Matched by containment because a queue item is named "Artist - Title" while
    the media item it came from is named "Title"; the two sides share no id
    that survives resolution through the library.
    """
    if len(queued) != len(asked):
        return False
    return all(want.lower() in got.lower() or got.lower() in want.lower()
               for got, want in zip(queued, asked))


def names_match(queued: list[str], asked: list[str]) -> bool:
    """Same items, order disregarded."""
    if len(queued) != len(asked):
        return False
    remaining = list(queued)
    for want in asked:
        found = next((g for g in remaining
                      if want.lower() in g.lower() or g.lower() in want.lower()), None)
        if found is None:
            return False
        remaining.remove(found)
    return True


# --- the apparatus a test actually holds -------------------------------------


class MaAlexa:
    """Everything a conformance case needs, with the traps already handled."""

    def __init__(self, session: LiveSession) -> None:
        self.s = session
        self._volume_snapshot: dict[str, int] = {}
        self._targets: dict[str, Target] = {}
        self._media: dict[str, list[dict[str, Any]]] = {}

    # -- discovery ------------------------------------------------------------

    def discover(self) -> dict[str, Target]:
        """Find the cleared speakers on the live server, not in a saved file.

        Player ids carry the provider instance id, which changes when the
        provider is re-added, so they are matched live on the Amazon serial -
        the part `safety` fails closed on. The group is additionally required to
        be the id recorded in `environment.json`: MA also holds a *disabled*
        `universal_group` player with the identical display name "Whole
        Apartment", and a name-based lookup is one config change away from
        picking it.
        """
        players = {p["player_id"]: p for p in self.s.players()}
        recorded_group = ENVIRONMENT["players"]["group"][0]["player_id"]
        preferred = os.environ.get("MA_ALEXA_LIVE_SPEAKER", "Living Room")

        singles: list[Target] = []
        group: Target | None = None
        for player_id, player in players.items():
            if not str(player.get("provider", "")).startswith("ma_alexa"):
                continue
            if not safety.is_allowed(player_id) and player_id != recorded_group:
                continue  # Home Theater lives here. It is enabled and available.
            try:
                target = authorise(player)
            except safety.Unsafe:
                raise
            if target.kind == "group":
                if player_id != recorded_group:
                    continue
                group = target
            else:
                singles.append(target)

        if not singles:
            raise Timeout("no cleared single speaker is present on the live server")
        if group is None:
            raise Timeout(f"the recorded group {recorded_group} is not present or not a group")

        single = next((t for t in singles if t.name == preferred), singles[0])
        self._targets = {"single": single, "group": group}
        return self._targets

    def target(self, kind: str) -> Target:
        return self._targets[kind]

    def all_targets(self) -> list[Target]:
        """Every target this run drives, in no particular order."""
        return list(self._targets.values())

    def members(self, group: Target) -> list[Target]:
        """The group's members as targets, each cleared on its own serial."""
        return [authorise(self.s.player(m)) for m in group.members]

    # -- media ----------------------------------------------------------------

    def media(self, source: str, count: int = 1) -> list[dict[str, Any]]:
        """`count` resolvable URIs for a source, verified before use.

        Verified rather than trusted because `play_media` will silently queue
        nothing for an item that has gone stale and still report success, and a
        cell that queues nothing looks from the outside exactly like a pass.
        """
        cached = self._media.get(source, [])
        if len(cached) >= count:
            return cached[:count]

        candidates: list[str] = []
        if source == "streaming":
            candidates.append(ENVIRONMENT["sources"]["streaming"]["uri"])
            candidates.append(ENVIRONMENT["extra"]["spare_streaming_track"]["uri"])
            candidates.extend(ENVIRONMENT["playlists"]["streaming"]["first_tracks"])
        elif source == "subsonic":
            candidates.append(ENVIRONMENT["sources"]["subsonic"]["uri"])
            playlist = ENVIRONMENT["playlists"]["subsonic"]
            item_id = playlist["uri"].rsplit("/", 1)[-1]
            tracks = self.s.call(
                "music/playlists/playlist_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=playlist["provider_instance"],
            ) or []
            candidates.extend(t["uri"] for t in tracks if t.get("uri"))
        elif source == "radio":
            candidates.append(ENVIRONMENT["sources"]["radio"]["uri"])
        else:  # pragma: no cover - matrix only names three
            raise ValueError(source)

        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for uri in candidates:
            if uri in seen:
                continue
            seen.add(uri)
            try:
                item = self.s.resolve(uri)
            except (MAError, Timeout):
                continue
            resolved.append({"uri": uri, "name": item.get("name", ""),
                             "duration": item.get("duration") or 0})
            if len(resolved) >= max(count, 4):
                break
        if len(resolved) < count:
            raise Timeout(f"only {len(resolved)} playable {source} items, needed {count}")
        self._media[source] = resolved
        return resolved[:count]

    # -- volume custody -------------------------------------------------------

    def snapshot_volumes(self) -> None:
        """Record every cleared speaker's level, then bring them all down.

        Both halves matter. The restore is the obvious one. The bring-down is
        because the group volume cases scale *relative* to whatever the members
        were, and a member left at 60 would be scaled up from 60.
        """
        for target in self._all_speakers():
            state = self.s.player(target.player_id)
            level = state.get("volume_level")
            if isinstance(level, int):
                self._volume_snapshot[target.player_id] = level
        for target in self._all_speakers():
            self.set_volume(target, BASELINE_VOLUME, wait=False)
        time.sleep(VOLUME_QUEUE_DELAY + 1.0)

    def restore_volumes(self) -> None:
        for player_id, level in self._volume_snapshot.items():
            try:
                self.s.call("players/cmd/volume_set", player_id=safety.check(player_id),
                            volume_level=min(level, MAX_VOLUME))
            except (MAError, safety.Unsafe):
                continue
        time.sleep(VOLUME_QUEUE_DELAY + 1.0)

    def _all_speakers(self) -> list[Target]:
        group = self._targets.get("group")
        return self.members(group) if group else [self._targets["single"]]

    def set_volume(self, target: Target, level: int, wait: bool = True) -> None:
        if level > MAX_VOLUME:
            raise safety.Unsafe(f"refusing to set {target.name} to {level}; cap is {MAX_VOLUME}")
        self.s.call("players/cmd/volume_set", player_id=safety.check(target.player_id),
                    volume_level=level)
        if wait:
            time.sleep(VOLUME_QUEUE_DELAY + 0.5)

    # -- playback -------------------------------------------------------------

    def quiesce(self, target: Target | None = None) -> None:
        """Leave the room quiet. Stop, then clear, on everything cleared.

        A queue that is already idle and empty is left alone, so that the
        between-tests cleanup on a skipped cell costs one read rather than four
        writes and a wait.
        """
        targets = list(self._targets.values()) if target is None else [target]
        acted = False
        for one in targets:
            try:
                queue = self.s.queue(one.queue_id)
            except (MAError, Timeout):
                queue = {"state": "unknown", "items": 1}
            if queue.get("state") == "idle" and not queue.get("items"):
                continue
            acted = True
            for command in ("player_queues/stop", "player_queues/clear"):
                try:
                    self.s.call(command, queue_id=one.queue_id)
                except MAError:
                    continue
        if acted:
            # Wait for quiet rather than for a duration. This runs after every
            # one of the 88 cells, and at a flat RESYNC_SECONDS it was 22% of
            # the whole run (265s of 1176s measured) spent sleeping past a
            # condition that is usually true within a tenth of a second:
            # Music Assistant reports IDLE the moment it is stopped, because Alexa
            # answers a stopped music-skill queue with PAUSED and there is no
            # third state to wait for anyway. The budget keeps the old
            # behaviour for the case where it genuinely has not settled.
            observe(
                self.s,
                lambda: all(
                    (q := self.s.queue(t.queue_id)).get("state") == "idle"
                    and not q.get("items")
                    for t in targets
                ),
                floor=0.0, budget=RESYNC_SECONDS, issued=time.monotonic(),
                poll=0.1,
            )

    def arrange_playing(
        self, target: Target, source: str, tracks: int = 1,
    ) -> tuple[list[dict[str, Any]], Observation]:
        """Get `target` genuinely playing `tracks` items from `source`.

        The precondition for most of the matrix, and itself the `play` cell.
        Returns the media it queued and the observation for the play, so a test
        of some other feature does not silently proceed on a queue that never
        loaded.
        """
        media = self.media(source, tracks)
        self.quiesce(target)
        # Shuffle and repeat live on the queue and survive `clear`, a restart,
        # and the end of a test run. A queue left shuffled by an earlier case -
        # or by a person using the app last week - reorders what `play_media`
        # loads and changes what `next` and `previous` mean, which shows up
        # later as an intermittent failure in a case that has nothing to do with
        # either setting. Reset explicitly rather than hoped for.
        for command, args in (
            ("player_queues/shuffle", {"shuffle_enabled": False}),
            ("player_queues/repeat", {"repeat_mode": "off"}),
        ):
            try:
                self.s.call(command, queue_id=target.queue_id, **args)
            except MAError:
                pass
        obs = self.play(target, source, media, option="replace")
        return media, obs

    def play(
        self, target: Target, source: str, media: list[dict[str, Any]], option: str,
    ) -> Observation:
        """`play_media`, then check the queue actually holds what was asked for.

        `option` is passed explicitly on every call. Omitting it does not mean
        `play`: MA falls back to a per-media-type core config key
        (`default_enqueue_option_<media_type>`), so a suite that leaves it out is
        testing whatever that instance happens to be configured to do.
        """
        uris = [m["uri"] for m in media]
        expected = len(uris) if option == "replace" else None
        result, issued, ack = self.s.call_timed(
            "player_queues/play_media", queue_id=target.queue_id, media=uris,
            option=option, radio_mode=False,
        )
        del result  # play_media returns None on success and on having queued nothing

        event_ms = self.s.wait_for_event(
            issued, target.queue_id, {"queue_updated", "queue_items_updated"}, timeout=6.0
        )

        # The snapshot that satisfied the predicate is kept, rather than taking a
        # fresh read afterwards for the report. A Subsonic handover flips back to
        # `paused` for several seconds while Alexa buffers, so a detail line read
        # a moment later says "paused" under a row marked pass, which reads as a
        # contradiction in the report and is really just a later fact.
        witness: dict[str, Any] = {}

        def loaded() -> bool:
            queue = self.s.queue(target.queue_id)
            if expected is not None and queue.get("items") != expected:
                return False
            if queue.get("items", 0) < 1:
                return False
            if queue.get("state") != "playing":
                return False
            witness.update(queue)
            return True

        ok, effect_ms = observe(
            self.s, loaded, floor=PLAY_CONFIRM_SECONDS, budget=25.0, issued=issued,
        )
        queue = witness or self.s.queue(target.queue_id)
        items = self.s.queue_items(target.queue_id)
        detail = (f"items={queue.get('items')} state={queue.get('state')} "
                  f"expected={expected if expected is not None else 'n/a'}")
        # Recorded, not asserted. `play_media` resolves its items concurrently
        # and has been observed loading a four-URI list in a different order
        # than it was given, so the order the queue ends up in is evidence
        # rather than a contract this suite is prepared to claim.
        queued = [str(i.get("name", "")) for i in items]
        obs = record(
            "play" if option == "replace" else f"play[{option}]", target, source,
            ok=ok, detail=detail, ack_ms=ack, event_ms=event_ms, effect_ms=effect_ms,
            floor=PLAY_CONFIRM_SECONDS, budget=25.0,
            queued=queued, asked=[m["name"] for m in media],
            order_preserved=_names_in_order(queued, [m["name"] for m in media]),
        )
        return obs

    def settle(self, target: Target, expect_item: str | None = None,
               budget: float = 40.0) -> bool:
        """Wait until one track change has finished before starting another.

        A transition is in flight from the moment MA moves `current_index` until
        Alexa is actually playing the new item and reporting a position for it,
        which spans the 1s debounce, a `play_media` republish and a poll. Issuing
        the next command inside that window is asking two questions at once, and
        the answer that comes back - MA's index snapping to whatever Alexa was
        still playing - looks like a defect in the second command.

        Settled means all three agree: the queue is playing, the player is
        reporting the item the queue thinks is current, and the position has
        started to move.

        The budget is long because a queue reports `paused` for up to ten
        seconds after a Subsonic track is handed over - Alexa buffering a track
        it is fetching from a self-hosted server, surfacing in MA as a pause
        nobody asked for. Waiting it out here is what keeps that from being
        mistaken for a failed transport command somewhere else.

        `expect_item` names the queue item the caller is waiting *for*. Without
        it this returns immediately after a track-change command, because the
        queue is still validly settled on the item it has not left yet - which
        makes "wait for the transition" mean "do not wait at all".
        """
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            queue = self.s.queue(target.queue_id)
            player = self.s.player(target.player_id)
            item = queue.get("current_item") or {}
            current = item.get("name") or ""
            title = (player.get("current_media") or {}).get("title") or ""
            # A live stream has no track to agree about. The queue item is the
            # station ("SomaFM: Groove Salad") and the player reports whatever
            # song the station happens to be playing ("One Day"), so requiring
            # them to match means a radio queue is never settled and every
            # radio case fails on its precondition. Identified by the item
            # having no duration, which is the same thing `seek` refuses on.
            live = not item.get("duration")
            agreed = live or (bool(title) and (title.lower() in current.lower()
                                               or current.lower() in title.lower()))
            arrived = expect_item is None or expect_item.lower() == current.lower()
            if (arrived and agreed and queue.get("state") == "playing"
                    and float(queue.get("elapsed_time") or 0.0) > 1.0):
                return True
            time.sleep(0.5)
        return False

    def wait_state(
        self, target: Target, state: str, *, floor: float, budget: float, issued: float,
    ) -> tuple[bool, float | None]:
        return observe(
            self.s, lambda: self.s.player(target.player_id).get("state") == state,
            floor=floor, budget=budget, issued=issued,
        )


# --- reporting ---------------------------------------------------------------


def write_report(directory: Path, session: LiveSession, targets: dict[str, Target],
                 matrix_rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Write the run out as JSON and as a table a person will read."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    payload = {
        "generated": stamp,
        "ma_version": session.server_version,
        "schema_version": session.schema_version,
        "targets": {k: {"player_id": v.player_id, "name": v.name, "members": list(v.members)}
                    for k, v in targets.items()},
        "matrix": matrix_rows,
        "observations": [asdict(o) for o in RECORDER.observations],
        "latency": RECORDER.latency_table(),
        # What the run spent making Amazon obey. Reported at run level because
        # that is the level the number means something at: Music Assistant answers
        # optimistically, so this is the only place a dropped command shows up
        # at all.
        "convergence": {"resends": RECORDER.resends, "lost": RECORDER.lost},
        "notes": RECORDER.notes,
    }
    json_path = directory / f"conformance-{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    md_path = directory / f"conformance-{stamp}.md"
    md_path.write_text(_markdown(payload))
    return json_path, md_path


def _markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Music Assistant live conformance - {payload['generated']}",
        "",
        f"Music Assistant {payload['ma_version']} (schema {payload['schema_version']}).",
        "",
        "Targets:",
        "",
    ]
    for kind, target in payload["targets"].items():
        members = f" ({len(target['members'])} members)" if target["members"] else ""
        lines.append(f"- **{kind}**: {target['name']} `{target['player_id']}`{members}")

    lines += ["", "## Results", "",
              "| feature | source | target | outcome | detail |",
              "| --- | --- | --- | --- | --- |"]
    for obs in payload["observations"]:
        outcome = "pass" if obs["ok"] else "FAIL"
        lines.append(
            f"| {obs['action']} | {obs['source']} | {obs['target']} | {outcome} | "
            f"{obs['detail'].replace('|', '/')} |"
        )

    lines += ["", "## Latency", "",
              "All milliseconds. `ack` is MA's acknowledgement, `event` is MA's "
              "published state change (optimistic for Music Assistant controls), `effect` "
              "is the first honest read at or after the floor.", "",
              "| action | n | ack p50 | ack max | event p50 | event max | "
              "effect p50 | effect max | budget s | over |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for action, row in payload["latency"].items():
        lines.append(
            f"| {action} | {row['n']} | {row['ack_ms_p50']} | {row['ack_ms_max']} | "
            f"{row['event_ms_p50']} | {row['event_ms_max']} | {row['effect_ms_p50']} | "
            f"{row['effect_ms_max']} | {row['budget_s']} | {row['over_budget']} |"
        )

    conv = payload.get("convergence") or {}
    if conv.get("resends") or conv.get("lost"):
        lines += [
            "", "## What it cost to make Amazon obey", "",
            f"- Commands resent: **{conv.get('resends', 0)}**",
            f"- Commands that never stuck: **{len(conv.get('lost') or [])}**"
            + (f" ({', '.join(conv['lost'])})" if conv.get("lost") else ""),
            "",
            "Music Assistant reports the state it was asked for without waiting for "
            "Alexa to agree, so none of this is visible in the rows above. A "
            "resend is Amazon dropping a command and taking it on the second "
            "ask, which the retry exists for. A command that never stuck means "
            "the speaker was left doing something else.",
        ]
    if payload["notes"]:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in payload["notes"]]

    lines += ["", "## Matrix", "",
              "| feature | source | target | status | reason |",
              "| --- | --- | --- | --- | --- |"]
    for cell in payload["matrix"]:
        lines.append(
            f"| {cell['feature']} | {cell['source']} | {cell['target']} | "
            f"{cell['status']} | {cell['reason']} |"
        )
    return "\n".join(lines) + "\n"
