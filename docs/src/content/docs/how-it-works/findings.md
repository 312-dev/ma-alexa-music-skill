---
title: Findings
description: Things that are true about the Music, Radio, and Podcast Skill API and are not obvious from the documentation.
---

Each of these cost time. They are collected here because most of them are not in
Amazon's documentation, are contradicted by it, or are only visible in an error
message you have to provoke.

## Getting a skill to exist

### A music skill accepts an HTTPS endpoint, not only a Lambda ARN

Amazon's music-skill documentation shows a Lambda ARN and nothing else, which
reads as a requirement. It is not.

The evidence is in the rejection message. SMAPI refuses the manifest with
`MISSING_REQUIRED_PROPERTY: sslCertificateType`, and `sslCertificateType` is a
field that only applies to HTTPS endpoints. The API is asking for something the
documentation never shows.

No AWS account is required.

### Icons must carry no cache validators

Amazon's manifest validator (`AlexaSkillManagementAPI/1.0`) re-fetches the skill
icons on every manifest update, and it sends a conditional request.

Flask's default conditional handling answered `304 Not Modified` with an empty
body. Amazon reported that as `RESOURCE_NOT_FOUND` against `largeIconUri` and
failed the entire update.

`send_from_directory(conditional=False)` is not enough, because it still emits an
`ETag` and a validator that echoes it back still gets a 304. The only reliable
fix is to send **no validators at all**: read the bytes, set
`Cache-Control: no-store`, return them every time. The files are tens of
kilobytes and fetched rarely, so nothing is lost.

### The Alexa app opens the endpoint root in a webview

Serving only `POST` at `/` returns a 405 with no body, which the webview renders
as a blank black screen. It looks like a failure and is not. The bridge serves a
small landing page at `GET /` for that reason.

### Music skills cannot be simulated

`ask smapi simulate-skill` refuses with "Unsupported skill type. Please note that
only custom skills are currently supported". The developer console Test tab and
the utterance profiler are custom-skill only.

Every diagnosis has to come from inbound request captures plus what Alexa says
out loud. This is the single most expensive constraint in the project, and it is
worth knowing on day one rather than day three.

## Answering directives

### An undeclared control is a disabled control

Amazon's own wording: "if the enablement status of a control isn't specified,
Alexa assumes the control is disabled."

An empty controls list therefore silently kills shuffle and loop. There is no
error and no warning; the buttons are inert. The same rule is why `NEXT`
has to be explicitly enabled on the last item of an endless queue, and why
`SEEK_POSITION` is declared at all.

### `stream.validUntil` defaults to about 60 seconds

Omit it and Amazon treats the stream URL as valid for roughly a minute. Always
set it explicitly.

### `GetItem.Response` uses namespace `Alexa.Audio.PlayQueue`

Even though the directive arrives on `Alexa.Media.PlayQueue`. `GetView` behaves
the same way. This matches Amazon's own examples.

### The empty acknowledgement carries payload version 3.0

`SetShuffle`, `SetLoop` and `SetRepeat` are acknowledged with an empty
`Alexa.Response`. Amazon documents the payload as empty and gives no state echo,
and the only literal example they publish carries payload version 3.0 even
though the PlayQueue directives themselves are 1.0.

Mirroring 1.0 instead left the Alexa app never registering the new state.

### `REPEAT` is emitted as type `CYCLE`

That combination appears only in Amazon's `Initiate` examples, and not in the
published `BaseControl` or `QueueControl` tables. It is the only shape they have
ever shown for it.

### `speech.text` is what renders on screen

Amazon's examples lowercase `speech.text`, because they use it for text to
speech, and put natural case in `display`. But their own documentation notes
that "Currently the Alexa service ignores this [display] field".

So `speech.text` is what actually appears in the player. Lowercasing it made
every artist and album name render lowercase on screen. Natural case reads
correctly and speaks the same.

### Art is required, and an empty source list is not an answer

`art` is a required member of `MediaMetadata`, and every `ArtSource` requires an
HTTPS url. Content that legitimately has no cover, such as a genre or the whole
library, still needs something. The bridge falls back to the skill icon.

### `GetView` is truncated at 10 items

Amazon discards anything past ten, so the bridge sends a window around the
current track rather than the whole queue.

### Alexa sends `shuffle: false` on every fresh `Initiate`

Which makes "the user turned shuffle off" and "nothing was specified"
indistinguishable at that point. Any shuffle-by-default behavior has to be a
policy decision in your code, not a reading of the request.

## Content and catalog

### Alexa carries queue position for you

Every queue request echoes back `{id, queueId, contentId}`, so the service can
store no per-user playback state at all. This is not merely convenient: it is
what makes the 100ms p50 and 400ms p99 budget on `Initiate` achievable.

### Content cannot be swapped mid-queue

Alexa echoes back the contentId it was handed at `Initiate` on every later
request. This was confirmed against captures: items 6, 7 and 8 of one queue all
carried the contentId the queue started with, and an `Item` has no field to hand
back a different one.

A queue that continues past its end therefore has to **extend itself**, not
switch. See [Queues and contentIds](../queues/#continuing-past-the-end).

### Uploading a catalog silently unbinds the skill

Playback falls back to the default provider and Alexa announces it ("Here's ...
from Spotify") while the skill still answers every request correctly and
`ER_INGESTION` reports `SUCCEEDED`.

The fix is to cycle enablement: `ask smapi delete-skill-enablement` then
`set-skill-enablement`, stage `development`. Nothing about the failure points at
enablement, which is what makes it costly. The catalog sync now does the cycle
itself after any run that uploaded something, provided it knows the skill id.

### A recreated skill sits half provisioned until the manifest is re-PUT

After deleting and recreating a skill, everything reads correct. A signed
`GetPlayableContent` arrives, entity resolution answers with a catalog id, the
response is spec-valid, the skill shows enabled, and a replayed `Initiate`
returns a working stream queue. Amazon simply never sends `Initiate`.

**Cycling enablement alone does not fix this one.** Re-PUT the manifest (GET it,
PUT it back), wait for `lastUpdateRequest` to report `SUCCEEDED`, and only then
cycle. The manifest update is what forces Amazon to re-run the skill's
music-provider provisioning; the cycle then binds against it.

This is why the wizard's enablement step does re-PUT, poll, delete, set rather
than just delete and set.

### The provider-slot binding decays on a clock

The worst of them, because nothing anywhere reports it.

Enablement is not a one-time provisioning step. The binding between the skill
and its provider slot **rots on its own within hours**, with no upload, no
manifest change and no deploy involved. Observed: playback working 16 minutes
after a cycle, dead 7 hours later. Throughout the failure, `enablement_status`
reports enabled, the search resolves against the catalog, and the bridge answers
200 with a valid payload. `Initiate` never comes. The only symptom is silence.

Identical requests fail before a cycle and succeed seconds after one, which is
what rules out the response being the variable.

**Amazon's acknowledgement proves nothing.** A cycle returning 200 is not
evidence the slot is bound, because the status reports bound during the outage
too. This is the trap the whole failure mode lives in, and any fix that treats
the API's answer as confirmation will report success while the skill stays dead.

The only honest measure is your own inbound traffic: for each
`Alexa.Media.Search.GetPlayableContent`, did an `Alexa.Media.Playback.Initiate`
or an `Alexa.Audio.PlayQueue.GetNextItem` follow it before the next search did.
`GetNextItem` counts because it only arrives at a track boundary of a session
this skill is already serving, so it proves the binding as surely as an
`Initiate` does. The bridge counts exactly that ratio and shows it on the Status
page. Two mechanisms act on it:

- **A keep-alive** re-provisions when the enablement is older than
  `BINDING_KEEPALIVE_HOURS` (default 4) and nothing is currently playing.
- **A miss detector** cycles once when **two or more searches in a row** fail to
  reach playback, then **disarms until playback is actually observed**. One
  attempt per outage. Without that breaker, a cause a cycle cannot fix would
  churn the enablement against Amazon's rate limits on every failed request.

:::danger[A single unanswered search is not an outage]
This is the expensive one, and it was learned by shipping the wrong version
first. A voice transfer between speakers emits one search and no `Initiate`, by
design, because Amazon never asks the skill to start anything (see
[Moving audio between rooms](#moving-audio-between-rooms-never-reaches-the-skill)).
A superseded or cancelled utterance looks identical.

A detector that cycles on one miss will therefore re-provision the skill in the
middle of healthy playback, which **breaks the session it was trying to
protect**. Measured here: a cycle fired one second after a successful `Initiate`
while an Echo was mid-stream. Every failure that evening clustered around a
cycle, and none recurred once the threshold was raised. An unbounded reactor
does not merely waste rate limit, it manufactures the outage it detects.

Count misses by the instant the search happened, too. Counting per check
interval instead reported ten misses across a window in which no search arrived
at all.
:::

:::caution[Read the ratio from filenames, not mtimes]
If you build something similar: order the captures by their filename timestamp,
not by file mtime. Anything that rewrites a capture in place, such as a pass
that strips credentials out of old ones, resets its mtime and collapses the
history into one instant. Measured here, that misclassified 37 of 108 pairs
while leaving the aggregate close enough to look healthy.
:::

### The alias competes with your own catalog

Alexa resolves content against your uploaded catalog **before** it routes to a
provider, so every artist and track you own is competing with your invocation
name.

"jukebox" was eaten by *Jukebox The Ghost* and *Juke Box Hero*. "gray tunes"
became the artist *Conan Gray*. "phono" was consistently heard as "Sonos" by
speech recognition, which is a different failure with the same outcome.

Collisions are per-library, so this cannot be a constant in anyone's code. See
[Choosing an alias](../../setup/alias/).

### `lastUpdatedTime` decides what Amazon reprocesses

From their documentation: "If you upload a catalog with changed entries but an
unchanged lastUpdatedTime field, the changes might be ignored."

Two failure modes follow. Stamping everything with "now" makes Amazon reprocess
the whole catalog on every run. Omitting a removed track leaves it in Amazon's
entity resolution forever, so Alexa keeps offering songs that no longer exist. A
removal needs an explicit `deleted: true` tombstone.

### `ER_INGESTION` is the only state that gates voice

`SLU_MODELING: PENDING` is normal and takes weeks. The top-level upload status is
pinned by it, so `IN_PROGRESS` there means nothing at all.

## Performance

### `getArtistInfo2` is affordable, `getSimilarSongs2` is not

Similar artists come back in about 725ms. Similar *songs* take about 10 seconds,
every time, uncached. For a long while similar songs looked unsupported by the
server; they were exceeding the client timeout.

Stations are built from similar artists for that reason alone.

`getTopSongs` is a separate case: about 0.9s and empty against the test library.
That is a fact about `getTopSongs`, not about Last.fm-backed endpoints generally,
which matters because `getArtistInfo2` is equally Last.fm-backed and works well.

### Cache whole records, not ids

An earlier version cached track ids and re-fetched each song with `getSong` on
every item. That cost one round trip per track and pushed `Initiate` past three
seconds against a 400ms p99 budget. Subsonic returns full song records from
album, playlist and genre listings, so there is no reason to go back for them.

### A cache should hold answers, not failures

The music server returned no similar artists for one artist, once. The result was
cached, and every request afterwards served a station of that artist alone until
the process restarted, because the cache never re-derives an entry it already
holds.

Now neither the similar-artist lookup nor the pool built from it is cached unless
the lookup returned more than the seed. The same rule applies to display-name
lookups.

### One worker, because the caches are in-process

Two gunicorn workers meant two of everything. A queue warmed on one worker was
still cold on the other, and roughly half of all track transitions paid a full
three-second station build. One worker with eight threads, on entirely I/O-bound
work, is the right shape.

### Multi-room does not multiply bandwidth

About 115 MB per listening hour at MP3 256 kbit/s, whether one Echo is playing or
four in the same group. Alexa fetches the stream once and distributes it locally.
Confirmed from bridge logs while four Echoes played.

Starting a **group** does fetch twice within a few seconds, on two different
tracks. That is Alexa buffering the next track to keep the group in sync, not
per-device pulling: it does not scale with member count. Starting a single
speaker fetches once.

### Moving audio between rooms never reaches the skill

Saying "move the music to the kitchen" produces **no directive at all**. Not a
search, not an `Initiate`, nothing. Amazon re-forms the speaker cluster inside
its own audio layer and has the new master re-request the URL the previous one
was already playing:

```
22:31:50  GET /stream/5YQ3odIi.../...  200  6146115   studio starts the track
22:31:56  GET /stream/5YQ3odIi.../...  200    81389   probe
22:31:59  GET /stream/5YQ3odIi.../...  206  5908871   kitchen joins mid-track
```

A probe, then a `Range` request, on an **unchanged URL**. Observed in both
directions.

That makes three properties of a signed stream URL load-bearing for multi-room
rather than incidental, and none of them are visible as a multi-room feature
when you are looking at the streaming code:

1. **Not bound to whichever device asked first.** The second Echo presents the
   identical URL. Anything that pinned a URL to a session or a client address
   would break room-to-room moves and nothing else.
2. **`Range` forwarded, `206` and `Content-Range` preserved.** Answering `200`
   with the whole file restarts the song in every room.
3. **Valid far longer than a track.** A move near the end of a long track
   re-requests a URL minted when the track began. Ampere signs for 12 hours.

Ampere pins all three in `tests/test_security.py` for that reason.

## Serving Music Assistant's own sources

### Alexa plays a stream with no length and no ranges

Music Assistant serves realtime audio: always `200`, no `Content-Length`, no
`Accept-Ranges`, chunked transfer encoding. The expectation was that Alexa
would refuse it, or stall, or behave strangely at track boundaries.

It does none of that. A 14-track Deezer album played start to finish through a
chunked, length-less stream, with correct per-track metadata and working skip.
Alexa sent **zero** range requests across four stream fetches.

Ranges come from exactly two things: scrubbing the progress bar, and moving
audio between rooms. Neither happens on a first fetch, which is why a source
that cannot answer one looks perfect right up until someone touches it.

Both were then confirmed against a real Echo and a real buffered track, and
both are byte ranges against the identical URL:

```
# dragging the progress bar in the Alexa app
GET /mastream/<ref>/...  206  4455002  "Echo/1.0(APNG)"

# "Alexa, move the music to the bedroom"
GET /mastream/<ref>/...  200        0  "Echo/1.0(APNG)"   <- probe
GET /mastream/<ref>/...  206  2639362  "Echo/1.0(APNG)"   <- resumed
```

The room move sends **no directive of any kind** to the skill. Amazon re-forms
the audio cluster in its own layer and has the new speaker re-pull the same
stream URL from wherever playback had reached. So the only thing that makes a
transfer work is the stream URL being device-agnostic, long-lived and
range-capable; nothing in the skill ever learns that it happened.

### A stream reference must not be a stream URL

MA's own per-track stream URL is scoped to a queue session:

```
/single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}
```

Alexa is handed the whole track list at `Initiate` and may fetch track twelve
an hour later, long after MA has rolled that session. Keying on the item's
**uri** instead removes sessions from the problem: every track resolves
independently at fetch time and stays resolvable for as long as it exists.

This dissolved what had been written down as the estimate-breaking unknown of
the buffering work. It was not solved by the cache; it was solved by choosing a
different name for a track one phase earlier.

### The route is on port 8097, not 8095

Music Assistant runs two webservers: the API and frontend on 8095, and a
separate streams server on 8097. `mass.streams.register_dynamic_route`
registers against the second. Asking 8095 for that path returns a bare `404`
with an empty body, which reads exactly like a route that was never registered
at all.

### Buffer the whole track, because it takes about five seconds

The obvious design for serving a non-seekable source seekably is intricate:
serve ranges out of a growing file, or force a constant bitrate so byte offsets
map to time. Both exist to avoid waiting for a complete object.

One measurement removed the need for either. Music Assistant produces a
complete track in about 4.5 seconds, first byte in 0.1:

```
bytes=9058264  total=4.726s  firstbyte=0.206s
bytes=8503423  total=4.376s  firstbyte=0.101s
```

So the whole track is buffered to disk and then served as an ordinary file,
which answers ranges exactly and needs no cleverness at all. The wait is
usually zero anyway: a queue is published a second or two before the utterance
and several before the first audio fetch, and prefetching starts at publish.

:::tip[Measure before designing around a cost]
The buffering work was rated the highest-risk phase of the plan and estimated
at 400 to 800 lines with a new subsystem. It came in far smaller, because every
complicated part of the design existed to avoid a five-second wait that nobody
had timed.
:::

### mediaProgress and mediaLength are seconds

`/api/np/player` returns both in **seconds**, despite names that read like
milliseconds:

```
{'mediaLength': 226, 'mediaProgress': 11, 'allowScrubbing': False}
{'mediaLength': 226, 'mediaProgress': 21, ...}   # ten seconds later
```

Dividing them by 1000 made the reported position advance at a thousandth of
real time, and made the duration round to zero. The zero was invisible, because
the code carries a field forward when a poll omits it, so Music Assistant's own
duration silently stood in for Alexa's. Two tests asserted the wrong value
against the wrong input and passed, because both halves were wrong together.

### A live stream needs less, not more

Internet radio looked like the case that would need special handling, and it
needs the opposite: everything that makes a track work has to be switched off.
No buffer, because an endless stream has no end to buffer to and trying would
write until the disk filled. No `durationInMilliseconds`, because a progress
bar over something with no end is a lie the app will happily render. No
`SEEK_POSITION`, because there is no position.

What is left is exactly Music Assistant's own realtime output, which was always
the right shape for endless audio and only ever wrong for tracks.

The kind of thing is read out of the reference rather than carried beside it. A
Music Assistant uri is `provider://mediatype/itemid`, so `somafm://radio/...`
already says radio. That is not just tidier: it means a queue published
yesterday decides correctly today, and nothing that publishes a queue can
misdescribe what it is publishing.

One thing came for free. Alexa reports the station's **currently playing track**
rather than the station name, picked up from ICY metadata and surfaced through
the ordinary state poll, so live now-playing works with no code at all.
