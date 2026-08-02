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
follow it before the next search did. The bridge counts exactly that ratio and
shows it on the Status page. Two mechanisms act on it:

- **A keep-alive** re-provisions when the enablement is older than
  `BINDING_KEEPALIVE_HOURS` (default 4) and nothing is currently playing.
- **A miss detector** cycles once when a search fails to reach playback, then
  **disarms until an `Initiate` is actually observed**. One attempt per outage.
  Without that breaker, a cause a cycle cannot fix would churn the enablement
  against Amazon's rate limits on every failed request.

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
