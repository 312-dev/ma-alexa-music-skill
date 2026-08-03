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

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import handoff
from . import subsonic

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
}

ASK_TIMEOUT = 120

TYPES = {
    "artists": "AMAZON.MusicGroup",
    "albums": "AMAZON.MusicAlbum",
    "tracks": "AMAZON.MusicRecording",
    "playlists": "AMAZON.MusicPlaylist",
    "genres": "AMAZON.Genre",
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


def collect(progress=None) -> dict[str, list[dict]]:
    """Read the whole library. `progress` gets a short human line at each
    stage; the album-by-album track crawl is minutes long on a real
    collection, and a silent minutes-long job reads as a hung one."""
    tell = progress or (lambda text, fraction=None: None)
    out: dict[str, list[dict]] = {k: [] for k in CATALOGS}

    tell("reading artists", 0.02)
    artist_name: dict[str, str] = {}
    for index in subsonic.call("getArtists.view").get("artists", {}).get("index", []):
        for artist in index.get("artist", []):
            artist_name[artist["id"]] = artist.get("name", "")
            out["artists"].append(base(f"artist.{artist['id']}", artist.get("name")))
    log(f"artists: {len(out['artists'])}")

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

    missing = [k for k, v in CATALOGS.items() if not v]
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
