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

**"Subsonic-only is a hard limit of what the bridge can serve."** Settled and
false, as expected: phases 2 and 3 removed it. Deezer tracks with no Subsonic
id play, seek and report metadata.

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

Phases 1 to 3 are done. Any track Music Assistant can play now reaches an
Echo, whatever provider it comes from, with metadata, artwork, skipping,
seeking and multi-room. Phase 4 (live radio streams) is the next one open.

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

| Phase | Delivers | Size | Risk | Status |
|---|---|---|---|---|
| 1. Play one track | Confirms the premise | none | none | **done** 2026-08-02 |
| 2. MA as source, unbuffered | Group B plays, degraded | 200-400 lines | low | **done** 2026-08-03 |
| 3. Buffering cache | Group B at parity with Navidrome | 400-800 lines, new subsystem | **high** | **done** 2026-08-03 |
| 4. Live streams | Group C | 100-200 lines | low | open |
| 5. Fold bridge into MA | One deployable | 3,195-line Flask to aiohttp port | medium, broad | open |
| 6. Upstream merge | Ships to everyone | negotiation | outside our control | open |

### Phase 1: play one track

No new code. Queue Navidrome tracks in MA, send to a single Echo under the
Ampere provider, then to `Whole Apartment`. Confirms the handoff, the queue
publish, the stream proxy and group distribution in one shot.

### Phase 2: MA as source, unbuffered — done 2026-08-03

Shipped as `ma_provider/stream_route.py` plus the `ext:` queue carrying track
objects as well as Subsonic ids. It answered its three questions, and none of
the answers were what the phase was braced for.

**Does Alexa tolerate a `200`-only, `Content-Length`-less source?** Yes,
completely. A chunked response with `Accept-Ranges: none` played on an Echo
with correct per-track metadata, advanced across track boundaries, and skipped.
A 14-track Deezer album played start to finish this way.

**How often does it send `Range`?** Never, on a first fetch. Zero range
requests across four stream fetches. Ranges come from the two things phase 3
addresses, scrubbing and moving audio between rooms, and from nothing else.

**Does an MA queue session survive a full Alexa queue?** The question turned
out not to apply. It assumed the reference would be one of MA's own
session-scoped stream URLs. Keying the route on the item's **uri** instead
removes sessions from the problem entirely: every track resolves independently
at fetch time, so track twelve resolves as well an hour later as track one did
at the start. The estimate-breaking unknown named under phase 3 was answered by
the shape of phase 2 rather than by any code in phase 3.

The one thing that carried over as a real limit: with no length and no ranges,
seeking had to be withdrawn and a room-to-room move restarted the track.

### Phase 3: buffering cache — done 2026-08-03

Shipped as `mastream_cache.py`. Much smaller than estimated, because the design
that survived is the simple one.

The plan called for lazy buffering with a partial-object server and prefetch
depth tuning, and rated it the highest-risk phase. One measurement collapsed
it: **Music Assistant produces a complete track in about 4.5 seconds**, first
byte in 0.1.

```
bytes=9058264  total=4.726s  firstbyte=0.206s
bytes=8503423  total=4.376s  firstbyte=0.101s
```

At that speed there is no reason to serve a partial object at all. Buffer the
whole track to disk, then hand Alexa an ordinary file that answers ranges
exactly. Everything intricate in the original plan existed to avoid a wait that
is a few seconds, and is usually zero because publishing runs ahead of the
utterance and prefetching starts there.

Measured over the public path once buffered:

```
HTTP/2 200   accept-ranges: bytes   content-length: 12815717
HTTP/2 206   content-range: bytes 5000000-12815716/12815717
```

Which is what Navidrome has always done, and is why Subsonic tracks always
behaved. A Deezer track now seeks: sought to 120s, read back 140s twenty
seconds later.

Still open from the original phase 3 list: nothing. Eviction is by size and
age, prefetch is bounded, and the session question dissolved in phase 2.

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
