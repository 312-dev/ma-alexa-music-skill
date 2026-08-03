# Regenerating `environment.json`

`environment.json` describes the live Music Assistant instance the conformance
suite runs against: the container it lives in, the Ampere provider instance, the
Echo devices that are safe to drive, and a set of real, currently-playable media
URIs.

Everything in it was read out of the live instance. Nothing was invented, and no
playback was started to produce it.

## Prerequisites

- `ssh hetzner` works (BatchMode is fine).
- You are at the repo root. `tools/ma.sh` and `tools/ma_token.sh` are run from
  there.

## 1. Mint an API token

`tools/ma.sh` needs a token staged inside the MA container. Tokens are
short-lived (6 hours), so a stale checkout of this file usually means the token
has expired:

```bash
tools/ma.sh players        # "auth failed: Invalid or expired token" -> re-mint
tools/ma_token.sh          # mint, 6 hours
tools/ma_token.sh revoke   # when you are done
```

**`tools/ma_token.sh` stops and restarts Music Assistant.** MA opens its
databases with `PRAGMA locking_mode=exclusive`, so `auth.db` cannot be written -
or even read - while MA is running, and `authenticate_with_token` refuses a JWT
with no matching `auth_tokens` row. Stop/insert/start is the only window. Check
that nothing is playing before you run it:

```bash
ssh hetzner 'docker logs --since 10m app-<alloc> 2>&1 | tail -20'
```

## 2. Re-derive the container name

`container` is `app-<nomad alloc id>` and **changes on every MA restart**,
including the restart `ma_token.sh` just performed. Always re-read it:

```bash
ssh hetzner 'nomad job allocs -json music-assistant | python3 -c "import json,sys; print([x[\"ID\"] for x in json.load(sys.stdin) if x[\"ClientStatus\"]==\"running\"])"'
```

The container is `app-<full alloc id>`. A test suite should prefer to resolve
this at runtime rather than trust the recorded value.

## 3. Players, the group, and the exclusions

```bash
tools/ma.sh raw players/all '{}'
```

Take `player_id`, `display_name`, `type`, `available`, and - for the group -
`group_members`. The serial is the part of `player_id` after the colon.

Two things this file encodes that `players/all` will not tell you:

- **The allow/exclude split is a user decision, not a property of the system.**
  Only Bedroom Echo, Bathroom Echo, Kitchen Echo and Living Room Echo Studio may
  be driven. Everything in `excluded` is off limits. Most excluded devices are
  also disabled in MA and so never appear in `players/all` - but **Home Theater
  is enabled and present**, so the exclusion list is the only thing stopping a
  test from playing on it. Re-confirm the split with the user; do not infer it
  from `enabled`.
- **Select the group by `player_id`.** MA also has a disabled `universal_group`
  player (`ugp_sfvxyatc`) with the same display name, "Whole Apartment".

At the time of writing, the Ampere provider exposed exactly one Alexa speaker
group, and all four of its members were allowed devices. If that ever changes,
re-check `group_members` before letting the suite touch the group.

## 4. Media items

The MA library contains **only** OpenSubsonic items plus the builtin playlists.
Deezer and SomaFM are enabled providers but are not synced into the library, so
their items never appear in `music/tracks/library_items`,
`music/playlists/library_items` or `music/radios/library_items` and have to be
addressed by provider URI.

URI form is `<provider_instance_or_domain>://<media_type>/<item_id>`.

```bash
# subsonic tracks and playlists (these do live in the library)
tools/ma.sh raw music/tracks/library_items '{"limit":25}'
tools/ma.sh raw music/playlists/library_items '{"limit":25}'

# radio library - currently empty
tools/ma.sh raw music/radios/library_items '{"limit":25}'

# verify any chosen URI resolves, without playing it
tools/ma.sh raw music/item_by_uri '{"uri":"deezer--tyYQFjC6://track/916424"}'

# playlist contents / track count (no limit parameter; large playlists are slow)
tools/ma.sh raw music/playlists/playlist_tracks \
  '{"item_id":"dVe64uPkleCj29X39NpDd6","provider_instance_id_or_domain":"opensubsonic--etezT5sV"}'

# provider instance ids
tools/ma.sh raw providers '{}'
```

Verify playability with `music/item_by_uri` (and `is_playable` /
`provider_mappings[].available`). **Do not verify by playing** - these are real
speakers in a real apartment.

Two stronger-than-metadata signals worth reusing when picking items:

- `/opt/music-assistant/data/ampere/mastream/` holds Ampere's cached streams.
  The filenames are base64url of the MA URI, so anything in there is proven to
  have been fetched and served end to end.
- A track with `play_count > 0` in `library.db` has really been played on this
  instance.

## 5. Read-only fallback when there is no token

If MA must not be restarted, most of this can still be recovered without the
API, from files on the box:

| Fact | Where |
| --- | --- |
| provider instance ids, player ids, enabled flags | `/opt/music-assistant/data/settings.json` |
| player names, registration, group name | `docker logs app-<alloc>` at startup |
| tracks, playlists, radios, provider mappings | `/opt/music-assistant/data/library.db` |
| cached provider search / playlist results | `/opt/music-assistant/data/.cache/cache.db` |
| proven-streamed URIs | `/opt/music-assistant/data/ampere/mastream/` filenames (base64url) |
| current MA player state | Home Assistant `media_player.*_3` entities (`active_queue`, `mass_player_type`) |

Both SQLite files are locked while MA runs, so copy them first and query the
copy:

```bash
ssh hetzner 'mkdir -p /tmp/malib && cp /opt/music-assistant/data/library.db /opt/music-assistant/data/library.db-wal /tmp/malib/'
```

The one fact this fallback **cannot** produce is the group's member list -
`clusterMembers` comes from the Alexa device list at discovery time, is only
held in memory, and is logged at DEBUG. It needs `players/all`, i.e. a token.

`settings.json` also holds provider credentials. Filter for the structural
fields; never print the whole file.

## Cleanup

```bash
tools/ma_token.sh revoke
```
