# Plan: Ampere as Music Assistant's Alexa playback path

Status as of 2026-08-02. This is a design record and a phased plan, not a
description of shipped behavior. Where a claim has been measured it says so;
where it is reasoning it says that too. That distinction matters here, because
this plan exists partly to correct three things that were written down as
findings and were actually assumptions.

## The short version

Music Assistant already has an Alexa player provider. It works, and it cannot
show per-track metadata, cannot skip, and does not expose speaker groups. Those
three gaps come from one root cause: it is a **custom skill** driving
`AudioPlayer` over a single flow-mode stream.

Ampere is a **music skill**, which is a different Amazon product with a
different capability set. It has all three of those things working today. What
it does not have is Music Assistant's source coverage, because it streams from
Subsonic rather than from MA.

The plan is to close that gap from the Ampere side, in phases, and to end up as
a playback mode on the upstream provider rather than a competing one.

## What is verified

Each of these was read out of running code or measured on a live deployment.

**Upstream is a custom skill.** Its intents are `AMAZON.PauseIntent`,
`AMAZON.ResumeIntent`, `AMAZON.StopIntent`. `play_media` resolves one flow-mode
stream URL, POSTs it to a companion API at `/ma/push-url`, then fires an
utterance so the custom skill plays it via `AudioPlayer`.

**Upstream's setup is heavier than Ampere's**, per its own documentation:
create a custom skill by hand in the Amazon developer console (add the
`PlayAudio` intent, enable AudioPlayer and APL, build the model), run a separate
Docker Compose service, and reverse-proxy TLS for two endpoints. Ampere creates
its skill through the SMAPI REST API from the wizard.

**Multi-room works through the music skill, and it is native distribution.**
`ask ampere to play music assistant on whole apartment` took four idle Echoes to
playing at once. Bandwidth confirms Alexa fetches once and distributes locally:
four Echoes playing for an hour transferred about 115 MB from the bridge, not
four times that.

**The MA handoff never touches the catalog.** In `handle_get_playable_content`
the handoff phrase is resolved and returned before entity resolution runs.

**The bridge's proxy is source-agnostic.** `_proxy(upstream, content_type)`
takes any URL, forwards `Range`, and passes through status and `Content-Range`.
The only Subsonic coupling in the stream path is one call,
`subsonic.stream_url(song_id)`.

**Providers can serve HTTP routes from inside MA.**
`mass.webserver.register_dynamic_route(path, handler, method)` is public API,
and upstream already uses it for its auth proxy.

**MA serves audio as a realtime stream, not a file.** Even the per-track
endpoint:

```python
headers = {
    **DEFAULT_STREAM_HEADERS,
    "icy-name": queue_item.name...,
    "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
}
resp = web.StreamResponse(status=200, reason="OK", headers=headers)
```

Always `200`, never `206`. No `Content-Length`, no `Accept-Ranges`.

**MA stream URLs are session-scoped.**

```
/single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}
if queue.session_id and session_id != queue.session_id:
    raise web.HTTPNotFound(...)
```

**Alexa uses byte ranges.** Observed in the reverse-proxy log during real
playback: `Range: bytes=5951231-`, `Range: bytes=287859-`. Ampere works today
because Navidrome serves seekable files that answer them.

**Transport is API calls, not utterances.** `next_track`, `pause`, `stop`,
`volume_set` all call `alexapy` directly. Only starting a queue is an utterance,
because that is the only way to invoke a skill. Upstream, by contrast, sends a
text command for stop, play and pause.

## What is assumed, and was previously miswritten as fact

**"Custom skills using AudioPlayer cannot target multi-room groups."** This
appears in `docs/setup/what-this-is.md` stated flatly. It has never been tested
here. It traces to Amazon's documentation, which this project has already
proven wrong on exactly this subject: Amazon's own help pages say multi-room
"will not stream audio from Alexa skills", and it demonstrably does. Treat the
custom-skill limitation as **open**, not settled.

Testing it is not cheap. Upstream's companion API has never been deployed on
this box, so that provider has never played audio here. Settling the question
means standing up the prototype service and creating a custom skill.

**"Subsonic-only is a hard limit of what the bridge can serve."** In
`ma_provider/README.md`. True of the current code, false of the architecture.
The proxy is generic; the coupling is one line.

**A stub catalog was considered and rejected.** The idea was to drop
`catalog_sync.py` and upload a single placeholder entity, since the handoff
bypasses the catalog. It does work for MA-driven playback, but it silently
removes voice-first library requests, because entity resolution has nothing to
resolve against. Catalog contents and stream source are independent axes and
should not be traded against each other.

## Current state

Working today, on the deployed instance:

- Provider loads against MA 2.9.9 (`music-assistant-models` 1.1.129.post1)
- Login succeeds by reusing the upstream provider's cookie
- Every Echo registered, plus `Whole Apartment` as `type=group`
- Bridge reachable from the MA container

Not yet done: **nobody has played a track through it.** That is phase 1.

## Target architecture

```
MA composes a queue
   |
   +-- provider publishes the track list  -->  bridge  -->  ext:<token>
   |
   +-- provider utters "ask <alias> to play <phrase> on <target>"
   |
   +-- Alexa GetPlayableContent, handoff phrase matches, returns ext:<token>
   |
   +-- Alexa Initiates, then pulls each item's stream URL from the bridge
```

Unchanged from today. What changes is what sits behind the stream URL.

## How each provider class behaves

MA ships 47 music providers. The discriminator is not the service, it is
whether the audio is finite and seekable.

| Group | Providers | Bridge work | Result |
|---|---|---|---|
| **A. Finite, seekable at source** | `filesystem_*` `webdav` `opensubsonic` `plex` `jellyfin` `emby` `audiobookshelf` `qobuz` `bandcamp` `internet_archive` `phishin` `nugs` `podcast_index` `podcastfeed` `itunes_podcasts` `gpodder` | Minimal. Can proxy the source directly, as with Navidrome today | Full: art, seek, skip, multi-room |
| **B. Finite, MA must decode** | `spotify` `tidal` `apple_music` `deezer` `ytmusic` `soundcloud` `qqmusic` `neteasecloudmusic` `yandex_music` `zvuk_music` `kion_music` `musicme` `ibroadcast` `nicovideo` `yousee` `digitally_incorporated` `audible` | **The entire cache.** Pull once, buffer to a complete object, serve with `Content-Length` and `206` | Full, with first-play latency |
| **C. Infinite live** | `radiobrowser` `tunein` `somafm` `radioparadise` `siriusxm` `bbc_sounds` `nts` `pandora` `motherearthradio` `orf_radiothek` `ard_audiothek` | None, and buffering must be **skipped** | Station semantics: plays, multi-room, no seek or duration |

Group A is where Navidrome already lives. Group B is the entire reason the
buffering work exists. Group C is counter-intuitively the easiest, because a
live stream never needed seeking, so MA's realtime output is already correct.

The grouping is reasoned from how those services deliver audio, not measured.
`audible` and `siriusxm` in particular should be checked before being trusted.

## Phases

| Phase | Delivers | Size | Risk |
|---|---|---|---|
| 1. Play one track | Confirms the premise | none | none |
| 2. MA as source, unbuffered | Group B plays, degraded | 200-400 lines | low |
| 3. Buffering cache | Group B at parity with Navidrome | 400-800 lines, new subsystem | **high** |
| 4. Live streams | Group C | 100-200 lines | low |
| 5. Fold bridge into MA | One deployable | 3,195-line Flask to aiohttp port | medium, broad |
| 6. Upstream merge | Ships to everyone | negotiation | outside our control |

### Phase 1: play one track

No new code. Queue Navidrome tracks in MA, send to a single Echo under the
Ampere provider, then to `Whole Apartment`. Confirms the handoff, the queue
publish, the stream proxy and group distribution in one shot.

### Phase 2: MA as source, unbuffered

Publish MA item references instead of, or alongside, Subsonic ids. Branch
`stream()` on which kind of id it holds. Resolve to an MA stream URL **at fetch
time**, never at publish time, matching how `subsonic.stream_url(song_id)` is
already called lazily.

Deliberately ships degraded: no seek, and vulnerable to MA's session scoping.
Its real purpose is to answer empirically what phase 3 must handle. Treat it as
a timeboxed spike whose code may be thrown away.

Questions it should answer:

- Does Alexa tolerate a `200`-only, `Content-Length`-less source at all?
- How often does it actually send `Range` in practice?
- Does an MA queue session survive a full Alexa queue?

### Phase 3: buffering cache

The real work, and the piece most likely to overrun.

- Pull each track from MA once, buffer to a complete seekable object
- Serve it with `Content-Length`, `Accept-Ranges` and correct `206` responses
- Pre-fetch far enough ahead that Alexa never waits
- Evict on a size or age bound
- Survive MA's queue session rolling underneath a live Alexa queue

That last point is the estimate-breaking unknown. Alexa owns its queue for
hours and may fetch track 12 long after track 1. Eager buffering solves it but
does not scale: a 50-track queue would mean 50 transcodes up front. So this
needs lazy buffering plus either keeping MA's session alive for the life of the
Alexa queue, or re-resolving items when the session changes. Neither has been
prototyped.

### Phase 4: live streams

Detect infinite sources and route them around the cache entirely. Needs a
station content type distinct from Ampere's existing `rad:`, which today means
"artist radio built from your library" rather than an external stream.

### Phase 5: fold the bridge into MA

The bridge is 3,195 lines of Flask across 15 route handlers. MA is aiohttp, so
this is a framework port rather than a move. The 3,668-line setup wizard would
not be ported; MA's own config flow replaces it.

Worth doing only after 2 to 4 have proven the design. It buys one deployable
instead of two and direct file access for local providers, at the cost of a
broad mechanical rewrite.

### Phase 6: upstream merge

Target shape: one provider, two playback modes, branching at `play_media`,
sharing auth, discovery, the device model and the `run_custom` channel.

The pitch is not "here is a competing provider" and not even "here is a mode
that adds features." It is: **this mode removes your prototype service and your
manual console setup, and adds metadata, skipping and groups, while keeping
flow mode for gapless.**

Flow mode should stay. It is not an accident: mixing the queue into one stream
is how MA delivers gapless playback, crossfade and its volume normalization.
Handing Alexa discrete tracks bypasses all of that. The two modes trade
honestly:

| Flow mode | Queue mode |
|---|---|
| gapless, crossfade, MA DSP | per-track metadata and art |
| one opaque entry | native skip, next, previous |
| no groups | multi-room groups |

## What not to do

- **Do not ship standalone alongside upstream.** Two providers means two
  logins, every Echo listed twice, and no clear answer to which one a player
  belongs to. This was tried on 2026-08-02 and is measurably confusing.
- **Do not use a stub catalog.** It trades away voice-first library requests to
  solve a problem that does not exist.
- **Do not treat the custom-skill multi-room limitation as settled.** It is
  untested here and the documentation it came from has already been wrong once.

## Open questions

- Does a music skill bind at all with no ingested catalog? `ER_INGESTION:
  SUCCEEDED` is documented as the only gate, but that was measured with a full
  library loaded.
- Can a custom skill reach multi-room groups with the same utterance trick?
- Why does the Alexa app render no scrubber, despite `ADJUST/SEEK_POSITION`
  being declared with a real duration over a `206`-capable stream? Open in
  `docs/reference/limits.md` and unaffected by any of this work.
- Is the binding decay interval account-specific? `BINDING_KEEPALIVE_HOURS`
  exists so finding out needs no fork, but there is still one data point.

## Where the value actually is

The value curve is steeply front-loaded, and the steep part is already behind
us. Multi-room and per-track metadata, the two things upstream cannot do at
all, work today with Navidrome. Everything from phase 2 onward buys **source
coverage**, not capability.

That is worth having. It is also a different and much more expensive kind of
win, and it is worth being deliberate that phase 3 trades roughly a week of
work for Spotify rather than for the feature that made this interesting.
