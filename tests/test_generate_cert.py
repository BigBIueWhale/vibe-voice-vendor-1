from __future__ import annotations

import json
import stat
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from scripts.generate_cert import _generate_cert, _RequestHandler


class _FakeRfile(BytesIO):
    pass


def _make_handler(method: str, path: str, body: bytes = b"") -> _RequestHandler:
    """Build a _RequestHandler wired to an in-memory wfile."""
    handler: Any = _RequestHandler.__new__(_RequestHandler)
    handler.command = method
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = _FakeRfile(body)
    handler.wfile = BytesIO()
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.server = MagicMock()
    return cast(_RequestHandler, handler)


# ── _generate_cert tests ─────────────────────────────────────────────


def test_generates_cert_and_key(tmp_path: Path) -> None:
    result = _generate_cert("myhost", 365, str(tmp_path / "out"))
    assert "cert_path" in result
    assert "key_path" in result
    assert Path(result["cert_path"]).exists()
    assert Path(result["key_path"]).exists()


def test_cert_has_correct_sans(tmp_path: Path) -> None:
    _generate_cert("example.com", 365, str(tmp_path / "out"))
    cert_pem = (tmp_path / "out" / "fullchain.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_names = san.value.get_values_for_type(x509.DNSName)
    assert "example.com" in dns_names
    assert "localhost" in dns_names


def test_cert_common_name(tmp_path: Path) -> None:
    _generate_cert("myhost.local", 30, str(tmp_path / "out"))
    cert_pem = (tmp_path / "out" / "fullchain.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(cert_pem)
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    assert cn[0].value == "myhost.local"


def test_custom_days(tmp_path: Path) -> None:

    _generate_cert("h", 10, str(tmp_path / "out"))
    cert_pem = (tmp_path / "out" / "fullchain.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(cert_pem)
    delta = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert delta.days == 10


def test_overwrite_refused(tmp_path: Path) -> None:
    certs_dir = str(tmp_path / "out")
    _generate_cert("h", 30, certs_dir)
    result = _generate_cert("h", 30, certs_dir)
    assert "error" in result


def test_key_permissions(tmp_path: Path) -> None:
    _generate_cert("h", 30, str(tmp_path / "out"))
    key_path = tmp_path / "out" / "privkey.pem"
    mode = key_path.stat().st_mode & 0o777
    assert mode == stat.S_IRUSR | stat.S_IWUSR  # 0o600


def test_key_is_valid_ec(tmp_path: Path) -> None:
    _generate_cert("h", 30, str(tmp_path / "out"))
    key_bytes = (tmp_path / "out" / "privkey.pem").read_bytes()
    key = load_pem_private_key(key_bytes, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.key_size == 256  # P-256


# ── HTTP handler tests ───────────────────────────────────────────────


def test_handler_get_root_serves_html() -> None:
    handler = _make_handler("GET", "/")
    handler.do_GET()
    raw = cast(BytesIO, handler.wfile).getvalue().decode()
    assert "200" in raw
    assert "VVV Certificate Generator" in raw


def test_handler_get_unknown_returns_404() -> None:
    handler = _make_handler("GET", "/defaults")
    handler.do_GET()
    raw = cast(BytesIO, handler.wfile).getvalue().decode()
    assert "404" in raw


def test_handler_post_generate(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "hostname": "test.local",
            "days": 30,
            "certs_dir": str(tmp_path / "gen"),
        }
    ).encode()
    handler = _make_handler("POST", "/generate", payload)
    handler.do_POST()
    raw = cast(BytesIO, handler.wfile).getvalue().decode()
    body = raw.split("\r\n\r\n", 1)[1]
    data = json.loads(body)
    assert "cert_path" in data
    assert Path(data["cert_path"]).exists()


def test_handler_post_generate_overwrite(tmp_path: Path) -> None:
    certs_dir = str(tmp_path / "gen")
    _generate_cert("h", 30, certs_dir)
    payload = json.dumps(
        {
            "hostname": "h",
            "days": 30,
            "certs_dir": certs_dir,
        }
    ).encode()
    handler = _make_handler("POST", "/generate", payload)
    handler.do_POST()
    raw = cast(BytesIO, handler.wfile).getvalue().decode()
    body = raw.split("\r\n\r\n", 1)[1]
    data = json.loads(body)
    assert "error" in data


def test_handler_404() -> None:
    handler = _make_handler("GET", "/nonexistent")
    handler.do_GET()
    raw = cast(BytesIO, handler.wfile).getvalue().decode()
    assert "404" in raw
