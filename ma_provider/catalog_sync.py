"""Keep Alexa's catalogs in step with Navidrome.

Amazon uses `lastUpdatedTime` to decide what changed:

    "If you upload a catalog with changed entries but an unchanged
    lastUpdatedTime field, the changes might be ignored."

So a naive rebuild has two failure modes. Stamping every entity with "now"
makes Amazon reprocess the whole catalog on each run, and simply omitting a
removed track leaves it in Amazon's entity resolution forever - Alexa keeps
offering songs that no longer exist. This tracks a content hash per entity and
only bumps the timestamp for genuine changes, emitting explicit
`deleted: true` tombstones for anything that disappeared.

A run that uploaded anything then cycles skill enablement, because uploading a
catalog silently unbinds the skill. See `cycle_enablement` for what that looks
like from the outside; the short version is that nothing reports it and voice
playback quietly becomes someone else's.

Run it on a schedule. Amazon suggests staying under fifty uploads per catalog
per day, so anything daily or slower is comfortable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

from . import handoff
from . import subsonic
from .stream_ref import MA_ID_PREFIX

STATE_PATH = pathlib.Path(os.environ.get("CATALOG_STATE", "/data/catalog-state.json"))
OUT_DIR = pathlib.Path(os.environ.get("CATALOG_OUT", "/tmp/catalog"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
LOCALES = [{"country": "US", "language": "en"}]
POPULARITY = {"default": 100, "overrides": [{"locale": LOCALES[0], "value": 100}]}

# catalogId per kind. Set via env so the ids aren't baked into the image.
CATALOGS = {
    "artists": os.environ.get("CATALOG_ARTISTS", ""),
    "albums": os.environ.get("CATALOG_ALBUMS", ""),
    "tracks": os.environ.get("CATALOG_TRACKS", ""),
    "playlists": os.environ.get("CATALOG_PLAYLISTS", ""),
    "genres": os.environ.get("CATALOG_GENRES", ""),
    # Stations are the second half of "play [artist] radio". Alexa strips the
    # word "radio" while resolving a bare artist, so the only way the phrase
    # survives to the skill as a station is for Alexa to match the *whole*
    # phrase against a catalog entity literally named "<artist> Radio". That is
    # what these are. handle_get_playable_content routes a resolved
    # station.<id> straight to rad:<id> (see core.py). OPTIONAL: unset by
    # default so an operator who has not created the extra Amazon catalog is
    # not forced into it, and the core five keep syncing without it.
    "stations": os.environ.get("CATALOG_STATIONS", ""),
}

# Kinds that may be left unconfigured without failing the whole run.
OPTIONAL_KINDS = {"stations"}

ASK_TIMEOUT = 120

TYPES = {
    "artists": "AMAZON.MusicGroup",
    "albums": "AMAZON.MusicAlbum",
    "tracks": "AMAZON.MusicRecording",
    "playlists": "AMAZON.MusicPlaylist",
    "genres": "AMAZON.Genre",
    # There is no native "station" entity type in the Alexa music catalog
    # schema. A station is a dynamic playlist, and MusicPlaylist is the closest
    # type; the "<artist> Radio" name is what Alexa actually matches the spoken
    # phrase against, and the station.<id> id is what tells the skill it is a
    # station rather than a real playlist.
    "stations": "AMAZON.MusicPlaylist",
}


def log(msg: str) -> None:
    print(f"[catalog-sync] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def fingerprint(entity: dict) -> str:
    """Hash the parts that matter, ignoring the timestamp itself."""
    body = {k: v for k, v in entity.items() if k != "lastUpdatedTime"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def base(entity_id: str, name: str) -> dict:
    return {
        "id": entity_id,
        "names": [{"language": "en", "value": (name or "Unknown")[:512]}],
        "popularity": POPULARITY,
        "locales": LOCALES,
    }


def station_entity(entity_id: str, artist_name: str) -> dict:
    """A station entity Alexa resolves from "<artist> radio" or "<artist>
    station".

    Two alternative names on one entity, so either spoken word matches. The
    suffix is load-bearing: a bare-artist name would collide with the artist
    entity in the artists catalog and Alexa would not know which was meant, so
    every alias keeps a distinguishing word. station_request strips it back off
    before the name is rendered, so the display never doubles it.
    """
    entity = base(entity_id, f"{artist_name} Radio")
    entity["names"] = [
        {"language": "en", "value": f"{artist_name} {word}"[:512]}
        for word in ("Radio", "Station")
    ]
    return entity


def handoff_entity() -> dict:
    """The catalog entry that makes the Music Assistant handoff sayable.

    Amazon resolves an utterance against the catalog before it routes anywhere,
    and does not forward one it cannot resolve: a phrase absent from the
    catalog produces no request at all, rather than a search carrying the words
    (measured 2026-08-02, see handoff.HANDOFF_ENTITY_ID). So the phrase has
    to be an entity, even though the bridge answers it without looking anything
    up.

    Every configured phrase becomes an alternative name on one entity, so
    moving off a phrase that collides with the library is still a config
    change and not a second catalog identity.
    """
    entity = base(f"playlist.{handoff.HANDOFF_ENTITY_ID}", "Music Assistant")
    entity["names"] = [
        {"language": "en", "value": phrase[:512]}
        for phrase in handoff.HANDOFF_PHRASES
    ] or entity["names"]
    return entity


# --- collection -------------------------------------------------------------


def collect(progress=None, *, want_stations: bool | None = None) -> dict[str, list[dict]]:
    """Read the whole library. `progress` gets a short human line at each
    stage; the album-by-album track crawl is minutes long on a real
    collection, and a silent minutes-long job reads as a hung one.

    `want_stations` decides whether a station-per-artist is emitted; None falls
    back to the env-configured catalog, so a caller that knows the setup-state
    (run_upload) drives it from there while the standalone path keeps the env.
    """
    tell = progress or (lambda text, fraction=None: None)
    out: dict[str, list[dict]] = {k: [] for k in CATALOGS}

    tell("reading artists", 0.02)
    # A station per artist, named "<artist> Radio"/"<artist> Station", keyed
    # station.<id> off the same Navidrome artist id the rad: resolver
    # understands. Emitted only when the stations catalog exists; the upload
    # loop skips an empty list, so an operator without it pays nothing.
    if want_stations is None:
        want_stations = bool(CATALOGS.get("stations"))
    artist_name: dict[str, str] = {}
    for index in subsonic.call("getArtists.view").get("artists", {}).get("index", []):
        for artist in index.get("artist", []):
            name = artist.get("name", "")
            artist_name[artist["id"]] = name
            out["artists"].append(base(f"artist.{artist['id']}", name))
            if want_stations and name:
                out["stations"].append(
                    station_entity(f"station.{artist['id']}", name))
    log(f"artists: {len(out['artists'])}")
    if want_stations:
        log(f"stations: {len(out['stations'])}")

    tell(f"reading albums ({len(out['artists'])} artists found)", 0.05)
    albums, offset = [], 0
    while True:
        page = (
            subsonic.call("getAlbumList2.view", type="alphabeticalByName",
                          size=500, offset=offset)
            .get("albumList2", {}).get("album", [])
        )
        if not page:
            break
        albums.extend(page)
        offset += len(page)
        if len(page) < 500:
            break

    for album in albums:
        entity = base(f"album.{album['id']}", album.get("name"))
        if aid := album.get("artistId"):
            entity["artists"] = [{
                "id": f"artist.{aid}",
                "names": [{"language": "en",
                           "value": artist_name.get(aid, album.get("artist", "")) or "Unknown"}],
            }]
        out["albums"].append(entity)
    log(f"albums: {len(out['albums'])}")

    # Album track listings in parallel; this is the slow part by far.
    def tracks_of(album):
        try:
            return album, subsonic.album_tracks(album["id"])
        except Exception as exc:
            log(f"  skip album {album['id']}: {exc}")
            return album, []

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for album, songs in pool.map(tracks_of, albums):
            done += 1
            tell(f"reading tracks: album {done} of {len(albums)}, "
                 f"{len(out['tracks'])} tracks so far",
                 0.05 + 0.9 * done / max(1, len(albums)))
            for song in songs:
                entity = base(f"track.{song['id']}", song.get("title"))
                aid = song.get("artistId") or album.get("artistId")
                if aid:
                    entity["artists"] = [{
                        "id": f"artist.{aid}",
                        "names": [{"language": "en",
                                   "value": artist_name.get(aid, song.get("artist", "")) or "Unknown"}],
                    }]
                entity["albums"] = [{
                    "id": f"album.{album['id']}",
                    "names": [{"language": "en", "value": album.get("name") or "Unknown"}],
                }]
                out["tracks"].append(entity)
    log(f"tracks: {len(out['tracks'])}")

    tell(f"reading playlists ({len(out['tracks'])} tracks found)", 0.97)
    for playlist in subsonic.playlists():
        out["playlists"].append(base(f"playlist.{playlist['id']}", playlist.get("name")))
    out["playlists"].append(handoff_entity())
    log(f"playlists: {len(out['playlists'])}")

    for genre in subsonic.genres():
        if value := genre.get("value"):
            out["genres"].append(base(f"genre.{value}", value))
    log(f"genres: {len(out['genres'])}")

    return out


# --- collection from Music Assistant's library ------------------------------

# library_items pages at 500 by default; matched here so a page is one round
# trip and the loop below terminates on a short page.
_MA_PAGE = 500


def collect_from_ma(
    mass: MusicAssistant,
    loop: asyncio.AbstractEventLoop,
    providers: list[str] | None = None,
    *,
    progress=None,
    want_stations: bool | None = None,
) -> dict[str, list[dict]]:
    """
    Read the catalog from Music Assistant's synced library rather than Subsonic.

    :param mass: The running Music Assistant instance.
    :param loop: MA's event loop. This function runs in the upload worker thread
        (see tasks.py), so every controller call is bounced onto the loop with
        run_coroutine_threadsafe rather than awaited.
    :param providers: Provider instance ids to include; None or empty means the
        whole library across every provider.
    :param progress: Optional ``(text, fraction)`` callback for the task UI.
    """
    tell = progress or (lambda text, fraction=None: None)
    prov: list[str] | None = providers or None
    out: dict[str, list[dict]] = {k: [] for k in CATALOGS}

    def gather(coro):
        # Bounce an MA controller coroutine onto its own loop and block the
        # worker thread for the result. The reverse of the call_soon_threadsafe
        # the task uses for progress: there the loop calls into MA, here the
        # thread calls into the loop.
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def walk(controller, **kwargs):
        # library_items is paged; read it to exhaustion. summary=True keeps each
        # row to the few fields a catalog entity needs (name, ids, artist/album
        # mappings) and does no per-item network.
        offset = 0
        while True:
            batch = gather(
                controller.library_items(
                    provider=prov, limit=_MA_PAGE, offset=offset, summary=True, **kwargs
                )
            )
            if not batch:
                return
            yield from batch
            if len(batch) < _MA_PAGE:
                return
            offset += len(batch)

    def eid(kind: str, item_id) -> str:
        # A catalog entity id that names a Music Assistant library item. The
        # MA_ID_PREFIX marks it so the resolver (core.resolve_tracks) routes the
        # content id to MA rather than Subsonic without needing any other state.
        return f"{kind}.{MA_ID_PREFIX}{item_id}"

    def artist_ref(item) -> dict | None:
        # The first credited artist as a nested catalog reference. item_id is the
        # library artist db id (the summary subquery joins the artists table), so
        # it matches the id this same run emits into the artists catalog.
        artists = getattr(item, "artists", None) or ()
        if not artists:
            return None
        first = artists[0]
        return {
            "id": eid("artist", first.item_id),
            "names": [{"language": "en", "value": (getattr(first, "name", "") or "Unknown")[:512]}],
        }

    tell("reading artists (from Music Assistant)", 0.02)
    # A station per artist, named "<artist> Radio"/"<artist> Station", keyed
    # station.ma-<id> off the same library artist id the artists catalog uses,
    # so "play <artist> radio" resolves to a rad:ma-<id> the MA similar-tracks
    # resolver builds a real station from. Emitted only when the stations catalog
    # exists (want_stations); the standalone env fallback matches the collect
    # path.
    if want_stations is None:
        want_stations = bool(CATALOGS.get("stations"))
    for artist in walk(mass.music.artists):
        out["artists"].append(base(eid("artist", artist.item_id), artist.name))
        if want_stations and artist.name:
            out["stations"].append(
                station_entity(eid("station", artist.item_id), artist.name))
    log(f"artists: {len(out['artists'])}")
    if want_stations:
        log(f"stations: {len(out['stations'])}")

    tell(f"reading albums ({len(out['artists'])} artists)", 0.2)
    for album in walk(mass.music.albums):
        entity = base(eid("album", album.item_id), album.name)
        if ref := artist_ref(album):
            entity["artists"] = [ref]
        out["albums"].append(entity)
    log(f"albums: {len(out['albums'])}")

    tell(f"reading tracks ({len(out['albums'])} albums)", 0.4)
    for track in walk(mass.music.tracks):
        entity = base(eid("track", track.item_id), track.name)
        if ref := artist_ref(track):
            entity["artists"] = [ref]
        album = getattr(track, "album", None)
        if album is not None and getattr(album, "item_id", None) is not None:
            entity["albums"] = [{
                "id": eid("album", album.item_id),
                "names": [{"language": "en",
                           "value": (getattr(album, "name", "") or "Unknown")[:512]}],
            }]
        out["tracks"].append(entity)
    log(f"tracks: {len(out['tracks'])}")

    tell(f"reading playlists ({len(out['tracks'])} tracks)", 0.9)
    for playlist in walk(mass.music.playlists):
        out["playlists"].append(base(eid("playlist", playlist.item_id), playlist.name))
    out["playlists"].append(handoff_entity())
    log(f"playlists: {len(out['playlists'])}")

    tell("reading genres", 0.97)
    for genre in walk(mass.music.genres):
        out["genres"].append(base(eid("genre", genre.item_id), genre.name))
    log(f"genres: {len(out['genres'])}")

    return out


# --- diffing ----------------------------------------------------------------


def apply_timestamps(kind: str, entities: list[dict], state: dict) -> tuple[list[dict], dict]:
    """Stamp only what changed, and tombstone what vanished."""
    previous = state.get(kind, {})
    current, final = {}, []
    changed = new = 0

    for entity in entities:
        digest = fingerprint(entity)
        seen = previous.get(entity["id"])
        if seen and seen["hash"] == digest:
            entity["lastUpdatedTime"] = seen["ts"]
        else:
            entity["lastUpdatedTime"] = NOW
            if seen:
                changed += 1
            else:
                new += 1
        current[entity["id"]] = {"hash": digest, "ts": entity["lastUpdatedTime"]}
        final.append(entity)

    gone = set(previous) - set(current)
    for entity_id in gone:
        # A tombstone needs only these three fields. Without it the entity
        # lingers in Amazon's resolution and Alexa offers missing tracks.
        final.append({"id": entity_id, "lastUpdatedTime": NOW, "deleted": True})

    log(f"{kind}: {new} new, {changed} changed, {len(gone)} removed, "
        f"{len(final) - new - changed - len(gone)} unchanged")
    return final, current


# --- upload -----------------------------------------------------------------


def upload(kind: str, catalog_id: str, entities: list[dict]) -> bool:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{kind}.json"
    path.write_text(json.dumps({
        "type": TYPES[kind], "version": 2.0, "locales": LOCALES, "entities": entities,
    }))
    size = path.stat().st_size / 1024 / 1024
    log(f"{kind}: {len(entities)} entities, {size:.2f} MiB -> uploading")

    result = subprocess.run(
        ["ask", "smapi", "upload-catalog", "-c", catalog_id, "-f", str(path)],
        capture_output=True, text=True, timeout=600,
    )

    # The exit code is the verdict on its own. An earlier version ANDed it with
    # a search for "successfully" in the output, which meant any reword of the
    # CLI's success message would have reported every upload as a failure,
    # stalled the state file and re-uploaded the whole catalog on every run.
    if result.returncode != 0:
        log(f"{kind}: FAILED rc={result.returncode} {result.stderr.strip()[:200]}")
        return False

    # The string check survives as a second opinion rather than a verdict: it
    # is the only success wording anyone here has actually observed, so its
    # absence is worth saying out loud without being worth failing on.
    if "successfully" not in (result.stdout + result.stderr).lower():
        log(f"{kind}: uploaded (rc=0) but the output did not say 'successfully'; "
            "check the catalog status in the developer console")
    else:
        log(f"{kind}: uploaded")
    return True


# --- skill enablement -------------------------------------------------------


def _ask(verb: str, skill_id: str, stage: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ask", "smapi", verb, "--skill-id", skill_id, "--stage", stage],
        capture_output=True, text=True, timeout=ASK_TIMEOUT,
    )


def cycle_enablement(*, no_cycle: bool = False) -> bool:
    """Turn the skill off and back on again after a catalog upload.

    This is not superstition and it is not a leftover. Uploading a catalog
    unbinds the skill from the provider slot. Nothing anywhere reports it:
    ER_INGESTION says SUCCEEDED, the service keeps answering every directive
    correctly, and the only symptom is Alexa quietly playing the same request
    from the default provider and announcing "Here's ... from Spotify". It cost
    several hours to find once. Delete-then-set on the enablement rebinds it.

    Called only when something was actually uploaded, because the cycle is
    itself a short window with no skill enabled, and paying for that after a
    run that changed nothing is a pure loss.

    Returns False only when re-enabling failed, which is the state that leaves
    the skill off.
    """
    if no_cycle:
        log("enablement: skipping the cycle by request (--no-cycle)")
        return True

    skill_id = (os.environ.get("SKILL_ID") or "").strip()
    stage = (os.environ.get("SKILL_STAGE") or "").strip() or "development"

    if not skill_id:
        log("!! SKILL_ID is not set, so skill enablement was NOT cycled.")
        log("!! Voice playback is probably broken as of right now: Alexa will")
        log("!! fall back to the default music provider and say so out loud,")
        log("!! while this service keeps answering every request correctly and")
        log("!! the catalog reports SUCCEEDED. Nothing will alert you.")
        log("!! Run these two by hand, then say something to a device:")
        log("!!   ask smapi delete-skill-enablement --skill-id <your-skill-id> "
            "--stage development")
        log("!!   ask smapi set-skill-enablement --skill-id <your-skill-id> "
            "--stage development")
        # Not a failure of the sync itself. The catalog did upload.
        return True

    log(f"enablement: cycling {skill_id} ({stage})")

    deleted = _ask("delete-skill-enablement", skill_id, stage)
    if deleted.returncode != 0:
        # Deleting an enablement that does not exist is the normal state after
        # a failed earlier run, and is not something to fail on: the set that
        # follows is what actually matters.
        log(f"enablement: delete returned {deleted.returncode}, continuing "
            f"(harmless if it was not enabled): {deleted.stderr.strip()[:200]}")

    enabled = _ask("set-skill-enablement", skill_id, stage)
    if enabled.returncode != 0:
        log(f"!! enablement: FAILED to re-enable rc={enabled.returncode}: "
            f"{enabled.stderr.strip()[:200]}")
        log("!! The skill is now disabled. Voice playback will fall back to the")
        log("!! default provider until 'ask smapi set-skill-enablement' succeeds.")
        return False

    log("enablement: cycled")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_cycle = "--no-cycle" in argv or os.environ.get("CATALOG_NO_CYCLE") == "1"

    missing = [k for k, v in CATALOGS.items() if not v and k not in OPTIONAL_KINDS]
    if missing:
        log(f"no catalog id configured for: {', '.join(missing)}")
        return 2

    state = load_state()
    collected = collect()
    failures = uploaded = 0

    for kind, entities in collected.items():
        if not entities:
            log(f"{kind}: nothing collected, skipping (refusing to tombstone everything)")
            continue
        final, current = apply_timestamps(kind, entities, state)
        if upload(kind, CATALOGS[kind], final):
            state[kind] = current
            uploaded += 1
        else:
            failures += 1

    save_state(state)

    if uploaded:
        if not cycle_enablement(no_cycle=no_cycle):
            failures += 1
    else:
        log("nothing uploaded, leaving skill enablement alone")

    log("done" if not failures else f"done with {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
