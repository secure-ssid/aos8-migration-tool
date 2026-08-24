"""TLS trust-on-first-use / certificate pinning for AOS 8 controllers.

These tests stand up real HTTPS servers with self-signed certificates rather
than mocking, because the whole point of pinning is what the TLS stack does
with a certificate it cannot chain to a CA. A mock would prove nothing.
"""
import datetime
import http.server
import socket
import ssl
import threading

import pytest

from lib.aos8_client import (
    AOS8Client, AOS8TLSError, fetch_cert_fingerprint, normalize_fingerprint,
)

cryptography = pytest.importorskip("cryptography")
from cryptography import x509                                    # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa         # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402


def _make_self_signed(tmp_path, cn="127.0.0.1", tag="a"):
    """Write a throwaway self-signed cert/key pair, like a factory AOS 8 cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"test-{tag}"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(
                __import__("ipaddress").ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"cert-{tag}.pem"
    key_path = tmp_path / f"key-{tag}.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return cert_path, key_path


class _LoginHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for the AOS 8 /v1/api/login endpoint."""

    def do_POST(self):
        body = b'{"_global_result":{"status":0,"UIDARUBA":"tok123"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _TLSServer:
    """A real HTTPS listener bound to an ephemeral localhost port.

    Threaded on purpose: the trust-on-first-use flow opens a second connection
    (to read the certificate) right after the first one fails its handshake, so
    a single-threaded server would serialise them and race.
    """

    def __init__(self, cert_path, key_path):
        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # Failed TLS handshakes are the point of these tests, not noise.
                pass

        self.httpd = _Server(("127.0.0.1", 0), _LoginHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]

    def __enter__(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def certs(tmp_path):
    return _make_self_signed(tmp_path, tag="a")


def _client(port, monkeypatch, **kwargs):
    """AOS8Client pointed at a local test server instead of port 4343."""
    return AOS8Client("127.0.0.1", "admin", "pw", timeout=5, port=port, **kwargs)


# ───────────────── fingerprint helpers ─────────────────

def test_normalize_fingerprint_accepts_any_formatting():
    canonical = "AB" * 32
    assert normalize_fingerprint("ab:" * 31 + "ab") == canonical
    assert normalize_fingerprint("ab ab " + "ab" * 30) == canonical
    assert normalize_fingerprint("") == ""


def test_fetch_cert_fingerprint_matches_served_cert(certs, monkeypatch):
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        fp = fetch_cert_fingerprint("127.0.0.1", port=srv.port, timeout=5)
    # Compare against the DER the cert file actually contains
    import hashlib
    der = x509.load_pem_x509_certificate(
        cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)
    assert normalize_fingerprint(fp) == hashlib.sha256(der).hexdigest().upper()
    assert fp.count(":") == 31, "should be colon-separated for readability"


# ───────────────── the trust-on-first-use flow ─────────────────

def test_selfsigned_raises_recoverable_error_with_fingerprint(certs, monkeypatch):
    """A self-signed controller must not dead-end: the error carries the cert."""
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        client = _client(srv.port, monkeypatch)
        with pytest.raises(AOS8TLSError) as ei:
            client.connect()
        assert ei.value.fingerprint, "operator has nothing to inspect or pin"
        expected = fetch_cert_fingerprint("127.0.0.1", port=srv.port, timeout=5)
        assert ei.value.fingerprint == expected


def test_pinned_fingerprint_connects_to_selfsigned(certs, monkeypatch):
    """Pinning the right cert lets the migration proceed."""
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        fp = fetch_cert_fingerprint("127.0.0.1", port=srv.port, timeout=5)
        client = _client(srv.port, monkeypatch, pin_fingerprint=fp)
        assert client.connect() is True
        assert client.uidaruba == "tok123"


def test_pinning_rejects_a_different_certificate(tmp_path, monkeypatch):
    """The security property: a swapped cert is refused even though both are
    self-signed. This is what ``verify=False`` silently allowed."""
    cert_a, key_a = _make_self_signed(tmp_path, tag="a")
    cert_b, key_b = _make_self_signed(tmp_path, tag="b")
    with _TLSServer(cert_a, key_a) as srv_a:
        good_fp = fetch_cert_fingerprint("127.0.0.1", port=srv_a.port, timeout=5)

    # Same pinned fingerprint, but an impostor now answers on the port.
    with _TLSServer(cert_b, key_b) as srv_b:
        client = _client(srv_b.port, monkeypatch, pin_fingerprint=good_fp)
        with pytest.raises(AOS8TLSError) as ei:
            client.connect()
        assert "does not match the pinned fingerprint" in str(ei.value)
        # No retry loop: a mismatch is not something to click through.
        assert ei.value.fingerprint is None


def test_env_fingerprint_is_honoured(certs, monkeypatch):
    """Headless/Docker runs pin via AOS8_CERT_FINGERPRINT."""
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        fp = fetch_cert_fingerprint("127.0.0.1", port=srv.port, timeout=5)
        monkeypatch.setenv("AOS8_CERT_FINGERPRINT", fp.lower())
        client = _client(srv.port, monkeypatch)
        assert client.pinned_fingerprint == normalize_fingerprint(fp)
        assert client.connect() is True


def test_explicit_pin_beats_env(certs, monkeypatch):
    monkeypatch.setenv("AOS8_CERT_FINGERPRINT", "FF" * 32)
    client = AOS8Client("127.0.0.1", "a", "b", pin_fingerprint="ab:" * 31 + "ab")
    assert client.pinned_fingerprint == "AB" * 32


def test_insecure_flag_still_works_as_an_escape_hatch(certs, monkeypatch):
    """Operators who genuinely want no verification keep that option."""
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        client = _client(srv.port, monkeypatch, verify=False)
        assert client.connect() is True


def test_unreachable_host_reports_no_fingerprint(monkeypatch):
    """Nothing listening => connectivity problem, not a trust decision."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    client = AOS8Client("127.0.0.1", "admin", "pw", timeout=3, port=dead_port)
    with pytest.raises(Exception) as ei:
        client.connect()
    # Either a plain connection error, or a TLS error with nothing to pin.
    if isinstance(ei.value, AOS8TLSError):
        assert ei.value.fingerprint is None


# ───────────────── the whole Step 1 recovery sequence ─────────────────

def test_end_to_end_trust_then_migrate(certs, monkeypatch):
    """What an operator with a factory self-signed controller actually lives:

    connect -> blocked with a fingerprint -> trust it -> connect succeeds.
    Previously this sequence dead-ended and the only way forward was to turn
    verification off entirely.
    """
    cert_path, key_path = certs
    with _TLSServer(cert_path, key_path) as srv:
        # 1. First attempt is refused, but hands back something actionable.
        first = _client(srv.port, monkeypatch)
        with pytest.raises(AOS8TLSError) as ei:
            first.connect()
        offered = ei.value.fingerprint
        assert offered, "no fingerprint => operator is stuck"

        # 2. Operator eyeballs it against 'show crypto pki servercert'.
        assert offered == fetch_cert_fingerprint(
            "127.0.0.1", port=srv.port, timeout=5)

        # 3. Trusting it (what the Step 1 button stores) unblocks the pull.
        second = _client(srv.port, monkeypatch, pin_fingerprint=offered)
        assert second.connect() is True
        assert second.uidaruba == "tok123"

        # 4. And trust is scoped to that cert, not "anything goes".
        assert second.session.verify is False
        assert second.pinned_fingerprint == normalize_fingerprint(offered)
