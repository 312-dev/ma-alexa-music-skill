# Plan: replace polling with Amazon's push channel

Status: **phase 1 complete, phases 2-5 blocked on an auth problem.** Written
2026-08-03 after the Music Assistant migration; revised the same day once the
push stream was actually observed rather than reasoned about.

Read "What phase 1 found" before anything else. Two of the assumptions this
plan was originally built on turned out to be wrong, one in our favour and one
against.

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

**The economics invert.** Polling costs one request per player per interval, so
more speakers force a slower interval. Push costs **one connection for the
account, regardless of how many Echoes are on it.** Latency stops being
something bought with request budget.

## What phase 1 found

Method: Home Assistant's `alexa_media_player` is configured against the *same*
Amazon account, so it already holds a live push stream. Rather than open a
competing connection, `alexapy.alexahttp2` was set to debug in Home Assistant
and the raw directives were read out of its log while actions were driven
through Ampere. Zero new connections, zero risk to the running system.

### 1. Push fires for music-skill playback. It names us.

This was the open question that could have killed the plan, and the answer is
unambiguous. Starting a track through Ampere produced, within one second of
each other:

```
PUSH_MEDIA_QUEUE_CHANGE   changeType=NEW_QUEUE
PUSH_AUDIO_PLAYER_STATE   audioPlayerState=PLAYING  mediaReferenceId=<queue>:1
NotifyNowPlayingUpdated   cause=PLAYBACK_STARTED
                          provider.providerName='Ampere'
```

Amazon reports our skill by name in the payload. Pause, resume and skip each
produced their own directives.

### 2. Push fires for speaker groups.

The other open question, also yes. Playing to "Whole Apartment" produced
directives whose `dopplerId.deviceSerialNumber` was
`00000000000000000000000000000001` with `deviceType: A3C9PE6TNYLTCH` -- that is
verbatim Ampere's own group player id. No mapping table is needed for either
groups or physical devices.

A group volume change additionally emits a separate `PUSH_VOLUME_CHANGE` per
member device, which is more useful than the poll, not less.

### 3. Delivery transit is effectively zero.

Each directive carries Amazon's own event clock. For one volume change:

| | |
|---|---|
| Amazon's `timeStamp` | 09:37:48.697 |
| Receipt logged | 09:37:48.611 |

Receipt *precedes* the stamp by 86ms, which is clock skew between the two
machines. So the push arrives the instant Amazon knows.

This corrects a misreading worth keeping. The wall-clock gap between issuing a
volume command and seeing the push was about 2 seconds, but that 2 seconds is
Amazon propagating to the device and hearing back. **Polling pays that same 2
seconds and then adds 0-10 seconds of interval on top.** Push does not make the
truth arrive sooner than Amazon has it; it removes the extra wait entirely.

### 4. The event vocabulary

The phase 1 deliverable. Observed, not guessed. Every payload is reached
through three levels of JSON-encoded-inside-a-string:

```
directive.payload.renderingUpdates[0].resourceMetadata   -> a JSON *string*
  -> {"command": "...", "payload": "<another JSON string>", "timeStamp": <ms>}
```

| Command | Carries |
|---|---|
| `PUSH_VOLUME_CHANGE` | `deviceSerialNumber`, `volumeSetting` (0-100), `isMuted` |
| `PUSH_MEDIA_QUEUE_CHANGE` | `changeType` (`NEW_QUEUE`, `STATUS_CHANGED`), `playBackOrder`, `loopMode` |
| `PUSH_AUDIO_PLAYER_STATE` | `audioPlayerState` (`PLAYING`, `INTERRUPTED`), `mediaReferenceId` |
| `NotifyNowPlayingUpdated` | `cause`, `playerState`, `progress.mediaProgress`, `progress.mediaLength`, `provider.providerName`, `infoText.{title,subText1}`, `mediaReference.value` (our `contentId` and `queueId`), `mainArt.*` |

### 5. The scrubber correction is sitting in the payload

At the moment of a pause, push reported `mediaProgress: 83306` against
`mediaLength: 232000`, and on resume `mediaProgress: 60309`. Music Assistant's
own `media_position` at those same moments was **0**, with a
`media_position_updated_at` hours stale.

The number the scrubber has been getting wrong arrives, correct, unprompted.

### 6. The blocker: push needs a bearer token we do not have

`HTTP2EchoClient` authenticates with `Bearer {login.access_token}`. Ampere's
login restores a **cookie jar** written by the upstream alexa provider. Cookies
authenticate the REST API perfectly well and yield no OAuth token at all.

Confirmed live, not inferred. A second stream was opened from inside the Music
Assistant container using Ampere's own session:

```
logged in, access_token present: False
stream open
stream CLOSED
done, 0 directives received
```

It connected with `Bearer None` and Amazon dropped it immediately.

`alexapy` has one route to a token that does not need a browser --
`AlexaLogin.get_tokens()`, which registers a virtual device against
`/auth/register` and is followed by `register_capabilities()`, whose docstring
says outright it is "Required for HTTP2/Push". That was tried:

```
get_tokens -> False | access_token now: False
```

Reading the source explains why. `/auth/register` sends an `auth_data` block
built from *either* an existing `access_token` *or* an
`authorization_code` + `code_verifier` pair. With a cookie-only session both
are absent, `auth_data` goes up empty, and Amazon refuses. Those fields are
only ever populated from the `oauth` dict handed to the `AlexaLogin`
constructor, which in turn only ever comes from `alexapy`'s interactive
**proxy login** -- the browser-based flow Ampere deliberately avoids by
piggybacking on a cookie file.

So push is not "attach a client and supervise it". It is **"add an OAuth login
to the setup wizard, then attach a client."** That is a different and much
larger piece of work, and it lands on the user as a new setup step.

### 7. Corrections to this document's earlier claims

- **There is no `read_timeout` parameter.** An earlier draft of this plan said
  `HTTP2EchoClient(..., read_timeout=300)` gave staleness detection. In
  alexapy 1.29.17 no such parameter exists. What exists is a `manage_pings()`
  loop every 299 seconds that only reacts to an HTTP 403. A stream that goes
  silent without closing is **not** detected. That makes the supervisor's job
  harder and makes "polling stays" mandatory rather than prudent.
- **Coexistence with Home Assistant is still untested.** The second stream
  never authenticated, so it proved nothing either way about whether two
  clients can share one account. Home Assistant's `alexa_media_player` is on
  this account and its alarm and timer sensors depend on its stream; breaking
  it would be a visible regression in the apartment.

## Two findings that are not about push

Both surfaced from the same capture and are cheaper than the push project.

### `mediaProgress` units differ by endpoint

`provider.py` documents, from a measurement taken earlier the same day, that
the REST poll returns `{'mediaLength': 226, 'mediaProgress': 11}` for a
226-second track -- **seconds** -- and the code was corrected to stop dividing
by 1000.

The push payload for a 232-second track reads
`{'mediaLength': 232000, 'mediaProgress': 83306}` -- **milliseconds**.

Both were measured on 2026-08-03. Whatever the explanation, anything that
consumes push must convert, and feeding a push value straight into
`_attr_elapsed_time` would recreate the exact "scrubber never moves" bug the
existing comment was written to explain, running 1000x the other way. Confirm
the units per endpoint before writing either path.

### Every publish is a one-item queue

Every `NotifyNowPlayingUpdated` reports
`transport: {next: 'DISABLED', previous: 'DISABLED'}` and a `mediaReference`
whose `id` is always `"0"`, and every skip emits `NEW_QUEUE` rather than an
advance within a queue.

That is Amazon being told our queue has one item in it. It explains the
measured 7.3 seconds for a skip -- a full re-publish plus a fresh utterance --
and is the likely reason a spoken "Alexa, next" is refused on an Ampere
session. **This is probably a bigger win than push and does not need an auth
change.**

## Verdict

The value is confirmed, and it is larger than this plan originally claimed:
push carries the exact track-progress correction, it covers groups, and it
names our own content ids so events can be matched to queue items without
guessing.

The cost is also larger, and lands somewhere the plan did not anticipate: an
OAuth login flow, not a client and a supervisor.

Recommended order:

1. **The one-item queue** (no auth change, likely fixes skip latency and
   spoken next/previous).
2. **The `mediaProgress` unit question** (cheap, and the scrubber is the
   loudest remaining complaint).
3. **Push**, and only as a deliberate project that starts with adding a proxy
   OAuth login to the wizard -- not as an incremental improvement.

## Design, if and when it is built

### One client per provider instance, not per player

The stream is per account. `AmpereAlexaProvider` owns it; players subscribe.
Route by `dopplerId.deviceSerialNumber`, which matches player ids exactly for
both devices and groups.

### The consumer reconnects, not the library

**`HTTP2EchoClient` does not reconnect itself.** It calls `close_callback` and
`error_callback` and stops. A supervisor that assumes otherwise produces a
provider that works for an hour and then silently stops updating, which is
worse than polling because nothing looks wrong.

Because there is no read timeout (see corrections above), the supervisor must
supply its own staleness clock. A stalled stream delivering nothing forever is
exactly the failure that masquerades as "the house is quiet".

### Polling stays

Not as a transition step, permanently, at a much slower interval (60 to 120
seconds). Push tells us what changed. It cannot tell us what we missed while
disconnected, and it says nothing about a device that changed state before we
connected. Poll immediately on `open_callback` for the same reason.

## Phases

| Phase | Delivers | Status |
|---|---|---|
| 1. Observe | recorded vocabulary of real directives | **done** |
| 2. Obtain a token | proxy OAuth login in the wizard | **blocked, not started** |
| 3. Connect | supervised client, logging only | not started |
| 4. Consume | volume, playback state, progress from push | not started |
| 5. Slow the poll | the actual win | not started |
| 6. Remove the workarounds | `_resync_soon`, volume defence | not started |

Phase 2 did not exist in the original plan and is the whole reason the rest is
stalled.

Phase 3 should be shipped and left alone for several days before anything
consumes it. The point of that phase is to find out whether the connection
survives on this account -- how often it drops, how the cookie refresh
interacts with it, and whether it coexists with Home Assistant's client. A
connection that is fine for an hour proves nothing.

Phase 6 keeps `_confirm_volume` and `_confirm_play`. Those resend commands
Amazon accepted and silently dropped, which is a different defect and one push
does nothing about.

## What not to do

- **Do not lower `POLL_INTERVAL` as an interim measure.** It is the thing push
  exists to avoid needing, and throttling from Amazon presents as unrelated
  failures elsewhere.
- **Do not remove polling entirely.** The gap after a reconnect has no other
  repair, and there is no read timeout to notice a silent stall.
- **Do not open a client per player.** One account, one stream.
- **Do not reach into Home Assistant's config entry for its OAuth tokens.** It
  would work, and it would make Ampere stop working the day the user removes
  an unrelated integration.

## Where the value actually is

Ranked by what a person would notice:

1. The scrubber telling the truth.
2. Volume and playback state within a second rather than up to ten.
3. A quieter API footprint, which matters more as speakers are added.

None of it makes music start faster. Measured 2026-08-03: `run_custom` returns
in ~0.2s and Alexa arrives 1.8 to 2.0 seconds later; a skip took 7.3 seconds
end to end. That is Amazon's utterance pipeline, and the only lever on it is
the one-item-queue finding above, not push.
