"""SMAPI over REST, authorized by Login with Amazon.

This exists because the ASK CLI cannot be driven from a container. `ask` is a
Node program that stores credentials obtained through an interactive browser
round trip in ~/.ask/cli_config, and there is no documented way to mint those
headlessly. Shipping Node and npm in the image would still leave the operator
running `ask configure` by hand in a terminal, which defeats the point of
having a setup wizard at all.

Amazon documents this route for precisely this case: "If you're building your
own tool or service to integrate with the API, you must implement OAuth 2.0
integration with Login with Amazon to request your users' authorization and
retrieve access tokens. The API requires the authorization code grant type."

So the wizard does the authorization code grant in the browser the operator is
already sitting in, keeps the refresh token, and calls the REST API directly.
No Node, no CLI, nothing to install.

Two notes on the shape of it:

The redirect has to be https, which is why this step comes after endpoint
validation rather than before. By then PUBLIC_BASE is proven to answer from the
public internet, so it is the one https origin we know works.

Only the refresh token is stored. Access tokens last an hour and are kept in
memory, so a stolen state file is worth less and there is nothing to expire on
disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import pathlib
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("ma-music-skill.smapi")

AUTHORIZE_URL = "https://www.amazon.com/ap/oa"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
API_BASE = "https://api.amazonalexa.com"

# Everything the wizard needs and nothing else. catalogs:read is included
# alongside readwrite because the upload status poll uses it, and asking for it
# up front avoids a second consent round trip halfway through setup.
SCOPES = (
    "alexa::ask:skills:readwrite "
    "alexa::ask:models:readwrite "
    "alexa::ask:skills:test "
    "alexa::ask:catalogs:read "
    "alexa::ask:catalogs:readwrite"
)

TIMEOUT = 30
_LOCK = threading.Lock()
_ACCESS: dict = {"token": None, "expires": 0.0}


class SmapiError(RuntimeError):
    """A SMAPI or LWA call that failed, carrying enough to act on."""

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


# --- credential storage -----------------------------------------------------


def state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SETUP_STATE_DIR", "/data"))


def _creds_path() -> pathlib.Path:
    return state_dir() / "smapi-credentials.json"


def load_credentials() -> dict:
    try:
        return json.loads(_creds_path().read_text())
    except (OSError, ValueError):
        return {}


def save_credentials(data: dict) -> None:
    path = _creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    # Refresh tokens do not expire on their own, so this file is as good as the
    # developer account until it is revoked.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    with _LOCK:
        _ACCESS["token"] = None
        _ACCESS["expires"] = 0.0


def forget_credentials() -> None:
    try:
        _creds_path().unlink()
    except OSError:
        pass
    with _LOCK:
        _ACCESS["token"] = None
        _ACCESS["expires"] = 0.0


def connected() -> bool:
    return bool(load_credentials().get("refresh_token"))


def redirect_uri() -> str:
    base = (os.environ.get("PUBLIC_BASE") or "").rstrip("/")
    return f"{base}/setup/oauth/callback" if base else ""


# --- the authorization code grant -------------------------------------------


def _verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def begin(client_id: str) -> tuple[str, str, str]:
    """Return (url, state, code_verifier) to start consent.

    PKCE is used even though this is a confidential client with a secret. The
    authorization code travels back through the operator's browser over a
    public route, and binding it to a verifier this process holds means an
    intercepted code alone is not enough to exchange.
    """
    state = secrets.token_urlsafe(24)
    verifier = _verifier()
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}", state, verifier


def _post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise SmapiError(f"token request failed: {exc.code}", exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise SmapiError(f"token request failed: {exc.reason}") from exc


def complete(code: str, client_id: str, client_secret: str, verifier: str) -> dict:
    """Exchange the authorization code and persist the refresh token."""
    tokens = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    })
    if not tokens.get("refresh_token"):
        raise SmapiError("Amazon returned no refresh token")
    save_credentials({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        "obtained": time.time(),
    })
    with _LOCK:
        _ACCESS["token"] = tokens.get("access_token")
        _ACCESS["expires"] = time.time() + int(tokens.get("expires_in", 3600)) - 60
    return tokens


def access_token() -> str:
    with _LOCK:
        if _ACCESS["token"] and time.time() < _ACCESS["expires"]:
            return _ACCESS["token"]

    creds = load_credentials()
    if not creds.get("refresh_token"):
        raise SmapiError("not connected to Amazon yet")

    tokens = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
    })
    token = tokens.get("access_token")
    if not token:
        raise SmapiError("refresh returned no access token")
    with _LOCK:
        _ACCESS["token"] = token
        _ACCESS["expires"] = time.time() + int(tokens.get("expires_in", 3600)) - 60
    return token


# --- the API ----------------------------------------------------------------


def call(method: str, path: str, body: dict | None = None) -> dict:
    """One SMAPI request. Returns {} for the empty bodies many calls answer."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": access_token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        raise SmapiError(f"{method} {path} failed: {exc.code}", exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise SmapiError(f"{method} {path} failed: {exc.reason}") from exc


def vendor_id() -> str:
    """The vendor the skill is created under.

    An account can hold more than one. Taking the first silently would put the
    skill somewhere the operator did not choose, so this only auto-selects when
    the choice is unambiguous.
    """
    if forced := (os.environ.get("VENDOR_ID") or "").strip():
        return forced
    vendors = call("GET", "/v1/vendors").get("vendors") or []
    if not vendors:
        raise SmapiError("this developer account has no vendor")
    if len(vendors) > 1:
        names = ", ".join(f"{v.get('name')} ({v.get('id')})" for v in vendors)
        raise SmapiError(
            f"this account has several vendors, set VENDOR_ID to one of: {names}"
        )
    return vendors[0]["id"]


# --- skills -----------------------------------------------------------------


def _unwrap(manifest: dict) -> dict:
    """Accept either the bare manifest or one already wrapped in {"manifest": ...}.

    smapi.manifest() returns the wrapped form because that is what the CLI
    wanted on disk. Wrapping it again here sends manifest.manifest, which SMAPI
    rejects with a validation error that names a field nobody wrote.
    """
    inner = manifest.get("manifest")
    return inner if isinstance(inner, dict) else manifest


def vendors() -> list[dict]:
    """Every vendor on the account, for the wizard to offer rather than guess."""
    return call("GET", "/v1/vendors").get("vendors") or []


def create_skill(manifest: dict, vendor: str = "") -> str:
    result = call("POST", "/v1/skills", {
        "vendorId": vendor or vendor_id(), "manifest": _unwrap(manifest),
    })
    skill_id = result.get("skillId")
    if not skill_id:
        raise SmapiError("skill creation returned no skillId")
    return skill_id


def skill_status(skill_id: str) -> dict:
    return call("GET", f"/v1/skills/{skill_id}/status?resource=manifest")


def get_manifest(skill_id: str, stage: str = "development") -> dict:
    return call("GET", f"/v1/skills/{skill_id}/stages/{stage}/manifest")


def update_manifest(skill_id: str, manifest: dict, stage: str = "development") -> None:
    call("PUT", f"/v1/skills/{skill_id}/stages/{stage}/manifest",
         {"manifest": _unwrap(manifest)})


def list_skills() -> list[dict]:
    query = urllib.parse.urlencode({"vendorId": vendor_id(), "maxResults": 50})
    return call("GET", f"/v1/skills?{query}").get("skills") or []


def delete_skill(skill_id: str) -> None:
    call("DELETE", f"/v1/skills/{skill_id}")


def set_enablement(skill_id: str, stage: str = "development") -> None:
    call("PUT", f"/v1/skills/{skill_id}/stages/{stage}/enablement")


def delete_enablement(skill_id: str, stage: str = "development") -> None:
    call("DELETE", f"/v1/skills/{skill_id}/stages/{stage}/enablement")


def enablement_status(skill_id: str, stage: str = "development") -> bool:
    try:
        call("GET", f"/v1/skills/{skill_id}/stages/{stage}/enablement")
        return True
    except SmapiError as exc:
        if exc.status in (404, 401):
            return False
        raise


# --- catalogs ---------------------------------------------------------------


def create_catalog(title: str, catalog_type: str) -> str:
    # The usage must pair with the type exactly: AMAZON.MusicGroup goes with
    # AlexaMusic.Catalog.MusicGroup, and so on. Any other combination is
    # refused with "The specified type/usage combination is invalid."
    usage = catalog_type.replace("AMAZON.", "AlexaMusic.Catalog.", 1)
    result = call("POST", "/v0/catalogs", {
        "vendorId": vendor_id(),
        "title": title,
        "type": catalog_type,
        "usage": usage,
    })
    catalog_id = result.get("id") or result.get("catalogId")
    if not catalog_id:
        raise SmapiError(f"catalog creation returned no id: {result}")
    return catalog_id


def associate_catalog(skill_id: str, catalog_id: str) -> None:
    """Must happen before any upload, or the content has nowhere to resolve."""
    call("PUT", f"/v0/skills/{skill_id}/catalogs/{catalog_id}")


def list_catalogs() -> list[dict]:
    query = urllib.parse.urlencode({"vendorId": vendor_id(), "maxResults": 50})
    return call("GET", f"/v0/catalogs?{query}").get("catalogs") or []


def delete_catalog(catalog_id: str) -> None:
    call("DELETE", f"/v0/catalogs/{catalog_id}")


def upload_catalog(catalog_id: str, payload: bytes) -> str:
    """Presigned multipart upload, then completion. Returns the upload id.

    One part. Catalogs here are a few megabytes and S3's multipart minimum does
    not apply to a single-part upload, so splitting would add failure modes for
    nothing.
    """
    created = call("POST", f"/v0/catalogs/{catalog_id}/uploads",
                   {"numberOfUploadParts": 1})
    upload_id = created.get("id") or created.get("uploadId")
    parts = created.get("presignedUploadParts") or created.get("presignedUploadUrls") or []
    if not upload_id or not parts:
        raise SmapiError(f"upload creation returned nothing usable: {created}")

    first = parts[0]
    url = first.get("url") if isinstance(first, dict) else first
    part_number = first.get("partNumber", 1) if isinstance(first, dict) else 1

    put = urllib.request.Request(url, data=payload, method="PUT")
    try:
        with urllib.request.urlopen(put, timeout=300) as response:
            # S3 quotes the ETag and the completion call wants it exactly as
            # sent back, quotes included.
            etag = response.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        raise SmapiError(f"upload PUT failed: {exc.code}",
                         exc.code, exc.read().decode(errors="replace")[:400]) from exc
    except urllib.error.URLError as exc:
        raise SmapiError(f"upload PUT failed: {exc.reason}") from exc

    call("POST", f"/v0/catalogs/{catalog_id}/uploads/{upload_id}",
         {"partETags": [{"eTag": etag, "partNumber": part_number}]})
    return upload_id


def upload_status(catalog_id: str, upload_id: str) -> dict:
    return call("GET", f"/v0/catalogs/{catalog_id}/uploads/{upload_id}")


def ingestion_verdict(status: dict) -> tuple[str, str]:
    """Reduce an upload status to the one thing that decides whether voice works.

    ER_INGESTION is the gate. SLU_MODELING sits at IN_PROGRESS for weeks and
    never blocks playback, and because the top-level status is pinned by the
    slowest step, that top-level value carries no information at all. Reading
    it as progress is the single most common way to misread this screen.
    """
    steps = {}
    for step in (status.get("ingestionSteps") or []):
        name = step.get("name") or step.get("stepName") or ""
        steps[name] = step.get("status", "")

    gate = steps.get("ER_INGESTION", "")
    if gate == "SUCCEEDED":
        return "ready", "ER_INGESTION succeeded. Voice resolution works."
    if gate in ("FAILED", "ERROR"):
        return "failed", "ER_INGESTION failed. The catalog was not accepted."
    if gate:
        return "waiting", f"ER_INGESTION is {gate}. Voice is not ready yet."
    return "waiting", "No ER_INGESTION step reported yet."
