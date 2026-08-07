# Provider-agnostic Ampere: playback, catalog, and radio beyond Subsonic

Goal: let Ampere play, catalog (for voice search), and radio from **any** Music
Assistant library provider (Plex, Jellyfin, Emby, filesystem, iBroadcast, ...) and
any streamable provider, instead of only OpenSubsonic. The lever is to source from
MA's unified library / streaming / radio rather than calling the Subsonic API
directly.

The work splits into three layers that are coupled to Subsonic to very different
degrees. They should ship in order; each is useful on its own.

---

## What we already verified (grounding, from live code)

- **Transport is already generic.** `stream_route` serves `/ampere_stream/<ref>.mp3`
  "from any MA music provider" by decoding the ref to an MA uri and calling
  `mass.get_provider(provider_id).get_stream_details(item_id, media_type)` per
  request. `encode_ref` is just `base64(provider://type/id)`.
- **Heterogeneous queues already work.** The published queue is a list of per-track
  records, each with its own `ref`; resolution is per-track at fetch time. The queue
  token is explicitly built to "hash the same whichever kinds it mixes."
- **The only hard Subsonic gate in playback is one function**: `_subsonic_id(item)`
  (provider.py), which returns None for non-Subsonic tracks; the publish path then
  drops them ("dropped rather than silently substituted").
- **`library_items(summary=True, provider=...)` is a fast DB read** of MA's synced
  library (`library.db` already holds the whole Navidrome library flat: 9,194 tracks
  / 6,426 albums / 2,975 artists). No per-item network, no enrichment.
- **MA has a provider-agnostic radio primitive**: `mass.music.tracks.similar_tracks()`
  tries the item's own provider, then falls back cross-provider and to
  metadata/plugin recommenders.

---

## Layer 1 - Transport (playback): ALREADY DONE

**Correction (2026-08-06, from reading the code):** this layer is already built and
enabled by default. The earlier premise ("`publish_tracks` drops any queue item
without a Subsonic id") was wrong.

- `publish_tracks` (provider.py:2463) already falls back to `_ma_track(item)`
  (provider.py:2475) for any item with no Subsonic id, building a `ref` record from
  MA metadata + `encode_ref(item.uri)`.
- Gated by `CONF_MA_SOURCE`, which **defaults `True`** (settings.py:226). An item is
  only skipped when the fallback itself returns None (no uri) or the operator turned
  the toggle off.
- `stream_route` (stream_route.py:130-141) resolves the ref via
  `parse_uri` -> `mass.get_provider(provider_id)` -> `get_stream_details`, with no
  Subsonic assumption. Mixed-provider queues already work; the queue token already
  hashes across kinds.

So multi-provider **playback** needs no code. The one thing left is empirical:

**Remaining (verification only):**
- Streaming smoke test end to end through the **public** endpoint for a couple of
  non-Subsonic providers present in this MA (Spotify, Tidal), confirming Alexa plays
  the `ref` audio. MA reports realtime audio (no length, no ranges); the point of the
  test is what Alexa does with that (stream_route.py already logs Range requests).
- Decide the stance on proxying streaming-service audio to Amazon's fetchers
  (licensing / public exposure). Fine personally; flag for any distribution.

---

## Layer 2 - Catalog enumeration: MODERATE

**Current:** `catalog_sync.collect()` walks Subsonic directly (getArtists,
getAlbumList2, per-album track fetch, playlists, genres). Entity ids are Subsonic
ids.

**Target:** enumerate from MA's synced library via
`mass.music.<type>.library_items(summary=True, provider=<selected>)`, paged. Entity
ids become a stable MA reference.

**Approach:**
- Swap the Subsonic walk for `library_items` (or `iter_library_items`) per kind.
- Choose a catalog entity-id scheme that encodes the MA uri so resolution (Layer 2b)
  can map back, and stays stable across syncs so incremental add/remove diffing keeps
  working.
- Config: a "which library provider(s) to catalog" selector (this is also the setup
  UX win - a dropdown of existing MA music providers instead of retyping server
  creds). Default preserves today's behaviour (the one OpenSubsonic instance).

**Resolved by code reading (2026-08-06):**
- `library_items(favorite, search, limit=500, offset, order_by, provider, genre, *,
  summary=True)` at base.py:447 - paged, provider-filterable; summary rows carry
  `provider_mappings` and the library `item_id`. `order_by` + `favorite` cover the
  `rnd:all`/`star` cases in Layer 2b.
- **Execution-context constraint (load-bearing):** `collect()` runs in a worker
  thread (tasks.py:94 `loop.run_in_executor`), so it cannot `await` MA controllers.
  Bridge with `asyncio.run_coroutine_threadsafe(coro, loop).result()` - the reverse
  of the `loop.call_soon_threadsafe` the file already uses for progress (tasks.py:91).
  The loop must be passed into `collect()` (or `run_upload`) from `_upload`.
- **Entity-id scheme:** `<kind>.<library_db_id>` (a plain integer). Layer 2b resolves
  it with `mass.music.<type>.get_library_item(db_id)`.

**Discovery still needed:**
- **Amazon catalog size limits.** A whole multi-provider library (especially with the
  tracks catalog) may exceed per-catalog caps. Decide which kinds to upload and
  whether to cap/drop tracks.
- Freshness: catalog is as-of-last-MA-sync. Document it; decide rebuild cadence vs MA
  sync cadence.

---

## Layer 2b - Voice catalog resolution: DONE (2026-08-06)

Built in `ma_resolve.py`, wired into `core.resolve_tracks`.

**How it turned out to be smaller than feared:** `core.build_item` already
serves two record shapes - a Subsonic id and an MA `ma_ref` (the Layer 1
transport). So resolution did not need a new publish path; it only needed
`resolve_tracks` to *emit* `ma_ref`-bearing records when the content id points at
MA. Everything downstream (streaming, controls, art) already handled them.

**Source disambiguation - the `ma-` marker:** a catalog built from MA emits entity
ids like `artist.ma-1234` (see `stream_ref.MA_ID_PREFIX`). The existing
entity->content mapping carries the native part through unchanged, so it becomes
`ar:ma-1234`, and `resolve_tracks` reads the marker off the id and routes to
`ma_resolve` instead of Subsonic. A single content id is self-describing; no
external state decides its source.

**Per kind** (`ma_resolve.resolve`): `ar` -> `artists.tracks(id,"library")`,
`al` -> `albums.tracks(id,"library")`, `tr` -> `tracks.get_library_item(id)`,
`pl` -> `playlists.tracks(id,"library")` (async generator, drained), `gen` ->
`tracks.library_items(genre=[id])`.

**Whole-collection intents** (`star`, `rnd:all`) carry no id to mark, so their
source is decided by `core._ma_mode()`: MA running + catalog providers selected.
`star` -> `library_items(favorite=True)`, `rnd:all` -> a bounded, `sort_name`
(stable, not random) slice, shuffled per-queue by `queuestate`.

**Execution:** `resolve_tracks` runs in the bridge's worker thread (webserver
thread pool), so each MA call is bounced onto the loop with
`run_coroutine_threadsafe`; the per-content-id cache means once per queue.

**Interim contract honoured:** the MA catalog emits **no station entities**, and a
`rad:` with an MA seed degrades to the seed artist's own tracks rather than
calling Subsonic similar-artists with an id it does not own. After-content on an
MA seed degrades to library shuffle naturally (MA song records carry no
`artistId`/`genre`, so `continuation_content` falls back to `rnd:all`).

---

## Layer 3 - Radio / after-content / suggestions: LARGEST (but MA has the engine)

**Current:** `core.radio_pool` builds a station from `subsonic.similar_artists(seed)`
+ `subsonic.album_tracks`; the after-content modes (artist / genre / library / radio)
all go through the `subsonic` module. The seed must be a Subsonic id.

**Target:** seed from the currently-playing MA item; generate suggestions with MA's
provider-agnostic radio; feed results as `ref` records (Layer 1).

**Approach per mode:**
- **radio:** `mass.music.tracks.similar_tracks(seed_item, limit=...)` replaces
  similar-artists-then-tracks. Reconcile the `radio_artists` / `radio_tracks_per_artist`
  tuning against track-level similarity.
- **artist:** MA artist tracks / similar artists.
- **genre:** MA library filtered by genre.
- **library:** `library_items` random.
- **favorites:** `library_items(favorite=True)`.

**Discovery needed:**
- **Seed extraction.** How Ampere derives the seed at content-end today, and how to
  get the current MA item's uri instead of a Subsonic id (poll/now-playing ->
  queue item -> `media_item.uri`).
- **Similar-tracks quality per provider.** Navidrome keeps its own getSimilarSongs
  (unchanged). Plex/local have no native similar, so MA falls back to
  cross-provider / metadata-plugin recommenders - which requires such a plugin
  (e.g. last.fm) to be installed and returns variable quality for local-only
  libraries. Discover what SIMILAR_TRACKS-capable providers/plugins are present and
  how good the results are.
- **Stations catalog.** Keep the `rad:<id>` entity but resolve it via MA similar
  rather than a Subsonic station; decide how a station is defined for a non-Subsonic
  seed.
- Confirm the growing-token / append flow accepts MA-radio tracks (it will, via the
  `ma`/ref append path).

**Interim contract (important):** until Layer 3 is rebuilt, **gate radio /
after-content to Subsonic seeds only** - do not let it silently no-op when the seed
is a Plex/Spotify/local track. A half-migrated radio that goes quiet is worse than
one that honestly only runs where it can.

---

## Phasing

1. **Layer 1 (playback).** Self-contained, immediately useful, ship first.
2. **Layers 2 + 2b (catalog + resolution).** Together - the entity-id scheme couples
   them.
3. **Layer 3 (radio).** Last and largest; interim is Subsonic-gated radio.

## Cross-cutting decisions to make up front

- **Whole MA library vs per-provider.** Recommend config-selectable, default to the
  provider(s) the user picks, so today's behaviour is preserved.
- **Amazon catalog size limits** drive which kinds get catalogued.
- **Freshness** (as-of-last-sync) is acceptable but must be documented.
- **Streaming-service licensing / public exposure** for non-owned providers.

## Risks

- Radio quality regression for local-only libraries with no native similar.
  Mitigation: keep Subsonic similar for Subsonic seeds; use MA cross-provider only
  where it adds signal.
- Catalog bloat against Amazon limits when cataloguing the whole library + tracks.
- Per-provider streaming reliability (Spotify/librespot the main asterisk).
