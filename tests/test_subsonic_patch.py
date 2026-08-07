"""Tests for the in-process OpenSubsonic playlist patch machinery.

The fast replacement's own behaviour (one getPlaylist call, no per-track album or
lyrics fetch) is identical to, and covered by, the upstream change's test. What is
exercised here is the patch machinery that is unique to Music Assistant: applying,
restoring, idempotency, and the self-disabling detection of an already-fixed MA.
"""

from __future__ import annotations

import logging

import pytest

from ma_provider import subsonic_patch

_LOG = logging.getLogger("test.subsonic_patch")


class _UnfixedProvider:
    """Stands in for an MA that still fetches lyrics per track."""

    async def get_playlist_tracks(self, prov_playlist_id, page=0):
        # references get_track_lyrics -- the string already_fixed() scans the source for
        _ = self.get_track_lyrics
        return []


class _FixedProvider:
    """Stands in for an MA that already carries the upstream fix."""

    async def get_playlist_tracks(self, prov_playlist_id, page=0):
        return []


@pytest.fixture(autouse=True)
def _isolate_patch_state():
    """Guarantee each test starts and ends with the fakes and module state clean."""
    saved = {
        _UnfixedProvider: _UnfixedProvider.get_playlist_tracks,
        _FixedProvider: _FixedProvider.get_playlist_tracks,
    }
    subsonic_patch._ORIGINAL = None
    yield
    for cls, meth in saved.items():
        cls.get_playlist_tracks = meth
    subsonic_patch._ORIGINAL = None


def _use(monkeypatch: pytest.MonkeyPatch, cls: type) -> None:
    monkeypatch.setattr(subsonic_patch, "_provider_cls", lambda: cls)


def test_apply_then_restore_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _UnfixedProvider)
    original = _UnfixedProvider.get_playlist_tracks

    assert subsonic_patch.apply(_LOG) is True
    assert subsonic_patch.is_applied() is True
    assert _UnfixedProvider.get_playlist_tracks is not original

    subsonic_patch.restore(_LOG)
    assert subsonic_patch.is_applied() is False
    assert _UnfixedProvider.get_playlist_tracks is original


def test_apply_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _UnfixedProvider)
    assert subsonic_patch.apply(_LOG) is True
    assert subsonic_patch.apply(_LOG) is False  # second call is a no-op
    assert subsonic_patch.is_applied() is True


def test_skips_when_already_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _FixedProvider)
    assert subsonic_patch.already_fixed() is True
    assert subsonic_patch.apply(_LOG) is False
    assert subsonic_patch.is_applied() is False


def test_detects_unfixed(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _UnfixedProvider)
    assert subsonic_patch.already_fixed() is False


def test_restore_without_apply_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _UnfixedProvider)
    original = _UnfixedProvider.get_playlist_tracks
    subsonic_patch.restore(_LOG)  # nothing applied
    assert _UnfixedProvider.get_playlist_tracks is original
