"""The setup rail, as Music Assistant config entries.

These need only music_assistant_models, not the whole of Music Assistant, so
unlike the provider tests they run everywhere. That matters: this is the code
that decides whether an operator can complete setup at all, and it would
otherwise be the least tested code in the project.
"""

from __future__ import annotations

import pytest

pytest.importorskip("music_assistant_models")

from ma_provider import core, setup_ops, setup_state as store, subsonic  # noqa: E402
from ma_provider import setup_steps  # noqa: E402
from ma_provider import wizard  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A state file and a capture directory of this test's own."""
    monkeypatch.setattr(store, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "captures")
    monkeypatch.setattr(core, "PUBLIC_BASE", "https://music.example.test")
    monkeypatch.setattr(subsonic, "BASE", "http://navidrome.test")
    monkeypatch.setattr(subsonic, "USER", "tester")
    wizard._LAST.clear()
    # Amazon is never actually connected in these tests, so the steps that ask
    # it anything must not reach the network.
    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: False)
    yield
    wizard._LAST.clear()


def by_key(entries) -> dict:
    return {entry.key: entry for entry in entries}


def visible(entries) -> list[str]:
    return [entry.key for entry in entries if not entry.hidden]


# --- the rail ---------------------------------------------------------------


def test_the_prerequisites_stay_a_numbered_route():
    """The four prerequisites still read as a sequence.

    Music Assistant sorts nothing for us: a category is a string, and unordered
    headings would read as unrelated settings sections rather than as a route.
    The numbering is what keeps server -> endpoint -> amazon -> alias in order;
    the four provisioning steps after them are the collapse's job, not the
    rail's, so they are deliberately not numbered here.
    """
    categories = [entry.category for entry in wizard.entries()]
    for index, key in enumerate(wizard.DATA_KEYS):
        title = setup_steps.BY_KEY[key].title
        assert f"Step {index + 1} - {title}" in categories


def test_the_provisioning_steps_collapse_into_one_action(monkeypatch):
    """The four buttons the rail used to show are one 'Set up Music Assistant' now.

    Their numbered categories are gone from the primary view; the provisioning
    actions live under Advanced instead. This is the whole point of Part C:
    one button in front, the granular steps a disclosure away. The Advanced
    actions only appear once the prerequisites are met, so Amazon is faked
    connected to reach that state without touching the network.
    """
    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(setup_ops, "existing_music_skills",
                        lambda exclude="": [])
    store.update(endpoint_ok=True, alias="ma_alexa")  # server + endpoint + alias

    categories = [entry.category for entry in wizard.entries()]
    assert f"Step {len(wizard.DATA_KEYS) + 1} - Set up Music Assistant" in categories
    for key in wizard.PROV_KEYS:
        title = setup_steps.BY_KEY[key].title
        assert f"Step {index_of(key)} - {title}" not in categories
    # Every provisioning action is still reachable, behind the advanced toggle.
    keys = by_key(wizard.entries())
    for action in (wizard.ACTION_CREATE_SKILL, wizard.ACTION_CREATE_CATALOGS,
                   wizard.ACTION_UPLOAD, wizard.ACTION_ENABLE):
        assert keys[action].category == wizard.ADVANCED_CATEGORY
        # The flag, not the category name, is what the toggle reads.
        assert keys[action].advanced is True


def index_of(key: str) -> int:
    return next(i for i, step in enumerate(setup_steps.STEPS)
               if step.key == key) + 1


def _make_complete(monkeypatch):
    """Drive the derived state to all-done without touching the network."""
    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(setup_steps, "_skill_exists",
                        lambda skill_id, max_age=60.0: True)
    monkeypatch.setattr(setup_steps, "_ingestion_complete",
                        lambda state, max_age=60.0: True)
    store.update(endpoint_ok=True, alias="ma_alexa", skill_id="s",
                 catalogs={"artists": "cat-1"}, uploads={"artists": "up-1"},
                 enabled=True)


def test_a_finished_setup_collapses_to_a_summary_and_sync(monkeypatch):
    """Eight green ticks are not a route, so once everything is done the rail
    becomes one sentence and the button that keeps it current."""
    _make_complete(monkeypatch)
    assert setup_steps.complete(store.load())

    keys = by_key(wizard.entries())
    assert "ma_alexa" in keys["label_summary"].label.lower()
    assert keys[wizard.ACTION_SETUP].action_label == "Sync now"
    assert keys[wizard.ACTION_SETUP].category == wizard.SUMMARY_CATEGORY
    # The summary and its one button are primary, never behind the toggle.
    assert keys["label_summary"].advanced is False
    assert keys[wizard.ACTION_SETUP].advanced is False
    # No numbered step categories survive the collapse.
    assert not [e for e in wizard.entries()
                if (e.category or "").startswith("Step ")]


def test_a_finished_setup_keeps_every_step_reachable_under_advanced(monkeypatch):
    """The collapse is lossless: teardown, disconnect, re-enable and the rest
    move under Advanced rather than away."""
    _make_complete(monkeypatch)
    keys = by_key(wizard.entries())
    for action in (wizard.ACTION_CHECK_SERVER, wizard.ACTION_CHECK_ENDPOINT,
                   wizard.ACTION_DISCONNECT_AMAZON, wizard.ACTION_CREATE_CATALOGS,
                   wizard.ACTION_UPLOAD, wizard.ACTION_ENABLE,
                   wizard.ACTION_TEARDOWN):
        assert action in keys, action
        assert keys[action].category == wizard.ADVANCED_CATEGORY, action
        # Present but behind the "Show advanced settings" toggle.
        assert keys[action].advanced is True, action


def test_a_step_that_cannot_be_reached_yet_is_hidden():
    """One next thing to do, not a wall of thirty fields.

    With nothing done past the music server, everything from the Amazon
    account onward is unreachable, and showing an operator a Create the skill
    button they cannot use is how a form teaches people to ignore it.
    """
    entries = wizard.entries()
    shown = visible(entries)

    assert "label_server" in shown
    assert "label_endpoint" in shown
    assert "action_create_skill" not in shown
    assert "action_upload" not in shown


def test_finishing_a_step_reveals_the_next_one():
    store.update(endpoint_ok=True, alias="ma_alexa")
    shown = visible(wizard.entries())

    assert "action_connect_amazon" in shown
    assert "label_alias" in shown
    # Still not the skill: Amazon is not connected.
    assert "action_create_skill" not in shown


def test_the_endpoint_step_can_be_checked_rather_than_only_waited_on():
    """Otherwise setup deadlocks in the middle.

    Completion also accepts a signed request from Amazon, which is much
    stronger evidence. But Amazon does not call until a skill exists, and the
    skill step refuses to run until this one is done, so an explicit check has
    to exist or neither side can ever go first.
    """
    assert "action_check_endpoint" in visible(wizard.entries())


def test_a_finished_step_stays_visible():
    """Going back to read a finished step is not the same as redoing it."""
    store.update(endpoint_ok=True, alias="ma_alexa")
    shown = visible(wizard.entries())
    assert "label_server" in shown and "label_endpoint" in shown


# --- what the entries say ---------------------------------------------------


def test_the_amazon_step_shows_the_redirect_url_to_register():
    """It cannot be guessed and setup cannot proceed without it.

    The operator has to paste this into Amazon's console before consent will
    work, so a step that named everything except the one value they need would
    send them to the source to find it.
    """
    store.update(endpoint_ok=True, alias="ma_alexa")
    label = by_key(wizard.entries())["label_amazon"].label
    assert "https://music.example.test/setup/oauth/callback" in label


def test_a_missing_public_base_says_so_instead_of_showing_half_a_url(monkeypatch):
    monkeypatch.setattr(core, "PUBLIC_BASE", "")
    store.update(endpoint_ok=True, alias="ma_alexa")
    label = by_key(wizard.entries())["label_amazon"].label
    assert "set the public base URL first" in label
    assert "/setup/oauth/callback" not in label


def test_a_stale_skill_record_offers_the_cure_not_the_creation(monkeypatch):
    """Recorded here, gone on Amazon: deleted outside this wizard.

    Offering Create the skill would hit the duplicate guard and refuse, which
    reads as the button being broken.

    Only detectable while connected: a definitive 404 is the evidence, and
    with no credentials the step deliberately keeps its last answer rather
    than un-finishing itself over a lookup it cannot make.
    """
    from ma_provider import setup_steps

    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(setup_steps.smapi_rest, "connected", lambda: True)

    def gone(_skill_id):
        raise setup_steps.smapi_rest.SmapiError("not found", status=404)

    monkeypatch.setattr(setup_steps.smapi_rest, "skill_status", gone)
    setup_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    store.update(endpoint_ok=True, alias="ma_alexa", skill_id="amzn1.ask.skill.x")
    keys = by_key(wizard.entries())
    assert "action_forget_skill" in keys
    assert "action_create_skill" not in keys
    assert "no longer exists on Amazon" in keys["label_skill"].label


def test_no_action_key_collides_with_a_setting_key():
    """MA puts both in one namespace.

    An action sharing a key with a stored setting would have the action's own
    name written over that setting the moment the button was pressed.
    """
    from ma_provider import settings as provider_settings

    keys = {entry.key for entry in provider_settings._settings_entries()}
    assert keys.isdisjoint(set(wizard.ACTIONS))


def test_no_two_entries_share_a_key():
    """A duplicate key silently wins or loses depending on render order."""
    keys = [entry.key for entry in wizard.entries()]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]


def test_no_step_entry_is_required():
    """Music Assistant refuses to load a provider whose required entries are
    empty, and `depends_on` does not relax that. A required field anywhere in
    the setup rail would take every Echo player off the list over setup that
    has not been done yet."""
    assert not [entry.key for entry in wizard.entries() if entry.required]


# --- running an action ------------------------------------------------------


def test_an_action_reports_what_it_did_on_the_next_render(monkeypatch):
    """MA rebuilds the form after an action and hands back only stored values,
    so a result that is not remembered is a button that reports nothing.

    The granular actions live under Advanced once the prerequisites are met, so
    the setup has to be past them for a create-catalogs result to have anywhere
    to render. Amazon is faked connected rather than actually reached, and the
    one derived check that would hit the network is stubbed."""
    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(setup_steps, "_skill_exists",
                        lambda skill_id, max_age=60.0: True)
    wizard.remember(wizard.ACTION_CREATE_CATALOGS,
                    setup_ops.Outcome(True, "5 of 5 catalogs ready",
                                      [{"kind": "artists", "ok": True,
                                        "detail": "cat-1"}]))
    store.update(endpoint_ok=True, alias="ma_alexa", skill_id="s",
                 catalogs={"artists": "cat-1"})
    label = by_key(wizard.entries())["label_catalogs_result"].label
    assert "5 of 5 catalogs ready" in label
    assert "artists: ok, cat-1" in label


def test_a_failure_is_reported_as_one(monkeypatch):
    monkeypatch.setattr(wizard.smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(setup_ops, "existing_music_skills",
                        lambda exclude="": [])
    wizard.remember(wizard.ACTION_CREATE_SKILL,
                    setup_ops.Outcome(False, "Amazon rejected the manifest"))
    store.update(endpoint_ok=True, alias="ma_alexa")
    label = by_key(wizard.entries())["label_skill_result"].label
    assert label.startswith("Failed")
    assert "Amazon rejected the manifest" in label


def test_connecting_hands_back_a_url_to_open(monkeypatch):
    """There is no browser to redirect, so the link is the answer."""
    monkeypatch.setattr(setup_ops, "begin_amazon_link",
                        lambda cid, secret, origin="":
                        setup_ops.Outcome(True, "https://amazon.test/consent"))
    outcome = wizard.run(wizard.ACTION_CONNECT_AMAZON,
                         {wizard.CONF_LWA_CLIENT_ID: "cid",
                          wizard.CONF_LWA_CLIENT_SECRET: "sec"})
    assert outcome.ok
    assert "https://amazon.test/consent" in outcome.detail


def test_an_unknown_action_does_not_raise():
    """MA sends whichever button was pressed. A stale tab must not be able to
    turn the settings page into a traceback."""
    assert wizard.run("action_from_an_old_tab", {}).ok is False


def test_creating_the_skill_passes_the_public_base_through(monkeypatch):
    """The manifest is built from it, and a wrong one is accepted by Amazon
    and then simply never called."""
    seen = {}
    monkeypatch.setattr(setup_ops, "create_skill",
                        lambda **kw: seen.update(kw) or setup_ops.Outcome(True))
    wizard.run(wizard.ACTION_CREATE_SKILL, {"alias": "ma_alexa"},
               public_base="https://music.example.test")
    assert seen["public_base"] == "https://music.example.test"
    assert seen["alias"] == "ma_alexa"


# --- what a migration can actually persist -----------------------------------


def test_every_generated_secret_is_a_declared_entry():
    """Music Assistant drops values that have no matching entry.

    It does it silently and reports success. Measured on 2026-08-03: a cutover
    wrote eleven settings, two were declared and saved, and nine were
    discarded. Nothing said so. The endpoint answered, healthz was green, ten
    players registered, and the linked Alexa account was dead because the
    signing key had gone.

    So anything a migration has to be able to write must be declared, however
    little it belongs in the UI.
    """
    from ma_provider import settings

    declared = {entry.key for entry in settings._settings_entries()}
    for key, _label in settings.GENERATED:
        assert key in declared, key


def test_the_generated_entries_are_hidden_and_optional():
    """Declared so they can be saved, hidden so nobody edits them, optional so
    an empty one cannot stop the provider loading and take every Echo player
    off the list."""
    from ma_provider import settings

    generated = {key for key, _ in settings.GENERATED}
    for entry in settings._settings_entries():
        if entry.key in generated:
            assert entry.hidden is True, entry.key
            assert entry.required is False, entry.key


def test_every_visible_setting_lands_in_a_named_section():
    """The whole of Part A: no entry renders in an unlabelled column.

    A visible entry with no category falls into Music Assistant's default
    "generic" bucket, which is exactly the flat undifferentiated list the
    grouping was meant to remove. The generated entries are the only exception,
    and they are hidden rather than uncategorised, so they never render at all.
    """
    from ma_provider import settings

    generated = {key for key, _ in settings.GENERATED}
    for entry in settings._settings_entries():
        if entry.key in generated:
            continue
        assert entry.category, entry.key


def test_advanced_headings_avoid_the_frontends_discarded_categories():
    """The frontend drops a fixed set of category names from the sections it
    draws, the literal "advanced" among them. An entry filed under one of those
    never renders however correct its advanced flag is, so the headings Music Assistant
    uses to hold advanced entries must stay clear of that list."""
    from ma_provider import settings

    discarded = {"preferences", "display_settings", "generic", "advanced",
                 "protocol_general"}
    assert settings.ADVANCED_CATEGORY not in discarded
    assert wizard.ADVANCED_CATEGORY not in discarded


def test_the_advanced_settings_are_behind_the_toggle():
    """The rarely-touched ports and tokens carry advanced=True, not just an
    "advanced" category. The flag is what the frontend's Show advanced settings
    toggle reads; the category name is only their heading."""
    from ma_provider import settings

    advanced = [e for e in settings._settings_entries()
                if e.category == settings.ADVANCED_CATEGORY]
    assert advanced, "expected some advanced-category settings"
    for entry in advanced:
        assert entry.advanced is True, entry.key


def test_voice_and_playback_heads_the_page():
    """Section order follows first appearance, so the everyday voice settings
    have to come before the set-once connection fields for the page to open on
    what a person actually tunes."""
    from ma_provider import settings

    order = [
        entry.category
        for entry in settings._settings_entries()
        if entry.category
    ]
    first = {}
    for index, category in enumerate(order):
        first.setdefault(category, index)

    assert first[settings.VOICE_CATEGORY] < first[settings.CONNECTIONS_CATEGORY]
    assert first[settings.CONNECTIONS_CATEGORY] < first[settings.ADVANCED_CATEGORY]


def test_the_special_setting_names_still_match_music_assistants_own():
    """Two keys are spelled out rather than imported, so that the settings can
    be read without the whole server installed. Spelling them out is only safe
    while they still agree, and MA renaming one would otherwise show up as an
    Amazon login that silently stopped being read."""
    pytest.importorskip("music_assistant")
    from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

    from ma_provider import settings

    assert settings.CONF_USERNAME == CONF_USERNAME
    assert settings.CONF_PASSWORD == CONF_PASSWORD
