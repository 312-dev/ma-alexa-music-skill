"""Decoding real push directives.

The sample in `RAW_VOLUME` is a live directive from 2026-08-03 with the
account and device identifiers replaced and nothing else touched -- the
backslashes, nesting and field order are exactly as Amazon sent them. That
matters more than it looks: the payload is JSON
encoded inside a string inside JSON encoded inside a string, and a parser
written against a hand-tidied example decodes the outer layer, returns
something that passes a shallow assertion, and reads nothing. Keeping one
untouched sample is what stops that from being discovered in production.

The rest are built with json.dumps, which produces byte-identical encoding and
is readable enough to check by eye.
"""

from __future__ import annotations

import json

from ma_provider import push_events


# Captured verbatim. Do not reformat.
RAW_VOLUME = (
    '{"directive":{"header":{"namespace":"Alexa.Mobile.Push","name":"RenderUpdate",'
    '"messageId":"05317452-19e6-4c14-a19e-83f0fc49df05"},"payload":{"renderingUpdates":'
    '[{"route":"EventBus:tcomm::message","resourceId":"PUSH_VOLUME_CHANGE",'
    '"resourceMetadata":"{\\"command\\":\\"PUSH_VOLUME_CHANGE\\",\\"payload\\":'
    '\\"{\\\\\\"dopplerId\\\\\\":{\\\\\\"deviceSerialNumber\\\\\\":\\\\\\"G000000000000001\\\\\\",'
    '\\\\\\"deviceType\\\\\\":\\\\\\"A3RMGO6LYLH7YN\\\\\\"},\\\\\\"volumeSetting\\\\\\":31,'
    '\\\\\\"isMuted\\\\\\":false,\\\\\\"destinationUserId\\\\\\":\\\\\\"A0000000000000\\\\\\"}\\",'
    '\\"timeStamp\\":1785767868697}"}]}}}'
)


def _wrap(resource_id: str, payload: dict, stamp: int = 1785767868697) -> dict:
    """Build a directive with the same triple encoding Amazon uses."""
    metadata = json.dumps({
        "command": resource_id,
        "payload": json.dumps(payload),
        "timeStamp": stamp,
    })
    return {
        "directive": {
            "header": {"namespace": "Alexa.Mobile.Push", "name": "RenderUpdate"},
            "payload": {
                "renderingUpdates": [
                    {
                        "route": "EventBus:tcomm::message",
                        "resourceId": resource_id,
                        "resourceMetadata": metadata,
                    }
                ]
            },
        }
    }


def _now_playing(**overrides) -> dict:
    data = {
        "progress": {"mediaProgress": 83306, "mediaLength": 232000},
        "playerState": "PLAYING",
        "queueId": "7f7199ae-6fa9-4bda-9948-4599af232b7d",
        "provider": {"providerName": "Music Assistant"},
        "infoText": {"title": "Dreamweaver", "subText1": "Daedric"},
        "mediaReference": {
            "namespace": "Alexa.Audio.PlayQueue",
            "name": "item",
            "value": json.dumps({
                "contentId": "ext:0a0f97bbc1cbeef684530c3a",
                "queueId": "87cf2078-53fe-418c-a19e-762f68a816d4",
                "id": "0",
            }),
        },
    }
    data.update(overrides)
    return _wrap(push_events.NOW_PLAYING, {
        "messageId": "4c5605ba",
        "update": {
            "type": "SessionUpdate",
            "taskSessionId": "amzn1.echo-api.session.e469860f",
            "update": {
                "type": "NotifyNowPlayingUpdated",
                "cause": "PLAYBACK_STARTED",
                "nowPlayingData": data,
            },
        },
        "name": "NotifyNowPlayingUpdated",
    })


# -- the wire format --------------------------------------------------------


def test_the_real_wire_format_decodes():
    """The whole point. An untouched directive off the stream."""
    events = push_events.decode(json.loads(RAW_VOLUME))

    assert len(events) == 1
    event = events[0]
    assert event.command == push_events.VOLUME_CHANGE
    assert event.serial == "G000000000000001"
    assert event.payload["volumeSetting"] == 31
    assert event.payload["isMuted"] is False


def test_amazons_own_clock_is_read_in_seconds():
    """Delivery latency is only measurable against Amazon's stamp, not ours."""
    event = push_events.decode(json.loads(RAW_VOLUME))[0]

    # 1785767868697 ms. Seconds, because everything else in this codebase is.
    assert event.at == 1785767868.697


def test_a_payload_that_is_a_string_and_one_that_is_not_agree():
    """The readable test form and the wire form must not diverge.

    Every other test here builds payloads with json.dumps. If the parser only
    handled pre-parsed dicts, those tests would pass against a parser that
    cannot read Amazon at all.
    """
    payload = {"dopplerId": {"deviceSerialNumber": "ABC"}, "volumeSetting": 7}
    as_string = _wrap(push_events.VOLUME_CHANGE, payload)

    raw = as_string["directive"]["payload"]["renderingUpdates"][0]
    as_dict = json.loads(raw["resourceMetadata"])
    as_dict["payload"] = payload  # already parsed
    unwrapped = json.loads(json.dumps(as_string))
    unwrapped["directive"]["payload"]["renderingUpdates"][0]["resourceMetadata"] = as_dict

    assert push_events.decode(as_string)[0].payload == payload
    assert push_events.decode(unwrapped)[0].payload == payload


# -- routing ----------------------------------------------------------------


def test_a_group_routes_by_the_id_ma_alexa_already_uses():
    """Measured: a group appears here exactly as a speaker does."""
    directive = _wrap(push_events.AUDIO_PLAYER_STATE, {
        "dopplerId": {
            "deviceSerialNumber": "00000000000000000000000000000001",
            "deviceType": "A3C9PE6TNYLTCH",
        },
        "audioPlayerState": "PLAYING",
        "mediaReferenceId": "4c74b3e5-67fb-4d90-82e0-5d39c626a4f3:1",
    })

    event = push_events.decode(directive)[0]

    assert event.serial == "00000000000000000000000000000001"
    # The queue id is the prefix, not the whole reference.
    assert event.queue_id == "4c74b3e5-67fb-4d90-82e0-5d39c626a4f3"


def test_now_playing_carries_no_serial_but_carries_our_content_id():
    """The reason there are two routing keys rather than one.

    A now-playing event describes a session and never names a device. What it
    does carry is the contentId we handed Alexa, which is exact.
    """
    event = push_events.decode(_now_playing())[0]

    assert event.serial == ""
    assert event.content_id == "ext:0a0f97bbc1cbeef684530c3a"
    assert event.queue_id == "7f7199ae-6fa9-4bda-9948-4599af232b7d"


def test_the_content_id_survives_a_fourth_layer_of_encoding():
    """mediaReference.value is JSON in a string inside the payload."""
    event = push_events.decode(_now_playing())[0]

    assert event.content_id.startswith("ext:")


# -- the payload we act on --------------------------------------------------


def test_progress_and_state_are_reachable():
    event = push_events.decode(_now_playing())[0]

    assert event.cause == "PLAYBACK_STARTED"
    assert event.now_playing["playerState"] == "PLAYING"
    assert event.now_playing["progress"]["mediaProgress"] == 83306
    assert event.now_playing["infoText"]["title"] == "Dreamweaver"


def test_a_batched_directive_does_not_lose_everything_after_the_first():
    directive = _wrap(push_events.VOLUME_CHANGE, {"volumeSetting": 1})
    second = _wrap(push_events.AUDIO_PLAYER_STATE, {"audioPlayerState": "PLAYING"})
    directive["directive"]["payload"]["renderingUpdates"].extend(
        second["directive"]["payload"]["renderingUpdates"]
    )

    events = push_events.decode(directive)

    assert [e.command for e in events] == [
        push_events.VOLUME_CHANGE, push_events.AUDIO_PLAYER_STATE
    ]


def test_an_unknown_command_is_passed_through_rather_than_dropped():
    """The command list is an observation, not a contract."""
    directive = _wrap("PUSH_SOMETHING_NEW", {"dopplerId": {"deviceSerialNumber": "X"}})

    events = push_events.decode(directive)

    assert len(events) == 1
    assert events[0].command == "PUSH_SOMETHING_NEW"
    assert events[0].serial == "X"


# -- malformed input --------------------------------------------------------


def test_nothing_here_raises():
    """A stream taken down by one bad message is worse than a missed update."""
    for junk in (
        None, "", 0, [], {}, {"directive": None}, {"directive": {}},
        {"directive": {"payload": None}},
        {"directive": {"payload": {"renderingUpdates": None}}},
        {"directive": {"payload": {"renderingUpdates": ["not a dict"]}}},
        {"directive": {"payload": {"renderingUpdates": [{"resourceMetadata": "{["}]}}},
        {"directive": {"payload": {"renderingUpdates": [{"resourceId": "X"}]}}},
    ):
        assert push_events.decode(junk) == [] or isinstance(
            push_events.decode(junk), list
        )


def test_an_unreadable_inner_payload_still_yields_a_named_event():
    """Knowing something happened is worth more than nothing at all."""
    directive = _wrap(push_events.VOLUME_CHANGE, {})
    raw = directive["directive"]["payload"]["renderingUpdates"][0]
    metadata = json.loads(raw["resourceMetadata"])
    metadata["payload"] = "{not json"
    raw["resourceMetadata"] = json.dumps(metadata)

    events = push_events.decode(directive)

    assert len(events) == 1
    assert events[0].command == push_events.VOLUME_CHANGE
    assert events[0].payload == {}
    assert events[0].serial == ""


def test_a_directive_with_no_command_name_is_dropped():
    directive = _wrap(push_events.VOLUME_CHANGE, {})
    raw = directive["directive"]["payload"]["renderingUpdates"][0]
    raw["resourceId"] = ""
    metadata = json.loads(raw["resourceMetadata"])
    metadata["command"] = ""
    raw["resourceMetadata"] = json.dumps(metadata)

    assert push_events.decode(directive) == []
