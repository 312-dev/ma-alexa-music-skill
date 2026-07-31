---
title: Routes
description: Every HTTP route the bridge serves, and who is expected to call it.
---

## Alexa

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/music` | POST | none | Alexa directives. See [Directives](../../how-it-works/directives/). |
| `/` | POST | none | The same handler, for manifests that point at the origin root. |

A body that is not a JSON object is answered with `INVALID_DIRECTIVE` and a 400.
An unrecognised namespace and name pair is answered with `INVALID_DIRECTIVE` at
payload version 3.0 and logged.

Inbound requests are verified against Amazon's signature headers, under
`VERIFY_REQUESTS`: `off` logs the outcome only, `warn` logs a warning and serves
anyway, `on` rejects with 403. The default is `warn`.

The certificate chain URL is pinned to Amazon's own bucket
(`s3.amazonaws.com/echo.api/`), percent-decoded and normalised before the prefix
is compared, so `/echo.api/../evil/cert.pem` does not pass. The chain is
validated and cached for an hour, because `Initiate` has a 100ms p50 budget and
a cache hit has to be one dict lookup and one RSA verify.

:::caution[No replay protection]
The music envelope has no timestamp, so the custom-skill replay window cannot be
implemented. A captured directive can be replayed forever. See
[Gaps and limits](../limits/).
:::

## Assets

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/stream/<song-id>/<expires>/<sig>` | GET | HMAC signature | Transcoded MP3 proxy, 256 kbit/s |
| `/art/<cover-id>/<expires>/<sig>` | GET | HMAC signature | Cover art proxy |
| `/icons/<name>` | GET | none | Skill icons for the manifest |

The signature is `HMAC-SHA256(SIGNING_KEY, "<kind>:<id>:<expires>")` truncated to
32 hex characters, compared in constant time. An expired or altered URL returns
403.

Both proxies forward the `Range` request header upstream, and return
`Content-Type`, `Content-Length`, `Accept-Ranges` and `Content-Range` from the
upstream response. The upstream timeout is 20 seconds.

`/icons` deliberately serves `Cache-Control: no-store` and no cache validators.
Path traversal is refused.

## Account linking

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/oauth/authorize` | GET | client id, redirect allowlist | Renders the linking passphrase form |
| `/oauth/authorize` | POST | linking passphrase | Issues a code and redirects back to Amazon |
| `/oauth/token` | POST | client id and secret | Exchanges a code or refresh token for tokens |

`/oauth/token` supports `authorization_code` and `refresh_token` grants. Client
credentials are accepted as form parameters or as HTTP Basic.

## Operations

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/healthz` | GET | none | Liveness. Answers `{"ok": true}`. |
| `/captures` | GET | `X-Admin-Token` | Every captured directive, as JSON |
| `/diag` | GET | `X-Admin-Token` | Live Subsonic search, with `?q=`, returning up to three songs |

Both admin routes return 401 unless `ADMIN_TOKEN` is set and the header matches.
`/diag` returns 502 with the error detail if the Subsonic call fails, which
makes it the fastest way to distinguish a credentials problem from a routing
problem.

## Out-of-band queues

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/queue` | POST | `X-Admin-Token` | Publish a track list, receive an `ext:<token>` contentId |
| `/queue/<token>` | GET | `X-Admin-Token` | Inspect a published queue. `current` returns the newest |

This is the handoff a queue composer such as Music Assistant uses. A queue it
composed has no Subsonic identity of its own, and Alexa needs a contentId it can
echo back for the life of the queue, so the list is published ahead of playback
and given an opaque id.

The token is an HMAC of the track list under `SIGNING_KEY`, truncated. Hashing
the list means republishing an unchanged queue returns the id Alexa is already
holding, rather than orphaning it. Records are on disk, not in memory, so a
restart mid-song does not end playback.

Tracks that do not resolve on the Subsonic server are dropped from the published
list, and the response reports both the count published and the count requested.

## Human-facing

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | none | Landing page. Exists because the Alexa app opens the endpoint root in a webview. |
| `/privacy` | GET | none | Minimal privacy statement |
| `/terms` | GET | none | Minimal terms of use |

## Setup wizard

Served under `/setup`, and under active development, so treat the sub-paths as
indicative rather than stable.

| Route | Purpose |
|---|---|
| `/setup/status` | The state of ingestion, enablement and last inbound request |
| `/setup/endpoint` | Endpoint checks and the QR proof |
| `/setup/verify/<token>` | The target the QR code points at |
| `/setup/alias` | Alias checker against your own library |
| `/setup/stations` | Station tuning, with a live pool preview |
| `/setup/wizard` | Skill creation, catalogs, ingestion and enablement |

:::caution[`/setup` refuses to serve without `ADMIN_TOKEN`]
Serving it open would hand anyone who found the URL the ability to run `ask`
against your Amazon account. With no token set it answers 503 and says why.

A browser cannot set `X-Admin-Token` on a normal navigation, so the wizard swaps
header auth for a signed session cookie carrying a digest of the same token.
Rotating `ADMIN_TOKEN` invalidates every session. Every page also works without
JavaScript.
:::
