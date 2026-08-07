"""Resolving catalog content ids to playable tracks out of Music Assistant.

Like the catalog-enumeration tests, these stand up a real event loop in a
background thread and call the resolver from the test thread (the bridge's
worker-thread role), so the run_coroutine_threadsafe hop is exercised for real.
"""

from __future__ import annotations

import asyncio
import threading
import types

from ma_provider import ma_resolve
from ma_provider.stream_ref import decode_ref


class _Loop:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _artist(name):
    return types.SimpleNamespace(name=name)


def _track(uri, name, *, artists=(), album=None, duration=0, art=None):
    image = None
    if art is not None:
        image = types.SimpleNamespace(remotely_accessible=True, path=art)
    return types.SimpleNamespace(
        uri=uri, name=name, artists=list(artists), album=album,
        duration=duration, image=image)


class _AsyncMethod:
    """Wrap a plain function as an awaitable that records its call."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._fn(*args, **kwargs)


# --- song_from_track ---------------------------------------------------------


def test_song_from_track_maps_the_fields_and_makes_a_ref():
    track = _track("spotify://track/abc", "Nude",
                   artists=[_artist("Radiohead"), _artist("Someone")],
                   album=types.SimpleNamespace(name="In Rainbows"),
                   duration=254, art="https://cdn.example/art.jpg")

    song = ma_resolve.song_from_track(track)

    assert song["title"] == "Nude"
    assert song["artist"] == "Radiohead, Someone"
    assert song["album"] == "In Rainbows"
    assert song["duration"] == 254
    assert song["art_url"] == "https://cdn.example/art.jpg"
    # the ref decodes back to the item's uri: that is the whole naming contract
    assert decode_ref(song["ma_ref"]) == "spotify://track/abc"
    assert song["id"] == "spotify://track/abc"


def test_song_from_track_drops_an_item_with_no_uri():
    assert ma_resolve.song_from_track(_track("", "No uri")) is None
    assert ma_resolve.song_from_track(types.SimpleNamespace(uri=None)) is None


def test_song_from_track_omits_unreachable_art():
    track = _track("plex://track/1", "Local", duration=100)
    track.image = types.SimpleNamespace(remotely_accessible=False, path="http://tailnet/x")
    song = ma_resolve.song_from_track(track)
    assert "art_url" not in song


def test_song_from_track_marks_the_first_artist_for_continuation():
    """The seed a radio/artist after-content is built from. Marked, so the
    continuation routes back through MA, and keyed on the library artist id the
    catalog uses so a station reached this way resolves to itself."""
    artist = types.SimpleNamespace(name="Radiohead", item_id="42")
    track = _track("spotify://track/abc", "Nude", artists=[artist])
    assert ma_resolve.song_from_track(track)["artistId"] == "ma-42"


def test_song_from_track_omits_artistId_when_the_artist_has_no_id():
    """Some imports carry an artist name but no id; leaving artistId unset makes
    continuation fall back to the library, the same as a Navidrome import without
    an artistId, rather than seeding from a bad id."""
    track = _track("spotify://track/abc", "Nude", artists=[_artist("Radiohead")])
    assert "artistId" not in ma_resolve.song_from_track(track)


# --- id helpers --------------------------------------------------------------


def test_marker_helpers():
    assert ma_resolve.is_ma_native("ma-1234")
    assert not ma_resolve.is_ma_native("1234")
    assert ma_resolve.library_id("ma-1234") == "1234"
    assert ma_resolve.library_id("1234") == "1234"


# --- resolve per kind --------------------------------------------------------


def _mass_with(**controllers):
    return types.SimpleNamespace(music=types.SimpleNamespace(**controllers))


def test_resolve_album_reads_library_album_tracks():
    album_tracks = _AsyncMethod(lambda item_id, prov: [
        _track("nav://track/1", "One", duration=100),
        _track("nav://track/2", "Two", duration=120),
    ])
    mass = _mass_with(albums=types.SimpleNamespace(tracks=album_tracks))

    with _Loop() as loop:
        songs = ma_resolve.resolve("al", "ma-10", mass, loop)

    assert album_tracks.calls == [(("10", "library"), {})]
    assert [s["title"] for s in songs] == ["One", "Two"]
    assert all(s["ma_ref"] for s in songs)


def test_resolve_artist_reads_library_artist_tracks():
    artist_tracks = _AsyncMethod(lambda item_id, prov: [_track("nav://track/9", "Nine")])
    mass = _mass_with(artists=types.SimpleNamespace(tracks=artist_tracks))

    with _Loop() as loop:
        songs = ma_resolve.resolve("ar", "ma-3", mass, loop)

    assert artist_tracks.calls == [(("3", "library"), {})]
    assert [s["title"] for s in songs] == ["Nine"]


def test_resolve_track_reads_one_library_item():
    get_item = _AsyncMethod(lambda db_id: _track("nav://track/5", "Five"))
    mass = _mass_with(tracks=types.SimpleNamespace(get_library_item=get_item))

    with _Loop() as loop:
        songs = ma_resolve.resolve("tr", "ma-5", mass, loop)

    assert get_item.calls == [(("5",), {})]
    assert [s["title"] for s in songs] == ["Five"]


def test_resolve_playlist_drains_the_async_generator():
    tracks = [_track("nav://track/1", "A"), _track("nav://track/2", "B")]

    class _Playlists:
        def __init__(self):
            self.calls = []

        def tracks(self, item_id, prov):
            self.calls.append((item_id, prov))

            async def gen():
                for t in tracks:
                    yield t

            return gen()

    playlists = _Playlists()
    mass = _mass_with(playlists=playlists)

    with _Loop() as loop:
        songs = ma_resolve.resolve("pl", "ma-7", mass, loop)

    assert playlists.calls == [("7", "library")]
    assert [s["title"] for s in songs] == ["A", "B"]


def test_resolve_genre_filters_library_tracks_by_genre_id():
    library_items = _AsyncMethod(lambda **kw: [_track("nav://track/1", "G")])
    mass = _mass_with(tracks=types.SimpleNamespace(library_items=library_items))

    with _Loop() as loop:
        songs = ma_resolve.resolve("gen", "ma-42", mass, loop)

    (_, kwargs), = library_items.calls
    assert kwargs["genre"] == [42]
    assert kwargs["summary"] is False
    assert [s["title"] for s in songs] == ["G"]


def test_resolve_swallows_errors_and_returns_empty():
    def boom(*_a, **_k):
        raise RuntimeError("nope")

    mass = _mass_with(albums=types.SimpleNamespace(tracks=_AsyncMethod(boom)))
    with _Loop() as loop:
        assert ma_resolve.resolve("al", "ma-1", mass, loop) == []


# --- favorites / library sample ---------------------------------------------


def test_favorites_reads_favorited_library_tracks():
    library_items = _AsyncMethod(lambda **kw: [_track("nav://track/1", "Fav")])
    mass = _mass_with(tracks=types.SimpleNamespace(library_items=library_items))

    with _Loop() as loop:
        songs = ma_resolve.favorites(mass, loop)

    (_, kwargs), = library_items.calls
    assert kwargs["favorite"] is True
    assert kwargs["order_by"] == "sort_name"  # stable, not random
    assert [s["title"] for s in songs] == ["Fav"]


def test_library_sample_reads_a_bounded_stable_slice():
    library_items = _AsyncMethod(lambda **kw: [_track("nav://track/1", "Lib")])
    mass = _mass_with(tracks=types.SimpleNamespace(library_items=library_items))

    with _Loop() as loop:
        songs = ma_resolve.library_sample(mass, loop)

    (_, kwargs), = library_items.calls
    assert kwargs["favorite"] is None
    assert kwargs["limit"] == ma_resolve.LIBRARY_SAMPLE
    assert kwargs["order_by"] == "sort_name"
    assert [s["title"] for s in songs] == ["Lib"]


# --- core.resolve_tracks routing (Subsonic vs MA) ---------------------------
#
# These reach into core.resolve_tracks to prove the marker (and, for the id-less
# whole-library intents, MA mode) picks the right source. The autouse
# fake_subsonic fixture stands in for Navidrome, so a wrongly-routed call is
# visible as Subsonic data leaking into an MA result or vice versa.

from ma_provider import core


def _set_ma(monkeypatch, loop, mass, *, providers):
    monkeypatch.setattr(core, "MASS", mass)
    monkeypatch.setattr(core, "LOOP", loop)
    monkeypatch.setattr(core.setup_state, "load",
                        lambda: {"catalog_providers": providers})
    core._QUEUE_CACHE.clear()


def test_resolve_tracks_routes_a_marked_id_to_ma(monkeypatch):
    album_tracks = _AsyncMethod(lambda item_id, prov: [
        _track("spotify://track/1", "One", duration=100)])
    mass = _mass_with(albums=types.SimpleNamespace(tracks=album_tracks))

    with _Loop() as loop:
        _set_ma(monkeypatch, loop, mass, providers=["p"])
        songs = core.resolve_tracks("al:ma-10")

    assert album_tracks.calls == [(("10", "library"), {})]
    assert songs and songs[0]["ma_ref"] and songs[0]["title"] == "One"


def test_resolve_tracks_keeps_an_unmarked_id_on_subsonic(monkeypatch):
    # MA is live, but a bare (unmarked) id is a Subsonic id and must stay there.
    album_tracks = _AsyncMethod(lambda item_id, prov: [_track("x://y", "MA")])
    mass = _mass_with(albums=types.SimpleNamespace(tracks=album_tracks))

    with _Loop() as loop:
        _set_ma(monkeypatch, loop, mass, providers=["p"])
        songs = core.resolve_tracks("al:al1")  # al1 is a Subsonic album in the fixture

    assert album_tracks.calls == []            # MA never consulted
    assert [s["id"] for s in songs] == ["t1", "t2"]  # Subsonic data


def test_star_reads_ma_favorites_in_ma_mode(monkeypatch):
    library_items = _AsyncMethod(lambda **kw: [_track("spotify://track/1", "Fav")])
    mass = _mass_with(tracks=types.SimpleNamespace(library_items=library_items))

    with _Loop() as loop:
        _set_ma(monkeypatch, loop, mass, providers=["p"])
        songs = core.resolve_tracks("star")

    assert library_items.calls and library_items.calls[0][1]["favorite"] is True
    assert songs and songs[0]["title"] == "Fav"


def test_star_stays_on_subsonic_without_a_provider_selection(monkeypatch):
    # MA is live but no providers chosen: the whole-library intents stay Subsonic.
    library_items = _AsyncMethod(lambda **kw: [_track("x://y", "MA")])
    mass = _mass_with(tracks=types.SimpleNamespace(library_items=library_items))

    with _Loop() as loop:
        _set_ma(monkeypatch, loop, mass, providers=[])
        songs = core.resolve_tracks("star")

    assert library_items.calls == []          # MA never consulted
    assert [s["id"] for s in songs] == ["t1"]  # Subsonic starred fixture


# --- streamable_uri: a library item must resolve to a PROVIDER uri -----------
#
# The bug these guard: a library track's own uri is library://track/<id>, and
# "library" is not a streaming provider, so a ref built from it 404s at the
# stream route. streamable_uri must pick a real provider mapping instead.

def _mapping(instance, item_id, *, available=True, quality=0, domain=None):
    return types.SimpleNamespace(
        provider_instance=instance, provider_domain=domain or instance,
        item_id=item_id, available=available, quality=quality)


def _lib_track(db_id, name, mappings, *, duration=0):
    # A library track as MA returns it: its own uri is library://, and the real
    # sources live in provider_mappings.
    return types.SimpleNamespace(
        uri=f"library://track/{db_id}", name=name, artists=[], album=None,
        duration=duration, image=None, provider_mappings=list(mappings))


def test_streamable_uri_builds_from_the_best_available_mapping():
    track = _lib_track(1, "T", [
        _mapping("spotify", "sp1", quality=5),
        _mapping("tidal", "td1", quality=9),  # higher quality wins
    ])
    assert ma_resolve.streamable_uri(track) == "tidal://track/td1"


def test_streamable_uri_skips_unavailable_mappings():
    track = _lib_track(1, "T", [
        _mapping("tidal", "td1", quality=9, available=False),
        _mapping("spotify", "sp1", quality=5, available=True),
    ])
    assert ma_resolve.streamable_uri(track) == "spotify://track/sp1"


def test_streamable_uri_skips_providers_not_loaded_when_mass_given():
    track = _lib_track(1, "T", [
        _mapping("tidal", "td1", quality=9),
        _mapping("spotify", "sp1", quality=5),
    ])
    # tidal is not loaded; spotify is
    mass = types.SimpleNamespace(
        get_provider=lambda inst: object() if inst == "spotify" else None)
    assert ma_resolve.streamable_uri(track, mass) == "spotify://track/sp1"


def test_song_from_a_library_track_uses_the_mapping_not_the_library_uri():
    track = _lib_track(42, "Real", [_mapping("plex", "px9", quality=1)], duration=200)
    song = ma_resolve.song_from_track(track)
    assert decode_ref(song["ma_ref"]) == "plex://track/px9"
    assert not decode_ref(song["ma_ref"]).startswith("library://")
    assert song["duration"] == 200


def test_a_library_track_with_no_streamable_mapping_is_dropped():
    # library uri only, no usable provider mapping -> cannot be streamed
    track = _lib_track(7, "Orphan", [])
    assert ma_resolve.song_from_track(track) is None
    # ... and a mapping whose provider is not loaded is not usable either
    track2 = _lib_track(8, "Gone", [_mapping("dead", "d1")])
    mass = types.SimpleNamespace(get_provider=lambda inst: None)
    assert ma_resolve.song_from_track(track2, mass) is None


# --- describe: real names for marked catalog entities ------------------------

def test_describe_reads_the_library_item_name():
    get_item = _AsyncMethod(lambda db_id: types.SimpleNamespace(name="bedtime"))
    mass = _mass_with(playlists=types.SimpleNamespace(get_library_item=get_item))

    with _Loop() as loop:
        name, art = ma_resolve.describe("pl", "59", mass, loop)

    assert get_item.calls == [(("59",), {})]
    assert name == "bedtime"
    assert art is None


def test_describe_returns_none_for_an_unknown_prefix():
    mass = _mass_with()
    with _Loop() as loop:
        assert ma_resolve.describe("station", "1", mass, loop) == (None, None)


def test_describe_swallows_lookup_errors():
    def boom(_):
        raise RuntimeError("gone")
    mass = _mass_with(artists=types.SimpleNamespace(get_library_item=_AsyncMethod(boom)))
    with _Loop() as loop:
        assert ma_resolve.describe("ar", "1", mass, loop) == (None, None)


# --- album art: public url passes through, everything else via imageproxy id --

def _img(path, *, remote, provider="opensubsonic"):
    return types.SimpleNamespace(path=path, remotely_accessible=remote,
                                 provider=provider)


def _mass_with_metadata(image_id="img-abc"):
    calls = []
    def compute(provider, path):
        calls.append((provider, path)); return image_id
    m = _mass_with()
    m.metadata = types.SimpleNamespace(compute_image_id=compute)
    m._compute_calls = calls
    return m


def test_song_from_track_passes_through_a_public_cover_url():
    t = _track("spotify://track/1", "T", duration=10)
    t.image = _img("https://cdn/x.jpg", remote=True)
    song = ma_resolve.song_from_track(t, _mass_with_metadata())
    assert song["art_url"] == "https://cdn/x.jpg"
    assert "art_id" not in song


def test_song_from_track_carries_an_imageproxy_id_for_a_tailnet_cover():
    t = _track("plex://track/1", "T", duration=10)
    t.image = _img("http://tailnet/cover", remote=False, provider="opensubsonic")
    mass = _mass_with_metadata("img-xyz")
    song = ma_resolve.song_from_track(t, mass)
    assert song.get("art_id") == "img-xyz"
    assert "art_url" not in song
    assert mass._compute_calls == [("opensubsonic", "http://tailnet/cover")]


def test_song_from_track_without_mass_skips_the_imageproxy_id():
    t = _track("plex://track/1", "T", duration=10)
    t.image = _img("http://tailnet/cover", remote=False)
    song = ma_resolve.song_from_track(t)  # no mass -> cannot compute the id
    assert "art_id" not in song and "art_url" not in song
