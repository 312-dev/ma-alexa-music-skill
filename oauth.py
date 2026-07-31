"""Minimal OAuth 2.0 authorization-code provider for Alexa account linking.

Alexa requires account linking before a music skill becomes usable, even for a
private single-user skill. Rather than run a database, every artifact (code,
access token, refresh token) is a self-contained HMAC-signed blob. That keeps
this correct across multiple gunicorn workers, which an in-memory store would
not be.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

SIGNING_KEY = os.environ.get("SIGNING_KEY", "").encode() or os.urandom(32)

CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "ma-alexa")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
# Passphrase typed once in the Alexa app to authorize linking.
LINK_SECRET = os.environ.get("OAUTH_LINK_SECRET", "")

CODE_TTL = 600  # 10 minutes
ACCESS_TTL = int(os.environ.get("OAUTH_ACCESS_TTL", str(30 * 24 * 3600)))

# Amazon posts the redirect back to one of its per-vendor endpoints.
ALLOWED_REDIRECT_PREFIXES = (
    "https://alexa.amazon.com/api/skill/link/",
    "https://pitangui.amazon.com/api/skill/link/",
    "https://layla.amazon.com/api/skill/link/",
    "https://alexa.amazon.co.jp/api/skill/link/",
)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(kind: str, ttl: int, **claims) -> str:
    body = {"k": kind, "exp": int(time.time()) + ttl, **claims}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    payload = _b64e(raw)
    sig = _b64e(hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read(token: str, kind: str) -> dict | None:
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64e(hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body = json.loads(_b64d(payload))
    except Exception:
        return None
    if body.get("k") != kind or body.get("exp", 0) < time.time():
        return None
    return body


def redirect_allowed(uri: str) -> bool:
    return uri.startswith(ALLOWED_REDIRECT_PREFIXES)


def issue_tokens() -> dict:
    return {
        "access_token": mint("access", ACCESS_TTL, sub="owner"),
        "refresh_token": mint("refresh", 365 * 24 * 3600, sub="owner"),
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
    }
