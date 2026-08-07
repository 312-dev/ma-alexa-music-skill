"""Every setting this provider has, and its config form.

Separate from `provider.py` for one practical reason: that module imports the
whole of Music Assistant and alexapy, so nothing about the settings could be
tested without a full server installed. The settings are also the half most
worth testing, because a mistake in them does not raise.

Music Assistant persists only values that have a matching entry here and drops
the rest without a word. On 2026-08-03 a migration wrote eleven settings, two
were declared and saved, and nine were discarded: the endpoint answered,
healthz was green, ten Echo players registered, and the linked Alexa account
was dead because the signing key had gone. So the entry list is not a
description of the settings. It is the definition of which ones can exist.
"""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from .webserver import DEFAULT_PORT

# Music Assistant's own names for the two it treats specially. Spelled out
# rather than imported from `music_assistant.constants`, because importing
# that would pull the whole server in and put this module back out of reach of
# the tests. Asserted against the real constants where MA is installed.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

CONF_AMAZON_URL = "url"
CONF_OTP_SECRET = "secret"
CONF_BRIDGE_URL = "bridge_url"
CONF_ADMIN_TOKEN = "admin_token"
CONF_ALIAS = "alias"
CONF_HANDOFF_PHRASE = "handoff_phrase"
CONF_EXPOSE_GROUPS = "expose_groups"
CONF_MA_SOURCE = "ma_source"
CONF_CATALOG_PROVIDERS = "catalog_providers"
CONF_SERVE_ENDPOINT = "serve_endpoint"
CONF_ENDPOINT_PORT = "endpoint_port"
CONF_PUBLIC_BASE = "public_base"
CONF_SUBSONIC_URL = "subsonic_url"
CONF_SUBSONIC_USER = "subsonic_user"
CONF_SUBSONIC_PASSWORD = "subsonic_password"
CONF_SIGNING_KEY = "signing_key"
CONF_ADMIN_SECRET = "admin_secret"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_LINK_SECRET = "link_secret"

# Continuation mode: what plays once the requested queue runs out. Named to
# match the key `effective_after_content` reads out of the setup-state file, so
# the provider can mirror the chosen value straight across with no translation.
# The five modes and their meaning live in core.AFTER_CONTENT_MODES; kept as a
# literal here rather than imported because importing core pulls the whole
# server in and would put this module back out of reach of the tests.
CONF_AFTER_CONTENT = "after_content"
AFTER_CONTENT_OPTIONS = (
    ("stop", "Stop when the queue ends"),
    ("artist", "Keep playing this artist"),
    ("genre", "Keep playing this genre"),
    ("library", "Shuffle the whole library"),
    ("radio", "Play a radio station from here"),
)

# Live updates. The status entry holds no value and exists to render a
# sentence; the action is a button. Neither is a setting in the usual sense,
# which is why they are grouped under a category of their own rather than
# scattered among the credentials they sit next to.
CONF_PUSH_ENABLED = "push_enabled"
CONF_PUSH_STATUS = "push_status"
ACTION_PUSH_SIGN_IN = "push_sign_in"
PUSH_CATEGORY = "Live updates"

# Section headings for the everyday settings. Without a category every entry
# renders in one long undifferentiated column, which is what made the page feel
# like a wall; grouping the ~20 flat entries into a few named sections is the
# whole of Part A. Order of first appearance sets section order, so the tuple
# below is arranged Voice & playback first, then Connections.
#
# The category is only the heading. What folds an entry behind the "Show
# advanced settings" toggle is the separate ConfigEntry.advanced flag, so the
# rarely touched ports and tokens below carry advanced=True as well as this
# heading.
#
# The heading must not be the literal string "advanced", though: the frontend
# keeps a fixed set of special categories -- "preferences", "display_settings",
# "generic", "advanced", "protocol_general" -- and drops any of those from the
# named sections it renders. An entry categorised "advanced" therefore lands in
# a bucket that is never drawn, so the toggle reveals nothing however correct
# the flag is. "Advanced" (capitalised) is an ordinary category name that draws
# a normal section, which the advanced flag then gates.
VOICE_CATEGORY = "Voice & playback"
CONNECTIONS_CATEGORY = "Connections"
ADVANCED_CATEGORY = "Advanced"

# In-process fix for MA's OpenSubsonic playlist resolution (see subsonic_patch).
CONF_PATCH_SUBSONIC_PLAYLISTS = "patch_subsonic_playlists"



def _settings_entries(
    catalog_provider_options: list[ConfigValueOption] | None = None,
) -> tuple[ConfigEntry, ...]:
    return (
        # -- Voice & playback: what a person tunes after it works, and the
        # everyday behaviour of the skill. First so it heads the page.
        ConfigEntry(
            key=CONF_ALIAS,
            type=ConfigEntryType.STRING,
            label="Skill invocation name",
            default_value="music assistant",
            category=VOICE_CATEGORY,
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
            default_value="my mix",
            category=VOICE_CATEGORY,
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
            key=CONF_AFTER_CONTENT,
            type=ConfigEntryType.STRING,
            label="When the queue ends",
            default_value="stop",
            category=VOICE_CATEGORY,
            options=[
                ConfigValueOption(title=title, value=value)
                for value, title in AFTER_CONTENT_OPTIONS
            ],
            description=(
                "What happens once the songs you asked for run out. Stop is the "
                "default; the others keep music going by seeding from the last "
                "track, so a queue never simply falls silent unless you want it "
                "to."
            ),
            required=False,
            # The value is read from the setup-state file, and the provider only
            # copies the config across into that file at init, so a change has
            # to reload for it to take hold.
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_CATALOG_PROVIDERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Catalog these music providers for voice search",
            category=VOICE_CATEGORY,
            # The catalog is built from these providers' entries in MA's synced
            # library, so "play <thing> on music assistant" resolves anything MA has
            # indexed (Plex, Tidal, Spotify, a local folder, an OpenSubsonic
            # server), keyed by MA library id rather than one server's ids. The
            # list is the music providers MA has loaded, filled in live by
            # get_config_entries. Nothing is catalogued until at least one is
            # chosen; there is no implicit source.
            options=catalog_provider_options or [],
            default_value=[],
            description=(
                "Pick the music providers whose library Alexa can search by "
                "name. Nothing is catalogued until you choose at least one. A "
                "whole large library can be big enough to matter against "
                "Amazon's per-catalog limits, so start with the providers you "
                "actually ask for by name."
            ),
            # Read into the setup-state file at init, like the after-content
            # mode, and the crawl reads it from there.
            requires_reload=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_EXPOSE_GROUPS,
            type=ConfigEntryType.BOOLEAN,
            label="Expose Alexa speaker groups as players",
            default_value=True,
            category=VOICE_CATEGORY,
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
            category=VOICE_CATEGORY,
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
        # -- Connections: the credentials and origins wired once at setup. Grouped
        # so the day-to-day voice settings above are not buried under them.
        ConfigEntry(
            key=CONF_SERVE_ENDPOINT,
            type=ConfigEntryType.BOOLEAN,
            label="Serve the Alexa endpoint from Music Assistant",
            default_value=True,
            category=CONNECTIONS_CATEGORY,
            description=(
                "On, Music Assistant listens on its own port in this process and no "
                "separate service is needed. Point your reverse proxy at that "
                "port. Off, it expects a standalone Music Assistant deployment at the "
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
            category=CONNECTIONS_CATEGORY,
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
            category=CONNECTIONS_CATEGORY,
            description=(
                "Your Subsonic-compatible server, for example "
                "http://navidrome.local:4533. Music Assistant streams every track from "
                "here; it is not required to be reachable from the internet, "
                "because Amazon fetches audio through Music Assistant rather than "
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
            category=CONNECTIONS_CATEGORY,
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_SUBSONIC_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Music server password",
            category=CONNECTIONS_CATEGORY,
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=True,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_AMAZON_URL,
            type=ConfigEntryType.STRING,
            label="Amazon domain",
            default_value="amazon.com",
            category=CONNECTIONS_CATEGORY,
            required=True,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Amazon account email",
            category=CONNECTIONS_CATEGORY,
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Amazon account password",
            category=CONNECTIONS_CATEGORY,
            required=True,
        ),
        ConfigEntry(
            key=CONF_OTP_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Amazon two-factor secret",
            category=CONNECTIONS_CATEGORY,
            description=(
                "The TOTP seed from Amazon's authenticator-app setup, not a "
                "six digit code. Leave empty if two-factor is off."
            ),
            required=False,
        ),
        # -- Advanced (Music Assistant's own collapsed section): ports, the
        # standalone-bridge fields, and the OpenSubsonic speedup. Rarely touched,
        # folded away by default.
        ConfigEntry(
            key=CONF_ENDPOINT_PORT,
            type=ConfigEntryType.INTEGER,
            label="Endpoint port",
            default_value=DEFAULT_PORT,
            category=ADVANCED_CATEGORY,
            advanced=True,
            description=(
                "The port Music Assistant listens on. A port of its own rather than "
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
            label="Music Assistant bridge URL",
            category=ADVANCED_CATEGORY,
            advanced=True,
            # No default. A placeholder default is worse than none here: it
            # saves as a real value, so a field left untouched looks configured
            # and fails later at publish time with a confusing error.
            description=(
                "Base URL of a standalone Music Assistant bridge, for example "
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
            category=ADVANCED_CATEGORY,
            advanced=True,
            description="The bridge's ADMIN_TOKEN. Sent as X-Admin-Token.",
            required=False,
            depends_on=CONF_SERVE_ENDPOINT,
            depends_on_value=False,
        ),
        ConfigEntry(
            key=CONF_PATCH_SUBSONIC_PLAYLISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Speed up OpenSubsonic playlist playback",
            default_value=True,
            category=ADVANCED_CATEGORY,
            advanced=True,
            description=(
                "Music Assistant resolves an OpenSubsonic playlist by fetching "
                "the full album and lyrics for every track, which makes a large "
                "playlist take minutes to start. On, Music Assistant replaces that with a "
                "single playlist fetch in the running process. It automatically "
                "does nothing once your Music Assistant already includes the "
                "upstream fix, so it is safe to leave on."
            ),
            required=False,
            requires_reload=True,
        ),
        *_push_entries(),
        *_generated_entries(),
    )


# --------------------------------------------------------------------------
# live updates
# --------------------------------------------------------------------------
#
# Amazon can push device state instead of being asked for it on a timer, and
# the difference is worth a settings group of its own: measured against
# Amazon's own event clock, a push arrives within a tenth of a second of Amazon
# knowing, where a poll adds up to its whole interval on top.
#
# It needs a bearer token, which the ordinary credential fields above cannot
# produce. Hence a button rather than a field: there is nothing here for anyone
# to type.


def _push_entries() -> tuple[ConfigEntry, ...]:
    """The live-updates group: a switch, a status line and a sign-in button."""
    return (
        ConfigEntry(
            key=CONF_PUSH_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            label="Live updates",
            default_value=True,
            category=PUSH_CATEGORY,
            description=(
                "Let Amazon report volume, playback and track position the "
                "moment they change, instead of asking every few seconds. "
                "Polling continues either way, more slowly, so nothing is "
                "lost if the connection drops."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_PUSH_STATUS,
            type=ConfigEntryType.LABEL,
            label=push_status_text(),
            category=PUSH_CATEGORY,
            required=False,
        ),
        ConfigEntry(
            key=ACTION_PUSH_SIGN_IN,
            type=ConfigEntryType.ACTION,
            label="Connect Amazon for live updates",
            action=ACTION_PUSH_SIGN_IN,
            category=PUSH_CATEGORY,
            description=(
                "Opens Amazon's own sign-in page so a captcha or a two-factor "
                "prompt can be answered. This is a separate authorization from "
                "the developer account used to create the skill, because "
                "Amazon issues them from different places: this one is the "
                "account your Echo devices are registered to."
            ),
            required=False,
        ),
    )


# Rewritten by the provider as the connection state changes, so the label above
# is rendered fresh on every settings load without this module needing to know
# anything about Music Assistant. A module level string is enough: there is one
# provider instance per Amazon account and the status is about the account.
_PUSH_STATUS = "Not connected. Live updates are off until you connect."


def set_push_status(text: str) -> None:
    global _PUSH_STATUS
    _PUSH_STATUS = text


def push_status_text() -> str:
    return _PUSH_STATUS


# The values this provider generates for itself. Declared, and hidden.
#
# Declaring them is not decoration. Music Assistant's `config/providers/save`
# only persists values that have a matching entry and drops the rest without
# saying so, so before these were declared a migration that wrote all eleven
# settings saved two of them and reported success. Every other signal agreed:
# the endpoint answered, healthz was green, ten players registered, and the
# linked Alexa account was broken because the signing key had been discarded.
#
# Hidden because there is still nothing here for anyone to decide, and a
# visible field for the signing key is an invitation to break every URL
# currently in flight. Same shape upstream's yandex_smarthome provider uses to
# round-trip its own generated artifacts.
GENERATED = (
    (CONF_SIGNING_KEY, "Signing key"),
    (CONF_ADMIN_SECRET, "Admin token"),
    (CONF_CLIENT_ID, "Account linking client id"),
    (CONF_CLIENT_SECRET, "Account linking client secret"),
    (CONF_LINK_SECRET, "Account linking passphrase"),
)


def _generated_entries() -> tuple[ConfigEntry, ...]:
    return tuple(
        ConfigEntry(
            key=key,
            # Not SECURE_STRING: Music Assistant replaces a secure value with a
            # placeholder on the way out and treats the placeholder coming back
            # as "unchanged", which is right for a field a person edits and
            # wrong for one a migration has to write.
            type=ConfigEntryType.STRING,
            label=f"{label} (generated)",
            required=False,
            hidden=True,
        )
        for key, label in GENERATED
    )


