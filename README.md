# Ampere

An Alexa **Music Skill** (not a custom skill) that plays a self-hosted music
library on Echo devices.

> "Alexa, play Radiohead on ampere."

Speaks the plain Subsonic API (1.16.1, no OpenSubsonic extensions), so it
should work with any Subsonic-compatible server: Navidrome, Airsonic,
Airsonic-Advanced, Gonic, LMS, Ampache, Funkwhale, Nextcloud Music, Astiga.
Tested against Navidrome.

Because it is a Music Skill rather than a custom skill, you say
`play <thing> on ampere` instead of `ask <skill> to play <thing>`, and you get
Alexa's native player: a real queue, per-track metadata and art, working
next/previous, shuffle, loop and repeat.

## Saying it

**Spoken and typed commands do not resolve the same way**, and the phrasing
that works out loud is not the phrasing that works from an automation. This
caught us for hours, so it is the first thing documented.

Substitute your own alias for `ampere` throughout.

### Out loud, to a device

```
"Alexa, play Gregory Alan Isakov on ampere"
"Alexa, play the bedtime playlist on ampere"
"Alexa, play jazz on ampere"
"Alexa, play Gregory Alan Isakov station on ampere"     <- a station
```

Spoken, Alexa resolves the name against the uploaded catalog and sends
`entityId` plus, for a station, `MEDIA_TYPE: STATION`. The provider slot binds
correctly and everything works.

### From Home Assistant, or a Routine's custom-command action

**`on <alias>` does not work here. Use `ask <alias> to ...` instead.**

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo      # any Echo that responds
data:
  media_content_type: custom
  media_content_id: "ask ampere to play Gregory Alan Isakov"
```

Append a device or group name to target it, which is how multi-room is reached:

```
"ask ampere to play Gregory Alan Isakov on whole apartment"
```

Confirmed multi-room: four Echoes went from idle to playing simultaneously on
one such command. The group name is part of the utterance, so an automation can
pick any device or group per call. That is better than the "preferred speakers"
workaround usually recommended for this, which is a static per-room binding
changed by hand in the Alexa app.

### What fails, and why

| Phrasing | Channel | Result |
|---|---|---|
| `play X on ampere` | spoken | works |
| `play X on ampere` | typed | Alexa hunts for a **speaker** named Ampere |
| `play X from ampere` | typed | provider dropped, plays from the default service |
| custom command in a Routine | typed | same as above |
| Music action in a Routine | n/a | the skill is **not in the provider list** at all |
| `ask ampere to play X` | typed | **works** |

`play X on <name>` is ambiguous: `<name>` could be a provider or a speaker.
Spoken, Alexa has enough context to pick provider. Typed, it picks speaker. The
`ask <name> to ...` form names the skill outright, leaving the trailing
`on <device>` free to be read as a target, which is why both jobs can be done
in one sentence.

### Targeting the right entity

Home Assistant often registers each Echo more than once. Only some are live:
in testing, `media_player.kitchen_echo` had no state change in over an hour
while its duplicate responded instantly. Fire an `announce` at a candidate and
watch the logbook before building anything on it. Sending to a Whole Home Audio
group entity directly does nothing, because a group has no dialog interface.

## Why this exists

Alexa custom skills using the `AudioPlayer` interface **cannot target
multi-room music groups**, and a Whole Home Audio group has no dialog interface,
so it cannot be spoken or written to directly.

Amazon's separate **Music, Radio, and Podcast Skill API** gives you Alexa's
native player instead: a real queue, per-track metadata, working transport
controls. Music skills are self-service, with no Amazon representative and no
certification needed for private development-stage use.

**Multi-room does work**, which is worth stating plainly because the
documentation says otherwise. Amazon's own help pages say multi-room "will not
stream audio from Alexa skills", and My Media for Alexa's support page says
"Amazon does not natively support multiroom for third party skills ... or any
others". Both appear to describe voice-targeted MRM. Invoking the skill by name
and naming a group in the same utterance does distribute:

```
"ask ampere to play Gregory Alan Isakov on whole apartment"
```

Four Echoes went from idle to playing on one such command. See **Saying it**.

## Findings that shaped the design

- **Music skills accept an HTTPS endpoint, not just a Lambda ARN.** Amazon's
  music-skill docs only show a Lambda ARN, but SMAPI rejects the manifest with
  `MISSING_REQUIRED_PROPERTY: sslCertificateType`, a field that only applies to
  HTTPS endpoints. No AWS account is required.
- **`stream.validUntil` defaults to roughly 60 seconds** when omitted. Always
  set it explicitly.
- **`GetItem.Response` uses namespace `Alexa.Audio.PlayQueue`** even though the
  directive arrives on `Alexa.Media.PlayQueue`.
- **Alexa carries queue position for you.** Every queue request echoes back
  `{id, queueId, contentId}`, so the service stores no per-user playback state.
  This matters: `Initiate` has a 100ms p50 / 400ms p99 budget.
- Navidrome is tailnet-only and not internet-routable, so audio and cover art
  are proxied through this service behind expiring HMAC-signed URLs rather than
  handing Amazon a permanent credentialed link.
- **`getArtistInfo2` is affordable, `getSimilarSongs2` is not.** Similar
  artists come back in ~725ms; similar *songs* take ~10s, every time, uncached,
  which is why they looked unsupported until someone noticed the requests were
  simply exceeding `SUBSONIC_TIMEOUT`. Stations are built from similar artists
  for that reason alone. `getTopSongs` returns nothing at all here, but that is
  a fact about `getTopSongs` and not about Last.fm-backed endpoints generally.
- **Alexa echoes the contentId it was given at `Initiate` on every later
  request**, and an `Item` has no field to hand back a different one. Content
  therefore cannot be swapped mid-queue; a queue that continues past its end
  has to extend itself instead.

## Architecture

```
Echo -> Amazon -> alexa-music.graysons.network -> bridge -> Navidrome (tailnet)
                                                    |
                                          signed, expiring /stream + /art
```

`contentId` encodes what to play (`tr:`, `al:`, `ar:`, `pl:`, `gen:`, `star:`,
`rnd:`, `rad:`), and the track list is re-derived from it on each request, with
a small in-memory cache.

`rad:<artist-id>` is a station: the seed artist plus the similar artists that
exist in the library, capped per artist and shuffled by queueId. It has no end.
Indexes past the pool are further passes over it, reshuffled on the pass
number, so any index is still reproducible from the contentId alone.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AFTER_CONTENT` | `stop` | What plays once the requested content runs out: `stop`, `artist`, `genre`, `library`, `radio`. Anything but `stop` extends the queue past its last track rather than replacing it, seeded from the request itself (`ar:`/`rad:` name their own seed, `tr:` uses its track, collections use their last track). Never pre-empts what was asked for. |
| `RADIO_ARTISTS` | `12` | How many similar artists a station draws on. |
| `RADIO_TRACKS_PER_ARTIST` | `12` | Per-artist cap, so a prolific seed artist is not most of their own station. |
| `STREAM_TTL` | `43200` | Lifetime of a signed stream URL, in seconds. |
| `SUBSONIC_TIMEOUT` | `6` | Per-call Subsonic timeout, in seconds. |

## Routes

| Route | Purpose |
|---|---|
| `POST /music` | Alexa directives |
| `GET /stream/<id>/<expires>/<sig>` | Transcoded MP3 proxy |
| `GET /art/<id>/<expires>/<sig>` | Cover art proxy |
| `GET /icons/<name>` | Skill icons for the manifest |
| `GET /healthz` | Liveness |
| `GET /captures`, `GET /diag` | Introspection, `X-Admin-Token` required |

## Deploy

Nomad job `alexa-music` on the Hetzner box, digest-pinned, host network on
`:5056`. Secrets live in Nomad var `nomad/jobs/alexa-music` and in the
1Password MCP vault under `hetzner/alexa-music/*`.

```sh
rsync app.py subsonic.py Dockerfile root@box:/opt/alexa-music/src/
docker build --provenance=false --sbom=false -t localhost:5000/alexa-music:probe .
docker push localhost:5000/alexa-music:probe   # then pin the digest in the job
nomad job run /opt/nomad/alexa-music.nomad.hcl
```

## Known gaps

- **No inbound request verification.** Amazon does not document request auth for
  music-skill HTTPS endpoints, and the custom-skill replay check binds to a
  `request.timestamp` field the music envelope does not contain. `POST /music`
  currently accepts any caller.
- **The Alexa app will not render a scrubber**, despite the item declaring
  `{"type": "ADJUST", "name": "SEEK_POSITION", "enabled": true}` alongside a
  known `durationInMilliseconds` and a stream that answers `206` with
  `Content-Range` end to end. First-party providers do show one. Undeclared
  controls are certainly disabled controls, but declaring this one is evidently
  not sufficient, and no cause has been established.
- **Uploading a catalog silently unbinds the skill.** Playback falls back to the
  default provider and Alexa announces it ("Here's ... from Spotify") while the
  skill still answers every request correctly and `ER_INGESTION` reports
  `SUCCEEDED`. Fix is to cycle enablement:
  `ask smapi delete-skill-enablement` then `set-skill-enablement`, stage
  `development`. The catalog sync does not do this automatically yet, so adding
  music can break voice playback until it is done by hand.
- **The alias must not collide with your own catalog.** Every track and artist
  uploaded competes for the alias word, because Alexa resolves content before it
  routes to a provider. "jukebox" was eaten by Jukebox The Ghost and Juke Box
  Hero; "gray tunes" became the artist Conan Gray. Check a candidate against the
  library before committing to it. The alias also has to survive ASR: "phono"
  was consistently heard as "Sonos".
- **Music skills cannot be tested without a device.** `ask smapi simulate-skill`
  refuses with "Unsupported skill type. Please note that only custom skills are
  currently supported", and the developer console Test tab and utterance
  profiler are custom-skill only. Every diagnosis has to come from inbound
  request captures plus what Alexa says out loud.
