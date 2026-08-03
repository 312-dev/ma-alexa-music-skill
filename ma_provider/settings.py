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

from music_assistant_models.config_entries import ConfigEntry
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
        *_generated_entries(),
    )


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


