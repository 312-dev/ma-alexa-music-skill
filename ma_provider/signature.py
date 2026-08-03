"""Verify that a POST /music request really came from Amazon.

Amazon documents request signing for *custom* skills, not for music skills, and
the two envelopes are not the same. The custom-skill recipe ends with a replay
check against `request.timestamp`, and a music directive has no timestamp
anywhere: it is `{header: {namespace, name, messageId, payloadVersion},
payload: {...}}`. So the replay window cannot be implemented, and this module
does not pretend to. Everything up to and including the RSA signature over the
raw body is identical between the two skill types, and captures confirm Amazon
sends `Signature`, `Signature-256` and `SignatureCertChainUrl` to a music
endpoint exactly as it does to a custom one. That part is checked here in full.

What that buys: the endpoint is public, so without this anyone who learns the
hostname can drive the skill and pull signed stream URLs for the whole library
out of it. What it does not buy: replay protection. A captured directive can be
replayed forever. That is worth stating plainly rather than implying otherwise.

## Why the default is `warn` and not `on`

`VERIFY_REQUESTS` is `off` (log nothing but the outcome), `warn` (log a warning
and serve anyway) or `on` (reject with 403). It defaults to **warn**.

Defaulting to `on` would silently break every deployment that upgrades, and a
music skill is uniquely bad at telling you that it broke: `ask smapi
simulate-skill` refuses music skills outright ("Unsupported skill type"), the
developer console Test tab is custom-skill only, and a skill that stops
answering just falls back to the default music provider while Alexa cheerfully
announces someone else's name. There is no way for an operator to confirm
verification works before flipping it on except by saying something to a real
Echo and listening. `warn` lets them watch the log for a day, see every real
Amazon request pass, and then turn it on knowing what will happen.

## Dependency

Full X.509 path validation is not something to hand-roll, so this uses
`cryptography`. Music Assistant already depends on it and the standalone
deployment declares it, so it costs nothing on either side. If it is not
importable this module still imports and `verify()` returns a clear
"unavailable" reason, so the absence of the library degrades to the
pre-existing behavior rather than to a crash on every request.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import posixpath
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Mapping

logger = logging.getLogger("ma-alexa.signature")

try:  # pragma: no cover - the import itself is trivial, its absence is tested
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509.oid import ExtensionOID
    from cryptography.x509.verification import PolicyBuilder, Store

    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTOGRAPHY = False


# Amazon publishes the chain in this bucket and nowhere else. Pinning the host
# is what stops the whole scheme collapsing into "fetch a cert from wherever
# the caller says and trust it".
CERT_HOST = "s3.amazonaws.com"
CERT_PATH_PREFIX = "/echo.api/"
ECHO_SAN = "echo-api.amazon.com"

# The URL is already constrained to Amazon's bucket, so this is a belt on top of
# braces. It costs nothing and means a compromised or misbehaving bucket cannot
# turn one request into an unbounded allocation.
MAX_CERT_BYTES = 128 * 1024
FETCH_TIMEOUT = float(os.environ.get("SIGNATURE_FETCH_TIMEOUT", "4"))

# Initiate has a 100ms p50 budget, so the fetch and the chain walk must happen
# once per certificate rotation and never again. A hit is a dict lookup and one
# RSA verify.
CACHE_TTL = 3600.0
NEGATIVE_TTL = 60.0
CACHE_MAX = 8

_CACHE: dict[str, tuple[float, Any, str]] = {}
_CACHE_LOCK = threading.Lock()

_ANCHORS: list[Any] | None = None
_ANCHORS_LOCK = threading.Lock()


# --- policy -----------------------------------------------------------------


def policy() -> str:
    """Current enforcement policy: off, warn or on.

    Read per call rather than at import so a test or an operator restarting
    with a new value does not need the module reloaded.
    """
    value = (os.environ.get("VERIFY_REQUESTS") or "warn").strip().lower()
    if value in ("off", "warn", "on"):
        return value
    logger.warning("VERIFY_REQUESTS=%r is not off/warn/on, treating as warn", value)
    return "warn"


# --- certificate chain URL --------------------------------------------------


def normalize_cert_url(url: str) -> tuple[str | None, str]:
    """Validate SignatureCertChainUrl and return the canonical form.

    The path is percent-decoded and `..` segments are resolved *before* the
    prefix is compared. Checking the raw path is the classic bypass: the string
    `https://s3.amazonaws.com/echo.api/../evil/cert.pem` starts with
    `/echo.api/` and fetches something else entirely.

    The normalized URL, not the caller's, is what gets fetched and what keys
    the cache, so a thousand spellings of one path cannot evict the real entry
    a thousand times over.
    """
    if not url:
        return None, "missing SignatureCertChainUrl header"
    if "\x00" in url:
        return None, "cert chain url contains a null byte"

    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError as exc:
        return None, f"cert chain url is unparseable: {exc}"

    if parsed.scheme.lower() != "https":
        return None, f"cert chain url scheme is {parsed.scheme!r}, not https"
    if (parsed.hostname or "").lower() != CERT_HOST:
        return None, f"cert chain url host is {parsed.hostname!r}, not {CERT_HOST}"
    if port is not None and port != 443:
        return None, f"cert chain url port is {port}, not 443"

    path = posixpath.normpath(urllib.parse.unquote(parsed.path))
    if not path.startswith(CERT_PATH_PREFIX):
        return None, f"cert chain url path {path!r} is not under {CERT_PATH_PREFIX}"

    return f"https://{CERT_HOST}{path}", "ok"


# --- trust anchors ----------------------------------------------------------


def _load_anchors() -> list[Any]:
    """System root certificates, as x509 objects.

    Read from the platform CA bundle rather than vendoring one, so the image's
    own `ca-certificates` package stays the single source of truth. Tests
    replace this wholesale.
    """
    candidates = []
    env_file = os.environ.get("SSL_CERT_FILE")
    if env_file:
        candidates.append(env_file)
    paths = ssl.get_default_verify_paths()
    candidates.extend([paths.cafile, paths.openssl_cafile])
    candidates.append("/etc/ssl/certs/ca-certificates.crt")

    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "rb") as handle:
                certs = x509.load_pem_x509_certificates(handle.read())
        except Exception:
            logger.warning("could not parse CA bundle %s", candidate)
            continue
        if certs:
            return certs
    return []


def trust_anchors() -> list[Any]:
    global _ANCHORS
    if _ANCHORS is None:
        with _ANCHORS_LOCK:
            if _ANCHORS is None:
                _ANCHORS = _load_anchors()
    return _ANCHORS


# --- chain fetch and validation ---------------------------------------------


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ampere/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read(MAX_CERT_BYTES + 1)
    if len(body) > MAX_CERT_BYTES:
        raise ValueError(f"cert chain exceeds {MAX_CERT_BYTES} bytes")
    return body


def validate_chain(pem: bytes) -> tuple[Any, str]:
    """PEM bundle -> the leaf's public key, or (None, reason).

    Amazon sends leaf first, then intermediates. Every one of these checks is a
    bypass if it is skipped, so none of them are optional:

    - the leaf must be inside its validity window;
    - it must carry echo-api.amazon.com in its SAN, otherwise any certificate
      Amazon's bucket has ever hosted would do;
    - the chain must build to a root we already trust.

    The path validator repeats the first two internally. They are done here as
    well so a failure names itself instead of arriving as one opaque
    VerificationError.
    """
    try:
        certs = x509.load_pem_x509_certificates(pem)
    except Exception as exc:
        return None, f"cert chain is not parseable PEM: {exc}"
    if not certs:
        return None, "cert chain is empty"

    leaf, intermediates = certs[0], certs[1:]

    now = time.time()
    if now < leaf.not_valid_before_utc.timestamp():
        return None, f"leaf certificate is not valid until {leaf.not_valid_before_utc}"
    if now > leaf.not_valid_after_utc.timestamp():
        return None, f"leaf certificate expired at {leaf.not_valid_after_utc}"

    try:
        san = leaf.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
        names = [n.lower() for n in san.get_values_for_type(x509.DNSName)]
    except x509.ExtensionNotFound:
        names = []
    if ECHO_SAN not in names:
        return None, f"leaf certificate SAN {names} does not include {ECHO_SAN}"

    anchors = trust_anchors()
    if not anchors:
        return None, "no system trust anchors available to validate the chain"

    try:
        verifier = (
            PolicyBuilder()
            .store(Store(anchors))
            .build_server_verifier(x509.DNSName(ECHO_SAN))
        )
        verifier.verify(leaf, intermediates)
    except Exception as exc:
        return None, f"cert chain does not validate to a trusted root: {exc}"

    key = leaf.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        return None, "leaf certificate key is not RSA"

    # Never outlive the certificate: a cached key whose cert expired at 03:00
    # must not still be accepting signatures at 03:30.
    expiry = min(now + CACHE_TTL, leaf.not_valid_after_utc.timestamp())
    return key, f"ok:{expiry}"


def public_key_for(url: str) -> tuple[Any, str]:
    """Cached leaf public key for a validated chain URL."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(url)
        if entry and entry[0] > now:
            return entry[1], entry[2]

    try:
        pem = _fetch(url)
    except Exception as exc:
        reason = f"could not fetch cert chain: {exc}"
        _remember(url, now + NEGATIVE_TTL, None, reason)
        return None, reason

    key, reason = validate_chain(pem)
    if key is None:
        _remember(url, now + NEGATIVE_TTL, None, reason)
        return None, reason

    expiry = float(reason.split(":", 1)[1])
    _remember(url, expiry, key, "ok")
    return key, "ok"


def _remember(url: str, expiry: float, key: Any, reason: str) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX and url not in _CACHE:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[url] = (expiry, key, reason)


def clear_cache() -> None:
    """Drop cached chains and trust anchors. For tests and for operators."""
    global _ANCHORS
    with _CACHE_LOCK:
        _CACHE.clear()
    with _ANCHORS_LOCK:
        _ANCHORS = None


# --- the check itself -------------------------------------------------------


def verify(headers: Mapping[str, str], body: bytes) -> tuple[bool, str]:
    """Is this request signed by Amazon? Returns (ok, reason).

    `body` must be the bytes as received. Re-serializing the parsed JSON
    produces a different byte string (key order, separators, unicode escaping)
    and the signature will never match.
    """
    if not HAVE_CRYPTOGRAPHY:
        return False, "unavailable: the cryptography package is not installed"

    lower = {str(k).lower(): v for k, v in dict(headers).items()}

    # Signature-256 is RSA-SHA256 and is what Amazon sends today. The SHA-1
    # header is still accepted because it is still sent alongside, and dropping
    # it outright would fail closed against a device fleet we do not control.
    signature_b64 = lower.get("signature-256")
    algorithm: Any = hashes.SHA256() if signature_b64 else None
    if not signature_b64:
        signature_b64 = lower.get("signature")
        algorithm = hashes.SHA1() if signature_b64 else None
    if not signature_b64:
        return False, "missing Signature-256 and Signature headers"

    url, reason = normalize_cert_url(lower.get("signaturecertchainurl", ""))
    if url is None:
        return False, reason

    key, reason = public_key_for(url)
    if key is None:
        return False, reason

    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return False, f"signature is not valid base64: {exc}"

    try:
        key.verify(raw, body, padding.PKCS1v15(), algorithm)
    except Exception:
        return False, "signature does not match the request body"

    return True, "ok"


def check_request(headers: Mapping[str, str], body: bytes) -> tuple[bool, str]:
    """Apply the VERIFY_REQUESTS policy. Returns (allow, reason).

    `allow` is False only under policy `on` with a failed check, so the caller
    can gate on it unconditionally and let the env var decide what happens.

    Both arguments are required. They used to default to None and fall back to
    reading Flask's ambient request, which was the last thing in this package
    that imported a web framework. Inside Music Assistant that import does not
    resolve, so the fallback was not a convenience but a way for signature
    checking to fail at the exact moment it was asked to do its job.
    """
    mode = policy()
    ok, reason = verify(headers, body)

    if ok:
        if mode != "off":
            logger.debug("request signature verified")
        return True, reason
    if mode == "on":
        logger.warning("rejecting unverified request: %s", reason)
        return False, reason
    if mode == "warn":
        logger.warning("unverified request served anyway (VERIFY_REQUESTS=warn): %s", reason)
    else:
        logger.info("request not verified (VERIFY_REQUESTS=off): %s", reason)
    return True, reason


# A Flask decorator form of check_request lived here and was never used by
# anything. It was the only other reference to a web framework in this package,
# so it went with the fallback above rather than being carried into Music
# Assistant unused. `core.dispatch` gates on check_request directly.
