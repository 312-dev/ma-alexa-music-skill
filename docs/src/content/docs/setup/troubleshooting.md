---
title: Troubleshooting
description: The real failure modes, organised by what you can observe.
sidebar:
  order: 10
---

:::danger[There is no simulator]
`ask smapi simulate-skill` refuses music skills with "Unsupported skill type.
Please note that only custom skills are currently supported". The developer
console Test tab and the utterance profiler are custom-skill only.

Everything below is therefore built on two observations: **what arrived at the
bridge**, and **what Alexa said out loud**. If a suggestion here sounds
indirect, that is why.
:::

## Your three instruments

```sh
# 1. Did anything arrive, and what was it?
docker exec ampere ls -t /data/captures | head
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" https://ampere.example.com/captures

# 2. Can the bridge reach the music server?
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" 'https://ampere.example.com/diag?q=radiohead'

# 3. What did each handler do, and how long did it take?
docker logs --tail 100 ampere
```

`/captures` and `/diag` both return 401 unless `ADMIN_TOKEN` is set and the
`X-Admin-Token` header matches it.

## Alexa says "Here's ... from Spotify"

Or from Amazon Music, or whatever your default provider is.

**The skill is unbound.** Alexa never routed the request to you. The bridge is
usually answering everything correctly at the same time, and `ER_INGESTION` will
report `SUCCEEDED`, which is what makes this one so misleading.

The usual cause is a catalog upload. Uploading a catalog silently unbinds the
skill. Cycle enablement:

```sh
ask smapi delete-skill-enablement --skill-id <skill-id> --stage development
ask smapi set-skill-enablement    --skill-id <skill-id> --stage development
```

If you did not upload a catalog, check that the skill is enabled at all, and
that the account is still linked.

:::caution
The catalog sync cycles enablement for you after any run that uploaded
something, but only when `SKILL_ID` is set. If you land here after a sync, check
the sync log: it says loudly when it could not rebind.
:::

## Nothing reaches the bridge

The capture directory stays empty and the log shows no inbound directives. Alexa
either says it cannot find the skill, or falls back to another provider.

Work through these in order:

1. **Is the skill enabled**, in the `development` stage?
2. **Is the account linked?** Music skills require account linking before they
   are usable at all.
3. **Is the endpoint publicly reachable?** Run the
   [QR check](../validate/#4-external-proof-the-qr-check). Reachability from
   your own network proves nothing.
4. **Does `sslCertificateType` match the certificate?** `Trusted` for a SAN
   naming the exact host, `Wildcard` for `*.example.com`. A mismatch means
   Amazon never calls, and reports nothing.
5. **Is the endpoint URI in the manifest right?** The bridge accepts `POST /`
   and `POST /music`.

All five of these produce the same symptom, which is why
[validating first](../validate/) is worth the ten minutes.

## Requests arrive and are rejected with 403

You have `VERIFY_REQUESTS=on` and signature verification is failing. The log
line carries the reason.

Set `VERIFY_REQUESTS=warn` to serve anyway while you read those reasons, which
is the setting a fresh deployment starts on for exactly this purpose. Common
causes:

- `cryptography` is not installed, so verification reports itself unavailable.
- Outbound HTTPS to `s3.amazonaws.com` is blocked, so the certificate chain
  cannot be fetched. Raise `SIGNATURE_FETCH_TIMEOUT` if the network is merely
  slow.
- Something in front of the bridge is rewriting the request body. The signature
  is over the raw bytes.

## Requests arrive, but Alexa plays the wrong thing

The captures show `GetPlayableContent` and `Initiate`, so routing works. What
you get back is wrong.

**If it plays something from your library that you did not ask for**, this is an
alias or catalog collision. Alexa resolved your utterance against your uploaded
catalog before routing it. Read [Choosing an alias](../alias/); "jukebox" lost
to *Jukebox The Ghost* and *Juke Box Hero*, and "gray tunes" lost to
*Conan Gray*.

**If Alexa says it cannot find the thing you asked for**, look at the capture
for `GetPlayableContent`. The `selectionCriteria.attributes` array shows exactly
what Amazon heard and whether it resolved to an `entityId` or arrived as free
text:

- An `entityId` like `artist.<id>` means the catalog resolved it. If playback
  still failed, the id is stale: your catalog is ahead of or behind your
  library. Re-run the sync.
- Free text with no `entityId` means the catalog did not resolve it, and the
  bridge fell back to a Subsonic search. Check the same query against `/diag`.

**If the name is wrong on screen but the right music plays**, that is the
metadata lookup deliberately not blocking. The first request for an entity can
answer with no name while it warms in the background; the label improves on a
later request and the response time never moves.

## Playback starts, then fails partway

Look for `403` on `/stream` in the access log.

- **`SIGNING_KEY` is unset.** The bridge generates a random one at startup, so
  every URL minted before the last restart is now invalid. Set it explicitly.
- **The URL genuinely expired.** `STREAM_TTL` defaults to 43200 seconds. A queue
  sitting paused for longer than that will fail to resume.
- **A proxy is stripping `Range` or `Content-Range`.** The bridge forwards
  `Content-Type`, `Content-Length`, `Accept-Ranges` and `Content-Range` from
  upstream, and Alexa issues ranged GETs.

If instead playback never starts and the upstream fetch is slow, check
`SUBSONIC_TIMEOUT` (default 6 seconds) and whether the Subsonic server is
transcoding on demand for a large library.

## The station is only the seed artist

A station is the seed artist plus similar artists that exist in your library. If
the similar-artist lookup returns nothing, there is nothing to build from.

Check that `getArtistInfo2` works on your server for that artist. Some servers
return similar artists that are not in the library; the bridge filters those
out, including entries marked with a negative id.

This used to be permanent once it happened, because the degraded pool was
cached. It no longer is: neither the artist lookup nor the pool built from it is
cached unless the lookup returned more than the seed. If you are on an older
build, restarting the process clears it.

## Music stops when the content runs out

That is the default. `AFTER_CONTENT` is `stop`, because a queue that ends is
what both Alexa and the listener expect.

Set it to `artist`, `genre`, `library` or `radio` to keep going past the last
track. Continuation never pre-empts what was asked for; it begins strictly after
the requested content.

Stations ignore the setting and always continue, because a station that ends is
not a station.

## A manifest update fails with RESOURCE_NOT_FOUND

Against `largeIconUri` or `smallIconUri`, usually.

Amazon's validator re-fetches the icons on every manifest update and sends a
conditional request. A `304 Not Modified` is reported as `RESOURCE_NOT_FOUND`
and fails the whole update. The bridge serves `/icons` with no cache validators
at all for this reason, so:

- Confirm the icon files are being served at all. They are baked into the image
  at `/app/icons`, so this only bites if `ICON_DIR` was pointed elsewhere.
- Confirm nothing in front of the bridge is adding an `ETag`,
  `Last-Modified`, or a cache layer that answers 304.

```sh
curl -sI https://ampere.example.com/icons/ampere-512.png
```

There should be no `ETag` and no `Last-Modified` in that response.

## The Alexa app shows no scrubber

Known, and unexplained. The bridge declares
`{"type": "ADJUST", "name": "SEEK_POSITION", "enabled": true}` alongside a known
duration, over a stream that answers `206` with `Content-Range` end to end.
First-party providers show a scrubber; this does not. No cause has been
established. See [Gaps and limits](../../reference/limits/).

## The first track of a station is slow

Building a station costs a similar-artist lookup plus one discography walk per
artist, about three seconds cold. The bridge warms the pool off the request path
when it can: on an explicit station request during `GetPlayableContent`, and on
`Initiate` for any queue that will later continue into one.

If you see the delay anyway, it is a cold cache with no warming opportunity.
Playing the same station again will be fast.

## Everything is slow, and the timings look bimodal

Check you are running one gunicorn worker. The caches are in-process, so two
workers give you two of everything: a queue warmed on one is still cold on the
other, and roughly half of all track transitions pay the full build cost.

## Ingestion looks stuck

It is probably not. `SLU_MODELING: PENDING` is normal and takes weeks, and the
top-level upload status is pinned by it, so `IN_PROGRESS` there means nothing.
`ER_INGESTION: SUCCEEDED` is the only state that gates voice. See
[Catalog and enablement](../catalog/#what-the-ingestion-states-mean).
