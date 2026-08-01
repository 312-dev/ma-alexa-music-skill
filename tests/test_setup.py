"""The /setup wizard.

Nothing here may touch the network or run the ASK CLI, so the four functions
that would (validate.resolve, validate.peer_cert, validate.http_get,
validate.http_post_json) and the single seam every ask invocation goes through
(smapi.run) are replaced for every test in this module.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

import app as app_module
from setup_ui import bp as setup_bp
import smapi_rest
from setup_ui import qr, smapi, state as store, validate, views

# The parent wires this up in app.py. Registering once here keeps the suite
# independent of whether that has happened yet.
if "setup" not in app_module.app.blueprints:
    app_module.app.register_blueprint(setup_bp)


ADMIN = "test-admin-token"
HEADERS = {"X-Admin-Token": ADMIN}


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No sockets, no subprocesses, no shared state between tests."""
    monkeypatch.setenv("SETUP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("PUBLIC_BASE", "https://ampere.example.com")

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

    views._SMAPI_CACHE.update(at=0.0, value=None)
    views._VENDORS.update(at=0.0, value=None)
    views.wizard_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    views.wizard_steps._INGESTION.update(at=0.0, sig="", ok=False)
    views._UPLOAD.update(running=False, phase="", percent=None, results=None,
                         message="", done_at=0.0, cancel=False)
    views.TOKENS = validate.Tokens()
    yield


@pytest.fixture
def anon(client):
    return client


@pytest.fixture
def ui(client):
    """A client that carries the admin header, which authed() accepts."""
    class Authed:
        def get(self, path, **kw):
            kw.setdefault("headers", {}).update(HEADERS)
            return client.get(path, **kw)

        def post(self, path, **kw):
            kw.setdefault("headers", {}).update(HEADERS)
            return client.post(path, **kw)

    return Authed()


def capture_file(name: str, body: dict) -> pathlib.Path:
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


def test_status_page_renders(ui):
    resp = ui.get("/setup/status")
    assert resp.status_code == 200
    assert b"ER_INGESTION" in resp.data


def test_setup_root_leads_to_the_wizard_while_unfinished(ui):
    """The wizard is the job in front of you until there is a skill."""
    resp = ui.get("/setup")
    assert resp.status_code == 302
    assert "/setup/wizard" in resp.headers["Location"]


def test_status_shows_the_last_request_from_amazon(ui):
    capture_file(
        "20260731T101500000000-Alexa.Media.Search.GetPlayableContent.json",
        {"headers": {"SignatureCertChainUrl": "https://s3.amazonaws.com/echo.api/x"},
         "body": {"header": {"namespace": "Alexa.Media.Search",
                             "name": "GetPlayableContent"}}},
    )
    body = ui.get("/setup/status").data.decode()
    assert "Alexa.Media.Search.GetPlayableContent" in body
    assert "seconds ago" in body


def test_status_says_so_when_amazon_has_never_called(ui, monkeypatch, tmp_path):
    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path / "empty"))
    body = ui.get("/setup/status").data.decode()
    assert "nothing yet" in body
    assert "not calling this bridge" in body


def test_status_never_prints_a_secret(ui):
    body = ui.get("/setup/status").data.decode()
    assert ADMIN not in body
    assert os.environ["SIGNING_KEY"] not in body
    assert "set</span>" in body


def test_status_poll_does_not_shell_out(ui):
    """smapi.run raises in this module, so a poll that ran ask would fail here."""
    assert ui.get("/setup/status").status_code == 200
    assert ui.get("/setup/status").status_code == 200


# --- endpoint validation ----------------------------------------------------


@pytest.mark.parametrize("address,reason", [
    ("192.168.1.10", "private"),
    ("10.0.0.5", "private"),
    ("172.16.4.4", "private"),
    ("127.0.0.1", "loopback"),
    ("169.254.10.1", "link-local"),
    ("100.85.183.28", "cgnat"),
])
def test_unroutable_addresses_are_rejected(monkeypatch, address, reason):
    monkeypatch.setattr(validate, "resolve", lambda host: [address])
    assert validate.classify_address(address) == reason
    row = validate.check_address("https://ampere.example.com")
    assert row["ok"] is False
    assert address in row["detail"]


def test_tailnet_address_is_called_out_by_name(monkeypatch):
    monkeypatch.setattr(validate, "resolve", lambda host: ["100.85.183.28"])
    row = validate.check_address("https://ampere.example.com")
    assert "Tailscale" in row["detail"]


def test_public_address_passes():
    assert validate.check_address("https://ampere.example.com")["ok"] is True


def test_http_scheme_is_rejected():
    assert validate.check_scheme("http://ampere.example.com")["ok"] is False
    assert validate.check_scheme("https://ampere.example.com")["ok"] is True


def test_wildcard_san_derives_wildcard():
    assert validate.derive_cert_type(
        "ampere.example.com", ["*.example.com"]) == "Wildcard"


def test_exact_san_derives_trusted():
    assert validate.derive_cert_type(
        "ampere.example.com", ["ampere.example.com"]) == "Trusted"


def test_unrelated_wildcard_does_not_derive_wildcard():
    assert validate.derive_cert_type(
        "ampere.example.com", ["*.other.net", "ampere.example.com"]) == "Trusted"


def test_cert_type_is_surfaced_on_the_page(ui, monkeypatch):
    monkeypatch.setattr(
        validate, "peer_cert",
        lambda host, port, timeout=8.0: {"subjectAltName": (("DNS", "*.example.com"),)},
    )
    body = ui.get("/setup/endpoint").data.decode()
    assert "sslCertificateType" in body
    assert "Wildcard" in body


def test_post_probe_failure_blames_the_proxy(monkeypatch):
    monkeypatch.setattr(validate, "http_post_json",
                        lambda url, body, timeout=12.0: (400, "no json",
                                                         {"Server": "cloudflare"}))
    row = validate.check_music_post("https://ampere.example.com")
    assert row["ok"] is False
    assert "dropped the request body" in row["detail"]
    # The layer that answered is visible, not guessed at.
    assert ("Server", "cloudflare") in row["diag"]["headers"]
    assert row["diag"]["status"] == 400


def test_check_rows_render_expandable_response_details(ui):
    body = ui.get("/setup/endpoint").data.decode()
    assert "Response details" in body


def test_the_checker_does_not_use_the_blocklisted_default_agent():
    """Cloudflare's free tier answers Python-urllib with 403; Amazon and
    browsers pass. The checks must not report an outage that is not there."""
    assert "Ampere" in validate._UA


def test_endpoint_page_gates_the_wizard(ui, monkeypatch):
    monkeypatch.setattr(validate, "resolve", lambda host: ["10.0.0.1"])
    body = ui.get("/setup/endpoint").data.decode()
    assert "stays locked" in body
    assert store.load()["endpoint_ok"] is False


def test_skill_creation_refuses_without_a_passing_endpoint(ui):
    store.update(endpoint_ok=False)
    body = ui.post("/setup/wizard/skill", data={"vendor_id": "M1", "alias": "ampere"}).data.decode()
    assert "Blocked" in body
    assert "/setup/endpoint" in body


# --- external proof ---------------------------------------------------------


def test_unseen_token_is_pending(ui):
    token = views.TOKENS.mint()
    assert views.TOKENS.status(token) == "pending"
    assert "waiting for the scan" in ui.get(f"/setup/endpoint/proof?token={token}").data.decode()


def test_hitting_the_link_marks_it_seen(anon):
    token = views.TOKENS.mint()
    resp = anon.get(f"/setup/verify/{token}")
    assert resp.status_code == 200
    assert b"Got it" in resp.data
    assert views.TOKENS.status(token) == "seen"


def test_verify_needs_no_cookie(anon):
    """The phone is on cellular and has never seen this site before."""
    token = views.TOKENS.mint()
    assert anon.get(f"/setup/verify/{token}").status_code == 200


def test_expired_token_is_rejected(anon, monkeypatch):
    views.TOKENS = validate.Tokens(ttl=1)
    token = views.TOKENS.mint()
    views.TOKENS._tokens[token]["born"] = time.time() - 60
    assert anon.get(f"/setup/verify/{token}").status_code == 410
    assert views.TOKENS.status(token) in ("expired", "unknown")


def test_unknown_token_is_404(anon):
    assert anon.get("/setup/verify/never-minted").status_code == 404


def test_proof_row_flips_to_green_after_the_hit(ui, anon):
    token = views.TOKENS.mint()
    anon.get(f"/setup/verify/{token}")
    body = ui.get(f"/setup/endpoint/proof?token={token}").data.decode()
    assert "confirmed" in body


# --- QR ---------------------------------------------------------------------


def test_qr_encodes_a_verify_url():
    matrix = qr.encode("https://ampere.example.com/setup/verify/abcdefghijkl")
    assert matrix is not None
    size = len(matrix)
    assert size == len(matrix[0])
    # Three finder patterns, so three dark corners with a light ring inside.
    for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert matrix[row][col] == 1
        assert matrix[row + 1][col + 1] == 0


def test_qr_refuses_rather_than_shipping_something_unscannable():
    assert qr.encode("x" * 5000) is None


def test_qr_svg_is_self_contained():
    svg = qr.svg(qr.encode("https://ampere.example.com/setup/verify/abc"))
    assert svg.startswith("<svg")
    assert "http://www.w3.org/2000/svg" in svg
    assert "src=" not in svg


def test_page_shows_the_url_as_text_even_with_a_qr(ui):
    token = views.TOKENS.mint()
    body = ui.get(f"/setup/endpoint/proof?token={token}").data.decode()
    assert f"/setup/verify/{token}" in body
    assert "WiFi" in body


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
    result = validate.assess_alias("ampere", app_module.subsonic)
    assert result["verdict"] == "clear"
    assert result["rows"] == []


def test_alias_flags_a_brand(monkeypatch):
    library(monkeypatch)
    monkeypatch.setattr(app_module.subsonic, "playlists", lambda: [])
    monkeypatch.setattr(app_module.subsonic, "genres", lambda: [])
    result = validate.assess_alias("sonos", app_module.subsonic)
    assert result["verdict"] == "bad"
    assert result["rows"][0]["kind"] == "brand"


def test_alias_page_explains_why_it_matters(cfg):
    body = cfg.get("/setup/alias").data.decode()
    assert "before" in body
    assert "catalog" in body


def test_alias_page_reports_a_collision(cfg, monkeypatch):
    library(monkeypatch, artists=["Jukebox The Ghost"])
    body = cfg.post("/setup/alias", data={"candidate": "jukebox"}).data.decode()
    assert "Jukebox The Ghost" in body


# --- stations ---------------------------------------------------------------


def test_stations_page_lists_every_after_content_mode(cfg):
    body = cfg.get("/setup/stations").data.decode()
    for mode in app_module.AFTER_CONTENT_MODES:
        assert f'value="{mode}"' in body


def test_stations_page_no_longer_demands_a_restart(cfg):
    """Saved values take effect live now; the old caveat must not resurface."""
    body = cfg.get("/setup/stations").data.decode()
    assert "restart" not in body
    assert "take effect immediately" in body


def test_saved_settings_take_effect_without_a_restart(cfg, app):
    """The point of the change: the form is a real settings page."""
    import app as bridge
    cfg.post("/setup/stations", data={"after_content": "stop", "radio_artists": "5",
                                     "radio_tracks_per_artist": "4"})
    assert bridge.effective_after_content() == "stop"
    assert bridge.effective_radio_artists() == 5
    assert bridge.effective_radio_tracks_per_artist() == 4
    assert bridge.continuation_mode("ar:a1") == "stop"

    cfg.post("/setup/stations", data={"after_content": "radio", "radio_artists": "9",
                                     "radio_tracks_per_artist": "4"})
    assert bridge.continuation_mode("ar:a1") == "radio"


def test_saving_settings_drops_the_stale_pools(cfg):
    """Cached pools were built under the old numbers."""
    import app as bridge
    bridge._RADIO_CACHE["a1"] = ["a1", "a2"]
    bridge._QUEUE_CACHE["rad:a1"] = [{"id": "t1"}]
    cfg.post("/setup/stations", data={"after_content": "radio", "radio_artists": "6",
                                     "radio_tracks_per_artist": "6"})
    assert not bridge._RADIO_CACHE
    assert not bridge._QUEUE_CACHE


def test_environment_remains_the_default_when_nothing_is_saved(app, monkeypatch):
    import app as bridge
    monkeypatch.setattr(bridge, "AFTER_CONTENT", "genre")
    # No saved value in this test's store, so the module constant wins.
    assert bridge.effective_after_content() == "genre"


def test_saving_stations_persists(cfg):
    cfg.post("/setup/stations", data={"after_content": "radio", "radio_artists": "7",
                                     "radio_tracks_per_artist": "3"})
    saved = store.load()
    assert saved["after_content"] == "radio"
    assert saved["radio_artists"] == 7


def test_saving_stations_refuses_an_unknown_mode(cfg):
    cfg.post("/setup/stations", data={"after_content": "teleport",
                                     "radio_artists": "7",
                                     "radio_tracks_per_artist": "3"})
    assert store.load()["after_content"] in app_module.AFTER_CONTENT_MODES


def test_station_preview_shows_the_artist_pool(cfg):
    body = cfg.get("/setup/stations/preview?seed=Gregory").data.decode()
    assert "Blind Pilot" in body
    assert "Iron and Wine" in body


def test_station_preview_calls_out_a_degraded_pool(cfg, monkeypatch):
    monkeypatch.setattr(app_module.subsonic, "similar_artists",
                        lambda artist_id, count=20: [])
    app_module._RADIO_CACHE.clear()
    body = cfg.get("/setup/stations/preview?seed=Gregory").data.decode()
    assert "seed artist alone" in body




def test_configuration_pages_wait_for_the_wizard(ui):
    for path in ("/setup/alias", "/setup/stations",
                 "/setup/stations/preview?seed=x"):
        resp = ui.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["Location"].endswith("/setup/wizard"), path


def test_saving_stations_waits_for_the_wizard_too(ui):
    """The gate covers writes, not just page views."""
    resp = ui.post("/setup/stations", data={"after_content": "stop",
                                            "radio_artists": "5",
                                            "radio_tracks_per_artist": "4"})
    assert resp.status_code == 302
    assert not store.load().get("after_content")


def test_sidebar_locks_configuration_until_setup_is_done(ui, monkeypatch):
    # The body of the status page may link to configuration wherever it likes;
    # the gate bounces those clicks. The sidebar is what must read as locked.
    body = ui.get("/setup/status").data.decode()
    assert "navlocked" in body

    _all_steps_done(monkeypatch)
    body = ui.get("/setup/status").data.decode()
    assert "navlocked" not in body


# --- smapi seam -------------------------------------------------------------


def test_run_refuses_a_shell_string(monkeypatch):
    monkeypatch.undo()
    with pytest.raises(TypeError):
        smapi.run("ask smapi get-vendor-list")


def test_manifest_is_a_music_skill_with_an_https_endpoint():
    body = smapi.manifest(name="Ampere", public_base="https://ampere.example.com",
                          cert_type="Wildcard")["manifest"]
    assert "music" in body["apis"]
    endpoint = body["apis"]["music"]["endpoint"]
    assert endpoint["uri"] == "https://ampere.example.com/music"
    assert endpoint["sslCertificateType"] == "Wildcard"


def test_wizard_catalogs_cover_every_kind():
    import catalog_sync

    assert set(views.CATALOG_KINDS) == set(catalog_sync.CATALOGS)
    assert views.CATALOG_KINDS == catalog_sync.TYPES


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


def test_setup_refuses_to_serve_without_an_admin_token(monkeypatch, client):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    resp = client.get("/setup", headers=HEADERS)
    assert resp.status_code == 503
    assert b"ADMIN_TOKEN is not set" in resp.data


def test_refusal_covers_login_too(monkeypatch, client):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get("/setup/login").status_code == 503


def setup_rules():
    for rule in app_module.app.url_map.iter_rules():
        if not rule.endpoint.startswith("setup."):
            continue
        if rule.endpoint in ("setup.static", "setup.login", "setup.verify",
                             "setup.oauth_callback"):
            continue
        yield rule


def test_every_route_requires_auth(anon):
    checked = 0
    for rule in setup_rules():
        method = "POST" if "POST" in rule.methods else "GET"
        resp = anon.open(rule.rule, method=method)
        assert resp.status_code == 401, f"{method} {rule.rule} was not gated"
        checked += 1
    assert checked >= 12


def test_the_route_list_is_not_empty():
    assert len(list(setup_rules())) >= 12


def test_wrong_token_is_rejected(client):
    resp = client.post("/setup/login", data={"token": "nope", "target": "/setup"})
    assert resp.status_code == 401
    assert client.get("/setup/status").status_code == 401


def test_login_sets_a_working_cookie(client):
    resp = client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    assert resp.status_code == 302
    assert client.get("/setup/status").status_code == 200


def test_login_will_not_redirect_off_setup(client):
    resp = client.post("/setup/login",
                       data={"token": ADMIN, "target": "https://evil.example/"})
    assert resp.headers["Location"] == "/setup"


def test_cookie_dies_when_the_admin_token_rotates(client, monkeypatch):
    client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    assert client.get("/setup/status").status_code == 200
    monkeypatch.setenv("ADMIN_TOKEN", "a-different-token")
    assert client.get("/setup/status").status_code == 401


def test_logout_clears_the_cookie(client):
    client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    client.get("/setup/logout")
    assert client.get("/setup/status").status_code == 401


# --- state ------------------------------------------------------------------


def test_state_survives_a_round_trip():
    store.update(alias="ampere", skill_id="amzn1.ask.skill.x")
    assert store.load()["alias"] == "ampere"


def test_state_tolerates_an_unwritable_directory(monkeypatch):
    monkeypatch.setenv("SETUP_STATE_DIR", "/proc/nowhere")
    assert store.load()["alias"] == ""
    assert store.save({"alias": "x"}) is False


def test_no_subsonic_password_is_ever_written(ui):
    ui.post("/setup/wizard/subsonic", data={"url": "http://nav.test",
                                            "user": "tester",
                                            "password": "hunter2"})
    assert "hunter2" not in json.dumps(store.load())


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
    manifest = smapi.manifest(name="Ampere",
                              public_base="https://ampere.example.com",
                              cert_type="Trusted")
    category = manifest["manifest"]["publishingInformation"]["category"]
    assert category == "STREAMING_SERVICE"


def test_wizard_creates_a_skill_once_the_endpoint_passes(ui, monkeypatch):
    created = fake_rest(monkeypatch)
    store.update(endpoint_ok=True, cert_type="Wildcard")
    body = ui.post("/setup/wizard/skill", data={"alias": "ampere"}).data.decode()
    assert "amzn1.ask.skill.abc" in body
    assert store.load()["skill_id"] == "amzn1.ask.skill.abc"
    assert len(created["skills"]) == 1


def test_skill_creation_requires_being_connected(ui, monkeypatch):
    """No CLI to fall back on now, so this has to say so plainly."""
    monkeypatch.setattr(smapi_rest, "connected", lambda: False)
    store.update(endpoint_ok=True)
    assert "Connect to Amazon first" in ui.post(
        "/setup/wizard/skill", data={"alias": "ampere"}).data.decode()


def test_created_manifest_carries_the_derived_cert_type(ui, monkeypatch):
    created = fake_rest(monkeypatch)
    store.update(endpoint_ok=True, cert_type="Wildcard")
    ui.post("/setup/wizard/skill", data={"alias": "ampere"})
    # _unwrap is what actually reaches Amazon, so assert through it.
    sent = smapi_rest._unwrap(created["skills"][0])
    endpoint = sent["apis"]["music"]["endpoint"]
    assert endpoint["sslCertificateType"] == "Wildcard"


def test_the_manifest_never_double_wraps(ui, monkeypatch):
    """smapi.manifest() is already {"manifest": ...}; wrapping again is rejected."""
    created = fake_rest(monkeypatch)
    store.update(endpoint_ok=True)
    ui.post("/setup/wizard/skill", data={"alias": "ampere"})
    assert "manifest" not in smapi_rest._unwrap(created["skills"][0])


def test_the_manifest_points_at_icons_that_exist(ui, monkeypatch):
    """A manifest naming a missing icon fails the whole update with
    RESOURCE_NOT_FOUND, which reads as a skill problem rather than a file one."""
    created = fake_rest(monkeypatch)
    store.update(endpoint_ok=True)
    ui.post("/setup/wizard/skill", data={"alias": "ampere"})
    locale = smapi_rest._unwrap(
        created["skills"][0])["publishingInformation"]["locales"]["en-US"]
    shipped = pathlib.Path(__file__).resolve().parent.parent / "icons"
    for key in ("smallIconUri", "largeIconUri"):
        name = locale[key].split("/icons/")[-1]
        assert (shipped / name).is_file(), f"{key} names {name}, which is not shipped"


def test_wizard_creates_every_catalog_kind(ui, monkeypatch):
    created = fake_rest(monkeypatch)
    store.update(skill_id="amzn1.ask.skill.abc")
    body = ui.post("/setup/wizard/catalogs").data.decode()
    for kind in views.CATALOG_KINDS:
        assert kind in body
    assert len(created["catalogs"]) == len(views.CATALOG_KINDS)


def test_every_catalog_is_associated_with_the_skill(ui, monkeypatch):
    """Association has to happen before upload or content cannot resolve."""
    created = fake_rest(monkeypatch)
    store.update(skill_id="amzn1.ask.skill.abc")
    ui.post("/setup/wizard/catalogs")
    assert len(created["associations"]) == len(views.CATALOG_KINDS)
    assert all(skill == "amzn1.ask.skill.abc" for skill, _ in created["associations"])


def test_catalog_creation_needs_a_skill_first(ui, monkeypatch):
    fake_rest(monkeypatch)
    assert "Create the skill first" in ui.post("/setup/wizard/catalogs").data.decode()


def test_status_refresh_shouts_about_missing_enablement(ui, monkeypatch):
    """The nastiest state in the project: healthy everywhere, silent in the room."""
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "enablement_status", lambda skill_id: False)
    store.update(skill_id="amzn1.ask.skill.abc")
    body = ui.post("/setup/status/refresh").data.decode()
    assert "not enabled" in body
    assert "default music provider" in body


def test_status_reports_a_healthy_enablement(ui, monkeypatch):
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "enablement_status", lambda skill_id: True)
    store.update(skill_id="amzn1.ask.skill.abc")
    assert "Enabled for development" in ui.post("/setup/status/refresh").data.decode()


def test_status_says_when_it_is_not_connected(ui, monkeypatch):
    monkeypatch.setattr(smapi_rest, "connected", lambda: False)
    store.update(skill_id="amzn1.ask.skill.abc")
    assert "not connected" in ui.post("/setup/status/refresh").data.decode()


def test_subsonic_step_reports_a_failure(ui, monkeypatch):
    monkeypatch.setattr(validate, "subsonic_ping",
                        lambda url, user, password, timeout=8.0: {
                            "ok": False, "detail": "server said 40: Wrong username"})
    body = ui.post("/setup/wizard/subsonic",
                   data={"url": "http://nav.test", "user": "x",
                         "password": "y"}).data.decode()
    assert "Wrong username" in body
    assert store.load()["subsonic_url"] == ""


# --- progressive enhancement -------------------------------------------------


def test_partials_come_back_wrapped_in_the_layout_without_htmx(ui):
    body = ui.post("/setup/status/refresh").data.decode()
    assert "<html" in body
    assert "vendor/basecoat/basecoat.css" in body


def test_partials_stay_bare_for_htmx(ui):
    body = ui.post("/setup/status/refresh",
                   headers={"HX-Request": "true"}).data.decode()
    assert "<html" not in body


def test_minting_a_proof_without_js_lands_back_on_the_page(ui):
    resp = ui.post("/setup/endpoint/proof")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/setup/endpoint?token=")


def test_endpoint_page_renders_the_qr_inline(ui):
    resp = ui.post("/setup/endpoint/proof")
    token = resp.headers["Location"].split("token=")[1]
    body = ui.get(f"/setup/endpoint?token={token}").data.decode()
    assert "<svg" in body
    assert "vendor/htmx.min.js" in body


def test_vendored_assets_are_served_locally(ui):
    for asset in ("vendor/basecoat/basecoat.css", "vendor/htmx.min.js", "setup.css"):
        assert ui.get(f"/setup/static/{asset}").status_code == 200


def test_no_page_links_a_cdn(ui):
    for path in ("/setup", "/setup/endpoint", "/setup/alias",
                 "/setup/wizard", "/setup/stations", "/setup/login"):
        body = ui.get(path).data.decode()
        assert "unpkg.com" not in body
        assert "cdn.jsdelivr.net" not in body


# --- the OAuth callback is open, and has to defend itself -------------------


def test_callback_rejects_a_response_it_did_not_ask_for(client):
    """It is reachable from anywhere, so state is the only thing guarding it."""
    views._PENDING.clear()
    body = client.get("/setup/oauth/callback?code=x&state=whatever").data.decode()
    assert "did not match" in body


def test_callback_rejects_a_mismatched_state(client, monkeypatch):
    views._PENDING.update({"state": "real", "verifier": "v", "at": time.time(),
                           "client_id": "c", "client_secret": "s"})
    body = client.get("/setup/oauth/callback?code=x&state=forged").data.decode()
    assert "did not match" in body
    views._PENDING.clear()


def test_callback_rejects_an_expired_request(client):
    views._PENDING.update({"state": "s", "verifier": "v", "at": time.time() - 1000,
                           "client_id": "c", "client_secret": "s"})
    body = client.get("/setup/oauth/callback?code=x&state=s").data.decode()
    assert "expired" in body
    views._PENDING.clear()


def test_callback_reports_an_error_from_amazon(client):
    body = client.get(
        "/setup/oauth/callback?error=access_denied&error_description=nope"
    ).data.decode()
    assert "access_denied" in body


def test_a_good_callback_stores_the_token(client, monkeypatch):
    seen = {}

    def complete(code, client_id, client_secret, verifier):
        seen.update(code=code, client_id=client_id, verifier=verifier)
        return {"refresh_token": "r"}

    monkeypatch.setattr(smapi_rest, "complete", complete)
    views._PENDING.update({"state": "s", "verifier": "v", "at": time.time(),
                           "client_id": "cid", "client_secret": "sec"})
    body = client.get("/setup/oauth/callback?code=abc&state=s").data.decode()
    assert "Connected" in body
    assert seen == {"code": "abc", "client_id": "cid", "verifier": "v"}
    # Single use: the pending request is consumed even on success.
    assert not views._PENDING


def test_callback_sends_the_operator_back_to_the_origin_they_came_from(client, monkeypatch):
    """Amazon redirects to the public hostname, where the admin plane does not
    serve. The way back must point at the address the wizard was driven from."""
    monkeypatch.setattr(smapi_rest, "complete",
                        lambda *a, **kw: {"refresh_token": "r"})
    views._PENDING.update({"state": "s", "verifier": "v", "at": time.time(),
                           "client_id": "cid", "client_secret": "sec",
                           "origin": "http://100.85.183.28:5056"})
    body = client.get("/setup/oauth/callback?code=abc&state=s").data.decode()
    assert 'href="http://100.85.183.28:5056/setup/wizard"' in body


def test_callback_without_a_known_origin_does_not_link_into_the_blocked_plane(client):
    views._PENDING.clear()
    body = client.get("/setup/oauth/callback?code=x&state=whatever").data.decode()
    assert 'href="/setup/wizard"' not in body
    assert "address you use for setup" in body


def test_connect_refuses_without_an_https_public_base(ui, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE", "http://192.168.1.50:5056")
    body = ui.post("/setup/wizard/amazon/begin",
                   data={"client_id": "c", "client_secret": "s"}).data.decode()
    assert "https" in body


def test_connect_needs_both_halves(ui):
    body = ui.post("/setup/wizard/amazon/begin", data={"client_id": "c"}).data.decode()
    assert "client secret" in body


# --- teardown ---------------------------------------------------------------


def test_teardown_refuses_without_an_exact_confirmation(ui, monkeypatch):
    store.update(skill_id="amzn1.ask.skill.abc")
    deleted = []
    monkeypatch.setattr(smapi_rest, "delete_skill", lambda s: deleted.append(s))
    body = ui.post("/setup/wizard/teardown", data={"confirm": "wrong"}).data.decode()
    assert "Type the skill id" in body
    assert deleted == []
    assert store.load()["skill_id"] == "amzn1.ask.skill.abc"


def test_teardown_removes_the_skill_and_its_catalogs(ui, monkeypatch):
    store.update(skill_id="amzn1.ask.skill.abc",
                 catalogs={"artists": "cat-1", "albums": "cat-2"})
    removed = {"skills": [], "catalogs": []}
    monkeypatch.setattr(smapi_rest, "delete_skill",
                        lambda s: removed["skills"].append(s))
    monkeypatch.setattr(smapi_rest, "delete_catalog",
                        lambda c: removed["catalogs"].append(c))
    body = ui.post("/setup/wizard/teardown",
                   data={"confirm": "amzn1.ask.skill.abc"}).data.decode()
    assert "deleted" in body
    assert removed["skills"] == ["amzn1.ask.skill.abc"]
    assert sorted(removed["catalogs"]) == ["cat-1", "cat-2"]
    assert store.load()["skill_id"] == ""


# --- the sign-in page has to be usable by someone who did not deploy it -----


def test_login_page_says_where_to_find_the_token(anon):
    body = anon.get("/setup/status").data.decode()
    assert "ADMIN_TOKEN" in body
    # The three places a self-hoster would actually have put it.
    for hint in ("printenv ADMIN_TOKEN", "nomad var get", ".env"):
        assert hint in body, hint


def test_login_page_covers_having_lost_the_token(anon):
    body = anon.get("/setup/status").data.decode()
    assert "openssl rand" in body
    assert "signs out every existing session" in body


def test_login_page_never_contains_the_token(anon, monkeypatch):
    """The page explains where the secret lives. It must not be the secret."""
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret-value")
    assert b"super-secret-value" not in anon.get("/setup/status").data


# --- the stepper ------------------------------------------------------------


def _all_steps_done(monkeypatch):
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "skill_status", lambda sid: {
        "manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}}})
    monkeypatch.setattr(smapi_rest, "upload_status", lambda c, u: {
        "ingestionSteps": [{"name": "ER_INGESTION", "status": "SUCCEEDED"}]})
    store.update(endpoint_ok=True, alias="ampere", skill_id="amzn1.ask.skill.a",
                 catalogs={"artists": "c1"}, uploads={"artists": "u1"},
                 enabled=True)


def test_the_wizard_resumes_at_the_first_unfinished_step(ui):
    resp = ui.get("/setup/wizard")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/wizard/server")


def test_a_later_step_is_locked_until_the_earlier_ones_are_done(ui):
    resp = ui.get("/setup/wizard/enable")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/wizard")


def test_a_finished_step_can_be_revisited(ui, monkeypatch):
    """Going back to review is not the same as redoing."""
    _all_steps_done(monkeypatch)
    assert ui.get("/setup/wizard/server").status_code == 200


def test_each_step_offers_back_and_continue(ui, monkeypatch):
    _all_steps_done(monkeypatch)
    body = ui.get("/setup/wizard/alias").data.decode()
    assert "/setup/wizard/amazon" in body   # back
    assert "/setup/wizard/skill" in body    # continue


def test_continue_is_disabled_on_an_unfinished_step(ui):
    body = ui.get("/setup/wizard/server").data.decode()
    assert '<button class="btn" disabled>Continue</button>' in body


def test_wizard_alias_step_prefills_the_default(ui, monkeypatch):
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    store.update(endpoint_ok=True)
    body = ui.get("/setup/wizard/alias").data.decode()
    assert 'value="ampere"' in body
    assert "placeholder" not in body
    # The old separate checker linked into the gated configuration pages.
    assert "Check it against my library" not in body


def test_wizard_alias_save_checks_and_advances_when_clear(ui, monkeypatch):
    resp = ui.post("/setup/wizard/alias", data={"alias": "ampere"},
                   headers={"HX-Request": "true"})
    assert resp.status_code == 204
    assert resp.headers.get("HX-Refresh") == "true"
    assert store.load()["alias"] == "ampere"


def test_wizard_alias_save_surfaces_a_collision_but_still_saves(ui, monkeypatch):
    library(monkeypatch, artists=["Jukebox The Ghost"])
    body = ui.post("/setup/wizard/alias", data={"alias": "jukebox"},
                   headers={"HX-Request": "true"}).data.decode()
    assert "Jukebox The Ghost" in body
    assert "Continue with" in body
    assert store.load()["alias"] == "jukebox"


def _skill_step_ready(monkeypatch):
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    store.update(endpoint_ok=True, alias="ampere")


def test_skill_step_prefills_the_detected_vendor(ui, monkeypatch):
    """A value the wizard can read is shown filled in, never as a placeholder."""
    _skill_step_ready(monkeypatch)
    monkeypatch.setattr(smapi_rest, "vendors",
                        lambda: [{"id": "M1REAL", "name": "Grayson"}])
    body = ui.get("/setup/wizard/skill").data.decode()
    assert 'value="M1REAL"' in body
    assert "placeholder" not in body
    assert 'data-reset="sk-vendor"' in body
    assert 'value="ampere"' in body          # alias arrives filled too


def test_skill_step_offers_a_dropdown_when_the_account_has_several_vendors(ui, monkeypatch):
    _skill_step_ready(monkeypatch)
    monkeypatch.setattr(smapi_rest, "vendors", lambda: [
        {"id": "M1A", "name": "one"}, {"id": "M1B", "name": "two"}])
    body = ui.get("/setup/wizard/skill").data.decode()
    assert "<select" in body
    assert "M1A" in body and "M1B" in body


def test_creating_the_skill_uses_the_vendor_from_the_form(ui, monkeypatch):
    _skill_step_ready(monkeypatch)
    seen = {}

    def create(manifest, vendor=""):
        seen["vendor"] = vendor
        return "amzn1.ask.skill.new"

    monkeypatch.setattr(smapi_rest, "create_skill", create)
    ui.post("/setup/wizard/skill", data={"vendor_id": "M1CHOSEN",
                                         "alias": "ampere"},
            headers={"HX-Request": "true"})
    assert seen["vendor"] == "M1CHOSEN"
    assert store.load()["skill_id"] == "amzn1.ask.skill.new"


def test_a_created_skill_replaces_the_form_and_refuses_duplicates(ui, monkeypatch):
    """The create button must not be able to mint duplicates."""
    created = fake_rest(monkeypatch)
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    store.update(endpoint_ok=True, alias="ampere",
                 skill_id="amzn1.ask.skill.already")
    body = ui.get("/setup/wizard/skill").data.decode()
    # The step title still says Create the skill; the button must be gone.
    assert '<button type="submit" class="btn">Create the skill</button>' not in body
    assert "amzn1.ask.skill.already" in body

    resp = ui.post("/setup/wizard/skill", data={"alias": "ampere"},
                   headers={"HX-Request": "true"})
    assert resp.status_code == 204          # acknowledged, nothing created
    assert created["skills"] == []
    assert store.load()["skill_id"] == "amzn1.ask.skill.already"


def test_a_preexisting_music_skill_is_surfaced_with_removal_guidance(ui, monkeypatch):
    fake_rest(monkeypatch)
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    store.update(endpoint_ok=True, alias="ampere")
    monkeypatch.setattr(smapi_rest, "list_skills", lambda: [
        {"skillId": "amzn1.ask.skill.old", "apis": ["music"],
         "nameByLocale": {"en-US": "Old Bridge"}, "stage": "development"},
        {"skillId": "amzn1.ask.skill.game", "apis": ["custom"],
         "nameByLocale": {"en-US": "Trivia"}, "stage": "development"},
    ])
    body = ui.get("/setup/wizard/skill").data.decode()
    assert "already has a music skill" in body
    assert "Old Bridge" in body
    assert "Trivia" not in body             # non-music skills are not noise


def test_removing_a_leftover_skill_requires_confirmation(ui, monkeypatch):
    fake_rest(monkeypatch)
    monkeypatch.setattr(smapi_rest, "list_skills", lambda: [])
    removed = []
    monkeypatch.setattr(smapi_rest, "delete_skill", removed.append)
    store.update(endpoint_ok=True)

    body = ui.post("/setup/wizard/skill/remove",
                   data={"skill_id": "amzn1.ask.skill.old"},
                   headers={"HX-Request": "true"}).data.decode()
    assert "confirmation" in body
    assert removed == []

    body = ui.post("/setup/wizard/skill/remove",
                   data={"skill_id": "amzn1.ask.skill.old", "confirm": "yes"},
                   headers={"HX-Request": "true"}).data.decode()
    assert "Deleted amzn1.ask.skill.old" in body
    assert removed == ["amzn1.ask.skill.old"]


def test_the_manifest_declares_every_required_music_request():
    """Bare namespaces fail async validation with "an interface is missing"."""
    body = json.dumps(smapi.manifest(name="Ampere",
                                     public_base="https://ampere.example.com",
                                     cert_type="Trusted", aliases=["ampere"]))
    for request_name in ("GetPlayableContent", "Initiate", "GetNextItem",
                         "GetPreviousItem"):
        assert request_name in body, request_name


def test_a_skill_that_fails_validation_is_deleted_and_explained(ui, monkeypatch):
    """Creation returning a skillId is not acceptance."""
    created = fake_rest(monkeypatch, skill_status=lambda sid: {
        "manifest": {"lastUpdateRequest": {
            "status": "FAILED",
            "errors": [{"message": "An interface is missing"}]}}})
    store.update(endpoint_ok=True, alias="ampere")
    body = ui.post("/setup/wizard/skill", data={"alias": "ampere"},
                   headers={"HX-Request": "true"}).data.decode()
    assert "Amazon rejected the manifest" in body
    assert "An interface is missing" in body
    assert created["deleted"] == ["amzn1.ask.skill.abc"]
    assert not store.load().get("skill_id")


def test_catalog_step_reuses_orphans_and_records_before_association(ui, monkeypatch):
    """A created-but-unassociated catalog must be found again, not re-minted."""
    boom = {"raise": True}

    def associate(skill_id, catalog_id):
        if boom["raise"]:
            raise smapi_rest.SmapiError("association exploded")

    created = fake_rest(monkeypatch, associate_catalog=associate,
                        list_catalogs=lambda: [
                            {"id": "cat-orphan", "title": "Ampere artists"}])
    store.update(endpoint_ok=True, skill_id="amzn1.ask.skill.a")

    ui.post("/setup/wizard/catalogs", headers={"HX-Request": "true"})
    saved = store.load()["catalogs"]
    assert saved["artists"] == "cat-orphan"       # reused, not re-created
    assert all(saved.values())                    # recorded despite failures
    assert ("artists", "AMAZON.MusicGroup") not in created["catalogs"]

    boom["raise"] = False                          # association healed
    resp = ui.post("/setup/wizard/catalogs", headers={"HX-Request": "true"})
    assert resp.status_code == 204                 # every kind associated
    assert store.load()["catalogs"] == saved       # nothing minted twice


def test_a_vanished_skill_undoes_the_step_and_offers_repair(ui, monkeypatch):
    """A recorded id is not proof: the skill can die outside the wizard."""
    fake_rest(monkeypatch, skill_status=lambda sid: (_ for _ in ()).throw(
        smapi_rest.SmapiError("GET failed: 404", status=404)))
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    store.update(endpoint_ok=True, alias="ampere",
                 skill_id="amzn1.ask.skill.gone", enabled=True)
    body = ui.get("/setup/wizard/skill").data.decode()
    assert "no longer exists" in body
    assert "/setup/wizard/skill/forget" in body

    resp = ui.post("/setup/wizard/skill/forget", headers={"HX-Request": "true"})
    assert resp.status_code == 204
    assert store.load()["skill_id"] == ""
    assert store.load()["enabled"] is False


def test_a_lookup_blip_does_not_undo_the_skill_step(monkeypatch):
    """Only a definitive 404 counts as gone; a 500 or timeout must not."""
    from setup_ui import steps as wizard_steps
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "skill_status", lambda sid: (_ for _ in ()).throw(
        smapi_rest.SmapiError("GET failed: 500", status=500)))
    assert wizard_steps._skill_done({"skill_id": "amzn1.ask.skill.x"}) is True


def test_recreating_the_skill_reattaches_recorded_catalogs(ui, monkeypatch):
    created = fake_rest(monkeypatch)
    store.update(endpoint_ok=True, alias="ampere",
                 catalogs={"artists": "cat-1", "albums": "cat-2"})
    ui.post("/setup/wizard/skill", data={"alias": "ampere"},
            headers={"HX-Request": "true"})
    assert ("amzn1.ask.skill.abc", "cat-1") in created["associations"]
    assert ("amzn1.ask.skill.abc", "cat-2") in created["associations"]


def test_upload_runs_as_a_job_and_reports_progress(ui, monkeypatch):
    """A minutes-long build cannot live inside one request."""
    ran = {}

    class Immediate:
        def __init__(self, target, daemon=None):
            ran["target"] = target
        def start(self):
            ran["target"]()

    monkeypatch.setattr(views.threading, "Thread", Immediate)
    monkeypatch.setattr(views.catalog_sync, "collect",
                        lambda progress=None: {"artists": [{"id": "a1"}]})
    monkeypatch.setattr(views.catalog_sync, "apply_timestamps",
                        lambda kind, entities, saved: (entities, "hash"))
    monkeypatch.setattr(smapi_rest, "upload_catalog",
                        lambda catalog_id, payload: "up-1")
    store.update(endpoint_ok=True, skill_id="amzn1.ask.skill.a",
                 catalogs={"artists": "cat-1"})

    body = ui.post("/setup/wizard/upload",
                   headers={"HX-Request": "true"}).data.decode()
    assert store.load()["uploads"]["artists"] == "up-1"
    assert "Reload" in body                    # the job already finished here

    # The finished poll answers with a reload script (the leave guard has to
    # come off before the reload, or the dialog would fire on success), so
    # the rail and the ingestion panel re-derive together.
    resp = ui.get("/setup/wizard/upload/progress",
                  headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"location.reload()" in resp.data
    assert b"removeEventListener" in resp.data


def test_leaving_the_upload_page_stops_the_job(ui, monkeypatch):
    # The parting beacon flags the job; the next progress tick aborts it
    # without recording anything.
    views._UPLOAD.update(running=True, cancel=False)
    resp = ui.post("/setup/wizard/upload/stop")
    assert resp.status_code == 204
    assert views._UPLOAD["cancel"] is True

    called = []
    monkeypatch.setattr(views.catalog_sync, "collect",
                        lambda progress=None: called.append("collect"))
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    views._run_upload()
    assert not called                      # aborted before the crawl began
    assert views._UPLOAD["message"].startswith("Stopped")
    assert views._UPLOAD["running"] is False
    assert store.load().get("uploads") in (None, {})


def test_upload_step_is_not_done_until_ingestion_succeeds(ui, monkeypatch):
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "upload_status", lambda c, u: {
        "ingestionSteps": [{"name": "ER_INGESTION", "status": "IN_PROGRESS"}]})
    state = {"catalogs": {"artists": "c1"}, "uploads": {"artists": "u1"}}
    assert views.wizard_steps._upload_done(state) is False

    views.wizard_steps._INGESTION.update(at=0.0, sig="", ok=False)
    monkeypatch.setattr(smapi_rest, "upload_status", lambda c, u: {
        "ingestionSteps": [{"name": "ER_INGESTION", "status": "SUCCEEDED"}]})
    assert views.wizard_steps._upload_done(state) is True


def test_upload_page_shows_the_running_phase(ui, monkeypatch):
    views._UPLOAD.update(running=True, phase="uploading tracks")
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    monkeypatch.setattr(smapi_rest, "skill_status", lambda sid: {
        "manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}}})
    store.update(endpoint_ok=True, alias="ampere",
                 skill_id="amzn1.ask.skill.a", catalogs={"artists": "c1"})
    body = ui.get("/setup/wizard/upload").data.decode()
    assert "uploading tracks" in body
    # No second start button while the job is running.
    assert "Build and upload the catalogs</button>" not in body


def test_amazon_step_offers_copyable_console_values(ui, monkeypatch):
    """Every value the Amazon console asks for is a copy row, not prose."""
    monkeypatch.setenv("SUBSONIC_URL", "http://nav.test")
    store.update(endpoint_ok=True)
    body = ui.get("/setup/wizard/amazon").data.decode()
    assert body.count("data-copy") >= 4
    assert 'value="Ampere"' in body          # Security Profile Name
    assert "cp-privacy-url" in body          # Consent Privacy Notice URL
    assert "cp-return-url" in body           # Allowed Return URL
    assert "copy.js" in body


def test_a_finished_wizard_shows_the_done_page(ui, monkeypatch):
    _all_steps_done(monkeypatch)
    body = ui.get("/setup/wizard").data.decode()
    assert "Setup complete" in body
    # The done page is a hub, not a dead end.
    for link in ("/setup/alias", "/setup/stations", "/setup/endpoint", "/setup"):
        assert link in body, link


def test_a_finished_wizard_stops_hijacking_the_landing_page(ui, monkeypatch):
    _all_steps_done(monkeypatch)
    assert ui.get("/setup").status_code == 200


def test_an_unknown_step_goes_back_to_the_wizard(ui):
    resp = ui.get("/setup/wizard/nonsense")
    assert resp.status_code == 302


def test_completion_is_derived_not_stored(ui, monkeypatch):
    """A stored flag would still claim done after the skill was deleted."""
    _all_steps_done(monkeypatch)
    assert ui.get("/setup").status_code == 200
    store.update(skill_id="")
    assert ui.get("/setup").status_code == 302


# --- completing a step reloads the page, failing keeps the error inline -----


def test_a_completed_step_asks_the_browser_to_refresh(ui, monkeypatch):
    """A fragment swap cannot update the rail or unlock Continue."""
    monkeypatch.setattr(validate, "subsonic_ping",
                        lambda url, user, password, timeout=8.0: {"ok": True,
                                                                  "detail": "ok"})
    resp = ui.post("/setup/wizard/subsonic",
                   data={"url": "http://nav.test", "user": "x", "password": "y"},
                   headers={"HX-Request": "true"})
    assert resp.status_code == 204
    assert resp.headers.get("HX-Refresh") == "true"


def test_a_failed_step_keeps_the_error_next_to_the_form(ui, monkeypatch):
    monkeypatch.setattr(validate, "subsonic_ping",
                        lambda url, user, password, timeout=8.0: {
                            "ok": False, "detail": "Wrong username"})
    resp = ui.post("/setup/wizard/subsonic",
                   data={"url": "http://nav.test", "user": "x", "password": "y"},
                   headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Wrong username" in resp.data.decode()
    assert "HX-Refresh" not in resp.headers


def test_without_htmx_a_completed_step_still_renders(ui, monkeypatch):
    """The no-JavaScript path gets a page, not an empty 204."""
    monkeypatch.setattr(validate, "subsonic_ping",
                        lambda url, user, password, timeout=8.0: {"ok": True,
                                                                  "detail": "ok"})
    resp = ui.post("/setup/wizard/subsonic",
                   data={"url": "http://nav.test", "user": "x", "password": "y"})
    assert resp.status_code == 200
    assert b"connected" in resp.data


# --- the endpoint step accepts live traffic as proof ------------------------


@pytest.fixture
def captures_dir(tmp_path, monkeypatch):
    """A capture directory of this test's own.

    The suite-wide CAPTURE_DIR accumulates files from other tests, some of
    which carry signature headers, so evidence tests must not share it.
    """
    from setup_ui import steps
    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path / "captures"))
    (tmp_path / "captures").mkdir()
    steps._TRAFFIC.update(at=0.0, ok=False)  # bypass the 60s cache
    yield tmp_path / "captures"
    steps._TRAFFIC.update(at=0.0, ok=False)


def _capture(directory, name: str, headers: dict):
    (directory / name).write_text(json.dumps({"headers": headers, "body": {}}))


def test_signed_amazon_traffic_satisfies_the_endpoint_step(captures_dir):
    from setup_ui import steps
    _capture(captures_dir, "sig.json",
             {"Signature-256": "x", "SignatureCertChainUrl": "y"})
    assert steps._endpoint_done({}) is True


def test_an_unsigned_capture_proves_nothing(captures_dir):
    """Local smoke tests write captures too, and must not unlock the step."""
    from setup_ui import steps
    _capture(captures_dir, "nosig.json", {"User-Agent": "curl"})
    assert steps._endpoint_done({}) is False


def test_no_captures_at_all_is_not_proof(captures_dir):
    from setup_ui import steps
    assert steps._endpoint_done({}) is False


def test_the_stored_flag_still_counts(captures_dir):
    from setup_ui import steps
    assert steps._endpoint_done({"endpoint_ok": True}) is True
