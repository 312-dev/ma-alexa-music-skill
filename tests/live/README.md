# The live conformance suite

Music Assistant is a Music Assistant player provider whose speakers are Amazon Echos on
the other side of a voice skill. Almost nothing about whether it works can be
established from inside a process: the provider answers Music Assistant
optimistically, Music Assistant swallows commands to players that are not there,
and Amazon is free to ignore a request and say nothing. The 699 offline tests
cover the parts that are decidable on a laptop. This suite covers the rest, by
playing real audio on real speakers and reading back what happened.

It is a **conformance** suite, not a smoke test. Its job is to state what the
provider is supposed to do and then find out, so a run that goes red because
Music Assistant is wrong has done its job. See "Reading the output" for how to tell that
apart from a run that went red because the suite is wrong.

---

## Running it

```bash
pytest                              # the offline suite. Touches no speaker.
pytest -m live tests/live           # the conformance run. Plays audio. ~35 min.
pytest -m live tests/live -k volume # one slice
```

`pytest.ini` carries `addopts = -m "not live"`, so an ordinary `pytest` never
starts one of these. A `-m` on the command line overrides it. The live cases are
still *collected* by a default run, deliberately - an import error or a typo in
the suite fails an ordinary `pytest` rather than waiting to be discovered on the
night someone runs it for real.

### What it needs

- `ssh my-box` works. The Music Assistant API token is read from
  `/opt/ma_alexa/.ma-token` on the box, into memory, and is never printed, logged
  or written into a report. Set `MA_TOKEN` to supply one directly instead.
- A token that has not expired. They last 6 hours:

  ```bash
  tools/ma.sh players     # "auth failed: Invalid or expired token" -> re-mint
  tools/ma_token.sh       # NOTE: this stops and restarts Music Assistant
  ```

- Reachability of the Music Assistant websocket. The default is the tailnet
  address in `environment.json`; override with `MA_WS_URL`.

### Knobs

| variable | default | what it does |
| --- | --- | --- |
| `MA_WS_URL` | the tailnet URL in `environment.json` | where to connect |
| `MA_TOKEN` | read over ssh | supply the token directly |
| `MA_ALEXA_BOX` | `my-box` | ssh host holding the token |
| `MA_ALEXA_LIVE_SPEAKER` | `Living Room` | which cleared speaker is the single-player target |

`MA_ALEXA_LIVE_SPEAKER` is a preference, not a permission: an unrecognised name
falls back to the first cleared speaker, and a name that is not on the allow
list is refused like any other.

---

## Safety

The suite drives an apartment. Four speakers are cleared and everything else on
the Amazon account is not, including two televisions, a projector and a car.

- Every target is obtained through `safety.py`, which matches on the **Amazon
  serial** and fails closed. A device that is not explicitly listed is refused,
  so a speaker that simply appeared cannot be swept into a run.
- **Home Theater is the one that matters.** Every other excluded device is also
  disabled in Music Assistant and never appears in `players/all`; Home Theater is
  enabled and available, so the allow list is the only thing between this suite
  and a television turning itself on. Do not "fix" a refusal by widening the
  list.
- A group is cleared only if every live member is. `harness.authorise()` is the
  sole constructor of a target and checks each member individually before
  applying the group rule, so a group that gained a device between two reads is
  refused rather than driven.
- The group is selected by `player_id`, never by name. Music Assistant also holds
  a disabled `universal_group` player with the identical display name
  "Whole Apartment".
- Volume is capped at 25 everywhere, including inside the group-volume case
  where the levels are chosen by interpolation rather than set directly. Every
  cleared speaker's level is recorded at session start and restored at session
  end, and playback is stopped, both in a `finally` - a run that failed still
  leaves the room as it found it.

`tests/live/test_safety.py` proves the allow list offline, with the rest of the
unit tests, because "can the suite reach a device it was told to leave alone"
has to be answerable before anything is run against an apartment.

---

## What is tested

`matrix.py` holds the grid as data: {single speaker, group} x {streaming,
subsonic, radio} x fourteen features, 84 cells. Every cell carries a status and,
when it is not exercised, the reason.

| status | meaning |
| --- | --- |
| `run` | exercised; the outcome is asserted |
| `expect_error` | exercised; MA is asserted to raise the documented error. Seeking in a live radio stream is the case - a refusal is behaviour, and a cell that merely skipped here would stop noticing if the refusal became a silent no-op |
| `unsupported` | exercised; the documented *silence* is asserted - no error raised **and** nothing changed. Mute is the case |
| `skip` | not exercised, for the reason recorded on the cell |

The reasons are written down rather than expressed by a cell's absence, because
an omission and a deliberate exclusion look identical in a results table and the
difference is the interesting part. "Radio has no next track" is a fact about
radio; a missing row is a fact about whoever wrote the suite.

Four further cases sit outside the grid, for documented API properties that vary
by neither source nor target: enum coercion, `QueueEmpty` on an empty resume,
the refusal of grouping commands, and the end-of-track seek clamp.

---

## Why the suite looks paranoid

Every rule below exists because the obvious version of the test passes on a
provider that is doing nothing.

**Nothing asserts on the absence of an error.** `player_queues/play_media` logs
and *skips* a media item it cannot resolve, then returns success, so it can
report success having queued nothing. `handle_player_command` swallows anything
aimed at an unavailable player and also returns success. Every case here reads
state back - queue contents, playback state, elapsed time, reported volume - and
asserts on what it finds.

**Nothing looks too early.** Music Assistant writes the state it expects and publishes it
*before* Alexa has been asked anything, then re-polls 1.5s later. A read taken
immediately confirms Music Assistant's optimism, not the speaker. `harness.observe()`
refuses to read before a floor:

| floor | seconds | why |
| --- | --- | --- |
| `RESYNC_SECONDS` | 1.5 | the provider's own re-poll after a transport command |
| `PLAY_CONFIRM_SECONDS` | 4.0 | `play_media` is only confirmed when Alexa comes to fetch the queue |
| `NEXT_PREV_DEBOUNCE` + confirm | 5.0 | next/previous move the index now and the audio a second later |
| `VOLUME_QUEUE_DELAY` | 1.5 | alexapy batches a group's per-member writes into one request |
| volume budget | ~11.5 | that delay plus three confirm attempts plus their retry jitter |

Group volume can legitimately take more than eight seconds to converge across
four speakers. Anything slower than its budget is reported as a finding, not
tolerated silently.

**`option` is always passed explicitly** to `play_media`. Omitting it does not
mean `play`; Music Assistant falls back to a per-media-type core config key, so
a suite that leaves it out is testing whatever that instance happens to be set
to.

**Media is resolved before it is queued.** A URI that has gone stale would be
skipped by `play_media`, which would then report success, and a cell that queued
nothing looks exactly like a pass.

**Tolerances are not laziness.** Speaker volume quantises - an Echo Studio asked
for 18 reports 17 - so volume is asserted to +/-2, the same tolerance Music Assistant's
own confirm loop uses. Group volume *interpolates*: `set_group_volume` scales
every child from a snapshot toward 100 or toward 0, so setting the group to 20
sets no member to 20 unless they were all equal first. That case asserts the
group's reported volume and asserts that the members were **not** all set to the
requested value, because a broadcast would be the bug.

**`previous` is asserted against elapsed time, not against an index delta.** It
restarts the current track when the track is five seconds or more in, and steps
back only when it is not. The test reads the elapsed time at the moment it
issues the command and asserts whichever behaviour was due.

**Shuffle asserts the half that is deterministic.** Enabling shuffle and
asserting the order changed is flaky - with three items left, one run in six
reshuffles them into the order they were already in. Turning shuffle *off*
re-sorts by `sort_index`, so the test enables it, checks the items are the same
set, disables it, and asserts the original order came back.

---

## Reading the output

Each run writes `results/conformance-<timestamp>.json` and a `.md` of the same
name. The path is printed at the end of the run.

- **Results table** - one row per action actually issued, including the ones
  that failed.
- **Latency table** - p50 and max per action type across three clocks:

  | clock | what it measures |
  | --- | --- |
  | `ack` | Music Assistant acknowledging the command. Round trip only. |
  | `event` | MA publishing a changed snapshot. For an Music Assistant control this is the *optimistic* write - it is a measure of the server, not of the speaker. |
  | `effect` | the first honest read, at or after the floor. **This is the only one that says the speaker did it.** Resolution is the ~0.4s poll interval. |

  `over` counts observations that took longer than the action's budget. Those
  are findings, not failures - the assertion still passed.

- **Matrix table** - all 84 cells with their status and reason, including the
  ones not exercised.

A failing assertion is a claim about Music Assistant, and the message carries the
measurement that supports it. To tell a genuine defect from a suite bug, the
question to ask is which of the two numbers in the message is wrong: several
cases deliberately assert on `player.elapsed_time` (what Music Assistant read back off
Alexa) *and* `queue.elapsed_time` (what every UI shows) separately, precisely so
that when they disagree the report says which layer is at fault instead of just
that something is off.

**A clean run is not currently expected.** Against the provider as deployed:

| what fails | how often | shape of it |
| --- | --- | --- |
| `seek`, `rewind` | every non-radio cell, every run | MA reports a position inflated by exactly the amount that was sought to |
| `stop` on Subsonic | both targets, reproducibly | reaches idle, then goes back to playing on its own |
| `volume` on the group | every run | asked for 20, the group reports 50 or 0 |
| `pause` on the group | roughly one run in three | pause is lost and the group returns to playing ~10s later |
| `next` / `previous` | occasionally, mostly Subsonic | the index moves and is then reverted |

Those are findings the suite is reporting, not maintenance debt in it - the
failure messages carry the evidence. Treat a *new* failure, or one of these
going quiet, as the thing worth looking at.

---

## Refreshing `environment.json`

The suite does not trust it for identity. Player ids carry the provider instance
id, which changes when the provider is re-added, so `Music Assistant.discover()` matches
live players on the Amazon serial and takes only the group id from the file.
What the file really supplies is the **media**: URIs verified playable against
this instance.

Full procedure in `ENVIRONMENT.md`. The short version:

```bash
tools/ma_token.sh                                  # 6h token; restarts MA
tools/ma.sh raw players/all '{}'                   # ids, types, group_members
tools/ma.sh raw music/item_by_uri '{"uri":"..."}'  # verify, do NOT verify by playing
tools/ma_token.sh revoke
```

Two things that file records which the API will not tell you: the allow/exclude
split is a **user decision**, not a property of the system - re-confirm it with
the user rather than inferring it from `enabled` - and the container name is
`app-<nomad alloc id>` and changes on every restart, including the one
`ma_token.sh` performs to mint the token. Re-derive it; never hardcode it.

---

## Files

| file | what it is |
| --- | --- |
| `safety.py` | the allow list. Fails closed on the Amazon serial. Do not widen it |
| `test_safety.py` | proves the allow list, **offline**, in CI |
| `environment.json` | the live instance: group id, verified media URIs |
| `ENVIRONMENT.md` | how to regenerate it |
| `COMMANDS.md` | the Music Assistant API surface, read out of the running server |
| `harness.py` | connection, targets, timed actions, observation, reporting |
| `matrix.py` | the grid, including why each unexercised cell is unexercised |
| `conftest.py` | session wiring: one socket, volume custody, the report |
| `test_conformance.py` | the cases |
| `results/` | one JSON and one markdown report per run |
