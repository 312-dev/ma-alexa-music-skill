"""Ampere player provider: Echo devices and speaker groups as MA players.

Targets Music Assistant 2.9.10 (models package music-assistant-models
1.1.129.post1). MA's provider API is not stable between releases; in 2.9.x a
device is a subclass of music_assistant.models.player.Player with instance
methods, registered by the provider, and the older provider-level
cmd_play/cmd_pause surface no longer exists.

How this differs from the upstream `alexa` provider, which this is meant to
become a mode of rather than a competitor to:

  - Upstream pushes one stream URL per queue to a companion API and then says
    "ask music assistant to play audio". Alexa sees a single opaque stream, so
    the queue must be flow-mode: one long file, no track boundaries, and the
    Alexa app shows one entry for the whole session.
  - This publishes the whole track list to the Ampere bridge, which serves it
    to Alexa as a real Music Skill queue. Alexa gets discrete items with their
    own title, artist, album and art, which is why requires_flow_mode is False
    here and True there.

Auth and discovery are deliberately identical to upstream: same alexapy login,
same cookie file under {storage_path}/.alexa, same MUSIC_SKILL filter. If both
providers are configured for the same account they share the stored session.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import secrets
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from alexapy import AlexaAPI, AlexaLogin
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import LoginFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from . import core
from .bridge import BridgeClient, BridgeError, LocalBridge
from .stream_ref import encode_ref, is_live
from .stream_route import MediaStreamRoute
from .utterance import custom_command, sanitize
from .webserver import DEFAULT_PORT, AmpereWebServer
from . import wizard

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_AMAZON_URL = "url"
CONF_OTP_SECRET = "secret"
CONF_BRIDGE_URL = "bridge_url"
CONF_ADMIN_TOKEN = "admin_token"
CONF_ALIAS = "alias"
CONF_HANDOFF_PHRASE = "handoff_phrase"
CONF_EXPOSE_GROUPS = "expose_groups"
CONF_MA_SOURCE = "ma_source"
CONF_SERVE_ENDPOINT = "serve_endpoint"
CONF_ENDPOINT_PORT = "endpoint_port"
CONF_PUBLIC_BASE = "public_base"
CONF_SUBSONIC_URL = "subsonic_url"
CONF_SUBSONIC_USER = "subsonic_user"
CONF_SUBSONIC_PASSWORD = "subsonic_password"
CONF_SIGNING_KEY = "signing_key"
CONF_ADMIN_SECRET = "admin_secret"
CONF_CLIENT_SECRET = "client_secret"
CONF_LINK_SECRET = "link_secret"

# Music providers whose item_id is a Subsonic song id. The bridge streams from
# one Subsonic server, so a queue can only carry tracks that server holds; a
# Tidal or Spotify item in the MA queue has no id the bridge could resolve and
# is dropped rather than silently substituted.
SUBSONIC_DOMAINS = ("opensubsonic", "subsonic")

# Nothing at the provider level: no player syncing, no group creation. Alexa
# owns its own groups and MA must not try to make more.
SUPPORTED_FEATURES: set[ProviderFeature] = set()

# SEEK is declared even though there is no seek in alexapy and
# Alexa.SeekController is a Video API that does not apply to speakers. It works
# by a different route: MA implements seek as play_media on the current item
# with an offset, and Alexa's own Item schema carries
# `stream.offsetInMilliseconds`, so the position rides along with the queue the
# seek republishes. See AmperePlayer._seek_offset_ms.
#
# Declaring it is not what makes MA offer the scrubber, either. PlayerQueues
# .seek never consults the feature set, so the control was always live; before
# the offset was threaded through it silently restarted the track.
PLAYER_FEATURES: set[PlayerFeature] = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.SEEK,
    # ENQUEUE is declared and implemented as a no-op. Alexa is handed the whole
    # queue at play_media and advances it itself, so there is nothing to
    # enqueue; but declaring it is what stops MA from re-issuing play_media per
    # track, and it is what makes requires_flow_mode False by default.
    PlayerFeature.ENQUEUE,
}

# Alexa reports position on a device that may be asleep. Ten seconds is often
# enough to notice a track change without turning the Amazon API into a
# heartbeat.
POLL_INTERVAL = 10

# How many consecutive failed state polls before a player is hidden. One is too
# few: an Echo that is asleep or briefly unreachable still plays when something
# is sent to it, and hiding it takes a working speaker off the list. Three
# misses at POLL_INTERVAL is half a minute of silence, which is a real fault.
POLL_FAILURES_BEFORE_UNAVAILABLE = 3


def build_stamp() -> str:
    """A short digest of this provider's own source files.

    The most expensive class of confusion in this project has not been wrong
    code, it has been *right code that was not running*: a bind mount onto a
    path that did not exist, a container that came back without re-reading, an
    edit that never left the laptop. None of those raise anything, and every
    one of them presents as "the fix did not work".

    So the provider says which build it is at load. `tools/build_stamp.py`
    computes the same digest over a working copy, and the two either match or
    the deployment is not what was written.
    """
    digest = hashlib.sha256()
    for path in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AmpereAlexaProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for setting up this provider.

    Two halves. First the ordinary settings, which are the same fields the
    standalone deployment reads from its environment. Then the setup rail: the
    eight numbered steps that register a skill with Amazon, which used to be a
    web wizard of its own and is now a group of entries per step.

    An action runs before the form is rebuilt, so a button's effect is already
    visible in the step it belongs to by the time the page comes back.
    """
    values = values or {}
    if action:
        # Blocking: SMAPI is a series of HTTPS round trips and the library
        # crawl is Subsonic calls. MA calls this from the event loop, so it
        # goes to a worker thread rather than stalling playback for everyone.
        await asyncio.to_thread(
            wizard.run, action, dict(values),
            str(values.get(CONF_PUBLIC_BASE) or core.PUBLIC_BASE or ""),
        )

    return (*_settings_entries(), *await asyncio.to_thread(wizard.entries))


def _settings_entries() -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(
            key=CONF_SERVE_ENDPOINT,
            type=ConfigEntryType.BOOLEAN,
            label="Serve the Alexa endpoint from Music Assistant",
            default_value=True,
            description=(
                "On, Ampere listens on its own port in this process and no "
                "separate service is needed. Point your reverse proxy at that "
                "port. Off, it expects a standalone Ampere deployment at the "
                "bridge URL below, which is how it works without Music "
                "Assistant."
            ),
            # The listener is raised at init, so it cannot be moved under a
            # running provider.
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_PUBLIC_BASE,
            type=ConfigEntryType.STRING,
            label="Public base URL",
            description=(
                "The https:// origin Amazon reaches this service on, for "
                "example https://music.example.com. Every stream and cover "
                "art URL Amazon fetches is built from it, so a wrong value "
                "does not fail here: it fails later as audio that will not "
                "play, with nothing in any log to say why."
            ),
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_SUBSONIC_URL,
            type=ConfigEntryType.STRING,
            label="Music server URL",
            description=(
                "Your Subsonic-compatible server, for example "
                "http://navidrome.local:4533. Ampere streams every track from "
                "here; it is not required to be reachable from the internet, "
                "because Amazon fetches audio through Ampere rather than "
                "directly."
            ),
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_SUBSONIC_USER,
            type=ConfigEntryType.STRING,
            label="Music server username",
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_SUBSONIC_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Music server password",
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_ENDPOINT_PORT,
            type=ConfigEntryType.INTEGER,
            label="Endpoint port",
            default_value=DEFAULT_PORT,
            description=(
                "The port Ampere listens on. A port of its own rather than "
                "one of Music Assistant's, because MA's webservers have no "
                "HTTP authentication and this one faces the public internet."
            ),
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_BRIDGE_URL,
            type=ConfigEntryType.STRING,
            label="Ampere bridge URL",
            # No default. A placeholder default is worse than none here: it
            # saves as a real value, so a field left untouched looks configured
            # and fails later at publish time with a confusing error.
            description=(
                "Base URL of a standalone Ampere bridge, for example "
                "http://127.0.0.1:5056. Only used when the endpoint above is "
                "not served from Music Assistant."
            ),
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=False,
        ),
        ConfigEntry(
            key=CONF_ADMIN_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Bridge admin token",
            description="The bridge's ADMIN_TOKEN. Sent as X-Admin-Token.",
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=False,
        ),
        ConfigEntry(
            key=CONF_ALIAS,
            type=ConfigEntryType.STRING,
            label="Skill invocation name",
            default_value="ampere",
            description=(
                "The skill's invocation name, spoken as "
                "'ask <name> to play ...'. Must match the skill manifest."
            ),
            required=True,
        ),
        ConfigEntry(
            key=CONF_HANDOFF_PHRASE,
            type=ConfigEntryType.STRING,
            label="Handoff phrase",
            default_value="music assistant",
            description=(
                "The words that mean 'the queue Music Assistant just "
                "published'. Must match MA_HANDOFF_PHRASE on the bridge, and "
                "must not collide with an artist, album or track in the "
                "library, because Alexa resolves content before it routes to a "
                "provider."
            ),
            required=True,
        ),
        ConfigEntry(
            key=CONF_EXPOSE_GROUPS,
            type=ConfigEntryType.BOOLEAN,
            label="Expose Alexa speaker groups as players",
            default_value=True,
            description=(
                "Multi-room works by naming the group in the utterance, so "
                "each Alexa speaker group can be its own MA player."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_MA_SOURCE,
            type=ConfigEntryType.BOOLEAN,
            label="Play tracks that are not on the Subsonic server",
            default_value=True,
            description=(
                "Stream anything with no Subsonic id, such as a Spotify or "
                "Tidal track, from Music Assistant itself. Tracks that do have "
                "a Subsonic id keep using it, because that source is seekable "
                "and this one is not: Music Assistant serves realtime audio "
                "with no length and no byte ranges, so seeking is unavailable "
                "on these tracks and moving them between rooms may restart "
                "them. Turn this off to go back to skipping such tracks."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_AMAZON_URL,
            type=ConfigEntryType.STRING,
            label="Amazon domain",
            default_value="amazon.com",
            required=True,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Amazon account email",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Amazon account password",
            required=True,
        ),
        ConfigEntry(
            key=CONF_OTP_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Amazon two-factor secret",
            description=(
                "The TOTP seed from Amazon's authenticator-app setup, not a "
                "six digit code. Leave empty if two-factor is off."
            ),
            required=False,
        ),
    )


class AlexaDevice:
    """The handful of fields alexapy's AlexaAPI actually reads off a device.

    Same shape the upstream alexa provider uses, deliberately: AlexaAPI reaches
    into _device_type and device_serial_number directly.
    """

    _device_type: str = ""
    device_serial_number: str = ""
    _device_family: str = ""
    _cluster_members: list[str]
    _locale: str = "en-US"


class AmperePlayer(Player):
    """An Echo device, or an Alexa speaker group, as a Music Assistant player."""

    # Class level so a player is never one attribute short of pollable,
    # whichever way it was constructed. refresh() deliberately leaves it alone:
    # a rediscovery pass is not evidence about whether the device is answering.
    _poll_failures = 0
    # Whether the last polled title resolved to an MA queue item. Class level
    # for the same reason as _poll_failures.
    _matched_last = False

    def __init__(
        self,
        provider: AmpereAlexaProvider,
        player_id: str,
        device: AlexaDevice,
        name: str,
        *,
        is_group: bool = False,
        speaker: AlexaDevice | None = None,
        member_ids: list[str] | None = None,
    ) -> None:
        super().__init__(provider, player_id)
        self.device = device
        # A speaker group has no dialog interface: sending it a text command
        # does nothing at all. The command goes to a member instead, and the
        # group is named in the sentence. `speaker` is that member.
        self.speaker = speaker or device
        self.is_group = is_group
        self.group_name = name if is_group else ""

        self._attr_name = name
        self._attr_type = PlayerType.GROUP if is_group else PlayerType.PLAYER
        self._attr_supported_features = set(PLAYER_FEATURES)
        self._attr_device_info = DeviceInfo(
            model="Alexa speaker group" if is_group else "Echo",
            manufacturer="Amazon",
        )
        self._attr_available = True
        # None for a group, not True. MA reads a group's `powered` to decide
        # whether its members are captured, and a group that claims to be
        # powered captures them permanently: every Echo in Whole Apartment
        # vanished from the player picker whether or not the group was playing,
        # while still appearing under Settings > Players. None makes MA fall
        # through to is_active_session below, which answers the question it is
        # really asking. A plain Echo is powered, and says so.
        self._attr_powered = None if is_group else True
        self._attr_needs_poll = True
        self._attr_poll_interval = POLL_INTERVAL
        self._attr_group_members = list(member_ids or [])

        # Alexa reports what is playing by title, not by any id we gave it, so
        # this is how a polled title gets back to the MA queue item it came
        # from. Best effort, and duplicated titles resolve to the first.
        self._titles_to_items: dict[str, str] = {}

    def refresh(
        self,
        device: AlexaDevice,
        name: str,
        *,
        speaker: AlexaDevice | None = None,
        member_ids: list[str] | None = None,
    ) -> None:
        """Take the fields a later discovery pass can legitimately change.

        Everything a rediscovery can tell us that the constructor set, and
        nothing else: the id, the group flag and the feature set are fixed for
        the life of the player, and the title index belongs to whatever queue
        is currently playing.
        """
        self.device = device
        self.speaker = speaker or device
        self._attr_name = name
        if self.is_group:
            self.group_name = name
        self._attr_group_members = list(member_ids or [])
        self._attr_available = True

    @property
    def provider_instance(self) -> AmpereAlexaProvider:
        return cast("AmpereAlexaProvider", self.provider)

    @property
    def requires_flow_mode(self) -> bool:
        """Discrete tracks, not one long stream.

        This is the entire point of the provider. MA's default would be False
        anyway now that ENQUEUE is declared, but it is stated outright because
        a later change to that default would silently undo it.
        """
        return False

    @property
    def is_active_session(self) -> bool:
        """Whether this group is holding its members right now.

        MA asks this to decide whether the member Echoes are owned by the group
        and should therefore be hidden from the player picker.

        Always False, including for a group, which is not what the name
        suggests and needs the reason written down.

        Capture exists so MA does not issue commands to a speaker it is
        currently streaming to as part of a group **MA itself formed**: the
        members are mid-sync and talking to one directly would break the
        session. Ampere forms nothing. An Alexa Whole Home Audio group is
        Amazon's, assembled and dissolved by Amazon, and a command sent to a
        member is a request Amazon knows how to service. There is no MA-side
        session to protect, so there is nothing to capture.

        Getting this wrong is expensive and hard to read as a bug: capturing
        members deletes them from the player picker while leaving them under
        Settings > Players, so they look present everywhere an operator would
        check. Two intermediate versions of this method, holding members while
        playing-or-paused and then while playing, each hid every Echo in the
        house for as long as the group was in the corresponding state.
        """
        return False

    @property
    def api(self) -> AlexaAPI:
        """AlexaAPI bound to the device that can be spoken to."""
        return AlexaAPI(self.speaker, self.provider_instance.login)

    @property
    def state_api(self) -> AlexaAPI:
        """AlexaAPI bound to the device that reports playback.

        For a group that is the group itself, which does answer /api/np/player
        even though it will not accept a text command.
        """
        return AlexaAPI(self.device, self.provider_instance.login)

    # -- playback ------------------------------------------------------------

    async def play_media(self, media: PlayerMedia) -> None:
        """Publish MA's queue to the bridge, then tell Alexa to play it."""
        provider = self.provider_instance
        items = provider.queue_items(media)
        tracks, self._titles_to_items = provider.publish_tracks(items)

        if not tracks:
            raise BridgeError(
                "nothing in this queue can be streamed: no track is on the "
                "Subsonic server, and none could be served from Music "
                "Assistant either"
            )

        # The setting is a comma separated list so a phrase that collides with
        # something else on the account can be moved off without giving up the
        # old one, which Alexa may still be holding. The bridge accepts any of
        # them; only one can be said, and it is the first.
        label = _first_phrase(
            provider.config.get_value(CONF_HANDOFF_PHRASE), "music assistant"
        )
        alias = str(provider.config.get_value(CONF_ALIAS) or "ampere")
        name = media.title or (items[0].name if items else "") or label

        offset_ms = self._seek_offset_ms(media)
        await provider.bridge.publish_queue(tracks, name, offset_ms)

        # The published queue is claimed by phrase, not by id. There is no
        # utterance that names an arbitrary track list, so the bridge maps one
        # fixed phrase onto whatever was published most recently and pins it to
        # a concrete contentId at the moment Alexa asks. Two players starting
        # at the same instant is the one case this loses; see README.
        text = custom_command(alias, label, self.group_name or None)
        self.logger.debug("run_custom on %s: %s", self.speaker.device_serial_number, text)
        await self.api.run_custom(text)

        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    def _seek_offset_ms(self, media: PlayerMedia) -> int:
        """Where in the first published track playback should start.

        MA has no seek command for a player that does not stream through it.
        `PlayerQueues.seek` calls `play_index(..., seek_position=N)`, which puts
        the offset on the queue item's streamdetails and then calls play_media
        exactly as an ordinary play does. Nothing in PlayerMedia carries it, so
        the queue is where it has to be read from.

        Left unread, a seek republished the queue and Alexa started it at zero:
        the song restarted while MA's own clock showed the position dragged to,
        and the disagreement made the next pause press look like it did
        nothing. queue_items already slices from current_index, so the offset
        always belongs to the track being published first.
        """
        queue_id = media.source_id
        if not queue_id:
            return 0
        queues = self.mass.player_queues

        # The item being played, addressed by id, not queue.current_item.
        # play_index loads the item first and assigns current_item afterwards,
        # so reading the queue's idea of "current" during a seek can still
        # return the previous item, whose streamdetails carry no offset. That
        # read as zero and republished the queue from the top, which is a seek
        # that restarts the song: observed 2026-08-02, one seek in a run of
        # them, the rest correct.
        item = queues.get_item(queue_id, media.queue_item_id)
        if item is None:
            queue = queues.get(queue_id)
            item = getattr(queue, "current_item", None)

        details = getattr(item, "streamdetails", None)
        # Seconds as a float on MA's side, milliseconds as an int on Alexa's.
        return max(0, int(float(getattr(details, "seek_position", 0) or 0) * 1000))

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Deliberately nothing.

        Alexa was handed the whole list at play_media and advances it itself.
        The feature is declared so MA does not fall back to re-issuing
        play_media per track, which would restart the queue on every track
        boundary.
        """

    async def play(self) -> None:
        await self.state_api.play()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        await self.state_api.pause()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        await self.state_api.stop()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def next_track(self) -> None:
        await self.state_api.next()

    async def previous_track(self) -> None:
        await self.state_api.previous()

    async def volume_set(self, volume_level: int) -> None:
        # alexapy takes 0..1 and multiplies by 100 on the way out.
        await self.state_api.set_volume(max(0, min(100, volume_level)) / 100)
        self._attr_volume_level = volume_level
        self.update_state()

    # -- state ---------------------------------------------------------------

    async def poll(self) -> None:
        """Read playback state back off Alexa.

        Once Alexa is playing it owns the position, and nothing MA does to its
        own queue afterwards is reflected on the speaker. This is the direction
        the truth actually flows in.
        """
        try:
            raw = await self.state_api.get_state()
        except Exception as err:  # alexapy raises a wide range of its own
            self._poll_failures += 1
            # Warning, not debug. Nothing runs this at debug level, so the old
            # line meant a player could vanish from Music Assistant with no
            # trace anywhere of why.
            if self._poll_failures == 1:
                self.logger.warning(
                    "state poll failed for %s (%s); keeping it available for "
                    "now", self.name, err)
            if self._poll_failures >= POLL_FAILURES_BEFORE_UNAVAILABLE:
                # An Echo that did not answer is usually an Echo that is asleep
                # or briefly unreachable, and playing to one wakes it. Hiding it
                # on a single miss removes a speaker the user can still use.
                if self._attr_available:
                    self.logger.warning(
                        "%s has failed %d state polls in a row, marking it "
                        "unavailable", self.name, self._poll_failures)
                self._attr_available = False
                self.update_state()
            return

        if self._poll_failures:
            self.logger.info("%s is answering state polls again", self.name)
        self._poll_failures = 0
        self._attr_available = True
        info = (raw or {}).get("playerInfo") or {}
        self._apply_state(info)
        self.update_state()

    def _queue_titles(self) -> list[str]:
        """Track names in this player's MA queue, for diagnosing a miss."""
        try:
            items = self.mass.player_queues.items(self.player_id) or ()
        except Exception:
            return []
        return [getattr(i, "name", "") or "" for i in items]

    def _queue_item_for(self, title: str) -> str | None:
        """Which MA queue item the polled title corresponds to.

        This is what lets MA's queue follow along. Alexa advances the queue
        itself and never tells MA, so the only way MA's own index moves is by
        recognising the title Alexa reports as one of its own items. Alexa
        reports what is playing by name and never by anything it was handed, so
        a name is all there is to match on. Duplicate titles resolve to the
        first.

        The map built at play_media is the fast path. It lives only in memory,
        so it is empty after a restart and after a queue transferred in from
        another player, and those are exactly the moments MA's queue has no
        other way to catch up: observed with the group audibly on one track
        while MA's UI showed the track the queue had arrived holding. So the
        live queue is scanned as a fallback.
        """
        wanted = title.lower()
        if known := self._titles_to_items.get(wanted):
            return known
        try:
            items = self.mass.player_queues.items(self.player_id)
        except Exception:  # no queue for this player yet
            return None
        for item in items or ():
            if wanted in _queue_item_titles(item):
                return item.queue_item_id
        return None

    def _apply_state(self, info: dict[str, Any]) -> None:
        # An empty payload is not a claim that nothing is playing. A speaker
        # group answers with no playerInfo at all while its members are audibly
        # playing, and reading that as IDLE is what stopped the position
        # advancing and left MA's optimistic guess as the only clock.
        if not info:
            return

        state = (info.get("state") or "").upper()
        before = self._attr_playback_state
        self._attr_playback_state = {
            "PLAYING": PlaybackState.PLAYING,
            "PAUSED": PlaybackState.PAUSED,
            "IDLE": PlaybackState.IDLE,
        }.get(state, PlaybackState.IDLE)

        # Logged because a group's state decides whether its members are hidden
        # from the player picker, and every theory about what a cluster device
        # reports has so far been wrong. On change only: this runs per poll.
        if self.is_group and self._attr_playback_state != before:
            self.logger.info(
                "group %s reports %r -> %s (holding members: %s)",
                self.name, state or "<no state>", self._attr_playback_state,
                self.is_active_session)

        volume = info.get("volume") or {}
        if isinstance(volume.get("volume"), int):
            self._attr_volume_level = volume["volume"]
        if isinstance(volume.get("muted"), bool):
            self._attr_volume_muted = volume["muted"]

        progress = info.get("progress") or {}
        text = info.get("infoText") or {}
        title = text.get("title") or ""
        previously_playing = self._attr_current_media is not None

        # mediaProgress and mediaLength are both in SECONDS. Measured
        # 2026-08-03 against a live Echo:
        #
        #   {'mediaLength': 226, 'mediaProgress': 11, 'allowScrubbing': False}
        #   {'mediaLength': 226, 'mediaProgress': 21, ...}   # 10s later
        #
        # They were divided by 1000 here, on the belief that they were
        # milliseconds. That made the position advance at a thousandth of real
        # time, which is what "the scrubber never moves" was; and it made
        # Alexa's duration round to zero, which was then hidden because the
        # carry-forward below quietly kept Music Assistant's own duration
        # instead. A wrong value that is never displayed is the hardest kind to
        # notice.
        self.logger.debug("progress on %s: %r", self.name, progress)
        elapsed = progress.get("mediaProgress")
        if isinstance(elapsed, (int, float)):
            self._attr_elapsed_time = float(elapsed)
            self._attr_elapsed_time_last_updated = time.time()

        if not title:
            # A poll with no title leaves current_media pointing at whatever
            # played last, which is a candidate explanation for MA showing a
            # stale track. Worth seeing, but only on the way in: an idle Echo
            # answers this way on every poll forever, and logging that turns a
            # signal into 36 lines a minute of noise.
            if previously_playing:
                self.logger.info(
                    "%s stopped reporting a track title (state %s) so its "
                    "media is left as it was", self.name, state or "<none>")
            return

        # Alexa omits fields it does not feel like sending, and omits more of
        # them around a transition or on a group. This is rebuilt from scratch
        # every poll, so anything absent used to be erased rather than left
        # alone: the duration visibly reset to zero on each resync, taking the
        # progress bar with it. A field Alexa did not mention is a field Alexa
        # said nothing about, so the last known value stands.
        previous = self._attr_current_media
        same_track = previous is not None and previous.title == title
        matched = self._queue_item_for(title)
        # Whether the title resolved to an MA queue item is the whole ball
        # game: PlayerQueues._update_queue_from_player bails out entirely when
        # it cannot parse an item id, so an unmatched title means the queue
        # index and the scrubber both stop dead. Logged when the track changes
        # and also when the match starts or stops working, because a queue can
        # be populated long after the track began.
        if not same_track or bool(matched) != self._matched_last:
            if matched:
                self.logger.info("%s is now playing %r; queue item %s",
                                 self.name, title, matched)
            else:
                self.logger.info(
                    "%s is now playing %r; NOT MATCHED against %d queue "
                    "item(s): %s", self.name, title, len(self._queue_titles()),
                    self._queue_titles()[:4] or "queue is empty")
        self._matched_last = bool(matched)

        def kept(value: Any, attribute: str) -> Any:
            if value not in (None, ""):
                return value
            return getattr(previous, attribute, None) if same_track else None

        # Seconds already; see the note on mediaProgress above.
        duration = progress.get("mediaLength")
        seconds = (int(duration)
                   if isinstance(duration, (int, float)) and duration > 0
                   else None)

        self._attr_current_media = PlayerMedia(
            uri=f"ampere://{self.player_id}/{title}",
            media_type=MediaType.TRACK,
            # Both of these, or neither counts. MA's
            # PlayerQueues._parse_player_current_item_id will only believe a
            # queue_item_id when source_id names the queue as well, and its
            # fallbacks parse a Sonos uri or an MA stream url, neither of which
            # an ampere:// uri can ever look like. Without source_id the queue
            # index never advances, so MA went on showing whatever track the
            # queue was holding when it arrived while the speakers played on:
            # observed with a group three tracks ahead of the display. A
            # player's own queue is keyed by its player_id.
            source_id=self.player_id,
            title=title,
            artist=kept(text.get("subText1"), "artist"),
            album=kept(text.get("subText2"), "album"),
            image_url=kept((info.get("mainArt") or {}).get("url"), "image_url"),
            duration=kept(seconds, "duration"),
            queue_item_id=kept(matched, "queue_item_id"),
        )


class AmpereAlexaProvider(PlayerProvider):
    """Discovers Echo devices and speaker groups and drives them via Ampere."""

    login: AlexaLogin
    bridge: BridgeClient

    def _minted(self, key: str, what: str, length: int = 32) -> str:
        """A secret this instance generates for itself, once, and keeps.

        Standalone Ampere takes these from environment variables. Music
        Assistant hands providers no environment, and there is nothing for
        anyone to decide about any of them, so rather than add fields nobody
        can answer they are generated here and stored encrypted in this
        provider's own config.

        Persisting is the whole point. Every one of these outlives the process
        that made it: the signing key validates URLs Amazon holds for hours, so
        a key that changed on reload would 403 Alexa partway through a queue it
        was already playing, unlink the account, and give a republished queue a
        new id that orphans the one Alexa is holding.

        Deliberately not visible settings. A field inviting someone to change
        one is a field inviting them to break every URL currently in flight.
        """
        stored = self.mass.config.get_raw_provider_config_value(
            self.instance_id, key
        )
        if stored:
            return str(stored)
        minted = secrets.token_hex(length)
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, key, minted, encrypted=True
        )
        self.logger.info("generated %s for this Ampere instance", what)
        return minted

    def _link_passphrase(self) -> str:
        """The passphrase typed once in the Alexa app to link the account.

        Minted like the others, but short and readable, because unlike them a
        person has to read this one off a screen and type it into a phone. A
        64 character hex string is technically fine and practically hostile.

        Nothing else guards the linking page, so it is not decoration: it is
        what stops anyone who finds the public endpoint from linking their own
        Alexa to this library.
        """
        return self._minted(CONF_LINK_SECRET, "an account linking passphrase",
                            length=6)

    async def handle_async_init(self) -> None:
        self.logger.info("ampere provider build %s", build_stamp())
        self._discovery_lock = asyncio.Lock()

        # Hosting the endpoint ourselves is the point of phase 5: the Alexa
        # skill, the audio proxy and the player provider all run in this
        # process. The separate Flask deployment is still supported, because
        # Ampere works for anyone with a Subsonic server whether or not they
        # run Music Assistant, and pointing at one is what turning this off
        # means.
        # Music Assistant does not hand providers an environment, so the
        # settings the standalone deployment reads from env vars are injected
        # here instead. Before anything starts serving, because every stream
        # and art URL Amazon fetches is built from the public base.
        #
        # The storage path is MA's, plus a directory of Ampere's own. Without
        # it the defaults are absolute paths chosen for a container this
        # service owned, and inside MA they land in MA's storage root beside
        # library.db.
        core.configure(
            public_base=str(self.config.get_value(CONF_PUBLIC_BASE) or ""),
            storage_path=str(pathlib.Path(self.mass.storage_path) / "ampere"),
            subsonic_url=str(self.config.get_value(CONF_SUBSONIC_URL) or ""),
            subsonic_user=str(self.config.get_value(CONF_SUBSONIC_USER) or ""),
            subsonic_password=str(self.config.get_value(CONF_SUBSONIC_PASSWORD) or ""),
            signing_key=self._minted(CONF_SIGNING_KEY, "a signing key"),
            admin_token=self._minted(CONF_ADMIN_SECRET, "an admin token"),
            oauth_client_id=f"ampere-{self.instance_id[:8]}",
            oauth_client_secret=self._minted(CONF_CLIENT_SECRET,
                                             "an account linking client secret"),
            oauth_link_secret=self._link_passphrase(),
        )

        self.webserver: AmpereWebServer | None = None
        serving = False
        if self.config.get_value(CONF_SERVE_ENDPOINT, True):
            port = int(self.config.get_value(CONF_ENDPOINT_PORT) or DEFAULT_PORT)
            self.webserver = AmpereWebServer(self.logger, port=port)
            serving = await self.webserver.start()

        # Publish where the endpoint actually is, which is not the same as
        # where it was configured to be. A published queue is a file on disk,
        # and the process that answers Alexa is the one that has to be able to
        # read it. If this process is not serving the endpoint then some other
        # deployment is, with its own state directory, and a local publish
        # would write a queue nobody can resolve: Alexa would be told to play a
        # contentId that, to the service answering, does not exist.
        if serving:
            # No HTTP hop to a service that is us. See LocalBridge for what
            # that removes besides the round trip.
            self.bridge = LocalBridge(executor=self.webserver._pool)
        else:
            self.bridge = BridgeClient(
                base_url=str(self.config.get_value(CONF_BRIDGE_URL) or ""),
                admin_token=str(self.config.get_value(CONF_ADMIN_TOKEN) or ""),
                session=self.mass.http_session,
            )

        self.stream_route = MediaStreamRoute(self.mass, self.logger)
        self.stream_route.register()

    async def unload(self, is_removed: bool = False) -> None:
        """Take the audio route and the endpoint down with the provider.

        The listener especially: a reload that left the old one holding the
        port would make the new one fail to bind, and the failure would look
        like a broken skill rather than a stale socket.
        """
        self.stream_route.unregister()
        if self.webserver is not None:
            await self.webserver.stop()
            self.webserver = None

    async def loaded_in_mass(self) -> None:
        await self._login()
        await self.discover_players()

    async def discover_players(self) -> None:
        """Enumerate Echo devices and speaker groups.

        Straight to Amazon through alexapy rather than through Home Assistant's
        Alexa Media Player integration. Both are the same session underneath,
        but HA only exposes what a media_player entity can express, and this
        needs the raw device list to tell a Whole Home Audio group from a
        speaker and to find a group's members. Going through HA is possible if
        MA runs as an HA add-on and the user would rather have one login: see
        README, "Running through Home Assistant instead".
        """
        async with self._discovery_lock:
            devices = await AlexaAPI.get_devices(self.login)
        if not devices:
            self.logger.warning("Amazon returned no devices")
            return

        expose_groups = bool(self.config.get_value(CONF_EXPOSE_GROUPS, True))
        by_serial = {d.get("serialNumber"): d for d in devices if d.get("serialNumber")}
        speakers: dict[str, AlexaDevice] = {}

        for raw in devices:
            if _is_group(raw):
                continue
            if "MUSIC_SKILL" not in (raw.get("capabilities") or []):
                # A device that cannot host a music skill cannot play this at
                # all, whatever else it can do.
                continue
            device = _device(raw)
            name = raw.get("accountName") or raw["serialNumber"]
            speakers[raw["serialNumber"]] = device
            await self._publish(
                f"{self.instance_id}:{raw['serialNumber']}", device, name
            )

        if not expose_groups:
            return

        for raw in devices:
            if not _is_group(raw):
                continue
            members = [m for m in (raw.get("clusterMembers") or []) if m in speakers]
            if not members:
                # A group whose members cannot host the skill has nothing that
                # can be spoken to, so it cannot be started.
                continue
            name = raw.get("accountName") or "Alexa group"
            spoken_to = _group_speaker(speakers, members)
            await self._publish(
                f"{self.instance_id}:{raw['serialNumber']}",
                _device(raw),
                name,
                is_group=True,
                speaker=speakers[spoken_to],
                member_ids=[f"{self.instance_id}:{m}" for m in members],
            )
            self.logger.debug("group %s speaks through %s", name, spoken_to)

    async def _publish(
        self,
        player_id: str,
        device: AlexaDevice,
        name: str,
        *,
        is_group: bool = False,
        speaker: AlexaDevice | None = None,
        member_ids: list[str] | None = None,
    ) -> None:
        """Register a player, or refresh the one already registered.

        Discovery runs again on every reload, so this is called repeatedly with
        the same ids. It deliberately does not hand a freshly built Player to
        `register_or_update` on those later passes: that method replaces the
        object in place without re-running registration, so the replacement
        never gets `set_initialized()` and drops straight out of `all_players`,
        which is what the UI and the API both read. The player stays in the
        controller's dict and vanishes from everywhere else.
        """
        existing = self.mass.players.get_player(player_id)
        if isinstance(existing, AmperePlayer):
            existing.refresh(
                device, name, speaker=speaker, member_ids=member_ids
            )
            existing.update_state()
            return

        await self.mass.players.register_or_update(
            AmperePlayer(
                self,
                player_id,
                device,
                name,
                is_group=is_group,
                speaker=speaker,
                member_ids=member_ids,
            )
        )

    # -- queue mapping -------------------------------------------------------

    def queue_items(self, media: PlayerMedia) -> list[QueueItem]:
        """The whole MA queue behind a PlayerMedia, from the current item on.

        MA hands play_media one item. Alexa needs the list, because it does the
        advancing. Items already played are left out: Alexa starts at the top
        of whatever it is given.
        """
        queue_id = media.source_id
        if not queue_id:
            return []
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return []
        items = self.mass.player_queues.items(queue_id)
        start = queue.current_index or 0
        return items[start:]

    def publish_tracks(
        self, items: list[QueueItem]
    ) -> tuple[list[str | dict[str, Any]], dict[str, str]]:
        """Map queue items to things the bridge can stream, with a title index.

        Two kinds come out, and the bridge accepts both in one list:

          - a plain string, which is a Subsonic song id and is what every
            queue published before phase 2 consisted of
          - a dict, which is a track the bridge fetches back out of Music
            Assistant through the route in `stream_route.py`

        **Subsonic wins whenever it is available**, and that is a deliberate
        preference rather than an ordering accident. Navidrome serves the audio
        as a finite file the bridge can proxy straight through; a Music
        Assistant track has to be buffered to disk first before it behaves the
        same way. Both end up seekable, but only one of them costs a copy, so
        routing a track through MA when Subsonic already has it would buy
        nothing and spend disk.

        The second return value is title -> queue_item_id, used later to guess
        which MA item a polled Alexa title corresponds to. Alexa reports what
        is playing by name and never by anything we handed it, so a name is all
        there is to match on.
        """
        allow_ma = bool(self.config.get_value(CONF_MA_SOURCE, True))
        tracks: list[str | dict[str, Any]] = []
        titles: dict[str, str] = {}
        from_ma = 0
        skipped = 0

        for item in items:
            entry: str | dict[str, Any] | None = _subsonic_id(item)
            if entry is None and allow_ma:
                entry = self._ma_track(item)
                if entry is not None:
                    from_ma += 1
            if entry is None:
                skipped += 1
                continue
            tracks.append(entry)
            if item.name:
                titles.setdefault(item.name.lower(), item.queue_item_id)

        if from_ma:
            self.logger.info(
                "%s of %s tracks are not on the Subsonic server and will be "
                "served from Music Assistant instead",
                from_ma, len(items),
            )
        if skipped:
            self.logger.warning(
                "%s of %s queue items cannot be streamed by the bridge at all "
                "and were left out",
                skipped, len(items),
            )
        return tracks, titles

    def _ma_track(self, item: QueueItem) -> dict[str, Any] | None:
        """A queue item described well enough for the bridge to serve it.

        Everything Alexa renders is carried here rather than looked up later,
        because the bridge has no way to ask Music Assistant what a track is
        called. Only the audio is deferred, and only because it has to be: a
        stream resolved at publish time would be stale by track twelve.
        """
        uri = getattr(item, "uri", "") or ""
        if not uri or "://" not in uri:
            return None

        media_item = getattr(item, "media_item", None)
        artists = getattr(media_item, "artists", None) or ()
        album = getattr(media_item, "album", None)
        duration = getattr(item, "duration", None) or getattr(media_item, "duration", 0)

        ref = encode_ref(uri)
        track: dict[str, Any] = {
            "source": "ma",
            # The uri, not a URL. The bridge holds the Music Assistant base
            # address itself, so nothing that arrives in a published queue can
            # redirect it somewhere else.
            "ref": ref,
            "title": getattr(media_item, "name", "") or item.name or uri,
            "artist": ", ".join(
                a.name for a in artists if getattr(a, "name", "")
            ),
            "album": getattr(album, "name", "") or "",
            # A station is endless, and Music Assistant sometimes reports a
            # duration for one anyway. Sending it would put a progress bar on
            # something with no end to progress towards. The bridge checks this
            # for itself as well, off the reference; this is here so nothing
            # downstream has to undo a number that should never have been sent.
            "duration": 0 if is_live(ref) else int(duration or 0),
        }

        # Only a URL Amazon can reach from the public internet. Spotify and
        # Tidal art is on their own CDNs and is exactly that, so it is handed
        # over as-is and never travels through the bridge. A local file's
        # artwork is not reachable and is simply left out; MA's image proxy is
        # on the tailnet and Amazon cannot fetch from there.
        image = getattr(item, "image", None) or getattr(media_item, "image", None)
        if image is not None and getattr(image, "remotely_accessible", False):
            if path := getattr(image, "path", ""):
                track["art_url"] = path

        return track

    # -- auth ----------------------------------------------------------------

    async def _login(self) -> None:
        """Log in to Amazon, reusing the upstream alexa provider's cookie file.

        Same path, same filename. Two providers configured for one account then
        share a session rather than racing each other into Amazon's rate limit,
        and this becoming a mode on the upstream provider later costs nobody a
        re-login.
        """
        username = str(self.config.get_value(CONF_USERNAME) or "")
        self.login = AlexaLogin(
            url=str(self.config.get_value(CONF_AMAZON_URL) or "amazon.com"),
            email=username,
            password=str(self.config.get_value(CONF_PASSWORD) or ""),
            outputpath=lambda path: path,
            otp_secret=str(self.config.get_value(CONF_OTP_SECRET) or ""),
        )

        cookie_dir = os.path.join(self.mass.storage_path, ".alexa")
        await asyncio.to_thread(os.makedirs, cookie_dir, exist_ok=True)
        self.login._cookiefile = [
            os.path.join(cookie_dir, f"alexa_media.{username}.pickle")
        ]

        # The cookies must be handed to login() explicitly. Setting _cookiefile
        # alone is not enough: alexapy's own loader cannot read the jar aiohttp
        # writes, so login() would fall through to a fresh credential login,
        # which Amazon refuses without the interactive proxy flow. Passing the
        # restored jar is what makes a saved session actually get reused.
        await self.login.login(cookies=await _load_cookie(self.login))
        if not await self.login.test_loggedin():
            # alexapy records why in .status (captcha_required,
            # securitycode_required, login_failed, ...). Losing that turns
            # every auth problem into the same unactionable sentence.
            detail = ", ".join(
                f"{key}={value}"
                for key, value in sorted((self.login.status or {}).items())
                if value and key not in ("password", "securitycode")
            )
            raise LoginFailed(
                f"Amazon rejected the login ({detail or 'no reason reported'}). "
                "A saved session from the upstream alexa provider is reused "
                "when present; otherwise set it up there first."
            )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _load_cookie(login: AlexaLogin) -> dict[str, str] | None:
    """Restore a saved Alexa session into the login's own aiohttp session.

    aiohttp writes its cookie jar as JSON, which alexapy's `load_cookie()`
    cannot parse, so the jar is loaded with aiohttp's own loader and the
    cookies handed back for `login(cookies=...)`. Loading it into the session
    jar rather than only returning it preserves the cookie domains, which the
    auth flow needs. Follows the upstream alexa provider deliberately: both
    read the same file, so whichever logs in first serves the other.
    """
    cookiefile = login._cookiefile[0] if login._cookiefile else None
    if not cookiefile or not await asyncio.to_thread(os.path.exists, cookiefile):
        return None
    if login._session is None:
        login._create_session()
    jar = login._session.cookie_jar
    if not isinstance(jar, aiohttp.CookieJar):
        return None
    try:
        await asyncio.to_thread(jar.load, cookiefile)
    except (OSError, EOFError, TypeError, ValueError, AttributeError):
        return None  # a corrupt jar is a fresh login, not a crash
    cookies = login._get_cookies_from_session()
    return cast("dict[str, str]", cookies) if cookies else None


def _queue_item_titles(item: Any) -> set[str]:
    """Every name a queue item could plausibly be reported under, lowercased.

    Measured 2026-08-02, and it is the whole reason queue following failed for
    every track: Music Assistant names a queue item `Artist - Title` while
    Alexa reports the title alone, so comparing the two directly never matched
    once. With no match MA cannot parse a current item id, and
    _update_queue_from_player returns early, which freezes the queue index and
    the scrubber and makes a later seek slice the queue from the wrong place.

    The media item's own name is the honest source. The composite is kept as a
    fallback, along with its tail, because a queue item that has lost its media
    item still has the string.
    """
    names: list[str] = []
    if media_name := getattr(getattr(item, "media_item", None), "name", ""):
        names.append(media_name)
    if composite := (getattr(item, "name", "") or ""):
        names.append(composite)
        # rsplit, not partition: a hyphen in the artist would otherwise take
        # the split with it and leave part of the artist on the title.
        head, sep, tail = composite.rpartition(" - ")
        if sep and head and tail:
            names.append(tail)
    return {n.strip().lower() for n in names if n and n.strip()}


def _group_speaker(speakers: dict[str, AlexaDevice], members: list[str]) -> str:
    """Pick the Echo that will be told to start a group.

    The constraint is only that the thing spoken to has to be a real Echo. A
    Whole Home Audio group is a cluster, not a device, and has no dialog
    interface of its own, so the command goes to a speaker and names the group
    in the sentence. Any speaker will do, **including one of the group's own
    members**.

    An earlier version of this required a speaker from outside the group, on
    the strength of a member-initiated attempt that resolved content and then
    never initiated. That measurement is withdrawn: it was taken while the
    binding detector was re-provisioning the skill underneath live sessions,
    which broke attempts indiscriminately. Requiring an outsider was worse than
    wrong, it made a group containing every Echo in the house look unstartable
    when it starts fine.

    Prefers a member so that Alexa's spoken confirmation lands in a room the
    music is about to play in, rather than surprising a different one. Sorted
    rather than first-seen, because discovery runs repeatedly and a speaker
    that moves between passes is a race nobody would find.
    """
    inside = sorted(s for s in members if s in speakers)
    return inside[0] if inside else sorted(speakers)[0]


def _first_phrase(value: Any, fallback: str) -> str:
    """The one phrase to say, out of a comma separated list of accepted ones.

    Saying the raw setting would utter the whole list, which resolves to
    nothing at all. The bridge accepts every entry; only the first is spoken.
    """
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return parts[0] if parts else fallback


def _is_group(raw: dict[str, Any]) -> bool:
    """Whole Home Audio groups come back in the same list as the speakers."""
    return (raw.get("deviceFamily") or "") == "WHA"


def _device(raw: dict[str, Any]) -> AlexaDevice:
    device = AlexaDevice()
    device._device_type = raw.get("deviceType", "")
    device.device_serial_number = raw.get("serialNumber", "")
    device._device_family = raw.get("deviceFamily", "")
    device._cluster_members = list(raw.get("clusterMembers") or [])
    return device


def _subsonic_id(item: QueueItem) -> str | None:
    """The Subsonic song id behind a queue item, if there is one."""
    track = getattr(item, "media_item", None)
    for mapping in getattr(track, "provider_mappings", None) or ():
        if mapping.provider_domain in SUBSONIC_DOMAINS and mapping.available:
            return mapping.item_id
    return None


def group_target(player: AmperePlayer) -> str | None:
    """The name to append as `on <target>`, or None for a single speaker.

    Exposed for tests and for anyone reading the utterance logic without
    Music Assistant installed.
    """
    return sanitize(player.group_name) or None
