# Layer 3: provider-agnostic radio

## Status: BUILT + DEPLOYED (build 5c658ff41abc), validated to the audio boundary

Parts 1-4 shipped. Validation on the box (read-only, no playback):

- **Seeds exist**: the MA library is 9194 tracks / 2975 artists, all mapped to
  one provider, `opensubsonic--etezT5sV` (plus 8 builtin playlists). So
  `mass.music.artists.tracks()` has tracks to seed from.
- **The similarity backend is real and native**: the deployed OpenSubsonic
  provider declares `ProviderFeature.SIMILAR_TRACKS` and implements
  `get_similar_tracks` (sonic_provider.py) by calling Navidrome's
  `getSimilarSongs2`. So `similar_tracks(seed, "library")` returns real similars
  with no dependence on an external last.fm/MusicBrainz provider - the quality
  risk the plan flagged does not apply to this deployment. When Navidrome finds
  nothing it returns `[]`, which is exactly the degraded case `ma_resolve.radio`
  floors to the artist and refuses to cache.
- **Wiring**: 807 tests pass, including the new Layer 3 coverage.

What is not machine-validated: the final voice command producing audio on a real
Echo, which the no-sound constraint precludes here. That is the one remaining
check, and it is the user's to make.

### Follow-up shipped: opt-in stations catalog (build c4bff0fc2f77)

The stations catalog is now creatable in-app rather than env-only:

- A **"Answer 'play <artist> radio'"** toggle (settings, Voice & playback,
  default off) flows through `core.configure(enable_stations=...)` into
  setup-state, the same way `after_content` and `catalog_providers` do.
- `create_catalogs` makes a sixth "Ampere stations" catalog when the toggle is
  on, reusing by title and associating idempotently like the other five.
- `catalog_ids` surfaces the stations id only once it exists; `catalogs_ready`
  makes the one-button setup re-create it if it was just enabled, so turning it
  on and hitting Sync actually provisions the catalog rather than skipping on
  "catalogs already there".
- `run_upload` drives `want_stations` off whether the catalog exists, so the
  crawl and the upload agree.
- Station entities carry two names, `"<artist> Radio"` and `"<artist> Station"`,
  so either spoken word resolves. The suffix is kept on every alias so it never
  collides with the bare artist entity.

So voice artist-radio is now a toggle-and-sync away, not an env edit. Amazon's
per-catalog limit is 500,000 entities; the ~2,975 station entities are ~0.6% of
it, so size is a non-issue at this library's scale.

### Operational note found during validation

The deployment has 5 catalogs (artists/albums/tracks/playlists/genres) and **no
stations catalog** - `stations` is in `OPTIONAL_KINDS` and its id comes only from
the `CATALOG_STATIONS` env var; `create_catalogs` does not make it. So:

- **After-content radio works now**, no catalog change: with "When the queue
  ends" set to radio (or artist), a queue ending on an MA track seeds
  `rad:ma-<artistId>` and plays a real similar-tracks station. This is live.
- **Voice "play <artist> radio"** resolves through the catalog, so it needs a
  stations catalog to exist. Until one is created, the spoken form falls back to
  the plain artist. Emitting the entities (Part 3) is done and gated correctly;
  the missing piece is an in-app way to create the stations catalog, which today
  is env-only. That is a small follow-up (a 6th, opt-in catalog in the create
  flow) and deliberately out of the original plan's scope.

---


Goal: make artist radio and the radio/artist after-content modes work when the
seed is a Music Assistant item from any provider, not only a Subsonic id. Keep
Subsonic's own similar-artists path for Subsonic seeds, where it is the stronger
signal; use MA's cross-provider engine only where Subsonic cannot answer.

This is the last piece of the provider-agnostic effort. Layers 1/2/2b (play,
catalog, voice-resolve) are shipped; radio is the one flow still gated to
Subsonic seeds.

---

## What already exists (so this is smaller than it looks)

The station-entity plumbing is built and working for the Subsonic path. Reading
the code rather than the original plan:

- `catalog_sync` already has a `stations` kind: type `AMAZON.MusicPlaylist`
  (there is no native Alexa station type; a station is a dynamic playlist), id
  `station.<artist_id>`, display name `"<artist> Radio"`, gated on the optional
  `CATALOG_STATIONS` catalog (`OPTIONAL_KINDS`).
- `core.handle_get_playable_content` maps a resolved `station.<id>` entity
  straight to `rad:<id>` (core.py ~1411), and `station_request` catches the
  spoken `"<artist> radio"` form when the catalog did not resolve it.
- `core.station_content(artist_id, spoken)` builds the `rad:<artist_id>`
  content id, warms the pool, and answers with the cover from
  `describe_content("ar", artist_id)` - which already handles `ma-` ids.
- `continuation_content` already routes `radio`/`artist` after-content through
  `rad:<artist_id>` / `ar:<artist_id>`.

So `rad:` is **artist-seeded** everywhere, and the entity/answer path is
provider-neutral. Three things are missing for the MA case, all in two files.

## The three gaps

1. **MA songs carry no `artistId`.** `ma_resolve.song_from_track` sets id,
   ma_ref, title, artist name, album, art - but not `artistId`. So when an MA
   track is the last thing played, `continuation_content` finds no artist seed
   and falls back to `rnd:all`: radio/artist after-content silently degrades to
   a library shuffle for every MA track today.

2. **The `rad` MA branch is a placeholder.** In `resolve_tracks`, a marked
   `rad:ma-<id>` currently returns the seed artist's own tracks (core.py ~598),
   with a comment saying real similar-tracks is Layer 3. It plays something
   coherent but it is not a station.

3. **`catalog_sync.collect_from_ma` deliberately emits no stations**
   (catalog_sync.py ~322): "emitting station entities now would let
   'play <artist> radio' resolve to a station id that plays nothing." True until
   gap 2 is closed; the first thing to undo once it is.

## The seam fact that shapes the design

`mass.music.tracks.similar_tracks(item_id, provider, limit, allow_lookup=False)`
is **track-seeded**, not artist-seeded. It walks the seed track's provider
mappings, calls each provider's `get_similar_tracks` where the provider has the
`SIMILAR_TRACKS` feature, and with `allow_lookup=True` falls back to a
cross-provider metadata lookup (last.fm / MusicBrainz) when the owning provider
has no native similar. That cross-provider fallback is the whole point: it is
what lets a local or OpenSubsonic track get a real station.

Our model is artist-seeded (`rad:<artist_id>`). So MA radio has to bridge
artist -> seed track: resolve the artist's tracks, pick a representative one (or
a few), and seed `similar_tracks` from it. The pick must be deterministic (a
seeded RNG, like `artist_sample`) so the same `rad:` id resolves to the same
pool every GetNextItem and stays cacheable.

---

## Plan

### Part 1 - `artistId` on MA songs (small, unblocks after-content)

In `ma_resolve.song_from_track`, set `song["artistId"] = "ma-<artist_library_db_id>"`
from `track.artists[0]`. Getting the library artist id is the same resolution
subtlety `streamable_uri` already deals with: a library track's `artists[0]`
may be an ItemMapping whose `item_id` is the library id when provider is
`library`, otherwise resolve via `get_library_item_by_prov_id`. Mirror the
existing helper's shape; guard for artistless tracks (leave `artistId` unset,
same as Navidrome imports without one).

Effect on its own: radio/artist after-content from an MA track seeds correctly
instead of shuffling the library. Independent of Parts 2-3, ship first.

### Part 2 - real similar-tracks radio (the core)

Add `ma_resolve.radio(ident, mass, loop, *, limit)` mirroring `core.radio_pool`:

- Resolve the seed artist's library tracks (reuse `resolve("ar", ident, ...)`).
- Pick N deterministic seed tracks (seeded RNG on `ident`), N small (1-3).
- For each seed, `similar_tracks(seed_track_id, provider, limit, allow_lookup=True)`
  bridged through the worker-thread -> loop the same way `resolve` runs its
  coroutines (`asyncio.run_coroutine_threadsafe(coro, loop).result()`).
- Map results with `song_from_track(track, mass)`; merge, dedupe, cap to
  `effective_radio_*`.

In `core.resolve_tracks`, replace the `rad` + `ma` placeholder with this call.
Keep the refuse-to-cache-degraded rule the Subsonic path already has: a station
that came back as the seed artist alone (no cross-provider similar found) must
not be cached, or the degraded pool pins permanently (the Foreigner bug in the
existing comment). If MA returns nothing usable, fall back to the seed artist's
tracks as a floor (today's behaviour) but do not cache it.

Leave the Subsonic branch exactly as is: Subsonic seeds keep
`similar_artist_ids` + `radio_pool`.

### Part 3 - emit `station.ma-<id>` entities (remove the skip)

Undo the deliberate omission in `collect_from_ma`: emit a station entity per
catalogued artist, `station.ma-<artist_db_id>` named `"<artist> Radio"`, gated
on `want_stations` (the existing `CATALOG_STATIONS` switch), exactly like the
Subsonic collect path. Once Part 2 makes those ids resolve to a real station,
"play <artist> radio" resolves against the catalog for MA artists too.

Order matters: Part 2 before Part 3, or a shipped catalog would advertise
stations that still play the placeholder.

### Part 4 - verify, don't build

`station_content`, `describe_content("ar", ma-id)`, cover art, and the
after-content warm path already handle `ma-` ids. Confirm with a live
`rad:ma-<id>` that the name and cover render; no new code expected here.

---

## Discovery needed before Part 2 lands

- **Seed-track selection.** Which of the artist's tracks seeds the station?
  First library track is simplest and deterministic; a few seeds blended gives a
  wider station. Decide N and the pick, keep it RNG-stable on the id.
- **Do the user's providers support SIMILAR_TRACKS, or is `allow_lookup` doing
  all the work?** OpenSubsonic/Navidrome likely have no native similar, so MA
  radio quality depends on last.fm/MusicBrainz metadata providers being enabled
  in MA. Verify they are; if not, MA radio is only as good as the floor. This is
  the main quality risk and worth checking before shipping Part 3.
- **Amazon catalog size.** A station per artist adds ~one MusicPlaylist entity
  per artist to the (optional) stations catalog. On a large library that is real
  volume against Amazon's per-catalog limits; it is already gated behind
  `CATALOG_STATIONS`, so document that stations are opt-in and why.

## Tests

- `song_from_track` sets a marked `artistId` (Part 1); artistless track leaves
  it unset.
- `continuation_content` from an MA seed song yields `rad:ma-<id>` /
  `ar:ma-<id>` rather than `rnd:all` (Part 1).
- `ma_resolve.radio` seeds `similar_tracks` from the artist's tracks and maps
  results; degraded result is not cached (Part 2) - mirror the existing
  `similar_artist_ids` cache-refusal test.
- `collect_from_ma` emits `station.ma-<id>` when `CATALOG_STATIONS` is set and
  none when it is not (Part 3).
  MA-runtime tests skip without `music_assistant`, as elsewhere.

## Risks

- **Radio quality regression** for local-only libraries with no native similar
  and no metadata providers: the station falls to the seed-artist floor. Not
  worse than today (today it does not run at all for MA seeds), but do not
  advertise stations (`CATALOG_STATIONS`) until similar is known to work.
- **Catalog bloat** against Amazon limits when stations are enabled on a large
  library. Mitigation: opt-in switch, documented.
- **Latency**: `similar_tracks` with `allow_lookup` can hit an external metadata
  provider. The pool is already warmed off the request path (`_WARM_POOL`), so
  Initiate does not pay it, but a cold GetNextItem might. Cache the resolved
  pool as the rest of `resolve_tracks` does.

## Phasing

1. **Part 1** - `artistId` on MA songs. Small, fixes after-content today,
   independent. Ship first.
2. **Part 2** - real similar-tracks radio, Subsonic path untouched. The core.
3. **Part 3** - emit station entities (needs Part 2 live first).
4. **Part 4** - verify the name/cover/warm path; expected no-op.

## Hard constraint

Subsonic radio for Subsonic seeds stays exactly as it is; this only adds a path
for MA seeds. No regression to the working Navidrome station flow.
