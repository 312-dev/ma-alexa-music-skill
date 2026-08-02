---
title: Gaps and limits
description: What Ampere does not do, stated plainly, including the things with no known cause.
---

Stated plainly, because finding these out by experiment is expensive.

## It cannot be your default music provider

Amazon's default-provider setting lists first-party and partner services. A
private development-stage music skill is not among them, and there is no
manifest field that changes this.

The practical consequence: you name the alias in every command, forever. Choose
one you do not mind saying. See [Choosing an alias](../../setup/alias/).

## It is not in Alexa Routines' Music action

The skill does not appear in the provider list for a Routine's Music action at
all. Routines can still drive it through a custom-command action using the
`ask <alias> to ...` form. See [Voice and text](../../playing/voice-and-text/).

## Uploading a catalog unbinds the skill

Playback falls back to your default provider and Alexa announces it, while the
bridge answers every request correctly and `ER_INGESTION` reports `SUCCEEDED`.

The catalog sync now cycles enablement itself after any run that uploaded
something, which closes this. It can only do so if `SKILL_ID` is set; without
it the run warns loudly and the skill stays unbound until you cycle by hand.
See [Catalog and enablement](../../setup/catalog/#the-trap-uploading-a-catalog-unbinds-the-skill).

## The skill's binding decays on its own

The single most expensive thing here to learn by experiment. The binding between
the skill and its provider slot rots within hours of being established, with no
upload, no manifest change and no deploy involved. Searches keep resolving,
Amazon keeps reporting the skill as enabled, the bridge keeps answering
correctly, and nothing plays.

Ampere works around it rather than fixing it, because the cause is on Amazon's
side and is not observable from here:

- a keep-alive re-provisions the skill every `BINDING_KEEPALIVE_HOURS`
  (default 4) while nothing is playing, and
- a detector cycles once when **two or more searches in a row** are seen not
  reaching playback, then waits for real evidence of recovery before it will try
  again. Two, not one: a voice transfer between speakers legitimately produces a
  single unanswered search, and cycling on that breaks the playback it was meant
  to protect.

The Status page shows how many recent searches reached playback. **That ratio is
the only honest health signal**, because every other indicator reads green
throughout the failure. See
[the finding](../../how-it-works/findings/#the-provider-slot-binding-decays-on-a-clock).

## The Alexa app will not render a scrubber

The item declares `{"type": "ADJUST", "name": "SEEK_POSITION", "enabled": true}`
alongside a known `durationInMilliseconds`, over a stream that answers `206`
with `Content-Range` end to end. First-party providers do show a scrubber.

Undeclared controls are certainly disabled controls, but declaring this one is
evidently not sufficient. **No cause has been established.**

## Request verification has no replay protection

Amazon documents request signing for *custom* skills, not for music skills, and
the two envelopes differ. Captures confirm Amazon sends `Signature`,
`Signature-256` and `SignatureCertChainUrl` to a music endpoint exactly as it
does to a custom one, and everything up to and including the RSA signature over
the raw body is verified.

What cannot be implemented is the replay window. The custom-skill recipe ends
with a check against `request.timestamp`, and a music directive has no timestamp
anywhere. **A captured directive can be replayed forever.**

Verification defaults to `warn` rather than `on`, deliberately: turning it on by
default would silently break every upgrading deployment, and a music skill is
uniquely bad at telling you it broke. Watch a day of real traffic verify in the
log, then set `VERIFY_REQUESTS=on`.

## Music skills cannot be tested without a device

`ask smapi simulate-skill` refuses with "Unsupported skill type. Please note that
only custom skills are currently supported". The developer console Test tab and
the utterance profiler are custom-skill only. Every diagnosis comes from inbound
request captures plus what Alexa says out loud.

## Smaller, known

- **`getTopSongs` returns nothing** against the test library, so it is not used.
  That is a fact about that endpoint on that server rather than a general one.
- **A track's display name can fall back to its raw id** in
  `GetPlayableContent`, where the single-song lookup fails but track resolution
  succeeds. Cosmetic, seen with "Juke Box Hero".
- **Captures are pruned to the newest `CAPTURE_KEEP`**, 400 by default, and
  queue state files expire after `QUEUE_STATE_TTL`. Neither directory grows
  without bound, but neither is archived either: a failure older than the last
  400 directives is no longer diagnosable.
- **Streams are always transcoded to MP3 at 256 kbit/s.** Whether Alexa would
  accept FLAC on the *music skill* path specifically has not been tested; the
  behavior is inherited from a note in the `AudioPlayer` documentation. It
  would cost roughly four times the bandwidth if enabled.
- **Home Assistant often registers each Echo more than once**, and automations
  pointed at the inactive duplicate fail silently. See
  [Home Assistant](../../playing/home-assistant/).

## Stated as unverified

- **Servers other than Navidrome are untested.** The client speaks plain
  Subsonic 1.16.1 with no OpenSubsonic extensions, so Airsonic,
  Airsonic-Advanced, Gonic, LMS, Ampache, Funkwhale, Nextcloud Music and Astiga
  should all work. None of them have been exercised.
- **Browse shelves are implemented but not confirmed on a device.** The bridge
  answers `GetDisplayableContent` with grouped shelves of artists, albums,
  songs, playlists and genres. There is no record in the repository of that
  surface being observed working on an Echo Show, a Fire TV or in the Alexa app.
