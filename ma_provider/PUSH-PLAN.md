# Plan: replace polling with Amazon's push channel

Status: **proposed**, not started. Written 2026-08-03 after the Music Assistant
migration, off the back of measurements taken that night.

## The problem, measured

Every piece of state Ampere shows about an Echo is read by asking Amazon, on a
timer, once per player. `POLL_INTERVAL = 10`.

That interval is not a guess and lowering it is not the fix. Each poll is an
HTTP call **per player**. With ten players, ten seconds is already a sustained
request per second, about 86,000 a day against an API that throttles. Two
seconds would be five times that.

What it costs, all measured on 2026-08-03 against the live system:

| Symptom | Cause |
|---|---|
| Resume shows the wrong position for seconds | correction waits for the next poll |
| Scrubber sits where it was, at the end of a track | same |
| A volume change appears to revert | poll re-reads a value Amazon has not caught up on |
| A track change is noticed up to 10s late | same |

Two of those were patched the same night, and both patches are workarounds for
the same missing capability:

- `_resync_soon()` fires one catch-up poll 1.5s after a control, so a resume
  does not sit wrong for ten seconds.
- `_reconcile_volume()` defends a requested volume for 30s so a lagging read
  cannot overwrite it.

Both exist only because there is no way to be *told* when something changed.

## What push is

`alexapy`, which this already depends on, ships `HTTP2EchoClient`. It opens one
long-lived HTTP/2 stream to Amazon's device gateway
(`alexa.na.gateway.devices.a2z.com`, path `/v20160207/directives`) and delivers
each directive to a callback as parsed JSON. It is the mechanism Home
Assistant's `alexa_media_player` uses, so this is a well-trodden path rather
than an experiment.

```python
HTTP2EchoClient(
    login,                      # the AlexaLogin this provider already holds
    msg_callback=...,           # async, one parsed directive per call
    open_callback=...,
    close_callback=...,
    error_callback=...,
    read_timeout=300,           # staleness detection, see below
)
```

**The economics invert.** Polling costs one request per player per interval, so
more speakers force a slower interval. Push costs **one connection for the
account, regardless of how many Echoes are on it.** Latency stops being
something bought with request budget.

## What this does not change

Playback is still triggered by sending Alexa a text command, and that is
unrelated. Measured the same night: `run_custom` returns in ~0.2s and Alexa
arrives 1.8 to 2.0 seconds later. There is no Amazon API to start a music-skill
queue directly, which is the reason the utterance mechanism exists at all.

So push fixes **knowing what happened**. It does not make playback start
faster. Anyone reading this expecting instant play should stop here.

## Design

### One client per provider instance, not per player

The stream is per account. `AmpereAlexaProvider` owns it; players subscribe.

```
AmpereAlexaProvider
  └── AlexaPushClient          supervises HTTP2EchoClient
        ├── on message  ->  route by device serial -> AmperePlayer.apply_push()
        ├── on close    ->  reconnect with backoff
        └── on error    ->  reconnect, or fall back to polling if login is dead
```

### The consumer reconnects, not the library

Read the source before writing any of this: **`HTTP2EchoClient` does not
reconnect itself.** It calls `close_callback` and `error_callback` and stops.
A supervisor that assumes otherwise produces a provider that works for an hour
and then silently stops updating, which is worse than polling because nothing
looks wrong.

`read_timeout` (default 300s) exists because a stream can go silent without a
transport-level close. Keep it. A stalled stream that delivers nothing forever
is exactly the failure that would masquerade as "the house is quiet".

### Polling stays

Not as a transition step, permanently, at a much slower interval.

Push tells us what changed. It cannot tell us what we missed while
disconnected, and it says nothing at all about a device that changed state
before we connected. A slow poll (60 to 120 seconds) is the floor under it, and
the thing that repairs state after any gap.

The provider should also poll immediately on `open_callback`, because
everything that happened while the stream was down is invisible.

### Event vocabulary

`msg_callback` receives whatever Amazon sends. The events that matter carry
volume, playback state and track boundaries; the exact command names have to be
read off a live stream rather than guessed, because they are undocumented and
`alexapy` only transports them.

**First task is therefore observation, not implementation.** Log the raw
directives for a session with real use in it, and write the mapping from what
is actually seen.

## Phases

| Phase | Delivers | Risk |
|---|---|---|
| 1. Observe | a recorded transcript of real directives | none |
| 2. Connect | supervised client, logging only, polling untouched | low |
| 3. Consume | volume and playback state from push | medium |
| 4. Slow the poll | the actual win | medium |
| 5. Remove the workarounds | `_resync_soon`, volume defence | low |

### Phase 1: observe

Attach the client, log every directive to a capture directory, change volume,
pause, skip, and move a queue between rooms. Produces the mapping in the
"Event vocabulary" gap above.

No behaviour change. Nothing depends on it yet.

### Phase 2: connect and supervise

Real client, real reconnect, real backoff, still nothing consuming the events.
This phase exists to find out whether the connection **survives** on this
account: how often it drops, whether the cookie refresh interacts with it,
whether the 300s staleness check fires in practice.

Ship it and leave it for a few days. A connection that is fine for an hour
proves nothing.

### Phase 3: consume

Route directives to players by device serial. Apply volume and playback state.
Keep polling at ten seconds throughout, and **log every disagreement between
push and poll**. That log is the evidence for phase 4; without it, slowing the
poll is a guess.

### Phase 4: slow the poll

Only when phase 3 has run long enough to show push and poll agreeing. Raise
`POLL_INTERVAL` to 60 to 120 seconds.

This is where the win is realised and where the risk lands. If push is
incomplete in some way not yet noticed, this is the change that turns it into a
visible fault.

### Phase 5: remove the workarounds

`_resync_soon()` and the volume defence become dead weight once state arrives
in under a second. Removing them is the last step, not the first, and only
after phase 4 has held.

Note that `_confirm_volume` and `_confirm_play` are **not** in this list. Those
resend commands Amazon accepted and silently dropped, which is a different
defect and one push does nothing about.

## What not to do

- **Do not lower `POLL_INTERVAL` as an interim measure.** It is the thing push
  exists to avoid needing, and throttling from Amazon presents as unrelated
  failures elsewhere.
- **Do not remove polling entirely.** See above; the gap after a reconnect has
  no other repair.
- **Do not consume events before phase 1 is written down.** The command names
  are undocumented and a mapping built from a guess will be wrong in exactly
  the cases that matter least often and hurt most.
- **Do not open a client per player.** One account, one stream.

## Open questions

- Does the push stream deliver events for **speaker groups**, or only for
  physical devices? Group state has been the source of most of the surprises in
  this provider so far, and there is no reason to assume it behaves here.
- Does it fire for changes made by a **music skill** (this one), or only for
  Amazon's own providers? If the latter, phases 3 to 5 buy much less than they
  look like they do, and this whole plan should stop after phase 2.
- How does the stream interact with the **cookie refresh** in `AlexaLogin`?
- Does running this alongside Home Assistant's `alexa_media_player` on the same
  Amazon account cause either to be disconnected?

The second question is the one that decides whether this is worth doing at all,
and phase 1 answers it for the cost of an afternoon.

## Where the value actually is

Ranked by what a person would notice:

1. Volume and playback state within a second, rather than up to ten.
2. The scrubber telling the truth.
3. A quieter API footprint, which matters more as speakers are added.

None of it makes music start faster. That is bounded by Amazon's utterance
pipeline and is not addressable from here.
