"""Music Assistant player provider: Echo devices and speaker groups as MA players.

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
  - This publishes the whole track list to the Music Assistant bridge, which serves it
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
from music_assistant_models.config_entries import ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.errors import LoginFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from . import alexapy_compat, core, mdns
from . import push, push_auth, push_events, push_router, push_signin, settings
from . import setup_ops, subsonic_patch
from .bridge import BridgeClient, BridgeError, LocalBridge
from .stream_ref import encode_ref, is_live
from .stream_route import MediaStreamRoute
from .utterance import custom_command, sanitize
from .webserver import DEFAULT_PORT, MaAlexaWebServer
from . import wizard
from .tasks import MaAlexaTasks

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Every setting lives in `settings`, which deliberately does not import Music
# Assistant itself so that the entry list can be tested without a server.
from .settings import (  # noqa: E402
    CONF_ADMIN_SECRET, CONF_ADMIN_TOKEN, CONF_AFTER_CONTENT, CONF_ALIAS,
    CONF_AMAZON_URL,
    CONF_BRIDGE_URL, CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_ENDPOINT_PORT,
    CONF_CATALOG_PROVIDERS,
    CONF_EXPOSE_GROUPS, CONF_HANDOFF_PHRASE, CONF_LINK_SECRET, CONF_MA_SOURCE,
    CONF_OTP_SECRET, CONF_PASSWORD, CONF_PATCH_SUBSONIC_PLAYLISTS,
    CONF_PUBLIC_BASE, CONF_SERVE_ENDPOINT,
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
# seek republishes. See MaAlexaPlayer._seek_offset_ms.
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

# How long after fanning a stop out to a group's members before sending it
# once more. A member's playback state is not observable (it reports PAUSED
# while audibly playing group audio), so there is nothing to confirm against;
# one blind resend is cheap insurance against Amazon dropping a command.
GROUP_STOP_RESEND_DELAY = 2.0

# How long after a stop a PLAYING report is treated as an artefact rather than
# as a new session. Alexa emits one while buffering a Subsonic handover, and
# believing it cancelled the stop that had just been issued.
STOP_SETTLE_SECONDS = 12.0

# What Alexa calls each of the states we ask for. Sets rather than single
# values because Alexa reports the same intent under several names and which
# one arrives depends on how playback ended: an interrupted stream and a
# deliberate pause are both a pause as far as anyone listening is concerned.
PLAYING_STATES = frozenset({"PLAYING"})
PAUSED_STATES = frozenset({"PAUSED", "INTERRUPTED"})
IDLE_STATES = frozenset({"IDLE", "STOPPED", "FINISHED"})

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

# Where the cost of making a command stick is published. `extra_attributes`,
# not `extra_data`: MA's docstring for the latter says outright "not exposed on
# the API", and the `extra_data` key that does appear in the players payload is
# an alias *for* `extra_attributes`. Writing to the wrong one produced a check
# that ran, passed, and checked nothing.
#
# Flat scalars because EXTRA_ATTRIBUTES_TYPES is `str | int | float | bool |
# None`; a nested dict is not a legal value. Read by the live conformance
# suite; see `_note_convergence` for why these are monotonic totals rather
# than a description of the last command.
ATTR_RESENDS = "ma_alexa_resends"
ATTR_GAVE_UP = "ma_alexa_gave_up"
ATTR_LAST_LOST = "ma_alexa_last_lost"


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
    return MaAlexaProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
        #
        # cycle_after=True so a manual upload re-binds the skill itself, the
        # same as the scheduled sync already does. An upload unbinds the
        # provider slot without reporting it, so leaving enablement as a
        # separate button the operator had to remember was how a fresh upload
        # silently stopped answering while every diagnostic stayed green. The
        # cycle only fires when a real delta was uploaded (uploaded_any), so an
        # unchanged re-run does not needlessly flap the skill off and on.
        MaAlexaTasks(mass, logging.getLogger(__name__)).start_upload(
            cycle_after=True)
        wizard.remember(action, setup_ops.Outcome(
            True, "Started. Progress is in Music Assistant's task list."))
    elif action == wizard.ACTION_SETUP:
        # The one-button orchestrator. Skill and catalog creation are seconds
        # of SMAPI and run in a worker thread; the minutes-long upload, which
        # re-binds the skill on completion, is then handed to the task list so
        # the button returns at once with somewhere to watch it. Every step is
        # idempotent, so a re-press after a partial failure resumes rather than
        # duplicates, and the same button does an ongoing "Sync now" once
        # everything already exists.
        public_base = str(values.get(CONF_PUBLIC_BASE) or core.PUBLIC_BASE or "")
        outcome = await asyncio.to_thread(
            setup_ops.provision,
            alias=str(values.get(CONF_ALIAS) or ""),
            public_base=public_base,
            vendor=str(values.get(wizard.CONF_VENDOR_ID) or ""),
        )
        if outcome.ok:
            MaAlexaTasks(mass, logging.getLogger(__name__)).start_upload(
                cycle_after=True)
            outcome = setup_ops.Outcome(
                True,
                "Skill and catalogs are ready. Uploading your library now; "
                "progress is in Music Assistant's task list, and the skill "
                "enables itself when it finishes.",
                outcome.rows,
            )
        wizard.remember(wizard.ACTION_SETUP, outcome)
    elif action:
        # Blocking: SMAPI is a series of HTTPS round trips and the library
        # crawl is Subsonic calls. MA calls this from the event loop, so it
        # goes to a worker thread rather than stalling playback for everyone.
        await asyncio.to_thread(
            wizard.run, action, dict(values),
            str(values.get(CONF_PUBLIC_BASE) or core.PUBLIC_BASE or ""),
        )

    # The catalog-source picker offers the music providers MA has loaded, so a
    # person chooses from a list rather than typing instance ids. Music Assistant itself
    # is a music provider and is left out: it is the consumer of the catalog,
    # not a source for it.
    catalog_provider_options = [
        ConfigValueOption(title=prov.name, value=prov.instance_id)
        for prov in mass.get_providers(ProviderType.MUSIC)
        if prov.instance_id != instance_id
    ]

    return (
        *_settings_entries(catalog_provider_options),
        *await asyncio.to_thread(wizard.entries),
    )


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
        store_path=str(storage / "ma_alexa" / "push-auth.json"),
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
    if isinstance(provider, MaAlexaProvider):
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


class MaAlexaPlayer(Player):
    """An Echo device, or an Alexa speaker group, as a Music Assistant player."""

    # Class level so a player is never one attribute short of pollable,
    # whichever way it was constructed. refresh() deliberately leaves it alone:
    # a rediscovery pass is not evidence about whether the device is answering.
    _poll_failures = 0
    # Whether the last polled title resolved to an MA queue item. Class level
    # for the same reason as _poll_failures.
    _matched_last = False
    # Distinct raw `state` strings this player has reported, for learning what
    # /api/np/player actually emits. A set literal here would be shared by every
    # instance; the per-player set is created lazily in `_states_seen`.
    _states_seen_store: set[str] | None = None
    # How often Alexa is actually read, and when it last was. The tick is
    # always POLL_INTERVAL; this is what makes a tick decide to ask Amazon or
    # to only carry the position forward. Class level for the same reason as
    # the rest: a player built any other way must not arrive one attribute
    # short of pollable. Zero means "never read", so the first tick reads.
    _read_interval: float = POLL_INTERVAL
    _last_read_at = 0.0
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
    # When this player was last deliberately stopped, and last asked to play.
    #
    # Two timestamps rather than one boolean, because a boolean cannot order
    # itself. Push events, polls and commands all set this from different
    # tasks, and a flag raced: a group pause read as idle because a stop's flag
    # had not been cleared yet, while a subsonic stop read as paused because a
    # buffering PLAYING event had cleared it early. Comparing the two moments
    # asks the question that actually matters -- which happened last -- instead
    # of relying on every writer to have run in the right order.
    _stopped_at = 0.0
    _played_at = 0.0
    # The last thing anyone deliberately asked this player to do: play, pause
    # or stop. Alexa reports a stopped queue and a paused one identically, so
    # this is the only thing that can tell them apart.
    _intent = "play"
    # A one-off catch-up poll after a control, so the correction to an
    # optimistic answer does not wait for the ten second cycle.
    _resync: asyncio.Task | None = None
    # How far into the track the stream Alexa is playing was published to
    # start. Kept because Music Assistant adds it back on and this is the only
    # place that knows Alexa never took it off. See `_report_position`.
    _stream_offset_s = 0.0
    # The last track title Alexa itself reported, which is the only reliable
    # way to notice Alexa moving to the next track on its own. Alexa's names
    # and MA's names for the same track differ, so this is kept apart from
    # `_attr_current_media`.
    _polled_title = ""

    def __init__(
        self,
        provider: MaAlexaProvider,
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
    def _states_seen(self) -> set[str]:
        """Per-player set of raw states seen; lazily created so it is never
        shared across instances the way a mutable class attribute would be."""
        if self._states_seen_store is None:
            self._states_seen_store = set()
        return self._states_seen_store

    @property
    def provider_instance(self) -> MaAlexaProvider:
        return cast("MaAlexaProvider", self.provider)

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
        session. Music Assistant forms nothing. An Alexa Whole Home Audio group is
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
                # force, because this read is the whole point of the resync:
                # see the note in `poll`.
                await self.poll(force=True)
                self.update_state()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("resync poll failed: %s", err)

        self._resync = asyncio.create_task(run())

    def _report_position(self, seconds: float) -> None:
        """Record where Alexa is, in the units Music Assistant expects.

        Not the same units. Alexa reports absolute media time -- how far into
        the track it is -- because a seek here republishes the whole track with
        `stream.offsetInMilliseconds` and lets Alexa start partway in. MA
        assumes the opposite of a player that is not in flow mode: that it was
        handed a stream which *begins* at the seek point, so the player's own
        position is relative to that point and the offset has to be added back
        to get media time. From `PlayerQueues._update_queue_from_player`:

            elapsed_time = player_elapsed * speed
            if seek_pos := queue.current_item.streamdetails.seek_position:
                elapsed_time += seek_pos

        Reporting absolute time into that made every seek land twice: seek to
        60s in a 226s track and the queue read 120s, the scrubber jumped past
        where the audio was, and a second seek compounded it. Measured by the
        live suite as four cells failing deterministically, and it is the same
        defect behind a resume that asked Alexa to start "at 256000ms" in a
        256 second track.

        Subtracting here rather than not sending the offset at all, because the
        offset is how seek works on this player: Alexa has no seek command we
        can reach, so the track is republished from the new position. The two
        layers agree once each is speaking its own units.
        """
        self._attr_elapsed_time = max(0.0, seconds - self._stream_offset_s)
        # Local time, not Amazon's; see the note in `_push_now_playing`.
        self._attr_elapsed_time_last_updated = time.time()

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
        # A newer command supersedes every pending retry, not just the one of
        # its own kind. Measured 2026-08-03: a stop's confirm loop was still
        # running when the next play started, read the new playback as its own
        # stop having been dropped, and stopped the speaker three times over
        # twenty seconds. The loops each cancelled their own predecessor and
        # neither knew about the other.
        self._supersede_confirms()
        # Alexa speaker groups play natively; Music Assistant assumes they
        # cannot. `_handle_play_media` marks a player as natively playing --
        #
        #     elif player.type != PlayerType.GROUP:
        #         player.set_active_output_protocol("native")
        #
        # -- on the reasoning that a group delegates to a sync leader which
        # manages its own protocol. That is true of a group MA assembled out of
        # separate speakers and false of one Amazon assembled for us: "Whole
        # Apartment" is a single endpoint that accepts transport commands on
        # its own, and there is no leader to delegate to.
        #
        # Left unset, `_get_control_target(player, PAUSE, require_active=True)`
        # finds no active protocol and returns None, and `_handle_cmd_pause`
        # takes its fallback branch: "does not support pause, using STOP
        # instead". So every pause on a group arrived here as a stop -- which
        # is why a paused group reported idle, and why no `pause on Whole
        # Apartment` ever appeared in the log. Four fixes modelled that as our
        # own state handling being wrong. It was never reached.
        if self.is_group:
            self.set_active_output_protocol("native")
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
        alias = str(provider.config.get_value(CONF_ALIAS) or "ma_alexa")
        name = media.title or (items[0].name if items else "") or label

        offset_ms = self._seek_offset_ms(media)
        # Remembered for as long as Alexa is playing this stream, because every
        # position it reports back will be measured from the start of the track
        # and MA will add this on again. See `_report_position`.
        self._stream_offset_s = offset_ms / 1000.0
        # Whatever Alexa reports next belongs to this publish, so the next poll
        # must not read it as Alexa having moved on by itself and throw the
        # offset away.
        self._polled_title = ""
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
        self._played_at = time.monotonic()
        self._intent = "play"
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

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
        self._played_at = time.monotonic()
        self._intent = "play"
        self._confirm_transport("play", PLAYING_STATES, self.state_api.play)

    async def pause(self) -> None:
        await self._timed("pause", self.state_api.pause())
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()
        self._resync_soon()
        # A pause is not a play, but it does end a stop: a paused player is
        # one someone intends to resume.
        self._intent = "pause"
        self._confirm_transport("pause", PAUSED_STATES, self.state_api.pause)

    def _member_players(self) -> list[MaAlexaPlayer]:
        """The group's member speakers, as players we can command directly.

        An Alexa group is a single endpoint that plays *through* these speakers
        but does not forward every command to them, and exposes no readable
        per-member playback state. So when a command has to reach the speakers
        themselves rather than the group endpoint, this is how.
        """
        out: list[MaAlexaPlayer] = []
        for pid in self._attr_group_members:
            member = self.mass.players.get_player(pid)
            if isinstance(member, MaAlexaPlayer):
                out.append(member)
        return out

    async def _stop_group(self) -> None:
        """Stop a group by stopping each of its members directly.

        A stop sent to the group endpoint does not reach the members. Measured
        2026-08-04: five group stops in a row left the bedroom audibly playing
        while the group's own /api/np/player kept reporting PLAYING, and a
        single direct stop to each member device silenced the room at once.
        This is the third command with that shape -- pause and volume were the
        first two -- and the rule is the same: a group is not one device.

        No confirm loop, unlike the single-player path, because there is
        nothing to confirm against: a member reports PAUSED even while it is
        audibly playing group audio, so its state cannot distinguish a landed
        stop from a dropped one. Instead the stop is sent once now and once
        more shortly after, stored in the transport-confirm slot so a later
        play supersedes it -- the same blind-resend the volume fan-out uses,
        for the same reason.
        """
        members = self._member_players()
        if not members:
            # A group MA has registered but whose members we cannot resolve;
            # the group endpoint is a worse target than nothing, but it is all
            # that is left, so fall back rather than silently do nothing.
            await self.state_api.stop()
            return

        async def send() -> None:
            await asyncio.gather(
                *(m.state_api.stop() for m in members), return_exceptions=True)

        await send()

        async def resend() -> None:
            try:
                await asyncio.sleep(GROUP_STOP_RESEND_DELAY)
                await send()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - a resend must not raise
                self.logger.debug("%s: group stop resend failed: %s", self.name, err)

        self._supersede_confirms()
        self._transport_confirm = asyncio.create_task(resend())

    async def stop(self) -> None:
        if self.is_group:
            # A group endpoint does not forward a stop to its members; fan it
            # out to them directly. See `_stop_group`.
            await self._stop_group()
        else:
            await self.state_api.stop()
        # Remembered, because Alexa cannot express it. A music skill queue that
        # has been stopped reports PAUSED, exactly as a paused one does: there
        # is no third state to read, so every poll and every push after a stop
        # says "paused" and overwrites IDLE within seconds. Measured by the
        # conformance suite on five of six cells -- audio does stop, and Music
        # Assistant settles on paused anyway, which leaves a stopped speaker
        # looking like a paused one and MA's pause watchdog unarmed.
        #
        # Resending stop cannot fix that, because nothing was dropped. The
        # intent is what has to survive, so it is held here and consulted when
        # a reported PAUSED is turned into a state.
        self._stopped_at = time.monotonic()
        self._intent = "stop"
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_active_source = None
        self.update_state()
        if self.is_group:
            # A group has already armed its own blind resend in `_stop_group`,
            # and must not fall into the confirm loop below: that loop reads the
            # group's aggregate /api/np/player, which keeps reporting PLAYING
            # for a stop that actually landed on the members. That false
            # disagreement is the whole defect -- 25 resends and 5 give-ups in
            # one run against a stop that had already worked.
            return
        # Still confirmed, but against silence rather than against a word.
        # Alexa reports a stopped queue as PAUSED and a dropped stop as
        # PLAYING, so only the latter is worth resending; treating PAUSED as
        # failure would retry a command that already worked, and treating it as
        # success on its own left a dropped stop with nothing to repeat it.
        self._confirm_transport(
            "stop", PAUSED_STATES | IDLE_STATES, self.state_api.stop)

    async def power(self, powered: bool) -> None:
        """Handle a power command that Music Assistant should not have sent.

        `PlayerFeature.POWER` is not declared, and MA's own base class says
        this "will only be called if the PlayerFeature.POWER is supported"
        before raising NotImplementedError. That promise is not kept for a
        group. `_handle_cmd_power` runs, for the fake-power branch only:

            if player_state.type == PlayerType.GROUP:
                await player.power(powered)

        with no feature check, because a group MA assembled has to form or
        dissolve its sync session when the user toggles the fake switch.
        Fake power is offered in the Power Control dropdown for every player,
        so one config change is enough to reach that line, and `cmd_ungroup`
        on a group routes into the same place whenever power control is not
        NONE. An Echo has a real power state that Amazon owns and no API to
        change it, so the honest answer is to do the audible half and say so
        rather than to raise out of a UI toggle.
        """
        self.logger.info(
            "%s: power %s is not something an Echo exposes; %s",
            self.name, "on" if powered else "off",
            "leaving it alone" if powered else "stopping playback instead")
        if not powered:
            await self.stop()

    def _playback_state_for(self, reported: str) -> PlaybackState | None:
        """Turn what Alexa says into what this player should report.

        The one piece of interpretation between the two: PAUSED means paused,
        unless this player was deliberately stopped and has not played since,
        in which case it still means stopped.
        """
        state = (reported or "").upper()
        if state in PLAYING_STATES:
            # A report of PLAYING is only taken as evidence that a stop is over
            # once the stop has had a moment to settle. Alexa emits a spurious
            # PLAYING while it buffers a Subsonic handover, and taken at face
            # value that cancelled a stop issued a second earlier -- which is
            # how a stopped speaker went back to reporting paused. Later
            # reports are believed, because a play started by voice reaches
            # this provider no other way.
            # Distrusted only while a stop is the most recent thing anyone
            # asked for. The first version dropped the second condition and
            # suppressed PLAYING for twelve seconds after *any* stop, which
            # turned a deliberate play in that window into idle -- three group
            # pause cells failed on exactly that, because the suite plays again
            # moments after stopping.
            settling = (
                self._intent == "stop"
                and time.monotonic() - self._stopped_at <= STOP_SETTLE_SECONDS
            )
            if settling:
                return PlaybackState.IDLE
            self._played_at = time.monotonic()
            self._intent = "play"
            return PlaybackState.PLAYING
        if state in PAUSED_STATES:
            # Only a stop turns a reported PAUSED into idle. Inferring that
            # from timestamps was wrong twice: a boolean raced, and comparing
            # a stop time against a play time still said "stopped" for a group
            # that had since been asked to pause, because a pause is not a
            # play and never moved the play clock. The intent is a third thing
            # and is now recorded as one.
            return (PlaybackState.IDLE if self._intent == "stop"
                    else PlaybackState.PAUSED)
        if state in IDLE_STATES:
            return PlaybackState.IDLE
        return None

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

    def _supersede_confirms(self) -> None:
        """Drop every pending retry, because something newer was asked for.

        There are two of these loops -- one repeats a transport command, one
        repeats the `run_custom` that starts playback -- and until this existed
        each cancelled only its own predecessor. That left a retry able to
        outlive the command it belonged to and fight the next one:

            22:42:06  publish on Whole Apartment took 0.09s   <- a play starts
            22:42:15  stop did not stick (Alexa reports PLAYING); resending
            22:42:20  stop did not stick (Alexa reports PLAYING); resending
            22:42:25  stop landed on attempt 3

        Alexa was reporting PLAYING because playback had legitimately started
        nine seconds after the stop, and the loop read that as its own command
        having been dropped. Every retry loop here has to be cancellable by
        anything newer, not just by another of its own kind; otherwise it is
        not converging on what was asked for, it is arguing with it.
        """
        for attr in ("_transport_confirm", "_play_confirm"):
            if (task := getattr(self, attr, None)) is not None:
                task.cancel()
                setattr(self, attr, None)

    def _confirm_transport(self, what: str, wanted: frozenset[str],
                           send: Any) -> None:
        """Check a transport command landed, and repeat it if it did not."""
        # A newer command supersedes an older one rather than racing it. This
        # is also what stops the loop arguing with the operator: press play
        # while a pause is still converging and the pause gives up.
        self._supersede_confirms()
        self._transport_confirm = asyncio.create_task(
            self._converge_transport(what, wanted, send))

    def _note_convergence(self, what: str, *, resends: int, gave_up: bool) -> None:
        """Publish what it cost to make a command stick.

        This exists because the cost was invisible. Music Assistant answers every
        control optimistically -- it writes the state that was asked for and
        Music Assistant shows it immediately -- so a command Amazon dropped,
        resent twice and still lost looks from MA exactly like one that worked
        first time. The live conformance suite asserts on MA's view, which
        means a whole class of failure could not reach it: one run had 25
        transport resends and 5 outright give-ups on the speaker group while
        reporting six green stop cells, and that was found by reading logs
        rather than by the suite that exists to find it.

        Monotonic counters rather than a last-command field, because the
        reader is a test that snapshots before issuing a command and looks
        again after. A diff of two totals cannot go stale between those two
        reads; a "what happened most recently" field can, and would report the
        wrong command's outcome under exactly the concurrency this provider
        has.

        `extra_attributes` because that is what the players payload actually
        carries. `extra_data` is MA's own alias for it on the way out; the
        `Player.extra_data` dict of the same name is documented "not exposed
        on the API" and writing there was a check that ran and checked
        nothing.
        """
        attrs = self.extra_attributes
        attrs[ATTR_RESENDS] = attrs.get(ATTR_RESENDS, 0) + resends
        attrs[ATTR_GAVE_UP] = attrs.get(ATTR_GAVE_UP, 0) + int(gave_up)
        if gave_up:
            attrs[ATTR_LAST_LOST] = what
        # So the new totals reach Music Assistant, and the API payload the
        # suite reads, rather than sitting on the player until something
        # else happens to publish.
        self.update_state()

    async def _converge_transport(self, what: str, wanted: frozenset[str],
                                  send: Any) -> None:
        resends = 0
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
                    self._note_convergence(what, resends=resends, gave_up=False)
                    return

                if attempt == TRANSPORT_ATTEMPTS:
                    self.logger.warning(
                        "%s: %s did not stick after %s attempts (Alexa reports "
                        "%s)", self.name, what, attempt, observed)
                    self._note_convergence(what, resends=resends, gave_up=True)
                    return

                self.logger.info(
                    "%s: %s did not stick (Alexa reports %s); resending "
                    "(%s of %s)", self.name, what, observed, attempt,
                    TRANSPORT_ATTEMPTS - 1)
                resends += 1
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
        # Changes what is playing, so it supersedes a retry the same way a
        # transport command does. A stop still converging would otherwise stop
        # the track this just moved to -- one of the shapes behind "a track
        # change moves and then reverts".
        self._supersede_confirms()
        await self._timed("next", self.state_api.next())
        self._resync_soon()

    async def previous_track(self) -> None:
        self._supersede_confirms()
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
        resends = 0
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
                    self._note_convergence("volume", resends=resends, gave_up=False)
                    return

                if attempt == VOLUME_ATTEMPTS:
                    self.logger.warning(
                        "%s would not take %s%% after %s attempts (still %s)",
                        self.name, wanted, attempt, reported)
                    self._note_convergence("volume", resends=resends, gave_up=True)
                    return

                resends += 1
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
        resolved = self._playback_state_for(
            str(event.payload.get("audioPlayerState") or ""))
        if resolved is not None:
            self._attr_playback_state = resolved

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
        # The third place that turned an Alexa state string into a playback
        # state, and the one that kept undoing a stop. The poll and the player
        # state event were routed through `_playback_state_for` together; this
        # was missed, so a stop reached idle and a now-playing event a few
        # seconds later put it back to paused. Measured: stop said
        # "reached idle=True; state 5.5s later =paused" on four of six cells.
        #
        # Three independent opinions about what Alexa meant is two too many.
        resolved = self._playback_state_for(str(data.get("playerState") or ""))
        if resolved is not None:
            self._attr_playback_state = resolved

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

        # Local time, not Amazon's.
        #
        # This was `event.at` on the reasoning that it avoids adding delivery
        # delay to the reading. That was a bad trade. Delivery is under a tenth
        # of a second, while the two clocks are on different machines and
        # nothing keeps them in step; Music Assistant extrapolates the position
        # forward from this timestamp, so any skew becomes a scrubber that is
        # permanently wrong by the size of the skew, and a clock that is ahead
        # makes it run into the future.
        #
        # Milliseconds on the push stream and seconds on the polled endpoint,
        # which is the one thing the two paths genuinely disagree about.
        self._report_position(float(elapsed) / 1000.0)

    def use_push_poll_interval(self, slow: bool) -> None:
        """Slow the reads of Alexa while push is delivering, restore them when
        it is not.

        Reversible on purpose and driven by the stream's own health rather than
        by configuration. The failure this guards against is a stream that is
        connected and silent: if that is ever mistaken for a healthy one, the
        only cost is this interval instead of ten seconds.

        This throttles the *network* read, not the tick. Music Assistant keeps
        calling `poll` every `POLL_INTERVAL`; the ticks in between carry the
        position forward locally rather than asking Amazon again. See
        `_advance_position` for why that is not the optimistic-state pattern
        that has caused trouble in this provider before.
        """
        self._read_interval = (
            (PUSH_POLL_INTERVAL_SUPERVISED
             if alexapy_compat.supports_read_timeout()
             else PUSH_POLL_INTERVAL_UNSUPERVISED)
            if slow else POLL_INTERVAL
        )

    def _advance_position(self) -> None:
        """Carry the position forward on a tick that did not read Alexa.

        This is arithmetic, not a guess. Music Assistant already computes
        `corrected_elapsed_time` as `elapsed_time + (now - elapsed_time_last_updated)`,
        so advancing the stored pair by the same amount leaves every consumer
        that extrapolates reading exactly what it read before. It is a no-op
        for them by construction.

        It is not a no-op for the one consumer that reads the raw value:

            async def skip(self, queue_id, seconds=10):
                await self.seek(queue_id, int(self._queues[queue_id].elapsed_time + seconds))

        `PlayerQueues.skip` subtracts from the number as stored, and `previous`
        branches on it for its restart-versus-step-back rule. With the read
        slowed to a minute those two were deciding from a position up to a
        minute stale: a rewind of 20 seconds from a track half a minute in was
        computed against a stored 2.2s and refused. The live suite spent 216
        seconds on the six rewind cells, one of them 94.9s, waiting for a fresh
        publish that a healthy push stream had no reason to send.

        Only while playing. A paused or stopped player's position does not
        move, and advancing one would be the guess this is careful not to make.
        """
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        last = self._attr_elapsed_time_last_updated
        if not last:
            return
        now = time.time()
        if (moved := now - last) <= 0:
            # A clock that went backwards. Leave the pair alone rather than
            # rewind the scrubber.
            return
        self._attr_elapsed_time = (self._attr_elapsed_time or 0.0) + moved
        self._attr_elapsed_time_last_updated = now

    # -- state ---------------------------------------------------------------

    async def poll(self, *, force: bool = False) -> None:
        """Read playback state back off Alexa.

        Once Alexa is playing it owns the position, and nothing MA does to its
        own queue afterwards is reflected on the speaker. This is the direction
        the truth actually flows in.

        Not every tick reads Alexa. While the push stream is healthy the read
        is throttled to `_read_interval` and the ticks in between only carry
        the position forward, so Music Assistant's stored value stays current
        without costing an Amazon call. Amazon is asked at exactly the rate it
        was asked before this existed.

        `force` is what `_resync_soon` uses, and it is not optional. That
        resync is the correction for every optimistically-answered control in
        this class: the state is written to what was asked for and read back
        moments later. Letting it fall to the throttle would leave the read
        skipped and the optimistic value standing as though it had been
        confirmed, which is a check that reports success while checking
        nothing.
        """
        now = time.monotonic()
        # A player that is already failing is retried at the tick rate rather
        # than the throttled one. `_poll_failures` has to reach
        # POLL_FAILURES_BEFORE_UNAVAILABLE before a speaker is hidden, and
        # counting it up at the supervised interval would take six times as
        # long to notice an Echo that had gone; recovery would be equally slow
        # to be believed.
        throttled = not force and not self._poll_failures
        if throttled and now - self._last_read_at < self._read_interval:
            self._advance_position()
            self.update_state()
            return
        self._last_read_at = now
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
        # Learn what vocabulary /api/np/player actually emits, passively.
        #
        # Alexa's device-side AudioPlayer models six states -- the app's
        # `PlayerActivity` enum is IDLE, PLAYING, STOPPED, PAUSED, FINISHED,
        # BUFFER_UNDERRUN (see ma_provider/MOBILE-APP.md). Music Assistant's state sets
        # are ready for STOPPED and FINISHED, but the whole `_intent`/
        # `_stopped_at` stop-synthesis exists on the theory that this REST
        # endpoint sends PAUSED for a stopped queue instead, and the buffering
        # settle-window exists on the theory that it sends PLAYING for a
        # BUFFER_UNDERRUN. Both are guesses about a flattening nobody has
        # measured. This records the first sighting of every distinct raw
        # state, so ordinary use answers it: if STOPPED / FINISHED /
        # BUFFER_UNDERRUN ever actually arrive here, the corresponding hack can
        # be deleted in favour of reading the real state. Once per novel
        # string, so it is silent forever after the vocabulary is known.
        if state and state not in self._states_seen:
            self._states_seen.add(state)
            novel = state not in PLAYING_STATES | PAUSED_STATES | IDLE_STATES
            self.logger.info(
                "%s: /api/np/player reported state %r for the first time%s",
                self.name, state,
                " (UNMAPPED - a state Music Assistant does not model)" if novel else "")
        before = self._attr_playback_state
        # Through the same interpretation the push path uses, so a stop does
        # not survive on one and get overwritten on the other. This is the
        # whole of the difference between them; anything else here would be a
        # second, quieter opinion about what Alexa meant.
        # A state Alexa did not name is not a claim that nothing is playing.
        # Defaulting the unmapped case to IDLE is what turned a paused group
        # into an idle one on every source: a group reports something this does
        # not recognise, and the old dict literal quietly defaulted the same
        # way. Leaving the last known state alone is the same rule the rest of
        # `_apply_state` already follows for a field Alexa omitted.
        resolved = self._playback_state_for(state)
        if resolved is not None:
            self._attr_playback_state = resolved
        elif state:
            self.logger.debug(
                "%s: unmapped playback state %r, leaving it as %s",
                self.name, state, self._attr_playback_state)

        # Logged because a group's state decides whether its members are hidden
        # from the player picker, and every theory about what a cluster device
        # reports has so far been wrong. On change only: this runs per poll.
        if self.is_group and self._attr_playback_state != before:
            self.logger.info(
                "group %s reports %r -> %s (holding members: %s)",
                self.name, state or "<no state>", self._attr_playback_state,
                self.is_active_session)

        # A group has no volume of its own as far as Music Assistant is
        # concerned, so it must not claim one. Every volume path for a
        # `PlayerType.GROUP` is redirected away from the group before it can
        # reach a provider - `_handle_cmd_volume_set` and `cmd_volume_up/down`
        # all route to `set_group_volume`, which writes to the members and
        # never to the group - so nothing in MA will ever set this field. And
        # `Player.group_volume`, which is what MA actually displays for a
        # group, is the maximum over the members with `exclude_self=True`, so
        # nothing in MA ever reads it either.
        #
        # What Alexa reports here is the cluster device's own volume, which is
        # 0 on a group that is audibly playing. Left published, that is a
        # write-only field whose only effect is a slider reading zero on four
        # speakers you can hear. None says what is true: ask the members.
        volume = info.get("volume") or {}
        if not self.is_group:
            if isinstance(volume.get("volume"), int):
                self._reconcile_volume(volume["volume"])
            if isinstance(volume.get("muted"), bool):
                self._attr_volume_muted = volume["muted"]

        progress = info.get("progress") or {}
        text = info.get("infoText") or {}
        title = text.get("title") or ""
        previously_playing = self._attr_current_media is not None

        # Before the position is read, not after it. A track Alexa moved to on
        # its own started at its beginning, so the offset the previous one was
        # published with no longer applies; left standing for even one poll it
        # would be subtracted from a position it was never part of, and the
        # scrubber would sit at zero for a cycle at the top of every track that
        # followed a seek.
        #
        # Against the last title *Alexa* reported, not against
        # `_attr_current_media`. That holds whatever Music Assistant passed to
        # play_media until the first poll replaces it, and the two name the
        # same track differently -- MA has "Eminem - Without Me" where Alexa
        # says "Without Me". Comparing across them made every seek look like a
        # track change on the very next poll, which cleared the offset a second
        # after it was set and put the whole of it back into the position.
        # Measured: the speaker read 23.0s and MA 30.5s where they should have
        # read 15.5 and 23.0.
        if title and self._polled_title and self._polled_title != title:
            self._stream_offset_s = 0.0
        if title:
            self._polled_title = title

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
            self._report_position(float(elapsed))

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
        # A title that is in none of our queue items is external playback: a
        # voice command Alexa is running on its own, not a queue MA composed and
        # handed over. Say so, so MA shows this track rather than freezing on the
        # queue item it can no longer follow. "alexa" is one of MA's
        # EXTERNAL_SOURCES, which is the signal __final_active_source reads to
        # stop preferring the (now stale) MA queue and fall back to current_media.
        external = matched is None
        self._attr_active_source = "alexa" if external else self.player_id
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
            uri=f"ma_alexa://{self.player_id}/{title}",
            media_type=MediaType.TRACK,
            # Both of these, or neither counts. MA's
            # PlayerQueues._parse_player_current_item_id will only believe a
            # queue_item_id when source_id names the queue as well, and its
            # fallbacks parse a Sonos uri or an MA stream url, neither of which
            # an ma_alexa:// uri can ever look like. Without source_id the queue
            # index never advances, so MA went on showing whatever track the
            # queue was holding when it arrived while the speakers played on:
            # observed with a group three tracks ahead of the display. A
            # player's own queue is keyed by its player_id. On external playback
            # there is no queue of ours to name, and naming one would re-arm the
            # stale queue as the active source through set_active_mass_source.
            source_id=None if external else self.player_id,
            title=title,
            artist=kept(text.get("subText1"), "artist"),
            album=kept(text.get("subText2"), "album"),
            image_url=kept((info.get("mainArt") or {}).get("url"), "image_url"),
            duration=kept(seconds, "duration"),
            queue_item_id=kept(matched, "queue_item_id"),
        )


class MaAlexaProvider(PlayerProvider):
    """Discovers Echo devices and speaker groups and drives them via Music Assistant."""

    login: AlexaLogin
    bridge: BridgeClient

    def _minted(self, key: str, what: str, length: int = 32,
                factory: Any = None) -> str:
        """A secret this instance generates for itself, once, and keeps.

        Standalone Music Assistant takes these from environment variables. Music
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
        self.logger.info("generated %s for this Music Assistant instance", what)
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
        self.logger.info("ma_alexa provider build %s", build_stamp())
        self._discovery_lock = asyncio.Lock()

        # Hosting the endpoint ourselves is the point of phase 5: the Alexa
        # skill, the audio proxy and the player provider all run in this
        # process. The separate Flask deployment is still supported, because
        # Music Assistant works for anyone with a Subsonic server whether or not they
        # run Music Assistant, and pointing at one is what turning this off
        # means.
        # Music Assistant does not hand providers an environment, so the
        # settings the standalone deployment reads from env vars are injected
        # here instead. Before anything starts serving, because every stream
        # and art URL Amazon fetches is built from the public base.
        #
        # The storage path is MA's, plus a directory of Music Assistant's own. Without
        # it the defaults are absolute paths chosen for a container this
        # service owned, and inside MA they land in MA's storage root beside
        # library.db.
        core.configure(
            public_base=str(self.config.get_value(CONF_PUBLIC_BASE) or ""),
            storage_path=str(pathlib.Path(self.mass.storage_path) / "ma_alexa"),
            subsonic_url=str(self.config.get_value(CONF_SUBSONIC_URL) or ""),
            subsonic_user=str(self.config.get_value(CONF_SUBSONIC_USER) or ""),
            subsonic_password=str(self.config.get_value(CONF_SUBSONIC_PASSWORD) or ""),
            signing_key=self._minted(CONF_SIGNING_KEY, "a signing key"),
            admin_token=self._minted(CONF_ADMIN_SECRET, "an admin token"),
            oauth_client_id=self._minted(
                CONF_CLIENT_ID, "an account linking client id",
                factory=lambda: f"ma-alexa-{self.instance_id[:8]}"),
            oauth_client_secret=self._minted(CONF_CLIENT_SECRET,
                                             "an account linking client secret"),
            oauth_link_secret=self._link_passphrase(),
            # Copied into the setup-state file core reads per queue. The entry
            # requires a reload, so this init re-runs and the file tracks the UI.
            after_content=str(self.config.get_value(CONF_AFTER_CONTENT) or ""),
            # Same rail: the catalog crawl reads these from the state file. A
            # list (possibly empty) rather than a string, so it is passed as-is.
            catalog_providers=list(
                self.config.get_value(CONF_CATALOG_PROVIDERS) or []),
            # Names the handoff catalog entity after the same phrase the spoken
            # command uses, so an MA-composed queue can be claimed by voice.
            handoff_phrase=str(self.config.get_value(CONF_HANDOFF_PHRASE) or ""),
            # The live MA instance and its loop, so the resolver can read tracks
            # from MA's library. handle_async_init runs on the loop, so this is
            # the running loop the bridge's worker threads bounce their MA calls
            # onto.
            mass=self.mass,
            loop=asyncio.get_running_loop(),
        )

        self.webserver: MaAlexaWebServer | None = None
        serving = False
        if self.config.get_value(CONF_SERVE_ENDPOINT, True):
            port = int(self.config.get_value(CONF_ENDPOINT_PORT) or DEFAULT_PORT)
            self.webserver = MaAlexaWebServer(self.logger, port=port)
            serving = await self.webserver.start()
            # Advertise the endpoint over MA's shared zeroconf responder, once
            # the listener is actually up. Optional and a no-op unless MDNS is
            # set; never allowed to fail the load, since a name is a convenience
            # and the endpoint is reachable by address regardless.
            if serving:
                with contextlib.suppress(Exception):
                    await mdns.advertise(self.mass, port)

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
        # of Music Assistant's. Registered whether or not it is switched on, because the
        # schedule and its enabled flag are things MA renders and the operator
        # edits; leaving it unregistered would hide the feature entirely.
        self.tasks = MaAlexaTasks(
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
                pathlib.Path(self.mass.storage_path) / "ma_alexa" / "push-auth.json"
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
        for unregister in getattr(self, "_debug_unregisters", ()):
            with contextlib.suppress(Exception):
                unregister()
        with contextlib.suppress(Exception):
            subsonic_patch.restore(self.logger)
        # Withdraw the mDNS name from MA's shared responder before the endpoint
        # it points at goes away. A no-op unless advertise() published one.
        with contextlib.suppress(Exception):
            await mdns.stop()
        # Tear down the module-level thread pools so a reload does not leak the
        # old worker threads and stack a second pool on top. Each rebuilds
        # lazily on next use.
        from . import mastream_cache, queue_api

        for module in (core, queue_api, mastream_cache):
            with contextlib.suppress(Exception):
                module.shutdown_pool()
        if self.webserver is not None:
            await self.webserver.stop()
            self.webserver = None

    async def loaded_in_mass(self) -> None:
        self._apply_subsonic_patch()
        await self._login()
        await self.discover_players()
        await self._start_push()
        self._register_debug_commands()

    def _apply_subsonic_patch(self) -> None:
        """Speed up MA's OpenSubsonic playlist resolution in-process (see subsonic_patch).

        Gated by a setting and self-disabling once the installed MA carries the
        upstream fix. Never allowed to break provider load, hence the suppress.
        """
        if not self.config.get_value(CONF_PATCH_SUBSONIC_PLAYLISTS, True):
            return
        with contextlib.suppress(Exception):
            subsonic_patch.apply(self.logger)

    # -- growing-token debug commands ----------------------------------------
    #
    # Two commands exist only to make the growing-token spike's live test
    # runnable. Under MA the bridge is in-process, so the HTTP /queue endpoints
    # are auth-dead (ADMIN_TOKEN is a standalone-only env var); the only way to
    # publish and then append IN the process that serves GetNextItem is from
    # inside MA. These run there, callable through MA's own authenticated API
    # (tools/ma.sh). They are not part of playback; the eventual feature would
    # append from a queue-growth listener, not a command. See PLAN phase 8.

    def _register_debug_commands(self) -> None:
        self._debug_unregisters = []
        for name, handler in (
            ("ma_alexa/queue_publish", self._api_queue_publish),
            ("ma_alexa/queue_append", self._api_queue_append),
        ):
            with contextlib.suppress(Exception):
                self._debug_unregisters.append(
                    self.mass.register_api_command(name, handler))

    async def _api_queue_publish(
        self, track_ids: list[str], name: str = "growing-token test"
    ) -> dict[str, Any]:
        """Publish an ext: queue in-process and return its content id.

        Seeds a specific queue as the newest published token so the
        play-music-assistant handoff hands Alexa exactly this queue.
        """
        from . import queue_api

        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            None, lambda: queue_api.publish([str(t) for t in track_ids], name))
        return {"content_id": f"{queue_api.CONTENT_PREFIX}:{record['token']}",
                "count": len(record["tracks"])}

    async def _api_queue_append(
        self, token: str, track_ids: list[str]
    ) -> dict[str, Any]:
        """Append to a published ext: queue in-process.

        The cache invalidation `append_to_queue` does is per-process; running
        it here lands it in the process that serves GetNextItem, which is the
        whole point of the live test.
        """
        from . import queue_api

        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            None, lambda: queue_api.append_to_queue(str(token),
                                                    [str(t) for t in track_ids]))
        if record is None:
            return {"ok": False, "error": "unknown or expired token"}
        return {"ok": True, "count": len(record["tracks"]),
                "requested": record["requested"]}

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
        if player is None or not isinstance(player, MaAlexaPlayer):
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
        # poll interval is a statement about how Music Assistant learns things and has
        # no business being applied to a Chromecast.
        for player in self.players:
            if isinstance(player, MaAlexaPlayer):
                player.use_push_poll_interval(connected)
        if connected:
            # Everything that happened while the stream was down is invisible
            # to it, so the first thing a reconnect does is ask.
            self.mass.create_task(self._repoll_all())

    async def _repoll_all(self) -> None:
        for player in self.players:
            if isinstance(player, MaAlexaPlayer):
                with contextlib.suppress(Exception):
                    # force, for the reason in the caller: this catch-up read
                    # exists precisely because the stream missed something, so
                    # a throttle that skipped it would skip the catch-up.
                    await player.poll(force=True)

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
            if not isinstance(player, MaAlexaPlayer):
                continue
            if player.is_group:
                # Same reason as the poll path in `_apply_state`: a group's own
                # volume is a field MA never writes and never reads, and the
                # value Alexa has for it is 0.
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
        if isinstance(existing, MaAlexaPlayer):
            existing.refresh(
                device, name, speaker=speaker, member_ids=member_ids
            )
            existing.update_state()
            return

        await self.mass.players.register_or_update(
            MaAlexaPlayer(
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
            entry: str | dict[str, Any] | None = None
            subsonic_id = _subsonic_id(item)
            if subsonic_id is not None:
                # A Subsonic track, streamed straight from Navidrome. Published
                # as a pre-resolved record built from the metadata Music
                # Assistant already handed over, so the bridge does not fetch it
                # back with a getSong per track -- which is the one round trip
                # that made a large queue publish slowly. Falls back to the bare
                # id when the item carries no usable title, letting the bridge
                # fetch it the old way for that one track.
                entry = self._subsonic_track(item, subsonic_id) or subsonic_id
            elif allow_ma:
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

    def _subsonic_track(
        self, item: QueueItem, subsonic_id: str
    ) -> dict[str, Any] | None:
        """A Subsonic track as a pre-resolved record, from MA's own metadata.

        The audio still streams from Navidrome by id -- a finite file the bridge
        proxies straight through, no disk copy, which is why a Subsonic id is
        preferred over the MA path for the same track. What changes is the
        *metadata*: title, artist, album and duration are taken from the queue
        item Music Assistant already handed over, rather than fetched back with
        a getSong per track at publish time. That per-track fetch is the whole
        cost of publishing a large queue, and MA has all of it in hand, so a
        30k-track queue can publish without touching the server.

        Shaped like a getSong record so nothing downstream can tell the
        difference: no `ref`, so `build_item` streams it from Subsonic and
        renders it exactly as a fetched one. `coverArt` is set to the song id,
        which Navidrome resolves to the album cover; a server that does not is
        left with no art rather than a broken image, which `art_block`
        tolerates. `source` marks it so the bridge routes it past the getSong.

        Returns None when the item has no usable title, so the caller can fall
        back to the bare id and let the bridge fetch that one the old way.
        """
        media_item = getattr(item, "media_item", None)
        title = (getattr(media_item, "name", "") or getattr(item, "name", "")
                 or "").strip()
        if not title:
            return None
        artists = getattr(media_item, "artists", None) or ()
        album = getattr(media_item, "album", None)
        duration = getattr(item, "duration", None) or getattr(media_item, "duration", 0)
        song: dict[str, Any] = {
            "source": "subsonic",
            "id": subsonic_id,
            "title": title,
            "artist": ", ".join(a.name for a in artists if getattr(a, "name", "")),
            "album": getattr(album, "name", "") or "",
            "coverArt": subsonic_id,
        }
        try:
            seconds = int(duration or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            song["duration"] = seconds
        return song

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


def group_target(player: MaAlexaPlayer) -> str | None:
    """The name to append as `on <target>`, or None for a single speaker.

    Exposed for tests and for anyone reading the utterance logic without
    Music Assistant installed.
    """
    return sanitize(player.group_name) or None
