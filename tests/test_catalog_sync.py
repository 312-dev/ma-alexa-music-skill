"""Catalog diffing.

The two failure modes worth guarding: stamping everything with a fresh
timestamp (Amazon reprocesses the whole catalog every run) and silently
dropping removed entities (they linger in Alexa's entity resolution and it
keeps offering tracks that no longer exist).
"""

from __future__ import annotations

import catalog_sync


def entity(eid: str, name: str) -> dict:
    return catalog_sync.base(eid, name)


def test_new_entities_get_current_timestamp():
    final, state = catalog_sync.apply_timestamps("artists", [entity("artist.1", "A")], {})
    assert final[0]["lastUpdatedTime"] == catalog_sync.NOW
    assert "artist.1" in state


def test_unchanged_entities_keep_their_timestamp():
    """Bumping unchanged rows makes Amazon reprocess the whole catalog."""
    first, state = catalog_sync.apply_timestamps("artists", [entity("artist.1", "A")], {})
    original = first[0]["lastUpdatedTime"]

    second, _ = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A")], {"artists": state}
    )
    assert second[0]["lastUpdatedTime"] == original


def test_changed_entities_are_restamped():
    """Amazon ignores edits whose lastUpdatedTime did not move."""
    _first, state = catalog_sync.apply_timestamps("artists", [entity("artist.1", "A")], {})
    state["artist.1"]["ts"] = "2020-01-01T00:00:00.000Z"

    second, _ = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "Renamed")], {"artists": state}
    )
    assert second[0]["lastUpdatedTime"] == catalog_sync.NOW


def test_removed_entities_become_tombstones():
    """Omitting a deleted entity leaves it live in Alexa forever."""
    _first, state = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A"), entity("artist.2", "B")], {}
    )
    second, current = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A")], {"artists": state}
    )

    tombs = [e for e in second if e.get("deleted")]
    assert len(tombs) == 1
    assert tombs[0]["id"] == "artist.2"
    assert set(tombs[0]) == {"id", "lastUpdatedTime", "deleted"}
    assert "artist.2" not in current


def test_tombstones_are_not_re_emitted_forever():
    _first, state = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A"), entity("artist.2", "B")], {}
    )
    _second, after = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A")], {"artists": state}
    )
    third, _ = catalog_sync.apply_timestamps(
        "artists", [entity("artist.1", "A")], {"artists": after}
    )
    assert [e for e in third if e.get("deleted")] == []


def test_fingerprint_ignores_timestamp():
    a = entity("artist.1", "A") | {"lastUpdatedTime": "2020-01-01T00:00:00.000Z"}
    b = entity("artist.1", "A") | {"lastUpdatedTime": "2026-01-01T00:00:00.000Z"}
    assert catalog_sync.fingerprint(a) == catalog_sync.fingerprint(b)


def test_fingerprint_detects_a_rename():
    assert catalog_sync.fingerprint(entity("artist.1", "A")) != \
           catalog_sync.fingerprint(entity("artist.1", "B"))


def test_kinds_do_not_share_state():
    _f, artists = catalog_sync.apply_timestamps("artists", [entity("artist.1", "A")], {})
    tracks, _ = catalog_sync.apply_timestamps(
        "tracks", [entity("track.1", "T")], {"artists": artists}
    )
    assert [e for e in tracks if e.get("deleted")] == []
