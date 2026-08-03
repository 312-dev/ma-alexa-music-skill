# Music Assistant API commands for a live player conformance suite

Everything here was read out of the running server, not remembered.

| what | value |
|---|---|
| MA version | `2.9.9` (`music-assistant-models` `1.1.129.post1`) |
| API schema version | `31` (min supported `28`) |
| source read | `/app/venv/lib/python3.14/site-packages/music_assistant/…` inside container `app-<alloc>` on `hetzner` |
| machine-readable index | `GET http://127.0.0.1:8095/api-docs/commands.json` (unauthenticated, inside the container) — the authoritative parameter list, generated from the same `@api_command` signatures |

Re-derive the schema at any time:

```bash
A=$(ssh hetzner 'nomad job allocs -json music-assistant | python3 -c "import json,sys; print([x[\"ID\"] for x in json.load(sys.stdin) if x[\"ClientStatus\"]==\"running\"][0])"')
ssh hetzner "docker exec app-$A /app/venv/bin/python -c \"
import urllib.request,sys
sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8095/api-docs/commands.json').read().decode())\""
```

---

## 0. Transport

### Websocket (what `tools/ma_cli.py` uses, and what a latency suite must use)

`ws://127.0.0.1:8095/ws` from inside the container.

1. Server sends `ServerInfoMessage` immediately on connect (`server_id`, `server_version`,
   `schema_version`, `min_supported_schema_version`, `base_url`, `status`). Consume it first.
2. Client sends `{"message_id": "0", "command": "auth", "args": {"token": "<jwt>"}}`.
3. Every subsequent call is `{"message_id": "<str>", "command": "<name>", "args": {...}}`.
   `message_id` is a **string** and responses are correlated by it. Responses may arrive
   interleaved with events, so match on `message_id`, never on arrival order.

Response shapes (`music_assistant_models/api.py`):

```jsonc
{"message_id": "1", "result": <any>, "partial": false}       // SuccessResultMessage
{"message_id": "1", "error_code": 12, "details": "…"}        // ErrorResultMessage
{"event": "queue_updated", "object_id": "<id>", "data": {…}} // MassEvent
```

`partial: true` is emitted for async-generator commands that exceed 500 items; none of the
player/queue commands below do this, but a client that assumes one response per `message_id`
will desync if it is ever pointed at a library listing.

**Error codes** (`music_assistant_models/errors.py`) — the ones a player suite will hit:

| code | exception | typical cause |
|---|---|---|
| 8 | `QueueEmpty` | `player_queues/resume` on an empty queue |
| 9 | `UnsupportedFeaturedException` | feature not declared by the player |
| 10 | `PlayerUnavailableError` | queue/player id not registered |
| 11 | `PlayerCommandFailed` | source cannot do this action |
| 12 | `InvalidCommand` | queue not active, seek out of range, no duration |
| 999 | anything non-`MusicAssistantError` | unhandled provider exception |

### HTTP JSON-RPC (single-shot, no events)

`POST /api` with `Authorization: Bearer <token>` and body
`{"message_id": "1", "command": "players/all", "args": {}}`. Returns the bare result as JSON
(**not** wrapped in a `SuccessResultMessage`), `400` on unknown command, `401` unauthenticated,
`500` on any handler exception with the body text `Internal server error` — the error class and
message are **lost**. Use the websocket for anything that needs to assert on failure modes.

### Auth

Tokens are minted by `tools/ma_token.sh` (writes `/opt/ampere/.ma-token`, stages it into the
container at `/tmp/.ma-token`, never prints it). Minting **stops and restarts MA** because
`auth.db` is opened with `PRAGMA locking_mode=exclusive`; a valid JWT without a matching
`auth_tokens` row is refused. Tokens expire after 6 hours — a suite run should mint once up
front, not per test. Every command below has `authenticated: true`; none require `admin` except
`players/create_group_player`, `players/remove_group_player`, `players/remove`.

### queue_id == player_id

`PlayerQueues._get_queue` does `queue_id = player.player_id`. There is one queue per player and
they share an identifier. Ampere player ids are `<provider_instance_id>:<amazon_serial>` (see
`AmpereAlexaProvider.discover_players`), which is what `tests/live/safety.serial_of` splits on.

---

## 1. Play a media item — `player_queues/play_media`

```jsonc
{
  "queue_id": "<str>",                 // required
  "media": "<uri>" | <MediaItem> | [ … ],  // required; str uri, MediaItem/ItemMapping dict, or a list of either
  "option": "play|replace|next|replace_next|add",  // optional, default null
  "radio_mode": false,                 // optional, default false
  "start_item": "<uri>" | <MediaItem>, // optional; where in a playlist/album to start
  "username": "<str>",                 // optional; attributes playback history to another user
  "sort_by": "<str>"                   // optional; orders tracks before start_item is applied
}
```

`media` accepts `Artist | Album | Track | Radio | Playlist | Audiobook | Podcast |
PodcastEpisode | Genre | AudioSource | ItemMapping | str`, or an array of those. A plain URI
string (`library://track/123`, `deezer://track/…`, `subsonic://track/…`) is resolved through
`mass.music.get_item_by_uri`. **An item that fails to resolve is logged and skipped, not
raised** — `play_media` can therefore return success having queued nothing. A conformance test
must assert on the resulting queue contents, never on the absence of an error.

`option` — `QueueOption` (`music_assistant_models/enums.py`):

| value | semantics |
|---|---|
| `play` | insert at the current position and start playing |
| `replace` | clear the queue, load, play from index 0 |
| `next` | insert after the current/buffered item |
| `replace_next` | replace whatever follows the current/buffered item |
| `add` | append (to the end, when shuffle is off) |
| `unknown` | fallback member; `QueueOption._missing_` maps any unrecognised string here rather than raising |

Omitting `option` (or sending `null`) does **not** mean `play`. `_handle_play_media` looks up a
per-media-type core config key — `default_enqueue_option_<media_type>`, or
`default_enqueue_option_live_sources` for `radio` and `audio_source` — and uses that. A
deterministic test must always pass `option` explicitly.

`radio_mode: true` puts the given items into `queue.radio_source` instead of expanding them,
and MA continuously fills the queue with similar tracks. It requires a music provider with
`ProviderFeature.SIMILAR_TRACKS`. It makes the queue effectively infinite — do not assert on
`queue.items` being stable under radio mode.

Side effects worth knowing: `option=replace` clears the queue before loading; any option other
than `add`/`next` also clears `queue.enqueued_media_items` and overwrites `queue.radio_source`.

---

## 2. Transport: pause / play / play_pause / stop / resume

Two parallel command families. **They are not independent.**

| action | queue command | player command |
|---|---|---|
| stop | `player_queues/stop {queue_id}` | `players/cmd/stop {player_id}` |
| play (unpause) | `player_queues/play {queue_id}` | `players/cmd/play {player_id}` |
| pause | `player_queues/pause {queue_id}` | `players/cmd/pause {player_id}` |
| toggle | `player_queues/play_pause {queue_id}` | `players/cmd/play_pause {player_id}` |
| resume | `player_queues/resume {queue_id, fade_in?}` | `players/cmd/resume {player_id, source?, media?}` |

`players/cmd/stop`, `players/cmd/pause`, `players/cmd/seek`, `players/cmd/next` and
`players/cmd/previous` **redirect to the queue controller** whenever `get_active_queue(player)`
returns a queue, which for an Ampere player it always does (Ampere leaves `active_source` at
`None`, so `active_source or player_id` resolves to the player's own queue, and
`queue.active = player.active_source in (queue_id, None)` is `True`). So the two families
converge on the same code path; testing both is testing the redirect, not two behaviours.

Asymmetries that matter:

- `players/cmd/play` is **not** a plain unpause. If the player is already `playing` it logs and
  returns. If the player is not `paused` it calls `player_queues/resume` (a restart from the
  resume position), not `play`.
- `player_queues/pause` starts a 30-second watchdog: a player left paused for 30s is
  automatically stopped. A test that pauses and then waits will observe an unrequested
  transition to `idle`.
- `player_queues/resume` with `fade_in` unset auto-enables fade-in when the player is `idle`
  and has been so for >60s. It raises `QueueEmpty` (code 8) on an empty queue. Resuming while
  already `playing` re-seeks to the current position rather than erroring.
- `player_queues/stop` snapshots `queue.resume_pos` from the corrected elapsed time before
  stopping, and cancels any pending `play_index`/preload timers.

---

## 3. Next / previous track

| action | queue command | player command |
|---|---|---|
| next | `player_queues/next {queue_id}` | `players/cmd/next {player_id}` |
| previous | `player_queues/previous {queue_id}` | `players/cmd/previous {player_id}` |

Both raise `InvalidCommand` (12) if the queue is not active, and return silently if
`current_index` is `None`.

Timing traps for a latency suite:

- Both **debounce by 1 second**: they update `queue.current_index` / `current_item` and signal
  `queue_updated` immediately, then schedule the actual `play_index` via
  `mass.call_later(1, …, task_id="queue_play_index_<queue_id>")`. The event fires ~instantly;
  audio changes ~1s later. Measure them separately.
- A second `next` inside that window replaces the pending call rather than skipping two tracks.
- `previous` restarts the **current** track if `queue.elapsed_time >= 5`, and only steps back an
  index when elapsed < 5s. Assert against elapsed time, not against a fixed index delta.

---

## 4. Seek, and how rewind is expressed

```jsonc
player_queues/seek  {"queue_id": "<str>", "position": <int seconds>}   // position default 10
players/cmd/seek    {"player_id": "<str>", "position": <int seconds>}  // position required
```

There is **no rewind command**. Rewind is:

```jsonc
player_queues/skip  {"queue_id": "<str>", "seconds": <int>}   // default 10; NEGATIVE = rewind
```

`skip` is literally `seek(queue_id, elapsed_time + seconds)`, so `{"seconds": -15}` is a
15-second rewind. `seek` clamps `position` to `>= 0` and raises `InvalidCommand` (12) when:
the queue is not active, no item is loaded, the current item has **no duration** (live radio),
or `position > duration`. `seek` is implemented as `play_index(queue_id, current_index,
seek_position=position)` — i.e. a seek re-issues `play_media` to the player.

---

## 5. Shuffle and repeat

```jsonc
player_queues/shuffle {"queue_id": "<str>", "shuffle_enabled": <bool>}
player_queues/repeat  {"queue_id": "<str>", "repeat_mode": "off|one|all"}
```

`RepeatMode` values: `off`, `one`, `all`, plus the fallback member `unknown`. An unrecognised
string is coerced to `unknown` by `RepeatMode._missing_` — it does **not** raise, and
`unknown` is then stored on the queue verbatim. A test asserting on rejection of a bad repeat
mode will fail; assert on the stored value instead.

Both are no-ops when the value already matches. `shuffle` only reshuffles the items *after*
`index_in_buffer`/`current_index`; turning shuffle off re-sorts the remainder by `sort_index`.
`repeat` re-enqueues the next item if the queue is playing and has buffered the current index.

Related, same family: `player_queues/dont_stop_the_music {queue_id, dont_stop_the_music_enabled}`
raises `UnsupportedFeaturedException` (9) if no provider supports `SIMILAR_TRACKS`.

---

## 6. Per-player volume and mute

```jsonc
players/cmd/volume_set  {"player_id": "<str>", "volume_level": <int 0..100>}
players/cmd/volume_up   {"player_id": "<str>"}
players/cmd/volume_down {"player_id": "<str>"}
players/cmd/volume_mute {"player_id": "<str>", "muted": <bool>}
```

`volume_up`/`volume_down` use a **variable step**, not a fixed one:

| current level | step |
|---|---|
| `<10` or `>90` | 1 |
| `<30` or `>70` | 2 |
| otherwise | 3 |

and clamp to 0..100. If the target player is `PlayerType.GROUP` they transparently redirect to
`cmd_group_volume_up` / `cmd_group_volume_down`.

`volume_mute` resolves through `player.mute_control`:
- `native` (player declares `PlayerFeature.VOLUME_MUTE`) → forwarded to the player.
- `fake` (configured) → stores the previous volume and sets volume 0, restoring it on unmute.
- a control/protocol-player id → forwarded there.
- `none` → **falls off the end of the function and silently does nothing.** No error. See §11.

`volume_set` raises `UnsupportedFeaturedException` only when `volume_control == "none"`.

---

## 7. Group volume — `players/cmd/group_volume`

```jsonc
players/cmd/group_volume      {"player_id": "<str>", "volume_level": <int 0..100>}
players/cmd/group_volume_up   {"player_id": "<str>"}
players/cmd/group_volume_down {"player_id": "<str>"}
players/cmd/group_volume_mute {"player_id": "<str>", "muted": <bool>}
```

How it differs from `players/cmd/volume_set`:

- **Target resolution.** `group_volume` accepts a group player, a sync leader, or a sync child
  (which it redirects to its leader). Given a plain ungrouped player it degrades to
  `cmd_volume_set` — so a group-volume test on a non-group player silently tests per-player
  volume.
- **It is not a broadcast.** `set_group_volume` interpolates: it caches a snapshot of every
  powered child's volume on the group player (`extra_attributes` key
  `group_volume_snapshot`), takes `base_group = max(snapshot.values())`, and moves each child
  from its snapshot value toward 100 (scaling up) or toward 0 (scaling down), preserving
  relative balance. **Setting a group to 50 does not set any member to 50** unless they were
  all equal beforehand. Assert on the group's reported `group_volume`, not on member levels.
- **The reported group volume is the MAX of powered children**, not the mean (`Player.group_volume`).
- **The snapshot is stateful.** Any per-player `volume_set` on a member invalidates it
  (`_invalidate_group_volume_snapshot`), as does a membership change. A suite that mixes member
  volume writes with group volume writes is testing a moving reference point; reset by setting
  each member explicitly before each group-volume case.
- Children with `volume_control == "none"` and unpowered children are excluded entirely.
- `group_volume_up`/`down` use the same variable step table as §6, applied to `group_volume`.
- `group_volume_mute` fans out `cmd_volume_mute` to every powered member (including self).

Serialization gotcha: `Player.__post_serialize__` rewrites `group_volume: null` to `0` on the
wire for HA backwards compatibility. **`group_volume == 0` is ambiguous** — it means either
"muted/zero" or "no child supports volume". Do not assert `group_volume > 0` as a proxy for
"volume control works".

---

## 8. Reading queue state

```jsonc
player_queues/all              {}                                        -> PlayerQueue[]
player_queues/get              {"queue_id": "<str>"}                     -> PlayerQueue | null
player_queues/items            {"queue_id": "<str>", "limit": 500, "offset": 0} -> QueueItem[]
player_queues/get_active_queue {"player_id": "<str>"}                    -> PlayerQueue | null
```

`player_queues/get` returns `null` (not an error) for an unknown id. `player_queues/items`
returns `[]` for an unknown id. `limit` defaults to 500, `offset` to 0.

`PlayerQueue` fields on the wire (`music_assistant_models/player_queue.py`):

| field | type | notes |
|---|---|---|
| `queue_id` | str | == `player_id` |
| `active` | bool | false ⇒ transport commands raise `InvalidCommand` |
| `display_name` | str | |
| `available` | bool | |
| `items` | int | **count**, not the list. The list is `player_queues/items`. |
| `shuffle_enabled` | bool | |
| `repeat_mode` | `RepeatMode` | |
| `dont_stop_the_music_enabled` | bool | |
| `current_index` | int \| null | index being played |
| `index_in_buffer` | int \| null | index preloaded by the player |
| `elapsed_time` | float | seconds, **media-time**, snapshot only |
| `elapsed_time_last_updated` | float | UTC epoch of that snapshot |
| `playback_speed` | float | in effect at `elapsed_time_last_updated` |
| `state` | `PlaybackState` | |
| `current_item` | `QueueItem` \| null | |
| `next_item` | `QueueItem` \| null | |
| `radio_source` | MediaItem[] | |
| `flow_mode` | bool | false for Ampere (`requires_flow_mode` is hardcoded False) |
| `resume_pos` | int | seconds, set at stop/pause |
| `is_dynamic` | bool | |
| `extra_attributes` | dict | |

`elapsed_time` is a **snapshot**, not live. Real position is
`elapsed_time + (now - elapsed_time_last_updated) * playback_speed`, and only while
`state == "playing"` (`PlayerQueue.corrected_elapsed_time`). Comparing raw `elapsed_time`
between two reads measures nothing.

Fields with `serialize="omit"` that a client will never see: `enqueued_media_items`,
`flow_mode_stream_log`, `next_item_id_enqueued`, `session_id`, `items_last_updated`, `userid`.

`QueueItem` fields: `queue_id`, `queue_item_id` (uuid4 hex, the stable handle for
`play_index`/`move_item`/`delete_item`), `name`, `duration`, `sort_index`, `streamdetails`,
`media_item`, `image`, `index`, `available`, `extra_attributes`. `streamdetails.seek_position`
is where a pending seek offset lives — Ampere reads exactly this in `_seek_offset_ms`.

Mutators, for completeness: `player_queues/clear {queue_id, skip_stop?}`,
`player_queues/delete_item {queue_id, item_id_or_index}`,
`player_queues/move_item {queue_id, queue_item_id, pos_shift=1}` (`pos_shift: 0` means "move to
top as next item"), `player_queues/move_item_end {queue_id, queue_item_id}`,
`player_queues/play_index {queue_id, index, seek_position=0, fade_in=false}` (`index` is an int
index **or** a `queue_item_id` string), `player_queues/transfer {source_queue_id,
target_queue_id, auto_play?}`, `player_queues/save_as_playlist {queue_id, name}`,
`player_queues/set_playback_speed {queue_id, speed, queue_item_id?}` (0.5–3.0, audiobooks and
podcast episodes only).

---

## 9. Reading player state

```jsonc
players/all         {"return_unavailable": true, "return_disabled": false,
                     "provider_filter": null, "return_protocol_players": false}  -> Player[]
players/get         {"player_id": "<str>", "raise_unavailable": false}           -> Player | null
players/get_by_name {"name": "<str>"}                                            -> Player | null
```

Defaults matter: `return_unavailable` defaults to **true** (an unavailable Ampere player is in
the list and looks normal apart from `available: false`), `return_disabled` **false**,
`return_protocol_players` **false**. `players/get` returns `null` rather than raising unless
`raise_unavailable: true`.

The returned object is `music_assistant_models.player.Player` (aliased `PlayerState`
server-side — it is a snapshot dataclass, not the live provider object). Fields that carry the
state a test cares about:

| concern | field | notes |
|---|---|---|
| playback state | `playback_state` | `PlaybackState` enum |
| " (alias) | `state` | injected by `__post_serialize__` for backwards compat; same value |
| elapsed | `elapsed_time` | float seconds, snapshot |
| elapsed | `elapsed_time_last_updated` | UTC epoch; live value = `elapsed + (now - this)` **only while playing** |
| volume | `volume_level` | int 0..100, or null |
| mute | `volume_muted` | bool or null |
| group volume | `group_volume` | max of powered children; `null` becomes `0` on the wire |
| group mute | `group_volume_muted` | true only if all powered children are muted |
| current media | `current_media` | `PlayerMedia`: `uri`, `media_type`, `title`, `artist`, `album`, `image_url`, `duration`, `source_id` (the queue id), `queue_item_id`, `elapsed_time`, `elapsed_time_last_updated` |
| power | `powered` | **`null` is rewritten to `true` on the wire** when `power_control == "none"` |
| availability | `available`, `enabled` | |
| identity | `player_id`, `provider`, `type`, `name` (alias `display_name`) | |
| capability | `supported_features` | list of `PlayerFeature` strings |
| capability | `power_control`, `volume_control`, `mute_control` | `native` / `fake` / `none` / a control id |
| grouping | `group_members` (alias `group_childs`), `static_group_members`, `can_group_with`, `synced_to`, `active_group` | |
| routing | `active_source` | queue id when MA is the source; **Ampere leaves this `null`** |
| misc | `source_list`, `sound_mode_list`, `options`, `output_protocols`, `active_output_protocol`, `extra_attributes` (alias `extra_data`), `icon`, `hide_in_ui`, `needs_setup` | |

`__post_serialize__` aliases to be aware of when writing assertions: `display_name` = `name`,
`state` = `playback_state`, `group_childs` = `group_members`, `extra_data` = `extra_attributes`,
`device_info.mac_address` / `device_info.ip_address` lifted out of `device_info.identifiers`,
`hide_player_in_ui` synthesised from `hide_in_ui`.

`PlayerType` values: `player`, `stereo_pair`, `group`, `protocol`, `display`, `visualizer`,
`light`, `unknown`. Ampere emits `player` for an Echo and `group` for an Alexa Whole Home Audio
group.

---

## 10. Subscribing to state-change events

**There is no subscribe command.** `WebsocketClientHandler._handle_auth_command` calls
`_subscribe_to_events()` immediately after a successful `auth`, wiring `mass.subscribe(...)`
straight to the socket. Every event the server emits arrives unsolicited from that moment. A
latency harness must therefore start reading frames *before* issuing the command it is timing,
and must tolerate events interleaved with command results.

Frame shape (`MassEvent`): `{"event": "<EventType>", "object_id": "<player_id|queue_id|uri>",
"data": <payload>}`. `object_id` is nullable for global events.

Events relevant to a player suite (`EventType` in `music_assistant_models/enums.py`):

| event | `object_id` | `data` | emitted when |
|---|---|---|---|
| `player_updated` | player_id | full `Player` snapshot | any player attribute changed (`PlayerController._on_player_update`) |
| `player_added` | player_id | `Player` | discovery |
| `player_removed` | player_id | — | |
| `player_options_updated` | player_id | option list | |
| `player_config_updated` | player_id | player config | |
| `queue_updated` | queue_id | full `PlayerQueue` | any queue field other than `elapsed_time` changed |
| `queue_items_updated` | queue_id | full `PlayerQueue` | items list changed |
| `queue_time_updated` | queue_id | **a bare float** (seconds) | position ticks, and on a seek jump |
| `queue_added` | queue_id | `PlayerQueue` | |
| `media_item_played` | uri | play record | scrobble-style record |

The critical asymmetry for latency measurement:

- `queue_time_updated` carries **only the elapsed seconds as a raw number**, not an object.
- When *only* `elapsed_time` changed, MA deliberately suppresses `queue_updated` and emits
  nothing at all unless the jump exceeds **2 seconds**, in which case it emits
  `queue_time_updated` and additionally triggers a player update. So: a normal position tick is
  quiet, and a seek is loud. Timing a seek off `queue_time_updated` works; timing ordinary
  playback progress off it does not.
- `player_updated` fires on the *snapshot* the player provider published. For Ampere the
  provider answers controls **optimistically** — `play()`/`pause()` set
  `_attr_playback_state` and call `update_state()` before Alexa has done anything, then
  re-poll `RESYNC_SECONDS` (1.5s) later. So the first `player_updated` after a transport
  command measures MA's round trip, **not** the speaker. The corrected value arrives in a
  second event ~1.5s later, and a third at the next poll (10s, or 30–60s once the push stream
  is up).
- Other event types on the same socket that a player suite should filter out:
  `media_item_added/updated/deleted`, `providers_updated`, `sync_tasks_updated`,
  `tasks_updated`, `music_sync_completed`, `auth_session`, `core_state_updated`,
  `dsp_presets_updated`, `player_dsp_config_updated`, `application_shutdown` (deprecated).

---

## 11. What Ampere actually implements

From `ma_provider/provider.py`.

### Declared `PLAYER_FEATURES`

```python
PLAYER_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.SEEK,
    PlayerFeature.ENQUEUE,
}
```

`SUPPORTED_FEATURES: set[ProviderFeature] = set()` — the provider declares **no** provider-level
features: no player syncing, no group creation. Alexa owns its own groups.

### Methods defined on `AmperePlayer`

| method | status |
|---|---|
| `play_media(media)` | real — publishes MA's queue to the bridge, then `run_custom` an utterance; confirms after 4s and resends once if Alexa never came asking |
| `enqueue_next_media(media)` | **deliberate no-op**. Declared so MA does not re-issue `play_media` per track (which would restart the queue at every track boundary) and so `requires_flow_mode` stays False |
| `play()` | real |
| `pause()` | real |
| `stop()` | real |
| `next_track()` | real |
| `previous_track()` | real |
| `volume_set(level)` | real, with a confirm-and-resend loop (up to 3 attempts, ±2 tolerance) |
| `poll()` | real — reads `/api/np/player` off Alexa; the direction truth actually flows |
| `apply_push(event)` | real — Amazon push events for volume / player state / now-playing |

### Declared but NOT implemented as a player method

- **`SEEK`.** There is no `AmperePlayer.seek()`. The feature is declared, and seek works by a
  different route: `player_queues/seek` → `play_index(..., seek_position=N)` → `play_media`,
  with `AmperePlayer._seek_offset_ms` reading `streamdetails.seek_position` off the queue item
  and threading it into Alexa's `stream.offsetInMilliseconds`.
  **Consequence for a test suite:** exercise seek via `player_queues/seek` (or
  `players/cmd/seek`, which redirects to it while a queue is active). Calling the player's
  `seek` directly — the only path taken when no MA queue is active — hits
  `Player.seek`'s `NotImplementedError`. `_seek_offset_ms` also silently clamps any offset
  within 3000 ms of the track end back to 0, so a seek to the last seconds of a track will
  restart it from the beginning by design, not by fault.

### NOT implemented, and what MA does if you command it anyway

| command | what happens |
|---|---|
| `players/cmd/volume_mute`, `players/cmd/group_volume_mute` | `VOLUME_MUTE` is not declared ⇒ `mute_control` resolves to `"none"` ⇒ `cmd_volume_mute` matches no branch and **returns silently**. No error, no state change. `volume_muted` is nevertheless *populated read-only* from Alexa's own reports (`_apply_state`, `_push_volume`). **Do not assert that muting works, and do not assert that it errors.** |
| `players/cmd/power` | `POWER` not declared ⇒ `power_control == "none"`. `Player.power()` is not overridden. Note `powered` is `True` for an Echo and **`None` for an Alexa group** (deliberate: a group claiming to be powered permanently captures its members and hides every Echo from the picker) — and `null` is rewritten to `true` on the wire. |
| `players/cmd/set_members`, `/group`, `/group_many`, `/ungroup`, `/ungroup_many`, `players/create_group_player` | `SET_MEMBERS` not declared and `can_group_with` is empty ⇒ `UnsupportedFeaturedException` (9). Alexa groups are Amazon's; they appear as separate `PlayerType.GROUP` players with `group_members` populated, and are driven as players, not assembled by MA. |
| `players/cmd/select_source` | `SELECT_SOURCE` not declared. `active_source` is always `null`. |
| `players/cmd/select_sound_mode` | `SELECT_SOUND_MODE` not declared; `sound_mode_list` empty. |
| `players/cmd/set_option` | `OPTIONS` not declared; `options` empty. |
| `players/cmd/play_announcement` | `PLAY_ANNOUNCEMENT` not declared. |
| `player_queues/set_playback_speed` | Ampere hands whole tracks to Alexa; no server-side atempo path. Not exercised. |
| gapless | neither `GAPLESS_PLAYBACK` nor `GAPLESS_DIFFERENT_SAMPLERATE` declared. |

Two silent-failure classes a suite must be written around:

1. **Commands to an unavailable player are swallowed.** `handle_player_command` logs
   `"Ignoring command … for unavailable player …"` and returns `None` — the websocket reports
   success. Ampere marks a player unavailable after 3 consecutive failed polls
   (`POLL_FAILURES_BEFORE_UNAVAILABLE`), i.e. ~30s of silence. Read `available` before
   asserting on any command's effect.
2. **Optimistic state.** Every Ampere control writes the expected state and calls
   `update_state()` before Alexa has acted. Reading state back immediately confirms the
   optimism, not the speaker. The earliest honest read is after `RESYNC_SECONDS` (1.5s), and
   `play_media` in particular is only confirmed when Alexa comes to fetch the queue
   (`queue_api.handoff_claimed_at()`), up to `PLAY_CONFIRM_SECONDS` (4s) later.

Volume timing constants a latency assertion must budget for: `VOLUME_QUEUE_DELAY = 1.5s`
(alexapy batches a group's per-member volume writes into one Amazon request — the floor of any
volume change), `VOLUME_CONFIRM_SECONDS = 2.0`, `VOLUME_ATTEMPTS = 3`,
`VOLUME_RETRY_SPREAD = 2.0`, `VOLUME_TOLERANCE = 2` (Echo Studio quantises: ask 18, get 17).
A group volume change can legitimately take >8s to converge across four speakers.

---

## 12. Enum reference (verbatim from `music_assistant_models/enums.py`)

```
PlaybackState : idle | paused | playing | unknown
                (PlayerState is an alias for PlaybackState)
RepeatMode    : off | one | all | unknown
QueueOption   : play | replace | next | replace_next | add | unknown
PlayerType    : player | stereo_pair | group | protocol | display | visualizer | light | unknown
PlayerFeature : power | volume_set | volume_mute | pause | set_members | multi_device_dsp |
                seek | next_previous | play_announcement | enqueue | select_sound_mode |
                select_source | options | gapless_playback |
                gapless_different_samplerate | play_media | unknown
                ("sync" is accepted as a deprecated alias for set_members)
EventType     : player_added | player_updated | player_removed | player_config_updated |
                player_dsp_config_updated | player_options_updated | dsp_presets_updated |
                queue_added | queue_updated | queue_items_updated | queue_time_updated |
                media_item_played | media_item_added | media_item_updated |
                media_item_deleted | providers_updated | sync_tasks_updated | tasks_updated |
                music_sync_completed | auth_session | core_state_updated |
                application_shutdown (deprecated) | unknown
MediaType     : artist | album | track | playlist | radio | audiobook | podcast |
                podcast_episode | folder | announcement | flow_stream | plugin_source
                (deprecated) | audio_source | sound_effect | genre | unknown
```

Every one of these enums defines `_missing_`, so **an unrecognised value is coerced to
`unknown` rather than raising**. No MA command validates enum arguments by rejecting them.

---

## 13. Safety

`tests/live/safety.py` is the only sanctioned way to obtain a target. It fails closed on the
Amazon serial (not the friendly name — "Living Room Echo Studio" is allowed, "Living Room Echo"
is not), and `group_is_safe()` refuses any Alexa group containing a non-allowed member, because
this account also holds two televisions, a projector and a car.
