# Music Assistant Alexa Music Skill bridge

An Alexa **Music Skill** (not a custom skill) that plays a self-hosted
Navidrome library on Echo devices.

## Why this exists

Alexa custom skills using the `AudioPlayer` interface **cannot target
multi-room music groups**. Only first-party providers (Amazon Music, Spotify,
TuneIn, ...) get multi-room, because the Whole Home Audio group object has no
dialog interface and can only be a routing target.

Amazon's separate **Music, Radio, and Podcast Skill API** does integrate with
multi-room music, and music skills are self-service: no Amazon representative,
no certification needed for private development-stage use.

## Findings that shaped the design

- **Music skills accept an HTTPS endpoint, not just a Lambda ARN.** Amazon's
  music-skill docs only show a Lambda ARN, but SMAPI rejects the manifest with
  `MISSING_REQUIRED_PROPERTY: sslCertificateType`, a field that only applies to
  HTTPS endpoints. No AWS account is required.
- **`stream.validUntil` defaults to roughly 60 seconds** when omitted. Always
  set it explicitly.
- **`GetItem.Response` uses namespace `Alexa.Audio.PlayQueue`** even though the
  directive arrives on `Alexa.Media.PlayQueue`.
- **Alexa carries queue position for you.** Every queue request echoes back
  `{id, queueId, contentId}`, so the service stores no per-user playback state.
  This matters: `Initiate` has a 100ms p50 / 400ms p99 budget.
- Navidrome is tailnet-only and not internet-routable, so audio and cover art
  are proxied through this service behind expiring HMAC-signed URLs rather than
  handing Amazon a permanent credentialed link.

## Architecture

```
Echo -> Amazon -> alexa-music.graysons.network -> bridge -> Navidrome (tailnet)
                                                    |
                                          signed, expiring /stream + /art
```

`contentId` encodes what to play (`tr:`, `al:`, `ar:`, `gen:`, `rnd:`), and the
track list is re-derived from it on each request, with a small in-memory cache.

## Routes

| Route | Purpose |
|---|---|
| `POST /music` | Alexa directives |
| `GET /stream/<id>/<expires>/<sig>` | Transcoded MP3 proxy |
| `GET /art/<id>/<expires>/<sig>` | Cover art proxy |
| `GET /icons/<name>` | Skill icons for the manifest |
| `GET /healthz` | Liveness |
| `GET /captures`, `GET /diag` | Introspection, `X-Admin-Token` required |

## Deploy

Nomad job `alexa-music` on the Hetzner box, digest-pinned, host network on
`:5056`. Secrets live in Nomad var `nomad/jobs/alexa-music` and in the
1Password MCP vault under `hetzner/alexa-music/*`.

```sh
rsync app.py subsonic.py Dockerfile root@box:/opt/alexa-music/src/
docker build --provenance=false --sbom=false -t localhost:5000/alexa-music:probe .
docker push localhost:5000/alexa-music:probe   # then pin the digest in the job
nomad job run /opt/nomad/alexa-music.nomad.hcl
```

## Known gaps

- **No inbound request verification.** Amazon does not document request auth for
  music-skill HTTPS endpoints, and the custom-skill replay check binds to a
  `request.timestamp` field the music envelope does not contain. `POST /music`
  currently accepts any caller.
- Multi-room targeting via `Alexa.Music.PlaySearchPhrase` is **not yet
  verified**; the skill does not appear in `behaviors/entities` yet.
- No catalog uploaded, so voice resolution relies on Alexa's generic parsing
  rather than entity resolution against the library.
