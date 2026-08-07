"""Setup, as Music Assistant config entries instead of a web page.

The web wizard is eight numbered steps, each one a panel that unlocks when the
previous is done. Music Assistant has no pages, but it has enough to say the
same thing: entries carry a `category` that renders as a group heading, an
`ACTION` type that renders as a button, and `hidden` to keep a step out of
sight until it can be reached. The upstream `yandex_smarthome` provider builds
a three-step skill-creation flow exactly this way, so this is the sanctioned
shape rather than an invention.

What does not carry over is the 1,600 lines of template, the vendored CSS, the
session cookie, the login form and the lockout counter. Music Assistant is
already authenticated and already has a settings UI, so all of that was
scaffolding for a page that no longer needs to exist.

The step model is not reimplemented here. `setup_steps.STEPS` already names
the eight steps and, more importantly, already derives each one's completion
from the thing itself rather than from a flag written when a button was
pressed. That distinction is what keeps this honest when a skill is deleted in
Amazon's console or credentials are revoked out from under it, so this module
renders that model and does not attempt to keep a second copy of the truth.
"""

from __future__ import annotations

from dataclasses import replace

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from . import setup_ops
from . import setup_state as store
from . import setup_steps
from . import smapi_rest

# Action keys. Prefixed so they cannot collide with a setting key: Music
# Assistant puts both in the same namespace, and an action that shadowed a
# stored value would overwrite it with the action's own name.
ACTION_CHECK_SERVER = "action_check_server"
ACTION_CHECK_ENDPOINT = "action_check_endpoint"
ACTION_CONNECT_AMAZON = "action_connect_amazon"
ACTION_DISCONNECT_AMAZON = "action_disconnect_amazon"
ACTION_CREATE_SKILL = "action_create_skill"
ACTION_FORGET_SKILL = "action_forget_skill"
ACTION_CREATE_CATALOGS = "action_create_catalogs"
ACTION_UPLOAD = "action_upload"
ACTION_ENABLE = "action_enable"
ACTION_DISABLE = "action_disable"
ACTION_TEARDOWN = "action_teardown"

# The one-button orchestrator. Not in ACTIONS below and not handled by run():
# it needs both a worker thread (create skill and catalogs) and Music
# Assistant's task controller (the minutes-long upload), so the provider
# handles it directly rather than through the blocking run() path the granular
# actions share.
ACTION_SETUP = "action_setup"

# The four steps that are genuine prerequisites, answered by fields and consent
# rather than by anything Music Assistant can run, and the four that are provisioning
# operations the orchestrator folds into one press.
DATA_KEYS = ("server", "endpoint", "amazon", "alias")
PROV_KEYS = ("skill", "catalogs", "upload", "enable")

# Where the collapsed views live. The finished summary sits under a plain
# "Music Assistant" heading; the granular re-run actions fold behind the "Show advanced
# settings" toggle via their advanced flag, under an "Advanced" heading.
#
# The heading is "Advanced", not "advanced": the frontend discards a fixed set
# of special category names -- including the literal "advanced" -- from the
# sections it draws, so an entry categorised "advanced" never renders even with
# the toggle on. Capitalised, it is an ordinary section the flag can gate. See
# the same note in settings.py.
SUMMARY_CATEGORY = "Music Assistant"
ADVANCED_CATEGORY = "Advanced"

# Settings the wizard steps need that are not part of ordinary operation.
CONF_LWA_CLIENT_ID = "lwa_client_id"
CONF_LWA_CLIENT_SECRET = "lwa_client_secret"
CONF_VENDOR_ID = "vendor_id"
CONF_TEARDOWN_CONFIRM = "teardown_confirm"

# Where the last action's result is kept between renders. Music Assistant
# rebuilds the whole form after running an action and hands back only the
# stored values, so a result that is not written down somewhere is a button
# that reports nothing at all.
_LAST: dict[str, setup_ops.Outcome] = {}


def remember(action: str, outcome: setup_ops.Outcome) -> None:
    _LAST[action] = outcome


def _result_label(action: str) -> str:
    outcome = _LAST.get(action)
    if outcome is None:
        return ""
    mark = "OK" if outcome.ok else "Failed"
    lines = [f"{mark}: {outcome.detail}" if outcome.detail else mark]
    for row in outcome.rows:
        state = "ok" if row.get("ok") else "failed"
        # Rows come from two places: per-catalog results keyed `kind`/`detail`,
        # and endpoint check rows keyed `name`/`note`. Both are rendered here
        # rather than normalized at the source, because the check rows also
        # carry response diagnostics that a normalizing pass would drop.
        name = row.get("kind") or row.get("name") or ""
        detail = row.get("detail") or row.get("note") or ""
        lines.append(f"  {name}: {state}, {detail}")
    return "\n".join(lines)


def _category(index: int, title: str) -> str:
    """Numbered so the groups render in order and read as a sequence."""
    return f"Step {index + 1} - {title}"


def entries(action: str | None = None) -> list[ConfigEntry]:
    """The whole setup form, as it stands right now.

    Rebuilt from scratch on every render because every step's completion is
    derived rather than stored, so a form built once and cached would be a
    snapshot of a past that may no longer be true.

    Three shapes, chosen from that derived state:

    - Set up: the four prerequisite steps as a numbered route, then the four
      provisioning steps collapsed into a single "Set up Music Assistant" button.
    - Complete: a one-line summary and a "Sync now" button, with every granular
      step folded under Advanced so nothing the rail could do is lost.

    The eight separate step-cards the rail used to show are gone from the
    primary view either way; they survive under Advanced for re-run and
    recovery, which is the whole point of the collapse.
    """
    state = store.load()
    rows = setup_steps.progress(state)
    done = {row["key"]: row["done"] for row in rows}

    if setup_steps.complete(state):
        return _complete_form(state, done)

    data_ready = all(done[key] for key in DATA_KEYS)
    built: list[ConfigEntry] = []

    # The prerequisites stay a numbered route. Each genuinely needs the one
    # before it, and there is nothing to automate about typing a URL or
    # approving Amazon's consent page, so folding them into a button would only
    # hide what the operator still has to do.
    for index, row in enumerate(rows):
        if row["key"] not in DATA_KEYS:
            continue
        category = _category(index, row["title"])
        # A step is shown when it is reachable or already finished. Hiding the
        # rest is what turns a wall of thirty fields into one next thing to do.
        hidden = not (row["reachable"] or row["done"])
        built.extend(_step_entries(row["key"], state, done, category, hidden))

    # The four provisioning steps, as one action.
    built.extend(_setup_group(state, done, data_ready))

    # The granular provisioning actions stay reachable under Advanced, but only
    # once the prerequisites are met: a Create the skill button that refuses
    # until Amazon is connected is the wall of unusable fields the rail's
    # progressive disclosure existed to prevent. Before then the one Set up
    # button, hidden behind its hint, is the whole of the provisioning view.
    if data_ready:
        built.extend(_advanced_actions(state, done, PROV_KEYS))

    return built


def _setup_group(state: dict, done: dict[str, bool],
                 data_ready: bool) -> list[ConfigEntry]:
    """The provisioning half of setup, collapsed into one button.

    Numbered Step 5 so it reads as the continuation of the four prerequisite
    steps above it. Hidden behind a hint until the prerequisites are met,
    because a skill cannot be created before Amazon is connected and the
    endpoint has been shown to work.
    """
    category = _category(len(DATA_KEYS), "Set up Music Assistant")
    if not data_ready:
        return [_status(
            "Finish the steps above and Music Assistant sets itself up in one action: "
            "it creates the skill, creates the five catalogs, uploads your "
            "library, and enables the skill.",
            "setup", category, hidden=False)]

    built = [_status(
        "One action creates the skill, creates the catalogs, uploads your "
        "library and enables the skill. Anything already finished is skipped, "
        "so it is safe to run again if it stops partway.",
        "setup", category, hidden=False)]
    built.append(_action(
        ACTION_SETUP, "Set up Music Assistant",
        "Runs the remaining setup in order. The library upload is minutes of "
        "work, so it is handed to Music Assistant's task list and the skill "
        "enables itself once that finishes.",
        category, hidden=False))
    if text := _result_label(ACTION_SETUP):
        built.append(_status(text, "setup_result", category, hidden=False))
    return built


def _complete_form(state: dict, done: dict[str, bool]) -> list[ConfigEntry]:
    """What the page is once setup is finished: a summary, Sync now, Advanced.

    The eight-step rail has served its purpose and would now be eight green
    ticks to scroll past, so it collapses to one sentence and the button that
    keeps a working install current. Every individual step moves under Advanced
    rather than away: a skill deleted in Amazon's console, or a catalog that
    needs recreating, still has somewhere to be fixed from.
    """
    alias = state.get("alias") or "ma_alexa"
    built = [_status(
        f'Music Assistant is set up. Say "ask {alias} to play ..." on any Echo. Use '
        "Sync now after adding music; every individual step is under Advanced "
        "if you ever need to re-run one.",
        "summary", SUMMARY_CATEGORY, hidden=False)]
    built.append(_action(
        ACTION_SETUP, "Sync now",
        "Uploads anything that changed and re-binds the skill. Unchanged "
        "catalogs are skipped, so this is cheap to run whenever you add music.",
        SUMMARY_CATEGORY, hidden=False))
    if text := _result_label(ACTION_SETUP):
        built.append(_status(text, "setup_result", SUMMARY_CATEGORY,
                             hidden=False))
    built.extend(_advanced_actions(
        state, done, tuple(step.key for step in setup_steps.STEPS)))
    return built


def _advanced_actions(state: dict, done: dict[str, bool],
                      keys: tuple[str, ...]) -> list[ConfigEntry]:
    """The named steps, folded behind the advanced toggle for re-run/recovery.

    The same per-step entries the rail rendered, each stamped advanced=True so
    the "Show advanced settings" toggle is what reveals them. The advanced flag
    is what hides them; the ADVANCED_CATEGORY heading is only their label, and
    is deliberately "Advanced" rather than "advanced" because the frontend
    discards the literal lower-case category (see the note on the constant).
    This is what makes the collapse lossless: the primary view is one button,
    and nothing the eight-step rail could do has gone missing, it has only moved
    behind a disclosure.
    """
    built: list[ConfigEntry] = []
    for key in keys:
        for entry in _step_entries(
                key, state, done, ADVANCED_CATEGORY, hidden=False):
            built.append(replace(entry, advanced=True))
    return built


def _status(text: str, key: str, category: str, hidden: bool,
            advanced: bool = False) -> ConfigEntry:
    return ConfigEntry(
        key=f"label_{key}",
        type=ConfigEntryType.LABEL,
        label=text,
        required=False,
        category=category,
        hidden=hidden,
        advanced=advanced,
    )


def _action(key: str, label: str, description: str, category: str,
            hidden: bool, advanced: bool = False) -> ConfigEntry:
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.ACTION,
        label=label,
        action=key,
        action_label=label,
        description=description,
        required=False,
        category=category,
        hidden=hidden,
        advanced=advanced,
    )


def _step_entries(key: str, state: dict, done: dict[str, bool],
                  category: str, hidden: bool) -> list[ConfigEntry]:
    """One step's worth of the form.

    The first four steps are answered by settings that already exist on the
    provider (the music server, the public base, the invocation name), so they
    contribute a status line and nothing else: repeating the fields here would
    put two editors on one value.
    """
    if key == "server":
        built = [_status(
            "Configured. Music Assistant streams every track from this server."
            if done["server"] else
            "Fill in the music server URL, username and password above.",
            key, category, hidden)]
        # Configured is not the same as reachable, and the difference does not
        # surface until Alexa is asked for something and gets silence.
        built.append(_action(ACTION_CHECK_SERVER, "Test the connection",
                             "Runs one search against the library.",
                             category, hidden))
        if text := _result_label(ACTION_CHECK_SERVER):
            built.append(_status(text, "server_result", category, hidden))
        return built

    if key == "endpoint":
        return _endpoint_entries(done, category, hidden)

    if key == "amazon":
        return _amazon_entries(state, done, category, hidden)

    if key == "alias":
        return [_status(
            f"The invocation name is \"{state.get('alias')}\"."
            if done["alias"] else
            "Set the skill invocation name above. It becomes the word you say "
            "after \"ask\", so it must not be one Alexa will confuse with an "
            "artist or album in your own library.",
            key, category, hidden)]

    if key == "skill":
        return _skill_entries(state, done, category, hidden)

    if key == "catalogs":
        return _catalog_entries(done, category, hidden)

    if key == "upload":
        return _upload_entries(done, category, hidden)

    if key == "enable":
        return _enable_entries(done, category, hidden)

    return []


def _endpoint_entries(done: dict[str, bool], category: str,
                      hidden: bool) -> list[ConfigEntry]:
    """Step two, which has to be an explicit check rather than a wait.

    Completion is satisfied either by this check passing or by a signed
    request from Amazon having arrived, and live traffic is much the stronger
    evidence. But it cannot be the only route: Amazon does not call until a
    skill exists, and the skill step refuses to run until this one is done, so
    waiting for traffic here would be a deadlock in the middle of setup.
    """
    built = [_status(
        "Amazon can reach this service."
        if done["endpoint"] else
        "Set the public base URL above, then check it. Every failure this "
        "catches is one Amazon does not report: it simply never calls, and "
        "the skill sits there looking created.",
        "endpoint", category, hidden)]
    built.append(_action(
        ACTION_CHECK_ENDPOINT, "Check the endpoint",
        "Resolves the name, opens TLS, and sends a real directive over the "
        "public URL.", category, hidden))
    if text := _result_label(ACTION_CHECK_ENDPOINT):
        built.append(_status(text, "endpoint_result", category, hidden))
    return built


def _amazon_entries(state: dict, done: dict[str, bool], category: str,
                    hidden: bool) -> list[ConfigEntry]:
    connected = done["amazon"]
    if connected:
        status = "Connected. Music Assistant can manage skills and catalogs on this account."
    else:
        status = (
            "Register a Login with Amazon security profile in the Amazon "
            "developer console, allow the redirect URL below, then paste the "
            "client ID and secret here and press Connect.\n"
            f"Redirect URL: {smapi_rest.redirect_uri() or '(set the public base URL first)'}"
        )
    built = [_status(status, "amazon", category, hidden)]

    if connected:
        built.append(_action(
            ACTION_DISCONNECT_AMAZON, "Disconnect from Amazon",
            "Deletes the stored refresh token. The skill and catalogs on "
            "Amazon are left alone.", category, hidden))
    else:
        built.extend([
            ConfigEntry(
                key=CONF_LWA_CLIENT_ID,
                type=ConfigEntryType.STRING,
                label="Login with Amazon client ID",
                required=False, category=category, hidden=hidden,
            ),
            ConfigEntry(
                key=CONF_LWA_CLIENT_SECRET,
                type=ConfigEntryType.SECURE_STRING,
                label="Login with Amazon client secret",
                required=False, category=category, hidden=hidden,
            ),
            _action(ACTION_CONNECT_AMAZON, "Connect to Amazon",
                    "Opens Amazon's consent page. Approve there and the link "
                    "completes on its own.", category, hidden),
        ])

    if text := _result_label(ACTION_CONNECT_AMAZON) or _result_label(
        ACTION_DISCONNECT_AMAZON
    ):
        built.append(_status(text, "amazon_result", category, hidden))
    return built


def _skill_entries(state: dict, done: dict[str, bool], category: str,
                   hidden: bool) -> list[ConfigEntry]:
    recorded = state.get("skill_id") or ""
    built: list[ConfigEntry] = []

    if done["skill"]:
        built.append(_status(f"Created: {recorded}", "skill", category, hidden))
    elif recorded:
        # Recorded here, gone on Amazon: deleted outside this wizard. Offering
        # creation would fail on the duplicate guard, so offer the cure.
        built.append(_status(
            f"The recorded skill ({recorded}) no longer exists on Amazon. "
            "Discard the record to create a new one.",
            "skill", category, hidden))
        built.append(_action(ACTION_FORGET_SKILL, "Discard the stale record",
                             "Removes the local record only. There is nothing "
                             "left on Amazon to delete.", category, hidden))
    else:
        built.append(_status(
            "Creates a development-stage music skill on your Amazon developer "
            "account, pointed at this service.", "skill", category, hidden))
        built.append(ConfigEntry(
            key=CONF_VENDOR_ID,
            type=ConfigEntryType.STRING,
            label="Vendor ID",
            description="Leave empty unless the account has more than one "
                        "vendor, in which case Amazon rejects an ambiguous "
                        "request rather than choosing for you.",
            required=False, category=category, hidden=hidden,
        ))
        built.append(_action(ACTION_CREATE_SKILL, "Create the skill",
                             "Takes a few seconds: the manifest is validated "
                             "after creation and a rejected skill is deleted "
                             "again rather than left behind.",
                             category, hidden))
        # Alexa routes an invocation across every enabled music skill, so a
        # leftover from an earlier install fights the new one for the same
        # words. The symptom is intermittent, which makes it very hard to
        # recognise later and very easy to point at here.
        if competing := setup_ops.existing_music_skills():
            named = ", ".join(f"{s['name'] or s['id']} ({s['stage']})"
                              for s in competing)
            built.append(_status(
                "Already on this account and competing for the invocation "
                f"name: {named}. Alexa routes across every enabled music "
                "skill, so remove these in the Amazon developer console or "
                "expect the wrong one to answer some of the time.",
                "skill_competing", category, hidden))

    if text := _result_label(ACTION_CREATE_SKILL) or _result_label(
        ACTION_FORGET_SKILL
    ):
        built.append(_status(text, "skill_result", category, hidden))
    return built


def _catalog_entries(done: dict[str, bool], category: str,
                     hidden: bool) -> list[ConfigEntry]:
    built = [_status(
        "All five catalogs exist and are associated with the skill."
        if done["catalogs"] else
        "Creates five catalogs, one per entity type, and associates each with "
        "the skill so spoken names have somewhere to resolve against. Safe to "
        "run again: existing catalogs are reused rather than duplicated.",
        "catalogs", category, hidden)]
    built.append(_action(ACTION_CREATE_CATALOGS, "Create the catalogs", "",
                         category, hidden))
    if text := _result_label(ACTION_CREATE_CATALOGS):
        built.append(_status(text, "catalogs_result", category, hidden))
    return built


def _upload_entries(done: dict[str, bool], category: str,
                    hidden: bool) -> list[ConfigEntry]:
    built = [_status(
        "Your library is uploaded and Amazon has ingested it."
        if done["upload"] else
        "Sends your artists, albums, tracks, playlists and genres to Amazon so "
        "Alexa can recognize them by name. This takes minutes on a real "
        "library; it runs in the background and reports progress in Music "
        "Assistant's task list.",
        "upload", category, hidden)]
    built.append(_action(ACTION_UPLOAD, "Upload the library", "",
                         category, hidden))
    if text := _result_label(ACTION_UPLOAD):
        built.append(_status(text, "upload_result", category, hidden))
    return built


def _enable_entries(done: dict[str, bool], category: str,
                    hidden: bool) -> list[ConfigEntry]:
    built = [_status(
        "Enabled. Alexa routes matching requests to this skill."
        if done["enable"] else
        "Binds the skill to your Amazon account. Without this Alexa answers "
        "from your default music provider and says so out loud, while this "
        "service looks healthy because it is never asked anything.",
        "enable", category, hidden)]
    built.append(_action(
        ACTION_ENABLE, "Enable the skill",
        "Also re-puts the manifest, which is what actually fixes a skill that "
        "resolves searches but never starts playback. Run this again after "
        "every catalog upload: an upload unbinds the provider slot without "
        "reporting it.", category, hidden))
    if done["enable"]:
        built.append(_action(
            ACTION_DISABLE, "Disable the skill",
            "Alexa stops routing to it and answers from your default music "
            "provider instead. The skill and catalogs are left alone.",
            category, hidden))
    if text := _result_label(ACTION_ENABLE) or _result_label(ACTION_DISABLE):
        built.append(_status(text, "enable_result", category, hidden))

    # Starting over lives at the end of the last step rather than in a
    # category of its own, because it is not a ninth thing to do. It is the
    # way back to the first.
    recorded = setup_ops.skill_id()
    if recorded:
        built.append(ConfigEntry(
            key=CONF_TEARDOWN_CONFIRM,
            type=ConfigEntryType.STRING,
            label="Remove the skill: type the skill id to confirm",
            description=(
                f"Deletes {recorded} and its five catalogs. Not recoverable: "
                "the replacement has to be uploaded and ingested from nothing, "
                "which on a real library is an hour of Amazon's time."),
            required=False, category=category, hidden=hidden,
        ))
        built.append(_action(ACTION_TEARDOWN, "Remove the skill and catalogs",
                             "", category, hidden))
        if text := _result_label(ACTION_TEARDOWN):
            built.append(_status(text, "teardown_result", category, hidden))
    return built


# --------------------------------------------------------------------------
# running an action
# --------------------------------------------------------------------------


def run(action: str, values: dict, public_base: str = "") -> setup_ops.Outcome:
    """Perform one wizard action and remember what it said.

    Blocking, because everything under it is: SMAPI is HTTPS round trips and
    the library crawl is Subsonic calls. The caller is responsible for keeping
    it off the event loop.

    Unknown actions return rather than raise. Music Assistant sends the action
    key of whatever button was pressed, and a provider that raised on an
    unrecognized one would turn a stale browser tab into a broken settings
    page.
    """
    if action == ACTION_CHECK_SERVER:
        outcome = setup_ops.probe_music_server()

    elif action == ACTION_CHECK_ENDPOINT:
        outcome = setup_ops.check_endpoint(public_base)

    elif action == ACTION_CONNECT_AMAZON:
        outcome = setup_ops.begin_amazon_link(
            str(values.get(CONF_LWA_CLIENT_ID) or ""),
            str(values.get(CONF_LWA_CLIENT_SECRET) or ""),
        )
        if outcome.ok:
            # The URL is the answer, not a message: there is no browser here to
            # redirect, so the operator is given the link to open themselves.
            outcome = setup_ops.Outcome(
                True, f"Open this to approve, then come back:\n{outcome.detail}")

    elif action == ACTION_DISCONNECT_AMAZON:
        outcome = setup_ops.disconnect_amazon()

    elif action == ACTION_CREATE_SKILL:
        outcome = setup_ops.create_skill(
            alias=str(values.get("alias") or ""),
            public_base=public_base,
            vendor=str(values.get(CONF_VENDOR_ID) or ""),
        )

    elif action == ACTION_FORGET_SKILL:
        outcome = setup_ops.forget_skill()

    elif action == ACTION_CREATE_CATALOGS:
        outcome = setup_ops.create_catalogs()

    elif action == ACTION_ENABLE:
        outcome = setup_ops.cycle_enablement(setup_ops.skill_id())

    elif action == ACTION_DISABLE:
        outcome = setup_ops.disable_skill(setup_ops.skill_id())

    elif action == ACTION_TEARDOWN:
        outcome = setup_ops.teardown(str(values.get(CONF_TEARDOWN_CONFIRM) or ""))

    else:
        return setup_ops.Outcome(False, "")

    remember(action, outcome)
    return outcome


ACTIONS = (
    ACTION_CHECK_SERVER, ACTION_CHECK_ENDPOINT, ACTION_CONNECT_AMAZON, ACTION_DISCONNECT_AMAZON, ACTION_CREATE_SKILL,
    ACTION_FORGET_SKILL, ACTION_CREATE_CATALOGS, ACTION_UPLOAD, ACTION_ENABLE,
    ACTION_DISABLE, ACTION_TEARDOWN,
)
