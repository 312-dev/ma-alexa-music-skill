"""The buffer that makes a Music Assistant track seekable.

Phase 3 of ma_provider/PLAN.md. Music Assistant serves realtime audio: always
`200`, no length, no ranges. Buffering a whole track to disk turns it into an
ordinary file, which is the entire difference between a track that can be
scrubbed and moved between rooms and one that cannot.

Nothing here talks to Music Assistant; the fetch is replaced. What is being
tested is the part that goes wrong on its own: a partial download being
mistaken for a whole one, two requests fetching the same track twice, a cache
that grows without bound, and a failure taking playback down with it.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

import mastream_cache
from ma_provider import stream_ref

AUDIO = b"ID3" + b"\x00" * 4096
REF = stream_ref.encode_ref("deezer://track/3135556")
OTHER = stream_ref.encode_ref("spotify://track/abc")


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mastream_cache, "CACHE_DIR", tmp_path / "mastream")
    monkeypatch.setattr(mastream_cache, "ENABLED", True)
    monkeypatch.setattr(mastream_cache, "MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(mastream_cache, "TTL", 3600)
    mastream_cache._LOCKS.clear()
    mastream_cache._INFLIGHT.clear()
    yield


class FakeUpstream:
    """Stands in for urlopen: hands back the audio in two chunks."""

    def __init__(self, payload=AUDIO, fail=False, delay=0.0):
        self.payload = payload
        self.fail = fail
        self.delay = delay
        self.calls = 0
        self._offset = 0

    def __call__(self, url, timeout=None):
        self.calls += 1
        if self.fail:
            raise OSError("music assistant said no")
        if self.delay:
            time.sleep(self.delay)
        self._offset = 0
        return self

    def read(self, size):
        chunk = self.payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- fetching ---------------------------------------------------------------


def test_a_track_is_buffered_whole_and_then_reused():
    upstream = FakeUpstream()
    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        first = mastream_cache.ensure(REF)
        second = mastream_cache.ensure(REF)

    assert first is not None and first == second
    assert first.read_bytes() == AUDIO
    assert upstream.calls == 1, "the second request must not refetch"


def test_a_partial_download_never_becomes_a_cache_entry():
    """The failure that would be worst and hardest to see.

    Half a track served with a confident Content-Length is worse than no track
    at all, because nothing downstream can tell. So bytes land in a .part file
    and are renamed into place only once the transfer has finished.
    """
    class Truncated(FakeUpstream):
        def read(self, size):
            chunk = super().read(size)
            if self._offset > 2048:
                raise OSError("connection reset")
            return chunk

    with mock.patch.object(mastream_cache.urllib.request, "urlopen", Truncated()):
        assert mastream_cache.ensure(REF) is None

    assert not mastream_cache.path_for(REF).exists()
    assert list(mastream_cache.CACHE_DIR.glob("*.part")) == []


def test_an_empty_response_is_not_a_track():
    with mock.patch.object(
        mastream_cache.urllib.request, "urlopen", FakeUpstream(payload=b"")
    ):
        assert mastream_cache.ensure(REF) is None
    assert not mastream_cache.path_for(REF).exists()


def test_a_failure_is_none_rather_than_an_exception():
    """A caller that gets None falls back to streaming from MA.

    Losing the buffer must cost seeking, never playback.
    """
    with mock.patch.object(
        mastream_cache.urllib.request, "urlopen", FakeUpstream(fail=True)
    ):
        assert mastream_cache.ensure(REF) is None


def test_two_requests_for_one_track_fetch_it_once():
    """Four Echoes in a group start together. That is the normal case."""
    upstream = FakeUpstream(delay=0.2)
    results = []

    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        threads = [
            threading.Thread(target=lambda: results.append(mastream_cache.ensure(REF)))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert all(r is not None for r in results)
    assert upstream.calls == 1


# --- what is refused --------------------------------------------------------


def test_something_that_is_not_a_reference_is_never_fetched():
    with mock.patch.object(mastream_cache.urllib.request, "urlopen") as urlopen:
        assert mastream_cache.ensure("!!!not-a-ref!!!") is None
        assert mastream_cache.ensure(
            stream_ref.encode_ref("http://169.254.169.254/")) is None
    urlopen.assert_not_called()


def test_turning_the_cache_off_skips_it_entirely(monkeypatch):
    monkeypatch.setattr(mastream_cache, "ENABLED", False)
    with mock.patch.object(mastream_cache.urllib.request, "urlopen") as urlopen:
        assert mastream_cache.ensure(REF) is None
    urlopen.assert_not_called()


def test_the_filename_cannot_escape_the_cache_directory():
    assert "/" not in mastream_cache._safe("../../etc/passwd")
    assert mastream_cache.path_for("../../etc/passwd").parent == \
        mastream_cache.CACHE_DIR


# --- eviction ---------------------------------------------------------------


def test_the_cache_stays_under_its_size_bound(monkeypatch):
    monkeypatch.setattr(mastream_cache, "MAX_BYTES", 5000)
    mastream_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    now = time.time()
    for index in range(4):
        path = mastream_cache.CACHE_DIR / f"track{index}.mp3"
        path.write_bytes(b"x" * 2000)
        # Oldest touched first, so make the ages explicit.
        import os
        os.utime(path, (now - (10 - index), now - (10 - index)))

    mastream_cache.evict()

    left = sorted(p.name for p in mastream_cache.CACHE_DIR.glob("*.mp3"))
    assert left == ["track2.mp3", "track3.mp3"]


def test_a_stale_track_goes_whatever_the_size_is(monkeypatch):
    monkeypatch.setattr(mastream_cache, "TTL", 60)
    mastream_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import os

    fresh = mastream_cache.CACHE_DIR / "fresh.mp3"
    stale = mastream_cache.CACHE_DIR / "stale.mp3"
    fresh.write_bytes(b"x" * 10)
    stale.write_bytes(b"x" * 10)
    os.utime(stale, (time.time() - 600, time.time() - 600))

    mastream_cache.evict()

    assert fresh.exists()
    assert not stale.exists()


def test_playing_a_track_again_makes_it_look_recent():
    """A track on repeat must not be the first thing thrown away."""
    upstream = FakeUpstream()
    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        path = mastream_cache.ensure(REF)
    import os
    os.utime(path, (time.time() - 9999, time.time() - 9999))

    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        mastream_cache.ensure(REF)

    assert time.time() - path.stat().st_mtime < 60


# --- read-ahead -------------------------------------------------------------


def test_prefetch_is_bounded_and_does_not_block(monkeypatch):
    """The queue may be hundreds of tracks. Only the next few are worth having."""
    monkeypatch.setattr(mastream_cache, "PREFETCH_AHEAD", 2)
    upstream = FakeUpstream()
    refs = [stream_ref.encode_ref(f"deezer://track/{i}") for i in range(10)]

    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        mastream_cache.prefetch(refs)
        for _ in range(50):
            if upstream.calls >= 2:
                break
            time.sleep(0.05)

    assert upstream.calls == 2


def test_prefetch_ignores_anything_that_is_not_a_reference():
    with mock.patch.object(mastream_cache.urllib.request, "urlopen") as urlopen:
        mastream_cache.prefetch(["", "nonsense", None])
        time.sleep(0.2)
    urlopen.assert_not_called()


def test_stats_report_what_is_held():
    upstream = FakeUpstream()
    with mock.patch.object(mastream_cache.urllib.request, "urlopen", upstream):
        mastream_cache.ensure(REF)
        mastream_cache.ensure(OTHER)

    stats = mastream_cache.stats()
    assert stats["tracks"] == 2
    assert stats["bytes"] == 2 * len(AUDIO)
    assert stats["enabled"] is True
