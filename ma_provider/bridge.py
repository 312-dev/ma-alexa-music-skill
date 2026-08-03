"""Talking to the Ampere bridge.

One call: hand it an ordered list of Subsonic song ids and get back a contentId
that Alexa can be told to play. See queue_api.py in the same repo for why an
out-of-band queue has to exist at all.

The HTTP session is injected rather than created. Music Assistant already owns
an aiohttp session (`mass.http_session`) and providers are expected to use it;
building a second one leaks connections on reload and gives tests nothing to
hold onto. Anything with the aiohttp `post`/`get` shape works here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class BridgeError(RuntimeError):
    """The bridge refused or could not be reached."""


class BridgeClient:
    def __init__(
        self,
        base_url: str,
        admin_token: str,
        session,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.admin_token = admin_token or ""
        self.session = session
        self.timeout = timeout

    # -- request shaping, kept separate so it can be asserted on -------------

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.admin_token, "Content-Type": "application/json"}

    def publish_request(self, tracks: list, name: str = "",
                        start_offset_ms: int = 0) -> tuple[str, dict]:
        """URL and body for a queue publish.

        A track is either a Subsonic song id as a plain string, or a dict
        describing a track the bridge should fetch back out of Music Assistant.
        Strings are coerced so a caller holding ids as something else still
        publishes; dicts are passed through untouched, because the bridge
        validates their shape and should see exactly what was sent.
        """
        return (
            f"{self.base_url}/queue",
            {
                "tracks": [t if isinstance(t, dict) else str(t) for t in tracks],
                "name": name or "",
                "start_offset_ms": max(0, int(start_offset_ms or 0)),
            },
        )

    # -- calls ---------------------------------------------------------------

    async def publish_queue(self, tracks: list, name: str = "",
                            start_offset_ms: int = 0) -> str:
        """Publish a track list, returning its contentId.

        Raises rather than returning a sentinel: a caller that goes on to speak
        an utterance for a queue that was never stored gets a speaker that says
        it cannot find anything, which is much harder to trace back than a
        stack trace at the point of failure.
        """
        if not tracks:
            raise BridgeError("refusing to publish an empty queue")

        url, body = self.publish_request(tracks, name, start_offset_ms)
        async with self.session.post(
            url, json=body, headers=self.headers, timeout=self.timeout
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise BridgeError(f"publish failed: HTTP {resp.status} {text[:200]}")
            payload = await resp.json()

        content_id = payload.get("content_id")
        if not content_id:
            raise BridgeError(f"publish returned no content_id: {payload!r}")

        # Tracks the bridge could not find in the library are dropped there
        # rather than failing the publish, so the count is worth surfacing:
        # a queue that plays four of its twelve tracks otherwise looks like an
        # Alexa bug.
        requested = payload.get("requested", len(tracks))
        count = payload.get("count", requested)
        if count < requested:
            logger.warning(
                "bridge stored %s of %s tracks; the rest are not in the library",
                count, requested,
            )
        return content_id
