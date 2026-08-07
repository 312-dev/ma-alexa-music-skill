"""How a Music Assistant item is named on the wire.

Two processes read this module and only one of them can import Music
Assistant, so it is deliberately stdlib-only and these tests run everywhere.
The interesting part is `is_ref`: it is the check that keeps a published queue
from being a way to point the bridge's proxy at something that is not Music
Assistant.
"""

from __future__ import annotations

import pytest

from ma_provider import stream_ref

SPOTIFY = "spotify://track/4uLU6hMCjMI75M1A2tKUQC"


def test_a_uri_survives_the_round_trip():
    assert stream_ref.decode_ref(stream_ref.encode_ref(SPOTIFY)) == SPOTIFY


def test_the_encoding_is_safe_in_a_path_segment():
    """A uri has a scheme and slashes; a path segment cannot."""
    ref = stream_ref.encode_ref(SPOTIFY)
    assert "/" not in ref
    assert ":" not in ref
    assert "=" not in ref  # padding is stripped, and decode puts it back


@pytest.mark.parametrize(
    "uri",
    [
        "tidal://track/12345",
        "ytmusic://track/abc_def-123",
        "filesystem_local://track/Music/Some Album/01 Track.flac",
        "apple_music://track/1440857781",
    ],
)
def test_every_provider_shape_round_trips(uri):
    assert stream_ref.decode_ref(stream_ref.encode_ref(uri)) == uri
    assert stream_ref.is_ref(stream_ref.encode_ref(uri))


def test_garbage_is_not_a_reference():
    """Refused, not guessed at. A ref that does not decode names nothing."""
    for bad in ("", "!!!!", "notbase64$$", "____"):
        assert stream_ref.is_ref(bad) is False


def test_a_url_is_not_a_reference():
    """The whole point of the check.

    If an http URL passed as a reference, anything that could publish a queue
    could make the bridge fetch an arbitrary host. The bridge holds the Music
    Assistant address itself and a reference only ever names an item.
    """
    for url in ("http://169.254.169.254/latest/meta-data/",
                "https://example.com/evil.mp3"):
        assert stream_ref.is_ref(stream_ref.encode_ref(url)) is False


def test_something_without_a_scheme_is_not_a_reference():
    assert stream_ref.is_ref(stream_ref.encode_ref("just-a-track-id")) is False


def test_the_path_a_reference_is_served_at():
    ref = stream_ref.encode_ref(SPOTIFY)
    assert stream_ref.stream_path(ref) == f"/ma_alexa_stream/{ref}.mp3"


def test_the_reference_comes_back_out_of_its_path():
    """The route is a prefix wildcard, so the tail is parsed rather than matched."""
    ref = stream_ref.encode_ref(SPOTIFY)
    assert stream_ref.ref_from_path(stream_ref.stream_path(ref)) == ref
    assert stream_ref.ref_from_path(f"/ma_alexa_stream/{ref}") == ref
    assert stream_ref.ref_from_path(f"/ma_alexa_stream/{ref}.mp3?x=1") == ref


def test_a_path_that_is_not_ours_yields_nothing():
    """The catch-all hands every unmatched request to some handler."""
    assert stream_ref.ref_from_path("/single/abc/def") == ""
    assert stream_ref.ref_from_path("/") == ""


# --- live streams -----------------------------------------------------------
#
# A Music Assistant uri is `provider://mediatype/itemid`, so what kind of thing
# a reference names is already in the reference. Reading it there rather than
# carrying a flag beside it means a queue published yesterday still decides
# correctly today, and nothing that publishes a queue can misdescribe what it
# is publishing.


def test_a_station_is_recognised_as_endless():
    for uri in ("somafm://radio/groovesalad",
                "tunein://radio/s25111",
                "radiobrowser://radio/abc-def"):
        assert stream_ref.is_live(stream_ref.encode_ref(uri)) is True


def test_a_track_is_not_a_station():
    for uri in (SPOTIFY, "deezer://track/3135556",
                "filesystem_local://track/x.flac"):
        assert stream_ref.is_live(stream_ref.encode_ref(uri)) is False


def test_the_media_type_comes_out_of_the_uri():
    assert stream_ref.media_type(stream_ref.encode_ref(SPOTIFY)) == "track"
    assert stream_ref.media_type(
        stream_ref.encode_ref("somafm://radio/groovesalad")) == "radio"


def test_something_that_is_not_a_reference_has_no_media_type():
    assert stream_ref.media_type("!!!!") == ""
    assert stream_ref.is_live("!!!!") is False
