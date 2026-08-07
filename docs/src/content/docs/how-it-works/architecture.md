---
title: Architecture
description: How a spoken request becomes audio, and why the bridge proxies its own assets.
---

Music Assistant sits between two APIs it does not control: Amazon's Music, Radio, and
Podcast Skill API on one side, and the Subsonic API on the other. Nothing in it
is specific to one music server.

## Request flow

<figure class="ma-alexa-figure">
<div class="ma-alexa-scroll">
<svg viewBox="0 0 720 232" xmlns="http://www.w3.org/2000/svg" role="img" aria-labeledby="flow-title">
  <title id="flow-title">Echo talks to Amazon, Amazon talks to the Music Assistant bridge, the bridge talks to your Subsonic server. Amazon separately fetches audio and cover art from signed, expiring URLs on the bridge.</title>
  <defs>
    <marker id="flow-ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path class="arrow" d="M0 0 L10 5 L0 10 z"/>
    </marker>
    <marker id="flow-ar-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path class="arrow-accent" d="M0 0 L10 5 L0 10 z"/>
    </marker>
  </defs>

  <rect class="node" x="10" y="36" width="140" height="56" rx="8"/>
  <text class="label" x="80" y="60" text-anchor="middle">Echo device</text>
  <text class="label-sub" x="80" y="78" text-anchor="middle">or Echo group</text>

  <rect class="node" x="195" y="36" width="140" height="56" rx="8"/>
  <text class="label" x="265" y="60" text-anchor="middle">Amazon</text>
  <text class="label-sub" x="265" y="78" text-anchor="middle">Alexa service</text>

  <rect class="node-accent" x="380" y="36" width="140" height="56" rx="8"/>
  <text class="label" x="450" y="60" text-anchor="middle">Music Assistant</text>
  <text class="label-sub" x="450" y="78" text-anchor="middle">the bridge</text>

  <rect class="node" x="565" y="36" width="145" height="56" rx="8"/>
  <text class="label" x="637" y="60" text-anchor="middle">Subsonic server</text>
  <text class="label-sub" x="637" y="78" text-anchor="middle">not internet-facing</text>

  <line class="edge" x1="152" y1="64" x2="187" y2="64" marker-end="url(#flow-ar)"/>
  <line class="edge" x1="337" y1="64" x2="372" y2="64" marker-end="url(#flow-ar)"/>
  <line class="edge" x1="522" y1="64" x2="557" y2="64" marker-end="url(#flow-ar)"/>

  <text class="label-sub" x="169" y="26" text-anchor="middle">voice</text>
  <text class="label-sub" x="354" y="26" text-anchor="middle">POST /music</text>
  <text class="label-sub" x="539" y="20" text-anchor="middle">Subsonic</text>
  <text class="label-sub" x="539" y="32" text-anchor="middle">API</text>

  <path class="edge-accent" d="M265 94 L265 172 L450 172 L450 100" marker-end="url(#flow-ar-accent)"/>
  <text class="label-sub" x="357" y="192" text-anchor="middle">GET /stream/&lt;id&gt;/&lt;expires&gt;/&lt;sig&gt;</text>
  <text class="label-sub" x="357" y="208" text-anchor="middle">GET /art/&lt;id&gt;/&lt;expires&gt;/&lt;sig&gt;</text>
</svg>
</div>
<figcaption>Two conversations, not one. Directives arrive on the top path; audio and art are fetched separately on the amber path.</figcaption>
</figure>

## Two conversations

The distinction matters more than it looks.

**Directives** are small JSON envelopes on `POST /music`. Amazon asks what to
play, asks for the next item, tells you shuffle changed. These are latency
critical: `Initiate` has a 100ms p50 and 400ms p99 budget.

Each one is verified against Amazon's signature headers before it is handled.
The certificate chain URL is pinned to Amazon's own bucket, normalized before
the prefix is compared, validated once and then cached for an hour, so a
verified request costs one dict lookup and one RSA verify. The music envelope
carries no timestamp, so there is no replay window to check. See
[Gaps and limits](../../reference/limits/).

**Assets** are audio and cover art, fetched later, by different Amazon
infrastructure, over URLs the bridge handed out in a directive response. These
are throughput work, not latency work, and they are the reason the container
runs threads rather than a single synchronous worker: a blocking worker serving
a three-minute song would stall every directive behind it.

## Why the bridge proxies its own assets

A self-hosted music server is usually not reachable from the internet. The
original deployment's Navidrome lives on a tailnet with no public route at all.

Amazon has to fetch audio from somewhere it can reach, so the bridge stands in
front. Every item it hands back carries a URL on its own public hostname:

```
https://music.example.com/stream/<song-id>/<expires>/<signature>
https://music.example.com/art/<cover-id>/<expires>/<signature>
```

The signature is `HMAC-SHA256(SIGNING_KEY, "<kind>:<id>:<expires>")`, truncated
to 32 hex characters, checked with a constant-time comparison. An expired or
altered URL gets a 403.

This is a feature rather than a workaround. It means:

- Amazon never holds a credentialed, permanent link into your music server.
- URL lifetime is yours to set (`STREAM_TTL`, 12 hours by default).
- Transcoding is decided by the bridge, which sends MP3 at 256 kbit/s.
- `Range` requests pass through end to end, so ranged GETs are satisfied.

:::note[`stream.validUntil` defaults to about 60 seconds]
If a music skill omits `validUntil` on a stream, Amazon treats the URL as valid
for roughly a minute. The bridge always sets it explicitly, from the same expiry
that is baked into the signature.
:::

## Stateless where it counts

Alexa echoes `{id, queueId, contentId}` back on every queue request, so the
bridge stores no per-user playback position. A queue is re-derived from its
contentId on each request, backed by a small in-process cache.

That is what keeps the hot path inside Amazon's budget, and it is worth its own
page: [Queues and contentIds](../queues/).

The one thing that cannot be derived is shuffle, loop and repeat state, because
Amazon sends those as fire-and-forget directives whose only acknowledgement is
an empty response, and expects the very next `GetNextItem` to reflect the
change. Those live in one small JSON file per queue, written atomically.

## Process shape

```
gunicorn --bind 0.0.0.0:5056 --workers 1 --threads 8 app:app
```

One worker, because every cache is in-process. Two workers meant two of
everything, and roughly half of all track transitions paid a full three-second
station build against a cache that had been warmed on the other worker.

Eight threads, because `/stream` proxies audio through the same application and
the work is entirely I/O bound. The GIL costs nothing here.

### What is cached

| Cache | Keyed on | Why it is separate |
|---|---|---|
| Queue tracks | contentId | Whole song records, not ids. An earlier version kept ids and re-fetched each song, which cost a round trip per track and pushed `Initiate` past three seconds. |
| Similar artists | seed artist id | The expensive half of a station at about 725ms, and its answer does not go stale when the track cache turns over. |
| Display names and art | entity id | Warmed in the background, never on the request path. Artist names are loaded up front at startup in one `getArtists` call. |

Failed lookups are deliberately not cached. Caching a station that fell back to
its seed artist alone pinned that failure until the process restarted, which is
exactly what happened once and is why the rule exists.

## What is on disk

| Path | Contents |
|---|---|
| `/data/queuestate` | One small JSON file per queue: shuffle, loop, repeat |
| `/data/captures` | Every inbound directive, headers and body |
| `/data/icons` | Skill icons Amazon fetches for the manifest |

Nothing else persists. There is no database and no user table.
