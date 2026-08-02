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
import os
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

from .bridge import BridgeClient, BridgeError
from .utterance import custom_command, sanitize

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
    """Return config entries for setting up this provider."""
    return (
        ConfigEntry(
            key=CONF_BRIDGE_URL,
            type=ConfigEntryType.STRING,
            label="Ampere bridge URL",
            # No default. A placeholder default is worse than none here: it
            # saves as a real value, so a field left untouched looks configured
            # and fails later at publish time with a confusing error.
            description=(
                "Base URL of the Ampere bridge, for example "
                "http://127.0.0.1:5056 when it runs beside this container, or "
                "the public HTTPS host the Alexa skill endpoint points at."
            ),
            required=True,
        ),
        ConfigEntry(
            key=CONF_ADMIN_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Bridge admin token",
            description="The bridge's ADMIN_TOKEN. Sent as X-Admin-Token.",
            required=True,
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

        Playing only, deliberately, against MA's own guidance that a group
        should also hold its members while paused and through an idle grace
        period. That guidance assumes a group the user can put down. Music
        Assistant offers this player no stop control, only pause, so a group
        that held its members while paused held them until something else
        started playing, and in practice that meant every Echo in the group was
        permanently missing from the picker. Releasing on pause is the lesser
        wrong: the worst it costs is a member being individually selectable
        while the group happens to be paused.

        A non-group player must always answer False, which is what the base
        class does and why this only overrides for a group.
        """
        if not self.is_group:
            return False
        return self._attr_playback_state == PlaybackState.PLAYING

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
        track_ids, self._titles_to_items = provider.subsonic_ids(items)

        if not track_ids:
            raise BridgeError(
                "nothing in this queue exists on the Subsonic server the "
                "bridge streams from"
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
        await provider.bridge.publish_queue(track_ids, name, offset_ms)

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

        # mediaProgress and mediaLength come back in milliseconds, which is not
        # what the field names suggest and is why the Alexa Media Player
        # integration divides both by 1000 as well.
        elapsed = progress.get("mediaProgress")
        if isinstance(elapsed, (int, float)):
            self._attr_elapsed_time = elapsed / 1000
            self._attr_elapsed_time_last_updated = time.time()

        if not title:
            return
        duration = progress.get("mediaLength")
        art = (info.get("mainArt") or {}).get("url")
        self._attr_current_media = PlayerMedia(
            uri=f"ampere://{self.player_id}/{title}",
            media_type=MediaType.TRACK,
            title=title,
            artist=text.get("subText1"),
            album=text.get("subText2"),
            image_url=art,
            duration=int(duration / 1000) if isinstance(duration, (int, float)) else None,
            queue_item_id=self._titles_to_items.get(title.lower()),
        )


class AmpereAlexaProvider(PlayerProvider):
    """Discovers Echo devices and speaker groups and drives them via Ampere."""

    login: AlexaLogin
    bridge: BridgeClient

    async def handle_async_init(self) -> None:
        self._discovery_lock = asyncio.Lock()
        self.bridge = BridgeClient(
            base_url=str(self.config.get_value(CONF_BRIDGE_URL) or ""),
            admin_token=str(self.config.get_value(CONF_ADMIN_TOKEN) or ""),
            session=self.mass.http_session,
        )

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

    def subsonic_ids(
        self, items: list[QueueItem]
    ) -> tuple[list[str], dict[str, str]]:
        """Map queue items to Subsonic song ids, keeping a title index.

        The second return value is title -> queue_item_id, used later to guess
        which MA item a polled Alexa title corresponds to. Alexa reports what
        is playing by name and never by anything we handed it, so a name is all
        there is to match on.
        """
        track_ids: list[str] = []
        titles: dict[str, str] = {}
        skipped = 0

        for item in items:
            song_id = _subsonic_id(item)
            if song_id is None:
                skipped += 1
                continue
            track_ids.append(song_id)
            if item.name:
                titles.setdefault(item.name.lower(), item.queue_item_id)

        if skipped:
            self.logger.warning(
                "%s of %s queue items are not on the Subsonic server the bridge "
                "streams from and were left out",
                skipped, len(items),
            )
        return track_ids, titles

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
