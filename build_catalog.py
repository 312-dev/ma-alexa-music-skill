"""Export a Navidrome library into Alexa music-skill catalog files.

Produces three JSON documents (artists, albums, tracks) in Amazon's
AMAZON.MusicGroup / MusicAlbum / MusicRecording schema, ready for
`ask smapi upload-catalog`.

Entity ids must be globally unique and stable across catalogs for the skill,
so Navidrome's own ids are reused with a type prefix. Cross-references inside
track/album records use those same ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("SUBSONIC_URL", "http://100.93.15.8:4533").rstrip("/")
USER = os.environ.get("SUBSONIC_USER", "grayson")
PASSWORD = os.environ.get("SUBSONIC_PASSWORD", "")
OUT_DIR = os.environ.get("OUT_DIR", "/tmp/catalog")

STAMP = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
LOCALES = [{"country": "US", "language": "en"}]
POPULARITY = {"default": 100, "overrides": [{"locale": LOCALES[0], "value": 100}]}


def call(view: str, **params) -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{PASSWORD}{salt}".encode()).hexdigest()
    query = {
        "u": USER, "t": token, "s": salt,
        "v": "1.16.1", "c": "catalog-export", "f": "json",
        **{k: str(v) for k, v in params.items() if v is not None},
    }
    url = f"{BASE}/rest/{view}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    inner = body.get("subsonic-response", {})
    if inner.get("status") != "ok":
        raise RuntimeError(f"{view}: {inner.get('error')}")
    return inner


def names(value: str) -> list[dict]:
    return [{"language": "en", "value": (value or "Unknown")[:512]}]


def document(kind: str, entities: list[dict]) -> dict:
    return {
        "type": kind,
        "version": 2.0,
        "locales": LOCALES,
        "entities": entities,
    }


def base_entity(entity_id: str, display: str) -> dict:
    return {
        "id": entity_id,
        "names": names(display),
        "popularity": POPULARITY,
        "lastUpdatedTime": STAMP,
        "locales": LOCALES,
    }


def collect() -> tuple[list[dict], list[dict], list[dict]]:
    artists, albums, tracks = [], [], []
    artist_name: dict[str, str] = {}

    for index in call("getArtists.view").get("artists", {}).get("index", []):
        for artist in index.get("artist", []):
            artist_name[artist["id"]] = artist.get("name", "Unknown")
            artists.append(base_entity(f"artist.{artist['id']}", artist.get("name")))
    print(f"artists: {len(artists)}", file=sys.stderr)

    offset = 0
    raw_albums = []
    while True:
        page = (
            call("getAlbumList2.view", type="alphabeticalByName", size=500, offset=offset)
            .get("albumList2", {})
            .get("album", [])
        )
        if not page:
            break
        raw_albums.extend(page)
        offset += len(page)
        print(f"  albums fetched: {offset}", file=sys.stderr)
        if len(page) < 500:
            break

    for album in raw_albums:
        entity = base_entity(f"album.{album['id']}", album.get("name"))
        aid = album.get("artistId")
        if aid:
            entity["artists"] = [
                {"id": f"artist.{aid}", "names": names(artist_name.get(aid, album.get("artist", "")))}
            ]
        albums.append(entity)
    print(f"albums: {len(albums)}", file=sys.stderr)

    for n, album in enumerate(raw_albums, 1):
        try:
            songs = call("getAlbum.view", id=album["id"]).get("album", {}).get("song", [])
        except Exception as exc:
            print(f"  skip album {album['id']}: {exc}", file=sys.stderr)
            continue
        for song in songs:
            entity = base_entity(f"track.{song['id']}", song.get("title"))
            aid = song.get("artistId") or album.get("artistId")
            if aid:
                entity["artists"] = [
                    {"id": f"artist.{aid}", "names": names(artist_name.get(aid, song.get("artist", "")))}
                ]
            entity["albums"] = [
                {"id": f"album.{album['id']}", "names": names(album.get("name"))}
            ]
            tracks.append(entity)
        if n % 100 == 0:
            print(f"  tracks so far: {len(tracks)} ({n}/{len(raw_albums)} albums)", file=sys.stderr)
    print(f"tracks: {len(tracks)}", file=sys.stderr)

    return artists, albums, tracks


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    artists, albums, tracks = collect()
    for name, kind, entities in (
        ("artists", "AMAZON.MusicGroup", artists),
        ("albums", "AMAZON.MusicAlbum", albums),
        ("tracks", "AMAZON.MusicRecording", tracks),
    ):
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w") as handle:
            json.dump(document(kind, entities), handle)
        size = os.path.getsize(path) / 1024 / 1024
        print(f"wrote {path}  {len(entities)} entities  {size:.2f} MiB")


if __name__ == "__main__":
    main()
