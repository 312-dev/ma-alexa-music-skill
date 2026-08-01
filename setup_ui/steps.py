"""The wizard's step model.

Completion is derived from the things themselves, not from a flag written when
a button was pressed. A stored flag drifts: the skill is deleted in Amazon's
console, the credentials are revoked, the volume is restored from a backup, and
the wizard still reports the step as done. Deriving it means the wizard tells
the truth about the current state rather than about a past click.
"""

from __future__ import annotations

import os
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


def _endpoint_done(state: dict) -> bool:
    return bool(state.get("endpoint_ok"))


def _amazon_done(_state: dict) -> bool:
    return smapi_rest.connected()


def _alias_done(state: dict) -> bool:
    return bool(state.get("alias"))


def _skill_done(state: dict) -> bool:
    return bool(state.get("skill_id") or os.environ.get("SKILL_ID"))


def _catalogs_done(state: dict) -> bool:
    return bool(state.get("catalogs"))


def _upload_done(state: dict) -> bool:
    return bool(state.get("uploads"))


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
         "Authorises this service to manage skills and catalogs on your "
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
         "so Alexa can recognise them.",
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
