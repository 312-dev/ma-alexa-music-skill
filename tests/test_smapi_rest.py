"""The SMAPI REST client and its Login with Amazon round trip.

No test here reaches Amazon. Everything goes through _post_form, call, or
urlopen, all of which are replaced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from ma_provider import smapi_rest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE", "https://music.example.com")
    monkeypatch.delenv("VENDOR_ID", raising=False)
    smapi_rest.forget_credentials()
    yield
    smapi_rest.forget_credentials()


# --- the authorization code grant -------------------------------------------


def test_redirect_uri_is_derived_from_public_base():
    assert smapi_rest.redirect_uri() == "https://music.example.com/setup/oauth/callback"


def test_redirect_uri_is_empty_without_a_public_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE", "")
    assert smapi_rest.redirect_uri() == ""


def test_authorize_url_carries_everything_amazon_needs():
    url, state, verifier = smapi_rest.begin("amzn1.application-oa2-client.x")
    assert url.startswith(smapi_rest.AUTHORIZE_URL)
    for fragment in ("client_id=amzn1.application-oa2-client.x",
                     "response_type=code",
                     "code_challenge_method=S256",
                     "alexa%3A%3Aask%3Askills%3Areadwrite"):
        assert fragment in url, fragment
    assert state and verifier


def test_the_pkce_challenge_is_the_sha256_of_the_verifier():
    """A wrong challenge fails only at the exchange, with an opaque error."""
    url, _state, verifier = smapi_rest.begin("client")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert f"code_challenge={expected}" in url


def test_each_consent_request_is_unique():
    _, state_a, verifier_a = smapi_rest.begin("client")
    _, state_b, verifier_b = smapi_rest.begin("client")
    assert state_a != state_b and verifier_a != verifier_b


# --- tokens -----------------------------------------------------------------


def fake_tokens(monkeypatch, payload, calls=None):
    def post(url, fields):
        if calls is not None:
            calls.append(fields)
        return payload
    monkeypatch.setattr(smapi_rest, "_post_form", post)


def test_exchange_stores_only_the_refresh_token(monkeypatch):
    """An access token on disk would be a stale secret nobody prunes."""
    fake_tokens(monkeypatch, {"refresh_token": "r1", "access_token": "a1",
                              "expires_in": 3600})
    smapi_rest.complete("code", "cid", "secret", "verifier")

    stored = smapi_rest.load_credentials()
    assert stored["refresh_token"] == "r1"
    assert "access_token" not in stored
    assert smapi_rest.connected()


def test_exchange_sends_the_verifier(monkeypatch):
    calls = []
    fake_tokens(monkeypatch, {"refresh_token": "r"}, calls)
    smapi_rest.complete("thecode", "cid", "sec", "theverifier")
    assert calls[0]["code_verifier"] == "theverifier"
    assert calls[0]["grant_type"] == "authorization_code"
    assert calls[0]["redirect_uri"] == smapi_rest.redirect_uri()


def test_a_response_without_a_refresh_token_is_an_error(monkeypatch):
    fake_tokens(monkeypatch, {"access_token": "a"})
    with pytest.raises(smapi_rest.SmapiError):
        smapi_rest.complete("c", "cid", "sec", "v")
    assert not smapi_rest.connected()


def test_access_token_is_cached_between_calls(monkeypatch):
    calls = []
    fake_tokens(monkeypatch, {"refresh_token": "r", "access_token": "a",
                              "expires_in": 3600}, calls)
    smapi_rest.complete("c", "cid", "sec", "v")
    before = len(calls)
    assert smapi_rest.access_token() == "a"
    assert smapi_rest.access_token() == "a"
    assert len(calls) == before, "a cached token must not hit the network"


def test_an_expired_access_token_is_refreshed(monkeypatch):
    fake_tokens(monkeypatch, {"refresh_token": "r", "access_token": "a",
                              "expires_in": 3600})
    smapi_rest.complete("c", "cid", "sec", "v")
    smapi_rest._ACCESS["expires"] = time.time() - 1
    fake_tokens(monkeypatch, {"access_token": "fresh", "expires_in": 3600})
    assert smapi_rest.access_token() == "fresh"


def test_asking_for_a_token_while_disconnected_is_a_clear_error():
    with pytest.raises(smapi_rest.SmapiError, match="not connected"):
        smapi_rest.access_token()


def test_disconnecting_removes_the_token(monkeypatch):
    fake_tokens(monkeypatch, {"refresh_token": "r"})
    smapi_rest.complete("c", "cid", "sec", "v")
    smapi_rest.forget_credentials()
    assert not smapi_rest.connected()


# --- the manifest shape -----------------------------------------------------


def test_an_already_wrapped_manifest_is_not_wrapped_again():
    """smapi.manifest() returns {"manifest": ...}; sending that nests it."""
    inner = {"manifestVersion": "1.0"}
    assert smapi_rest._unwrap({"manifest": inner}) is inner


def test_a_bare_manifest_passes_through():
    bare = {"manifestVersion": "1.0"}
    assert smapi_rest._unwrap(bare) is bare


def test_catalog_usage_pairs_with_its_type(monkeypatch):
    """AMAZON.MusicGroup must ship with AlexaMusic.Catalog.MusicGroup; a
    mismatched usage is refused with a type/usage 404."""
    sent = {}

    def call(method, path, body=None):
        sent.update(body or {})
        return {"id": "cat-1"}

    monkeypatch.setattr(smapi_rest, "call", call)
    monkeypatch.setattr(smapi_rest, "vendor_id", lambda: "M1")
    smapi_rest.create_catalog("ampere-artists", "AMAZON.MusicGroup")
    assert sent["usage"] == "AlexaMusic.Catalog.MusicGroup"
    smapi_rest.create_catalog("ampere-genres", "AMAZON.Genre")
    assert sent["usage"] == "AlexaMusic.Catalog.Genre"


# --- vendors ----------------------------------------------------------------


def test_vendor_id_comes_from_the_environment_when_forced(monkeypatch):
    monkeypatch.setenv("VENDOR_ID", "M1FORCED")
    assert smapi_rest.vendor_id() == "M1FORCED"


def test_a_single_vendor_is_selected(monkeypatch):
    monkeypatch.setattr(smapi_rest, "call",
                        lambda *a, **k: {"vendors": [{"id": "M1", "name": "me"}]})
    assert smapi_rest.vendor_id() == "M1"


def test_several_vendors_refuse_to_be_guessed(monkeypatch):
    """Picking one silently would put the skill somewhere nobody chose."""
    monkeypatch.setattr(smapi_rest, "call", lambda *a, **k: {"vendors": [
        {"id": "M1", "name": "one"}, {"id": "M2", "name": "two"}]})
    with pytest.raises(smapi_rest.SmapiError, match="VENDOR_ID"):
        smapi_rest.vendor_id()


def test_no_vendor_at_all_is_an_error(monkeypatch):
    monkeypatch.setattr(smapi_rest, "call", lambda *a, **k: {"vendors": []})
    with pytest.raises(smapi_rest.SmapiError):
        smapi_rest.vendor_id()


# --- ingestion, which is the only thing that decides whether voice works -----


def steps(**pairs):
    return {"ingestionSteps": [{"name": k, "status": v} for k, v in pairs.items()]}


def test_er_ingestion_succeeded_is_ready():
    state, detail = smapi_rest.ingestion_verdict(steps(ER_INGESTION="SUCCEEDED"))
    assert state == "ready"
    assert "ER_INGESTION" in detail


def test_slu_modelling_pending_does_not_block():
    """It sits here for weeks and never affects playback."""
    state, _ = smapi_rest.ingestion_verdict(
        steps(ER_INGESTION="SUCCEEDED", SLU_MODELING="IN_PROGRESS"))
    assert state == "ready"


def test_a_top_level_in_progress_carries_no_information():
    status = steps(ER_INGESTION="SUCCEEDED", SLU_MODELING="IN_PROGRESS")
    status["status"] = "IN_PROGRESS"
    assert smapi_rest.ingestion_verdict(status)[0] == "ready"


def test_er_ingestion_failed_is_failed():
    assert smapi_rest.ingestion_verdict(steps(ER_INGESTION="FAILED"))[0] == "failed"


def test_er_ingestion_still_running_is_waiting():
    assert smapi_rest.ingestion_verdict(
        steps(ER_INGESTION="IN_PROGRESS"))[0] == "waiting"


def test_no_steps_at_all_is_waiting():
    assert smapi_rest.ingestion_verdict({})[0] == "waiting"


# --- catalog upload ---------------------------------------------------------


def test_upload_puts_the_body_and_completes_with_the_etag(monkeypatch):
    """S3 wants the ETag back exactly as sent, quotes included."""
    seen = {}

    def call(method, path, body=None):
        seen.setdefault("calls", []).append((method, path, body))
        if method == "POST" and path.endswith("/uploads"):
            return {"id": "up-1", "presignedUploadParts": [
                {"url": "https://s3.example/put", "partNumber": 1}]}
        return {}

    class Response:
        headers = {"ETag": '"abc123"'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=0):
        seen["put"] = {"url": request.full_url, "data": request.data,
                       "method": request.method}
        return Response()

    monkeypatch.setattr(smapi_rest, "call", call)
    monkeypatch.setattr(smapi_rest.urllib.request, "urlopen", urlopen)

    assert smapi_rest.upload_catalog("cat-1", b'{"entities":[]}') == "up-1"
    assert seen["put"]["method"] == "PUT"
    assert seen["put"]["data"] == b'{"entities":[]}'

    complete = seen["calls"][-1]
    assert complete[1] == "/v0/catalogs/cat-1/uploads/up-1"
    assert complete[2] == {"partETags": [{"eTag": '"abc123"', "partNumber": 1}]}


def test_an_upload_that_returns_nothing_usable_is_an_error(monkeypatch):
    monkeypatch.setattr(smapi_rest, "call", lambda *a, **k: {})
    with pytest.raises(smapi_rest.SmapiError):
        smapi_rest.upload_catalog("cat-1", b"{}")


# --- enablement -------------------------------------------------------------


def test_enablement_status_reads_a_404_as_not_enabled(monkeypatch):
    def call(method, path, body=None):
        raise smapi_rest.SmapiError("nope", 404, "")
    monkeypatch.setattr(smapi_rest, "call", call)
    assert smapi_rest.enablement_status("skill") is False


def test_enablement_status_does_not_swallow_a_real_failure(monkeypatch):
    def call(method, path, body=None):
        raise smapi_rest.SmapiError("boom", 500, "")
    monkeypatch.setattr(smapi_rest, "call", call)
    with pytest.raises(smapi_rest.SmapiError):
        smapi_rest.enablement_status("skill")


def test_errors_carry_the_status_and_body(monkeypatch):
    exc = smapi_rest.SmapiError("failed", 400, "violations: ...")
    assert exc.status == 400 and "violations" in exc.body
