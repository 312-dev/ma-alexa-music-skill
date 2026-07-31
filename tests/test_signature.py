"""Amazon request verification.

Every step of the chain is a bypass on its own if it is skipped, so each one
gets a test that fails for exactly that reason. The `..` case is the one that
matters most: an unnormalised path check accepts
`https://s3.amazonaws.com/echo.api/../anything/at/all.pem`, which is a
host-pinned URL that fetches an unpinned certificate.

The certificates here are generated per session with `cryptography` and the
trust anchor is monkeypatched, so nothing touches the network or the system CA
store.
"""

from __future__ import annotations

import base64
import datetime

import pytest

import signature

cryptography = pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402


GOOD_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert-7.pem"


# --- certificate fixtures ---------------------------------------------------


def _ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ampere Test Root")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(ca_key, ca_cert, *, san="echo-api.amazon.com", days_from=-1, days_to=30):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=days_from))
        .not_valid_after(now + datetime.timedelta(days=days_to))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _pem(*certs) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


@pytest.fixture(scope="module")
def ca():
    return _ca()


@pytest.fixture(scope="module")
def good_leaf(ca):
    return _leaf(ca[0], ca[1])


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """No cached chain and no ambient policy leaks between tests."""
    signature.clear_cache()
    monkeypatch.delenv("VERIFY_REQUESTS", raising=False)
    yield
    signature.clear_cache()


@pytest.fixture
def amazon(monkeypatch, ca, good_leaf):
    """A working Amazon: trusted root, served chain, and a signing helper."""
    ca_key, ca_cert = ca
    leaf_key, leaf_cert = good_leaf
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca_cert])

    served = {"pem": _pem(leaf_cert, ca_cert), "fetches": 0}

    def fake_fetch(url):
        served["fetches"] += 1
        return served["pem"]

    monkeypatch.setattr(signature, "_fetch", fake_fetch)

    def sign(body: bytes, algorithm=None) -> dict[str, str]:
        algorithm = algorithm or hashes.SHA256()
        raw = leaf_key.sign(body, padding.PKCS1v15(), algorithm)
        header = "Signature-256" if isinstance(algorithm, hashes.SHA256) else "Signature"
        return {header: base64.b64encode(raw).decode(), "SignatureCertChainUrl": GOOD_URL}

    served["sign"] = sign
    return served


# --- cert chain URL ---------------------------------------------------------


def test_good_url_accepted():
    url, reason = signature.normalise_cert_url(GOOD_URL)
    assert url == GOOD_URL
    assert reason == "ok"


def test_dot_dot_escape_is_rejected():
    """The classic bypass: host is pinned, path is not, so nothing is pinned."""
    url, reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com/echo.api/../attacker/cert.pem"
    )
    assert url is None
    assert "/echo.api/" in reason


def test_percent_encoded_dot_dot_is_rejected():
    url, _reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com/echo.api/%2e%2e/attacker/cert.pem"
    )
    assert url is None


def test_dot_dot_that_lands_back_inside_is_accepted():
    """Normalising means resolving, not banning the characters."""
    url, _reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com/echo.api/x/../echo-api-cert-7.pem"
    )
    assert url == "https://s3.amazonaws.com/echo.api/echo-api-cert-7.pem"


def test_wrong_host_rejected():
    url, reason = signature.normalise_cert_url(
        "https://attacker.example.com/echo.api/cert.pem"
    )
    assert url is None
    assert "host" in reason


def test_host_lookalike_prefix_rejected():
    url, _reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com.attacker.example/echo.api/cert.pem"
    )
    assert url is None


def test_host_comparison_is_case_insensitive():
    url, _reason = signature.normalise_cert_url(
        "https://S3.AmazonAWS.com/echo.api/cert.pem"
    )
    assert url == "https://s3.amazonaws.com/echo.api/cert.pem"


def test_plain_http_rejected():
    url, reason = signature.normalise_cert_url(
        "http://s3.amazonaws.com/echo.api/cert.pem"
    )
    assert url is None
    assert "https" in reason


def test_non_443_port_rejected():
    url, reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com:8443/echo.api/cert.pem"
    )
    assert url is None
    assert "port" in reason


def test_explicit_443_accepted():
    url, _reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com:443/echo.api/cert.pem"
    )
    assert url == "https://s3.amazonaws.com/echo.api/cert.pem"


def test_sibling_path_rejected():
    url, _reason = signature.normalise_cert_url(
        "https://s3.amazonaws.com/echo.api.evil/cert.pem"
    )
    assert url is None


def test_missing_url_rejected():
    url, reason = signature.normalise_cert_url("")
    assert url is None
    assert "missing" in reason


# --- chain validation -------------------------------------------------------


def test_valid_chain_yields_a_key(monkeypatch, ca, good_leaf):
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca[1]])
    key, reason = signature.validate_chain(_pem(good_leaf[1], ca[1]))
    assert key is not None
    assert reason.startswith("ok:")


def test_expired_leaf_rejected(monkeypatch, ca):
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca[1]])
    _key, expired = _leaf(ca[0], ca[1], days_from=-40, days_to=-10)
    key, reason = signature.validate_chain(_pem(expired, ca[1]))
    assert key is None
    assert "expired" in reason


def test_not_yet_valid_leaf_rejected(monkeypatch, ca):
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca[1]])
    _key, future = _leaf(ca[0], ca[1], days_from=10, days_to=40)
    key, reason = signature.validate_chain(_pem(future, ca[1]))
    assert key is None
    assert "not valid until" in reason


def test_wrong_san_rejected(monkeypatch, ca):
    """Any cert Amazon's bucket has ever hosted would do without this."""
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca[1]])
    _key, other = _leaf(ca[0], ca[1], san="not-echo-api.example.com")
    key, reason = signature.validate_chain(_pem(other, ca[1]))
    assert key is None
    assert "SAN" in reason


def test_untrusted_root_rejected(monkeypatch, ca, good_leaf):
    """A self-signed chain served from the bucket must not be enough."""
    rogue_key, rogue_cert = _ca()
    monkeypatch.setattr(signature, "trust_anchors", lambda: [rogue_cert])
    key, reason = signature.validate_chain(_pem(good_leaf[1], ca[1]))
    assert key is None
    assert "trusted root" in reason


def test_garbage_chain_rejected(monkeypatch, ca):
    monkeypatch.setattr(signature, "trust_anchors", lambda: [ca[1]])
    key, reason = signature.validate_chain(b"not a certificate")
    assert key is None
    assert "PEM" in reason


def test_no_trust_anchors_is_a_clear_failure(monkeypatch, ca, good_leaf):
    monkeypatch.setattr(signature, "trust_anchors", lambda: [])
    key, reason = signature.validate_chain(_pem(good_leaf[1], ca[1]))
    assert key is None
    assert "trust anchors" in reason


# --- signature verification -------------------------------------------------


def test_good_signature_accepted(amazon):
    body = b'{"header":{"namespace":"Alexa.Media.Playback","name":"Initiate"}}'
    ok, reason = signature.verify(amazon["sign"](body), body)
    assert ok, reason


def test_tampered_body_rejected(amazon):
    body = b'{"header":{"name":"Initiate"}}'
    headers = amazon["sign"](body)
    ok, reason = signature.verify(headers, body + b" ")
    assert not ok
    assert "does not match" in reason


def test_sha1_header_used_when_256_is_absent(amazon):
    body = b'{"a":1}'
    headers = amazon["sign"](body, hashes.SHA1())
    assert "Signature" in headers and "Signature-256" not in headers
    ok, reason = signature.verify(headers, body)
    assert ok, reason


def test_sha256_is_preferred_over_sha1(amazon):
    """A valid SHA-1 header must not rescue a bogus SHA-256 one."""
    body = b'{"a":1}'
    headers = amazon["sign"](body, hashes.SHA1())
    headers["Signature-256"] = base64.b64encode(b"garbage" * 40).decode()
    ok, _reason = signature.verify(headers, body)
    assert not ok


def test_headers_are_matched_case_insensitively(amazon):
    body = b'{"a":1}'
    headers = {k.lower(): v for k, v in amazon["sign"](body).items()}
    ok, reason = signature.verify(headers, body)
    assert ok, reason


def test_missing_signature_headers_rejected(amazon):
    ok, reason = signature.verify({"SignatureCertChainUrl": GOOD_URL}, b"{}")
    assert not ok
    assert "missing Signature" in reason


def test_non_base64_signature_rejected(amazon):
    ok, reason = signature.verify(
        {"Signature-256": "!!!not base64!!!", "SignatureCertChainUrl": GOOD_URL}, b"{}"
    )
    assert not ok
    assert "base64" in reason


def test_bad_cert_url_is_rejected_before_any_fetch(amazon):
    body = b'{"a":1}'
    headers = amazon["sign"](body)
    headers["SignatureCertChainUrl"] = "https://s3.amazonaws.com/echo.api/../x/c.pem"
    ok, _reason = signature.verify(headers, body)
    assert not ok
    assert amazon["fetches"] == 0


def test_chain_is_fetched_once_and_cached(amazon):
    """Initiate has a 100ms p50 budget; a fetch per request would eat it."""
    body = b'{"a":1}'
    headers = amazon["sign"](body)
    for _ in range(5):
        assert signature.verify(headers, body)[0]
    assert amazon["fetches"] == 1


def test_fetch_failure_is_reported_not_raised(monkeypatch, ca, amazon):
    def boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(signature, "_fetch", boom)
    ok, reason = signature.verify(
        {"Signature-256": base64.b64encode(b"x").decode(),
         "SignatureCertChainUrl": GOOD_URL},
        b"{}",
    )
    assert not ok
    assert "could not fetch" in reason


def test_oversized_chain_rejected(monkeypatch):
    monkeypatch.setattr(signature, "MAX_CERT_BYTES", 16)

    class FakeResponse:
        def read(self, n):
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(signature.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(ValueError):
        signature._fetch(GOOD_URL)


# --- policy -----------------------------------------------------------------


def test_default_policy_is_warn(monkeypatch):
    monkeypatch.delenv("VERIFY_REQUESTS", raising=False)
    assert signature.policy() == "warn"


def test_unknown_policy_value_falls_back_to_warn(monkeypatch):
    monkeypatch.setenv("VERIFY_REQUESTS", "yes-please")
    assert signature.policy() == "warn"


def test_policy_off_allows_an_unsigned_request(monkeypatch):
    monkeypatch.setenv("VERIFY_REQUESTS", "off")
    allow, _reason = signature.check_request({}, b"{}")
    assert allow


def test_policy_warn_allows_an_unsigned_request(monkeypatch):
    monkeypatch.setenv("VERIFY_REQUESTS", "warn")
    allow, reason = signature.check_request({}, b"{}")
    assert allow
    assert "missing" in reason


def test_policy_on_blocks_an_unsigned_request(monkeypatch):
    monkeypatch.setenv("VERIFY_REQUESTS", "on")
    allow, _reason = signature.check_request({}, b"{}")
    assert not allow


def test_policy_on_allows_a_signed_request(monkeypatch, amazon):
    monkeypatch.setenv("VERIFY_REQUESTS", "on")
    body = b'{"a":1}'
    allow, reason = signature.check_request(amazon["sign"](body), body)
    assert allow, reason


def test_policy_on_blocks_a_tampered_request(monkeypatch, amazon):
    monkeypatch.setenv("VERIFY_REQUESTS", "on")
    body = b'{"a":1}'
    allow, _reason = signature.check_request(amazon["sign"](body), b'{"a":2}')
    assert not allow


# --- graceful absence of cryptography ---------------------------------------


def test_verify_reports_unavailable_without_cryptography(monkeypatch, amazon):
    monkeypatch.setattr(signature, "HAVE_CRYPTOGRAPHY", False)
    body = b'{"a":1}'
    ok, reason = signature.verify(amazon["sign"](body), body)
    assert not ok
    assert "unavailable" in reason


def test_warn_policy_still_serves_without_cryptography(monkeypatch):
    monkeypatch.setattr(signature, "HAVE_CRYPTOGRAPHY", False)
    monkeypatch.setenv("VERIFY_REQUESTS", "warn")
    allow, reason = signature.check_request({}, b"{}")
    assert allow
    assert "unavailable" in reason


def test_on_policy_fails_closed_without_cryptography(monkeypatch):
    """Asking for enforcement with no verifier available must not serve."""
    monkeypatch.setattr(signature, "HAVE_CRYPTOGRAPHY", False)
    monkeypatch.setenv("VERIFY_REQUESTS", "on")
    allow, _reason = signature.check_request({}, b"{}")
    assert not allow
