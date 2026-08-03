"""Placing a push event on a player."""

from __future__ import annotations

import time

from ma_provider import push_events, push_router

PREFIX = "ampere--3wyAVB8M"


def _event(**kwargs):
    class E:
        serial = kwargs.get("serial", "")
        content_id = kwargs.get("content_id", "")
        queue_id = kwargs.get("queue_id", "")

    return E()


def test_a_device_event_needs_no_lookup_at_all():
    """The serial Amazon sends is already the player id."""
    router = push_router.PushRouter()

    assert router.resolve(_event(serial="G000000000000001"), PREFIX) == (
        f"{PREFIX}:G000000000000001"
    )


def test_a_group_routes_the_same_way_as_a_speaker():
    """Measured: a group arrives carrying the group id Ampere discovered."""
    router = push_router.PushRouter()
    group = "00000000000000000000000000000001"

    assert router.resolve(_event(serial=group), PREFIX) == f"{PREFIX}:{group}"


def test_now_playing_is_placed_by_the_content_id_we_published():
    """The exact key. Ours, round-tripped by Alexa untouched."""
    router = push_router.PushRouter()
    router.note_publish(f"{PREFIX}:BEDROOM", "ext:0a0f97bb")

    resolved = router.resolve(_event(content_id="ext:0a0f97bb"), PREFIX)

    assert resolved == f"{PREFIX}:BEDROOM"


def test_an_unpublished_queue_falls_back_to_one_learned_from_a_state_event():
    """The gap case: a queue Alexa was already playing before a restart.

    A player state event carries a serial and a queue id together, which is
    what lets a later now-playing event carrying only the queue id be placed.
    """
    router = push_router.PushRouter()
    state = _event(serial="KITCHEN", queue_id="alexa-queue-1")

    router.learn_from(state, router.resolve(state, PREFIX))
    resolved = router.resolve(_event(queue_id="alexa-queue-1"), PREFIX)

    assert resolved == f"{PREFIX}:KITCHEN"


def test_the_exact_key_wins_over_the_learned_one():
    """A published id is a fact; a learned queue is an inference."""
    router = push_router.PushRouter()
    router.note_publish(f"{PREFIX}:RIGHT", "ext:abc")
    router.note_queue(f"{PREFIX}:WRONG", "q1")

    resolved = router.resolve(_event(content_id="ext:abc", queue_id="q1"), PREFIX)

    assert resolved == f"{PREFIX}:RIGHT"


def test_an_event_that_cannot_be_placed_is_dropped_rather_than_guessed():
    """Amazon streams every device on the account, including ones we do not own."""
    router = push_router.PushRouter()

    assert router.resolve(_event(), PREFIX) is None
    assert router.resolve(_event(content_id="ext:never-seen"), PREFIX) is None


def test_a_stale_association_expires_rather_than_misrouting():
    router = push_router.PushRouter()
    router.note_queue(f"{PREFIX}:OLD", "recycled")
    router._by_queue["recycled"] = (f"{PREFIX}:OLD", time.time() - push_router.QUEUE_TTL - 1)

    assert router.resolve(_event(queue_id="recycled"), PREFIX) is None


def test_forgetting_a_player_drops_everything_pointing_at_it():
    router = push_router.PushRouter()
    router.note_publish(f"{PREFIX}:GONE", "ext:1")
    router.note_queue(f"{PREFIX}:GONE", "q")

    router.forget_player(f"{PREFIX}:GONE")

    assert router.resolve(_event(content_id="ext:1"), PREFIX) is None
    assert router.resolve(_event(queue_id="q"), PREFIX) is None


def test_the_tables_cannot_grow_without_bound():
    """One entry per published queue, otherwise never cleaned."""
    router = push_router.PushRouter()

    for index in range(push_router.MAX_ENTRIES + 50):
        router.note_publish(f"{PREFIX}:P", f"ext:{index}")

    assert len(router._by_content) <= push_router.MAX_ENTRIES
    # The newest survive; the oldest are the ones dropped.
    assert router.resolve(
        _event(content_id=f"ext:{push_router.MAX_ENTRIES + 49}"), PREFIX
    ) == f"{PREFIX}:P"


def test_it_works_on_a_real_decoded_event():
    """Guards against the router and the parser drifting apart."""
    import json

    from tests.test_push_events import RAW_VOLUME

    event = push_events.decode(json.loads(RAW_VOLUME))[0]
    router = push_router.PushRouter()

    assert router.resolve(event, PREFIX) == f"{PREFIX}:G000000000000001"
