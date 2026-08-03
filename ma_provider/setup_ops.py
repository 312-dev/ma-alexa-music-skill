"""What the wizard's buttons actually do.

Every step of setup is one operation against Amazon: create the skill, create
the catalogs, upload the library, bind the skill to the account. Those
operations were written inside Flask view functions, interleaved with
`render_template` and `request.form`, which meant Music Assistant could not
run any of them without Flask and a request context.

This is the same extraction that produced `core`: the behaviour on one side,
the framework on the other. Both the web wizard and Music Assistant's config
form call these, so there is one implementation of "create the skill" rather
than two that drift.

Nothing here renders anything or knows what a request is. Every operation
returns an `Outcome`, and the caller decides whether that becomes an HTML
fragment or a line of text under a button in MA's settings.

The subtleties are load-bearing and were all found the hard way. They are
commented where they sit, but the shape of them is worth stating once: with
Amazon, the call returning 200 is routinely not the same as the thing having
worked. Skill creation returns an id before validation has run. A catalog
upload succeeds and silently unbinds the provider slot. Enablement reports
enabled for a skill that never receives a directive. Each operation here
therefore checks the thing itself afterwards rather than trusting the status
code, which is why several of them take a patient retry loop.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from . import catalog_sync
from . import setup_smapi as smapi
from . import setup_state as store
from . import setup_steps
from . import smapi_rest
from . import subsonic

logger = logging.getLogger("ampere.setup")

# One catalog per entity type, which is Amazon's model: a spoken name resolves
# against the catalog for its kind, so an album name and a playlist name of the
# same words land in different catalogs and do not compete.
CATALOG_KINDS = {
    "artists": "AMAZON.MusicGroup",
    "albums": "AMAZON.MusicAlbum",
    "tracks": "AMAZON.MusicRecording",
    "playlists": "AMAZON.MusicPlaylist",
    "genres": "AMAZON.Genre",
}


@dataclass
class Outcome:
    """Whether an operation worked, and what to tell the operator.

    `detail` is written to be read by a person who is midway through setup and
    does not know this codebase, because that is the only audience it ever has.
    `rows` carries per-item results for operations that act on five catalogs
    and can half-succeed.
    """

    ok: bool
    detail: str = ""
    rows: list[dict] = field(default_factory=list)


def error_text(exc: Exception) -> str:
    """Amazon's own words where there are any, truncated where there are not."""
    if isinstance(exc, smapi_rest.SmapiError):
        return f"{exc} {exc.body}".strip()
    return str(exc)[:400]


def skill_id(state: dict | None = None) -> str:
    """The skill this deployment owns, from wherever it was recorded."""
    import os

    current = store.load() if state is None else state
    return current.get("skill_id") or os.environ.get("SKILL_ID", "")


def catalog_ids(state: dict | None = None) -> dict[str, str]:
    import os

    current = store.load() if state is None else state
    stored = current.get("catalogs") or {}
    return {
        kind: os.environ.get(f"CATALOG_{kind.upper()}", "") or stored.get(kind, "")
        for kind in CATALOG_KINDS
    }


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def probe_music_server() -> Outcome:
    """A cheap search, to prove the library is reachable and not empty."""
    if not subsonic.configured():
        return Outcome(False, "No music server is configured yet.")
    try:
        result = subsonic.search("the", songs=3)
    except Exception as exc:
        return Outcome(False, str(exc)[:200])
    songs = result.get("song") or []
    noun = "result" if len(songs) == 1 else "results"
    return Outcome(True, f"{len(songs)} {noun} for a sample search")


def manifest_verdict(skill: str, tries: int = 8, delay: float = 1.5) -> str:
    """Wait out the async manifest validation that follows skill creation.

    Creation returning a skillId is not acceptance: validation runs after, and
    its failure is otherwise silent until catalog association 404s. An empty
    return means validated, or still pending after a patient wait; a non-empty
    return is Amazon's own error text.
    """
    for attempt in range(tries):
        try:
            status = smapi_rest.skill_status(skill)
        except Exception:
            return ""
        last = (status.get("manifest") or {}).get("lastUpdateRequest") or {}
        state = last.get("status", "")
        if state == "SUCCEEDED":
            return ""
        if state == "FAILED":
            return ("; ".join(e.get("message", "")
                              for e in last.get("errors") or [])
                    or "manifest validation failed")
        if attempt + 1 < tries:
            time.sleep(delay)
    return ""


def existing_music_skills(exclude: str = "") -> list[dict]:
    """Music skills already on the vendor, which would compete for the alias.

    Alexa routes an invocation across every enabled music skill, so a leftover
    from an earlier install fights the new skill for the same words. Surfaced
    before creation, not discovered by ear afterwards.
    """
    if not smapi_rest.connected():
        return []
    found = []
    try:
        for summary in smapi_rest.list_skills():
            if summary.get("skillId") == exclude:
                continue
            if "music" not in (summary.get("apis") or []):
                continue
            name = next(iter((summary.get("nameByLocale") or {}).values()), "")
            found.append({"id": summary.get("skillId", ""), "name": name,
                          "stage": summary.get("stage", "")})
    except Exception:
        return []
    return found


# --------------------------------------------------------------------------
# connecting the Amazon developer account
# --------------------------------------------------------------------------

# The verifier and state for an in-flight consent round trip. In memory on
# purpose: it is valid for one redirect, and writing it down would leave the
# thing that binds the authorization code to this process sitting on disk.
_PENDING: dict = {}

# How long a consent request stays answerable. Long enough to log in to Amazon
# and read a permissions page, short enough that a link left in a browser
# history is not a standing invitation.
CONSENT_TTL = 900


def begin_amazon_link(client_id: str, client_secret: str,
                      origin: str = "") -> Outcome:
    """Start the consent round trip, and return the URL to open.

    `origin` is where the operator is driving setup from, remembered only so
    the callback page can offer a way back. Amazon must redirect to the public
    https hostname, which is usually not the address the admin plane is being
    used on.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not (client_id and client_secret):
        return Outcome(False, "Both the client ID and the client secret are "
                              "needed.")
    if not smapi_rest.redirect_uri().startswith("https://"):
        return Outcome(False, "The public base URL must be an https origin "
                              "before connecting, because Amazon will only "
                              "redirect back to https.")

    url, state, verifier = smapi_rest.begin(client_id)
    _PENDING.clear()
    _PENDING.update({"state": state, "verifier": verifier, "at": time.time(),
                     "client_id": client_id, "client_secret": client_secret,
                     "origin": origin})
    return Outcome(True, url)


def pending_origin() -> str:
    return _PENDING.get("origin", "")


def complete_amazon_link(code: str, state: str) -> Outcome:
    """Exchange the authorization code Amazon sent back.

    The state and the PKCE verifier are what protect this: the callback is
    reachable from anywhere, because the operator's browser is on the public
    internet and cannot be judged by the network rule the rest of setup uses.
    Both are held only by this process and both are good for exactly one
    attempt, which is why the pending record is cleared before anything is
    checked rather than after a successful exchange.
    """
    pending = dict(_PENDING)
    _PENDING.clear()

    if not pending or not state or not _constant_equal(
        state, pending.get("state", "")
    ):
        return Outcome(False, "That response did not match a consent request "
                              "from this service. Start again from setup.")
    if time.time() - pending.get("at", 0) > CONSENT_TTL:
        return Outcome(False, "The consent request expired. Start again from "
                              "setup.")
    try:
        smapi_rest.complete(code, pending["client_id"],
                            pending["client_secret"], pending["verifier"])
    except smapi_rest.SmapiError as exc:
        return Outcome(False, error_text(exc))
    return Outcome(True, "Connected to Amazon.")


def _constant_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def disconnect_amazon() -> Outcome:
    smapi_rest.forget_credentials()
    return Outcome(True, "Disconnected. The refresh token was deleted.")


# --------------------------------------------------------------------------
# the steps
# --------------------------------------------------------------------------


def create_skill(*, alias: str, public_base: str, vendor: str = "") -> Outcome:
    """Register the music skill against the Amazon developer account."""
    current = store.load()
    if not smapi_rest.connected():
        return Outcome(False, "Connect to Amazon first.")
    if not public_base.startswith("https://"):
        return Outcome(False, "The public base URL must be an https origin "
                              "before a skill can point at it.")
    if current.get("skill_id"):
        # A resubmitted form, a stale browser tab, or MA re-rendering the
        # config page must not be able to mint duplicates on the account.
        return Outcome(True, f"The skill already exists: {current['skill_id']}. "
                             "Nothing was created.")

    word = (alias or current.get("alias") or "Ampere").strip()
    manifest = smapi.manifest(
        name=word.title(),
        public_base=public_base,
        cert_type=current.get("cert_type") or "Trusted",
        aliases=[word.lower()],
    )
    try:
        created = smapi_rest.create_skill(manifest, vendor)
    except Exception as exc:
        return Outcome(False, error_text(exc))

    if problem := manifest_verdict(created):
        # A skill that failed validation exists in name only: it lists, it
        # 404s for catalog association, and nothing ever calls its endpoint.
        # Better deleted now, with Amazon's own words shown, than discovered
        # two steps later.
        try:
            smapi_rest.delete_skill(created)
        except Exception:
            pass
        return Outcome(False, f"Amazon rejected the manifest: {problem}")

    store.update(skill_id=created, alias=word, vendor_id=vendor)
    setup_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    # A recreated skill starts with no catalog associations even though the
    # catalogs themselves survived on the vendor. Re-binding here keeps the
    # catalogs step honest about already being done.
    for catalog_id in (current.get("catalogs") or {}).values():
        try:
            smapi_rest.associate_catalog(created, catalog_id)
        except Exception:
            pass
    return Outcome(True, f"Created {created}")


def forget_skill() -> Outcome:
    """Discard the record of a skill Amazon no longer has.

    The record, not the skill: there is nothing left on Amazon's side to
    delete. Enablement is cleared with it, because it described the vanished
    skill.
    """
    store.update(skill_id="", enabled=False)
    setup_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    return Outcome(True, "Stale record discarded.")


def create_catalogs() -> Outcome:
    """Create the five catalogs and associate each with the skill."""
    current = store.load()
    skill = skill_id(current)
    if not skill:
        return Outcome(False, "Create the skill first.")

    catalogs = dict(current.get("catalogs") or {})
    # Orphans from an earlier run: a catalog created moments before its
    # association failed was never recorded anywhere. Reusing by title means a
    # re-run heals instead of minting duplicates on the vendor.
    try:
        existing = {c.get("title", ""): c.get("id", "")
                    for c in smapi_rest.list_catalogs()}
    except Exception:
        existing = {}

    rows = []
    for kind, catalog_type in CATALOG_KINDS.items():
        title = f"Ampere {kind}"
        try:
            catalog_id = catalogs.get(kind) or existing.get(title, "")
            if not catalog_id:
                catalog_id = smapi_rest.create_catalog(title, catalog_type)
            # Recorded before association, so a failure there cannot orphan it.
            catalogs[kind] = catalog_id
            store.update(catalogs=catalogs)
            # Association has to happen before any upload or the content has
            # nowhere to resolve against. The PUT is idempotent, so running it
            # again for an already-associated catalog is a no-op, not an error.
            smapi_rest.associate_catalog(skill, catalog_id)
        except Exception as exc:
            rows.append({"kind": kind, "ok": False, "detail": error_text(exc)})
            continue
        rows.append({"kind": kind, "ok": True, "detail": catalog_id})

    store.update(catalogs=catalogs)
    ok = bool(rows) and all(row["ok"] for row in rows)
    made = sum(1 for row in rows if row["ok"])
    return Outcome(ok, f"{made} of {len(rows)} catalogs ready", rows)


class UploadStopped(Exception):
    """The caller asked for the upload to stop before it finished."""


def run_upload(*, progress: Callable[[str, float | None], None] | None = None,
               should_stop: Callable[[], bool] | None = None,
               cycle_after: bool = False) -> Outcome:
    """Read the whole library and push the five catalogs to Amazon.

    Minutes on a real collection, so `progress` is called throughout with a
    phase and a fraction, and `should_stop` is consulted between units of work.
    Neither is required: called with neither, this is an ordinary blocking
    function that returns when it is done.

    `cycle_after` exists because an upload unbinds the provider slot without
    reporting it. A person is told to cycle enablement afterwards; anything
    running unattended has to do it itself, or it silently breaks voice
    playback while every diagnostic stays green.
    """
    def tell(text: str, fraction: float | None = None) -> None:
        if should_stop is not None and should_stop():
            raise UploadStopped()
        if progress is not None:
            progress(text, fraction)

    uploaded_any = False
    try:
        # Asked before anything else, including the configuration check. A
        # caller that has already given up wants to hear that it stopped, not
        # a complaint about setup it was never going to reach.
        tell("starting")

        current = store.load()
        ids = catalog_ids(current)
        if not any(ids.values()):
            return Outcome(False, "Create the catalogs first.")

        tell("reading the library", 0.0)
        collected = catalog_sync.collect(progress=lambda text, fraction=None: tell(text, fraction))
        saved = store.load().get("catalog_hashes") or {}
        rows, uploads = [], dict(current.get("uploads") or {})

        for index, (kind, entities) in enumerate(collected.items()):
            catalog_id = ids.get(kind)
            if not catalog_id or not entities:
                continue
            final, hashes = catalog_sync.apply_timestamps(kind, entities, saved)
            if kind in uploads and saved.get(kind) == hashes:
                # Nothing changed since the last accepted upload. Skipping is
                # what makes a schedule safe: only real deltas spend one of the
                # rate-limited upload slots.
                rows.append({"kind": kind, "ok": True,
                             "detail": "unchanged, skipped"})
                continue
            tell(f"uploading {kind} ({len(final)} entities)",
                 index / max(1, len(collected)))
            payload = json.dumps({
                "type": catalog_sync.TYPES[kind], "version": 2.0,
                "locales": catalog_sync.LOCALES, "entities": final,
            }).encode()
            try:
                upload_id = smapi_rest.upload_catalog(catalog_id, payload)
            except Exception as exc:
                rows.append({"kind": kind, "ok": False,
                             "detail": error_text(exc)})
                continue
            saved[kind] = hashes
            uploads[kind] = upload_id
            uploaded_any = True
            rows.append({"kind": kind, "ok": True,
                         "detail": f"{len(final)} entities, upload {upload_id}"})

        store.update(catalog_hashes=saved, uploads=uploads)

        if cycle_after and uploaded_any:
            tell("cycling enablement", 1.0)
            outcome = cycle_enablement(skill_id())
            rows.append({"kind": "enablement", "ok": outcome.ok,
                         "detail": outcome.detail})

    except UploadStopped:
        return Outcome(False, "Stopped before the upload finished. Nothing was "
                              "recorded; run it again when you are ready.")
    except Exception as exc:
        # Logged as well as returned, because a run started from a browser tab
        # that is then closed, or from a scheduled task nobody is watching,
        # failed here in complete silence: the whole library crawl ran, hit the
        # container's memory ceiling, and left nothing behind but a log that
        # stopped mid-crawl.
        logger.exception("catalog upload failed")
        return Outcome(False, f"Could not read the library: {exc}")

    ok = bool(rows) and all(row["ok"] for row in rows)
    return Outcome(ok, f"{sum(1 for r in rows if r['ok'])} of {len(rows)} "
                       "catalogs uploaded", rows)


def cycle_enablement(skill: str) -> Outcome:
    """Re-provision the skill, then delete and set the enablement.

    The manifest re-put is the load-bearing part, found the hard way: a freshly
    created skill can sit half provisioned on Amazon's side, where searches
    route and resolve but Playback.Initiate never comes, and no amount of
    enablement cycling alone fixes it. Re-putting the manifest forces the
    music-provider provisioning to run again; cycling on top of that fresh
    registration is what actually restored playback. The delete before set
    stays required as well: a catalog upload unbinds the provider slot without
    reporting it.
    """
    if not skill:
        return Outcome(False, "No skill id yet.")
    try:
        smapi_rest.update_manifest(skill, smapi_rest.get_manifest(skill))
        if problem := manifest_verdict(skill, tries=10, delay=1.5):
            return Outcome(False, f"Re-provisioning failed: {problem}")
        nudged = "Re-provisioned and enabled"
    except Exception:
        # The plain cycle is still worth doing when the nudge cannot run.
        nudged = "Enabled (re-provisioning was skipped)"
    try:
        try:
            smapi_rest.delete_enablement(skill)
        except smapi_rest.SmapiError:
            pass  # Not enabled is the normal state here, not a failure.
        smapi_rest.set_enablement(skill)
    except Exception as exc:
        return Outcome(False, error_text(exc))
    store.update(enabled=True, enabled_at=time.time())
    return Outcome(True, f"{nudged} for development.")


def disable_skill(skill: str) -> Outcome:
    if not skill:
        return Outcome(False, "No skill id yet.")
    try:
        smapi_rest.delete_enablement(skill)
    except Exception as exc:
        return Outcome(False, error_text(exc))
    store.update(enabled=False)
    return Outcome(True, "Disabled. Alexa will not route to this skill until "
                         "it is enabled again.")
