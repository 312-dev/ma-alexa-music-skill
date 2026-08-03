"""Serving Music Assistant's own audio, so the bridge is not limited to Subsonic.

Everything Ampere plays today comes from the Subsonic server the bridge
streams from. A Spotify or Tidal track sitting in a Music Assistant queue has
no Subsonic id, so it is dropped from the published list and silently does not
play. That is the gap phase 2 of PLAN.md exists to close.

## Why a route inside Music Assistant

MA already serves audio, and the obvious move is to hand Alexa one of its
existing stream URLs. That does not work, for a reason that is structural
rather than incidental:

    /single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}

    if queue.session_id and session_id != queue.session_id:
        raise web.HTTPNotFound(...)

The URL is scoped to a queue session. Alexa owns its queue for hours and may
fetch track twelve long after track one; MA rolls its session underneath and
the URL 404s. Alexa is also handed the whole track list up front, so every one
of those URLs would have to stay valid for the life of the Alexa queue.

So this registers a route of its own, keyed by nothing but the item's MA uri.
`get_preview_stream` in MA's own streams controller shows the whole chain and
none of it involves a queue:

    provider     = mass.get_provider(<domain>)
    streamdetails = await provider.get_stream_details(item_id, media_type)
    pcm          = get_media_stream(streamdetails, pcm_format)
    encoded      = get_ffmpeg_stream(pcm, ...)

A uri resolves to audio directly. That makes the reference stable, sessionless
and good for as long as the track exists, which is what a published Alexa queue
needs.

## What this deliberately does not do

Phase 2 is a spike. MA hands out realtime audio: no `Content-Length`, no
`Accept-Ranges`, always `200`, never `206`. This route inherits all of that and
does not pretend otherwise, because the point is to find out empirically what
Alexa does with such a source rather than to guess.

`Range` requests are therefore answered with the stream from the beginning. A
request that carries one is logged at warning, because on 2026-08-02 a
room-to-room transfer was measured to re-pull the same URL with a `Range`
header on every move, so how often Alexa asks is the single most important
number this spike can produce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.uri import parse_uri

from .stream_ref import ROUTE_PREFIX, decode_ref, ref_from_path, stream_path

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant

# Registered as a prefix wildcard, the way MA's own sonos provider registers
# `/sonos_queue/*`. The tail is read off the request path rather than from a
# route pattern, because dynamic routes are matched by string and carry no
# aiohttp match_info.
ROUTE_PATTERN = ROUTE_PREFIX + "*"

# MP3 because it is the format the bridge already declares for Subsonic
# (`audio/mpeg`) and the one an Alexa music skill is safest with. AAC would
# also play; changing it would mean changing the content type the bridge
# reports in the same commit.
OUTPUT_FORMAT = AudioFormat(content_type=ContentType.MP3)
CONTENT_TYPE = "audio/mpeg"


class MediaStreamRoute:
    """Serves `GET /ampere_stream/<ref>.mp3` from any MA music provider."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger) -> None:
        self.mass = mass
        self.logger = logger
        self._unregister = None

    def register(self) -> None:
        self._unregister = self.mass.streams.register_dynamic_route(
            ROUTE_PATTERN, self.handle
        )
        self.logger.debug("serving Music Assistant audio at %s", ROUTE_PATTERN)

    def unregister(self) -> None:
        # Through the handle the webserver returned rather than by path: a
        # provider that is reloaded registers again, and unregistering by path
        # after that would tear down the new route instead of the old one.
        if self._unregister is not None:
            self._unregister()
            self._unregister = None

    def local_url(self, ref: str) -> str:
        """Where this route answers, for logging and for a manual check.

        Not what the bridge is told. The bridge composes its own URL from a
        base it holds in config, so that a published queue can never make it
        fetch something other than Music Assistant.
        """
        return f"{self.mass.streams.base_url.rstrip('/')}{stream_path(ref)}"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        uri = decode_ref(ref_from_path(request.path))
        if not uri:
            raise web.HTTPNotFound(reason="not an Ampere stream reference")

        if rng := request.headers.get("Range"):
            # The number this spike exists to produce. MA's audio is realtime
            # and cannot answer a range, so this is served from the start and
            # whatever Alexa does next is the finding.
            self.logger.warning(
                "range request on an unbuffered Music Assistant stream (%s) "
                "for %s: serving from the beginning",
                rng, uri,
            )

        try:
            media_type, provider_id, item_id = await parse_uri(uri)
        except Exception as err:
            self.logger.warning("cannot parse %s: %s", uri, err)
            raise web.HTTPNotFound(reason="unparseable reference") from err

        provider = self.mass.get_provider(provider_id)
        if provider is None:
            self.logger.warning("provider %s is not loaded, cannot serve %s", provider_id, uri)
            raise web.HTTPNotFound(reason=f"provider {provider_id} unavailable")

        try:
            streamdetails = await provider.get_stream_details(item_id, media_type)
        except Exception as err:
            self.logger.warning("no streamdetails for %s: %s", uri, err)
            raise web.HTTPNotFound(reason="no stream for this item") from err

        # A live stream often reports nothing useful about its own format
        # until ffmpeg has looked at it, and a zero sample rate or bit depth
        # would produce an ffmpeg command line that cannot run. CD quality is
        # the right guess for the intermediate PCM: it is only what the decode
        # is asked to produce, not a claim about the source.
        source = streamdetails.audio_format
        bit_depth = source.bit_depth or 16
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(bit_depth),
            sample_rate=source.sample_rate or 44100,
            bit_depth=bit_depth,
            channels=source.channels or 2,
        )

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": CONTENT_TYPE,
                # Said explicitly rather than left out. A client that assumes
                # ranges are available and gets a 200 anyway behaves worse than
                # one that was told up front they are not.
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)

        self.logger.info("streaming %s from %s", uri, provider_id)
        sent = 0
        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=self.mass.streams.audio.get_media_stream(
                    streamdetails=streamdetails, pcm_format=pcm_format
                ),
                input_format=pcm_format,
                output_format=OUTPUT_FORMAT,
            ):
                await response.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionError):
            # Alexa closing the socket is how a skip or a stop reaches us.
            # Normal, and not worth a traceback.
            self.logger.debug("client went away after %d bytes of %s", sent, uri)
        except Exception:
            self.logger.exception("stream of %s failed after %d bytes", uri, sent)
        else:
            self.logger.debug("finished %s, %d bytes", uri, sent)

        return response
