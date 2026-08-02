# Ampere provider for Music Assistant

Exposes every Echo device and every Alexa speaker group as a Music Assistant
player. MA composes the queue; the Ampere bridge publishes it; Alexa plays it
as a **native music-skill queue**, so the Alexa app shows real per-track title,
artist, album and art, and next/previous work on the device itself.

Targets **Music Assistant 2.9.10** (`music-assistant-models` 1.1.129.post1).
MA's provider API changes between releases: 2.9.x wants a `Player` subclass
with instance methods, registered by the provider. If you are on something
older or newer, check `music_assistant/models/player.py` before assuming this
loads.

## What it is not

It is not the upstream `alexa` provider, and it is not trying to replace it.
See **Upstream** below. Short version: upstream pushes one stream URL and
flow-modes the queue, so Alexa sees a single opaque file. This hands Alexa the
whole track list, so `requires_flow_mode` is `False`.

## Requirements

The bridge, reachable over HTTPS from the MA container, with:

- the Alexa music skill created, catalog uploaded and enablement cycled
  (see the repo README, "Known gaps"),
- `ADMIN_TOKEN` set,
- `queue_api.bp` registered and the `ext:` branch wired into `resolve_tracks`.

Every track you want to play must exist on the Subsonic server the bridge
streams from. A Spotify or Tidal item sitting in the MA queue has no id the
bridge can resolve and is dropped from the published list, with a warning in
MA's log.

This is a limit of the current implementation rather than of the design. The
bridge's proxy takes any URL; only the id resolution assumes Subsonic. Serving
MA's own sources is planned, and the work it needs is set out in
[PLAN.md](PLAN.md).

## Deployment: bind-mount

Music Assistant loads providers only from inside its own package
(`PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")`; there is no external
provider path, no custom-components directory, no plugin loader). So the
provider is mounted into the container as if it had always been there. The
mount target directory name **must equal the manifest `domain`**, which is
`ampere`.

Docker:

```sh
docker run -d --name music-assistant \
  --network host \
  -v /opt/music-assistant/data:/data \
  -v /opt/ampere/ma_provider:/app/music_assistant/providers/ampere:ro \
  ghcr.io/music-assistant/server:2.9.10
```

### If a change appears not to have taken effect, cycle the provider

Unresolved, 2026-08-02. A change deployed by rewriting the bind-mounted files
and restarting Music Assistant appeared not to be running, and disabling and
re-enabling the provider in MA's own settings made it take effect. A later
change, deployed exactly the same way, **was** live after a plain restart, so
"a restart never reloads" is not the rule.

Whatever the real mechanism, disable-and-re-enable is the cheap thing to try
before concluding that a fix did not work. Do not trust `Loaded player provider
Ampere` in the log as evidence the new code is running: it appears either way.
The reliable check is to have the change log something that only the new build
could log.

Compose:

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:2.9.10
    network_mode: host
    volumes:
      - /opt/music-assistant/data:/data
      - /opt/ampere/ma_provider:/app/music_assistant/providers/ampere:ro
```

Nomad:

```hcl
config {
  image        = "ghcr.io/music-assistant/server:2.9.10"
  network_mode = "host"
  volumes = [
    "/opt/music-assistant/data:/data",
    "/opt/ampere/ma_provider:/app/music_assistant/providers/ampere:ro",
  ]
}
```

Home Assistant OS add-on users cannot do this: the add-on container is not
yours to mount into. Run MA as a container.

Verify the path inside the image before blaming the provider, because a mount
onto a path that does not exist creates it silently and MA then finds a
provider directory it never scanned:

```sh
docker exec music-assistant ls /app/music_assistant/providers/ampere
```

`alexapy` is installed by MA itself from the manifest's `requirements` on first
load. `requirements.txt` here restates the pin for anyone installing by hand.

Restart MA after mounting, then add the provider under Settings -> Providers.

## Configuration

| Key | Meaning |
|---|---|
| `bridge_url` | Public HTTPS base of the bridge, the same host the skill endpoint points at. |
| `admin_token` | The bridge's `ADMIN_TOKEN`. Sent as `X-Admin-Token`. |
| `alias` | The skill's invocation name. Must match the skill manifest. |
| `handoff_phrase` | Must match `MA_HANDOFF_PHRASE` on the bridge. Default `music assistant`. |
| `expose_groups` | Register Alexa speaker groups as players as well as individual Echoes. |
| `url` | Amazon domain, e.g. `amazon.com`. |
| `username` / `password` | Amazon account. |
| `secret` | TOTP **seed** for two-factor, not a six digit code. Blank if 2FA is off. |

The Amazon session is stored at
`{storage_path}/.alexa/alexa_media.{username}.pickle`, which is deliberately
the same file the upstream `alexa` provider uses. Configure both for the same
account and they share one session instead of racing each other into Amazon's
rate limit.

## How playback actually works

1. MA calls `play_media`. The provider reads the whole MA queue from the
   current index on, maps each item to a Subsonic song id through its provider
   mapping, and `POST`s the list to the bridge's `/queue`.
2. The bridge stores it and returns `ext:<token>`.
3. The provider sends the device a text command:
   `ask <alias> to play <handoff phrase>`, plus `on <group name>` when the
   player is a group.
4. Alexa resolves that phrase, the bridge answers `GetPlayableContent` with the
   `ext:<token>` of the queue that was just published, and Alexa runs it as a
   normal queue from there.

### Why a handoff phrase, and what it costs

Alexa resolves the words that were said against the uploaded catalog. An
arbitrary MA queue has no words. Three ways round it were considered:

- **On deck.** Publish, then let the next `Initiate` claim whatever is pending.
  No phrase needed, and wrong the first time two players start at once or the
  user says something else in between. The failure is silent and plays the
  wrong music.
- **One fixed phrase** that always means "the queue that was just published".
  Deterministic, and the user never has to say it themselves because the
  provider says it for them.
- **Catalog identities only**, so MA could ask for an album or a playlist that
  already exists and nothing else. Correct, and gives up the point.

The fixed phrase is what is implemented. Two honest costs:

- **The phrase competes with your library.** Alexa resolves content before it
  routes to a provider, so a phrase that matches an artist or track will be
  eaten by it. This is the same failure that cost "jukebox" (Jukebox The Ghost)
  and "gray tunes" (Conan Gray) their shot at being the alias. Both
  `MA_HANDOFF_PHRASE` and this provider's `handoff_phrase` take a comma
  separated list, so you can move to something your library does not contain.
- **Two players starting in the same second race.** The last publish wins and
  both speakers get the same queue. Starting two different queues on two
  different groups at once is the one case this loses.

### Transport

`NextCommand`, `PreviousCommand`, `PauseCommand`, `PlayCommand` and volume, all
through `alexapy`.

**Seek works, but not through any seek command.** There is no seek in `alexapy`
at all, and `Alexa.SeekController` is a Video API (TV, streaming device, games
console) that does not apply to a speaker. It works by republishing the queue
with a start offset instead; see [Seek](#seek-the-offset-rides-with-the-queue).

Declaring `PlayerFeature.SEEK` is not what exposes the scrubber, either.
`PlayerQueues.seek` never consults the feature set, so the control was live
while the feature was undeclared, and dragging it silently restarted the track.

### Speaker groups

An Alexa speaker group has **no dialog interface**: sending it a text command
does nothing at all. So a group player sends its command to one of its member
Echoes and names the group in the sentence. Any real Echo will do, including a
member of the group itself; an earlier version required a speaker from outside
the group, which was wrong and made a group holding every Echo in the house
look unstartable. That works, and it is better than
the "preferred speakers" workaround usually recommended for this, which is a
static per-room binding you change by hand in the Alexa app. Confirmed:
`ask ampere to play music assistant on whole apartment` took four idle Echoes
to playing at once.

`play X on <alias>` is **not** used, spoken or otherwise. Typed, Alexa reads
`on <name>` as naming a speaker and goes looking for one called Ampere;
`from <alias>` drops the provider and plays from the default service.
`ask <alias> to ...` names the skill outright, which leaves the trailing
`on <target>` free to be read as a target. That is how one sentence both picks
the provider and distributes to four speakers.

### A group must not claim to be powered

MA decides whether a group owns its members in `Player.__final_active_group`:
a group whose `powered` is `True` captures them outright, and only a group
whose `powered` is *not* `True` falls through to `is_active_session`. A captured
member is hidden from the player picker.

So a group sets `powered` to `None`, not `True`, and answers `is_active_session`
from its own playback state. Setting it `True` (which is right for a plain
Echo) made every Echo in Whole Apartment vanish from the picker permanently,
while still appearing under Settings > Players. **That gap is the diagnostic**:
a player missing from the picker but present in settings is being hidden for
being *owned*, not for being unavailable.

`is_active_session` returns true only while **playing**, deliberately against
MA's guidance that a group should also hold its members while paused. That
guidance assumes a group the user can put down, and MA offers this player no
stop control, only pause; holding through pause meant holding until something
else started, which is the same permanent disappearance by a slower route.

### What a cluster device actually reports

Settled 2026-08-02 after three contradictory claims, one of them in this
codebase's own docstring. A speaker group's `serialNumber` **does** answer
`/api/np/player`, and reports real state:

```
group Whole Apartment reports 'PAUSED' -> paused (holding members: False)
```

It is polled like any other device. What it will not accept is a text command,
which is a separate limitation and the reason a group's utterance goes to a
member instead.

An empty payload from a poll is not a report of idleness and must not be
treated as one; only an explicit `IDLE` means idle. Reading empty as idle reset
the group on every poll and discarded the position.

### Seek: the offset rides with the queue

MA has no seek command for a player that does not stream through it.
`PlayerQueues.seek` never consults the feature set at all: it calls
`play_index(..., seek_position=N)`, which puts the offset on the queue item's
streamdetails and feeds MA's own `AudioBuffer`, then calls `play_media` exactly
as an ordinary play does. Ampere has no reader for that buffer, so before this
was handled a seek simply republished the queue and Alexa started it from zero:
the song restarted while MA's clock showed the position dragged to.

Alexa's `Item` schema carries `stream.offsetInMilliseconds`, so the position
travels with the queue the seek republishes. Read the offset from the item
addressed by `media.queue_item_id`, **not** from `queue.current_item`:
`play_index` loads the item first and assigns `current_item` afterwards, so a
seek can read the previous item and find no offset. That failed one seek in a
run of otherwise correct ones.

Only the first item carries the offset. A `GetNextItem` that inherited it would
drop the same interval off every remaining track.

## Known wart: once Alexa is playing, Alexa owns the position

Reordering, adding to or shuffling MA's queue mid-playback is not reflected on
the speaker. The list was handed over at `play_media` and Alexa advances it
itself. MA composes; Alexa plays. Polling reads state back the other way, and
the polled track is matched to an MA queue item by **title**, because Alexa
reports what is playing by name and never by anything we gave it. Duplicate
titles resolve to the first.

This is not fixable from here. Alexa echoes back the `contentId` it was given
at `Initiate` on every later queue request, and an `Item` has no field to hand
back a different one, so the content behind a running queue cannot be swapped.

## Running through Home Assistant instead

If MA already runs beside Home Assistant with the Alexa Media Player
integration set up, the same commands can go through HA services
(`media_player.play_media` with `media_content_type: custom` and the utterance
as `media_content_id`) rather than through `alexapy` here. That avoids a second
Amazon login.

It is not the default because HA only exposes what a `media_player` entity can
express. This needs the raw Amazon device list to tell a Whole Home Audio group
from a speaker and to find a group's members, and it needs the raw
`/api/np/player` payload for position and volume. HA also registers each Echo
more than once and only some of the duplicates are live: in testing
`media_player.kitchen_echo` had no state change in over an hour while its
duplicate answered instantly, which is a class of bug that is very hard to see
from inside MA.

## Upstream

The intent is for this to become a **mode on the existing `alexa` provider by
@alams154**, not a competing provider. It already reuses that provider's auth
(same `alexapy` login, same cookie file), its device discovery (same
`get_devices` call, same `MUSIC_SKILL` capability filter) and its device model.
What differs is one axis:

| | upstream `alexa` | this |
|---|---|---|
| What Alexa receives | one stream URL per queue, pushed to a companion API at `/ma/push-url` | a published track list, served as a music-skill queue |
| `requires_flow_mode` | `True` | `False` |
| Alexa app shows | one entry for the whole session | one entry per track, with its own art |
| Next / previous | not offered | native, handled by Alexa |
| Speaker groups | not exposed | exposed, targeted by name in the utterance |
| Needs | companion API on `:5000` | the Ampere bridge |

A merged provider would keep the login and discovery exactly as they are and
branch at `play_media` on a config entry, something like
`playback_mode: stream | queue`. `requires_flow_mode` becomes
`mode == "stream"`. Nothing about the auth flow, the proxy login helper or the
device model needs to change.
