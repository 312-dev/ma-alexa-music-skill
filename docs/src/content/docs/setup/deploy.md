---
title: Deploy the bridge
description: Building and running Ampere with Docker, the environment variables it reads, and the data it needs on disk.
sidebar:
  order: 5
---

The bridge is a small Flask application. It has two runtime dependencies,
`flask` and `gunicorn`, and it keeps almost nothing on disk.

## Build

There is no published image. Build from the repository:

```sh
git clone https://github.com/GraysonCAdams/ampere.git
cd ampere
docker build -t ampere .
```

The image is `python:3.12-slim` plus the bridge, the setup wizard, the catalog
sync tool and the skill icons. It exposes port 5056.

Three runtime dependencies:

```
flask==3.1.0
gunicorn==23.0.0
cryptography==46.0.3
```

`cryptography` is used only for inbound request verification, and the module
degrades to an "unavailable" reason rather than crashing if it is missing.

## Generate secrets

Three values should be random and long. Generate them once and keep them:

```sh
openssl rand -hex 32   # SIGNING_KEY
openssl rand -hex 32   # OAUTH_CLIENT_SECRET
openssl rand -hex 16   # ADMIN_TOKEN
```

:::caution[SIGNING_KEY must be set explicitly]
If `SIGNING_KEY` is unset the bridge generates a random one at startup. Every
signed `/stream` and `/art` URL, and every OAuth token, is then invalidated the
next time the process restarts. Alexa will fetch a stream URL minted before the
restart and get a 403.
:::

You also need a linking passphrase (`OAUTH_LINK_SECRET`). You type it once in
the Alexa app when you link the account, so make it something you can type on a
phone.

## Run

The repository ships `.env.example` with every variable that matters and a note
on each. Copy it, fill it in, and pass it to the container:

```sh
cp .env.example .env
docker volume create ampere-data

docker run -d --name ampere --restart unless-stopped \
  -p 127.0.0.1:5056:5056 \
  --env-file .env \
  -v ampere-data:/data \
  ampere
```

Binding to `127.0.0.1` is deliberate: the reverse proxy is the only thing that
should reach the port directly.

The same as Compose:

```yaml
services:
  ampere:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:5056:5056"
    env_file: .env
    volumes:
      - ampere-data:/data

volumes:
  ampere-data:
```

With `ADMIN_TOKEN` set, the [setup wizard](../validate/#the-setup-wizard) is
served at `https://your-host/setup` and drives the rest of the process.

## Environment variables

These are the ones you have to set. The
[configuration reference](../../reference/configuration/) lists every variable
the code reads, with defaults.

| Variable | Purpose |
|---|---|
| `PUBLIC_BASE` | The public HTTPS origin Amazon will call, with no trailing slash. Every signed stream and art URL is built from it. |
| `SUBSONIC_URL` | Your Subsonic server's base URL, reachable from the bridge. |
| `SUBSONIC_USER` | Subsonic username. |
| `SUBSONIC_PASSWORD` | Subsonic password. Hashed with a fresh salt per call by the token auth. |
| `SIGNING_KEY` | HMAC key for signed asset URLs and OAuth tokens. |
| `OAUTH_CLIENT_ID` | Client id you will also enter in the skill's account-linking configuration. |
| `OAUTH_CLIENT_SECRET` | Client secret, same. |
| `OAUTH_LINK_SECRET` | Passphrase typed once in the Alexa app to authorise linking. |
| `ADMIN_TOKEN` | Gates `/setup`, `/captures`, `/diag` and the queue API. Without it the wizard refuses to serve at all rather than serving open. |

:::caution[`PUBLIC_BASE` and `SUBSONIC_URL` have no defaults]
Both are empty unless set. A bridge missing them starts, passes its health
check, and works for nothing. A deployment that answers `CONTENT_NOT_FOUND` for
every request is usually a `SUBSONIC_URL` that was never set.
:::

Worth tuning once things work:

| Variable | Default | Effect |
|---|---|---|
| `AFTER_CONTENT` | `stop` | What plays after the requested content runs out: `stop`, `artist`, `genre`, `library`, `radio`. |
| `RADIO_ARTISTS` | `12` | How many similar artists a station draws on. |
| `RADIO_TRACKS_PER_ARTIST` | `12` | Per-artist cap within a station. |
| `STREAM_TTL` | `43200` | Lifetime of a signed stream URL, in seconds. |
| `SUBSONIC_TIMEOUT` | `6` | Per-call Subsonic timeout, in seconds. |
| `VERIFY_REQUESTS` | `warn` | Inbound Amazon request verification: `off`, `warn` or `on`. |
| `SKILL_ID`, `SKILL_STAGE` | unset, `development` | Let the catalog sync rebind the skill after an upload. Set these. |

## What it needs on disk

Everything lives under `/data`:

| Path | Default from | Contents |
|---|---|---|
| `/data/captures` | `CAPTURE_DIR` | Every inbound directive, headers and body, as JSON. This is your primary diagnostic. |
| `/data/queuestate` | `QUEUE_STATE_DIR` | One small JSON file per queue holding shuffle, loop and repeat. |
| `/data` | `SETUP_STATE_DIR` | What the setup wizard has established so far. |
| `/app/icons` | `ICON_DIR` | Skill icons, baked into the image. |

Icons are in the image rather than on the volume deliberately: Amazon refetches
them on every manifest update, and a skill whose icons 404 fails the update with
`RESOURCE_NOT_FOUND`. Point `ICON_DIR` at a mounted directory if you want your
own artwork without rebuilding.

Captures grow without bound. There is no rotation, so prune the directory
occasionally or mount it somewhere you do not mind.

## Verify it is alive

```sh
curl -s http://127.0.0.1:5056/healthz
# {"ok":true}
```

Then check it can actually talk to your library, using the admin token:

```sh
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  'http://127.0.0.1:5056/diag?q=radiohead'
```

That runs a live Subsonic search and returns up to three matching songs. If it
returns `{"subsonic":"error", ...}` you have a credentials or reachability
problem between the bridge and your music server, and nothing further will work
until it is fixed.

Finally, from outside your network, over the public hostname:

```sh
curl -s https://ampere.example.com/healthz
```

## Why one worker and eight threads

The container runs:

```
gunicorn --bind 0.0.0.0:5056 --workers 1 --threads 8 app:app
```

Both numbers are load-bearing.

**One worker**, because the caches are in-process. With two workers there were
two of everything: a queue warmed on one worker was still cold on the other, and
roughly half of all track transitions paid a three-second station build.

**Eight threads**, rather than a single synchronous worker, because `/stream`
proxies audio through the same application. One blocking worker would stall
every directive for the length of a song. The work is entirely I/O bound, so the
GIL costs nothing here.

:::note
The `PORT` variable only applies when running `app.py` directly with Python. The
container's gunicorn command binds 5056 explicitly, so setting `PORT` on the
container has no effect. Remap the port on the host side instead.
:::

## Next

[Validate the endpoint](../validate/), before you create the skill.
