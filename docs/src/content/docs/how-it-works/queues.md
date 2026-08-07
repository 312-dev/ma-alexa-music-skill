---
title: Queues and contentIds
description: How the bridge derives an entire queue from a short string, and stores nothing.
---

The bridge holds no per-user playback state. It does not know where you are in a
queue, and it does not need to.

## Alexa carries the position

Every queue request from Amazon echoes back the same three fields:

```json
{
  "id": "7",
  "queueId": "0f3d...",
  "contentId": "ar:aB12cD34"
}
```

`contentId` is a string the bridge chose at `GetPlayableContent` and handed to
Amazon. `id` is the index within the queue. `queueId` is the bridge's own uuid
for this playback session.

Those three are sufficient to answer any question about the queue, which means
the answer is a pure function of the request. No lookup by user, no session
table, no cache that has to be warm for correctness.

This is what keeps `Initiate` inside Amazon's 100ms p50 and 400ms p99 budget.

## The contentId scheme

| Prefix | Identifier | Resolves to | Order |
|---|---|---|---|
| `tr:` | track id | One track | n/a |
| `al:` | album id | The album's tracks | Album order |
| `ar:` | artist id | The artist's whole discography | Shuffled |
| `pl:` | playlist id | The playlist's entries | Playlist order |
| `gen:` | genre name | An ordered genre listing | Shuffled |
| `star:` | unused | Starred tracks | Shuffled |
| `rnd:` | unused, `rnd:all` by convention | 50 random tracks | Shuffled |
| `rad:` | seed artist id | A station | Shuffled, endless |
| `ext:` | opaque token | A track list published out of band | As published |

`ext:` is the exception that proves the rule. Every other prefix names something
the music server already knows about, so the id alone re-derives the list. A
queue composed elsewhere, by Music Assistant for instance, is the output of a
smart playlist or three albums shuffled together: the tracks exist on the server
but the **list** has nothing to point at. So it is published ahead of playback
and given a token that behaves like every other id: stable, re-derivable, and
good for the life of the queue. An unknown or expired token resolves to nothing
and is deliberately not cached.

Albums and playlists keep their own order because their order is the point.
Everything else shuffles by default: asking for an artist and receiving the
first track of their earliest album, every single time, is not what anyone
means.

:::note[Shuffle has to be a default, not a reading of the request]
Alexa sends `shuffle: false` on every fresh `Initiate`, so "the user turned it
off" and "nothing was specified" are indistinguishable at that point. The bridge
therefore defaults it on for collections and reports shuffle as **on** in the
queue state it returns, so the app shows it on and an explicit `SetShuffle` can
still turn it off for album order.
:::

Genre listings use the ordered `getSongsByGenre` rather than a random genre
sample, precisely because the queue is re-derived on every request. A random
listing would reshuffle mid-playback.

## Deterministic ordering

Shuffle is seeded on the `queueId`, not on the clock:

```python
random.Random(queue_id).shuffle(out)
```

Every later request for the same queue derives the identical order without any
of it being stored. A plain shuffle would reorder the queue on each
`GetNextItem` and playback would wander.

## Continuing past the end

`AFTER_CONTENT` decides what happens when the requested content runs out:
`stop` (the default), `artist`, `genre`, `library` or `radio`.

The obvious implementation is to hand Alexa a different contentId when the queue
ends. That is not possible.

:::caution[Content cannot be swapped mid-queue]
Alexa echoes back the contentId it was given at `Initiate` on every later
request. This was confirmed against captures: items 6, 7 and 8 of one queue all
carried the contentId the queue started with. An `Item` has no field to hand
back a different one.
:::

So continuation is a **virtual extension** instead. Indexes past the end of the
base queue map deterministically into a continuation pool:

```
contentId: ar:aB12cD34      AFTER_CONTENT=radio

index    0   1   2  ...  n-1 │ n   n+1  ...        │ ...
         └── base queue ─────┘ └─ pool, pass 0 ────┘ └─ pass 1, reshuffled
             (the artist)         (rad:aB12cD34)        on the pass number
```

The continuation is expressed as a contentId of its own, so its tracks resolve
and cache through exactly the same path as everything else. `library` becomes
`rnd:all`; `artist` and `radio` use the seed artist, which an `ar:` or `rad:`
request names outright and anything else takes from the last track of its base
order; `genre` takes the genre from that same seed track.

Two properties fall out of doing it this way:

- **Any index is still reproducible** from `(contentId, queueId, index)` alone,
  with nothing stored.
- **Continuation never pre-empts what was asked for.** It begins strictly past
  the last track of the requested content.

If the seed track has no artist or genre, which happens with imported files that
lost their tags, continuation falls back to the library at random rather than
ending on a technicality.

The continuation pool is warmed off the request path, at `Initiate`, because the
alternative is paying for it on the first `GetNextItem` past the end of the
queue, which is the one moment with no slack at all.

## Loop and repeat

Loop is the exception to statelessness. Amazon sends `SetShuffle`, `SetLoop` and
`SetRepeat` as fire-and-forget directives acknowledged with an empty response,
and expects the next `GetNextItem` to reflect the change. That state is written
to one small JSON file per queue, atomically, so a concurrent read never sees a
partial file.

With loop on, the last track is followed by the first and the queue never
reports itself finished. With loop off and no continuation configured, the
bridge answers `isQueueFinished: true` with a null item.

## Items

Each item Alexa receives carries the track's title, artist, album, art, a
duration where known, and its stream URL with an explicit `validUntil`. It also
carries its controls, which is not a formality:

:::danger[An undeclared control is a disabled control]
Amazon: "if the enablement status of a control isn't specified, Alexa assumes
the control is disabled." An empty controls list silently kills shuffle and
loop.

This is why `NEXT` is explicitly enabled on the last item of an endless queue. A
station whose skip button grays out on track twelve is not a station.
:::
