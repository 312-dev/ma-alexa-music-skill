"""The wizard's step model.

Completion is derived from the things themselves, not from a flag written when
a button was pressed. A stored flag drifts: the skill is deleted in Amazon's
console, the credentials are revoked, the volume is restored from a backup, and
the wizard still reports the step as done. Deriving it means the wizard tells
the truth about the current state rather than about a past click.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Callable

import smapi_rest


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    blurb: str
    template: str
    done: Callable[[dict], bool]


def _music_server_done(state: dict) -> bool:
    return bool(os.environ.get("SUBSONIC_URL") or state.get("subsonic_url"))


_TRAFFIC = {"at": 0.0, "ok": False}


def _amazon_traffic(max_age: float = 7 * 86400) -> bool:
    """Has Amazon demonstrably reached this service recently.

    Signed inbound captures are stronger evidence of reachability than any
    check the wizard can run on itself, and a deployment that is already
    serving Alexa must not be asked to prove it can be reached. Only captures
    carrying Amazon's signature headers count: local smoke tests write capture
    files too, and those prove nothing about the public path.
    """
    now = time.time()
    if now - _TRAFFIC["at"] < 60:
        return _TRAFFIC["ok"]

    found = False
    capture_dir = pathlib.Path(os.environ.get("CAPTURE_DIR", "/data/captures"))
    try:
        entries = [e for e in os.scandir(capture_dir) if e.name.endswith(".json")]
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        for entry in entries[:8]:
            if now - entry.stat().st_mtime > max_age:
                break
            try:
                headers = json.loads(pathlib.Path(entry.path).read_text()).get("headers") or {}
            except (OSError, ValueError, AttributeError):
                continue
            if any(k.lower().startswith("signature") for k in headers):
                found = True
                break
    except OSError:
        pass
    _TRAFFIC.update(at=now, ok=found)
    return found


def _endpoint_done(state: dict) -> bool:
    return bool(state.get("endpoint_ok")) or _amazon_traffic()


def _amazon_done(_state: dict) -> bool:
    return smapi_rest.connected()


def _alias_done(state: dict) -> bool:
    return bool(state.get("alias"))


_SKILL_CHECK = {"at": 0.0, "id": "", "exists": True}


def _skill_exists(skill_id: str, max_age: float = 60.0) -> bool:
    """Does the recorded skill still exist on Amazon.

    A stored id drifts: the skill gets deleted in the developer console, or
    validation quietly kills it, and the wizard would still call the step
    done. Cached so the rail does not cost a SMAPI call per render. Only a
    definitive 404 counts as gone; lookup trouble of any other kind must not
    un-done a step over a network blip.
    """
    now = time.time()
    if _SKILL_CHECK["id"] == skill_id and now - _SKILL_CHECK["at"] < max_age:
        return _SKILL_CHECK["exists"]
    if not smapi_rest.connected():
        return True
    exists = True
    try:
        smapi_rest.skill_status(skill_id)
    except smapi_rest.SmapiError as exc:
        if exc.status == 404:
            exists = False
    except Exception:
        pass
    _SKILL_CHECK.update(at=now, id=skill_id, exists=exists)
    return exists


def _skill_done(state: dict) -> bool:
    skill_id = state.get("skill_id") or os.environ.get("SKILL_ID") or ""
    return bool(skill_id) and _skill_exists(skill_id)


def _catalogs_done(state: dict) -> bool:
    return bool(state.get("catalogs"))


_INGESTION = {"at": 0.0, "sig": "", "ok": False}


def _ingestion_complete(state: dict, max_age: float = 60.0) -> bool:
    """Has ER_INGESTION succeeded for every uploaded catalog.

    Uploading is not the finish line: until Amazon ingests the catalog, voice
    resolution does not work, and enabling the skill early just produces a
    skill that answers from the wrong provider. Cached so the rail does not
    cost five SMAPI calls per render; on lookup trouble the last known verdict
    stands rather than flapping the step over a network blip.
    """
    uploads = state.get("uploads") or {}
    catalogs = state.get("catalogs") or {}
    pairs = [(catalogs[k], u) for k, u in uploads.items() if catalogs.get(k)]
    if not pairs:
        return False
    sig = ",".join(f"{c}:{u}" for c, u in sorted(pairs))
    now = time.time()
    if _INGESTION["sig"] == sig and now - _INGESTION["at"] < max_age:
        return _INGESTION["ok"]
    if not smapi_rest.connected():
        return _INGESTION["ok"] if _INGESTION["sig"] == sig else False
    try:
        ok = all(
            smapi_rest.ingestion_verdict(
                smapi_rest.upload_status(catalog, upload))[0] == "ready"
            for catalog, upload in pairs)
    except Exception:
        return _INGESTION["ok"] if _INGESTION["sig"] == sig else False
    _INGESTION.update(at=now, sig=sig, ok=ok)
    return ok


def _upload_done(state: dict) -> bool:
    return bool(state.get("uploads")) and _ingestion_complete(state)


def _enabled_done(state: dict) -> bool:
    return bool(state.get("enabled"))


STEPS: tuple[Step, ...] = (
    Step("server", "Music server",
         "Where your library lives. Everything else depends on this "
         "connection working.",
         "wizard/_subsonic.html", _music_server_done),
    Step("endpoint", "Public endpoint",
         "Amazon calls this service directly, so it has to be reachable over "
         "HTTPS from the public internet. This step confirms that it is.",
         "wizard/_endpoint.html", _endpoint_done),
    Step("amazon", "Amazon account",
         "Authorizes this service to manage skills and catalogs on your "
         "behalf, using credentials you register yourself.",
         "wizard/_amazon.html", _amazon_done),
    Step("alias", "Alias",
         "The word you say after \"on\". It becomes the skill name, and it has "
         "to be one Alexa will not confuse with your own music.",
         "wizard/_alias.html", _alias_done),
    Step("skill", "Create the skill",
         "Registers a development-stage music skill against your Amazon "
         "developer account.",
         "wizard/_skill.html", _skill_done),
    Step("catalogs", "Create the catalogs",
         "Five catalogs, one per entity type, associated with the skill so "
         "spoken names can resolve against them.",
         "wizard/_catalogs.html", _catalogs_done),
    Step("upload", "Upload your library",
         "Sends your artists, albums, tracks, playlists and genres to Amazon "
         "so Alexa can recognize them.",
         "wizard/_upload.html", _upload_done),
    Step("enable", "Enable the skill",
         "Binds the skill to your account. Without this, Alexa answers from "
         "your default music provider instead.",
         "wizard/_enable.html", _enabled_done),
)

BY_KEY = {step.key: step for step in STEPS}


def progress(state: dict) -> list[dict]:
    """Each step with its completion, and whether it can be opened yet.

    A step is reachable when every step before it is done. Going back is always
    allowed, because reviewing a finished step is not the same as redoing it.
    """
    rows, blocked = [], False
    for index, step in enumerate(STEPS):
        done = bool(step.done(state))
        rows.append({
            "index": index,
            "number": index + 1,
            "key": step.key,
            "title": step.title,
            "blurb": step.blurb,
            "done": done,
            "reachable": not blocked,
        })
        if not done:
            blocked = True
    return rows


def complete(state: dict) -> bool:
    return all(step.done(state) for step in STEPS)


def current_index(state: dict) -> int:
    """The first unfinished step, or the last one when everything is done."""
    for index, step in enumerate(STEPS):
        if not step.done(state):
            return index
    return len(STEPS) - 1
