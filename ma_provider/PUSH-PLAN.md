# Plan: replace polling with Amazon's push channel

Status: **built and running.** Written 2026-08-03 after the Music Assistant
migration, revised the same day once the stream was observed rather than
reasoned about, and closed out when it shipped.

This is kept as a record of what was measured, because almost none of it could
be looked up. Amazon documents none of this and `alexapy` only transports it.
What was built from it is described in README and lives in `push_events.py`,
`push_router.py`, `push_auth.py`, `push.py`, `push_signin.py` and
`alexapy_compat.py`.

Read "What phase 1 found" before anything else. Several assumptions this plan
was originally built on turned out to be wrong, and section 7 lists the ones
this document itself got wrong before they were measured.

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


## What was built

The blocker in section 6 was real but the conclusion drawn from it was wrong.
It said push needs "a new OAuth login in the wizard", implying a novel flow.
Music Assistant's own alexa provider already had one: an ACTION entry,
`AuthenticationHelper`, and `alexapy`'s `AlexaProxy` on a webserver route. That
flow already walks the PKCE pages and already mints the token. It just throws
it away, because it only wants the cookie.

So the work was one button, not a new subsystem.

| Module | Does |
|---|---|
| `push_events` | Decodes the directive envelope. alexapy hands over a raw dict by design; interpreting it is ours. |
| `push_router` | Places an event on a player, by device serial or by the `ext:` contentId we published. |
| `push_auth` | Owns the registration, its token and its renewal, on a login of its own. |
| `push` | Supervises `HTTP2EchoClient`, which does not reconnect itself. |
| `push_signin` | The one button. |
| `alexapy_compat` | Every private touch, in one file, with a guard test. |

Polling was not removed, only slowed to 30 or 60 seconds depending on whether
the installed alexapy can detect a stalled stream. Push says what changed; it
can never say what was missed while disconnected.

## What implementation found that observation did not

Six things, all of which cost real time and none of which were visible from
reading the stream.

**Two push streams on one Amazon account coexist.** This was listed as an open
question that could have forced a redesign. Home Assistant's client and
Ampere's both received the same volume event 101ms apart.

**`HTTP2EchoClient` starts a ping loop that must be cancelled.** `async_run`
starts a reader *and* a ping loop that sleeps 299s forever. Closing the httpx
client leaves the ping running, so every reconnect leaked one, each still
pinging Amazon with the same token. The result was a reconnect loop causing the
disconnects it was reconnecting from. Cancel via alexapy's own `on_close`.

**Amazon's event clock is not safe as a timebase.** `event.at` was used for
`elapsed_time_last_updated` to avoid adding delivery delay. Delivery is under
0.1s, but the clocks are on different machines and nothing keeps them in step,
and Music Assistant extrapolates position forward from that stamp. Use local
time.

**Replayed state is real state from the wrong moment.** On connect the stream
reports the current state of every device, so a paused session's position was
applied to a player that was idle, putting the scrubber halfway through a track
nobody had started. Apply position only when the event says PLAYING.

**`mediaProgress` is milliseconds on push and seconds on the polled API.** Both
measured the same day. Feeding one into the other's units reproduces the
"scrubber never moves" bug at a thousand times the rate.

**A stored SECURE_STRING never comes back to a config action.** The form
receives `this_value_is_encrypted`, which is neither empty nor encrypted, so it
survives every obvious guard: it reached pyotp as a TOTP seed and Amazon as the
account password. Read credentials from `ProviderConfig`, which decrypts.

## A correction, and why it is worth recording

An early capture showed `transport: {next: DISABLED, previous: DISABLED}` and a
`mediaReference` whose `id` was always `"0"`, and this document concluded that
every publish was a one-item queue and that this explained slow skips. **That
was wrong.** A later capture of a multi-track queue showed `next: ENABLED` and
`id: "1"`. The first reading came from a single-track play, and a property of
the test was mistaken for a property of the system.
