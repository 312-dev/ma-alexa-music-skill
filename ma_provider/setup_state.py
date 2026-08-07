"""Wizard progress and tunable settings, on disk.

Small and boring on purpose. The wizard is a handful of decisions the operator
makes once, and losing them to a container restart would mean doing the ASK
OAuth dance again.
"""

from __future__ import annotations

import json
import os
import pathlib

FILENAME = "setup-state.json"

# Where the file lives. Overridden by `core.configure` when Music Assistant
# supplies a storage path, because the default is an absolute path chosen for a
# container this service owned and inside MA it resolves to MA's own storage
# root, next to library.db.
STATE_DIR: pathlib.Path | None = None

DEFAULTS = {
    "alias": "",
    "skill_id": "",
    "vendor_id": "",
    "catalogs": {},
    "endpoint_ok": False,
    "cert_type": "",
    "subsonic_url": "",
    "subsonic_user": "",
    "radio_artists": None,
    "radio_tracks_per_artist": None,
    "after_content": "",
    "uploads": {},
    "catalog_hashes": {},
    "enabled": False,
    # The reactive binding detector's circuit breaker. Armed means a miss may
    # trigger one re-provision cycle; it disarms on that attempt and only
    # re-arms when playback is observed working again, so a cause the cycle
    # cannot fix produces one attempt rather than one per failed request.
    "reactive_armed": True,
    "reactive_cycled_at": 0.0,
    "reactive_misses": 0,
    "reactive_last_miss": 0.0,
}


def path() -> pathlib.Path:
    if STATE_DIR is not None:
        return STATE_DIR / FILENAME
    return pathlib.Path(os.environ.get("SETUP_STATE_DIR", "/data")) / FILENAME


def load() -> dict:
    state = dict(DEFAULTS)
    try:
        stored = json.loads(path().read_text())
    except (OSError, ValueError):
        return state
    if isinstance(stored, dict):
        state.update(stored)
    return state


def save(state: dict) -> bool:
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2))
        temp.replace(target)
    except OSError:
        return False
    return True


def update(**fields) -> dict:
    state = load()
    state.update(fields)
    save(state)
    return state
