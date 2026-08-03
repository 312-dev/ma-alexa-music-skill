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
import contextlib
import logging
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
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.errors import LoginFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from . import alexapy_compat, core
from . import push, push_auth, push_events, push_router, push_signin, settings
from . import setup_ops
from .bridge import BridgeClient, BridgeError, LocalBridge
from .stream_ref import encode_ref, is_live
from .stream_route import MediaStreamRoute
from .utterance import custom_command, sanitize
from .webserver import DEFAULT_PORT, AmpereWebServer
from . import wizard
from .tasks import AmpereTasks

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Every setting lives in `settings`, which deliberately does not import Music
# Assistant itself so that the entry list can be tested without a server.
from .settings import (  # noqa: E402
    CONF_ADMIN_SECRET, CONF_ADMIN_TOKEN, CONF_ALIAS, CONF_AMAZON_URL,
    CONF_BRIDGE_URL, CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_ENDPOINT_PORT,
    CONF_EXPOSE_GROUPS, CONF_HANDOFF_PHRASE, CONF_LINK_SECRET, CONF_MA_SOURCE,
    CONF_OTP_SECRET, CONF_PASSWORD, CONF_PUBLIC_BASE, CONF_SERVE_ENDPOINT,
    CONF_SIGNING_KEY, CONF_SUBSONIC_PASSWORD, CONF_SUBSONIC_URL,
    CONF_SUBSONIC_USER, CONF_USERNAME, GENERATED, _settings_entries,
    ACTION_PUSH_SIGN_IN, CONF_PUSH_ENABLED,
)

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

# What the poll slows to once the push stream is delivering. Polling is never
# switched off, only slowed: push says what changed and cannot say what was
# missed while disconnected, so this is the floor that repairs state after any
# gap, and the interval to poll immediately at when a stream reopens.
#
# Two values because the pinned alexapy cannot notice a stream that stops
# delivering without closing. Where it can (1.30.0's read_timeout), a stall is
# detected within five minutes and the poll can afford to be lazy. Where it
# cannot, an undetected stall degrades silently to exactly this interval, so it
# is kept to something a person would find merely slow rather than broken.
PUSH_POLL_INTERVAL_SUPERVISED = 60
PUSH_POLL_INTERVAL_UNSUPERVISED = 30

# How long alexapy waits before sending a volume command, so that several
# arriving together become one request to Amazon.
#
# **Left at alexapy's default of 1.5 seconds, deliberately, after trying 0.3
# and being rate limited within minutes.**
#
# The delay is most of the latency of a volume change: measured 2026-08-03 the
# call took 1.68s, of which 1.5s was this sleep and 0.18s was Amazon, and the
# push event describing the change arrived 0.16s after that. Lowering it took
# the call to 0.48s, which felt exactly as much better as it sounds.
#
# Then Amazon started answering with TooManyRequests and backing off 1.1s, then
# 2s, then 4s, and a volume_set that had been 0.48s took 7.99s. Everything else
# went with it, because the state poll uses the same API.
#
# The reason is what this window is actually for, which is not what it looks
# like. It is not debouncing a slider drag. Changing the volume of a speaker
# *group* makes Music Assistant call volume_set once per member, and during the
# sleep alexapy collects those into `_sequence_queue` and sends them as ONE
# behavior. Whole Apartment is four speakers, so the window is the difference
# between one request and four, every time. Shortening it multiplies every
# group volume change by its member count.
#
# So the 1.5s is the price of a group volume change being one request. Push
# cannot help here either way: this delay happens before anything is sent.
VOLUME_QUEUE_DELAY = 1.5

# How long an account-wide volume reading is reused. Only has to span one
# burst of confirms, which all fire together after a group volume change.
VOLUME_READ_TTL = 1.0

# How many times a volume is checked before giving up. One send plus two
# resends. Measured on a four speaker group: two members took the first send,
# one took the first resend, one took neither, so a single retry does not
# converge. Each attempt costs a check and only the speakers still wrong send
# anything, so the cost of the extra attempts is paid only where it is needed.
VOLUME_ATTEMPTS = 3

# How far a speaker may settle from what it was asked for and still count as
# having taken it. Measured on an Echo Studio, which quantises: asked for 18 it
# reports 17, for 65 it reports 67, for 76 it reports 77. Requiring equality
# turns that into a permanent failure no resend can fix.
VOLUME_TOLERANCE = 2

# How far apart a group's retries are spread. Every member reaches the retry at
# the same instant, and sent together they are a burst against an API that rate
# limits. Wide enough to separate a large group, short enough that a correction
# still feels immediate.
VOLUME_RETRY_SPREAD = 2.0

# How close to the end of a track a resume position has to be before it is
# treated as "this already finished" rather than as somewhere to resume from.
# Three seconds is longer than any rounding and shorter than anything a person
# would deliberately seek to.
END_OF_TRACK_MS = 3000

# Making a transport command stick. Longer than the volume equivalent because
# a transport change travels further: Alexa has to act on it and the device has
# to report back, where a volume is applied locally. Measured, a pause shows up
# about 1.6s after it is asked for, so three seconds is comfortably past the
# point where "not yet" and "not at all" stop looking alike.
TRANSPORT_CONFIRM_SECONDS = 3.0
TRANSPORT_ATTEMPTS = 3

# What Alexa calls each of the states we ask for. Sets rather than single
# values because Alexa reports the same intent under several names and which
# one arrives depends on how playback ended: an interrupted stream and a
# deliberate pause are both a pause as far as anyone listening is concerned.
PLAYING_STATES = frozenset({"PLAYING"})
PAUSED_STATES = frozenset({"PAUSED", "INTERRUPTED"})
STOPPED_STATES = frozenset({"IDLE", "STOPPED", "FINISHED", "PAUSED"})

# How many consecutive failed state polls before a player is hidden. One is too
# few: an Echo that is asleep or briefly unreachable still plays when something
# is sent to it, and hiding it takes a working speaker off the list. Three
# misses at POLL_INTERVAL is half a minute of silence, which is a real fault.
POLL_FAILURES_BEFORE_UNAVAILABLE = 3

# How long a requested volume outranks the one Alexa reports. Amazon keeps
# reporting the previous volume for several seconds after accepting a change,
# and at POLL_INTERVAL this has to cover more than one poll or the value would
# be conceded on the first read. Thirty seconds is three polls; past that, a
# volume that has not arrived is one Amazon is not going to apply.
VOLUME_SETTLE_SECONDS = 30.0

# How long to wait before checking a volume change landed. Long enough for
# Amazon to have acted on a command it was going to act on, short enough that a
# resend still feels like part of the same gesture rather than a correction
# arriving later.
VOLUME_CONFIRM_SECONDS = 2.0

# How long to give Alexa to act on a play command before sending it again.
# Measured 2026-08-03: when Amazon does act, the search arrives 1.8 to 2.0
# seconds after `run_custom` returns. Four seconds is comfortably past that, so
# a resend means the command really was dropped rather than merely slow, and a
# working play is never spoken twice.
PLAY_CONFIRM_SECONDS = 4.0

# How soon after a control to read the real state back. Long enough for Alexa
# to have actually done the thing, short enough that a resume does not sit at
# the wrong position while the regular poll cycle comes round.
RESYNC_SECONDS = 1.5


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
    if action == ACTION_PUSH_SIGN_IN:
        # Not handed to the wizard runner like the others. That runs in a
        # worker thread and takes no `mass`, and this needs both an event loop
        # and MA's webserver to put Amazon's login pages in front of a person.
        await _run_push_sign_in(mass, instance_id, values)
    elif action == wizard.ACTION_UPLOAD:
        # The one action that is not answered here. A library crawl is minutes
        # of work, so it is handed to MA's task controller and the button
        # returns at once with somewhere to watch it.
        AmpereTasks(mass, logging.getLogger(__name__)).start_upload()
        wizard.remember(action, setup_ops.Outcome(
            True, "Started. Progress is in Music Assistant's task list."))
    elif action:
        # Blocking: SMAPI is a series of HTTPS round trips and the library
        # crawl is Subsonic calls. MA calls this from the event loop, so it
        # goes to a worker thread rather than stalling playback for everyone.
        await asyncio.to_thread(
            wizard.run, action, dict(values),
            str(values.get(CONF_PUBLIC_BASE) or core.PUBLIC_BASE or ""),
        )

    return (*_settings_entries(), *await asyncio.to_thread(wizard.entries))


async def _run_push_sign_in(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> None:
    """Sign in to Amazon and keep the token live updates need.

    Reads credentials from the form values rather than from saved config, so
    this works during first setup, before anything has been stored, which is
    exactly when an operator is most likely to press it.

    On success the running provider is told, so live updates come up without a
    restart. During first setup there is no running provider yet and the token
    on disk is picked up the next time it loads, which is moments later.
    """
    logger = logging.getLogger(__name__)
    storage = pathlib.Path(mass.storage_path)
    email = str(values.get(CONF_USERNAME) or "")
    if not email:
        settings.set_push_status(
            "Fill in the Amazon account email and password first, then "
            "connect.")
        return

    # Reading a secret out of a config action is not one lookup, it is three
    # cases, and two of them look like a value while being useless.
    #
    # Music Assistant never sends a stored SECURE_STRING back to the browser.
    # The form receives the literal `this_value_is_encrypted`, and that is what
    # an action gets handed back. It is not encrypted and not empty, so it
    # survives every obvious guard, and it is 23 characters of plausible
    # nonsense: it reached pyotp as a TOTP seed and reached Amazon as the
    # account password. Its own providers guard on this constant by name, which
    # is the giveaway that there is no cleverer way to detect it.
    #
    # So: use what is in the form only when it is a real value someone just
    # typed, and otherwise go to the stored config, where ProviderConfig
    # decrypts on the way out.
    async def _credential(key: str) -> str:
        typed = str(values.get(key) or "")
        if typed and typed != SECURE_STRING_SUBSTITUTE:
            try:
                return mass.config.decrypt_string(typed)
            except Exception:  # noqa: BLE001 - a bad value, not a crash
                return typed
        if instance_id is None:
            return ""
        try:
            stored = await mass.config.get_provider_config(instance_id)
            return str(stored.get_value(key) or "")
        except Exception as err:  # noqa: BLE001
            logger.debug("could not read stored %s: %s", key, type(err).__name__)
            return ""

    auth = push_auth.PushAuth(
        store_path=str(storage / "ampere" / "push-auth.json"),
        url=str(values.get(CONF_AMAZON_URL) or "amazon.com"),
        email=email,
        logger=logger,
    )
    password = await _credential(CONF_PASSWORD)
    otp_secret = await _credential(CONF_OTP_SECRET)
    if not password:
        # Better to refuse than to hand Amazon an empty password and let the
        # failure surface three pages later as a mobile number that cannot be
        # verified, which is what the placeholder did.
        settings.set_push_status(
            "Could not read the saved Amazon password. Re-enter it in the "
            "fields above, save, then connect.")
        return

    state = await push_signin.sign_in(
        mass, auth,
        session_id=str(values.get("session_id") or ""),
        url=str(values.get(CONF_AMAZON_URL) or "amazon.com"),
        email=email,
        password=password,
        otp_secret=otp_secret,
        cookie_path=alexapy_compat.cookie_path(str(storage), email),
        logger=logger,
    )
    settings.set_push_status(state.detail)
    if not state.ok or instance_id is None:
        return

    provider = mass.get_provider(instance_id)
    if isinstance(provider, AmpereAlexaProvider):
        with contextlib.suppress(Exception):
            await provider.push_stream.stop()
        provider.push_auth = auth
        provider.push_stream.auth = auth
        await provider.push_stream.start()


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
    # A volume asked for and not yet confirmed by Alexa. Class level for the
    # same reason again: `poll` reads these and a player built any other way
    # must not arrive one attribute short of pollable.
    _volume_wanted: int | None = None
    _volume_asked_at = 0.0
    _volume_resent = False
    _volume_confirm: asyncio.Task | None = None
    # The same for a play command, which Amazon drops in the same way.
    _play_confirm: asyncio.Task | None = None
    # And for pause, stop and play, which the conformance suite found Amazon
    # dropping too. Class level for the same reason as the rest: a player built
    # any other way must not arrive one attribute short.
    _transport_confirm: asyncio.Task | None = None
    # A one-off catch-up poll after a control, so the correction to an
    # optimistic answer does not wait for the ten second cycle.
    _resync: asyncio.Task | None = None

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

    def _resync_soon(self) -> None:
        """Read the real state back shortly, rather than at the next poll.

        Every control here answers optimistically: the state is set to what was
        asked for and shown immediately, because waiting on Amazon before
        moving a play button would feel worse than being briefly wrong. What
        made it *stay* briefly wrong was that the correction only came with the
        next poll, up to ten seconds later, so a resume showed the wrong
        position for several seconds and the scrubber sat where it had been.

        Alexa needs a moment to actually change what it is doing, so this is
        not immediate either. It is one read, soon, instead of a wait of
        unknown length.
        """
        if self._resync is not None:
            self._resync.cancel()

        async def run() -> None:
            try:
                await asyncio.sleep(RESYNC_SECONDS)
                await self.poll()
                self.update_state()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("resync poll failed: %s", err)

        self._resync = asyncio.create_task(run())

    async def _timed(self, what: str, coro):
        """Time an Amazon call and say how long it took.

        Every control here is a round trip to Amazon and back, and the
        interesting question when something feels slow is always which leg was
        slow: our own publish, the call to Amazon, or Amazon's own processing
        before it asks us for anything. Without this the answer is a guess.

        Logged at info, because these fire on user actions rather than on a
        timer, so the volume is bounded by how often somebody presses
        something.
        """
        started = time.monotonic()
        try:
            return await coro
        finally:
            self.logger.info("%s on %s took %.2fs", what, self.name,
                             time.monotonic() - started)

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
        content_id = await self._timed("publish", provider.bridge.publish_queue(
            tracks, name, offset_ms))

        # The one moment this association is known for certain. Alexa echoes
        # this id back on every now-playing event for the session, and those
        # events name no device at all, so without this they cannot be placed.
        provider.router.note_publish(self.player_id, str(content_id or ""))

        # The published queue is claimed by phrase, not by id. There is no
        # utterance that names an arbitrary track list, so the bridge maps one
        # fixed phrase onto whatever was published most recently and pins it to
        # a concrete contentId at the moment Alexa asks. Two players starting
        # at the same instant is the one case this loses; see README.
        text = custom_command(alias, label, self.group_name or None)
        self.logger.debug("run_custom on %s: %s", self.speaker.device_serial_number, text)
        sent_at = time.monotonic()
        await self._timed("run_custom", self.api.run_custom(text))

        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

        if self._play_confirm is not None:
            self._play_confirm.cancel()
        self._play_confirm = asyncio.create_task(self._confirm_play(text, sent_at))

    async def _confirm_play(self, text: str, sent_at: float) -> None:
        """Did Alexa act on the command. If not, say it once more.

        Amazon accepts a `run_custom` and then sometimes does nothing with it.
        Measured 2026-08-03: the call returned in 0.17s and no search ever
        arrived, so nothing played; pressing play a second time worked
        immediately. The same shape as a volume change needing two sends.

        Unlike volume there is a direct signal here. Alexa turning up to
        resolve the handoff is unambiguous proof the command was acted on, and
        `queue_api` records when that last happened, so this waits for that
        rather than guessing from a status code that already said fine.
        """
        try:
            await asyncio.sleep(PLAY_CONFIRM_SECONDS)
            from . import queue_api

            if queue_api.handoff_claimed_at() > sent_at:
                return  # Alexa came and asked; nothing was dropped

            self.logger.info(
                "%s: Alexa did not act on the play command within %.0fs; "
                "sending it again", self.name, PLAY_CONFIRM_SECONDS)
            await self._timed("run_custom (resend)", self.api.run_custom(text))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.debug("play confirm failed: %s", err)

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
        offset = max(0, int(float(getattr(details, "seek_position", 0) or 0) * 1000))

        # Never start a track at its own end. An offset within a few seconds of
        # the duration is not a resume, it is a track that already finished,
        # and handing it to Alexa produces the least diagnosable failure this
        # provider has: the queue is published, the utterance is spoken, Alexa
        # starts the song at the last second of it, the song ends immediately,
        # and the operator sees a long pause followed by a stopped player. Every
        # log line on the way says success.
        #
        # Observed 2026-08-03 with an offset of 256000 on a 256 second track,
        # caused by a stale position that has since been fixed at its source.
        # The guard stays because the source is not the interesting part: any
        # bad position produces this, and starting from the top is a harmless
        # wrong answer where starting from the end is not.
        duration = getattr(media, "duration", None) or 0
        if duration and offset >= (duration * 1000) - END_OF_TRACK_MS:
            self.logger.info(
                "%s: ignoring a resume position of %.0fs into a %.0fs track "
                "and starting from the beginning", self.name,
                offset / 1000, duration)
            return 0
        return offset

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Deliberately nothing.

        Alexa was handed the whole list at play_media and advances it itself.
        The feature is declared so MA does not fall back to re-issuing
        play_media per track, which would restart the queue on every track
        boundary.
        """

    async def play(self) -> None:
        await self._timed("play", self.state_api.play())
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()
        self._resync_soon()
        self._confirm_transport("play", PLAYING_STATES, self.state_api.play)

    async def pause(self) -> None:
        await self._timed("pause", self.state_api.pause())
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()
        self._resync_soon()
        self._confirm_transport("pause", PAUSED_STATES, self.state_api.pause)

    async def stop(self) -> None:
        await self.state_api.stop()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()
        self._confirm_transport("stop", STOPPED_STATES, self.state_api.stop)

    # -- making a transport command stick ------------------------------------
    #
    # Amazon accepts a command, answers success, and sometimes does nothing.
    # That was found first for volume, where a change needed sending twice, and
    # the live conformance suite then found the same thing in three more
    # places: a group pause is lost roughly one time in three and the speaker
    # returns to playing on its own; a stop settles on paused rather than idle;
    # a track change moves and then reverts. Four symptoms, one mechanism.
    #
    # So this is the volume confirm loop with the volume taken out of it. Same
    # three properties that make a retry loop safe rather than a nuisance: it
    # stops the moment the speaker agrees, a newer command cancels it outright
    # so it can never fight a person, and it is bounded and says so when it
    # gives up.

    def _confirm_transport(self, what: str, wanted: frozenset[str],
                           send: Any) -> None:
        """Check a transport command landed, and repeat it if it did not."""
        if self._transport_confirm is not None:
            # A newer command supersedes an older one rather than racing it.
            # This is also what stops the loop arguing with the operator: press
            # play while a pause is still converging and the pause gives up.
            self._transport_confirm.cancel()
        self._transport_confirm = asyncio.create_task(
            self._converge_transport(what, wanted, send))

    async def _converge_transport(self, what: str, wanted: frozenset[str],
                                  send: Any) -> None:
        try:
            for attempt in range(1, TRANSPORT_ATTEMPTS + 1):
                await asyncio.sleep(TRANSPORT_CONFIRM_SECONDS)

                observed = await self._reported_transport_state()
                if observed is None:
                    # Alexa told us nothing rather than told us "no". A group
                    # answers with no playerInfo at all while its members are
                    # audibly playing, so silence here is not evidence of
                    # anything and resending on it would be guessing loudly.
                    self.logger.debug(
                        "%s: cannot tell whether %s landed", self.name, what)
                    return

                if observed in wanted:
                    if attempt > 1:
                        self.logger.info("%s: %s landed on attempt %s",
                                         self.name, what, attempt)
                    return

                if attempt == TRANSPORT_ATTEMPTS:
                    self.logger.warning(
                        "%s: %s did not stick after %s attempts (Alexa reports "
                        "%s)", self.name, what, attempt, observed)
                    return

                self.logger.info(
                    "%s: %s did not stick (Alexa reports %s); resending "
                    "(%s of %s)", self.name, what, observed, attempt,
                    TRANSPORT_ATTEMPTS - 1)
                await send()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a retry must not raise
            self.logger.debug("%s confirm failed: %s", what, err)

    async def _reported_transport_state(self) -> str | None:
        """What Alexa says this player is doing, or None for "it did not say".

        The distinction matters more than the value. An empty payload is not a
        claim that nothing is playing -- it is what a speaker group answers
        with while its members are audibly playing -- and treating that as
        "stopped" is what would turn this loop into something that fights a
        working speaker.
        """
        try:
            raw = await self.state_api.get_state()
        except Exception:  # noqa: BLE001 - unreachable is not disagreement
            return None
        info = (raw or {}).get("playerInfo") or {}
        if not info:
            return None
        return str(info.get("state") or "").upper() or None

    async def next_track(self) -> None:
        await self._timed("next", self.state_api.next())
        self._resync_soon()

    async def previous_track(self) -> None:
        await self._timed("previous", self.state_api.previous())
        self._resync_soon()

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume, then check it landed and send it again if not.

        Measured 2026-08-03 on a live Echo: the command has to be sent twice
        before the speaker actually changes. Not a display artifact and not a
        lost response, because the first send returns without error; Amazon
        simply does not act on it.

        Two separate defects were in play and only one of them was cosmetic.
        The speaker ignoring the first send is this one, and it is handled by
        confirming shortly afterwards and resending once. The other is `poll`
        copying Amazon's lagging report over the requested value, which made
        even a successful change appear to snap back; that is handled in
        `_reconcile_volume`.

        The confirm runs as its own task so the command returns immediately.
        A control that waits two seconds before releasing the slider feels
        broken in a different way.
        """
        wanted = max(0, min(100, volume_level))
        # alexapy takes 0..1 and multiplies by 100 on the way out.
        # The requested value, not just the timing. A group volume change fans
        # out to every member and only one of them was landing; without the
        # numbers there is no way to tell a command that was never sent from
        # one that asked for the value the speaker already had.
        self.logger.info("volume_set on %s -> %s (was %s)",
                         self.name, wanted, self._attr_volume_level)
        await self._timed(
            "volume_set",
            self.state_api.set_volume(wanted / 100, queue_delay=VOLUME_QUEUE_DELAY))
        self._attr_volume_level = wanted
        self._volume_wanted = wanted
        self._volume_asked_at = time.time()
        self._volume_resent = False
        self.update_state()

        if self._volume_confirm is not None:
            # A newer request supersedes an older one rather than racing it.
            self._volume_confirm.cancel()
        self._volume_confirm = asyncio.create_task(self._confirm_volume(wanted))

    async def _confirm_volume(self, wanted: int) -> None:
        """Keep asking until the speaker actually has this volume.

        A loop rather than a single resend, because one resend does not
        converge: measured 2026-08-03 on a four speaker group, two members took
        the first send, one took the resend, and one took neither. Amazon
        accepts a volume command, returns success, and sometimes does nothing,
        and it does so often enough that a single retry leaves a speaker
        visibly wrong.

        Three things make a loop safe here, and without all three it would be
        the "retry per poll" this deliberately was not:

        - It stops the moment the speaker agrees, so a working change costs one
          check and no resends at all.
        - It abandons immediately if `_volume_wanted` has changed, which is
          what happens when the operator moves the slider again. The loop can
          never fight a person.
        - It is bounded. After VOLUME_ATTEMPTS it gives up and says so, rather
          than insisting forever at a speaker that is refusing for some reason
          this cannot see.

        **Retries are sent uncoalesced.** The first send is batched with the
        rest of a group fan-out, which is what keeps a group volume change to
        one request. But measured, a batch is exactly where commands go
        missing: four members sent as separate requests applied four times out
        of four, while the same four coalesced into one behavior dropped some.
        So the cheap path is tried first and the correction is sent on its own,
        which costs one request per speaker that actually needs it rather than
        one per speaker every time.
        """
        try:
            for attempt in range(1, VOLUME_ATTEMPTS + 1):
                await asyncio.sleep(VOLUME_CONFIRM_SECONDS)
                if self._volume_wanted != wanted:
                    return  # superseded by a newer request, or by the operator

                # Not from get_state. Two reasons, and the first was a plain
                # bug: this read `raw["volume"]` where the payload nests it
                # under `playerInfo`, as `poll` correctly does, so `reported`
                # was always None and the check never passed. Every volume
                # change resent itself, doubling traffic to an API that rate
                # limits.
                #
                # Fixing the path alone would not have been enough. Amazon only
                # reports volume inside `playerInfo`, which is empty on an idle
                # speaker, so a confirm for anything not currently playing
                # could never have succeeded either. The account-wide endpoint
                # answers for idle devices, and is cached for a moment so
                # several players confirming at once share one request.
                reported = await self.provider_instance.current_volume(
                    self.device.device_serial_number)

                # Close enough, not equal. Some speakers quantise: an Echo
                # Studio asked for 18 settles at 17, for 65 at 67, and for 76
                # at 77, every time. Demanding equality makes those a
                # permanent failure that no number of resends can fix, and the
                # loop then spends its whole budget and warns about a speaker
                # that did exactly what it was told.
                if reported is not None and abs(reported - wanted) <= VOLUME_TOLERANCE:
                    if attempt > 1:
                        self.logger.info(
                            "%s is at %s%% after %s attempts",
                            self.name, reported, attempt)
                    self._volume_wanted = None
                    return

                if attempt == VOLUME_ATTEMPTS:
                    self.logger.warning(
                        "%s would not take %s%% after %s attempts (still %s)",
                        self.name, wanted, attempt, reported)
                    return

                self._volume_resent = True
                self.logger.info(
                    "%s did not take %s%% (reported %s); resending (%s of %s)",
                    self.name, wanted, reported, attempt, VOLUME_ATTEMPTS - 1)
                # Spread out, because every member of a group reaches this line
                # at the same instant. Sent together and uncoalesced they are a
                # burst, and measured 2026-08-03 a four speaker group's retries
                # drew three TooManyRequests at once. alexapy backs off and
                # recovers, so this was self healing rather than broken, but a
                # retry that provokes throttling is a poor way to fix a dropped
                # command.
                #
                # Derived from the player id rather than randomised, so a given
                # speaker always waits the same amount and two of them can
                # never collide by chance.
                await asyncio.sleep(
                    (hash(self.player_id) % 100) / 100.0 * VOLUME_RETRY_SPREAD)
                await self.state_api.set_volume(wanted / 100, queue_delay=0)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.debug("volume confirm failed: %s", err)

    def _reconcile_volume(self, reported: int) -> None:
        """Decide whether a polled volume or a requested one is the truth.

        With nothing outstanding, Alexa wins: the volume may have been changed
        by voice or on the device itself, and those are real changes this has
        no other way to learn about.

        With a request outstanding, the requested value wins until Alexa
        reports it or the window runs out. Ceding immediately is what made a
        volume change need two attempts, because Amazon reports the previous
        volume for several seconds after accepting a new one.

        Giving up after the window matters as much as defending inside it. If
        Amazon has genuinely refused the change, saying so is better than a
        slider that lies about the volume of the room.

        Synchronous, because `_apply_state` is. Nothing here needs to await:
        the resend belongs to `_confirm_volume`, which runs two seconds after
        the request rather than whenever the next poll happens to land.
        """
        if self._volume_wanted is None:
            self._attr_volume_level = reported
            return

        if reported == self._volume_wanted:
            self._volume_wanted = None
            self._attr_volume_level = reported
            return

        if time.time() - self._volume_asked_at > VOLUME_SETTLE_SECONDS:
            self.logger.info(
                "%s stayed at %s%% after asking for %s%%; Alexa refused it",
                self.name, reported, self._volume_wanted)
            self._volume_wanted = None
            self._attr_volume_level = reported
            return

        # No resend here. `_confirm_volume` already did that two seconds after
        # the request, which is far sooner than the next poll and is where one
        # user action turning into one volume change belongs. All this does is
        # keep the slider honest about what was asked for while that plays out.
        self._attr_volume_level = self._volume_wanted

    # -- push ----------------------------------------------------------------

    def apply_push(self, event: push_events.PushEvent) -> None:
        """Act on one directive Amazon sent about this player.

        Synchronous, and called from the stream's reader. Everything here is
        assignment to attributes MA already reads, so there is nothing to await
        and nothing that should be allowed to block a socket.

        Deliberately narrow. Volume, playback state and position are the three
        things that lag visibly at a ten second poll, and they are the three
        things a directive states unambiguously. Track *identity* is left to
        the poll: the now-playing payload does carry a title, but adopting it
        here would mean two independent paths racing to set current_media, and
        the poll's version is the one that has been matched against the MA
        queue.
        """
        if event.command == push_events.VOLUME_CHANGE:
            self._push_volume(event)
        elif event.command == push_events.AUDIO_PLAYER_STATE:
            self._push_player_state(event)
        elif event.command == push_events.NOW_PLAYING:
            self._push_now_playing(event)
        else:
            return
        self.update_state()

    def _push_volume(self, event: push_events.PushEvent) -> None:
        volume = event.payload.get("volumeSetting")
        if isinstance(volume, int):
            # Through the same reconciliation the poll uses, not around it. A
            # push is faster but it is not more authoritative than a volume
            # this process asked for two seconds ago and is still defending;
            # bypassing that would reintroduce the snap-back it exists to stop.
            self._reconcile_volume(volume)
        muted = event.payload.get("isMuted")
        if isinstance(muted, bool):
            self._attr_volume_muted = muted

    def _push_player_state(self, event: push_events.PushEvent) -> None:
        state = str(event.payload.get("audioPlayerState") or "")
        if state == "PLAYING":
            self._attr_playback_state = PlaybackState.PLAYING
        elif state in ("INTERRUPTED", "PAUSED"):
            self._attr_playback_state = PlaybackState.PAUSED
        elif state in ("FINISHED", "IDLE", "STOPPED"):
            self._attr_playback_state = PlaybackState.IDLE

    def _push_now_playing(self, event: push_events.PushEvent) -> None:
        """The event that carries the position, which is the point of all this.

        **The units here are milliseconds and the polled API's are seconds.**
        Both were measured on 2026-08-03: the poll answered
        `{'mediaLength': 226, 'mediaProgress': 11}` for a 226 second track,
        and a push for an 811 second track read `{'mediaLength': 811000,
        'mediaProgress': 638}`. Feeding one into the other's units is not a
        subtle bug -- it is the "scrubber never moves" fault that was already
        found and fixed once in `_apply_state`, running a thousand times the
        other way.
        """
        data = event.now_playing
        state = str(data.get("playerState") or "")
        if state == "PLAYING":
            self._attr_playback_state = PlaybackState.PLAYING
        elif state == "PAUSED":
            self._attr_playback_state = PlaybackState.PAUSED

        # Only if this event is about the track Music Assistant thinks is
        # playing. A position is meaningless without knowing what it is a
        # position *in*, and this handler previously wrote whatever arrived
        # onto the current item.
        #
        # What that cost, measured 2026-08-03: a now-playing event carrying a
        # finished track's position set elapsed_time to the full length of the
        # song. Music Assistant resumes a queue from elapsed_time, so the next
        # press of play republished the queue with `stream.offsetInMilliseconds`
        # at 256000 on a 256 second track. Alexa started it at the very end,
        # the track finished instantly, and what the operator saw was ten
        # seconds of nothing followed by a paused player with no position.
        #
        # Matching on the title because it is the only identity the two sides
        # share, which is the same constraint `_queue_item_for` works under.
        # Compared against the media's own title. The first version of this
        # passed `_attr_current_media` to `_queue_item_titles`, which reads
        # `.name` and `.media_item.name` off a *queue item*; a PlayerMedia has
        # neither, so it returned an empty set and every position update was
        # dropped. The scrubber then sat at zero while Alexa reported 64
        # seconds, which is the same symptom this whole feature exists to fix,
        # reintroduced by the guard meant to protect it.
        info = data.get("infoText")
        reported_title = (info or {}).get("title") if isinstance(info, dict) else None
        current = self._attr_current_media
        current_title = str(getattr(current, "title", "") or "") if current else ""
        if current_title and reported_title:
            reported = str(reported_title).strip().lower()
            wanted = current_title.strip().lower()
            # Alexa reports the title alone where Music Assistant may hold
            # "Artist - Title", so a containment check either way is what
            # actually matches, the same problem `_queue_item_titles` solves
            # for the polled path.
            if reported != wanted and reported not in wanted and wanted not in reported:
                self.logger.debug(
                    "%s: ignoring a position for %r while playing %r",
                    self.name, reported_title, current_title)
                return

        progress = data.get("progress")
        if not isinstance(progress, dict):
            return
        elapsed = progress.get("mediaProgress")
        if not isinstance(elapsed, (int, float)):
            return

        # A paused session keeps reporting the position it stopped at, and on
        # startup the stream replays the current state of every device on the
        # account. Applying that to a player that is not playing is what put
        # the scrubber halfway through a track nobody had started: the position
        # was real, it just belonged to a session from hours ago.
        if self._attr_playback_state != PlaybackState.PLAYING:
            return

        self._attr_elapsed_time = float(elapsed) / 1000.0
        # Local time, not Amazon's.
        #
        # This was `event.at` on the reasoning that it avoids adding delivery
        # delay to the reading. That was a bad trade. Delivery is under a tenth
        # of a second, while the two clocks are on different machines and
        # nothing keeps them in step; Music Assistant extrapolates the position
        # forward from this timestamp, so any skew becomes a scrubber that is
        # permanently wrong by the size of the skew, and a clock that is ahead
        # makes it run into the future.
        self._attr_elapsed_time_last_updated = time.time()

    def use_push_poll_interval(self, slow: bool) -> None:
        """Slow the poll while push is delivering, restore it when it is not.

        Reversible on purpose and driven by the stream's own health rather than
        by configuration. The failure this guards against is a stream that is
        connected and silent: if that is ever mistaken for a healthy one, the
        only cost is this interval instead of ten seconds.
        """
        if slow:
            self._attr_poll_interval = (
                PUSH_POLL_INTERVAL_SUPERVISED
                if alexapy_compat.supports_read_timeout()
                else PUSH_POLL_INTERVAL_UNSUPERVISED
            )
        else:
            self._attr_poll_interval = POLL_INTERVAL

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
            self._reconcile_volume(volume["volume"])
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

    def _minted(self, key: str, what: str, length: int = 32,
                factory: Any = None) -> str:
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

        `factory` is for the values that are generated but not random. The
        account linking client id is one Amazon was told once, in the skill
        manifest, so a deployment migrating from the standalone service has to
        be able to keep the id it registered. Storing it rather than deriving
        it every time is what makes that possible: derived, it would silently
        become a different id and account linking would start refusing the
        credentials Amazon holds.
        """
        stored = self.mass.config.get_raw_provider_config_value(
            self.instance_id, key
        )
        if stored:
            return str(stored)
        minted = factory() if factory is not None else secrets.token_hex(length)
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
            oauth_client_id=self._minted(
                CONF_CLIENT_ID, "an account linking client id",
                factory=lambda: f"ampere-{self.instance_id[:8]}"),
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

        # The library sync, on MA's own scheduler rather than a daemon thread
        # of Ampere's. Registered whether or not it is switched on, because the
        # schedule and its enabled flag are things MA renders and the operator
        # edits; leaving it unregistered would hide the feature entirely.
        self.tasks = AmpereTasks(
            self.mass, self.logger,
            executor=self.webserver._pool if self.webserver is not None else None)
        self.tasks.register_sync()
        self.tasks.register_binding_keepalive()

        # Live updates. Built here and started in loaded_in_mass, so a stream
        # that cannot connect delays nothing: the provider is fully functional
        # without it and merely slower to notice things.
        self._volume_read: dict[str, tuple[int, bool]] = {}
        self._volume_read_at = 0.0
        self._volume_read_lock = asyncio.Lock()

        self.router = push_router.PushRouter()
        self.push_auth = push_auth.PushAuth(
            store_path=str(
                pathlib.Path(self.mass.storage_path) / "ampere" / "push-auth.json"
            ),
            url=str(self.config.get_value(CONF_AMAZON_URL) or "amazon.com"),
            email=str(self.config.get_value(CONF_USERNAME) or ""),
            logger=self.logger,
        )
        self.push_stream = push.PushStream(
            auth=self.push_auth,
            on_event=self._on_push_event,
            logger=self.logger,
            on_health=self._on_push_health,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Take the audio route and the endpoint down with the provider.

        The listener especially: a reload that left the old one holding the
        port would make the new one fail to bind, and the failure would look
        like a broken skill rather than a stale socket.
        """
        # First, because it holds a socket and a reconnect loop. A stream left
        # supervising a provider that is going away would keep rebuilding
        # itself and delivering events to players that no longer exist.
        with contextlib.suppress(Exception):
            await self.push_stream.stop()
        self.stream_route.unregister()
        # Before the pool it runs on goes away with the webserver, and before
        # anything else: its handler is bound to this instance, so a scheduled
        # sync left registered would keep firing into a dead provider.
        self.tasks.unregister_sync()
        if self.webserver is not None:
            await self.webserver.stop()
            self.webserver = None

    async def loaded_in_mass(self) -> None:
        await self._login()
        await self.discover_players()
        await self._start_push()

    async def _start_push(self) -> None:
        """Bring up live updates if they are switched on and authorised.

        Every failure path here is a log line and a status string, never a
        raise. This runs after discovery precisely so that a broken push
        connection cannot cost the operator their players.
        """
        if not self.config.get_value(CONF_PUSH_ENABLED, True):
            settings.set_push_status("Live updates are switched off.")
            return
        if not await self.push_auth.restore():
            settings.set_push_status(self.push_auth.state.detail)
            self.logger.info(
                "live updates are not connected: %s", self.push_auth.state.detail)
            return
        settings.set_push_status("Connecting...")
        await self.push_stream.start()

    def _on_push_event(self, event: push_events.PushEvent) -> None:
        """One directive, placed on a player and applied.

        Events for devices this provider does not expose arrive here too --
        Amazon streams the whole account, including sessions belonging to other
        music providers entirely -- so an event that cannot be placed is
        dropped rather than guessed at.
        """
        player_id = self.router.resolve(event, self.instance_id)
        if player_id is None:
            return
        player = self.mass.players.get_player(player_id)
        if player is None or not isinstance(player, AmperePlayer):
            return
        # A state event names both a device and Alexa's own queue id, which is
        # the only way a later now-playing event for a queue this process did
        # not publish can be attributed at all.
        self.router.learn_from(event, player_id)
        player.apply_push(event)

    def _on_push_health(self, connected: bool) -> None:
        """Slow the poll while the stream is up, restore it when it is not."""
        settings.set_push_status(
            "Connected. Amazon is reporting changes as they happen."
            if connected else
            f"Reconnecting. {self.push_stream.last_error or ''}".strip()
        )
        # This provider's own players, not every player MA knows about. The
        # poll interval is a statement about how Ampere learns things and has
        # no business being applied to a Chromecast.
        for player in self.players:
            if isinstance(player, AmperePlayer):
                player.use_push_poll_interval(connected)
        if connected:
            # Everything that happened while the stream was down is invisible
            # to it, so the first thing a reconnect does is ask.
            self.mass.create_task(self._repoll_all())

    async def _repoll_all(self) -> None:
        for player in self.players:
            if isinstance(player, AmperePlayer):
                with contextlib.suppress(Exception):
                    await player.poll()

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

        await self._seed_volumes()

    async def current_volume(self, serial: str) -> int | None:
        """This device's volume right now, from a briefly shared reading.

        A group volume change makes every member confirm at the same instant,
        and the reading is account-wide, so without the cache four players
        would make four identical requests to an API that rate limits. The
        window only has to cover one burst of confirms, not to be a cache in
        any useful sense.
        """
        async with self._volume_read_lock:
            now = time.monotonic()
            if now - self._volume_read_at > VOLUME_READ_TTL:
                self._volume_read = await alexapy_compat.device_volumes(self.login)
                self._volume_read_at = now
        entry = self._volume_read.get(serial)
        return entry[0] if entry else None

    async def _seed_volumes(self) -> None:
        """Give every player a volume before anything has played.

        Without this a speaker that has been idle since startup reports None,
        because Amazon only puts volume inside `playerInfo` and an idle device
        has none. Music Assistant then cannot scale that member when the group
        volume changes, which is what made a group change move one speaker and
        leave the rest alone.

        One request for the whole account, so this is cheap enough to do on
        every discovery pass rather than only at startup.
        """
        volumes = await alexapy_compat.device_volumes(self.login)
        if not volumes:
            return
        seeded = 0
        for player in self.players:
            if not isinstance(player, AmperePlayer):
                continue
            entry = volumes.get(player.device.device_serial_number)
            if entry is None:
                continue
            volume, muted = entry
            # Only a seed. A value this process asked for and is still
            # defending must win over a reading that may predate it, which is
            # the same rule the poll follows.
            if player._attr_volume_level is None:
                player._attr_volume_level = volume
                seeded += 1
            if player._attr_volume_muted is None:
                player._attr_volume_muted = muted
            player.update_state()
        if seeded:
            self.logger.info("read a starting volume for %s players", seeded)

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
