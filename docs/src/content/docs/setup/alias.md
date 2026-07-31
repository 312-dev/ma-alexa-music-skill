---
title: Choosing an alias
description: Why the invocation name matters more than it looks, and how to check one against your own library.
sidebar:
  order: 3
---

The alias is the word you say: `play Radiohead on **ampere**`. It looks like a
cosmetic choice. It is not, and getting it wrong produces failures that look
like bugs in the bridge.

## Why it matters

**Alexa resolves content against your uploaded catalog before it routes to a
provider.** You upload every artist, album, track, playlist and genre in your
library so that speech resolves to real entity ids. That same catalog is then
competing with your alias word for the same utterance.

So the failure mode is not "Alexa did not recognise the alias". It is "Alexa
recognised something else you own, and played that instead".

## Three real collisions

These are from a single library, and each one cost time before the cause was
obvious.

| Alias tried | What happened |
|---|---|
| `jukebox` | Eaten by the artist **Jukebox The Ghost** and the track **Juke Box Hero** |
| `gray tunes` | Resolved to the artist **Conan Gray** |
| `phono` | Consistently misheard by ASR as **Sonos** |

The first two are catalog collisions and are specific to that library. The third
is not: it is a speech-recognition collision with a word Alexa already knows,
and it would happen to anyone.

:::caution[Collisions are per-library]
There is no safe list. A word that is inert against one library is a hit against
another. The only way to know is to check against **your** catalog.
:::

## Check a candidate before committing

The bridge ships an alias checker in its setup wizard, at `/setup/alias`. It
queries your own library live and shows what a candidate collides with, before
you have created a skill or uploaded anything.

Use it. It is a text field, and it answers the only question that matters.

If you want to sanity-check by hand instead, search your Subsonic server for the
candidate word and each of its plausible mishearings, across artists, albums,
tracks and playlists. A single strong hit is enough to disqualify it.

## What makes a good alias

- **Two syllables or more.** Single syllables collide with everything.
- **Not a word in your library.** Check, do not assume.
- **Not a homophone of a device or service Alexa already knows.** "phono" and
  "Sonos" is the cautionary example, but so are anything close to "Spotify",
  "Sonos", "Pandora", "Deezer" or "Plex".
- **Comfortable to say every single time**, because you will. There is no
  default-provider setting that lets you drop it.
- **Distinct under ASR, not only on paper.** Say it out loud to an Echo in a
  routine sentence and listen to what comes back.

## Changing it later

The alias is the skill's invocation name, set in the skill manifest. Changing it
means updating the manifest and waiting for Amazon to re-model the skill.
`SLU_MODELING` runs on Amazon's schedule and
[takes weeks](../catalog/#what-the-ingestion-states-mean), so treat the alias as
expensive to change even though the edit itself is one field.

Pick it now, before the skill exists.

## Next

[Public HTTPS endpoint](../ingress/).
