---
title: First play
description: What to say, what should happen, and how to confirm the bridge is being called.
sidebar:
  order: 9
---

Stand near an Echo. Substitute your own alias for `music assistant`.

```
"Alexa, play Radiohead on music assistant"
```

If it works, Alexa starts playing and the app shows the track title, artist,
album and cover art, with working next and previous.

## What you can ask for

| Say | You get |
|---|---|
| `play <artist> on music assistant` | That artist's whole discography, shuffled |
| `play <album> on music assistant` | The album, in album order |
| `play <track> on music assistant` | That track |
| `play the <name> playlist on music assistant` | The playlist, in playlist order |
| `play <genre> on music assistant` | That genre, shuffled |
| `play music assistant` | Your library at random |

Albums and playlists keep their own order, because their order is the point.
Artists, genres, starred tracks and the random library shuffle by default:
asking for an artist and getting the first track of their earliest album, every
time, is not what anyone means.

Transport controls work as they do for any provider: next, previous, shuffle,
loop, repeat, and thumbs up or down. Thumbs are written through to your Subsonic
server as stars, and a thumbs-down also skips the track.

## Confirm the bridge is being called

The single most useful signal is whether requests are arriving at all. It
separates "Alexa is not calling us" from "Alexa is calling us and discarding the
answer", which are completely different problems.

Every inbound directive is written to the capture directory:

```sh
docker exec ma_alexa ls -t /data/captures | head
```

You should see files named after the directive, in this order, for a first play:

1. `Alexa.Media.Search.GetPlayableContent` - what did the user ask for
2. `Alexa.Media.Playback.Initiate` - start a queue, here is the first item
3. `Alexa.Audio.PlayQueue.GetNextItem` - one per track transition thereafter

Or over HTTP, with the admin token:

```sh
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  https://music.example.com/captures | head -c 2000
```

The container log gives the same shape with timings:

```sh
docker logs --tail 50 ma_alexa
```

Each handled directive logs a line like
`Alexa.Media.Playback.Initiate served in 84ms`. Watch those numbers.
`Initiate` has a 100ms p50 and 400ms p99 budget on Amazon's side.

## If it did not work

Nothing about the failure will be self-explanatory, because
[music skills cannot be simulated](../what-this-is/#the-one-thing-to-know-before-you-start)
and Amazon surfaces very little. Go to
[Troubleshooting](../troubleshooting/), which is organized by what you can
actually observe.

The two most common first-time answers:

- Alexa says "Here's ... from Spotify" or another provider. The skill is
  unbound. Cycle enablement.
- Nothing at all arrives in `/data/captures`. The endpoint, TLS, or
  `sslCertificateType` is wrong.

## Then

Read [Voice and text](../../playing/voice-and-text/) before wiring anything to
an automation. The phrasing that works out loud is not the phrasing that works
from Home Assistant, and that difference has cost more time than anything else
in this project.
