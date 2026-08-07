"""The conformance grid, as data, including the cells that are not run.

{single speaker, group} x {streaming, subsonic, radio} x {the transport,
queue and volume features} is 84 cells, and a good number of them are not
questions. Radio has no next track. `players/cmd/group_volume` aimed at a lone
Echo quietly becomes `players/cmd/volume_set`.

The reason a cell is not exercised is written down here rather than expressed by
the cell's absence, because an omission and a deliberate exclusion look identical
in a results table, and the difference is exactly what a reader wants: "radio has
no seek" is a fact about radio, whereas a missing row is a fact about whoever
wrote the suite. Four statuses, and only one of them means "don't look":

`RUN`          exercise it and assert the outcome happened.
`EXPECT_ERROR` exercise it and assert MA raises the documented error. A refusal
               is behaviour; a cell that merely skips here would stop noticing
               if the refusal turned into a silent no-op.
`UNSUPPORTED`  exercise it and assert the documented silence - no error raised
               *and* no state changed. Mute is the case: Music Assistant does not declare
               `VOLUME_MUTE`, so `mute_control` resolves to `"none"` and
               `cmd_volume_mute` falls off the end of the function. Asserting
               that it works and asserting that it errors are both wrong.
`SKIP`         do not exercise, for the reason given.
"""

from __future__ import annotations

from dataclasses import dataclass

RUN = "run"
EXPECT_ERROR = "expect_error"
UNSUPPORTED = "unsupported"
SKIP = "skip"

TARGETS = ("single", "group")
SOURCES = ("streaming", "subsonic", "radio")

FEATURES = (
    "play", "pause", "resume", "stop", "next", "previous", "seek", "rewind",
    "shuffle", "repeat", "enqueue", "volume", "group_volume", "mute",
)

# MA error codes, from music_assistant_models/errors.py.
INVALID_COMMAND = 12
QUEUE_EMPTY = 8
UNSUPPORTED_FEATURE = 9
PLAYER_COMMAND_FAILED = 11


@dataclass(frozen=True)
class Cell:
    feature: str
    source: str
    target: str
    status: str
    reason: str = ""
    error_code: int | None = None

    @property
    def id(self) -> str:
        return f"{self.feature}-{self.source}-{self.target}"

    @property
    def runs(self) -> bool:
        return self.status in (RUN, EXPECT_ERROR, UNSUPPORTED)


# --- the reasons, written once and shared by the cells that need them --------

_RADIO_NO_INDEX = (
    "a radio queue holds one live item; player_queues/next finds no next index "
    "and returns silently, so there is no transition to observe"
)
_RADIO_NO_DURATION = (
    "the current item has no duration, so player_queues/seek raises "
    "InvalidCommand (12) by design; asserted as a refusal rather than skipped"
)
_RADIO_NO_DURATION_SKIP = (
    "player_queues/skip is seek(elapsed + seconds); with no duration it raises "
    "InvalidCommand (12) the same way, so rewind is the same refusal"
)
_VOLUME_SOURCE_BLIND = (
    "volume does not pass through the queue or the media source - it is "
    "players/cmd/volume_set on the Echo either way. Exercised once, under "
    "streaming, rather than three times identically"
)
_GROUP_VOLUME_DEGRADES = (
    "players/cmd/group_volume aimed at an ungrouped player falls through to "
    "cmd_volume_set, so this cell would silently re-run the per-player volume "
    "case while claiming to test group volume"
)
_MUTE_NOT_DECLARED = (
    "Music Assistant does not declare PlayerFeature.VOLUME_MUTE, so mute_control is "
    "\"none\" and cmd_volume_mute matches no branch and returns silently. "
    "Exercised once per target to prove the silence, not the mute"
)
_MUTE_SOURCE_BLIND = "mute is not source-dependent; proven once per target"


def _cells() -> list[Cell]:
    out: list[Cell] = []

    def add(feature: str, source: str, target: str, status: str,
            reason: str = "", error_code: int | None = None) -> None:
        out.append(Cell(feature, source, target, status, reason, error_code))

    for target in TARGETS:
        for source in SOURCES:
            # Transport works on every source. A live stream can be paused,
            # resumed and stopped like anything else; only its *position*
            # cannot be moved.
            add("play", source, target, RUN)
            add("pause", source, target, RUN)
            add("resume", source, target, RUN)
            add("stop", source, target, RUN)

            if source == "radio":
                add("next", source, target, SKIP, _RADIO_NO_INDEX)
                add("previous", source, target, SKIP, _RADIO_NO_INDEX)
                add("seek", source, target, EXPECT_ERROR, _RADIO_NO_DURATION,
                    INVALID_COMMAND)
                add("rewind", source, target, EXPECT_ERROR, _RADIO_NO_DURATION_SKIP,
                    INVALID_COMMAND)
            else:
                add("next", source, target, RUN)
                add("previous", source, target, RUN)
                add("seek", source, target, RUN)
                add("rewind", source, target, RUN)

            # Shuffle and repeat are queue *settings*. They are meaningful even
            # on a one-item radio queue, where the question is whether the flag
            # is stored and read back, and that is worth asking because MA
            # coerces an unrecognised value to `unknown` rather than refusing it.
            add("shuffle", source, target, RUN)
            add("repeat", source, target, RUN)
            add("enqueue", source, target, RUN)

            if source == "streaming":
                add("volume", source, target, RUN)
                add("mute", source, target, UNSUPPORTED, _MUTE_NOT_DECLARED)
            else:
                add("volume", source, target, SKIP, _VOLUME_SOURCE_BLIND)
                add("mute", source, target, SKIP, _MUTE_SOURCE_BLIND)

            if target == "group" and source == "streaming":
                add("group_volume", source, target, RUN)
            elif target == "group":
                add("group_volume", source, target, SKIP, _VOLUME_SOURCE_BLIND)
            else:
                add("group_volume", source, target, SKIP, _GROUP_VOLUME_DEGRADES)

    return out


CELLS: tuple[Cell, ...] = tuple(_cells())

BY_ID: dict[str, Cell] = {c.id: c for c in CELLS}


def cell(feature: str, source: str, target: str) -> Cell:
    return BY_ID[f"{feature}-{source}-{target}"]


def for_feature(feature: str) -> list[Cell]:
    return [c for c in CELLS if c.feature == feature]


def as_rows() -> list[dict[str, object]]:
    """The grid in the shape the report writes out."""
    return [
        {"feature": c.feature, "source": c.source, "target": c.target,
         "status": c.status, "reason": c.reason, "error_code": c.error_code}
        for c in CELLS
    ]


def summary() -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in CELLS:
        counts[c.status] = counts.get(c.status, 0) + 1
    counts["total"] = len(CELLS)
    return counts
