"""Catalog diffing, and the enablement cycle that has to follow an upload.

The two diffing failure modes worth guarding: stamping everything with a fresh
timestamp (Amazon reprocesses the whole catalog every run) and silently
dropping removed entities (they linger in Alexa's entity resolution and it
keeps offering tracks that no longer exist).

The third failure mode is the enablement cycle, and it is the nastiest of the
three because there is no signal at all: the upload reports SUCCEEDED, the
service answers every directive correctly, and Alexa plays the request from
somebody else's catalog. These tests are what stop the cycle being deleted as
superstition.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from ma_provider import catalog_sync
from ma_provider import handoff
from ma_provider import subsonic


# The autouse fixture in conftest replaces subsonic.song with a dict lookup so
# that no app test touches the network. Grab the real one at import time, which
# is before any fixture has run, so the tests below can exercise it.
_REAL_SONG = subsonic.song


def entity(eid: str, name: str) -> dict:
    return catalog_sync.base(eid, name)


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ask"], returncode=returncode, stdout=stdout, stderr=stderr
    )


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


# --- upload verdict ---------------------------------------------------------


@pytest.fixture
def out_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_sync, "OUT_DIR", tmp_path)
    return tmp_path


def test_nonzero_exit_is_a_failure_whatever_the_output_says(monkeypatch, out_dir):
    monkeypatch.setattr(
        catalog_sync.subprocess, "run",
        lambda *a, **k: completed(1, stdout="uploaded successfully"),
    )
    assert catalog_sync.upload("artists", "cat-1", [entity("artist.1", "A")]) is False


def test_zero_exit_without_the_magic_word_still_counts(monkeypatch, out_dir):
    """A reworded CLI must not stall the state file and re-upload forever."""
    monkeypatch.setattr(
        catalog_sync.subprocess, "run", lambda *a, **k: completed(0, stdout="Done."),
    )
    assert catalog_sync.upload("artists", "cat-1", [entity("artist.1", "A")]) is True


# --- enablement cycle -------------------------------------------------------


@pytest.fixture
def ask(monkeypatch):
    """Record every `ask smapi` invocation the cycle makes."""
    calls: list[tuple[str, str, str]] = []
    codes: dict[str, int] = {}

    def fake_ask(verb, skill_id, stage):
        calls.append((verb, skill_id, stage))
        return completed(codes.get(verb, 0), stderr="boom" if codes.get(verb) else "")

    monkeypatch.setattr(catalog_sync, "_ask", fake_ask)
    monkeypatch.delenv("CATALOG_NO_CYCLE", raising=False)
    monkeypatch.setenv("SKILL_ID", "amzn1.ask.skill.test")
    monkeypatch.delenv("SKILL_STAGE", raising=False)
    return types.SimpleNamespace(calls=calls, codes=codes)


def test_cycle_deletes_then_sets(ask):
    assert catalog_sync.cycle_enablement() is True
    assert [c[0] for c in ask.calls] == [
        "delete-skill-enablement", "set-skill-enablement",
    ]
    assert all(c[2] == "development" for c in ask.calls)


def test_cycle_honours_a_custom_stage(monkeypatch, ask):
    monkeypatch.setenv("SKILL_STAGE", "live")
    catalog_sync.cycle_enablement()
    assert all(c[2] == "live" for c in ask.calls)


def test_delete_failing_is_tolerated(ask):
    """Not enabled in the first place is the normal state after a failed run."""
    ask.codes["delete-skill-enablement"] = 1
    assert catalog_sync.cycle_enablement() is True
    assert [c[0] for c in ask.calls][-1] == "set-skill-enablement"


def test_set_failing_is_an_error(ask):
    ask.codes["set-skill-enablement"] = 1
    assert catalog_sync.cycle_enablement() is False


def test_missing_skill_id_warns_loudly_but_does_not_fail(monkeypatch, ask, capsys):
    monkeypatch.delenv("SKILL_ID", raising=False)
    assert catalog_sync.cycle_enablement() is True
    assert ask.calls == []
    out = capsys.readouterr().out
    assert "SKILL_ID is not set" in out
    assert "delete-skill-enablement" in out
    assert "set-skill-enablement" in out


def test_no_cycle_flag_skips_it(ask):
    assert catalog_sync.cycle_enablement(no_cycle=True) is True
    assert ask.calls == []


# --- main() wiring ----------------------------------------------------------


@pytest.fixture
def sync_run(monkeypatch, ask):
    """A whole sync run with the network and the state file taken out."""
    monkeypatch.setattr(catalog_sync, "CATALOGS", {"artists": "cat-1"})
    monkeypatch.setattr(
        catalog_sync, "collect", lambda: {"artists": [entity("artist.1", "A")]}
    )
    monkeypatch.setattr(catalog_sync, "load_state", lambda: {})
    monkeypatch.setattr(catalog_sync, "save_state", lambda state: None)
    uploads = {"ok": True}
    monkeypatch.setattr(
        catalog_sync, "upload", lambda kind, cid, entities: uploads["ok"]
    )
    return types.SimpleNamespace(ask=ask, uploads=uploads)


def test_a_successful_upload_cycles_enablement(sync_run):
    assert catalog_sync.main([]) == 0
    assert [c[0] for c in sync_run.ask.calls] == [
        "delete-skill-enablement", "set-skill-enablement",
    ]


def test_nothing_collected_means_no_cycle(monkeypatch, sync_run):
    """The cycle is itself a short outage, so an idle run must not pay for it."""
    monkeypatch.setattr(catalog_sync, "collect", lambda: {"artists": []})
    assert catalog_sync.main([]) == 0
    assert sync_run.ask.calls == []


def test_a_failed_upload_does_not_cycle(sync_run):
    sync_run.uploads["ok"] = False
    assert catalog_sync.main([]) == 1
    assert sync_run.ask.calls == []


def test_no_cycle_argument_is_honoured(sync_run):
    assert catalog_sync.main(["--no-cycle"]) == 0
    assert sync_run.ask.calls == []


def test_no_cycle_env_var_is_honoured(monkeypatch, sync_run):
    monkeypatch.setenv("CATALOG_NO_CYCLE", "1")
    assert catalog_sync.main([]) == 0
    assert sync_run.ask.calls == []


def test_a_failed_re_enable_fails_the_run(sync_run):
    sync_run.ask.codes["set-skill-enablement"] = 1
    assert catalog_sync.main([]) == 1


def test_missing_skill_id_does_not_fail_the_run(monkeypatch, sync_run):
    monkeypatch.delenv("SKILL_ID", raising=False)
    assert catalog_sync.main([]) == 0


# --- subsonic.song ----------------------------------------------------------
#
# These live here rather than in a file of their own only because this is the
# test module that already imports the sync side of the Subsonic client. Move
# them if a tests/test_subsonic.py ever appears.


def test_song_reads_the_documented_shape(monkeypatch):
    monkeypatch.setattr(
        subsonic, "call", lambda view, **kw: {"song": {"id": "t1", "title": "Juke Box Hero"}}
    )
    assert _REAL_SONG("t1")["title"] == "Juke Box Hero"


def test_song_reads_a_list_wrapped_song(monkeypatch):
    """Servers that render the single child as a JSON array used to read empty."""
    monkeypatch.setattr(
        subsonic, "call",
        lambda view, **kw: {"song": [{"id": "t1", "title": "Juke Box Hero"}]},
    )
    assert _REAL_SONG("t1")["title"] == "Juke Box Hero"


def test_song_reads_a_nested_container(monkeypatch):
    monkeypatch.setattr(
        subsonic, "call",
        lambda view, **kw: {"songs": {"song": [{"id": "t1", "title": "Juke Box Hero"}]}},
    )
    assert _REAL_SONG("t1")["title"] == "Juke Box Hero"


def test_song_returns_none_when_the_server_answers_with_nothing(monkeypatch):
    monkeypatch.setattr(subsonic, "call", lambda view, **kw: {})
    assert _REAL_SONG("t1") is None


def test_song_returns_none_for_an_id_shaped_hole(monkeypatch):
    """An id-less record is not a song; returning it makes the queue lie."""
    monkeypatch.setattr(subsonic, "call", lambda view, **kw: {"song": {"title": "x"}})
    assert _REAL_SONG("t1") is None


def test_song_never_raises(monkeypatch):
    """A metadata lookup must not be able to take a directive down."""
    def boom(view, **kw):
        raise RuntimeError("subsonic error 70: not found")

    monkeypatch.setattr(subsonic, "call", boom)
    assert _REAL_SONG("t1") is None


def test_song_retries_the_other_spelling(monkeypatch):
    seen: list[str] = []

    def call(view, **kw):
        seen.append(view)
        if view.endswith(".view"):
            raise RuntimeError("HTTP Error 404")
        return {"song": {"id": "t1", "title": "Juke Box Hero"}}

    monkeypatch.setattr(subsonic, "call", call)
    assert _REAL_SONG("t1")["title"] == "Juke Box Hero"
    assert seen == ["getSong.view", "getSong"]


def test_song_does_not_retry_when_the_server_answered(monkeypatch):
    """A clean 'no such song' is an answer, not a reason for a second trip."""
    seen: list[str] = []

    def call(view, **kw):
        seen.append(view)
        return {}

    monkeypatch.setattr(subsonic, "call", call)
    assert _REAL_SONG("nope") is None
    assert seen == ["getSong.view"]


# --- the handoff entity -----------------------------------------------------


def test_the_handoff_phrase_is_in_the_catalog():
    """Without this entity the phrase is unsayable.

    Amazon resolves an utterance against the catalog before routing it, and an
    utterance it cannot resolve produces no request at all rather than a search
    carrying the words. So a handoff the bridge answers without any lookup
    still has to exist as an entity for Alexa to ask about it.
    """
    entity = catalog_sync.handoff_entity()
    assert entity["id"] == f"playlist.{handoff.HANDOFF_ENTITY_ID}"
    assert handoff.is_handoff_entity(entity["id"])
    assert [n["value"] for n in entity["names"]] == list(handoff.HANDOFF_PHRASES)


def test_every_configured_phrase_is_a_name_on_the_one_entity(monkeypatch):
    """Moving off a colliding phrase is a config change, not a second identity."""
    monkeypatch.setattr(handoff, "HANDOFF_PHRASES", ("hand off", "the queue"))
    entity = catalog_sync.handoff_entity()
    assert entity["id"] == f"playlist.{handoff.HANDOFF_ENTITY_ID}"
    assert [n["value"] for n in entity["names"]] == ["hand off", "the queue"]


def test_the_handoff_entity_ships_with_the_playlists(monkeypatch):
    monkeypatch.setattr(subsonic, "call", lambda *a, **k: {})
    monkeypatch.setattr(subsonic, "playlists", lambda: [{"id": "p1", "name": "Road trip"}])
    monkeypatch.setattr(subsonic, "genres", lambda: [])
    collected = catalog_sync.collect()
    ids = [e["id"] for e in collected["playlists"]]
    assert f"playlist.{handoff.HANDOFF_ENTITY_ID}" in ids
    assert "playlist.p1" in ids


# --- stations (play [artist] radio) -----------------------------------------


def _one_artist_library(monkeypatch, artist_id="a1", name="Gregory Alan Isakov"):
    """A minimal Navidrome: one artist, no albums, no playlists."""
    def call(view, **kw):
        if view == "getArtists.view":
            return {"artists": {"index": [{"artist": [{"id": artist_id, "name": name}]}]}}
        if view == "getAlbumList2.view":
            return {"albumList2": {"album": []}}
        return {}
    monkeypatch.setattr(catalog_sync.subsonic, "call", call)
    monkeypatch.setattr(catalog_sync.subsonic, "playlists", lambda: [])


def test_stations_are_emitted_when_the_catalog_is_configured(monkeypatch):
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "cat-stations")
    _one_artist_library(monkeypatch)

    out = catalog_sync.collect()

    (station,) = out["stations"]
    assert station["id"] == "station.a1"
    # Named "<artist> Radio" so Alexa can match the whole spoken phrase; the
    # id keys off the same Navidrome artist id rad: already resolves.
    assert station["names"][0]["value"] == "Gregory Alan Isakov Radio"


def test_stations_are_absent_when_the_catalog_is_not_configured(monkeypatch):
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "")
    _one_artist_library(monkeypatch)

    out = catalog_sync.collect()

    assert out["stations"] == []


def test_an_unconfigured_stations_catalog_does_not_fail_the_run(monkeypatch):
    """Stations are optional: leaving CATALOG_STATIONS unset must not abort the
    sync of the five core catalogs."""
    for k in ("artists", "albums", "tracks", "playlists", "genres"):
        monkeypatch.setitem(catalog_sync.CATALOGS, k, f"cat-{k}")
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "")

    missing = [k for k, v in catalog_sync.CATALOGS.items()
               if not v and k not in catalog_sync.OPTIONAL_KINDS]
    assert missing == [], "an unset optional catalog must not count as missing"


# --- collect_from_ma: enumerate the catalog from MA's library ----------------
#
# collect_from_ma runs in the upload worker thread and reaches MA by bouncing
# each library_items call onto MA's event loop. These tests stand up a real loop
# in a background thread and call the function from the test thread, which is the
# worker thread's role, so the run_coroutine_threadsafe bridge is exercised for
# real rather than mocked away.

import asyncio
import threading


class _Loop:
    """A real asyncio loop running in its own thread, for the bridge to target."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _summary(item_id, name, *, artists=None, album=None):
    ns = types.SimpleNamespace(item_id=item_id, name=name)
    if artists is not None:
        ns.artists = artists
    if album is not None:
        ns.album = album
    return ns


class _Controller:
    """A library controller whose library_items pages through a fixed list."""

    def __init__(self, items):
        self._items = items
        self.calls = []

    async def library_items(self, *, provider=None, limit=500, offset=0,
                            summary=True, **kwargs):
        self.calls.append({"provider": provider, "limit": limit,
                           "offset": offset, "summary": summary})
        return self._items[offset:offset + limit]


def _fake_mass(**controllers):
    music = types.SimpleNamespace(**controllers)
    return types.SimpleNamespace(music=music)


def _artist(item_id, name):
    return types.SimpleNamespace(item_id=item_id, name=name)


def test_collect_from_ma_builds_entities_with_library_ids(monkeypatch):
    # No stations catalog configured here; that path has its own test.
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "")

    mass = _fake_mass(
        artists=_Controller([_summary(1, "Radiohead"), _summary(2, "Bjork")]),
        albums=_Controller([
            _summary(10, "In Rainbows", artists=[_artist(1, "Radiohead")]),
        ]),
        tracks=_Controller([
            _summary(100, "Nude",
                     artists=[_artist(1, "Radiohead")],
                     album=_summary(10, "In Rainbows")),
        ]),
        playlists=_Controller([_summary(50, "Chill")]),
        genres=_Controller([_summary(7, "Rock")]),
    )

    with _Loop() as loop:
        out = catalog_sync.collect_from_ma(mass, loop)

    # artists: <kind>.ma-<library_db_id> (the ma- marker routes resolution to MA)
    assert {e["id"] for e in out["artists"]} == {"artist.ma-1", "artist.ma-2"}

    # album carries a nested artist reference keyed by the marked library artist id
    (album,) = out["albums"]
    assert album["id"] == "album.ma-10"
    assert album["artists"] == [
        {"id": "artist.ma-1", "names": [{"language": "en", "value": "Radiohead"}]}
    ]

    # track carries both artist and album references, all marked library ids
    (track,) = out["tracks"]
    assert track["id"] == "track.ma-100"
    assert track["artists"][0]["id"] == "artist.ma-1"
    assert track["albums"][0]["id"] == "album.ma-10"

    assert [e["id"] for e in out["genres"]] == ["genre.ma-7"]

    # the handoff entity is appended to playlists exactly as the Subsonic path does
    playlist_ids = [e["id"] for e in out["playlists"]]
    assert "playlist.ma-50" in playlist_ids
    assert f"playlist.{handoff.HANDOFF_ENTITY_ID}" in playlist_ids


def test_collect_from_ma_pages_to_exhaustion(monkeypatch):
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "")
    monkeypatch.setattr(catalog_sync, "_MA_PAGE", 2)  # force multiple pages

    artists = [_summary(i, f"A{i}") for i in range(5)]  # 2 + 2 + 1
    ctrl = _Controller(artists)
    mass = _fake_mass(
        artists=ctrl,
        albums=_Controller([]), tracks=_Controller([]),
        playlists=_Controller([]), genres=_Controller([]),
    )

    with _Loop() as loop:
        out = catalog_sync.collect_from_ma(mass, loop)

    assert len(out["artists"]) == 5
    # offsets walked in page-sized steps, stopping on the short final page
    assert [c["offset"] for c in ctrl.calls] == [0, 2, 4]


def test_collect_from_ma_forwards_the_provider_filter(monkeypatch):
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "")
    ctrl = _Controller([_summary(1, "A")])
    mass = _fake_mass(
        artists=ctrl,
        albums=_Controller([]), tracks=_Controller([]),
        playlists=_Controller([]), genres=_Controller([]),
    )

    with _Loop() as loop:
        catalog_sync.collect_from_ma(mass, loop, ["library--navidrome"])

    assert ctrl.calls[0]["provider"] == ["library--navidrome"]
    assert ctrl.calls[0]["summary"] is True


def test_station_entity_carries_both_radio_and_station_aliases():
    entity = catalog_sync.station_entity("station.ma-9", "Portishead")
    values = [n["value"] for n in entity["names"]]
    assert values == ["Portishead Radio", "Portishead Station"]


def test_collect_from_ma_emits_no_stations_even_with_the_catalog(monkeypatch):
    """The MA path never emits stations; provider-agnostic radio is parked on a
    branch, so even a configured stations catalog produces none."""
    monkeypatch.setitem(catalog_sync.CATALOGS, "stations", "cat-stations")
    mass = _fake_mass(
        artists=_Controller([_summary(1, "Radiohead")]),
        albums=_Controller([]), tracks=_Controller([]),
        playlists=_Controller([]), genres=_Controller([]),
    )

    with _Loop() as loop:
        out = catalog_sync.collect_from_ma(mass, loop)

    assert out["stations"] == []


# --- handoff phrase propagation (config -> entity name + free-text match) -----
#
# Inside Music Assistant there is no MA_HANDOFF_PHRASE env var, so the configured
# phrase has to be pushed into the handoff module or the spoken command ("play
# the <phrase> playlist") names an entity the catalog does not carry.

def test_configure_renames_the_handoff_phrase(monkeypatch):
    monkeypatch.setattr(handoff, "HANDOFF_PHRASES", ("music assistant",))
    handoff.configure("handoff")
    assert handoff.HANDOFF_PHRASES == ("handoff",)
    # the catalog entity Alexa resolves against now carries the configured phrase
    entity = catalog_sync.handoff_entity()
    assert [n["value"] for n in entity["names"]] == ["handoff"]
    # and the free-text fallback matches it
    assert handoff.is_handoff_phrase("handoff")
    assert not handoff.is_handoff_phrase("music assistant")


def test_configure_accepts_a_comma_list_and_ignores_empty(monkeypatch):
    monkeypatch.setattr(handoff, "HANDOFF_PHRASES", ("music assistant",))
    handoff.configure("play mine, my queue")
    assert handoff.HANDOFF_PHRASES == ("play mine", "my queue")
    handoff.configure("")  # empty must not blank a real phrase
    assert handoff.HANDOFF_PHRASES == ("play mine", "my queue")
