---
title: Catalog and enablement
description: Uploading your library to Amazon so speech resolves, what the ingestion states mean, and the upload that silently unbinds the skill.
sidebar:
  order: 8
---

Alexa resolves what you said against a catalog **you** upload, before it routes
anything to a provider. Without a catalog, spoken names arrive as free text and
the bridge has to guess. With one, they arrive as entity ids that map straight
to your library.

Five catalogs, one per kind:

| Kind | Amazon type | Entity id shape |
|---|---|---|
| Artists | `AMAZON.MusicGroup` | `artist.<subsonic id>` |
| Albums | `AMAZON.MusicAlbum` | `album.<subsonic id>` |
| Tracks | `AMAZON.MusicRecording` | `track.<subsonic id>` |
| Playlists | `AMAZON.MusicPlaylist` | `playlist.<subsonic id>` |
| Genres | `AMAZON.Genre` | `genre.<genre name>` |

Create the five catalogs on your vendor account, then give their ids to the
sync tool as `CATALOG_ARTISTS`, `CATALOG_ALBUMS`, `CATALOG_TRACKS`,
`CATALOG_PLAYLISTS` and `CATALOG_GENRES`. The ids are read from the environment
rather than baked into the image.

## Running the sync

`catalog_sync.py` walks your library, builds the five documents, uploads each
one with the ASK CLI, and then rebinds the skill. It needs `ask` on `PATH` and a
configured profile.

```sh
CATALOG_ARTISTS=amzn1.ask.catalog.... \
CATALOG_ALBUMS=amzn1.ask.catalog.... \
CATALOG_TRACKS=amzn1.ask.catalog.... \
CATALOG_PLAYLISTS=amzn1.ask.catalog.... \
CATALOG_GENRES=amzn1.ask.catalog.... \
SUBSONIC_URL=http://192.168.1.10:4533 \
SUBSONIC_USER=alexa SUBSONIC_PASSWORD='...' \
SKILL_ID=amzn1.ask.skill.... \
CATALOG_STATE=./catalog-state.json \
python catalog_sync.py
```

Point `CATALOG_STATE` somewhere durable. Losing it makes the next run restamp
every entity, which sends Amazon back through the whole catalog.

Run it on a schedule, daily or slower. Amazon suggests staying under fifty
uploads per catalog per day, so anything daily is comfortable.

`--no-cycle`, or `CATALOG_NO_CYCLE=1`, skips the enablement cycle. Only useful
when you intend to cycle by hand.

### Why it diffs instead of rebuilding

Amazon uses `lastUpdatedTime` to decide what changed, and their documentation is
explicit: "If you upload a catalog with changed entries but an unchanged
lastUpdatedTime field, the changes might be ignored."

That leaves two ways to get it wrong. Stamping every entity with "now" makes
Amazon reprocess the whole catalog on every run. Omitting a removed track leaves
it in Amazon's entity resolution forever, so Alexa keeps offering songs that no
longer exist.

The sync tool keeps a content hash per entity in its state file, bumps the
timestamp only for genuine changes, and emits explicit `deleted: true`
tombstones for anything that disappeared. It also refuses to upload a kind that
collected nothing, rather than tombstoning your entire library because the
Subsonic server was briefly unreachable.

## What the ingestion states mean

This is the part that looks broken and is not. Four states that are easy to
confuse:

| State | Meaning |
|---|---|
| `ER_INGESTION: SUCCEEDED` | **The gate.** Entity resolution has your catalog. Voice works. |
| `SLU_MODELING: PENDING` | Normal. Takes weeks. Never blocks playback. |
| Top-level upload `IN_PROGRESS` | Meaningless on its own. It is pinned by `SLU_MODELING`, so it stays in progress long after the catalog is usable. |
| Enablement missing | Silent fallback to your default music provider. |

:::tip[Watch ER_INGESTION and nothing else]
Waiting for the top-level status to reach a terminal state means waiting weeks
for something that has no bearing on whether your music plays. The wizard's
status screen surfaces `ER_INGESTION` specifically because it is the only one
that gates voice.
:::

## The trap: uploading a catalog unbinds the skill

:::danger[Set `SKILL_ID`, or every upload breaks voice playback]
After a catalog upload, playback falls back to your default provider and Alexa
announces it: "Here's ... from Spotify". Meanwhile the bridge answers every
request correctly and `ER_INGESTION` reports `SUCCEEDED`. Nothing looks wrong
anywhere except in what Alexa says. It cost several hours to find once.

The sync tool now cycles enablement itself when something was actually
uploaded, but **only if `SKILL_ID` is set**. Without it the run logs a loud
warning and leaves the skill unbound.
:::

The cycle is delete-then-set on the enablement:

```sh
ask smapi delete-skill-enablement --skill-id <skill-id> --stage development
ask smapi set-skill-enablement    --skill-id <skill-id> --stage development
```

A delete that fails is normal and is not fatal: an enablement that does not
exist is the usual state after an earlier failed run, and the set that follows
is what matters. A **set** that fails leaves the skill disabled, and the sync
says so plainly.

The cycle is a short window with no skill enabled, so it only runs when
something was uploaded. A run that changed nothing pays nothing.

## Next

[First play](../first-play/).
