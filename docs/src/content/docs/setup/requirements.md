---
title: Requirements
description: What you need in place before deploying the Music Assistant bridge.
sidebar:
  order: 2
---

Four things. None of them are optional.

## A Subsonic-compatible music server

Music Assistant speaks plain Subsonic API 1.16.1 with no OpenSubsonic extensions. Token
auth sets the compatibility floor at API version 1.13.

It should therefore work with Navidrome, Airsonic, Airsonic-Advanced, Gonic,
LMS, Ampache, Funkwhale, Nextcloud Music and Astiga. It has been tested against
Navidrome; the others are untested rather than known-good.

The server does **not** need to be reachable from the internet. The bridge
proxies audio and cover art on its behalf behind signed, expiring URLs, which is
[a deliberate design choice](../../how-it-works/architecture/) and not a
workaround.

You will need a username and password for it. The bridge uses Subsonic token
auth, so the password is hashed with a fresh salt per call rather than sent in
the clear, but the bridge itself holds the password in an environment variable.

### Endpoints the bridge relies on

| Subsonic view | Used for |
|---|---|
| `search3` | Free-text resolution when Alexa sends no catalog entity |
| `getArtist`, `getAlbum`, `getSong` | Track resolution and display names |
| `getArtists` | Artist name prewarm at startup, and catalog collection |
| `getAlbumList2` | Catalog collection |
| `getPlaylists`, `getPlaylist` | Playlists by spoken name |
| `getGenres`, `getSongsByGenre` | Genre queues |
| `getStarred2`, `star`, `unstar` | Starred content and thumbs feedback |
| `getRandomSongs` | The library-at-random queue |
| `getArtistInfo2` | Similar artists, which is what stations are built from |
| `stream`, `getCoverArt` | The proxied audio and art |

`getTopSongs` is deliberately not used. It cost about 0.9s and came back empty
against the test library.

## A publicly reachable HTTPS endpoint

Amazon calls your bridge over the public internet. It needs a real hostname with
a valid certificate, reachable from outside your network, answering on 443.

This is the step people get wrong, and it has [its own page](../ingress/). The
short version: use your own reverse proxy if you can, and a tunnel only if you
are behind CGNAT or cannot open ports.

## An Amazon developer account

Free, at the Alexa developer console.

You do **not** need the ASK CLI, Node, or a terminal. The bridge talks to
Amazon's Skill Management API directly, authorized through Login with Amazon in
your browser.

You will register **your own** Login with Amazon security profile during setup,
which takes about a minute in the developer console. There is no shared
application and nothing is hosted by this project, so your credentials and
tokens stay on your own machine. The wizard walks you through it and shows the
exact return URL to paste.

This is one of the reasons
the bridge's setup wizard is a web page rather than a terminal prompt.

:::note
No AWS account is needed. Amazon's music-skill documentation only shows a Lambda
ARN as an endpoint, but SMAPI accepts an HTTPS endpoint. See
[Findings](../../how-it-works/findings/).
:::

## Docker, or a Python host

The repository ships a `Dockerfile` based on `python:3.12-slim`. The runtime
dependencies are small:

```
flask==3.1.0
gunicorn==23.0.0
cryptography==46.0.3
```

`cryptography` is there only for inbound request verification, and its absence
degrades to an "unavailable" reason rather than a crash.

If you would rather not use Docker, any host with Python 3.12 and those
packages will run it. The container runs a single gunicorn worker with eight
threads, which is
[not an arbitrary choice](../deploy/#why-one-worker-and-eight-threads).

You also need a writable data directory. By default the bridge wants `/data`,
with `captures` and `queuestate` beneath it.

## Next

[Choosing an alias](../alias/). Do this before you create the skill, because
changing it later means changing the skill's invocation name and waiting for
Amazon to re-model it.
