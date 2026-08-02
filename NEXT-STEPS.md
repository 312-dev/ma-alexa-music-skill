# Next steps

State as of 2026-07-31. Everything in the previous version of this file is
built: the setup wizard, endpoint validation, the alias checker, station
tuning, request verification, the enablement cycle, and the Music Assistant
provider. What follows is what is genuinely left.

Architecture and design notes now live on the docs site under **How it works**,
not here. This file is only a to-do list.

## Deploy the new build

Not yet deployed. The running service on the box is the pre-rebrand build, and
two of tonight's fixes are in the repo only.

**`PUBLIC_BASE` is now required and has no default.** It used to fall back to
`https://alexa-music.graysons.network`. If the Nomad job for `alexa-music` was
relying on that fallback, the container will exit at boot with a message saying
so. Check the job spec before deploying.

`SUBSONIC_URL` lost its default the same way.

Also new in the image and worth knowing about on the box:

- Icons are baked in at `/app/icons` and `ICON_DIR` defaults there. The job may
  still set `ICON_DIR=/data/icons`, which is fine and keeps the volume copy.
- `SKILL_ID` should be set so `catalog_sync.py` can rebind the skill after an
  upload. Without it the sync prints a loud warning and leaves voice broken.
- `VERIFY_REQUESTS` defaults to `warn`. Watch a day of real Amazon traffic log
  `verified` before setting it to `on`.

## Stations are unreachable by voice

Observed live. "Play Gregory Alan Isakov radio" produced a plain artist queue:
twelve consecutive tracks by the seed artist walking the discography, none of
the similar artists a station interleaves. Found from Home Assistant's recorder
rather than from the bridge, which cannot see what Alexa decided.

`station_request()` has two signals and neither fires on this path. It looks for
a trailing "radio" or "station" in a free-text value, but when Alexa resolves
the utterance against the uploaded catalog it answers with the entity id
(`artist.<id>`) and the word is already gone. `MEDIA_TYPE: STATION` has never
appeared in a capture from this skill. So `rad:` is reachable only when entity
resolution misses and the raw text survives.

1. **Capture a real payload first.** There may be a field carrying the spoken
   form that is simply not being read. Do not guess at this without one.
2. **Then put stations in the catalog.** Emit a station entity per artist
   ("<name> Radio") in `catalog_sync.py` and map its entity id straight to
   `rad:<id>`. That stops it depending on what survives entity resolution.

## Open questions

- **The Alexa app will not render a scrubber.** We send
  `{"type": "ADJUST", "name": "SEEK_POSITION", "enabled": true}` with a known
  `durationInMilliseconds`, over a stream that answers `206` with
  `Content-Range` end to end. Every input is correct. First-party providers do
  show one. No cause established.
- **Should FLAC be offered?** `stream_url` transcodes to MP3 256k
  unconditionally, on a comment inherited from the AudioPlayer docs saying Alexa
  will not take FLAC. Worth verifying for the *music skill* path specifically.
  Costs roughly 4x the bandwidth if enabled.
- **Does `GetDisplayableContent` actually surface browse shelves?** It is
  implemented and tested, but has never been observed working on a device.

## Wire Home Assistant to the pre-emptive cycle

`POST /setup/skill/cycle` is live and answers JSON. It exists so the caller
that knows when the high-stakes moment is (the wake alarm) can guarantee a
fresh binding at that instant, which neither the keep-alive clock nor the miss
detector can promise.

Two user steps, because neither can be done from here: the HA connector has no
managed-YAML tool, and the admin token is not readable by design.

1. `secrets.yaml`: `ampere_admin_token: <the ADMIN_TOKEN value>`
2. `configuration.yaml`, alongside the existing `rest_command:` block:

```yaml
rest_command:
  ampere_cycle:
    url: http://100.85.183.28:5056/setup/skill/cycle
    method: post
    headers:
      X-Admin-Token: !secret ampere_admin_token
    timeout: 45
```

Over the tailnet, so it never crosses the public origin. Then an automation
firing at `input_datetime.wake_up_alarm` minus two minutes calls
`rest_command.ampere_cycle`. The cycle takes up to about fifteen seconds.

The alternative, if the YAML is not worth it: add `setup.skill_cycle` to
`_OPEN` in `setup_ui/views.py` so the route needs no token and relies on the
existing network gate alone. Cycling is idempotent and non-destructive, so the
blast radius is "someone already on the tailnet can re-enable your own skill",
but it does widen the admin plane and should be a deliberate choice.

## Smaller

- [ ] **Per-artist station exclusions.** The station tuning page mentions them
      but there is no mechanism in `app.py` to honour them, so they are not
      offered.
- [ ] **Rotate the Navidrome password.** It was decrypted into a session
      transcript.
- [ ] **Duplicate Home Assistant Echo entities.** Every Echo is registered two
      or three times and only some are live: `media_player.whole_apartment`,
      `_2` and `_3` all exist, and only `_2` follows playback. Automations
      pointed at a dead one fail silently. Root cause is likely more than one
      Alexa Media Player config entry. Removing entities breaks whatever
      references them, so check before deleting.
- [ ] **Pitch the MA provider upstream** to @alams154 as a `playback_mode`
      branch on the existing `alexa` provider rather than a competing one. See
      `ma_provider/README.md`.
