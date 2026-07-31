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

import subsonic

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


# --- collection -------------------------------------------------------------


def collect() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {k: [] for k in CATALOGS}

    artist_name: dict[str, str] = {}
    for index in subsonic.call("getArtists.view").get("artists", {}).get("index", []):
        for artist in index.get("artist", []):
            artist_name[artist["id"]] = artist.get("name", "")
            out["artists"].append(base(f"artist.{artist['id']}", artist.get("name")))
    log(f"artists: {len(out['artists'])}")

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

    with ThreadPoolExecutor(max_workers=8) as pool:
        for album, songs in pool.map(tracks_of, albums):
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

    for playlist in subsonic.playlists():
        out["playlists"].append(base(f"playlist.{playlist['id']}", playlist.get("name")))
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
    ok = result.returncode == 0 and "successfully" in (result.stdout + result.stderr).lower()
    log(f"{kind}: {'uploaded' if ok else 'FAILED ' + result.stderr.strip()[:200]}")
    return ok


def main() -> int:
    missing = [k for k, v in CATALOGS.items() if not v]
    if missing:
        log(f"no catalog id configured for: {', '.join(missing)}")
        return 2

    state = load_state()
    collected = collect()
    failures = 0

    for kind, entities in collected.items():
        if not entities:
            log(f"{kind}: nothing collected, skipping (refusing to tombstone everything)")
            continue
        final, current = apply_timestamps(kind, entities, state)
        if upload(kind, CATALOGS[kind], final):
            state[kind] = current
        else:
            failures += 1

    save_state(state)
    log("done" if not failures else f"done with {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
