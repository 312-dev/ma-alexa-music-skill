---
title: Stations
description: How an endless station is built from similar artists, and why not from similar songs.
---

```
"Alexa, play Gregory Alan Isakov radio on ampere"
```

A station is a `rad:<artist-id>` contentId. It never ends, and like everything
else it is derived from that string alone.

## How one is requested

Two signals, both in fields Amazon already sends:

- `MEDIA_TYPE` carrying `STATION` or `RADIO`. This is the shape Amazon's own
  metadata enum implies, but it has never appeared in a capture from this skill.
- The trailing word `radio` or `station` still attached to the free text, which
  is what actually happens in practice.

The trailing word is stripped before the search runs, so the lookup is for the
artist and not for their name plus a word they never said. It is matched
**trailing only**, so "Radiohead" and "Radio Moscow" are still themselves.

A station is always built from an artist, so if Alexa labeled the request as
something else the bridge resolves an artist anyway.

## How the pool is built

```
rad:<seed>
  │
  ├── seed artist            ── discography walk ── capped to 12 tracks
  ├── similar artist 1       ── discography walk ── capped to 12 tracks
  ├── similar artist 2       ── discography walk ── capped to 12 tracks
  ⋮   (up to RADIO_ARTISTS)
  │
  └── pool ── shuffled by queueId ── played
```

Similar artists come from `getArtistInfo2` with `includeNotPresent` pinned off:
a similar artist with nothing in the library is a name with no tracks behind it.
Some servers ignore that flag and mark absent artists with a negative id, so
those are filtered again in the client.

Per-artist capping is not cosmetic. Uncapped, a seed artist with 200 tracks
sitting next to neighbours who have 10 gives you a station that is mostly the
seed artist again. The cap is `RADIO_TRACKS_PER_ARTIST`, 12 by default, and the
sample is seeded on the **artist id** rather than the queueId so that the pool is
identical for every queue built from the same contentId. That is what allows the
pool to be cached against the contentId at all.

## Why similar artists and not similar songs

:::note[The measurement that decided it]
`getArtistInfo2` returns about 20 usable similar artists in **about 725ms**.

`getSimilarSongs2` hands back local songs directly, which is a better fit on
paper, but takes **about 10 seconds, every time, uncached**.
:::

Ten seconds cannot sit on an Alexa request. For a long while similar songs
looked unsupported by the server, because the requests were quietly
exceeding `SUBSONIC_TIMEOUT` and failing. They were not unsupported. They were
slow.

Stations are built from similar artists for that reason alone.

`getTopSongs` is a separate matter: it cost about 0.9s and returned nothing at
all against the test library. That is a fact about `getTopSongs`, not about
Last.fm-backed endpoints generally, which is worth being precise about because
`getArtistInfo2` is equally Last.fm-backed and works well.

## Why it never ends

Indexes past the end of the pool are further passes over it, reshuffled on the
pass number:

```
lap, position = divmod(index - len(pool_of_this_queue), len(pool))
track = shuffle(pool, seed = f"{queueId}:{lap}")[position]
```

A long listen therefore does not replay one running order, and **any index is
still derivable from the contentId, queueId and index alone**. Nothing is
stored, and skipping ahead is as cheap as playing forward.

A station also ignores `AFTER_CONTENT`. Whatever that setting says, `rad:`
continues with more of itself, because a station that ends is not a station.

## The degraded-station bug

:::caution[A failure that used to be permanent]
Navidrome returned no similar artists for Foreigner once. The bridge cached the
result, and every request after that served a Foreigner-only "station" until the
process was restarted.

The cache never re-derives an entry it already holds, so caching a failure pins
it. Both the similar-artist lookup and the track pool built from it now refuse
to cache a result that fell back to the seed artist alone.
:::

The general rule this produced, applied in three places in the codebase now: a
cache should hold answers, not failures.

## Warming

Building a station is one similar-artist lookup plus a discography walk per
artist, about three seconds cold. Two things keep that off the clock:

- An explicit station request warms the pool during `GetPlayableContent`, since
  `Initiate` follows roughly a second later.
- A queue that will *continue* into a station warms it at `Initiate`, because
  otherwise the cost lands on the first `GetNextItem` past the end of the base
  queue, the one moment with no slack.

## Tuning

| Variable | Default | Effect |
|---|---|---|
| `RADIO_ARTISTS` | `12` | How many similar artists the station draws on |
| `RADIO_TRACKS_PER_ARTIST` | `12` | Per-artist cap within the pool |

Both are worth adjusting for library size. A small library wants a wider cast; a
large one wants a tighter cap. The setup wizard is intended to offer a live
preview of the artist pool for a seed, which would have caught the
degraded-station bug by eye rather than by ear.
