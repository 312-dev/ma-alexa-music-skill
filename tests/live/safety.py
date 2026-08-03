"""What the live suite is allowed to touch, and what it must never touch.

This is a real apartment. The conformance suite plays audio, skips tracks and
changes volume on physical speakers that other people can hear, and some of the
devices on this Amazon account are not speakers at all: a projector, two
televisions and a car. Driving those is at best startling and at worst turns a
television on in an empty room.

Kept as code rather than a note in a README because a note is advisory and this
is not. `allowed()` is the only way the harness is permitted to obtain a target,
and it fails closed: a device that is not explicitly allowed is refused, so a
newly discovered speaker cannot be swept into a test run by having appeared.

Matched on the Amazon device serial, not the friendly name. Names are edited in
the Alexa app and two of them here differ by one word: "Living Room Echo Studio"
is allowed and "Living Room Echo" is not. A name-based rule would be one rename
away from playing on the wrong device.
"""

from __future__ import annotations

# Serial -> why it is safe. Speakers the user has explicitly cleared.
ALLOWED_SERIALS: dict[str, str] = {
    "G2A1A603050507A4": "Bedroom Echo",
    "G2A1A6031026028E": "Bathroom Echo",
    "G6G22M0625320M40": "Kitchen Echo",
    "G2A0XL12229302V4": "Living Room Echo Studio",
}

# Serial -> why it is excluded. Present so a reader can see the refusal was
# deliberate rather than an omission, and so a future change has to argue with
# a reason rather than an absence.
EXCLUDED_SERIALS: dict[str, str] = {
    "7b2c0191c89540adb404903cc9a98391": "Living Room TV Alexa - a television",
    "eeb2eea6b4174cf19db1acab6c59bb62": "Home Theater - a television",
    "f1648a9004eb4e70a18bcc636f184dd5": "Samsung Smart Projector - a projector",
    "c1bbdd06939141b9b5788213c6c80a03": "BMW Alexa Car Integration - a car",
    "G6G22J0633840B05": "Living Room Echo - excluded; the Studio is the allowed one",
}


class Unsafe(Exception):
    """Raised rather than skipping, so a refusal cannot pass unnoticed."""


def serial_of(player_id: str) -> str:
    """The Amazon serial out of an Ampere player id (`<instance>:<serial>`)."""
    return player_id.rsplit(":", 1)[-1] if ":" in player_id else player_id


def is_allowed(player_id: str) -> bool:
    return serial_of(player_id) in ALLOWED_SERIALS


def check(player_id: str) -> str:
    """Return the player id, or refuse to hand it back.

    Raises instead of returning False so that a caller which forgets to check
    the result still cannot drive an excluded device. The failure mode this
    guards against is a test that silently does nothing, which looks like a
    pass.
    """
    serial = serial_of(player_id)
    if serial in ALLOWED_SERIALS:
        return player_id
    reason = EXCLUDED_SERIALS.get(serial, "not on the allow list")
    raise Unsafe(f"refusing to drive {player_id}: {reason}")


def group_is_safe(member_ids: list[str]) -> tuple[bool, list[str]]:
    """Whether a speaker group can be driven, and which members block it.

    A group is only as safe as its worst member: telling "Whole Apartment" to
    play reaches every device in it, so one excluded television is enough to
    make the whole group off limits.
    """
    unsafe = [m for m in member_ids if not is_allowed(m)]
    return (not unsafe), unsafe
