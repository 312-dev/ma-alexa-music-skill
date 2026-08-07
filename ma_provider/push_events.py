"""Decoding Amazon's push directives into something worth acting on.

Amazon delivers device state over a long-lived HTTP/2 stream instead of making
us ask for it. `alexapy` transports those directives and stops there: what
arrives at a callback is the raw envelope, undocumented, and this module is the
whole of our understanding of it. Everything here was read off a live stream on
2026-08-03 rather than taken from a specification, because there is no
specification.

The envelope is the same for every event, which is not obvious from looking at
one:

    {"directive": {"header": {"namespace": "Alexa.Mobile.Push",
                              "name": "RenderUpdate", ...},
                   "payload": {"renderingUpdates": [
                       {"route": "EventBus:tcomm::message",
                        "resourceId": "PUSH_VOLUME_CHANGE",
                        "resourceMetadata": "<JSON string>"}]}}}

`resourceMetadata` is JSON encoded inside a string, and the `payload` inside
*that* is JSON encoded inside a string again. Three levels, two of them
stringly typed. A parser written against a pretty-printed example will look
correct and decode nothing.

`NotifyNowPlayingUpdated` arrives through the identical path, which is worth
saying because the reference implementation logs it so differently that it
reads like a second protocol. It is not. One parser handles all of it.

Nothing here raises. A directive we cannot read is a directive we ignore: this
is a supplementary source of truth sitting next to a poll that still works, and
taking the stream down over one malformed message would trade a missing update
for a missing everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Commands seen on a live stream. Anything else is passed through with its name
# intact rather than dropped, because this list is an observation and not a
# contract, and a command we have not seen yet is not a command we should hide.
VOLUME_CHANGE = "PUSH_VOLUME_CHANGE"
AUDIO_PLAYER_STATE = "PUSH_AUDIO_PLAYER_STATE"
MEDIA_QUEUE_CHANGE = "PUSH_MEDIA_QUEUE_CHANGE"
MEDIA_CHANGE = "PUSH_MEDIA_CHANGE"
MEDIA_PROGRESS = "PUSH_MEDIA_PROGRESS_CHANGE"
NOW_PLAYING = "NotifyNowPlayingUpdated"

# The commands this provider currently acts on. Used to keep debug logging of
# the rest quiet without discarding them at the parse layer.
ACTED_ON = frozenset({VOLUME_CHANGE, AUDIO_PLAYER_STATE, NOW_PLAYING})


@dataclass(frozen=True)
class PushEvent:
    """One decoded directive.

    `payload` is the innermost object, already unwrapped from both layers of
    string encoding. `at` is Amazon's own clock in seconds, not ours: it is the
    only way to tell a directive describing something that just happened from
    one replayed after a reconnect, and measured against it the delivery
    latency of this channel is under a tenth of a second.
    """

    command: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    # -- routing keys --------------------------------------------------------
    #
    # Two disjoint families, and the difference decides how an event finds its
    # player. The PUSH_* family names a device. NotifyNowPlayingUpdated does
    # not name one at all: it describes a *session*, and the only identity in
    # it is content we published. Hence two properties rather than one, and a
    # caller that tries both.

    @property
    def serial(self) -> str:
        """The device or group this is about, if it says.

        Groups appear here exactly like speakers do, carrying the group id
        Music Assistant already uses as a player id, so nothing has to be translated.
        """
        doppler = self.payload.get("dopplerId")
        if isinstance(doppler, dict):
            return str(doppler.get("deviceSerialNumber") or "")
        return ""

    @property
    def now_playing(self) -> dict[str, Any]:
        """The `nowPlayingData` block, dug out of its two wrapper levels."""
        update = self.payload.get("update")
        if not isinstance(update, dict):
            return {}
        inner = update.get("update")
        if not isinstance(inner, dict):
            return {}
        data = inner.get("nowPlayingData")
        return data if isinstance(data, dict) else {}

    @property
    def cause(self) -> str:
        """Why the now-playing state changed: PLAYBACK_STARTED, STOPPED, ..."""
        update = self.payload.get("update")
        if not isinstance(update, dict):
            return ""
        inner = update.get("update")
        if not isinstance(inner, dict):
            return ""
        return str(inner.get("cause") or "")

    @property
    def content_id(self) -> str:
        """The contentId Music Assistant published for the queue that is playing.

        This is the routing key for now-playing events and it is exact. Alexa
        echoes back the `ext:<token>` handed to it at Initiate, so an event can
        be attributed to the player that published that token without matching
        on a track title, which is what every other correlation in this
        provider has had to fall back on.

        Nested inside yet another JSON string, one level deeper than the rest
        of the payload.
        """
        reference = self.now_playing.get("mediaReference")
        if not isinstance(reference, dict):
            return ""
        raw = reference.get("value")
        if not isinstance(raw, str):
            return ""
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        if not isinstance(decoded, dict):
            return ""
        return str(decoded.get("contentId") or "")

    @property
    def queue_id(self) -> str:
        """Alexa's own id for the queue.

        Not ours. It appears bare on a now-playing event and as the prefix of
        `mediaReferenceId` on a player-state event (`<queueId>:<index>`), which
        is what lets a device serial learned from one be carried to the other.
        """
        if queue := self.now_playing.get("queueId"):
            return str(queue)
        reference = self.payload.get("mediaReferenceId")
        if isinstance(reference, str) and ":" in reference:
            return reference.rsplit(":", 1)[0]
        return str(reference or "")


def decode(directive: Any) -> list[PushEvent]:
    """Every event carried by one raw directive.

    A list because `renderingUpdates` is one, and treating it as a single
    object would silently drop everything after the first on any directive that
    batches. Returns empty for anything unrecognisable.
    """
    if not isinstance(directive, dict):
        return []
    payload = (directive.get("directive") or {})
    if not isinstance(payload, dict):
        return []
    body = payload.get("payload")
    if not isinstance(body, dict):
        return []
    updates = body.get("renderingUpdates")
    if not isinstance(updates, list):
        return []

    events: list[PushEvent] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        if (event := _decode_update(update)) is not None:
            events.append(event)
    return events


def _decode_update(update: dict[str, Any]) -> PushEvent | None:
    metadata = _loads(update.get("resourceMetadata"))
    if not isinstance(metadata, dict):
        return None

    # resourceId and the command inside the metadata have always agreed on a
    # live stream. resourceId is preferred anyway: it is the outer, structural
    # field, and an event whose two names disagreed would be one we understand
    # less well than we think, not one to guess about.
    command = str(update.get("resourceId") or metadata.get("command") or "")
    if not command:
        return None

    inner = _loads(metadata.get("payload"))
    if not isinstance(inner, dict):
        inner = {}

    # Milliseconds, and Amazon's clock rather than ours.
    stamp = metadata.get("timeStamp")
    at = float(stamp) / 1000.0 if isinstance(stamp, (int, float)) else 0.0

    return PushEvent(command=command, payload=inner, at=at)


def _loads(value: Any) -> Any:
    """Parse a field that is JSON inside a string, tolerating both forms.

    Amazon sends these as strings. Accepting an already-parsed dict as well
    costs nothing and means a test can be written against a readable literal
    instead of a wall of backslashes, which is the difference between a test
    anyone can check and one nobody rereads.
    """
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
