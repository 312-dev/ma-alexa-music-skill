---
title: Configuration
description: Every environment variable the bridge and the catalog sync tool read, with defaults.
---

Everything is configured by environment variable. There is no config file.

## Bridge

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_BASE` | empty | Public HTTPS origin Amazon calls. Trailing slash is stripped. Every signed stream and art URL is built from it. **Set this.** |
| `SIGNING_KEY` | random per process | HMAC key for signed asset URLs, OAuth tokens, external queue tokens and the setup session cookie. If unset, a fresh random key is generated at startup and everything previously issued stops working on restart. **Set this.** |
| `STREAM_TTL` | `43200` | Lifetime of a signed stream URL, in seconds. 12 hours. |
| `ADMIN_TOKEN` | unset | Gates `/setup`, `/captures`, `/diag` and the queue API. Unset means the wizard refuses to serve and the admin routes refuse everything. **Set this.** |
| `CAPTURE_DIR` | `/data/captures` | Where inbound directives are written. Created at startup. |
| `ICON_DIR` | `/app/icons` | Where skill icons are served from. Populated by the image. Override to use your own artwork without rebuilding. |
| `QUEUE_STATE_DIR` | `/data/queuestate` | Per-queue shuffle, loop and repeat state. |
| `SETUP_STATE_DIR` | `/data` | Where the setup wizard records what it has established. |
| `PREWARM` | `1` | Load every artist name at startup in one call. Set to `0` to skip, which is what the test suite does. |
| `PORT` | `5056` | Only used when running `app.py` directly with Python. The container's gunicorn command binds 5056 explicitly and ignores this. |

## Request verification

| Variable | Default | Purpose |
|---|---|---|
| `VERIFY_REQUESTS` | `warn` | `off` logs the outcome only, `warn` logs a warning and serves anyway, `on` rejects with 403. An unrecognized value is treated as `warn`, with a warning. |
| `SIGNATURE_FETCH_TIMEOUT` | `4` | Seconds allowed to fetch Amazon's certificate chain. |

The policy is read per request rather than at import, so changing it needs a
restart of the process but not a reload of the module.

## Playback behavior

| Variable | Default | Purpose |
|---|---|---|
| `AFTER_CONTENT` | `stop` | What plays once the requested content runs out: `stop`, `artist`, `genre`, `library` or `radio`. Anything but `stop` extends the queue past its last track rather than replacing it, seeded from the request itself. An unrecognized value logs a warning and falls back to `stop`. |
| `RADIO_ARTISTS` | `12` | How many similar artists a station draws on. |
| `RADIO_TRACKS_PER_ARTIST` | `12` | Per-artist cap within a station, so a prolific seed artist is not most of their own station. |

`AFTER_CONTENT` never pre-empts what was asked for. Continuation begins strictly
past the last track of the requested content. Stations ignore the setting and
always continue.

## Subsonic

| Variable | Default | Purpose |
|---|---|---|
| `SUBSONIC_URL` | empty | Base URL of your Subsonic-compatible server, reachable from the bridge. Trailing slash is stripped. **Set this.** |
| `SUBSONIC_USER` | empty | Username. |
| `SUBSONIC_PASSWORD` | empty | Password. Used for token auth: `md5(password + salt)` with a fresh salt per call. |
| `SUBSONIC_TIMEOUT` | `6` | Per-call timeout, in seconds. |

The client identifies itself as `ma-alexa-skill` at API version `1.16.1` and
requests JSON.

## Account linking

| Variable | Default | Purpose |
|---|---|---|
| `OAUTH_CLIENT_ID` | `ma-alexa` | Client id, matched against what Amazon sends. |
| `OAUTH_CLIENT_SECRET` | empty | Client secret. Accepted as form parameters or HTTP Basic. |
| `OAUTH_LINK_SECRET` | empty | Passphrase typed once in the Alexa app to authorize linking. Empty means linking always fails. |
| `OAUTH_ACCESS_TTL` | `2592000` | Access token lifetime in seconds, 30 days. Refresh tokens are fixed at one year. |
| `SIGNING_KEY` | random per process | Shared with the bridge. Rotating it invalidates every token and forces a relink. |

Authorization codes are valid for 10 minutes. Redirects are only accepted to
Amazon's own per-vendor link endpoints under `alexa.amazon.com`,
`pitangui.amazon.com`, `layla.amazon.com` and `alexa.amazon.co.jp`.

## Catalog sync

Read by `catalog_sync.py`, which is a separate command and is not included in
the container image.

| Variable | Default | Purpose |
|---|---|---|
| `CATALOG_ARTISTS` | empty | Catalog id for `AMAZON.MusicGroup`. |
| `CATALOG_ALBUMS` | empty | Catalog id for `AMAZON.MusicAlbum`. |
| `CATALOG_TRACKS` | empty | Catalog id for `AMAZON.MusicRecording`. |
| `CATALOG_PLAYLISTS` | empty | Catalog id for `AMAZON.MusicPlaylist`. |
| `CATALOG_GENRES` | empty | Catalog id for `AMAZON.Genre`. |
| `CATALOG_STATE` | `/data/catalog-state.json` | Per-entity content hashes and timestamps. Losing this file makes the next run restamp everything. |
| `CATALOG_OUT` | `/tmp/catalog` | Where the generated JSON documents are written before upload. |
| `SKILL_ID` | unset | Needed to rebind the skill after an upload. Without it an upload silently unbinds the skill. |
| `SKILL_STAGE` | `development` | Stage passed to the enablement calls. |
| `CATALOG_NO_CYCLE` | unset | `1` skips the enablement cycle. Same as passing `--no-cycle`. |

It also reads the Subsonic variables above. If any catalog id is missing it
exits with status 2 without uploading anything.

Uploads go through the Skill Management REST API, authorized by the Login with
Amazon credentials the wizard stored. The older CLI form still works if you
have it:

```sh
ask smapi upload-catalog -c <catalog-id> -f <file>
```

`ASK_CONFIG` points at an optional ASK CLI configuration, defaulting to
`~/.ask/cli_config`.

## Out-of-band queues

Read by `queue_api.py`, which serves the `/queue` endpoints used by a queue
composer such as Music Assistant.

| Variable | Default | Purpose |
|---|---|---|
| `EXTERNAL_QUEUE_TTL` | `604800` | How long a published queue stays resolvable, in seconds. Seven days. |
| `EXTERNAL_QUEUE_MAX` | `64` | How many published queues are kept. |
| `MA_HANDOFF_PHRASE` | `music assistant` | Comma-separated phrases recognized as the handoff. |

## Defaults worth overriding immediately

:::caution
`PUBLIC_BASE` and `SUBSONIC_URL` both default to the original author's
deployment, and `SIGNING_KEY` defaults to a value that changes on every restart.
A bridge left on those three defaults will start, pass its health check, and
work for nothing.
:::
