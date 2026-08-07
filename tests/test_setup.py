"""The checks and classifications setup is built out of.

Nothing here may touch the network or run the ASK CLI, so the four functions
that would (validate.resolve, validate.peer_cert, validate.http_get,
validate.http_post_json) and the single seam every ask invocation goes through
(smapi.run) are replaced for every test in this module.

These used to be interleaved with tests that posted forms at a Flask wizard.
That wizard is gone: the operations it wrapped are covered by
tests/test_setup_ops.py and the form it rendered by tests/test_wizard.py. What
is left here is the layer underneath both, which never had a framework in it.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone

import pytest

from ma_provider import core as app_module
from ma_provider import core as _core
from ma_provider import setup_state as _setup_state
from ma_provider import smapi_rest
from ma_provider import setup_smapi as smapi
from ma_provider import setup_ops
from ma_provider import setup_state as store
from ma_provider import setup_steps
from ma_provider import setup_validate as validate


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No sockets, no subprocesses, no shared state between tests."""
    monkeypatch.setattr(_setup_state, "STATE_DIR", pathlib.Path(tmp_path / "state"))
    monkeypatch.setattr(_core, "PUBLIC_BASE", "https://ma_alexa.example.com")

    monkeypatch.setattr(validate, "resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        validate, "peer_cert",
        lambda host, port, timeout=8.0: {"subjectAltName": (("DNS", host),)},
    )
    monkeypatch.setattr(validate, "http_get",
                        lambda url, timeout=8.0: (200, '{"ok":true}', {"Server": "test"}))
    monkeypatch.setattr(
        validate, "http_post_json",
        lambda url, body, timeout=12.0: (
            200,
            json.dumps({"header": {"namespace": "Alexa.Media.Search",
                                   "name": "GetPlayableContent.Response"},
                        "payload": {}}),
            {"Server": "test"},
        ),
    )
    monkeypatch.setattr(
        validate, "subsonic_ping",
        lambda url, user, password, timeout=8.0: {"ok": True, "detail": "connected"},
    )

    def refuse(argv, timeout=120):
        raise AssertionError(f"a test tried to run the ASK CLI: {argv}")

    monkeypatch.setattr(smapi, "run", refuse)
    monkeypatch.setattr(smapi, "ask_on_path", lambda: False)
    monkeypatch.setattr(smapi, "ask_configured", lambda: False)

    setup_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    setup_steps._INGESTION.update(at=0.0, sig="", ok=False)
    setup_steps._TRAFFIC.update(at=0.0, ok=False)
    yield


def capture_file(name: str, body: dict) -> pathlib.Path:
    # The directory is made by `capture()` on first write rather than at
    # import, so a test that plants a file has to make it the same way.
    app_module.LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = app_module.LOG_DIR / name
    path.write_text(json.dumps(body))
    return path


# --- ingestion classification ----------------------------------------------


def upload(er: str, slu: str = "PENDING", top: str = "IN_PROGRESS") -> dict:
    return {"status": top, "ingestionSteps": [
        {"name": "ER_INGESTION", "status": er},
        {"name": "SLU_MODELING", "status": slu},
    ]}


def test_er_succeeded_means_voice_works():
    verdict = smapi.classify_ingestion(upload("SUCCEEDED"))
    assert verdict["state"] == "ok"
    assert verdict["voice_ready"] is True


def test_pending_slu_modeling_is_reported_as_fine():
    """The state that looks broken and is not, for weeks at a time."""
    verdict = smapi.classify_ingestion(upload("SUCCEEDED", slu="PENDING"))
    assert verdict["voice_ready"] is True
    assert "normal" in verdict["detail"]
    assert "never blocks" in verdict["detail"]


def test_top_level_in_progress_is_explained_away():
    verdict = smapi.classify_ingestion(upload("SUCCEEDED", top="IN_PROGRESS"))
    assert verdict["voice_ready"] is True
    assert "pinned" in verdict["detail"]


def test_er_in_progress_is_not_ready():
    verdict = smapi.classify_ingestion(upload("IN_PROGRESS"))
    assert verdict["state"] == "waiting"
    assert verdict["voice_ready"] is False


def test_er_failed_is_failed():
    verdict = smapi.classify_ingestion(upload("FAILED"))
    assert verdict["state"] == "failed"
    assert verdict["voice_ready"] is False


def test_no_upload_at_all():
    verdict = smapi.classify_ingestion(None)
    assert verdict["state"] == "none"
    assert verdict["voice_ready"] is False


def test_missing_enablement_names_the_silent_fallback():
    verdict = smapi.classify_enablement(
        smapi.Result(["ask"], 1, "", "skill is not enabled")
    )
    assert verdict["enabled"] is False
    assert "default provider" in verdict["detail"]


# --- status screen ----------------------------------------------------------


# --- endpoint validation ----------------------------------------------------


@pytest.mark.parametrize("address,reason", [
    ("192.168.1.10", "private"),
    ("10.0.0.5", "private"),
    ("172.16.4.4", "private"),
    ("127.0.0.1", "loopback"),
    ("169.254.10.1", "link-local"),
    ("100.64.0.1", "cgnat"),
])
def test_unroutable_addresses_are_rejected(monkeypatch, address, reason):
    monkeypatch.setattr(validate, "resolve", lambda host: [address])
    assert validate.classify_address(address) == reason
    row = validate.check_address("https://ma_alexa.example.com")
    assert row["ok"] is False
    assert address in row["detail"]


def test_tailnet_address_is_called_out_by_name(monkeypatch):
    monkeypatch.setattr(validate, "resolve", lambda host: ["100.64.0.1"])
    row = validate.check_address("https://ma_alexa.example.com")
    assert "Tailscale" in row["detail"]


def test_public_address_passes():
    assert validate.check_address("https://ma_alexa.example.com")["ok"] is True


def test_http_scheme_is_rejected():
    assert validate.check_scheme("http://ma_alexa.example.com")["ok"] is False
    assert validate.check_scheme("https://ma_alexa.example.com")["ok"] is True


def test_wildcard_san_derives_wildcard():
    assert validate.derive_cert_type(
        "ma_alexa.example.com", ["*.example.com"]) == "Wildcard"


def test_exact_san_derives_trusted():
    assert validate.derive_cert_type(
        "ma_alexa.example.com", ["ma_alexa.example.com"]) == "Trusted"


def test_unrelated_wildcard_does_not_derive_wildcard():
    assert validate.derive_cert_type(
        "ma_alexa.example.com", ["*.other.net", "ma_alexa.example.com"]) == "Trusted"


def test_post_probe_failure_blames_the_proxy(monkeypatch):
    monkeypatch.setattr(validate, "http_post_json",
                        lambda url, body, timeout=12.0: (400, "no json",
                                                         {"Server": "cloudflare"}))
    row = validate.check_music_post("https://ma_alexa.example.com")
    assert row["ok"] is False
    assert "dropped the request body" in row["detail"]
    # The layer that answered is visible, not guessed at.
    assert ("Server", "cloudflare") in row["diag"]["headers"]
    assert row["diag"]["status"] == 400


def test_the_checker_does_not_use_the_blocklisted_default_agent():
    """Cloudflare's free tier answers Python-urllib with 403; Amazon and
    browsers pass. The checks must not report an outage that is not there."""
    assert "Music Assistant" in validate._UA


# --- external proof ---------------------------------------------------------


# --- alias ------------------------------------------------------------------


@pytest.fixture
def cfg(ui, monkeypatch):
    """An authed client whose wizard is complete: configuration pages open."""
    _all_steps_done(monkeypatch)
    return ui


def library(monkeypatch, artists=(), albums=(), songs=()):
    monkeypatch.setattr(app_module.subsonic, "search", lambda q, songs_=None, **kw: {
        "artist": [{"id": f"a{i}", "name": n} for i, n in enumerate(artists)],
        "album": [{"id": f"al{i}", "name": n} for i, n in enumerate(albums)],
        "song": [{"id": f"t{i}", "title": n} for i, n in enumerate(songs)],
    })


def test_alias_flags_an_artist_collision(monkeypatch):
    library(monkeypatch, artists=["Jukebox The Ghost"])
    result = validate.assess_alias("jukebox", app_module.subsonic)
    assert result["verdict"] == "bad"
    assert any(row["name"] == "Jukebox The Ghost" and row["risk"] == "high"
               for row in result["rows"])


def test_alias_flags_a_track_collision_across_spacing(monkeypatch):
    """The exact shape that took "jukebox": no shared whole word."""
    library(monkeypatch, songs=["Juke Box Hero"])
    result = validate.assess_alias("jukebox", app_module.subsonic)
    assert any(row["name"] == "Juke Box Hero" for row in result["rows"])


def test_alias_flags_a_word_inside_an_artist_name(monkeypatch):
    library(monkeypatch, artists=["Conan Gray"])
    result = validate.assess_alias("gray tunes", app_module.subsonic)
    assert any(row["name"] == "Conan Gray" for row in result["rows"])


def test_alias_passes_a_clean_candidate(monkeypatch):
    library(monkeypatch, artists=["Gregory Alan Isakov"], songs=["Big Black Car"])
    monkeypatch.setattr(app_module.subsonic, "playlists", lambda: [])
    monkeypatch.setattr(app_module.subsonic, "genres", lambda: [])
    result = validate.assess_alias("nimbus", app_module.subsonic)
    assert result["verdict"] == "clear"
    assert result["rows"] == []


def test_alias_flags_a_brand(monkeypatch):
    library(monkeypatch)
    monkeypatch.setattr(app_module.subsonic, "playlists", lambda: [])
    monkeypatch.setattr(app_module.subsonic, "genres", lambda: [])
    result = validate.assess_alias("sonos", app_module.subsonic)
    assert result["verdict"] == "bad"
    assert result["rows"][0]["kind"] == "brand"


# --- stations ---------------------------------------------------------------


def test_environment_remains_the_default_when_nothing_is_saved(app, monkeypatch):
    from ma_provider import core as bridge
    monkeypatch.setattr(bridge, "AFTER_CONTENT", "genre")
    # No saved value in this test's store, so the module constant wins.
    assert bridge.effective_after_content() == "genre"


def test_after_content_default_stays_stop(app):
    """The shipped default is still stop, both as the entry and as behaviour."""
    from ma_provider import settings

    entry = next(e for e in settings._settings_entries()
                 if e.key == settings.CONF_AFTER_CONTENT)
    assert entry.default_value == "stop"
    assert app.AFTER_CONTENT == "stop"


def test_after_content_entry_offers_every_mode(app):
    """The dropdown must cover exactly the modes the reader will accept."""
    from ma_provider import settings

    entry = next(e for e in settings._settings_entries()
                 if e.key == settings.CONF_AFTER_CONTENT)
    offered = {o.value for o in entry.options}
    assert offered == set(app.AFTER_CONTENT_MODES)


def test_after_content_setting_round_trips_through_configure(app, monkeypatch, tmp_path):
    """The config value the provider passes reaches the file the reader opens."""
    monkeypatch.setattr(app.setup_state, "STATE_DIR", tmp_path)
    # configure copies the mode into setup-state; effective_after_content reads
    # it straight back, so a chosen mode survives the trip the provider makes.
    app.configure(after_content="radio")
    assert app.effective_after_content() == "radio"


def test_configure_ignores_an_empty_after_content(app, monkeypatch, tmp_path):
    """An unset value must not overwrite a mode already on disk."""
    monkeypatch.setattr(app.setup_state, "STATE_DIR", tmp_path)
    app.setup_state.update(after_content="library")
    app.configure(after_content="")
    assert app.effective_after_content() == "library"


def test_enable_stations_round_trips_through_configure(app, monkeypatch, tmp_path):
    """The toggle the provider passes reaches the setup-state file create_catalogs
    and the crawl both read."""
    monkeypatch.setattr(app.setup_state, "STATE_DIR", tmp_path)
    app.configure(enable_stations=True)
    assert app.setup_state.load().get("enable_stations") is True


def test_configure_leaves_enable_stations_alone_when_omitted(app, monkeypatch,
                                                             tmp_path):
    """A partial reconfigure (the omitted default) must not clear the choice."""
    monkeypatch.setattr(app.setup_state, "STATE_DIR", tmp_path)
    app.setup_state.update(enable_stations=True)
    app.configure()  # enable_stations omitted -> None -> no write
    assert app.setup_state.load().get("enable_stations") is True


# --- smapi seam -------------------------------------------------------------


def test_run_refuses_a_shell_string(monkeypatch):
    monkeypatch.undo()
    with pytest.raises(TypeError):
        smapi.run("ask smapi get-vendor-list")


def test_manifest_is_a_music_skill_with_an_https_endpoint():
    body = smapi.manifest(name="Music Assistant", public_base="https://ma_alexa.example.com",
                          cert_type="Wildcard")["manifest"]
    assert "music" in body["apis"]
    endpoint = body["apis"]["music"]["endpoint"]
    assert endpoint["uri"] == "https://ma_alexa.example.com/music"
    assert endpoint["sslCertificateType"] == "Wildcard"


def test_enablement_cycle_deletes_before_setting(monkeypatch):
    calls = []

    def record(argv, timeout=120):
        calls.append(argv)
        return smapi.Result(argv, 0, "{}", "")

    monkeypatch.setattr(smapi, "run", record)
    smapi.cycle_enablement("amzn1.ask.skill.x")
    assert calls[0][2] == "delete-skill-enablement"
    assert calls[1][2] == "set-skill-enablement"


# --- auth -------------------------------------------------------------------


# --- state ------------------------------------------------------------------


def test_state_survives_a_round_trip():
    store.update(alias="ma_alexa", skill_id="amzn1.ask.skill.x")
    assert store.load()["alias"] == "ma_alexa"


def test_state_tolerates_an_unwritable_directory(monkeypatch):
    monkeypatch.setattr(_setup_state, "STATE_DIR", pathlib.Path("/proc/nowhere"))
    assert store.load()["alias"] == ""
    assert store.save({"alias": "x"}) is False


# --- wizard, with the CLI faked out ------------------------------------------


def fake_cli(monkeypatch, stdout: str = "{}", code: int = 0):
    calls = []

    def record(argv, timeout=120):
        calls.append(argv)
        return smapi.Result(argv, code, stdout, "" if code == 0 else "boom")

    monkeypatch.setattr(smapi, "run", record)
    monkeypatch.setattr(smapi, "ask_on_path", lambda: True)
    monkeypatch.setattr(smapi, "ask_configured", lambda: True)
    return calls


def fake_rest(monkeypatch, **overrides):
    """Replace the REST layer. Nothing here may reach Amazon."""
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    created = {"skills": [], "catalogs": [], "associations": []}

    def create_skill(manifest, vendor=""):
        created["skills"].append(manifest)
        created["vendor"] = vendor
        return "amzn1.ask.skill.abc"

    def create_catalog(title, catalog_type):
        created["catalogs"].append((title, catalog_type))
        return f"amzn1.ask.catalog.{len(created['catalogs'])}"

    monkeypatch.setattr(smapi_rest, "create_skill",
                        overrides.get("create_skill", create_skill))
    monkeypatch.setattr(smapi_rest, "create_catalog",
                        overrides.get("create_catalog", create_catalog))
    monkeypatch.setattr(smapi_rest, "associate_catalog",
                        overrides.get("associate_catalog",
                                      lambda s, c: created["associations"].append((s, c))))
    created["deleted"] = []
    monkeypatch.setattr(smapi_rest, "skill_status", overrides.get(
        "skill_status",
        lambda sid: {"manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}}}))
    monkeypatch.setattr(smapi_rest, "delete_skill", overrides.get(
        "delete_skill", created["deleted"].append))
    monkeypatch.setattr(smapi_rest, "list_catalogs",
                        overrides.get("list_catalogs", lambda: []))
    return created


def test_the_manifest_category_is_a_valid_enum():
    """Amazon rejects MUSIC_AND_AUDIO (the display category) with
    INVALID_ENUM_VALUE; STREAMING_SERVICE is the manifest value."""
    manifest = smapi.manifest(name="Music Assistant",
                              public_base="https://ma_alexa.example.com",
                              cert_type="Trusted")
    category = manifest["manifest"]["publishingInformation"]["category"]
    assert category == "STREAMING_SERVICE"


# --- progressive enhancement -------------------------------------------------


# --- the OAuth callback is open, and has to defend itself -------------------


# --- teardown ---------------------------------------------------------------


# --- the sign-in page has to be usable by someone who did not deploy it -----


# --- the stepper ------------------------------------------------------------


def _all_steps_done(monkeypatch):
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "skill_status", lambda sid: {
        "manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}}})
    monkeypatch.setattr(smapi_rest, "upload_status", lambda c, u: {
        "ingestionSteps": [{"name": "ER_INGESTION", "status": "SUCCEEDED"}]})
    store.update(endpoint_ok=True, alias="ma_alexa", skill_id="amzn1.ask.skill.a",
                 catalogs={"artists": "c1"}, uploads={"artists": "u1"},
                 enabled=True)


def _skill_step_ready(monkeypatch):
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    store.update(endpoint_ok=True, alias="ma_alexa")


def test_the_manifest_declares_every_required_music_request():
    """Bare namespaces fail async validation with "an interface is missing"."""
    body = json.dumps(smapi.manifest(name="Music Assistant",
                                     public_base="https://ma_alexa.example.com",
                                     cert_type="Trusted", aliases=["ma_alexa"]))
    for request_name in ("GetPlayableContent", "Initiate", "GetNextItem",
                         "GetPreviousItem"):
        assert request_name in body, request_name


def test_a_lookup_blip_does_not_undo_the_skill_step(monkeypatch):
    """Only a definitive 404 counts as gone; a 500 or timeout must not."""
    from ma_provider import setup_steps as wizard_steps
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "skill_status", lambda sid: (_ for _ in ()).throw(
        smapi_rest.SmapiError("GET failed: 500", status=500)))
    assert wizard_steps._skill_done({"skill_id": "amzn1.ask.skill.x"}) is True


# --- completing a step reloads the page, failing keeps the error inline -----


# --- the endpoint step accepts live traffic as proof ------------------------


@pytest.fixture
def captures_dir(tmp_path, monkeypatch):
    """A capture directory of this test's own.

    The suite-wide CAPTURE_DIR accumulates files from other tests, some of
    which carry signature headers, so evidence tests must not share it.
    """
    from ma_provider import setup_steps as steps
    monkeypatch.setattr(_core, "LOG_DIR", pathlib.Path(tmp_path / "captures"))
    (tmp_path / "captures").mkdir()
    steps._TRAFFIC.update(at=0.0, ok=False)  # bypass the 60s cache
    yield tmp_path / "captures"
    steps._TRAFFIC.update(at=0.0, ok=False)


def _capture(directory, name: str, headers: dict):
    (directory / name).write_text(json.dumps({"headers": headers, "body": {}}))


def test_signed_amazon_traffic_satisfies_the_endpoint_step(captures_dir):
    from ma_provider import setup_steps as steps
    _capture(captures_dir, "sig.json",
             {"Signature-256": "x", "SignatureCertChainUrl": "y"})
    assert steps._endpoint_done({}) is True


def test_an_unsigned_capture_proves_nothing(captures_dir):
    """Local smoke tests write captures too, and must not unlock the step."""
    from ma_provider import setup_steps as steps
    _capture(captures_dir, "nosig.json", {"User-Agent": "curl"})
    assert steps._endpoint_done({}) is False


def test_no_captures_at_all_is_not_proof(captures_dir):
    from ma_provider import setup_steps as steps
    assert steps._endpoint_done({}) is False


def test_a_scrub_does_not_make_ancient_traffic_look_current(captures_dir):
    """Age comes from the filename, so rewriting a capture cannot refresh it.

    The credential scrub rewrites captures in place, which resets mtime. A
    signed directive from a year ago must not start counting as proof that
    Amazon can reach this deployment today.
    """
    from ma_provider import setup_steps as steps
    _capture(captures_dir, "20250101T120000000000-Alexa.Media.Playback.Initiate.json",
             {"Signature-256": "x", "SignatureCertChainUrl": "y"})
    os.utime(captures_dir / "20250101T120000000000-Alexa.Media.Playback.Initiate.json",
             (time.time(), time.time()))  # the scrub
    assert steps._endpoint_done({}) is False

    # The same capture, arriving today, is proof.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    _capture(captures_dir, f"{stamp}-Alexa.Media.Playback.Initiate.json",
             {"Signature-256": "x", "SignatureCertChainUrl": "y"})
    steps._TRAFFIC.update(at=0.0, ok=False)
    assert steps._endpoint_done({}) is True


def test_the_stored_flag_still_counts(captures_dir):
    from ma_provider import setup_steps as steps
    assert steps._endpoint_done({"endpoint_ok": True}) is True
