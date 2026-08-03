"""Deciding which player a push event is about.

Harder than it looks, because the two event families identify themselves in
completely different ways and only one of them names a device.

`PUSH_*` events carry `dopplerId.deviceSerialNumber`, and that value is already
a player id here: measured 2026-08-03, a speaker group arrives with the same
group id Ampere discovered it under, so nothing needs translating.

`NotifyNowPlayingUpdated` names no device at all. It describes a *session*. It
is also by far the most useful event on the stream, because it is the only one
carrying track progress, so it cannot simply be ignored. What it does carry is
`mediaReference.value.contentId` -- the `ext:<token>` Ampere itself published
and handed to Alexa, which Alexa echoes back untouched.

So routing works forwards, from something we issued, rather than backwards from
something we have to recognise. That is worth stating because every other
correlation in this provider matches on a *track title*, which is all the
polled API offers and which breaks on duplicate names, on renames, and on
Alexa reporting a title differently from Music Assistant. This path has none of
those failure modes: the id is ours, it round-trips exactly, and a match is a
fact rather than a guess.

There is a second, weaker key for the case where a queue was not published by
this process -- a queue Alexa was already playing before a restart. A player
state event carries both a serial and a `mediaReferenceId` prefixed with
Alexa's own queue id, so seeing one teaches us that queue's owner, and a
now-playing event for the same queue can then be placed. Learned, not assumed,
and it only ever fills a gap the exact key could not.
"""

from __future__ import annotations

import time
from typing import Any

# How long a learned queue-to-player association is trusted. Long enough to
# outlive an ordinary listening session, short enough that a stale entry cannot
# misroute a queue id Amazon has recycled onto a different speaker.
QUEUE_TTL = 6 * 3600.0

# A published content id is remembered for longer, because it is exact: Alexa
# can hold one for hours and echo it back on a resume long after the publish.
CONTENT_TTL = 24 * 3600.0

MAX_ENTRIES = 256


class PushRouter:
    """A map from what an event says about itself to a player id."""

    def __init__(self) -> None:
        self._by_content: dict[str, tuple[str, float]] = {}
        self._by_queue: dict[str, tuple[str, float]] = {}

    # -- learning ------------------------------------------------------------

    def note_publish(self, player_id: str, content_id: str) -> None:
        """Record that this player published this queue.

        Called at play_media, which is the moment the association is created
        and the only moment it is known for certain.
        """
        if not (player_id and content_id):
            return
        self._by_content[content_id] = (player_id, time.time())
        self._prune(self._by_content)

    def note_queue(self, player_id: str, queue_id: str) -> None:
        """Learn Alexa's own queue id for a player, from an event that has both."""
        if not (player_id and queue_id):
            return
        self._by_queue[queue_id] = (player_id, time.time())
        self._prune(self._by_queue)

    def forget_player(self, player_id: str) -> None:
        for table in (self._by_content, self._by_queue):
            for key in [k for k, (pid, _) in table.items() if pid == player_id]:
                del table[key]

    # -- resolving -----------------------------------------------------------

    def resolve(self, event: Any, prefix: str) -> str | None:
        """The player id for this event, or None if it cannot be placed.

        None is a normal outcome, not a failure. Amazon streams events for
        every device on the account, including ones Ampere does not expose and
        sessions started by other providers entirely, and acting on those would
        be worse than dropping them.
        """
        if serial := getattr(event, "serial", ""):
            return f"{prefix}:{serial}"

        if content_id := getattr(event, "content_id", ""):
            if found := self._live(self._by_content, content_id, CONTENT_TTL):
                return found

        if queue_id := getattr(event, "queue_id", ""):
            if found := self._live(self._by_queue, queue_id, QUEUE_TTL):
                return found

        return None

    def learn_from(self, event: Any, player_id: str) -> None:
        """Take whatever identity an event offers, for placing later ones.

        A player state event names a device *and* a queue, so it is the bridge
        that lets a now-playing event with neither a serial nor a recognised
        content id still be attributed.
        """
        if queue_id := getattr(event, "queue_id", ""):
            self.note_queue(player_id, queue_id)

    # -- internals -----------------------------------------------------------

    def _live(self, table: dict[str, tuple[str, float]], key: str,
              ttl: float) -> str | None:
        entry = table.get(key)
        if entry is None:
            return None
        player_id, at = entry
        if time.time() - at > ttl:
            del table[key]
            return None
        return player_id

    def _prune(self, table: dict[str, tuple[str, float]]) -> None:
        """Keep the tables bounded.

        These grow one entry per published queue and are never otherwise
        cleaned, so on a long-running server they would be a slow leak. Oldest
        out first.
        """
        if len(table) <= MAX_ENTRIES:
            return
        for key, _ in sorted(table.items(), key=lambda kv: kv[1][1])[
            : len(table) - MAX_ENTRIES
        ]:
            del table[key]
