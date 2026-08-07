"""The allow list is the one part of the live suite that must never be wrong.

These run offline with the rest of the unit tests, deliberately. Everything
else under `tests/live/` needs a real Music Assistant and real speakers; this
does not, because the question it answers -- can the suite reach a device it
was told to leave alone -- has to be answerable in CI, before anything is run
against an apartment.
"""

from __future__ import annotations

import pytest

from tests.live import safety


def test_a_cleared_speaker_is_allowed():
    bedroom = "ma-alexa--3wyAVB8M:G2A1A603050507A4"

    assert safety.is_allowed(bedroom)
    assert safety.check(bedroom) == bedroom


@pytest.mark.parametrize("serial", sorted(safety.EXCLUDED_SERIALS))
def test_every_excluded_device_is_refused(serial):
    with pytest.raises(safety.Unsafe):
        safety.check(f"ma-alexa--3wyAVB8M:{serial}")


def test_an_unknown_device_is_refused_rather_than_allowed():
    """Fails closed. A speaker that simply appeared must not be swept in."""
    with pytest.raises(safety.Unsafe):
        safety.check("ma-alexa--3wyAVB8M:BRAND_NEW_DEVICE")


def test_refusal_raises_rather_than_returning_false():
    """A test that silently does nothing looks exactly like a passing one."""
    with pytest.raises(safety.Unsafe) as caught:
        safety.check("ma-alexa--3wyAVB8M:eeb2eea6b4174cf19db1acab6c59bb62")

    # And says why, because the next person to read it will be wondering
    # whether the exclusion was deliberate.
    assert "television" in str(caught.value)


def test_the_two_living_room_devices_are_told_apart():
    """The whole reason this matches on serial and not on name.

    "Living Room Echo Studio" is cleared and "Living Room Echo" is not. They
    differ by one word, and the names are editable in the Alexa app.
    """
    studio = "ma-alexa--3wyAVB8M:G2A0XL12229302V4"
    plain = "ma-alexa--3wyAVB8M:G6G22J0633840B05"

    assert safety.is_allowed(studio)
    assert not safety.is_allowed(plain)


def test_no_serial_is_both_allowed_and_excluded():
    assert not (set(safety.ALLOWED_SERIALS) & set(safety.EXCLUDED_SERIALS))


def test_a_group_is_only_as_safe_as_its_worst_member():
    """One excluded television makes the whole group off limits."""
    ok, blockers = safety.group_is_safe([
        "ma-alexa--3wyAVB8M:G2A1A603050507A4",
        "ma-alexa--3wyAVB8M:eeb2eea6b4174cf19db1acab6c59bb62",
    ])

    assert ok is False
    assert blockers == ["ma-alexa--3wyAVB8M:eeb2eea6b4174cf19db1acab6c59bb62"]


def test_a_group_of_cleared_speakers_is_safe():
    ok, blockers = safety.group_is_safe([
        "ma-alexa--3wyAVB8M:G2A1A603050507A4",
        "ma-alexa--3wyAVB8M:G6G22M0625320M40",
    ])

    assert ok is True
    assert blockers == []


def test_a_bare_serial_works_as_well_as_a_player_id():
    """Callers hold both forms; a mismatch here would fail open."""
    assert safety.is_allowed("G2A1A603050507A4")
    assert safety.serial_of("ma-alexa--x:G2A1A603050507A4") == "G2A1A603050507A4"
