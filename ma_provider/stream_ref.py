"""How a Music Assistant item is named on the wire between MA and the bridge.

Two processes have to agree on this and only one of them can import Music
Assistant, so it lives on its own with nothing but the standard library behind
it. `stream_route.py` serves the route; `app.py` composes the URL that reaches
it; both read the shape from here rather than each spelling it out.

The reference is the item's MA uri and nothing else. Not a queue item id, not a
session id, not a resolved stream URL:

  - a **stream URL** is scoped to a queue session, and Alexa holds a published
    queue for hours and may fetch track twelve long after MA has rolled that
    session out from under it
  - a **queue item id** dies with the queue, and the whole point of publishing
    is that Alexa outlives it
  - a **uri** is the item's permanent identity and resolves to audio on its own

`spotify://track/4uLU6hMCjMI75M1A2tKUQC` cannot be a URL path segment as it
stands, so it travels base64url-encoded without padding.
"""

from __future__ import annotations

import base64
import binascii

# The prefix the route is registered under inside Music Assistant. Trailing
# slash included: it is concatenated, never joined.
ROUTE_PREFIX = "/ma_alexa_stream/"

# Marks a catalog native id as a Music Assistant library id rather than a
# Subsonic one. A catalog built from MA's library (see catalog_sync) emits
# entity ids like "artist.ma-1234"; the resolver (core.resolve_tracks) reads
# this prefix off the id and routes to MA instead of Subsonic, so a single
# content id is self-describing and needs no external state to place its source.
MA_ID_PREFIX = "ma-"

# Cosmetic. Some clients decide how to treat a URL by looking at its extension,
# and nothing in this path reads it back.
EXTENSION = ".mp3"


def encode_ref(uri: str) -> str:
    """A Music Assistant uri, safe to carry in a URL path segment."""
    return base64.urlsafe_b64encode(uri.encode()).decode().rstrip("=")


def decode_ref(ref: str) -> str:
    """The uri behind a ref, or "" if it is not one this module issued."""
    try:
        padded = ref + "=" * (-len(ref) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


# Media types whose audio never ends. Buffering one of these would fill the
# disk and never finish, and seeking into one means nothing, so they are routed
# around the cache entirely.
LIVE_TYPES = frozenset({"radio"})


def media_type(ref: str) -> str:
    """The media type named by a ref, or "" if it does not name one.

    A Music Assistant uri is `provider://mediatype/itemid`, so the type is
    already in the reference and does not have to be carried alongside it. That
    matters more than it looks: a queue published yesterday decides correctly
    today, and nothing that publishes a queue can misdescribe what it is
    publishing.
    """
    uri = decode_ref(ref)
    if not uri or "://" not in uri:
        return ""
    return uri.split("://", 1)[1].split("/", 1)[0].strip().lower()


def is_live(ref: str) -> bool:
    """Whether this reference is an endless stream rather than a track."""
    return media_type(ref) in LIVE_TYPES


def is_ref(value: str) -> bool:
    """Whether a string decodes to something shaped like an MA uri.

    The bridge signs and serves whatever it is handed, so this is the check
    that stops a published queue turning into a way to make the bridge fetch
    arbitrary things: a ref that does not decode to a `scheme://rest` uri is
    refused at publish time rather than at play time.
    """
    uri = decode_ref(value)
    return bool(uri) and "://" in uri and not uri.startswith(("http://", "https://"))


def stream_path(ref: str) -> str:
    """The path on Music Assistant's webserver that serves this ref."""
    return f"{ROUTE_PREFIX}{ref}{EXTENSION}"


def ref_from_path(path: str) -> str:
    """The ref out of a request path, ignoring query string and extension."""
    if not path.startswith(ROUTE_PREFIX):
        return ""
    tail = path[len(ROUTE_PREFIX):].split("?", 1)[0].strip("/")
    return tail.rsplit(".", 1)[0] if "." in tail else tail
