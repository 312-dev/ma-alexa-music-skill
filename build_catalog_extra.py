"""Build AMAZON.MusicPlaylist and AMAZON.Genre catalogs from Navidrome."""
import hashlib, json, os, secrets, time, urllib.parse, urllib.request

BASE = os.environ.get("SUBSONIC_URL", "http://100.93.15.8:4533").rstrip("/")
USER = os.environ.get("SUBSONIC_USER", "grayson")
PASSWORD = os.environ.get("SUBSONIC_PASSWORD", "")
OUT = os.environ.get("OUT_DIR", "/tmp/catalog")
STAMP = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
LOCALES = [{"country": "US", "language": "en"}]
POP = {"default": 100, "overrides": [{"locale": LOCALES[0], "value": 100}]}

def call(view, **params):
    salt = secrets.token_hex(8)
    tok = hashlib.md5(f"{PASSWORD}{salt}".encode()).hexdigest()
    q = {"u": USER, "t": tok, "s": salt, "v": "1.16.1", "c": "cat", "f": "json", **params}
    url = f"{BASE}/rest/{view}?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())["subsonic-response"]

def entity(eid, name):
    return {"id": eid, "names": [{"language": "en", "value": name[:512]}],
            "popularity": POP, "lastUpdatedTime": STAMP, "locales": LOCALES}

os.makedirs(OUT, exist_ok=True)

pls = call("getPlaylists.view").get("playlists", {}).get("playlist", [])
doc = {"type": "AMAZON.MusicPlaylist", "version": 2.0, "locales": LOCALES,
       "entities": [entity(f"playlist.{p['id']}", p.get("name", "")) for p in pls]}
json.dump(doc, open(f"{OUT}/playlists.json", "w"))
print(f"playlists: {len(pls)} -> {OUT}/playlists.json")

gs = call("getGenres.view").get("genres", {}).get("genre", [])
doc = {"type": "AMAZON.Genre", "version": 2.0, "locales": LOCALES,
       "entities": [entity(f"genre.{g['value']}", g.get("value", "")) for g in gs if g.get("value")]}
json.dump(doc, open(f"{OUT}/genres.json", "w"))
print(f"genres: {len(gs)} -> {OUT}/genres.json")
