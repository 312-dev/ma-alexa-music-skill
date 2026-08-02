---
title: Directives
description: Every directive the bridge handles, what it answers with, and the namespace and version quirks.
---

Directives arrive as JSON on `POST /music` (and `POST /`). The bridge dispatches
on `(header.namespace, header.name)` and writes every request to the capture
directory before handling it.

An unrecognized pair is answered with `INVALID_DIRECTIVE` at payload version
3.0, and logged.

## The routing table

| Directive namespace | Name | What the handler does |
|---|---|---|
| `Alexa.Media.Search` | `GetPlayableContent` | Turns what the user said into a contentId |
| `Alexa.Media.Search` | `GetDisplayableContent` | Builds browse shelves for a screen device |
| `Alexa.Media.Playback` | `Initiate` | Opens a queue and returns its first item |
| `Alexa.Audio.PlayQueue` | `GetNextItem` | The item after this one, or `isQueueFinished` |
| `Alexa.Audio.PlayQueue` | `GetPreviousItem` | The item before this one |
| `Alexa.Audio.PlayQueue` | `JumpToItem` | The item at an explicit index |
| `Alexa.Media.PlayQueue` | `GetItem` | The item at an index, by target reference |
| `Alexa.Media.PlayQueue` | `GetView` | A window of up to 10 items for the queue view |
| `Alexa.Media.PlayQueue` | `SetShuffle` | Records shuffle for this queue |
| `Alexa.Media.PlayQueue` | `SetLoop` | Records loop for this queue |
| `Alexa.Media.PlayQueue` | `SetRepeat` | Records repeat status for this queue |
| `Alexa.UserPreference` | `ReceiveFeedback` | Thumbs up or down, written through as stars |

## Response namespaces do not always match

:::caution[`GetItem` and `GetView` answer on a different namespace than they arrive on]
Both directives arrive on `Alexa.Media.PlayQueue` and are answered on
`Alexa.Audio.PlayQueue`. This matches Amazon's own examples, and it is not a
typo in this codebase.
:::

Payload versions vary too, and the variation is load-bearing:

| Response | Payload version | Note |
|---|---|---|
| `GetPlayableContent.Response` | 1.0 | |
| `GetDisplayableContent.Response` | 2.0 | Errors from it still go out on `Alexa.Media` at 1.0 |
| `Initiate.Response` | 1.0 | |
| Queue item responses | 1.0 | |
| `Alexa.Response` acknowledging `Set*` | **3.0** | |
| `ReceiveFeedback.Response` | 2.0 | |

The empty `Alexa.Response` that acknowledges `SetShuffle`, `SetLoop` and
`SetRepeat` is the odd one. Amazon documents the payload as empty and gives no
state echo, and the only literal example they publish carries payload version
3.0 even though the PlayQueue directives themselves are 1.0. Mirroring 1.0
instead left the Alexa app never registering the new shuffle or loop state.

## Two reference shapes

Amazon sends the queue reference two different ways, and the bridge normalizes
both:

```json
// GetNextItem, GetPreviousItem, JumpToItem
{ "currentItemReference": { "id": "3", "queueId": "...", "contentId": "ar:..." } }

// SetShuffle, SetLoop, SetRepeat, GetView, GetItem
{ "currentItemReference": { "namespace": "...", "name": "...",
                            "value": { "id": "3", "queueId": "...", "contentId": "ar:..." } } }
```

`GetItem` uses `targetItemReference` rather than `currentItemReference`.

## GetPlayableContent in detail

This is where a spoken phrase becomes a contentId. In order:

1. **Station detection.** Is `MEDIA_TYPE` `STATION` or `RADIO`, or does the free
   text end in "radio" or "station"? If so, strip the word and resolve an
   artist. See [Stations](../stations/).
2. **Catalog entity hit.** If any attribute carries an `entityId` such as
   `artist.<id>`, that is a direct hit from your uploaded catalog. It maps
   straight to a contentId with no search at all.
3. **Free-text search.** Otherwise the attribute values are joined and searched.
   Alexa tells the bridge what *kind* of thing was asked for, and that is
   honored: an artist request queues that artist rather than a track whose
   title happens to match better.

| Requested kind | Search preference |
|---|---|
| `ARTIST` | artist, album, song |
| `ALBUM` | album, artist, song |
| `TRACK` | song, album, artist |
| `GENRE` | genre |
| `PLAYLIST` | playlist |
| nothing stated | playlist, song, album, artist |

Playlists are matched against the playlist list directly rather than through
search, because Subsonic's `search3` does not cover playlists. The trailing word
"playlist" is normalized away first, and casing is ignored.

An empty query with no criteria, which is what `play <alias>` on its own
produces, plays the library at random.

:::note[`MediaMetadata.type` is a closed enum]
`ALBUM`, `ARTIST`, `GENRE`, `PLAYLIST`, `TRACK` for music, plus `STATION` and
`PROGRAM` for radio and podcasts. An earlier version answered `TRACK` for
everything, so an artist request came back described as a track.
:::

Display names are looked up from the music server, but **never on the request
path**. A miss returns nothing and warms the cache in the background, so the
label improves on a later request and the response time never moves. Artist
names are loaded up front at startup in a single call, because artists are the
overwhelmingly common case.

## GetDisplayableContent

Browse and show experiences on Echo Show, Fire TV and in the Alexa app. It
differs from `GetPlayableContent` in three ways: it returns grouped shelves
rather than a single result, it takes an array of resolved criteria, and it
answers on payload version 2.0.

With search terms it returns Artists, Albums and Songs shelves. With no criteria
at all, or with the undocumented `SEARCH_METHOD` attribute that appears for
contextual recommendations, it returns your playlists and your genres.

Requests carry no device or viewport information, so there is no way to pick an
art size per screen. Every rung of the size ladder is returned: 48, 60, 110, 256
and 600 pixels square.

## ReceiveFeedback

Thumbs up stars the track on your Subsonic server; thumbs down unstars it and
also answers `skip: true`, after which Alexa asks for the next item.

Feedback is resolved against the same index arithmetic as playback, so a thumb
on a track the continuation is playing stars that track rather than falling off
the end of the base queue.
