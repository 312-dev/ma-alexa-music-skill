# Next steps

A to-do list. Architecture and design notes live on the docs site under
**How it works**, and known gaps live under
[Gaps and limits](https://graysoncadams.github.io/ampere/reference/limits/);
neither belongs here.

## Stations are unreachable by voice

Observed live. "Play Gregory Alan Isakov radio" produced a plain artist queue:
twelve consecutive tracks by the seed artist walking the discography, none of
the similar artists a station interleaves.

`station_request()` has two signals and neither fires on this path. It looks for
a trailing "radio" or "station" in a free-text value, but when Alexa resolves
the utterance against the uploaded catalog it answers with the entity id
(`artist.<id>`) and the word is already gone. `MEDIA_TYPE: STATION` has never
appeared in a capture from this skill. So `rad:` is reachable only when entity
resolution misses and the raw text survives.

1. **Read a real payload first.** There may be a field carrying the spoken form
   that is simply not being read. Do not guess at this without one. The capture
   pairing needed for this now exists.
2. **Then put stations in the catalog.** Emit a station entity per artist
   ("<name> Radio") in `catalog_sync.py` and map its entity id straight to
   `rad:<id>`. That stops it depending on what survives entity resolution.

## Tune the keep-alive interval from measured misses

`BINDING_KEEPALIVE_HOURS` defaults to 4, extrapolated from two observations: the
binding worked sixteen minutes after a cycle and was dead by seven hours.
`_binding_health` measures the thing that guess stands in for, so the interval
can now be set from data rather than from a hunch.

Each time `_reactive_check` fires, the gap between `enabled_at` and the miss is
an observed survival time. Lower the interval until the detector stops firing.
Cost is only more enablement cycles, which draw on a different rate pool from
catalog uploads and are nowhere near it: four hours is six a day, two hours is
twelve.

It is deliberately a bridge-side interval rather than something an external
scheduler drives. Driving it from outside means putting `ADMIN_TOKEN` in a
second system and making that system responsible for this one's housekeeping,
to achieve what an interval here already does.

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
- **Is the decay interval account-specific?** Four hours came from one account.
  `BINDING_KEEPALIVE_HOURS` exists so finding out does not need a fork, but
  there is no second data point yet.

## Smaller

- [ ] **Per-artist station exclusions.** The station tuning page mentions them
      but there is no mechanism in `app.py` to honor them, so they are not
      offered.
- [ ] **No server other than Navidrome has been exercised.** The client speaks
      plain Subsonic 1.16.1 with no OpenSubsonic extensions, so the others named
      in the README should work. Nobody has confirmed one.
- [ ] **Pitch the MA provider upstream** to @alams154 as a `playback_mode`
      branch on the existing `alexa` provider rather than a competing one. See
      `ma_provider/README.md`.
