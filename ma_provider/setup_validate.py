"""Endpoint, alias and library checks.

Every check here corresponds to a failure that produced no error message
anywhere: a PUBLIC_BASE pointing at a tailnet address, a wildcard certificate
declared as Trusted, a reverse proxy that answered GET and quietly dropped the
POST body, an alias word already claimed by an artist in the user's own
library. Amazon reports none of these. The only way to find them is to look.

The network-touching functions are deliberately small and named, because tests
replace them wholesale and must never reach a real socket.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

CGNAT = ipaddress.ip_network("100.64.0.0/10")

PROBE_DIRECTIVE = {
    "header": {
        "namespace": "Alexa.Media.Search",
        "name": "GetPlayableContent",
        "messageId": "ma-alexa-setup-probe",
        "payloadVersion": "1.0",
    },
    # MEDIA_TYPE only, so the bridge answers from its own logic without
    # touching the music server. All we are proving is that the body arrived.
    "payload": {"selectionCriteria": {"attributes": [
        {"type": "MEDIA_TYPE", "value": "TRACK"},
    ]}},
}


def check(name: str, ok: bool | None, detail: str, note: str = "",
          diag: dict | None = None) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "note": note,
            "diag": diag}


def _diag(url: str, status: int, headers, body: str = "") -> dict:
    """What actually came back, for the expandable details on a check row.

    The status line and headers say which layer answered: a Server header of
    cloudflare on a 403 is a CDN rule, not the bridge, and no amount of
    prose in the check's summary can substitute for seeing that.
    """
    return {
        "url": url,
        "status": status,
        "headers": [(k, v) for k, v in (headers or {}).items()],
        "body": (body or "")[:600],
    }


# --- address ----------------------------------------------------------------


def split_base(base: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(base if "//" in base else f"//{base}")
    scheme = parts.scheme or ""
    port = parts.port or (443 if scheme != "http" else 80)
    return scheme, (parts.hostname or ""), port


def resolve(host: str) -> list[str]:
    """Every address the hostname answers with. Replaced in tests."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def classify_address(raw: str) -> str:
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.version == 4 and addr in CGNAT:
        # Tailscale hands out 100.64.0.0/10, so a PUBLIC_BASE resolving here is
        # almost always a tailnet name that only the operator's own machines
        # can reach. It looks completely healthy from the box running the test.
        return "cgnat"
    if addr.is_private:
        return "private"
    return "public"


_UNROUTABLE = {
    "loopback": "loopback. Amazon cannot reach your own machine.",
    "link-local": "link-local. Not routable from outside this network segment.",
    "cgnat": "in 100.64.0.0/10, the carrier-grade NAT range Tailscale uses. "
             "Reachable from your tailnet only, which includes the box running "
             "this check, so it will look fine from here and never work.",
    "private": "an RFC1918 private address. Not routable from the internet.",
}


def check_scheme(base: str) -> dict:
    scheme, host, _ = split_base(base)
    if not host:
        return check("PUBLIC_BASE is set", False, "PUBLIC_BASE is empty or unparseable.")
    if scheme != "https":
        return check("PUBLIC_BASE uses https", False,
                     f"Scheme is {scheme or 'missing'}. Amazon requires https.")
    return check("PUBLIC_BASE uses https", True, f"https, host {host}")


def check_address(base: str) -> dict:
    _, host, _ = split_base(base)
    if not host:
        return check("Resolves to a public address", False, "No host to resolve.")
    try:
        addresses = resolve(host)
    except OSError as exc:
        return check("Resolves to a public address", False,
                     f"DNS lookup for {host} failed: {exc}")
    if not addresses:
        return check("Resolves to a public address", False, f"{host} resolves to nothing.")
    for address in addresses:
        kind = classify_address(address)
        if kind in _UNROUTABLE:
            return check("Resolves to a public address", False,
                         f"{host} resolves to {address}, which is {_UNROUTABLE[kind]}")
    return check("Resolves to a public address", True,
                 f"{host} resolves to {', '.join(addresses)}")


# --- TLS --------------------------------------------------------------------


def peer_cert(host: str, port: int, timeout: float = 8.0) -> dict:
    """The certificate the endpoint actually serves. Replaced in tests."""
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert() or {}


def sans_of(cert: dict) -> list[str]:
    return [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]


def derive_cert_type(host: str, sans: list[str]) -> str:
    """Trusted or Wildcard, the way Amazon means it.

    This is the field that, set wrong, makes Amazon accept the manifest and
    then never call the endpoint. Nothing is logged, on either side.
    """
    parent = host.split(".", 1)[1] if "." in host else ""
    for san in sans:
        if san.startswith("*.") and parent and san[2:].lower() == parent.lower():
            return "Wildcard"
    return "Trusted"


def check_tls(base: str) -> dict:
    scheme, host, port = split_base(base)
    if not host or scheme != "https":
        return check("TLS handshake and certificate", False, "Needs an https PUBLIC_BASE.")
    try:
        cert = peer_cert(host, port)
    except (OSError, ssl.SSLError) as exc:
        return check("TLS handshake and certificate", False,
                     f"Handshake with {host}:{port} failed: {exc}")
    sans = sans_of(cert)
    cert_type = derive_cert_type(host, sans)
    listed = ", ".join(sans[:8]) or "none published"
    return check(
        "TLS handshake and certificate", True,
        f"Handshake ok. SAN: {listed}",
        note=cert_type,
    )


# --- HTTP over the public path ----------------------------------------------


# An honest, named user agent. Python's default one sits on CDN bot
# blocklists: Cloudflare's free tier answers Python-urllib with 403 while
# letting curl, browsers and Amazon straight through, which made these checks
# report an outage that did not exist.
_UA = "Music Assistant-endpoint-check/1.0 (+https://github.com/312-dev/ma-alexa-music-skill)"


def http_get(url: str, timeout: float = 8.0) -> tuple[int, str, dict]:
    """Replaced in tests. Never called against localhost by design."""
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return (resp.status, resp.read(4096).decode("utf-8", "replace"),
                    dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read(4096).decode("utf-8", "replace"),
                dict(exc.headers or {}))


def http_post_json(url: str, body: dict,
                   timeout: float = 12.0) -> tuple[int, str, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return (resp.status, resp.read(8192).decode("utf-8", "replace"),
                    dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read(8192).decode("utf-8", "replace"),
                dict(exc.headers or {}))


def check_healthz(base: str) -> dict:
    url = f"{base.rstrip('/')}/healthz"
    try:
        status, body, headers = http_get(url)
    except OSError as exc:
        return check("GET /healthz over the public URL", False, f"{url} failed: {exc}")
    diag = _diag(url, status, headers, body)
    if status != 200:
        return check("GET /healthz over the public URL", False,
                     f"{url} answered {status}.", diag=diag)
    return check("GET /healthz over the public URL", True,
                 f"{url} answered 200.", diag=diag)


def check_music_post(base: str) -> dict:
    """Prove the proxy passes a POST body through unchanged.

    A proxy that answers GET perfectly and buffers, strips or rewrites POST
    bodies is common and invisible: the bridge sees no JSON, answers 400, and
    Alexa reports nothing at all.
    """
    url = f"{base.rstrip('/')}/music"
    try:
        status, body, headers = http_post_json(url, PROBE_DIRECTIVE)
    except OSError as exc:
        return check("POST /music with a real directive", False, f"{url} failed: {exc}")
    diag = _diag(url, status, headers, body)
    if status != 200:
        return check("POST /music with a real directive", False,
                     f"{url} answered {status}. A 400 here usually means the proxy "
                     "dropped the request body.", diag=diag)
    try:
        envelope = json.loads(body)
        namespace = envelope["header"]["namespace"]
    except (ValueError, KeyError, TypeError):
        return check("POST /music with a real directive", False,
                     "200, but the response was not an Alexa envelope.", diag=diag)
    return check("POST /music with a real directive", True,
                 f"200 with a {namespace} envelope. The body survived the proxy.",
                 diag=diag)


# --- external proof ---------------------------------------------------------


class Tokens:
    """One-shot verification tokens for the scan-it-on-cellular check.

    In memory on purpose: they are worth nothing after a few minutes, and a
    token that survives a restart is a token that outlives its reason to exist.
    """

    def __init__(self, ttl: int = 900) -> None:
        self.ttl = ttl
        self._tokens: dict[str, dict] = {}

    def mint(self) -> str:
        self._sweep()
        token = secrets.token_urlsafe(12)
        self._tokens[token] = {"born": time.time(), "seen": None, "agent": ""}
        return token

    def _sweep(self) -> None:
        cutoff = time.time() - self.ttl
        for token in [t for t, r in self._tokens.items() if r["born"] < cutoff]:
            del self._tokens[token]

    def mark_seen(self, token: str, agent: str = "") -> str:
        record = self._tokens.get(token)
        if record is None:
            return "unknown"
        if record["born"] < time.time() - self.ttl:
            del self._tokens[token]
            return "expired"
        if record["seen"] is None:
            record["seen"] = time.time()
            record["agent"] = agent[:200]
        return "seen"

    def status(self, token: str) -> str:
        record = self._tokens.get(token)
        if record is None:
            return "unknown"
        if record["born"] < time.time() - self.ttl:
            return "expired"
        return "seen" if record["seen"] else "pending"

    def record(self, token: str) -> dict | None:
        return self._tokens.get(token)


# --- Subsonic ---------------------------------------------------------------


def subsonic_ping(url: str, user: str, password: str, timeout: float = 8.0) -> dict:
    """Test one set of credentials without disturbing the live client.

    Deliberately does not reuse subsonic.build_url: that reads module globals,
    so testing a candidate would mean mutating the settings the running bridge
    is serving requests with.
    """
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    query = urllib.parse.urlencode({
        "u": user, "t": token, "s": salt,
        "v": "1.16.1", "c": "ma-alexa-setup", "f": "json",
    })
    target = f"{url.rstrip('/')}/rest/ping.view?{query}"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "detail": f"{url} did not answer: {exc}"}
    inner = body.get("subsonic-response", {})
    if inner.get("status") != "ok":
        err = inner.get("error", {})
        return {"ok": False,
                "detail": f"server said {err.get('code')}: {err.get('message')}"}
    return {"ok": True,
            "detail": f"connected, Subsonic API {inner.get('version', '?')}"}


# --- alias ------------------------------------------------------------------

# Words Alexa already owns, or that a phone speaker in the room answers to.
# Short and hardcoded on purpose: unlike catalog collisions, this list does not
# depend on the user's library.
BRANDS = {
    "alexa", "amazon", "echo", "apple", "spotify", "pandora", "sonos", "siri",
    "google", "nest", "tidal", "deezer", "napster", "tunein", "iheart",
    "iheartradio", "audible", "youtube", "netflix", "plex", "jellyfin", "roku",
    "prime", "sirius", "siriusxm", "soundcloud", "bandcamp", "kodi",
}

# Nouns Alexa parses structurally. An alias made of these competes with the
# grammar itself rather than with any one catalog entry.
COMMON = {
    "music", "radio", "station", "song", "songs", "track", "album", "artist",
    "playlist", "play", "stop", "pause", "next", "volume", "speaker",
    "speakers", "sound", "audio", "home", "everywhere", "shuffle", "library",
}

# Too common to mean anything as a shared word between an alias and a title.
STOPWORDS = {
    "the", "and", "for", "you", "your", "our", "its", "with", "from", "that",
    "this", "are", "was", "not", "all", "one", "out", "his", "her", "them",
    "into", "over", "when", "what", "who", "how",
}

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def soundex(word: str) -> str:
    """Cheap soundalike key, for collisions spelling alone would miss.

    It catches same-sounding variants like Gray and Grey. It does not catch
    genuine mishearing: "phono" came back as "Sonos" every time, and nothing
    textual relates those two. The page says so rather than pretending.
    """
    word = "".join(ch for ch in (word or "").lower() if ch.isalpha())
    if not word:
        return ""
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
             **dict.fromkeys("dt", "3"), "l": "4",
             **dict.fromkeys("mn", "5"), "r": "6"}
    out = word[0].upper()
    previous = codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != previous:
            out += code
        if ch not in "hw":
            previous = code
    return (out + "000")[:4]


def soundalike(a: str, b: str) -> bool:
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return False
    return all(
        any(soundex(word) == soundex(other) for other in right.split())
        for word in left.split()
    ) or all(
        any(soundex(word) == soundex(other) for other in left.split())
        for word in right.split()
    )


def compare(candidate: str, name: str) -> tuple[str, str] | None:
    """How badly one library entity threatens one alias, if at all."""
    alias, other = normalize(candidate), normalize(name)
    if not alias or not other:
        return None
    if alias == other:
        return "high", "the same name"
    if alias in other.split() or other in alias.split():
        return "high", "one is a whole word of the other"
    # Spacing is not a defense. "jukebox" lost to the track "Juke Box Hero",
    # which shares no whole word with it and is not a substring of it either.
    tight_alias, tight_other = alias.replace(" ", ""), other.replace(" ", "")
    if tight_alias == tight_other:
        return "high", "the same name apart from spacing"
    if tight_alias in tight_other or tight_other in tight_alias:
        return "medium", "one contains the other once spacing is ignored"
    # A single shared word is enough to lose the slot: "gray tunes" resolved to
    # the artist Conan Gray on the strength of one word out of two.
    shared = {word for word in set(alias.split()) & set(other.split())
              if len(word) >= 3 and word not in STOPWORDS}
    if shared:
        return "medium", f"shares the word \"{sorted(shared)[0]}\""
    if soundalike(alias, other):
        return "medium", "sounds the same to speech recognition"
    return None


def library_names(candidate: str, subsonic_module) -> list[tuple[str, str]]:
    """(kind, name) pairs from the user's own library that might collide.

    Server-side search does the fuzzy half. Playlists and genres are small
    enough to list whole, and the search endpoint does not cover them.
    """
    found: list[tuple[str, str]] = []
    try:
        hits = subsonic_module.search(candidate, songs=25, albums=15, artists=15)
    except Exception:
        hits = {}
    for key, kind in (("artist", "artist"), ("album", "album"), ("song", "track")):
        for item in hits.get(key) or []:
            name = item.get("name") or item.get("title")
            if name:
                found.append((kind, name))
    for getter, kind, field in (
        (getattr(subsonic_module, "playlists", None), "playlist", "name"),
        (getattr(subsonic_module, "genres", None), "genre", "value"),
    ):
        if getter is None:
            continue
        try:
            for item in getter() or []:
                if name := item.get(field):
                    found.append((kind, name))
        except Exception:
            continue
    return found


def assess_alias(candidate: str, subsonic_module) -> dict:
    """Rank everything competing with an alias word, and say what to do.

    Alexa resolves content against the uploaded catalog before it decides which
    provider to route to, so every artist, album, track, playlist and genre in
    the library is a rival for the alias. That is why this cannot be a constant
    in the code: "jukebox" lost to Jukebox The Ghost and Juke Box Hero here,
    and "gray tunes" resolved to the artist Conan Gray.
    """
    alias = normalize(candidate)
    if not alias:
        return {"candidate": candidate, "verdict": "empty", "rows": [],
                "summary": "Type a candidate alias."}

    rows: list[dict] = []
    for word in alias.split():
        if word in BRANDS:
            rows.append({"risk": "high", "kind": "brand", "name": word,
                         "reason": "a brand Alexa already routes to"})
        elif word in COMMON:
            rows.append({"risk": "medium", "kind": "common word", "name": word,
                         "reason": "a word Alexa parses as part of the request"})
        elif any(soundex(word) == soundex(brand) for brand in BRANDS):
            near = next(b for b in sorted(BRANDS) if soundex(b) == soundex(word))
            rows.append({"risk": "medium", "kind": "brand", "name": near,
                         "reason": f"sounds like {near} to speech recognition"})

    seen: set[tuple[str, str]] = set()
    for kind, name in library_names(candidate, subsonic_module):
        verdict = compare(candidate, name)
        if verdict is None or (kind, name) in seen:
            continue
        seen.add((kind, name))
        risk, reason = verdict
        rows.append({"risk": risk, "kind": kind, "name": name, "reason": reason})

    order = {"high": 0, "medium": 1}
    rows.sort(key=lambda row: (order.get(row["risk"], 2), row["kind"], row["name"]))

    if any(row["risk"] == "high" for row in rows):
        verdict, summary = "bad", "Pick something else. This will be taken from you."
    elif rows:
        verdict, summary = "risky", "Usable, but expect to be misheard some of the time."
    else:
        verdict, summary = "clear", "Nothing in your library competes with this."
    return {"candidate": candidate, "verdict": verdict, "rows": rows, "summary": summary}
