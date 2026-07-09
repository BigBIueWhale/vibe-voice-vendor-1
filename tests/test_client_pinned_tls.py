from __future__ import annotations

import base64
import ssl
from collections.abc import Coroutine
from pathlib import Path
from unittest.mock import patch

import pytest

from client.client import (
    VibevoiceClient,
    _build_ssl_context,
    _normalize_base_url,
    _normalize_server_pin,
    _spki_pin_from_certificate_der,
)
from scripts.generate_cert import _generate_cert
from scripts.generate_client_cert import _generate_client_auth_artifacts

GOOD_PIN = "sha256/" + base64.b64encode(b"\x01" * 32).decode("ascii")


def _make_client_tls_files(tmp_path: Path) -> dict[str, str]:
    return _generate_client_auth_artifacts(
        certs_dir=str(tmp_path / "certs"),
        keys_dir=str(tmp_path / "keys"),
        subject="client",
        days=30,
    )


def test_server_pin_normalizer_is_exact_sha256_base64() -> None:
    assert _normalize_server_pin(GOOD_PIN + "\n") == GOOD_PIN

    for bad in (
        "sha256/not-base64!",
        "sha256/" + base64.b64encode(b"short").decode("ascii"),
        "sha1/" + base64.b64encode(b"\x01" * 32).decode("ascii"),
        base64.b64encode(b"\x01" * 32).decode("ascii"),
    ):
        with pytest.raises(ValueError):
            _normalize_server_pin(bad)


def test_base_url_is_routing_origin_only() -> None:
    assert _normalize_base_url(" https://example.test:42862/ ") == "https://example.test:42862"
    assert _normalize_base_url("https://192.0.2.10:42862") == "https://192.0.2.10:42862"

    for bad in (
        "http://example.test:42862",
        "https://example.test:42862/v1/transcribe",
        "https://example.test:42862?x=1",
        "https://user@example.test:42862",
        "https://[2001:db8::10]:42862",
    ):
        with pytest.raises(ValueError):
            _normalize_base_url(bad)


def test_generated_server_certificate_pin_matches_spki(tmp_path: Path) -> None:
    generated = _generate_cert(30, str(tmp_path / "server"))
    cert_der = ssl.PEM_cert_to_DER_cert(Path(generated["cert_path"]).read_text())

    assert _spki_pin_from_certificate_der(cert_der) == generated["server_spki_pin"]
    pin_file = Path(generated["server_spki_pin_path"])
    assert pin_file.read_text() == generated["server_spki_pin"] + "\n"


def test_client_tls_context_has_no_ca_or_hostname_authority(tmp_path: Path) -> None:
    tls = _make_client_tls_files(tmp_path)
    context = _build_ssl_context((tls["client_cert_path"], tls["client_key_path"]))

    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.maximum_version == ssl.TLSVersion.TLSv1_3
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_client_requires_server_pin_and_client_certificate(tmp_path: Path) -> None:
    tls = _make_client_tls_files(tmp_path)
    client = VibevoiceClient(
        base_url="https://example.test:42862",
        server_pin=GOOD_PIN,
        cert=(tls["client_cert_path"], tls["client_key_path"]),
    )

    assert client._base_url == "https://example.test:42862"
    assert client._server_pin == GOOD_PIN
    assert client._cert == (tls["client_cert_path"], tls["client_key_path"])


def test_cli_rejects_ca_cert_argument(tmp_path: Path) -> None:
    from client.cli import main

    client_cert = tmp_path / "client-cert.pem"
    client_key = tmp_path / "client-key.pem"
    fake_ca = tmp_path / "ca.pem"
    for path in (client_cert, client_key, fake_ca):
        path.write_text("placeholder")

    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://example.test:42862",
                "--server-pin",
                GOOD_PIN,
                "--ca-cert",
                str(fake_ca),
                "--client-cert",
                str(client_cert),
                "--client-key",
                str(client_key),
                "status",
            ],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 2


def test_cli_passes_pin_and_mtls_paths_to_client(tmp_path: Path) -> None:
    from client.cli import main

    client_cert = tmp_path / "client-cert.pem"
    client_key = tmp_path / "client-key.pem"
    client_cert.write_text("placeholder")
    client_key.write_text("placeholder")
    captured: dict[str, object] = {}

    def spy_init(self: VibevoiceClient, *args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    def close_and_exit(coro: Coroutine[object, object, object]) -> None:
        coro.close()
        raise SystemExit(0)

    with (
        patch(
            "sys.argv",
            [
                "vvv",
                "--server",
                "https://example.test:42862",
                "--server-pin",
                GOOD_PIN,
                "--client-cert",
                str(client_cert),
                "--client-key",
                str(client_key),
                "status",
            ],
        ),
        patch.object(VibevoiceClient, "__init__", spy_init),
        patch("client.cli.asyncio.run", side_effect=close_and_exit),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    assert captured == {
        "base_url": "https://example.test:42862",
        "server_pin": GOOD_PIN,
        "cert": (str(client_cert), str(client_key)),
    }
