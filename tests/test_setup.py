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
    monkeypatch.setattr(validate, "http_get", lambda url, timeout=8.0: (200, '{"ok":true}'))
    monkeypatch.setattr(
        validate, "http_post_json",
        lambda url, body, timeout=12.0: (
            200,
            json.dumps({"header": {"namespace": "Alexa.Media.Search",
                                   "name": "GetPlayableContent.Response"},
                        "payload": {}}),
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
    resp = ui.get("/setup")
    assert resp.status_code == 200
    assert b"ER_INGESTION" in resp.data


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
    body = ui.get("/setup").data.decode()
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
                        lambda url, body, timeout=12.0: (400, "no json"))
    row = validate.check_music_post("https://ampere.example.com")
    assert row["ok"] is False
    assert "dropped the request body" in row["detail"]


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


def test_alias_page_explains_why_it_matters(ui):
    body = ui.get("/setup/alias").data.decode()
    assert "before" in body
    assert "catalog" in body


def test_alias_page_reports_a_collision(ui, monkeypatch):
    library(monkeypatch, artists=["Jukebox The Ghost"])
    body = ui.post("/setup/alias", data={"candidate": "jukebox"}).data.decode()
    assert "Jukebox The Ghost" in body


# --- stations ---------------------------------------------------------------


def test_stations_page_lists_every_after_content_mode(ui):
    body = ui.get("/setup/stations").data.decode()
    for mode in app_module.AFTER_CONTENT_MODES:
        assert f'value="{mode}"' in body


def test_stations_page_says_a_restart_is_needed(ui):
    assert "restart" in ui.get("/setup/stations").data.decode()


def test_saving_stations_persists(ui):
    ui.post("/setup/stations", data={"after_content": "radio", "radio_artists": "7",
                                     "radio_tracks_per_artist": "3"})
    saved = store.load()
    assert saved["after_content"] == "radio"
    assert saved["radio_artists"] == 7


def test_saving_stations_refuses_an_unknown_mode(ui):
    ui.post("/setup/stations", data={"after_content": "teleport",
                                     "radio_artists": "7",
                                     "radio_tracks_per_artist": "3"})
    assert store.load()["after_content"] in app_module.AFTER_CONTENT_MODES


def test_station_preview_shows_the_artist_pool(ui):
    body = ui.get("/setup/stations/preview?seed=Gregory").data.decode()
    assert "Blind Pilot" in body
    assert "Iron and Wine" in body


def test_station_preview_calls_out_a_degraded_pool(ui, monkeypatch):
    monkeypatch.setattr(app_module.subsonic, "similar_artists",
                        lambda artist_id, count=20: [])
    app_module._RADIO_CACHE.clear()
    body = ui.get("/setup/stations/preview?seed=Gregory").data.decode()
    assert "seed artist alone" in body


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
        if rule.endpoint in ("setup.static", "setup.login", "setup.verify"):
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
    assert client.get("/setup").status_code == 401


def test_login_sets_a_working_cookie(client):
    resp = client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    assert resp.status_code == 302
    assert client.get("/setup").status_code == 200


def test_login_will_not_redirect_off_setup(client):
    resp = client.post("/setup/login",
                       data={"token": ADMIN, "target": "https://evil.example/"})
    assert resp.headers["Location"] == "/setup"


def test_cookie_dies_when_the_admin_token_rotates(client, monkeypatch):
    client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    assert client.get("/setup").status_code == 200
    monkeypatch.setenv("ADMIN_TOKEN", "a-different-token")
    assert client.get("/setup").status_code == 401


def test_logout_clears_the_cookie(client):
    client.post("/setup/login", data={"token": ADMIN, "target": "/setup"})
    client.get("/setup/logout")
    assert client.get("/setup").status_code == 401


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


def test_wizard_creates_a_skill_once_the_endpoint_passes(ui, monkeypatch):
    calls = fake_cli(monkeypatch, '{"skillId": "amzn1.ask.skill.abc"}')
    store.update(endpoint_ok=True, cert_type="Wildcard")
    body = ui.post("/setup/wizard/skill",
                   data={"vendor_id": "M1VENDOR", "alias": "ampere"}).data.decode()
    assert "amzn1.ask.skill.abc" in body
    assert store.load()["skill_id"] == "amzn1.ask.skill.abc"
    assert calls[0][:3] == ["ask", "smapi", "create-skill-for-vendor"]
    assert all(isinstance(arg, str) for arg in calls[0])


def test_created_manifest_carries_the_derived_cert_type(ui, monkeypatch, tmp_path):
    written = {}

    def capture_manifest(body, directory):
        written.update(body)
        return str(tmp_path / "m.json")

    fake_cli(monkeypatch, '{"skillId": "amzn1.ask.skill.abc"}')
    monkeypatch.setattr(smapi, "write_manifest", capture_manifest)
    store.update(endpoint_ok=True, cert_type="Wildcard")
    ui.post("/setup/wizard/skill", data={"vendor_id": "M1", "alias": "ampere"})
    endpoint = written["manifest"]["apis"]["music"]["endpoint"]
    assert endpoint["sslCertificateType"] == "Wildcard"


def test_wizard_creates_every_catalog_kind(ui, monkeypatch):
    calls = fake_cli(monkeypatch, '{"id": "amzn1.ask.catalog.x"}')
    store.update(skill_id="amzn1.ask.skill.abc", vendor_id="M1VENDOR")
    body = ui.post("/setup/wizard/catalogs").data.decode()
    for kind in views.CATALOG_KINDS:
        assert kind in body
    created = [c for c in calls if c[2] == "create-catalog"]
    assert len(created) == len(views.CATALOG_KINDS)


def test_catalog_creation_needs_a_skill_first(ui, monkeypatch):
    fake_cli(monkeypatch)
    assert "Create the skill first" in ui.post("/setup/wizard/catalogs").data.decode()


def test_status_refresh_shouts_about_missing_enablement(ui, monkeypatch):
    fake_cli(monkeypatch, "", code=1)
    store.update(skill_id="amzn1.ask.skill.abc")
    body = ui.post("/setup/status/refresh").data.decode()
    assert "not enabled" in body
    assert "from Spotify" in body


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
    assert "pico.min.css" in body


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
    for asset in ("vendor/pico.min.css", "vendor/htmx.min.js", "setup.css"):
        assert ui.get(f"/setup/static/{asset}").status_code == 200


def test_no_page_links_a_cdn(ui):
    for path in ("/setup", "/setup/endpoint", "/setup/alias",
                 "/setup/wizard", "/setup/stations", "/setup/login"):
        body = ui.get(path).data.decode()
        assert "unpkg.com" not in body
        assert "cdn.jsdelivr.net" not in body
