"""CA_BUNDLE: trust a corporate SSL-inspection root in addition to the public CAs.

Simulates SSL inspection for real: a throwaway "corporate" root CA signs the certificate of
a local HTTPS server, exactly as an inspecting proxy re-signs github.com.
"""

import ipaddress
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from auditor.config import Settings
from auditor.github.client import make_http, ssl_context


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def corp_pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Acme Corp SSL Inspection Root"))
        .issuer_name(_name("Acme Corp SSL Inspection Root"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    tmp = tmp_path_factory.mktemp("pki")
    paths = {"ca": tmp / "corp-ca.pem", "cert": tmp / "server.pem", "key": tmp / "server.key"}
    paths["ca"].write_bytes(_pem(ca_cert))
    paths["cert"].write_bytes(_pem(server_cert))
    paths["key"].write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@pytest.fixture(scope="module")
def inspected_server(corp_pki: dict[str, Path]) -> Iterator[str]:
    """HTTPS server presenting a certificate signed by the corporate root."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # keep test output quiet
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(corp_pki["cert"], corp_pki["key"])
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_inspected_traffic_fails_without_the_bundle(inspected_server: str) -> None:
    with (
        make_http(inspected_server) as http,
        pytest.raises(httpx.ConnectError, match="CERTIFICATE"),
    ):
        http.get("/")


def test_inspected_traffic_succeeds_with_the_bundle(
    inspected_server: str, corp_pki: dict[str, Path]
) -> None:
    with make_http(inspected_server, ca_bundle=corp_pki["ca"]) as http:
        assert http.get("/").json() == {"ok": True}


def test_public_cas_are_still_trusted_with_the_bundle(corp_pki: dict[str, Path]) -> None:
    """Hosts excluded from inspection keep their public certificates; those must still work."""
    default_count = len(ssl_context().get_ca_certs())
    with_bundle = ssl_context(corp_pki["ca"]).get_ca_certs()
    assert len(with_bundle) == default_count + 1
    assert any(
        ("commonName", "Acme Corp SSL Inspection Root") in rdn
        for cert in with_bundle
        for rdn in cert["subject"]
    )


def _settings(**kw: object) -> Settings:
    return Settings(
        _env_file=None,
        github_org="acme",
        github_app_id=1,
        github_installation_id=1,
        github_private_key="PEM",
        database_url="postgresql://x@localhost/x",
        **kw,  # type: ignore[arg-type]
    )


def test_settings_reject_missing_or_invalid_bundle(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="CA bundle file not found"):
        _settings(ca_bundle=tmp_path / "nope.pem")
    junk = tmp_path / "junk.pem"
    junk.write_text("this is not a certificate", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid PEM"):
        _settings(ca_bundle=junk)


def test_settings_accept_valid_bundle(corp_pki: dict[str, Path]) -> None:
    assert _settings(ca_bundle=corp_pki["ca"]).ca_bundle == corp_pki["ca"]
