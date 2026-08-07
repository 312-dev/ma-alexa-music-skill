"""What an adapter is asked to put on the wire.

Five shapes cover every route Music Assistant has. They are plain tuples of plain
values, so that no adapter has to know anything about another's framework and
no framework leaks into the modules that decide things.

The alternative was for each route to return an already-built response, which
is what the Flask version did and is exactly what made the service unmovable:
a Flask `Response` cannot be handed to aiohttp, so every route would have had
to be written twice.

They live in a module of their own rather than in `core` because `core` and
`queue_api` both answer requests and `core` imports `queue_api`. A shared
vocabulary in one direction is a dependency; a shared vocabulary in both is a
cycle.
"""

from __future__ import annotations

import contextlib
import urllib.request
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, NamedTuple


class Json(NamedTuple):
    """A JSON body. The overwhelming majority of what this service answers."""

    status: int
    payload: dict | list


class Page(NamedTuple):
    """A body the adapter should send verbatim, text or bytes."""

    status: int
    body: str | bytes
    content_type: str
    headers: Mapping[str, str] = MappingProxyType({})


class Redirect(NamedTuple):
    """A 302. Only account linking produces one."""

    location: str


class LocalFile(NamedTuple):
    """A file on disk, to be served with range support.

    Deliberately not read into memory here. Both frameworks already have a
    range-aware file sender (`send_file(conditional=True)` and
    `web.FileResponse`), and a buffered Music Assistant track is the one thing
    Alexa does send ranges for, so handing over the path rather than the bytes
    is what keeps scrubbing and room-to-room moves working.
    """

    path: pathlib.Path
    content_type: str


class Upstream(NamedTuple):
    """An open response from Navidrome or Music Assistant, to be piped through.

    `stream` is the raw file-like object rather than a generator, because the
    two adapters have to consume it differently: Flask can iterate it on the
    request thread, while aiohttp has to read each chunk in a worker thread or
    it would stall Music Assistant's event loop for the length of the track.
    """

    status: int
    headers: Mapping[str, str]
    stream: Any


# How much of an upstream response to move at a time. 64 KiB is large enough
# that the per-chunk overhead disappears against the audio and small enough
# that a client going away is noticed promptly rather than after a megabyte.
CHUNK = 64 * 1024


def iter_chunks(stream: Any) -> Iterator[bytes]:
    """Read an upstream response to exhaustion, closing it afterwards."""
    try:
        while chunk := stream.read(CHUNK):
            yield chunk
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def fetch_upstream(url: str, range_header: str | None = None,
                   default_content_type: str = "application/octet-stream") -> Upstream:
    """Open a proxied fetch of Navidrome or Music Assistant.

    Only the headers that matter to a media client are carried across. Copying
    everything would also carry Navidrome's cookies and cache validators, which
    are about a conversation Amazon is not part of.
    """
    req = urllib.request.Request(url)
    if range_header:
        req.add_header("Range", range_header)
    resp = urllib.request.urlopen(req, timeout=20)
    headers = {}
    for key in ("Content-Type", "Content-Length", "Accept-Ranges", "Content-Range"):
        if value := resp.headers.get(key):
            headers[key] = value
    # Navidrome does not always name the type on a transcode, and Amazon will
    # not play audio it has not been told the type of.
    headers.setdefault("Content-Type", default_content_type)
    return Upstream(resp.status, headers, resp)
